"""Behavioral regressions found during the open-model harness review."""
import json
from types import SimpleNamespace
from unittest.mock import patch

import httpx
import pytest

from resonant_client.backends import (
    EVENT_TOOL_CALL, ExoBackend, OllamaBackend, _recover_tool_calls,
)
from resonant_client.engine.artifacts import ArtifactStore
from resonant_client.engine.compression import (
    compress, estimate_tokens, evict_old_tool_outputs, model_context_budget,
    request_overhead_tokens, should_compress,
)
from resonant_client.engine.session import Session
from resonant_client.gui.runtime import BackendSpec
from tests.streaming_stub import StreamingBackend, done, text_delta, tool_call
from tests.test_model_wire_contract import _collect_stream_events, _content_chunk

TOOLS = [{"type": "function", "function": {
    "name": "bash", "parameters": {"type": "object", "properties": {
        "command": {"type": "string"}}, "required": ["command"]}}}]
CALL = json.dumps({"name": "bash", "arguments": {"command": "echo example"}})


@pytest.mark.parametrize("text", [
    "Example only: " + CALL,
    "Do not run this:\n```json\n" + CALL + "\n```",
    "```json\n" + CALL + "\n```",
    "`" + CALL + "`",
    CALL + " is an example.",
    CALL + '\n{"name":"bash","arguments":',
    "Example: <tool_call>" + CALL + "</tool_call>",
    "```xml\n<tool_call>" + CALL + "</tool_call>\n```",
    "For example, bash(echo example)",
])
def test_explanations_never_become_executable(text):
    backend = OllamaBackend("http://stub", "review-stub")
    events = _collect_stream_events(backend, [
        _content_chunk(text), json.dumps({"done": True}).encode() + b"\n",
    ], tools=TOOLS)
    assert not any(event == EVENT_TOOL_CALL for event, _ in events)
    assert _recover_tool_calls(text, TOOLS, xml=True, dsml=True) == []


def test_fallback_requires_available_tool_and_negotiated_xml():
    assert _recover_tool_calls(CALL, []) == []
    assert _recover_tool_calls(CALL, [{"function": {"name": "file_read"}}]) == []
    xml = "<tool_call>" + CALL + "</tool_call>"
    assert _recover_tool_calls(xml, TOOLS) == []
    assert _recover_tool_calls(xml, TOOLS, xml=True)[0]["name"] == "bash"
    assert _recover_tool_calls(CALL, TOOLS)[0]["name"] == "bash"


def test_exo_example_is_visible_text_not_a_call():
    example = "Example only: " + CALL
    def handler(request):
        if request.url.path == "/state":
            return httpx.Response(200, json={"instances": {"ready": {"modelId": "local/model"}}})
        event = {"choices": [{"delta": {"content": example}}]}
        return httpx.Response(200, text="data: " + json.dumps(event) + "\n\ndata: [DONE]\n\n")
    backend = ExoBackend("local/model", transport=httpx.MockTransport(handler))
    events = list(backend.stream("Explain", [], "system", TOOLS))
    assert not any(event == EVENT_TOOL_CALL for event, _ in events)
    assert example == "".join(data["delta"] for event, data in events if event == "text.delta")


def test_large_arguments_reasoning_schemas_and_short_history_count():
    history = [{"role": "tool_call", "content": "Called file_write",
                "arguments": json.dumps({"content": "x" * 200_000}),
                "reasoning_content": "r" * 40_000}]
    assert estimate_tokens(history) >= 60_000
    assert should_compress(history, context_window=32_768)
    assert should_compress([{"role": "user", "content": "x" * 200_000}], context_window=32_768)
    assert should_compress([], context_window=32_768,
                           overhead_tokens=request_overhead_tokens("x" * 100_000, TOOLS))
    assert request_overhead_tokens("", TOOLS) > request_overhead_tokens()


@pytest.mark.parametrize("window", [4096, 8192, 16384, 32768, 131072])
def test_small_and_large_windows_always_reserve_output(window):
    assert 0 < model_context_budget("any", context_window=window) < window


def test_uncompactable_request_is_retained_and_not_sent(tmp_path):
    backend = StreamingBackend(scripts=[[text_delta("should not run"), done()]])
    backend.effective_context_tokens = 32768
    session = Session(backend)
    session.project_path = str(tmp_path)
    request = "explain " + "x" * 200_000
    events = list(session.run(request))
    assert backend.stream_calls == []
    assert any(event.get("event") == "error" for event in events)
    assert session.conversation_history[0]["content"] == request
    assert events[-1]["outcome"] == "failed"


