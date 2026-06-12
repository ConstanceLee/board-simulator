import os
import json
import logging
import asyncio
import re
from typing import AsyncGenerator, List, Literal, Dict, Any
from pydantic import BaseModel, Field

import google.auth
from google.adk.agents import BaseAgent
from google.adk.apps import App
from google.adk.events import Event, EventActions
from google.adk.agents.invocation_context import InvocationContext
from google.adk.plugins.save_files_as_artifacts_plugin import SaveFilesAsArtifactsPlugin
from google.adk.agents.context_cache_config import ContextCacheConfig
from google import genai
from google.genai import types

from app.tools import extract_board_paper_async, search_member_news

logger = logging.getLogger("board-simulator.agent")

# Setup GCP credentials
_, project_id = google.auth.default()
os.environ["GOOGLE_CLOUD_PROJECT"] = project_id
os.environ["GOOGLE_CLOUD_LOCATION"] = "global"
os.environ["GOOGLE_GENAI_USE_VERTEXAI"] = "True"

# Define the structured output schema for a single board member's response (Phase 2)
class MemberSimulation(BaseModel):
    name: str = Field(description="The full name of the board member.")
    stance: Literal["Supportive", "Conditionally Supportive", "Likely to Push Back"] = Field(
        description="The member's stance on the uploaded proposal."
    )
    rationale: str = Field(
        description="A concise 2-3 sentence rationale written strictly in the third person (using 'he', 'she', or 'they' to refer to the member) in Australian English. Grounded in their career experience. NEVER use first-person pronouns like 'I', 'my', 'me', or 'we'. The tone must be soft, measured, and professional, expressing a cautious confidence level (e.g. using 'may', 'could', 'appears to', 'seems')."
    )
    focus_points: List[str] = Field(
        description="2-4 specific questions or concerns grounded in their documented expertise, written in Australian English with a soft, polite, and professional tone expressing a cautious confidence level."
    )
    key_request: str = Field(
        description="Specific data or analysis they would seek, written in Australian English with a soft, polite, and professional tone expressing a cautious confidence level."
    )

# Define the structured output schema for Phase 1 classification
class Phase1Classification(BaseModel):
    reasoning: str = Field(
        description="Chain-of-thought logic: Brief analysis of the board paper, explaining why it matches a specific classification, which committees it routes to based on their mandates, and why the sensitivity level is low/medium/high. Must be written strictly in Australian English, with a soft, professional, measured, and cautious confidence level."
    )
    classification: Literal[
        "strategic proposal",
        "capital request",
        "financial approval",
        "operational update",
        "governance matter",
        "other"
    ] = Field(description="The primary classification category for this board paper.")
    committees: List[Literal[
        "Audit & Finance Committee",
        "Risk Committee",
        "Nomination Committee",
        "People Committee",
        "Sustainability Committee"
    ]] = Field(description="The Board committees that would review this submission.")
    sensitivity: Literal["low", "medium", "high"] = Field(
        description="Assessed stakeholder sensitivity level."
    )


