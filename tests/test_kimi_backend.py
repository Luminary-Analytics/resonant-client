"""Wire-contract tests for the direct Moonshot Kimi K3 provider."""

from __future__ import annotations

import json
from types import SimpleNamespace

import httpx
import pytest

from resonant_client.backends import (
    EVENT_BACKEND_STATUS,
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


def test_kimi_preserves_repeated_legacy_tool_call_ids_as_separate_turns():
    history = [
        {"role": "user", "content": "Inspect twice"},
        {
            "role": "tool_call", "name": "read", "arguments": '{"path":"a"}',
            "call_id": "call-reused", "content": "Called read",
        },
        {"role": "tool_result", "call_id": "call-reused", "content": "first"},
        {"role": "assistant", "content": "Check it again."},
        {
            "role": "tool_call", "name": "read", "arguments": '{"path":"a"}',
            "call_id": "call-reused", "content": "Called read",
        },
        {"role": "tool_result", "call_id": "call-reused", "content": "second"},
    ]

    messages = KimiBackend("key")._messages(history, "system", "Inspect twice")
    tool_assistants = [message for message in messages if message.get("tool_calls")]

    assert len(tool_assistants) == 2
    assert [message["role"] for message in messages] == [
        "system", "user", "assistant", "tool", "assistant", "assistant", "tool",
        "user",
    ]
    assert [message["tool_call_id"] for message in messages if message["role"] == "tool"] == [
        "call-reused", "call-reused",
    ]


def _browseros_tool(name="click"):
    return {
        "type": "function",
        "function": {
            "name": f"mcp_browseros_{name}",
            "description": f"BrowserOS {name.replace('_', ' ')} capability.",
            "parameters": {"type": "object", "properties": {}},
        },
    }


def test_kimi_session_advertises_compact_core_tools():
    backend = SimpleNamespace(
        name="kimi", model="kimi-k3", supports_dynamic_tool_catalog=True,
    )
    session = Session(backend=backend)
    session.mcp_tools = [_browseros_tool()]
    full_names = {tool["function"]["name"] for tool in session.tools}
    provider_names = {tool["function"]["name"] for tool in session.provider_tools}

    assert "search_tools" in provider_names
    assert {"file_read", "file_edit", "bash", "task"} <= provider_names
    assert "mcp_browseros_click" in full_names
    assert "mcp_browseros_click" not in provider_names
    assert len(provider_names) < len(full_names) / 2
    assert provider_names == {
        "search_tools", "bash", "file_read", "file_write", "file_edit",
        "glob", "grep", "batch", "task", "await_user",
    }
    assert sum(len(json.dumps(tool, sort_keys=True)) for tool in session.provider_tools) < 8_000


def test_tool_search_finds_specialized_capabilities():
    session = Session(backend=SimpleNamespace(name="kimi", model="kimi-k3"))
    session.mcp_tools = [
        _browseros_tool("click"),
        _browseros_tool("get_page_content"),
        _browseros_tool("take_screenshot"),
    ]

    # Native browser tools should win the top slots for a browser query —
    # they are the ones that work without any server configured.
    top = [tool["function"]["name"] for tool in
           session._search_tool_catalog("click and inspect a browser page", limit=6)]
    assert top[0] == "browser_click"
    assert {"browser_read", "browser_screenshot"} <= set(top)

    # MCP-provided equivalents must still be discoverable for users who run
    # BrowserOS; they rank lower, they are not excluded.
    names = {tool["function"]["name"] for tool in
             session._search_tool_catalog("click and inspect a browser page", limit=20)}
    assert "mcp_browseros_click" in names
    assert names & {"mcp_browseros_get_page_content", "mcp_browseros_take_screenshot"}


def test_kimi_emits_dynamic_catalog_as_system_tool_declarations():
    tool = _browseros_tool()
    history = [{"role": "tool_catalog", "tools": [tool], "content": "loaded"}]

    messages = KimiBackend("key")._messages(history, "system", "Continue")

    catalog = next(message for message in messages if message.get("tools"))
    assert catalog == {"role": "system", "tools": [tool]}


def test_session_search_tools_loads_catalog_for_next_kimi_step():
    class Backend:
        name = "kimi"
        model = "kimi-k3"
        handles_tools = False
        supports_dynamic_tool_catalog = True

        def __init__(self):
            self.calls = 0
            self.advertised: list[set[str]] = []

        def stream(self, **kwargs):
            self.calls += 1
            self.advertised.append({
                tool["function"]["name"] for tool in kwargs.get("tools", [])
            })
            if self.calls == 1:
                yield EVENT_TOOL_CALL, {
                    "name": "search_tools",
                    "arguments": '{"query":"browser click","limit":4}',
                    "call_id": "search-1",
                }
            else:
                yield EVENT_TEXT_DELTA, {"delta": "Loaded."}
            yield EVENT_DONE, {"model": self.model, "stats": {}, "cognitive_state": None}

    backend = Backend()
    session = Session(backend=backend, max_steps=2, auto_approve=True)
    session.mcp_tools = [_browseros_tool()]

    list(session.run("Use the browser"))

    catalog = next(turn for turn in session.conversation_history if turn["role"] == "tool_catalog")
    names = {tool["function"]["name"] for tool in catalog["tools"]}
    assert "mcp_browseros_click" in names
    assert "search_tools" in backend.advertised[0]
    assert "mcp_browseros_click" not in backend.advertised[0]
    assert "mcp_browseros_click" in backend.advertised[1]


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

    assert "rejected the API key" in error
    assert "do-not-leak" not in error


def test_kimi_quota_error_is_actionable_and_not_retried():
    requests = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(429, json={
            "error": {
                "message": (
                    "Account org-private is suspended due to insufficient balance; "
                    "please recharge your account"
                ),
                "type": "exceeded_current_quota_error",
            }
        })

    backend = KimiBackend("secret-key", transport=httpx.MockTransport(handler))
    events = list(backend.stream("Hi", [], "system", [], None))

    assert requests == 1
    assert not any(event == EVENT_BACKEND_STATUS for event, _ in events)
    error = next(data["message"] for event, data in events if event == EVENT_ERROR)
    assert "insufficient balance" in error
    assert "Recharge" in error
    assert "org-private" not in error


def test_kimi_rate_limit_remains_retryable(monkeypatch):
    requests = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        if requests == 1:
            return httpx.Response(429, json={
                "error": {"message": "Rate limit exceeded", "type": "rate_limit_error"}
            })
        return httpx.Response(200, text=_sse_response([
            {"id": "chatcmpl-retry", "choices": [{"delta": {"content": "OK"}}]},
        ]))

    monkeypatch.setattr("resonant_client.backends._wait_with_cancel", lambda *_: False)
    backend = KimiBackend("secret-key", transport=httpx.MockTransport(handler))
    events = list(backend.stream("Hi", [], "system", [], None))

    assert requests == 2
    retry = next(data for event, data in events if event == EVENT_BACKEND_STATUS)
    assert retry["kind"] == "kimi_retry"
    assert retry["status_code"] == 429
    assert (EVENT_TEXT_DELTA, {"delta": "OK"}) in events


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
