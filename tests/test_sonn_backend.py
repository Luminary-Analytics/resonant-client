"""SONN wire, credential, discovery, and agent-loop contracts (no live API)."""

import asyncio
import json
import threading
from types import SimpleNamespace

import httpx
import pytest

from resonant_client.backends import EVENT_DONE, EVENT_ERROR, EVENT_TEXT_DELTA, EVENT_TOOL_CALL, create_backend
from resonant_client.gui.app import AppState
from resonant_client.gui.settings import SettingsManager
from resonant_client.gui.ws_commands import CommandContext, _cmd_provider_connection, _cmd_update_settings
from resonant_client.network_defaults import resolve_sonn_url
from resonant_client.sonn import SonnBackend


BASE = "https://sonn.example/v1/workspace/projects/project-test/openai/v1"


def sse(*chunks):
    return "".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks) + "data: [DONE]\n\n"


@pytest.fixture(autouse=True)
def isolated_sonn(monkeypatch):
    monkeypatch.setattr(SonnBackend, "_catalogs", {})
    monkeypatch.delenv("SONN_API_URL", raising=False)
    monkeypatch.delenv("SONN_API_KEY", raising=False)


def test_endpoint_resolution_preserves_project_path(monkeypatch):
    settings = {"network": {"sonn_url": BASE + "/"}}
    assert resolve_sonn_url(settings_data=settings) == BASE
    monkeypatch.setenv("SONN_API_URL", BASE + "-env/")
    assert resolve_sonn_url(settings_data=settings) == BASE + "-env"
    assert resolve_sonn_url(BASE, settings_data=settings) == BASE
    assert SonnBackend.validate_base_url(BASE) == BASE
    assert SonnBackend.validate_base_url("http://127.0.0.1:8000/v1") == "http://127.0.0.1:8000/v1"


@pytest.mark.parametrize("url", ["", "ftp://host/v1", "https://key:secret@host/v1",
                                      "https://host/v1?key=secret", "https://host/v1#secret",
                                      "http://remote.example/v1", "https://host:invalid/v1"])
def test_invalid_urls_fail_without_echoing_credentials(url):
    with pytest.raises(ValueError, match="SONN API base URL") as error:
        SonnBackend("key", base_url=url)
    assert "secret" not in str(error.value)


@pytest.mark.parametrize("key", ["", "YOUR_PRIVATE_INVITATION"])
def test_missing_or_placeholder_key_is_actionable(key):
    with pytest.raises(ValueError, match="SONN API key"):
        SonnBackend(key, base_url=BASE)


def test_catalog_is_authenticated_cached_and_scoped_to_url_and_key():
    requests = []
    def handle(request):
        requests.append(request)
        assert request.url.path.endswith("/openai/v1/models")
        assert request.headers["authorization"].startswith("Bearer ")
        return httpx.Response(200, json={"data": [None, {}, {"id": "sonn-auto", "context_length": 64000},
                                                       {"id": "sonn-auto"}, {"id": "", "secret": "x"}]})
    transport = httpx.MockTransport(handle)
    rows = SonnBackend.catalog("key-one", base_url=BASE, transport=transport)
    rows[0]["id"] = "changed-by-caller"
    assert SonnBackend.catalog("key-one", base_url=BASE, transport=transport)[0]["id"] == "sonn-auto"
    assert len(requests) == 1
    assert SonnBackend("key-one", base_url=BASE).effective_context_tokens == 64000
    assert SonnBackend("key-two", base_url=BASE).effective_context_tokens == 32768
    SonnBackend.catalog("key-two", base_url=BASE, transport=transport)
    SonnBackend.catalog("key-one", base_url=BASE.replace("project-test", "project-other"), transport=transport)
    SonnBackend.catalog("key-one", base_url=BASE, transport=transport, force=True)
    assert len(requests) == 4
    assert "key-one" not in repr(SonnBackend._catalogs)


@pytest.mark.parametrize("body", ["not json", '{"models": []}', '{"data": {}}'])
def test_malformed_discovery_is_an_error(body):
    with pytest.raises(ValueError, match="invalid model catalog"):
        SonnBackend.catalog("key", base_url=BASE, transport=httpx.MockTransport(
            lambda _: httpx.Response(200, text=body)))


