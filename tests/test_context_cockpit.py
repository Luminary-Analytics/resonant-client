from __future__ import annotations

from resonant_client.engine.session import Session
from tests.streaming_stub import StreamingBackend, done


def test_context_snapshot_reports_budget_layers_sources_and_payloads(tmp_path):
    backend = StreamingBackend(model="glm-5.2:cloud")
    backend.effective_context_tokens = 131_072
    session = Session(
        backend=backend,
        project_instructions="Run pytest -q",
    )
    session.project_path = str(tmp_path)
    session.conversation_history = [
        {"role": "user", "content": "inspect this"},
        {"role": "tool_result", "name": "file_read", "content": "x" * 4000},
    ]
    session.todos = [{"text": "inspect", "done": True}]
    session._last_context_sources = {
        "rag": {"characters": 800, "estimated_tokens": 200}
    }

    snapshot = session.context_snapshot()

    assert snapshot["model"] == "glm-5.2:cloud"
    assert snapshot["context_window"] == 131_072
    assert snapshot["compression_threshold"] == 98_304
    assert snapshot["system_prompt"]["layers"]
    assert snapshot["sources"]["rag"]["estimated_tokens"] == 200
    assert snapshot["largest_tool_payloads"][0]["name"] == "file_read"
    assert snapshot["largest_tool_payloads"][0]["estimated_tokens"] == 1000
    assert snapshot["todos"][0]["done"] is True


def test_session_stream_emits_live_context_state():
    session = Session(
        backend=StreamingBackend(events=[done()]),
        max_steps=1,
    )

    events = list(session.run("hello"))
    context = next(event for event in events if event["event"] == "context.state")

    assert context["history"]["entries"] == 1
    assert context["estimated_total_tokens"] > 0
    assert context["compression_count"] == 0
