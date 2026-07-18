"""Wire-contract tests for the direct Moonshot Kimi K3 provider."""

from __future__ import annotations

import json
from types import SimpleNamespace

import httpx
import pytest

from resonant_client.backends import (
    EVENT_DONE,
    EVENT_ERROR,
    EVENT_TEXT_DELTA,
    EVENT_TOOL_CALL,
    KimiBackend,
    create_backend,
)
from resonant_client.engine.session import Session
from resonant_client.gui.app import AppState
from resonant_client.gui.costs import CostTracker
from resonant_client.gui.runtime import BackendSpec


def _sse_response(events: list[dict]) -> str:
    return "".join(f"data: {json.dumps(event)}\n\n" for event in events) + "data: [DONE]\n\n"


def test_kimi_payload_uses_documented_k3_contract_and_streams_text(tmp_path):
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["auth"] = request.headers["authorization"]
        captured["payload"] = json.loads(request.read())
        return httpx.Response(
            200,
            text=_sse_response([
                {
                    "id": "chatcmpl-1",
                    "choices": [{"delta": {"reasoning_content": "private"}}],
                },
                {
                    "id": "chatcmpl-1",
                    "choices": [{"delta": {"content": "Hello"}}],
                },
                {
                    "id": "chatcmpl-1",
                    "choices": [],
                    "usage": {
                        "prompt_tokens": 100,
                        "completion_tokens": 20,
                        "prompt_tokens_details": {"cached_tokens": 80},
                    },
                },
            ]),
        )

    backend = KimiBackend("secret-key", transport=httpx.MockTransport(handler))
    events = list(backend.stream(
        "Hello",
        [],
        "Stable system prompt",
        [{"type": "function", "function": {"name": "read", "parameters": {"type": "object"}}}],
        max_tokens=4096,
    ))

    assert captured["url"] == "https://api.moonshot.ai/v1/chat/completions"
    assert captured["auth"] == "Bearer secret-key"
    payload = captured["payload"]
    assert payload["model"] == "kimi-k3"
    assert payload["reasoning_effort"] == "max"
    assert payload["max_completion_tokens"] == 4096
    assert payload["stream_options"] == {"include_usage": True}
    assert "temperature" not in payload and "top_p" not in payload
    assert payload["messages"][-1] == {"role": "user", "content": "Hello"}
    assert (EVENT_TEXT_DELTA, {"delta": "Hello"}) in events
    done = next(data for event, data in events if event == EVENT_DONE)
    assert done["stats"] == {"input_tokens": 100, "output_tokens": 20, "cached_tokens": 80}


def test_kimi_assembles_streamed_tool_calls_and_retains_reasoning():
    body = _sse_response([
        {
            "id": "response-7",
            "choices": [{"delta": {
                "reasoning_content": "Need repository evidence.",
                "tool_calls": [{
                    "index": 0,
                    "id": "call-7",
                    "function": {"name": "file_", "arguments": '{"pa'},
                }],
            }}],
        },
        {
            "id": "response-7",
            "choices": [{"delta": {"tool_calls": [{
                "index": 0,
                "function": {"name": "read", "arguments": 'th":"README.md"}'},
            }]}}],
        },
    ])
    backend = KimiBackend(
        "key",
        transport=httpx.MockTransport(lambda request: httpx.Response(200, text=body)),
    )

    events = list(backend.stream("Inspect", [], "system", [], None))
    call = next(data for event, data in events if event == EVENT_TOOL_CALL)

    assert call["name"] == "file_read"
    assert json.loads(call["arguments"]) == {"path": "README.md"}
    assert call["call_id"] == "call-7"
    assert call["reasoning_content"] == "Need repository evidence."
    assert call["response_id"] == "response-7"
    assert call["response_tool_calls"][0]["function"]["name"] == "file_read"


def test_kimi_reconstructs_complete_assistant_tool_message_unchanged():
    calls = [
        {"id": "call-a", "type": "function", "function": {"name": "read", "arguments": '{"path":"a"}'}},
        {"id": "call-b", "type": "function", "function": {"name": "read", "arguments": '{"path":"b"}'}},
    ]
    history = [
        {"role": "user", "content": "Inspect both"},
        {"role": "assistant", "content": ""},
        {
            "role": "tool_call", "name": "read", "arguments": '{"path":"a"}',
            "call_id": "call-a", "content": "Called read", "response_id": "response-1",
            "response_tool_calls": calls, "reasoning_content": "I should inspect both.",
            "assistant_content": "",
        },
        {"role": "tool_result", "call_id": "call-a", "content": "A"},
        {
            "role": "tool_call", "name": "read", "arguments": '{"path":"b"}',
            "call_id": "call-b", "content": "Called read", "response_id": "response-1",
            "response_tool_calls": calls, "reasoning_content": "I should inspect both.",
            "assistant_content": "",
        },
        {"role": "tool_result", "call_id": "call-b", "content": "B"},
    ]

    messages = KimiBackend("key")._messages(history, "system", "Inspect both")
    assistants = [message for message in messages if message["role"] == "assistant"]

    assert len(assistants) == 1
    assert assistants[0]["tool_calls"] == calls
    assert assistants[0]["reasoning_content"] == "I should inspect both."
    assert [message["tool_call_id"] for message in messages if message["role"] == "tool"] == [
        "call-a", "call-b"
    ]


