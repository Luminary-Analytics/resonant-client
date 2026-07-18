"""Flagship GLM/DeepSeek wire-format regression tests."""

from __future__ import annotations

import json
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

from resonant_client.backends import (
    EVENT_TOOL_CALL,
    OllamaBackend,
    _detect_json_tool_calls,
)
from resonant_client.engine.sandbox import PathSandbox
from resonant_client.engine.session import Session
from resonant_client.protocol import parse_dsml_tool_calls
from tests.streaming_stub import StreamingBackend, done, text_delta, tool_call


class _Response:
    status_code = 200

    def __init__(self, chunks):
        self._chunks = chunks

    def iter_raw(self):
        return iter(self._chunks)


def _capture_stream_payload(backend, chunks, **stream_kwargs):
    captured = {}

    @contextmanager
    def fake_open(payload, *_args, **_kwargs):
        captured.update(payload)
        yield object(), _Response(chunks)

    backend._use_native_tools = True
    OllamaBackend._vision_support_cache[backend.model] = False
    backend._open_chat_stream_with_retry = fake_open
    list(backend.stream(
        user_msg="continue",
        conversation_history=stream_kwargs.pop("conversation_history", []),
        instructions="stable",
        tools=stream_kwargs.pop("tools", []),
        **stream_kwargs,
    ))
    return captured


def test_thinking_is_top_level_without_model_specific_sampling():
    backend = OllamaBackend("http://stub", "glm-5.2", thinking="high")
    done_chunk = json.dumps({"done": True}).encode() + b"\n"

    payload = _capture_stream_payload(backend, [done_chunk])

    assert payload["think"] == "high"
    assert "think" not in payload["options"]
    assert "temperature" not in payload["options"]
    assert "top_p" not in payload["options"]


def test_current_user_message_is_not_duplicated_in_ollama_payload():
    backend = OllamaBackend("http://stub", "glm-5.2:cloud")
    done_chunk = json.dumps({"done": True}).encode() + b"\n"

    payload = _capture_stream_payload(
        backend,
        [done_chunk],
        conversation_history=[{"role": "user", "content": "continue"}],
    )

    user_messages = [message for message in payload["messages"] if message["role"] == "user"]
    assert user_messages == [{"role": "user", "content": "continue"}]


def test_text_only_ollama_payload_represents_attached_image_in_text():
    backend = OllamaBackend("http://stub", "deepseek-v4-pro:cloud")
    done_chunk = json.dumps({"done": True}).encode() + b"\n"
    history = [{
        "role": "user",
        "content": [
            {"type": "image", "media_type": "image/png", "data": "aGVsbG8="},
            {"type": "text", "text": "inspect this"},
        ],
    }]

    payload = _capture_stream_payload(
        backend,
        [done_chunk],
        conversation_history=history,
    )

    user = next(message for message in payload["messages"] if message["role"] == "user")
    assert "images" not in user
    assert "No textual representation is available" in user["content"]
    assert "inspect this" in user["content"]


def test_deepseek_thinking_omits_unsupported_sampling_controls():
    backend = OllamaBackend("http://stub", "deepseek-v4-pro:cloud", thinking="high")

    assert "temperature" not in backend._ollama_options
    assert "top_p" not in backend._ollama_options


def test_reasoning_is_replayed_on_assistant_tool_call_message():
    backend = OllamaBackend("http://stub", "deepseek-v4-pro:cloud", thinking="high")
    history = [
        {
            "role": "tool_call",
            "name": "file_read",
            "arguments": '{"path":"x.py"}',
            "call_id": "call_one",
            "content": "Called file_read",
            "reasoning_content": "I need to inspect the implementation.",
        },
        {"role": "tool_result", "call_id": "call_one", "content": "print('x')"},
    ]
    done_chunk = json.dumps({"done": True}).encode() + b"\n"

    payload = _capture_stream_payload(
        backend,
        [done_chunk],
        conversation_history=history,
        tools=[{"type": "function", "function": {"name": "file_read"}}],
    )

    assistant = next(message for message in payload["messages"] if message["role"] == "assistant")
    assert assistant["thinking"] == "I need to inspect the implementation."