def test_prefix_overflow_is_checked_before_inference(tmp_path):
    backend = StreamingBackend(scripts=[[text_delta("should not run"), done()]])
    backend.effective_context_tokens = 32768
    session = Session(backend, project_instructions="instruction " * 12000)
    session.project_path = str(tmp_path)
    list(session.run("hello"))
    assert backend.stream_calls == []


def test_re_read_restores_evicted_content():
    session = Session(OllamaBackend("http://stub", "review-stub"))
    args, original = {"path": "sample.py"}, "original evidence\n" * 200
    session._compact_tool_result_for_context("file_read", args, "first", original, is_error=False)
    session.conversation_history = [{"role": "tool_result", "name": "file_read",
                                     "call_id": "first", "content": original}]
    session.conversation_history, _ = evict_old_tool_outputs(session.conversation_history, keep_recent=0)
    output, metadata = session._compact_tool_result_for_context("file_read", args, "second", original, is_error=False)
    assert output == original
    assert not metadata.get("context_deduplicated")


def test_command_evidence_archives_and_is_retrieved_without_execution(tmp_path):
    store = ArtifactStore(tmp_path, root=tmp_path / "artifacts")
    original = "Historical test failure\n" * 100
    history = [{"role": "tool_result", "name": "bash", "call_id": "test", "content": original}]
    assert evict_old_tool_outputs(history, keep_recent=0)[1] == 0
    evicted, count = evict_old_tool_outputs(history, keep_recent=0, artifact_store=store)
    assert count == 1
    artifact_id = evicted[0]["artifact_id"]
    assert original == store.read_text_page(artifact_id)
    backend = StreamingBackend(scripts=[
        [tool_call("artifact_read", {"artifact_id": artifact_id}), done()],
        [text_delta("The recorded test failed."), done()],
    ])
    session = Session(backend, max_steps=2)
    session.project_path = str(tmp_path)
    session.artifact_store = store
    with patch("resonant_client.engine.session.execute_tool") as execute:
        events = list(session.run("Read the archived evidence"))
    execute.assert_not_called()
    assert any(event.get("output") == original for event in events)


def test_oversized_new_result_can_be_compacted_without_model_call(tmp_path):
    store = ArtifactStore(tmp_path, root=tmp_path / "artifacts")
    history = [{"role": "user", "content": "Inspect the failing test"},
               {"role": "tool_call", "name": "bash", "arguments": '{"command":"pytest"}', "call_id": "one"},
               {"role": "tool_result", "name": "bash", "call_id": "one", "content": "failure\n" * 20000}]
    session = SimpleNamespace(conversation_history=history, artifact_store=store, backend=None)
    compacted, note = compress(session, context_window=32768)
    assert note
    assert not should_compress(compacted, context_window=32768)
    assert store.read_text_page(compacted[-1]["artifact_id"], limit=7)[:7] == "failure"


@pytest.mark.parametrize("offset,limit", [(-1, 20), (0, 0), (0, 16001)])
def test_artifact_page_bounds(tmp_path, offset, limit):
    store = ArtifactStore(tmp_path, root=tmp_path / "artifacts")
    item = store.put_text("abc")
    with pytest.raises(ValueError):
        store.read_text_page(item.id, offset, limit)


@pytest.mark.parametrize("mode,wire", [(None, None), ("default", None), ("off", False), ("high", "high")])
def test_thinking_setting_survives_backend_spec_round_trip(mode, wire):
    spec = BackendSpec("ollama", model="qwen3", url="http://stub", thinking_mode=mode or "")
    backend = BackendSpec.from_dict(spec.to_dict()).create_backend()
    payload = backend._with_thinking({})
    if wire is None:
        assert "think" not in payload
    else:
        assert payload["think"] == wire
        assert type(payload["think"]) is type(wire)
    if mode in {"off", "default"}:
        assert backend.thinking_mode == mode


@pytest.mark.parametrize("mode", ["off", "max"])
def test_level_only_model_rejects_unsupported_controls(mode):
    with pytest.raises(ValueError):
        OllamaBackend("http://stub", "gpt-oss:20b", thinking=mode)


def test_exo_uses_deployment_context_override(monkeypatch):
    monkeypatch.delenv("RESONANT_EXO_CONTEXT_TOKENS", raising=False)
    backend = ExoBackend("local/model")
    assert backend.effective_context_tokens == 32768
    monkeypatch.setenv("RESONANT_EXO_CONTEXT_TOKENS", "65536")
    assert backend.effective_context_tokens == 65536


def test_incomplete_summary_never_replaces_history():
    history = [{"role": "user", "content": "Keep the API unchanged"}]
    history += [{"role": "assistant", "content": "exploration " * 10000}] * 4
    backend = StreamingBackend(scripts=[[text_delta('{"summary":"done"}'), done()]])
    session = SimpleNamespace(conversation_history=history, backend=backend)
    compacted, note = compress(session, max_tokens=100)
    assert compacted is history
    assert note == ""


