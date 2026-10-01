"""A guarded native invocation cannot hide extra generation HTTP attempts."""

import json

import httpx
import pytest

from lumi.backends import ExoBackend, KimiBackend, OllamaBackend
from lumi.connections import create_connection_backend
from lumi.engine.execution_guard import ExecutionBoundary, ExecutionGuardError
from lumi.openrouter import OpenRouterBackend
from lumi.sonn import SonnBackend
from tests.test_swarm_session_guard import RecordingGuard


def _provider(name, transport):
    if name == "kimi":
        return KimiBackend("fixture-key", transport=transport)
    if name == "sonn":
        return SonnBackend("fixture-key", base_url="https://sonn.example/project/openai/v1", transport=transport)
    if name == "openrouter":
        return OpenRouterBackend("fixture-key", "fixture/model", transport=transport)
    if name == "connection":
        return create_connection_backend({"id": "nim", "name": "NVIDIA NIM", "type": "openai-compatible",
            "base_url": "https://integrate.api.nvidia.com/v1", "auth": "bearer", "headers": {}, "client_cert": ""},
            "fixture/model", "fixture-key", transport=transport)
    backend = ExoBackend(model="fixture/model", base_url="http://127.0.0.1:59999/v1", transport=transport)
    backend._ensure_instance = lambda cancel_event=None: False
    return backend


@pytest.mark.parametrize("status", [503, 429])
@pytest.mark.parametrize("provider", ["kimi", "sonn", "openrouter", "exo", "connection"])
def test_guarded_http_rejection_is_one_generation_attempt(provider, status, monkeypatch):
    calls = []
    def respond(request):
        calls.append(request)
        return httpx.Response(status, json={"error": {"type": "learning_queue_full", "message": "fixture unavailable"}})
    backend = _provider(provider, httpx.MockTransport(respond))
    waits = []
    def wait(delay, cancel_event):
        if status != 429:
            pytest.fail("A supervised request cannot enter retry backoff")
        waits.append(delay)
        return False
    monkeypatch.setattr("lumi.backends._wait_with_cancel", wait)
    guard = RecordingGuard()
    boundary = ExecutionBoundary(guard)
    with pytest.raises(ExecutionGuardError):
        list(boundary.stream(backend, purpose="primary", inputs={"user_msg": "Inspect"},
                             invoke=lambda: backend.stream("Inspect", [], "Generated fixture", [])))
    # Only a rate limit, which generated nothing, is waited out and sent again
    # (SONN's full learning queue isn't retryable); every attempt was refused.
    retried = status == 429 and provider != "sonn"
    assert len(calls) == (4 if retried else 1)
    assert waits == ([5.0, 10.0, 20.0] if retried else [])
    assert all(call.url.path.endswith("/chat/completions") for call in calls)
    ends = [record for record in guard.records if record["kind"] == "request.end"]
    # A 4xx refusal before any output generated nothing, so its outcome is
    # known; a 5xx may have failed mid-generation and stays uncertain.
    assert len(ends) == 1 and ends[0]["outcome"] == ("completed" if status == 429 else "uncertain")
    # The status is kept (a number, unlike the provider's text) for diagnosis.
    assert ends[0]["error"].endswith(f"provider status {status}")
    assert "fixture unavailable" not in ends[0]["error"]


def test_guarded_exo_stream_restart_cannot_repeat_generation():
    calls = []
    def respond(request):
        calls.append(request)
        events = [{"id": "fixture", "choices": [{"delta": {"content": "Partial"}}]},
                  {"error": {"message": "runner shutdown before completing command"}}]
        return httpx.Response(200, text="".join("data: " + json.dumps(event) + "\n\n" for event in events))
    backend = _provider("exo", httpx.MockTransport(respond))
    boundary = ExecutionBoundary(RecordingGuard())
    with pytest.raises(ExecutionGuardError):
        list(boundary.stream(backend, purpose="primary", inputs={},
                             invoke=lambda: backend.stream("Inspect", [], "Fixture", [])))
    assert len(calls) == 1


@pytest.mark.parametrize("outcome", ["rejected", "timeout"])
def test_guarded_ollama_open_does_not_retry_unknown_or_rejected_generation(monkeypatch, outcome):
    calls = []
    def respond(request):
        calls.append(request)
        if outcome == "timeout":
            raise httpx.ReadTimeout("fixture response unavailable", request=request)
        return httpx.Response(503, text="fixture unavailable")
    original_client = httpx.Client
    monkeypatch.setattr("lumi.backends.httpx.Client",
        lambda **kwargs: original_client(**kwargs, transport=httpx.MockTransport(respond)))
    monkeypatch.setattr("lumi.backends._wait_with_cancel",
                        lambda *_: pytest.fail("A supervised request cannot retry"))
    backend = OllamaBackend("http://127.0.0.1:59999", "fixture")
    # The same adapter used by Session enables the one-request transport mode.
    ExecutionBoundary(RecordingGuard()).check_backend(backend)
    if outcome == "timeout":
        with pytest.raises(httpx.ReadTimeout):
            with backend._open_chat_stream_with_retry({}, 1, None):
                pytest.fail("The timed-out request has no response")
    else:
        with backend._open_chat_stream_with_retry({}, 1, None) as (_, response):
            assert response.status_code == 503
    assert len(calls) == 1


