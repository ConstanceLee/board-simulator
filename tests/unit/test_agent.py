import os
import json
import re
import pytest
from unittest.mock import MagicMock, patch, AsyncMock

# Mock google.auth.default to prevent auth during imports
with patch("google.auth.default", return_value=(None, "mock-project-id")):
    from google.adk.agents.invocation_context import InvocationContext
    from google.adk.events import Event
    from google.adk.sessions import InMemorySessionService
    from google.adk.artifacts import InMemoryArtifactService
    from google.genai import types
    from app.agent import BoardSimulator, MemberSimulation

@pytest.mark.asyncio
async def test_board_simulator_workflow() -> None:
    """Test the complete stateful board simulator workflow:
    Turn 1: Intake & Classification, then halt.
    Turn 2: Confirm with 'yes' and execute simulations + synthesis.
    """
    session_service = InMemorySessionService()
    await session_service.create_session(
        app_name="app",
        user_id="test_user",
        session_id="session_123"
    )
    
    artifact_service = InMemoryArtifactService()
    # Save a fake PDF file as an artifact
    part = types.Part(
        inline_data=types.Blob(mime_type="application/pdf", data=b"fake-pdf-data")
    )
    await artifact_service.save_artifact(
        app_name="app",
        user_id="test_user",
        session_id="session_123",
        filename="board_paper.pdf",
        artifact=part
    )
    
    # ---------------------------------------------------------
    # TURN 1: Initial upload with artifact placeholder
    # ---------------------------------------------------------
    user_content_t1 = types.Content(
        role="user",
        parts=[types.Part(text='[Uploaded Artifact: "board_paper.pdf"]')]
    )
    
    agent = BoardSimulator(name="board_simulator")
    
    # Mock profiles extraction
    agent._get_profiles_content = MagicMock(return_value="Board Member Baseline Profiles")
    
    # Mock Gemini client
    mock_genai_client = MagicMock()
    mock_response_p1 = MagicMock()
    # Phase 1 expected JSON response
    mock_response_p1.text = json.dumps({
        "reasoning": "This is a strategic proposal requiring Sustainability Committee and Risk Committee reviews.",
        "classification": "strategic proposal",
        "committees": ["Sustainability Committee", "Risk Committee"],
        "sensitivity": "medium"
    })
    
    # Setup generator calls for genai.Client
    mock_genai_client.models.generate_content.return_value = mock_response_p1
    agent._get_client = MagicMock(return_value=mock_genai_client)
    
    session = await session_service.get_session(app_name="app", user_id="test_user", session_id="session_123")
    ctx_t1 = InvocationContext(
        session_service=session_service,
        artifact_service=artifact_service,
        invocation_id="inv_t1",
        agent=agent,
        user_content=user_content_t1,
        session=session
    )
    
    # Mock PDF extraction
    mock_extraction_result = {
        "status": "success",
        "file_type": "pdf",
        "text": "Fake extracted board paper text containing financials."
    }
    
    with patch("app.agent.extract_board_paper_async", new_callable=AsyncMock) as mock_extract:
        mock_extract.return_value = mock_extraction_result
        
        events_t1 = []
        async for event in agent._run_async_impl(ctx_t1):
            events_t1.append(event)
            
        # Assertions for Turn 1
        mock_extract.assert_called_once_with(file_bytes=b"fake-pdf-data", filename="board_paper.pdf")
        assert len(events_t1) > 0
        final_event_t1 = events_t1[-1]
        assert final_event_t1.content is not None
        text_t1 = final_event_t1.content.parts[0].text
        assert "Phase 1: Intake & Classification" in text_t1
        assert "Shall I proceed with the full simulation?" in text_t1
        
        # Verify state is updated to awaiting approval
        assert ctx_t1.session.state.get("awaiting_approval") is True
        assert ctx_t1.session.state.get("extracted_text_filename") == "extracted_text_session_123.txt"
        
    # ---------------------------------------------------------
    # TURN 2: User says "yes" to proceed
    # ---------------------------------------------------------
    user_content_t2 = types.Content(
        role="user",
        parts=[types.Part(text="yes")]
    )
    
    # Mock response for MemberSimulations (Phase 2)
    mock_response_p2 = MagicMock()
    mock_response_p2.text = json.dumps({
        "name": "Scott Perkins",
        "stance": "Supportive",
        "rationale": "Aligned with core Woolworths retail operations goals.",
        "focus_points": ["What are the expected margins?"],
        "key_request": "Provide margins forecast."
    })
    
    # Mock response for Synthesis Report (Phase 3 & 4)
    mock_response_synthesis = MagicMock()
    mock_response_synthesis.text = "## Phase 3: Cross-Cutting Themes and Vulnerabilities\n* Theme 1...\n## Phase 4: Recommendations\n* Rec 1..."
    
    # Configure the client's models.generate_content to return Synthesis response
    mock_genai_client.models.generate_content.side_effect = None
    mock_genai_client.models.generate_content.return_value = mock_response_synthesis
    
    # Configure the client's aio.models.generate_content as an AsyncMock to support awaited parallel calls
    mock_genai_client.aio.models.generate_content = AsyncMock(return_value=mock_response_p2)
    
    # Update the internal session service storage state directly to simulate Runner's state sync
    session_service.sessions["app"]["test_user"]["session_123"].state.update(ctx_t1.session.state)
    session_t2 = await session_service.get_session(app_name="app", user_id="test_user", session_id="session_123")
    ctx_t2 = InvocationContext(
        session_service=session_service,
        artifact_service=artifact_service,
        invocation_id="inv_t2",
        agent=agent,
        user_content=user_content_t2,
        session=session_t2
    )
    
    events_t2 = []
    async for event in agent._run_async_impl(ctx_t2):
        events_t2.append(event)
        
    # Assertions for Turn 2
    assert len(events_t2) > 0
    final_event_t2 = events_t2[-1]
    assert final_event_t2.content is not None
    text_t2 = final_event_t2.content.parts[0].text
    assert "# Woolworths Group Board Simulation Report" in text_t2
    assert "Phase 1: Intake & Classification" in text_t2
    assert "Phase 2: Detailed Individual Responses" in text_t2
    assert "Phase 3: Cross-Cutting Themes and Vulnerabilities" in text_t2
    assert "Phase 4: Recommendations" in text_t2
    assert "Download file: **[Woolworths_Board_Simulation_Report.docx](/download_artifact/Woolworths_Board_Simulation_Report.docx)**" in text_t2
    
    # State should preserve document details and report context
    assert ctx_t2.session.state.get("awaiting_approval") is False
    assert ctx_t2.session.state.get("phase1_approved") is False
    assert ctx_t2.session.state.get("extracted_text_filename") == "extracted_text_session_123.txt"
    assert ctx_t2.session.state.get("report_md_filename") == "Woolworths_Board_Simulation_Report.md"