class BoardSimulator(BaseAgent):
    """Orchestrates the dynamic PDF/Slides hybrid OCR, stateful multi-turn approval flow,
    and board simulation following the raw instructions.
    """
    
    def _get_client(self) -> genai.Client:
        return genai.Client(vertexai=True, location="global")
        
    def _get_profiles_content(self) -> str:
        current_dir = os.path.dirname(os.path.abspath(__file__))
        cache_path = os.path.join(current_dir, "profiles_cache.md")
        
        if os.path.exists(cache_path):
            logger.info("Loading board member profiles from local cache.")
            with open(cache_path, "r", encoding="utf-8") as f:
                return f.read()
                
        logger.info("Parsing Detailed_Profiles_March_2026.pdf with Gemini...")
        pdf_path = os.path.join(current_dir, "Detailed_Profiles_March_2026.pdf")
        if not os.path.exists(pdf_path):
            raise FileNotFoundError(f"Profiles PDF not found at: {pdf_path}")
            
        with open(pdf_path, "rb") as f:
            pdf_bytes = f.read()
            
        client = self._get_client()
        prompt = (
            "Generate a clean, detailed, and comprehensive Markdown reference file "
            "profiling all the Woolworths Group Board of Directors from this PDF. "
            "For each board member, include their full name, role, appointed tenure, "
            "career background, key strategic focus areas, and known biases or perspectives."
        )
        
        response = client.models.generate_content(
            model="gemini-3.5-flash",
            contents=[
                types.Part.from_bytes(data=pdf_bytes, mime_type="application/pdf"),
                prompt
            ]
        )
        content = response.text or ""
        with open(cache_path, "w", encoding="utf-8") as f:
            f.write(content)
            
        logger.info("Board member profiles parsed and cached successfully.")
        return content

    async def _run_async_impl(
        self, ctx: InvocationContext
    ) -> AsyncGenerator[Event, None]:
        current_dir = os.path.dirname(os.path.abspath(__file__))
        
        # Extract user message parts
        user_message = ""
        uploaded_filename = ""
        uploaded_bytes = None
        board_paper_path = ""
        
        if ctx.user_content and ctx.user_content.parts:
            for part in ctx.user_content.parts:
                if part.text:
                    user_message += part.text + " "
                    # Match placeholder inserted by SaveFilesAsArtifactsPlugin
                    match = re.search(r'\[Uploaded Artifact: "([^"]+)"\]', part.text)
                    if match:
                        uploaded_filename = match.group(1)
                        board_paper_path = uploaded_filename
        user_message = user_message.strip()

        # Check session states
        phase1_approved = ctx.session.state.get("phase1_approved", False)
        awaiting_approval = ctx.session.state.get("awaiting_approval", False)

        # -------------------------------------------------------------
        # STATEFUL TURN CONTROLLER
        # -------------------------------------------------------------
        if awaiting_approval:
            # We are waiting for user to approve Phase 1
            if re.search(r'\b(yes|y|proceed|go|continue|approve|ok)\b', user_message, re.IGNORECASE):
                ctx.session.state["phase1_approved"] = True
                ctx.session.state["awaiting_approval"] = False
                phase1_approved = True
                yield Event(
                    author=self.name,
                    content=types.Content(
                        role="model",
                        parts=[types.Part.from_text(text="Confirmation received. Starting full simulation...")]
                    ),
                    actions=EventActions(
                        state_delta={
                            "phase1_approved": True,
                            "awaiting_approval": False
                        }
                    )
                )
            else:
                yield Event(
                    author=self.name,
                    content=types.Content(
                        role="model",
                        parts=[types.Part.from_text(text="I am waiting for your confirmation. Please say 'yes' or 'proceed' to launch the full board simulation, or let me know what needs to be changed.")]
                    )
                )
                return

        # Load board paper from state or run hybrid extraction (Turn 1)
        board_paper_text = ""
        file_type = ctx.session.state.get("file_type", "")
        extracted_text_filename = ctx.session.state.get("extracted_text_filename", "")
        board_paper_path = ctx.session.state.get("board_paper_path", "")

        # Try to load from cached artifact if filename exists in session state
        if extracted_text_filename and ctx.artifact_service:
            try:
                artifact_part = await ctx.artifact_service.load_artifact(
                    app_name=ctx.app_name,
                    user_id=ctx.user_id,
                    session_id=ctx.session.id,
                    filename=extracted_text_filename
                )
                if artifact_part and artifact_part.inline_data:
                    board_paper_text = artifact_part.inline_data.data.decode("utf-8")
                    logger.info(f"Loaded cached extracted text artifact: {extracted_text_filename}")
            except Exception as e:
                logger.error(f"Failed to load extracted text artifact: {e}")

        # If not loaded from cache, perform extraction
        if not board_paper_text:
            # Retrieve uploaded bytes from artifact service if available
            if uploaded_filename and ctx.artifact_service:
                yield Event(
                    author=self.name,
                    content=types.Content(
                        role="model",
                        parts=[types.Part.from_text(text=f"Retrieving uploaded file: {uploaded_filename}...")]
                    )
                )
                try:
                    artifact_part = await ctx.artifact_service.load_artifact(
                        app_name=ctx.app_name,
                        user_id=ctx.user_id,
                        session_id=ctx.session.id,
                        filename=uploaded_filename
                    )
                    if artifact_part and artifact_part.inline_data:
                        uploaded_bytes = artifact_part.inline_data.data
                except Exception as e:
                    logger.error(f"Failed to load artifact: {e}")

            if not uploaded_bytes:
                # No bytes, fall back to parsing file path from text
                if "gs://" in user_message:
                    match = re.search(r"gs://[^\s]+", user_message)
                    if match:
                        board_paper_path = match.group(0)
                else:
                    board_paper_path = user_message.strip()
                ctx.session.state["board_paper_path"] = board_paper_path
                
                yield Event(
                    author=self.name,
                    content=types.Content(
                        role="model",
                        parts=[types.Part.from_text(text=f"Loading board paper path: {board_paper_path}...")]
                    )
                )
                extraction_result = await extract_board_paper_async(file_path=board_paper_path)
            else:
                yield Event(
                    author=self.name,
                    content=types.Content(
                        role="model",
                        parts=[types.Part.from_text(text="Extracting uploaded file content...")]
                    )
                )
                extraction_result = await extract_board_paper_async(file_bytes=uploaded_bytes, filename=uploaded_filename)

            if extraction_result["status"] == "error":
                yield Event(
                    author=self.name,
                    content=types.Content(
                        role="model",
                        parts=[types.Part.from_text(text=f"❌ Error extracting board paper: {extraction_result.get('message')}")]
                    ),
                    actions=EventActions(escalate=True)
                )
                return

            board_paper_text = extraction_result["text"]
            file_type = extraction_result["file_type"]
            ctx.session.state["file_type"] = file_type
            ctx.session.state["board_paper_path"] = board_paper_path
            
            # Save extracted text to artifact service to avoid large string in session state
            extracted_text_filename = f"extracted_text_{ctx.session.id}.txt"
            ctx.session.state["extracted_text_filename"] = extracted_text_filename
            if ctx.artifact_service:
                try:
                    part = types.Part.from_bytes(data=board_paper_text.encode("utf-8"), mime_type="text/plain")
                    await ctx.artifact_service.save_artifact(
                        app_name=ctx.app_name,
                        user_id=ctx.user_id,
                        session_id=ctx.session.id,
                        filename=extracted_text_filename,
                        artifact=part
                    )
                    logger.info(f"Saved extracted text artifact: {extracted_text_filename}")
                except Exception as e:
                    logger.error(f"Failed to save extracted text artifact: {e}")

        # -------------------------------------------------------------
        # PHASE 1: INTAKE AND CLASSIFICATION (Turn 1)
        # -------------------------------------------------------------
        if not phase1_approved:
            yield Event(
                author=self.name,
                content=types.Content(
                    role="model",
                    parts=[types.Part.from_text(text="Performing Phase 1 Intake & Classification...")]
                )
            )
            
            phase1_prompt = f"""
You are the Woolworths Group Virtual Board Paper Reviewer, an expert in ASX-listed corporate governance.
Review the submitted board paper and classify it, determine likely committee routing, and assess stakeholder sensitivity.

Use the following guidelines to ensure accuracy:

1. Classification Categories:
- "strategic proposal": Major initiatives, M&A, entry into new markets, structural adjustments, or significant new partnerships.
- "capital request": Requests for capital expenditure, project funding, property acquisitions, or infrastructure investments.
- "financial approval": Approvals of financial statements, dividend policy, debt facilities, refinancing, or material financial commitments.
- "operational update": Business performance, project progress, supply chain, store network updates, safety reports, or customer metrics.
- "governance matter": Board composition, director selection, board evaluations, revisions to charters/policies, remuneration reports, or structural compliance.
- "other": Matters not fitting the above.

2. Board Committee Routing Criteria (a paper can route to multiple committees):
- "Audit & Finance Committee": Financial performance, capital structure, debt/refinancing, budget approvals, external/internal audit, accounting policies, taxation, and financial disclosures.
- "Risk Committee": Non-financial risks, regulatory inquiries/compliance breaches (e.g., ACCC, Senate inquiries), legal disputes, workplace health & safety (WHS), cybersecurity, insurance, and brand reputation risks.
- "Nomination Committee": Board renewal, board succession, director recruitment, board committee composition, and independent director reviews.
- "People Committee": Executive remuneration, enterprise agreements (EAs), talent and culture strategy, diversity, employee engagement, and senior leadership succession.
- "Sustainability Committee": Environmental issues, climate change targets/reporting, ethical sourcing, modern slavery statement, animal welfare, packaging commitments, and community investment.

3. Stakeholder Sensitivity:
- "low": Routine updates, standard compliance reports, or minor financial approvals with no significant public interest or customer trust impact.
- "medium": Material strategic changes, moderate financial impact, or projects that involve public/community impact but are manageable within existing business processes.
- "high": Serious regulatory investigations/scrutiny (e.g., ACCC pricing inquiries, Senate hearings), material compliance/governance failures, major executive remuneration changes, large-scale workplace safety/underpayment issues, or decisions with high risk of severe public/political backlash affecting customer trust.

Submission Content:
{board_paper_text}

Analyze the submission and output a valid JSON object matching the requested schema.
The reasoning must be written in a soft, professional, and measured tone, using a cautious confidence level (e.g. using hedging language like 'may', 'could', 'appears to', 'seems to indicate').
Provide your reasoning strictly in Australian English (e.g. using 'organisation', 'remediation', 'behaviour', 'programme', 'summarise', 'categorise', 'prioritise', 'modelling' and avoiding American spellings like 'organization', 'behavior', 'program', 'summarize', 'categorize', 'prioritize', 'modeling').
"""
            client = self._get_client()
            try:
                response = client.models.generate_content(
                    model="gemini-3.5-flash",
                    contents=phase1_prompt,
                    config=types.GenerateContentConfig(
                        response_mime_type="application/json",
                        response_schema=Phase1Classification
                    )
                )
                p1_data = json.loads(response.text)
                ctx.session.state["phase1_data"] = p1_data
                ctx.session.state["awaiting_approval"] = True
                
                classification = p1_data.get("classification")
                committees_list = ", ".join(p1_data.get("committees", []))
                reasoning = p1_data.get("reasoning", "No detailed reasoning provided.")
                
                state_delta = {
                    "phase1_data": p1_data,
                    "awaiting_approval": True,
                    "extracted_text_filename": extracted_text_filename,
                    "file_type": file_type,
                    "board_paper_path": board_paper_path
                }
                
                # Output Stage 1 Classification directly & prompt for approval
                yield Event(
                    author=self.name,
                    content=types.Content(
                        role="model",
                        parts=[
                            types.Part.from_text(text=f"### **Phase 1: Intake & Classification**\n"
                                                      f"* **Classification**: {classification.title()}\n"
                                                      f"* **Likely Committees**: {committees_list}\n"
                                                      f"* **Stakeholder Sensitivity**: {p1_data.get('sensitivity').upper()}\n"
                                                      f"* **Reasoning**: {reasoning}\n\n"
                                                      f"\u003e *I have classified this as {classification}. It would likely go through {committees_list}. Shall I proceed with the full simulation?*")
                        ]
                    ),
                    actions=EventActions(state_delta=state_delta)
                )
            except Exception as e:
                logger.error(f"Phase 1 execution failed: {e}")
                yield Event(
                    author=self.name,
                    content=types.Content(
                        role="model",
                        parts=[types.Part.from_text(text=f"❌ Phase 1 execution failed: {e}")]
                    ),
                    actions=EventActions(escalate=True)
                )
            return

        # -------------------------------------------------------------
        # PHASE 2: DIRECTOR-BY-DIRECTOR ANALYSIS (Turn 2)
        # -------------------------------------------------------------
        yield Event(
            author=self.name,
            content=types.Content(
                role="model",
                parts=[types.Part.from_text(text="Analyzing baseline board profiles and starting Director Persona Simulations...")]
            )
        )
        
        try:
            profiles_text = self._get_profiles_content()
        except Exception as e:
            yield Event(
                author=self.name,
                content=types.Content(
                    role="model",
                    parts=[types.Part.from_text(text=f"❌ Error loading baseline profiles: {e}")]
                ),
                actions=EventActions(escalate=True)
            )
            return

        board_members = [
            "Scott Perkins",
            "Amanda Bardwell",
            "Maxine Brenner",
            "Jennifer Carr-Smith",
            "Philip Chronican",
            "Kathee Tesija",
            "Warwick Bray",
            "Ken Meyer",
            "Jon Alferness"
        ]

        client = self._get_client()
        yield Event(
            author=self.name,
            content=types.Content(
                role="model",
                parts=[types.Part.from_text(text="🔍 Performing targeted search & simulating stances in parallel...")]
            )
        )

        async def simulate_member(name: str) -> MemberSimulation:
            grounding_context = ""
            try:
                grounding_prompt = f"Find recent 2026 news, comments, stances, and board views for Woolworths Group director {name}."
                response = await client.aio.models.generate_content(
                    model="gemini-3.5-flash",
                    contents=grounding_prompt,
                    config=types.GenerateContentConfig(
                        tools=[types.Tool(google_search=types.GoogleSearch())],
                    )
                )
                grounding_context = response.text or ""
            except Exception as e:
                logger.warning(f"Google Search grounding failed for {name}: {e}. Using fallback database.")
                search_res = search_member_news(name, "recent news stances board views")
                grounding_context = "\n".join(search_res.get("results", []))
            
            simulation_prompt = f"""
You are simulating a professional board meeting of the Woolworths Group.
Roleplay exactly as the board member: {name}.

Here is your known background, tenure, and strategic focus area extracted from the official board profiles:
{profiles_text}

Here are recent news, comments, and stances related to your role (grounding context):
{grounding_context}

Here is the newly uploaded Board Paper / Proposal for the meeting:
{board_paper_text}

Based on your background and perspectives:
1. Decide your stance on the proposal. Must select exactly one of: Supportive, Conditionally Supportive, or Likely to Push Back.
2. Write a concise 2-3 sentence rationale. Ground it strictly in your background (e.g., if you are a tech expert, reflect on technology viability; if you are a banker, focus on risk/governance; if you are a retail operations expert, focus on store margins). The rationale MUST be written strictly in the third person. Refer to the board member by name or by third-person pronouns ('he', 'she', or 'they'). NEVER use first-person pronouns (I, my, me, we, etc.) in the rationale.
3. Formulate 2-4 specific, realistic questions or concerns you will raise during the meeting.
4. Formulate 1 specific data or analysis request you would seek.

Tone & Confidence Level:
The tone of your entire response (rationale, questions, and requests) must be soft, measured, and professional. Avoid overly aggressive, definitive, or absolute statements. Express stances and rationales with a softer, more tentative confidence level (e.g. use hedging words such as 'may', 'could', 'potentially', 'appears to', 'seems to indicate', 'seeks to understand', 'raises a query regarding', rather than 'will', 'must', 'clearly shows', 'violates'). Ensure the questions and concerns are framed constructively and politely.

STRICT NUMERICAL REFERENCES RULE:
If the board paper contains financial results or numeric targets, you MUST extract and reference specific numerical data points (percentages, dollar figures, basis point movements) in your rationale and questions. Generic observations are not allowed.
WARWICK BRAY SPECIFICS: If you are Warwick Bray, you MUST query specific line items and reference numerical values (basis points, dollar amounts, percentage movements) from the board paper. Do not ask thematic questions.

Provide your response strictly in Australian English. Use spelling like 'organisation', 'remediation', 'behaviour', 'programme', 'summarise', 'categorise', 'modelling', 'prioritise', 'licence', 'labour', 'centre'. Avoid American English spelling entirely (do NOT use 'organization', 'behavior', 'program', 'summarize', 'categorize', 'modeling', 'prioritize', 'license', 'labor', 'center').
Provide your response as a structured JSON matching this schema:
{{
  "name": "{name}",
  "stance": "Supportive | Conditionally Supportive | Likely to Push Back",
  "rationale": "...",
  "focus_points": ["Question 1", "Question 2"],
  "key_request": "..."
}}
"""
            try:
                response = await client.aio.models.generate_content(
                    model="gemini-3.5-flash",
                    contents=simulation_prompt,
                    config=types.GenerateContentConfig(
                        response_mime_type="application/json",
                        response_schema=MemberSimulation,
                    )
                )
                res_data = json.loads(response.text)
                return MemberSimulation.model_validate(res_data)
            except Exception as e:
                logger.error(f"Failed to simulate board member {name}: {e}")
                return MemberSimulation(
                    name=name,
                    stance="Conditionally Supportive",
                    rationale="The director requires further details and risk assessment concerning operational overhead.",
                    focus_points=[f"What are the immediate implementation timelines and costs for the proposal?"],
                    key_request="Provide a detailed cost-benefit analysis."
                )

        # Execute all simulations concurrently
        tasks = [simulate_member(name) for name in board_members]
        simulated_members = await asyncio.gather(*tasks)

        # -------------------------------------------------------------
        # PHASE 3 & 4: CROSS-CUTTING THEMES, VULNERABILITIES & RECOMMENDATIONS (Turn 2)
        # -------------------------------------------------------------
        yield Event(
            author=self.name,
            content=types.Content(
                role="model",
                parts=[types.Part.from_text(text="📊 Synthesizing final board dynamics, vulnerabilities, and executive recommendations...")]
            )
        )

        # Convert simulated members to text for synthesis
        simulated_text = json.dumps([m.model_dump() for m in simulated_members], indent=2)

        synthesis_prompt = f"""
You are the Woolworths Group Virtual Board Paper Reviewer.
You are synthesizing the results of the board simulation for the submitted board paper.

Board Paper Text:
{board_paper_text}

Simulated Director Stances & Focus Points:
{simulated_text}

Generate the final synthesis report covering Phase 3 and Phase 4.

### Phase 3: Cross-Cutting Themes and Vulnerabilities
1. Cross-Cutting Themes: Identify 3–5 themes where multiple directors converge, explicitly specifying which directors and why it matters.
2. Critical Vulnerabilities: Identify 2–3 vulnerabilities most likely to derail approval, including the specific risk and the recommended mitigation.
3. Director Tensions: Identify any director tensions, specifying exactly which directors are involved and why.
4. Overall Assessment: Rate the likelihood of approval as High, Medium, or Low along with detailed reasoning.
   - Crucial Dominant Risk Rule: This assessment must reflect the dominant risk in the paper, not just the quality of operating results. If the paper contains a significant items charge, remediation provision, regulatory exposure, or trust deficit, the overall assessment MUST reflect "Medium" or "Medium-High" at best, regardless of underlying financial performance.

### Phase 4: Recommendations (Actionable advice for the executive team)
1. Proposal Modifications: Specific steps to strengthen the submission.
2. Additional Analysis: Specific data or modelling to prepare.
   - Gaps/Weaknesses constraint: Identify specific gaps or weaknesses in the paper's current analytical framing. For each item, explain WHAT is missing or conflated and WHY the board cannot make a confident assessment without it.
3. Hardest Questions: List the 3–5 hardest questions the board will ask, alongside suggested executive responses.
4. Director Navigation: Outline 2–3 specific, non-obvious dynamics between directors or between directors and management that could affect how this paper lands. Focus on relationships, tensions, or sequencing. Do not include general presentation advice.

STRICT RECOMMENDATION QUALITY FILTER & EXCLUSIONS:
Apply this filter before including any recommendation or presentation strategy: "Would a competent, experienced executive team at a major ASX company already be doing this as a matter of course?" If yes — do not include it.
Do NOT recommend any of the following:
- Pre-briefing the Chair.
- Pre-briefing the Audit & Finance Chair.
- Ensuring directors have received pre-reading materials.
- Having the CFO handle detailed financial questions.
- Linking strategy to financial outcomes in board papers.
- Acknowledging challenges in the CEO's opening remarks.
- Directing how the CEO should open remarks or opening scripts.
- CFO preparing supplementary analysis.
- Integrating new directors.

Tone & Confidence Level:
The tone of the entire synthesis report must be soft, measured, and professional. Frame all themes, vulnerabilities, tensions, and assessments with a softer confidence level, using appropriate hedging language (e.g., 'suggests', 'likely to encounter', 'might pose a challenge', 'appears to converge', rather than 'is', 'will fail', 'are in conflict'). Ensure that any criticisms or risks are presented constructively and neutrally.

Provide the entire output strictly in Australian English. Use spelling like 'organisation', 'remediation', 'behaviour', 'programme', 'summarise', 'categorise', 'modelling', 'prioritise', 'licence', 'labour', 'centre'. Avoid American English spelling entirely (do NOT use 'organization', 'behavior', 'program', 'summarize', 'categorize', 'modeling', 'prioritize', 'license', 'labor', 'center').
Do not include any conversational introduction or meta-commentary. Start directly with "## Phase 3: Cross-Cutting Themes and Vulnerabilities".
"""
        
        try:
            response = client.models.generate_content(
                model="gemini-3.5-flash",
                contents=synthesis_prompt
            )
            synthesis_markdown = response.text or ""
        except Exception as e:
            logger.error(f"Synthesis generation failed: {e}")
            synthesis_markdown = "## Error during synthesis generation."

        # Collate Phase 2 results into Markdown
        md_lines = []
        md_lines.append("# Woolworths Group Board Simulation Report")
        md_lines.append(f"* **Simulation Date**: March 2026")
        md_lines.append(f"* **Analyzed Board Paper**: `{ctx.session.state.get('board_paper_path') or uploaded_filename}`\n")
        
        md_lines.append("## **Executive Stances Summary**\n")
        md_lines.append("| Board Member | Role | Simulated Stance |")
        md_lines.append("| --- | --- | --- |")
        
        roles_map = {
            "Scott Perkins": "Independent Chair of the Board",
            "Amanda Bardwell": "Chief Executive Officer (CEO)",
            "Maxine Brenner": "Non-Executive Director (Risk & Gov)",
            "Jennifer Carr-Smith": "Non-Executive Director (E-commerce)",
            "Philip Chronican": "Non-Executive Director (Governance)",
            "Kathee Tesija": "Non-Executive Director (Merchandising & Supply Chain)",
            "Warwick Bray": "Non-Executive Director (Audit & Finance Chair)",
            "Ken Meyer": "Non-Executive Director (Operations)",
            "Jon Alferness": "Non-Executive Director (Retail AI)"
        }
        
        for sim in simulated_members:
            role = roles_map.get(sim.name, "Non-Executive Director")
            stance_emoji = "✅" if sim.stance == "Supportive" else "⚠️" if sim.stance == "Conditionally Supportive" else "🛑"
            md_lines.append(f"| **{sim.name}** | {role} | {stance_emoji} **{sim.stance}** |")
            
        md_lines.append("\n---\n")
        md_lines.append("## **Phase 2: Detailed Individual Responses**\n")
        
        for sim in simulated_members:
            role = roles_map.get(sim.name, "Non-Executive Director")
            md_lines.append(f"### **{sim.name}**")
            md_lines.append(f"* **Role**: *{role}*")
            md_lines.append(f"* **Stance**: **{sim.stance}**")
            md_lines.append(f"* **Rationale**: {sim.rationale}")
            md_lines.append("* **Questions & Concerns Raised**:")
            for q in sim.focus_points:
                md_lines.append(f"  - \"{q}\"")
            md_lines.append(f"* **Key Data Request**: \"{sim.key_request}\"")
            md_lines.append("")
            
        md_lines.append("\n---\n")
        md_lines.append(synthesis_markdown)
        
        report_md = "\n".join(md_lines)
        
        # Save output locally as an artifact
        artifacts_dir = os.path.join(current_dir, "artifacts")
        os.makedirs(artifacts_dir, exist_ok=True)
        report_path = os.path.join(artifacts_dir, "board_simulation_report.md")
        with open(report_path, "w", encoding="utf-8") as f:
            f.write(report_md)
            
        # Clean session state variables to allow starting over
        ctx.session.state["phase1_approved"] = False
        ctx.session.state["awaiting_approval"] = False
        ctx.session.state["extracted_text_filename"] = ""
        ctx.session.state["file_type"] = ""
        ctx.session.state["board_paper_path"] = ""
        
        # Yield the final report
        yield Event(
            author=self.name,
            content=types.Content(
                role="model",
                parts=[types.Part.from_text(text=f"### board_simulation_report.md generated successfully!\n\n{report_md}")]
            ),
            actions=EventActions(
                state_delta={
                    "phase1_approved": False,
                    "awaiting_approval": False,
                    "extracted_text_filename": "",
                    "file_type": "",
                    "board_paper_path": ""
                }
            )
        )

# Initialize the root agent
root_agent = BoardSimulator(name="board_simulator")

# Wrap in App
app = App(
    root_agent=root_agent,
    name="app",
    plugins=[SaveFilesAsArtifactsPlugin()],
    context_cache_config=ContextCacheConfig(
        min_tokens=2048,
        ttl_seconds=1800
    )
)