def test_guarded_ollama_unknown_capability_cannot_make_hidden_generation_probe(monkeypatch):
    calls = []
    def metadata(url, **kwargs):
        calls.append(url)
        assert url.endswith("/api/show"), "Capability detection must not dispatch unaccounted generation"
        return httpx.Response(200, json={"capabilities": ["completion"], "template": ""})
    monkeypatch.setattr("lumi.backends.httpx.post", metadata)
    backend = OllamaBackend("http://127.0.0.1:59999", "unknown-fixture-tool-model")
    backend._tool_support_cache.pop(backend.model, None)
    ExecutionBoundary(RecordingGuard()).check_backend(backend)
    with pytest.raises(ValueError, match="generation probes are disabled"):
        list(backend.stream("Inspect", [], "Fixture", [{"type": "function", "function": {
            "name": "file_read", "parameters": {"type": "object", "properties": {}}}}]))
    assert calls == ["http://127.0.0.1:59999/api/show"]
    assert backend.model not in backend._tool_support_cache


def test_a_supervised_request_waits_out_a_rate_limit_and_generates_once(monkeypatch):
    calls = []
    def respond(request):
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(429, headers={"retry-after": "2"}, json={"error": {"message": "slow down"}})
        body = "".join("data: " + json.dumps(event) + "\n\n" for event in (
            {"id": "ok", "choices": [{"delta": {"content": "Answer"}}]},
            {"id": "ok", "choices": [{"delta": {}, "finish_reason": "stop"}]},
            {"id": "ok", "choices": [], "usage": {"prompt_tokens": 5, "completion_tokens": 1}})) + "data: [DONE]\n\n"
        return httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})
    backend = _provider("connection", httpx.MockTransport(respond))
    waits = []
    monkeypatch.setattr("lumi.backends._wait_with_cancel", lambda delay, _: waits.append(delay) or False)
    guard = RecordingGuard()
    events = list(ExecutionBoundary(guard).stream(backend, purpose="primary", inputs={"user_msg": "Inspect"},
                  invoke=lambda: backend.stream("Inspect", [], "Generated fixture", [])))
    assert len(calls) == 2 and waits == [2.0]  # Retry-After honoured.
    assert any("Answer" in json.dumps(data) for _, data in events)
    ends = [record for record in guard.records if record["kind"] == "request.end"]
    assert len(ends) == 1 and ends[0]["outcome"] == "completed" and ends[0]["error"] == ""


def _sse(*events):
    return httpx.Response(200, text="".join("data: " + json.dumps(event) + "\n\n" for event in events)
                          + "data: [DONE]\n\n", headers={"content-type": "text/event-stream"})


OVERLOADED = {"error": {"message": "Service temporarily overloaded"}}
ANSWER = ({"id": "ok", "choices": [{"delta": {"content": "Answer"}}]},
          {"id": "ok", "choices": [{"delta": {}, "finish_reason": "stop"}]},
          {"id": "ok", "choices": [], "usage": {"prompt_tokens": 5, "completion_tokens": 1}})


def _guarded(responses, monkeypatch):
    calls = []
    def respond(request):
        calls.append(request)
        return responses[min(len(calls), len(responses)) - 1]
    backend = _provider("connection", httpx.MockTransport(respond))
    waits = []
    monkeypatch.setattr("lumi.backends._wait_with_cancel", lambda delay, _: waits.append(delay) or False)
    guard = RecordingGuard()
    stream = ExecutionBoundary(guard).stream(backend, purpose="primary", inputs={"user_msg": "Inspect"},
                                             invoke=lambda: backend.stream("Inspect", [], "Generated fixture", []))
    return calls, waits, guard, stream


def test_an_overload_before_any_output_is_waited_out_then_settled_as_known(monkeypatch):
    # NVIDIA NIM answered 200 and then "Service temporarily overloaded" under a
    # four-worker team: nothing was generated, so it is waited out and, if it
    # persists, settled as a known failure rather than an uncertain request.
    calls, waits, guard, stream = _guarded([_sse(OVERLOADED)], monkeypatch)
    with pytest.raises(ExecutionGuardError):
        list(stream)
    assert len(calls) == 4 and waits == [5.0, 10.0, 20.0]
    ends = [record for record in guard.records if record["kind"] == "request.end"]
    assert len(ends) == 1 and ends[0]["outcome"] == "completed"
    assert ends[0]["error"] == "Provider refused the request before generating"


def test_an_overload_then_an_answer_is_one_generation(monkeypatch):
    calls, waits, guard, stream = _guarded([_sse(OVERLOADED), _sse(*ANSWER)], monkeypatch)
    events = list(stream)
    assert len(calls) == 2 and waits == [5.0]
    assert any("Answer" in json.dumps(data) for _, data in events)
    ends = [record for record in guard.records if record["kind"] == "request.end"]
    assert ends[0]["outcome"] == "completed" and ends[0]["error"] == ""


def test_an_overload_after_output_is_neither_resent_nor_known(monkeypatch):
    calls, waits, guard, stream = _guarded([_sse(ANSWER[0], OVERLOADED)], monkeypatch)
    with pytest.raises(ExecutionGuardError):
        list(stream)
    assert len(calls) == 1 and waits == []
    ends = [record for record in guard.records if record["kind"] == "request.end"]
    assert ends[0]["outcome"] == "uncertain"
