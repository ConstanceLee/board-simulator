# Woolworths Group Board Simulator

The **Woolworths Group Virtual Board Paper Reviewer** (Board Simulator) is an agentic system built on the Google Agent Development Kit (ADK) framework. It ingests complex, visual corporate board papers (traditional PDFs, dense PowerPoint slides, etc.), classifies proposals and determines committee routing, and simulates a mock board meeting with 9 Woolworths Group directors using real-time search grounding and persona voice discipline.

---

## 1. System Architecture

The Board Simulator runs as a stateful, event-driven agent exposed via a custom FastAPI web server that hosts both the API and the playground web UI on the same port.

```mermaid
graph TD
    Client[Client / UI] -->|API Request / SSE| FastAPI[FastAPI App (Port 8000)]
    FastAPI -->|Runs Agent| ADK[ADK Runner]
    ADK -->|Invokes| Agent[BoardSimulator Agent]
    Agent -->|State Management| Session[InMemorySessionService]
    Agent -->|File Storage| Artifacts[InMemoryArtifactService / Local Disk]
    Agent -->|Model Calls| Gemini[Gemini 3.5 Flash]
```

### Core Components
- **`BoardSimulator` Agent (`app/agent.py`)**: Inherits from `BaseAgent`. Orchestrates the multi-turn review, parses board papers, launches parallel director simulations, and generates the final synthesis report.
- **FastAPI Integration (`app/fast_api_app.py`)**: Exposes the ADK agent via Server-Sent Events (SSE) `/run_sse` endpoints, serves the Angular playground UI, exposes `/feedback` to collect structure telemetry, and hosts a custom `/download_artifact/{filename}` endpoint to serve generated report files.
- **Session & Artifact Services**: In-memory session service (`InMemorySessionService`) is used locally to store session state. Local files are written to `app/artifacts/` and registered with the artifact service.

---

## 2. Ingestion & Hybrid OCR Pipeline

To review board papers of varying visual formats, the system implements a hybrid text extraction strategy.

```mermaid
flowchart TD
    Start([Receive Board Paper]) --> Source{Source Type?}
    Source -->|Local Path / GCS| ReadBytes[Read Bytes]
    Source -->|Google Slides URL| DownloadPDF[Download as PDF]
    
    ReadBytes & DownloadPDF --> Ext{File Extension?}
    Ext -->|PDF| PDFPipeline[PDF Processing]
    Ext -->|PPTX| PPTXPipeline[LibreOffice PDF Conversion]
    
    PPTXPipeline -->|Success| PDFPipeline
    PPTXPipeline -->|Fallback| ShapeExtract[Direct Shapes Text Extraction]
    
    PDFPipeline --> NativeCheck{Native Text Length < 150 Chars?}
    NativeCheck -->|No| KeepNative[Use Native Text]
    NativeCheck -->|Yes| VisionOCR[Render Page to JPG + Vision OCR via Gemini]
    
    KeepNative & VisionOCR & ShapeExtract --> Combine[Aggregate Pages into Markdown]
    Combine --> End([Extraction Complete])
```

### Hybrid OCR Highlights:
- **Low-Text Page Detection**: Pages with fewer than 150 native characters are treated as images (e.g., charts, diagrams, slide graphics).
- **Concurrency Management**: Uses an `asyncio.Semaphore` with a limit of **10** parallel vision OCR workers to prevent triggering Gemini API rate limits.
- **PPTX Fidelity**: Headless `LibreOffice` converts PowerPoint slides to high-resolution PDFs before rendering to ensure visual elements and layout structures are fully captured.

---

## 3. Stateful Multi-Turn Workflow

The simulation requires a strict confirmation step before executing the computationally expensive simulation of all 9 directors. This is implemented via stateful turns.

### State Transitions
- **`awaiting_approval`**: When `True`, the agent halts execution after Phase 1 and waits for user input.
- **`phase1_approved`**: Set to `True` when the user submits a positive response (`yes`, `proceed`, etc.), triggering Phase 2.

```mermaid
stateDiagram-v2
    [*] --> Idle
    Idle --> Turn1 : Upload Board Paper
    Turn1 --> AwaitingApproval : Perform Phase 1 Intake & output Classification
    AwaitingApproval --> Turn2 : User sends "yes" or "proceed"
    AwaitingApproval --> Idle : User rejects or stops
    Turn2 --> Idle : Simulates 9 Directors, yields report, resets state
```

---

## 4. Simulation Phases & Execution