def test_summary_cannot_drop_user_constraint_or_failure():
    history = [{"role": "user", "content": "Keep the API unchanged; do not remove validation."}]
    history += [{"role": "assistant", "content": "exploration " * 1000}]
    history += [{"role": "tool_result", "name": "bash", "call_id": "test", "is_error": True,
                 "content": "FAILED test_public_api"}]
    history += [{"role": "assistant", "content": "working " * 1000}] * 3
    summary = {key: "none recorded" for key in ("summary", "decisions", "changes", "verification", "unresolved_failures", "next_action")}
    backend = StreamingBackend(scripts=[[text_delta(json.dumps(summary)), done()]])
    session = SimpleNamespace(conversation_history=history, backend=backend, todos=[{"text": "Fix test", "done": False}])
    compacted, note = compress(session, max_tokens=100)
    assert note
    assert history[0]["content"] in compacted[0]["content"]
    assert "FAILED test_public_api" in compacted[0]["content"]
    assert "Fix test" in compacted[0]["content"]


@pytest.mark.parametrize("mode,expected", [("default", None), ("off", False), ("high", "high")])
def test_ui_command_rebuilds_with_selected_thinking_setting(mode, expected):
    import asyncio
    from resonant_client.gui.ws_commands import _cmd_set_thinking_mode
    messages = []
    built = []
    async def send(message):
        messages.append(message)
    spec = BackendSpec("ollama", model="qwen3", url="http://stub")
    saved = SimpleNamespace(thinking_mode="", session_role="generator", save=lambda: None)
    state = SimpleNamespace(backend_spec=spec, project=SimpleNamespace(current_session=saved),
                            get_init_data=lambda: {"event": "init"})
    state.create_backend = lambda *args, **kwargs: built.append(spec.create_backend())
    asyncio.run(_cmd_set_thinking_mode(SimpleNamespace(msg={"mode": mode}, state=state, send=send)))
    assert saved.thinking_mode == mode
    assert built[0]._with_thinking({}).get("think") == expected
    assert not any(message.get("event") == "error" for message in messages)


def test_ui_invalid_reasoning_setting_is_not_saved():
    import asyncio
    from resonant_client.gui.ws_commands import _cmd_set_thinking_mode
    messages = []
    async def send(message):
        messages.append(message)
    spec = BackendSpec("ollama", model="gpt-oss:20b", thinking_mode="low")
    saved = SimpleNamespace(thinking_mode="low", save=lambda: pytest.fail("must not save"))
    state = SimpleNamespace(backend_spec=spec, project=SimpleNamespace(current_session=saved))
    asyncio.run(_cmd_set_thinking_mode(SimpleNamespace(msg={"mode": "off"}, state=state, send=send)))
    assert saved.thinking_mode == spec.thinking_mode == "low"
    assert messages[0]["event"] == "error"



def test_preview_does_not_count_as_full_read_evidence():
    session = Session(OllamaBackend("http://stub", "review-stub"))
    original = "full evidence " * 500
    args = {"path": "sample.py"}
    session._compact_tool_result_for_context("file_read", args, "first", original, is_error=False)
    session.conversation_history = [{"role": "tool_result", "call_id": "first", "content": original[:100]}]
    output, _ = session._compact_tool_result_for_context("file_read", args, "second", original, is_error=False)
    assert output == original


def test_multimodal_requirement_survives_compaction():
    media = {"role": "user", "content": [
        {"type": "text", "text": "Match this screenshot"},
        {"type": "image", "media_type": "image/png", "data": "aGVsbG8="},
    ]}
    history = [media] + [{"role": "assistant", "content": "observations " * 1000}] * 5
    summary = {key: "none recorded" for key in ("summary", "decisions", "changes", "verification", "unresolved_failures", "next_action")}
    backend = StreamingBackend(scripts=[[text_delta(json.dumps(summary)), done()]])
    session = SimpleNamespace(conversation_history=history, backend=backend)
    compacted, note = compress(session, max_tokens=100)
    assert note
    assert media in compacted


def test_restricted_worker_keeps_command_output_it_cannot_retrieve(tmp_path):
    store = ArtifactStore(tmp_path, root=tmp_path / "artifacts")
    history = [{"role": "tool_result", "name": "bash", "content": "test failure\n" * 20000}]
    session = SimpleNamespace(conversation_history=history, backend=None, artifact_store=store, _allowed_tools=TOOLS)
    compacted, note = compress(session, context_window=32768)
    assert compacted is history
    assert not note