def test_empty_catalog_is_cached_and_account_failures_clear_it():
    calls = []
    def handle(request):
        calls.append(request)
        return httpx.Response(200, json={"data": []})
    transport = httpx.MockTransport(handle)
    assert SonnBackend.catalog("key", base_url=BASE, transport=transport) == []
    assert SonnBackend.catalog("key", base_url=BASE, transport=transport) == []
    assert len(calls) == 1
    with pytest.raises(ValueError, match="API key"):
        SonnBackend.catalog("key", base_url=BASE, force=True, transport=httpx.MockTransport(
            lambda _: httpx.Response(401, json={"error": {"message": "key"}})))
    assert not SonnBackend._catalogs


def test_standard_stream_and_tool_history_exclude_other_provider_extensions():
    sent = []
    def handle(request):
        assert str(request.url) == BASE + "/chat/completions"
        assert request.headers["authorization"] == "Bearer fixture-secret"
        assert "x-openrouter-title" not in request.headers
        sent.append(json.loads(request.read()))
        return httpx.Response(200, text=sse(
            {"id": "reply", "choices": [{"delta": {"content": "Reading.", "tool_calls": [
                {"index": 0, "id": "read-1", "function": {"name": "read", "arguments": '{"path":'}}]}}]},
            {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": '"README.md"}'}}]}}]},
            {"choices": [], "usage": {"prompt_tokens": 120, "completion_tokens": 20}},
        ))
    backend = SonnBackend("fixture-secret", base_url=BASE, transport=httpx.MockTransport(handle))
    tools = [{"type": "function", "function": {"name": "read", "parameters": {"type": "object"}}}]
    events = list(backend.stream("Read README", [], "Keep project instructions", tools + tools))
    assert not [data for event, data in events if event == EVENT_ERROR]
    call = next(data for event, data in events if event == EVENT_TOOL_CALL)
    assert json.loads(call["arguments"]) == {"path": "README.md"}
    assert any(event == EVENT_TEXT_DELTA for event, _ in events)
    done = next(data for event, data in events if event == EVENT_DONE)
    assert done["stats"]["input_tokens"] == 120
    assert set(sent[0]) == {"model", "messages", "stream", "stream_options", "tools"}
    assert len(sent[0]["tools"]) == 1
    history = [{"role": "system", "content": "Retained summary"},
               {"role": "tool_catalog", "tools": tools},
               {**call, "role": "tool_call", "reasoning_content": "foreign reasoning",
                "reasoning_details": [{"data": "foreign-state"}]},
               {"role": "tool_result", "call_id": "read-1", "content": "File contents"}]
    payload = backend._payload("Continue", history, "Keep project instructions", tools, 1200)
    assert payload["max_tokens"] == 1200
    assert "Retained summary" in json.dumps(payload)
    assert "foreign reasoning" not in json.dumps(payload) and "foreign-state" not in json.dumps(payload)
    assistant = next(index for index, message in enumerate(payload["messages"]) if message.get("tool_calls"))
    assert payload["messages"][assistant + 1] == {"role": "tool", "tool_call_id": "read-1", "content": "File contents"}
    assert not backend.capability_profile.supports("vision")
    assert not backend.capability_profile.reasoning_levels


@pytest.mark.parametrize("status", [401, 403, 404, 402, 500])
def test_request_errors_do_not_echo_secrets(status, monkeypatch):
    import resonant_client.backends as module
    monkeypatch.setattr(module, "_wait_with_cancel", lambda *args: False)
    backend = SonnBackend("secret-key", base_url=BASE, transport=httpx.MockTransport(
        lambda _: httpx.Response(status, json={"error": {"message": "secret-key", "type": "secret-key"}})))
    events = list(backend.stream("hello", [], "", []))
    assert any(event == EVENT_ERROR for event, _ in events)
    assert "secret-key" not in json.dumps(events)


def test_in_stream_error_is_redacted():
    backend = SonnBackend("secret-key", base_url=BASE, transport=httpx.MockTransport(
        lambda _: httpx.Response(200, text=sse({"error": {"message": "secret-key"}}))))
    events = list(backend.stream("hello", [], "", []))
    assert "secret-key" not in json.dumps(events)
    assert any(event == EVENT_ERROR for event, _ in events)


@pytest.mark.parametrize("status,expected", [
    (402, "prepaid credit"), (422, "project or model limit"),
    (429, "at capacity"), (502, "upstream generation failed"),
    (504, "not automatically retried"),
])
def test_sonn_failure_categories_do_not_replay_paid_requests(status, expected):
    calls = []
    def handle(request):
        calls.append(request)
        return httpx.Response(status, json={"error": {"message": "private-provider-secret"}})
    backend = SonnBackend("key", base_url=BASE, transport=httpx.MockTransport(handle))
    events = list(backend.stream("hello", [], "", []))
    errors = [data for event, data in events if event == EVENT_ERROR]
    assert len(calls) == 1
    assert expected in errors[0]["message"]
    assert "private-provider-secret" not in json.dumps(events)


def test_interrupted_stream_never_executes_partial_tool_or_inherits_exo_retry():
    calls = []
    def handle(request):
        calls.append(request)
        return httpx.Response(200, text=sse(
            {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "write-1",
                "type": "function", "function": {"name": "file_write", "arguments": '{"path":'}}]}}]},
            {"error": {"message": "runner crashed private-provider-secret"}},
        ))
    backend = SonnBackend("key", base_url=BASE, transport=httpx.MockTransport(handle))
    events = list(backend.stream("hello", [], "", []))
    assert len(calls) == 1
    assert not any(event in (EVENT_TOOL_CALL, EVENT_DONE) for event, _ in events)
    error = next(data for event, data in events if event == EVENT_ERROR)
    assert error["code"] == "sonn_stream_failed"
    assert "Completed work is retained" in error["message"]
    assert "access" not in error["message"]
    assert "private-provider-secret" not in json.dumps(events)