### Phase 1: Intake & Classification (Turn 1)
- **Prompting**: Analyzes paper content against specific categories and routing criteria.
- **Pydantic Validation**: Uses a Pydantic schema `Phase1Classification` mapped to Gemini's `response_schema` to enforce structured JSON output.
- **Chain-of-Thought Reasoning**: Instructs the model to output a detailed `reasoning` field describing its logical justification before selecting fields, improving classification and committee routing accuracy.

### Phase 2: Director Persona Simulation (Turn 2)
- **Parallel Roleplay**: Spawns concurrent execution tasks (`asyncio.gather`) for all 9 directors.
- **Real-Time Grounding**: Integrates Google Search tool queries for each director to retrieve live 2026 news, perspectives, and stances, falling back to a structured local database if the search fails.
- **Voice Discipline**: Constraints the output using `MemberSimulation` schema. Enforces strict numerical reference rules (e.g., Warwick Bray must query specific dollar values and basis point movements). The rationale MUST be written strictly in the third person using 'he', 'she', or 'they' (first-person references such as 'I' or 'my' are strictly forbidden).

### Phase 3 & 4: Synthesis & Recommendations (Turn 2)
- **Consolidation**: Gathers the outcomes of all simulated director responses and the raw board paper.
- **Vulnerabilities**: Analyzes critical vulnerabilities (e.g., trust deficit or governance failures) and rates the likelihood of approval.
- **Obvious Filter**: Applies a strict quality filter to recommendation output to exclude standard corporate behaviors (e.g., recommending to pre-brief the Chair).
- **Artifact Export**: Saves the final report to `app/artifacts/Woolworths_Board_Simulation_Report.md` and generates a styled Word document `Woolworths_Board_Simulation_Report.docx` based on the Woolworths template.

---

## 5. Future Enhancements

We identify the following enhancement opportunities to further improve accuracy, latency, and features:

### A. Dynamic Committee Member Matching
*   **Current State**: All 9 directors are simulated for every proposal in Turn 2.
*   **Enhancement**: Map Phase 1 routed committees directly to their respective director members (e.g., if only routed to the *People Committee*, dynamically include only members of that committee in Phase 2). This reduces unnecessary API calls and latency.

### B. Persistent Session & State Storage
*   **Current State**: Uses an in-memory session manager (`use_local_storage=False`), meaning restarts wipe agent state.
*   **Enhancement**: Integrate a persistent storage database (e.g., PostgreSQL or Firestore) for ADK session service so simulations and states can be restored across restarts.

### C. Live Profile Scraping & Updates
*   **Current State**: Director baselines are parsed from a static PDF (`Detailed_Profiles_March_2026.pdf`).
*   **Enhancement**: Build a tool to scrape official Woolworths Group corporate governance portals to dynamically update director profiles, tenure, and committee roles in real-time.

### D. Multi-modal Board Paper Synthesis
*   **Current State**: Ingestion extracts text or OCR descriptions as Markdown before passing to the simulation prompt.
*   **Enhancement**: Directly send relevant paper/slide page image bytes along with the text during simulation steps, letting Gemini utilize its native multimodal vision capabilities to interpret charts and layout structures directly.

### E. Search Query Parallelization & Caching
*   **Current State**: Google Searches for directors are run sequentially or in parallel inside Turn 2.
*   **Enhancement**: Pre-fetch or cache grounding context for all directors once the paper is uploaded in Turn 1, significantly cutting down Phase 2 response latency.

---

## Requirements

Before you begin, ensure you have:
- **uv**: Python package manager - [Install](https://docs.astral.sh/uv/getting-started/installation/)
- **agents-cli**: Agents CLI - Install with `uv tool install google-agents-cli`
- **Google Cloud SDK**: For GCP services - [Install](https://cloud.google.com/sdk/docs/install)

## Quick Start

Install `agents-cli` and its skills if not already installed:
```bash
uvx google-agents-cli setup
```

Install required packages:
```bash
agents-cli install
```

Start the playground with the custom FastAPI app (serves UI and API on port 8000):
```bash
uv run python app/fast_api_app.py
```
Open **[http://127.0.0.1:8000/dev-ui/](http://127.0.0.1:8000/dev-ui/)** in your browser to interact with the board simulator.

## Commands

| Command | Description |
| --- | --- |
| `agents-cli install` | Install dependencies using uv |
| `uv run python app/fast_api_app.py` | Launch custom FastAPI playground server |
| `agents-cli lint` | Run code quality checks |
| `uv run pytest tests/unit` | Run unit tests |

## Deployment

```bash
gcloud config set project <your-project-id>
agents-cli deploy
```

## Observability

Built-in telemetry exports to Cloud Trace, BigQuery, and Cloud Logging.