def test_kimi_converts_base64_images_to_openai_content_parts():
    content = [
        {"type": "image", "media_type": "image/png", "data": "aGVsbG8="},
        {"type": "text", "text": "Inspect this"},
    ]

    converted = KimiBackend._api_content(content)

    assert converted[0]["image_url"]["url"] == "data:image/png;base64,aGVsbG8="
    assert converted[1] == {"type": "text", "text": "Inspect this"}


def test_kimi_factory_and_backend_spec_resolve_settings_key():
    class Settings:
        def get(self, section, key=None, default=None):
            if section == "api_keys" and key == "kimi":
                return "stored-key"
            return default

    spec = BackendSpec(
        backend_type="kimi",
        model="kimi-k3",
        base_url="https://api.moonshot.ai/v1",
        api_key_source="settings",
        api_key_setting="kimi",
    )
    backend = spec.create_backend(Settings())

    assert isinstance(backend, KimiBackend)
    assert backend.api_key == "stored-key"
    assert backend.capability_profile.supports("vision")
    assert create_backend("kimi", api_key="x").model == "kimi-k3"
    with pytest.raises(ValueError, match="Kimi API key required"):
        create_backend("kimi", api_key="")


def test_kimi_errors_do_not_expose_api_key():
    backend = KimiBackend(
        "do-not-leak",
        transport=httpx.MockTransport(
            lambda request: httpx.Response(401, json={"error": {"message": "Invalid token"}})
        ),
    )

    events = list(backend.stream("Hi", [], "system", [], None))
    error = next(data["message"] for event, data in events if event == EVENT_ERROR)

    assert "Invalid token" in error
    assert "do-not-leak" not in error


def test_kimi_cost_uses_cached_input_discount(tmp_path):
    tracker = CostTracker(tmp_path / "costs.json")

    cost = tracker.record_usage("kimi-k3", 1_000_000, 1_000_000, cached_tokens=800_000)

    assert cost == pytest.approx(15.84)


def test_session_persists_kimi_response_metadata_for_next_tool_turn():
    class Backend:
        name = "kimi"
        model = "kimi-k3"
        handles_tools = False

        def __init__(self):
            self.calls = 0

        def stream(self, *args, **kwargs):
            self.calls += 1
            if self.calls == 1:
                response_calls = [{
                    "id": "call-1", "type": "function",
                    "function": {"name": "missing_tool", "arguments": "{}"},
                }]
                yield EVENT_TOOL_CALL, {
                    "name": "missing_tool",
                    "arguments": "{}",
                    "call_id": "call-1",
                    "reasoning_content": "Inspect first.",
                    "assistant_content": "",
                    "response_id": "response-1",
                    "response_tool_calls": response_calls,
                }
            else:
                yield EVENT_TEXT_DELTA, {"delta": "Finished."}
            yield EVENT_DONE, {"model": self.model, "stats": {}, "cognitive_state": None}

    session = Session(backend=Backend(), max_steps=2, auto_approve=True)
    list(session.run("Inspect"))
    tool_turn = next(turn for turn in session.conversation_history if turn["role"] == "tool_call")

    assert tool_turn["reasoning_content"] == "Inspect first."
    assert tool_turn["response_id"] == "response-1"
    assert tool_turn["response_tool_calls"][0]["id"] == "call-1"


def test_app_state_builds_and_routes_active_kimi_backend_spec():
    class Settings:
        def get(self, section, key=None, default=None):
            if section == "api_keys" and key == "kimi":
                return "stored-key"
            if section == "general" and key == "default_model":
                return ""
            return default

    state = AppState.__new__(AppState)
    state.project = SimpleNamespace(project_path="D:/Repos/Playground")
    state.settings = Settings()
    state.permission_mode = "bypass"
    state.backend_spec = None
    state.available_backends = {
        "kimi": {
            "url": KimiBackend.DEFAULT_BASE_URL,
            "models": ["kimi-k3"],
        }
    }

    spec = state.build_backend_spec("kimi")
    state.backend_spec = spec

    assert spec.api_key_source == "settings"
    assert spec.api_key_setting == "kimi"
    assert spec.api_key == ""
    assert state.select_harness_backend(session_role="generator") == ("kimi", "kimi-k3")