def test_sonn_timeout_does_not_depend_on_exo_attributes():
    backend = SonnBackend("key", base_url=BASE, transport=httpx.MockTransport(
        lambda request: (_ for _ in ()).throw(httpx.ReadTimeout("private-provider-secret"))))
    events = list(backend.stream("hello", [], "", []))
    error = next(data for event, data in events if event == EVENT_ERROR)
    assert "SONN stopped responding" in error["message"]
    assert "private-provider-secret" not in json.dumps(events)
    assert backend._progress_idle_timeout_seconds() == 0


def test_learning_queue_wait_replays_exact_input_then_generates_once(monkeypatch):
    import resonant_client.backends as module
    calls, waits = [], []
    monkeypatch.setattr(module, "_wait_with_cancel", lambda delay, cancel: waits.append(delay) or False)
    def handle(request):
        calls.append(request.content)
        if len(calls) < 3:
            return httpx.Response(429, headers={"retry-after": "75"}, json={
                "error": {"code": "learning_queue_full", "message": "private-secret"}})
        return httpx.Response(200, text=sse({"choices": [{"delta": {"content": "Completed"}}]}))
    backend = SonnBackend("key", base_url=BASE, transport=httpx.MockTransport(handle))
    backend.conversation_id = "persistent-conversation"
    events = list(backend.stream("continue", [], "", []))
    assert len(calls) == 3 and len(set(calls)) == 1
    assert waits == [75, 75]
    assert sum(event == EVENT_DONE for event, _ in events) == 1
    assert not any(event == EVENT_ERROR for event, _ in events)
    assert "private-secret" not in json.dumps(events)


