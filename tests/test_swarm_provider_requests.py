"""A guarded native invocation cannot hide extra generation HTTP attempts."""

import json

import httpx
import pytest

from lumi.backends import ExoBackend, KimiBackend, OllamaBackend
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
    backend = ExoBackend(model="fixture/model", base_url="http://127.0.0.1:59999/v1", transport=transport)
    backend._ensure_instance = lambda cancel_event=None: False
    return backend


@pytest.mark.parametrize("provider", ["kimi", "sonn", "openrouter", "exo"])
def test_guarded_http_rejection_is_one_generation_attempt(provider, monkeypatch):
    calls = []
    def respond(request):
        calls.append(request)
        return httpx.Response(429 if provider == "sonn" else 503,
            json={"error": {"type": "learning_queue_full", "message": "fixture unavailable"}})
    backend = _provider(provider, httpx.MockTransport(respond))
    monkeypatch.setattr("lumi.backends._wait_with_cancel",
                        lambda *_: pytest.fail("A supervised request cannot enter retry backoff"))
    guard = RecordingGuard()
    boundary = ExecutionBoundary(guard)
    with pytest.raises(ExecutionGuardError):
        list(boundary.stream(backend, purpose="primary", inputs={"user_msg": "Inspect"},
                             invoke=lambda: backend.stream("Inspect", [], "Generated fixture", [])))
    assert len(calls) == 1
    assert calls[0].url.path.endswith("/chat/completions")
    ends = [record for record in guard.records if record["kind"] == "request.end"]
    assert len(ends) == 1 and ends[0]["outcome"] == "uncertain"


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