@pytest.mark.asyncio
async def test_board_simulator_chat_fallback() -> None:
    """Test conversational chat fallback when a report exists in the session state."""
    session_service = InMemorySessionService()
    await session_service.create_session(
        app_name="app",
        user_id="test_user",
        session_id="session_123"
    )
    
    # Pre-populate session state with report_md_filename
    session = await session_service.get_session(app_name="app", user_id="test_user", session_id="session_123")
    session.state["report_md_filename"] = "Woolworths_Board_Simulation_Report.md"
    session.state["phase1_approved"] = False
    session.state["awaiting_approval"] = False
    
    artifact_service = InMemoryArtifactService()
    # Save a mock markdown report as an artifact
    part = types.Part(
        inline_data=types.Blob(mime_type="text/markdown", data=b"Mock Report: Phase 3 details")
    )
    await artifact_service.save_artifact(
        app_name="app",
        user_id="test_user",
        session_id="session_123",
        filename="Woolworths_Board_Simulation_Report.md",
        artifact=part
    )
    
    user_content = types.Content(
        role="user",
        parts=[types.Part(text="Can you export this report to a docx?")]
    )
    
    agent = BoardSimulator(name="board_simulator")
    
    # Mock Gemini client
    mock_genai_client = MagicMock()
    mock_response = MagicMock()
    mock_response.text = "You can download the board_simulation_report.docx file from the Session Artifacts section in the playground UI."
    mock_genai_client.models.generate_content.return_value = mock_response
    agent._get_client = MagicMock(return_value=mock_genai_client)
    
    ctx = InvocationContext(
        session_service=session_service,
        artifact_service=artifact_service,
        invocation_id="inv_chat",
        agent=agent,
        user_content=user_content,
        session=session
    )
    
    events = []
    async for event in agent._run_async_impl(ctx):
        events.append(event)
        
    assert len(events) == 2
    assert "Processing your request..." in events[0].content.parts[0].text
    assert "You can download the board_simulation_report.docx" in events[1].content.parts[0].text