@pytest.mark.parametrize("cancel", [False, True])
def test_learning_queue_wait_is_bounded_and_cancellable(monkeypatch, cancel):
    import resonant_client.backends as module
    calls, waits = [], []
    stop = threading.Event()
    def wait(delay, event):
        waits.append(delay)
        if cancel:
            event.set()
        return event.is_set()
    monkeypatch.setattr(module, "_wait_with_cancel", wait)
    def handle(request):
        calls.append(request)
        return httpx.Response(429, json={"error": {"code": "learning_queue_full"}})
    backend = SonnBackend("key", base_url=BASE, transport=httpx.MockTransport(handle))
    events = list(backend.stream("hello", [], "", [], cancel_event=stop))
    assert len(calls) == (1 if cancel else 3)
    assert waits == ([60] if cancel else [60, 60])
    assert not any(event == EVENT_DONE for event, _ in events)
    if not cancel:
        assert "before paid generation" in next(data["message"] for event, data in events if event == EVENT_ERROR)


@pytest.mark.parametrize("status,body,cooldown", [
    (429, {"error": {"type": "learning_queue_full"}}, "60"),
    (429, {"error": {"message": "learning_queue_full"}}, "60"),
    (502, {"error": {"code": "learning_queue_full"}}, "60"),
    (429, {"error": {"code": "provider_rate_limited"}}, "60"),
    (429, {"error": {"code": "learning_queue_full"}}, "3600"),
    (429, {"error": {"code": "learning_queue_full"}}, "invalid"),
])
def test_only_bounded_structured_predispatch_rejection_can_retry(status, body, cooldown):
    calls = []
    def handle(request):
        calls.append(request)
        return httpx.Response(status, headers={"retry-after": cooldown}, json=body)
    events = list(SonnBackend("key", base_url=BASE, transport=httpx.MockTransport(handle)).stream("hello", [], "", []))
    assert len(calls) == 1
    assert any(event == EVENT_ERROR for event, _ in events)


def test_screenshot_tool_history_remains_text_only_and_is_not_a_human_turn():
    import copy
    history = [
        {"role": "user", "content": "Inspect the browser"},
        {"role": "tool_call", "call_id": "screen-1", "name": "browser_screenshot", "arguments": {}},
        {"role": "tool_result", "call_id": "screen-1", "name": "browser_screenshot",
         "content": "Captured browser artifact screenshot-1.",
         "image": {"type": "base64", "data": "aW1hZ2UtYnl0ZXM=", "media_type": "image/png"}},
    ]
    original = copy.deepcopy(history)
    payloads = []
    def handle(request):
        payload = json.loads(request.content)
        payloads.append(payload)
        assert all(isinstance(message["content"], str) for message in payload["messages"])
        return httpx.Response(200, text=sse({"choices": [{"delta": {"content": "I will inspect the DOM."}}]}))
    backend = SonnBackend("key", base_url=BASE, transport=httpx.MockTransport(handle))
    events = list(backend.stream("", history, "", []))
    assert any(event == EVENT_DONE for event, _ in events)
    assert history == original  # Keep the actual screenshot available locally.
    messages = payloads[0]["messages"]
    assert len([m for m in messages if m["role"] == "user"]) == 1
    result = next(m for m in messages if m["role"] == "tool")
    assert result["tool_call_id"] == "screen-1"
    assert "Captured browser artifact" in result["content"]
    assert "accepts text only" in result["content"]
    assert "aW1hZ2UtYnl0ZXM=" not in json.dumps(payloads)


def test_cancellation_closes_blocked_stream_without_remote_cancel_endpoint():
    started, closed, cancel = threading.Event(), threading.Event(), threading.Event()
    class BlockingStream(httpx.SyncByteStream):
        def __iter__(self):
            started.set()
            assert closed.wait(5)
            yield b"data: [DONE]\n\n"
        def close(self):
            closed.set()
    requests = []
    def handle(request):
        requests.append(request)
        return httpx.Response(200, stream=BlockingStream())
    backend = SonnBackend("key", base_url=BASE, transport=httpx.MockTransport(handle))
    events = []
    worker = threading.Thread(target=lambda: events.extend(backend.stream("hi", [], "", [], cancel_event=cancel)))
    worker.start()
    try:
        assert started.wait(2)
        cancel.set()
        worker.join(2)
        assert not worker.is_alive()
        assert closed.is_set()
        assert len(requests) == 1 and requests[0].method == "POST"
        assert not any(event in {EVENT_TOOL_CALL, EVENT_DONE} for event, _ in events)
    finally:
        cancel.set()
        closed.set()
        worker.join(5)