def test_native_ollama_thinking_is_attached_to_tool_event():
    backend = OllamaBackend("http://stub", "deepseek-v4-pro:cloud", thinking="high")
    chunks = [
        json.dumps({"message": {"thinking": "inspect first"}}).encode() + b"\n",
        json.dumps({
            "message": {
                "tool_calls": [{
                    "function": {"name": "file_read", "arguments": {"path": "x.py"}}
                }]
            }
        }).encode() + b"\n",
        json.dumps({"done": True}).encode() + b"\n",
    ]
    events = []

    @contextmanager
    def fake_open(payload, *_args, **_kwargs):
        yield object(), _Response(chunks)

    backend._use_native_tools = True
    OllamaBackend._vision_support_cache[backend.model] = False
    backend._open_chat_stream_with_retry = fake_open
    events = list(backend.stream(
        user_msg="go",
        conversation_history=[],
        instructions="stable",
        tools=[{"type": "function", "function": {"name": "file_read"}}],
    ))

    call = next(data for event, data in events if event == EVENT_TOOL_CALL)
    assert call["reasoning_content"] == "inspect first"


def test_session_preserves_tool_reasoning_for_next_step(tmp_path):
    (tmp_path / "x.py").write_text("x = 1", encoding="utf-8")
    scripted_call = tool_call("file_read", {"path": "x.py"})
    scripted_call[1]["reasoning_content"] = "read before editing"
    backend = StreamingBackend(scripts=[
        [scripted_call, done()],
        [text_delta("done"), done()],
    ])
    session = Session(backend=backend, max_steps=3, auto_approve=True)
    session.project_path = str(tmp_path)
    session.sandbox = PathSandbox(str(tmp_path))

    list(session.run("inspect"))

    history_call = next(item for item in session.conversation_history if item["role"] == "tool_call")
    assert history_call["reasoning_content"] == "read before editing"


def test_dsml_leak_is_reconstructed_with_typed_parameters():
    text = (
        "checking\n<|DSML|tool_calls><|DSML|invoke name=\"file_read\">"
        "<|DSML|parameter name=\"path\" string=\"true\">src/app.py"
        "</|DSML|parameter><|DSML|parameter name=\"limit\" string=\"false\">20"
        "</|DSML|parameter></|DSML|invoke></|DSML|tool_calls>"
    )

    plain, calls = parse_dsml_tool_calls(text)

    assert plain == "checking"
    assert calls[0]["name"] == "file_read"
    assert json.loads(calls[0]["arguments"]) == {"limit": 20, "path": "src/app.py"}


def test_identical_calls_in_one_response_receive_distinct_ids():
    text = (
        '{"name":"grep","arguments":{"pattern":"x"}}\n'
        '{"name":"grep","arguments":{"pattern":"x"}}'
    )

    calls = _detect_json_tool_calls(text)

    assert len(calls) == 2
    assert calls[0]["call_id"] != calls[1]["call_id"]


def test_structured_generation_uses_json_schema_and_disables_second_reasoning_pass():
    backend = OllamaBackend("http://stub", "glm-5.2", thinking="high")
    schema = {
        "type": "object",
        "properties": {"verdict": {"type": "string"}},
        "required": ["verdict"],
    }
    response = MagicMock(status_code=200)
    response.json.return_value = {"message": {"content": '{"verdict":"pass"}'}}

    with patch("httpx.post", return_value=response) as post:
        result = backend.generate_structured("finalize", schema)

    payload = post.call_args.kwargs["json"]
    assert result == {"verdict": "pass"}
    assert payload["format"] == schema
    assert payload["stream"] is False
    assert "think" not in payload
    assert payload["options"]["temperature"] == 0


def test_structured_generation_fails_fast_for_unsupported_ollama_cloud():
    backend = OllamaBackend("http://stub", "glm-5.2:cloud")
    with patch("httpx.post") as post:
        try:
            backend.generate_structured("finalize", {"type": "object"})
        except NotImplementedError as exc:
            assert "Cloud" in str(exc)
        else:
            raise AssertionError("expected Ollama Cloud structured-output rejection")
    post.assert_not_called()