def test_real_engine_creates_file_and_continues_with_tool_result(tmp_path):
    from resonant_client.engine.session import Session
    target = tmp_path / "hello.py"
    requests = []
    def handle(request):
        payload = json.loads(request.read())
        requests.append(payload)
        if len(requests) == 1:
            chunk = {"id": "write", "choices": [{"delta": {"tool_calls": [
                {"index": 0, "id": "write-1", "function": {"name": "file_write", "arguments": json.dumps(
                    {"path": str(target), "content": 'print("hello")\n'})}}]}}]}
        else:
            assert any(m.get("tool_call_id") == "write-1" for m in payload["messages"])
            chunk = {"choices": [{"delta": {"content": "Created hello.py."}}]}
        return httpx.Response(200, text=sse(chunk))
    backend = SonnBackend("key", base_url=BASE, transport=httpx.MockTransport(handle))
    session = Session(backend=backend, max_steps=2, auto_approve=True)
    session.project_path = str(tmp_path)
    list(session.run("Create hello.py with a hello print statement."))
    assert target.read_text() == 'print("hello")\n'
    assert len(requests) == 2


def test_spec_credentials_follow_settings_and_preserve_explicit_model(tmp_path, monkeypatch):
    settings = SettingsManager(tmp_path / "settings.json")
    settings.set("network", "sonn_url", BASE)
    monkeypatch.setenv("SONN_API_KEY", "env-secret")
    state = AppState.__new__(AppState)
    state.settings = settings
    state.backend_spec = None
    state.project = SimpleNamespace(project_path=str(tmp_path))
    state.available_backends = {"sonn": {"models": ["sonn-auto", "chosen-model"]}}
    spec = state.build_backend_spec("sonn", "chosen-model")
    assert spec.resolve_api_key(settings) == "env-secret"
    state.backend_spec = spec
    settings.set("api_keys", "sonn", "settings-secret")
    settings.set("network", "sonn_url", BASE.replace("project-test", "project-other"))
    changed = state.build_backend_spec("sonn")
    assert changed.model == "chosen-model"
    assert changed.api_key_source == "settings"
    assert "project-other" in changed.base_url
    assert "secret" not in json.dumps(changed.to_dict())
    assert settings.get_masked()["api_keys"]["sonn"] == ""
    backend = changed.create_backend(settings)
    assert backend.api_key == "settings-secret" and backend.model == "chosen-model"
    assert create_backend("sonn", api_key="key", base_url=BASE).model == "sonn-auto"


def test_connection_refresh_publishes_models_without_switching_or_reprobing(tmp_path, monkeypatch):
    settings = SettingsManager(tmp_path / "settings.json")
    settings.set("network", "sonn_url", BASE)
    sent = []
    async def send(payload):
        sent.append(payload)
    monkeypatch.setattr(SonnBackend, "health", lambda self: {
        "status": "ready", "model_count": 1, "models": ["sonn-auto"], "model_labels": {"sonn-auto": "SONN Auto"}})
    backend = object()
    state = SimpleNamespace(settings=settings, backend=backend, available_backends={},
                            _api_key_details=lambda *args: ("key", "settings", "", "sonn"),
                            get_init_data=lambda **kw: {"event": "init"})
    ctx = CommandContext(ws=SimpleNamespace(send_json=send), state=state,
                         msg={"provider": "sonn"}, runs=SimpleNamespace(busy=False))
    asyncio.run(_cmd_provider_connection(ctx))
    assert sent[0]["data"]["status"] == "ready"
    assert state.available_backends["sonn"]["models"] == ["sonn-auto"]
    assert state.backend is backend
    ctx.msg = {"section": "api_keys", "key": "sonn", "value": "new-secret"}
    ctx.runs.busy = True
    asyncio.run(_cmd_update_settings(ctx))
    assert sent[-1]["event"] == "error"
    assert "new-secret" not in json.dumps(sent)
