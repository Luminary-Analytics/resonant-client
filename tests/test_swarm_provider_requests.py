"""A guarded native invocation cannot hide extra generation HTTP attempts."""

import base64
import json

import httpx
import pytest

from lumi.anthropic_api import AnthropicBackend, encode_event_frame
from lumi.backends import ExoBackend, KimiBackend, OllamaBackend
from lumi.connections import create_connection_backend
from lumi.engine.execution_guard import ExecutionBoundary, ExecutionGuardError
from lumi.openai_api import OpenAIResponsesBackend
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


# ── The Messages and Responses adapters keep the same contract ────────────────
# Anthropic (direct, a connection, Bedrock with a Bedrock API key) and OpenAI
# (direct, Azure) override stream(); under a team's supervision each keeps one
# generation per request, as the Chat Completions adapter above does.

ANTHROPIC_FAMILY = ("anthropic", "anthropic-connection", "bedrock")
OPENAI_FAMILY = ("openai", "azure")


def _api_provider(name, transport):
    if name == "anthropic":
        return AnthropicBackend("fixture-key", "claude-sonnet-5", transport=transport)
    if name == "anthropic-connection":
        return create_connection_backend({"id": "claude", "name": "Claude gateway", "type": "anthropic",
            "base_url": "https://llm.example.com", "auth": "header", "auth_header": "x-api-key", "headers": {},
            "client_cert": ""}, "claude-sonnet-5", "fixture-key", transport=transport)
    if name == "bedrock":
        return AnthropicBackend("bedrock-api-key", "us.anthropic.claude-sonnet-5-v1:0", platform="bedrock",
                                region="us-east-1", transport=transport)
    if name == "openai":
        return OpenAIResponsesBackend("fixture-key", "gpt-5", transport=transport)
    return OpenAIResponsesBackend("azure-key", "gpt-5-deployment", azure=True,
                                  base_url="https://acme.openai.azure.com/openai/v1", transport=transport)


def _api_guarded(name, responses, monkeypatch):
    calls = []
    def respond(request):
        calls.append(request)
        return responses[min(len(calls), len(responses)) - 1]
    backend = _api_provider(name, httpx.MockTransport(respond))
    waits = []
    def wait(delay, _):
        waits.append(delay)
        return False
    for module in ("lumi.backends", "lumi.anthropic_api", "lumi.openai_api"):
        monkeypatch.setattr(f"{module}._wait_with_cancel", wait)
    guard = RecordingGuard()
    stream = ExecutionBoundary(guard).stream(backend, purpose="primary", inputs={"user_msg": "Inspect"},
                                             invoke=lambda: backend.stream("Inspect", [], "Generated fixture", []))
    return calls, waits, guard, stream


def _ends(guard):
    return [record for record in guard.records if record["kind"] == "request.end"]


def _anthropic_sse(*events):
    return httpx.Response(200, headers={"content-type": "text/event-stream"}, content="".join(
        f"event: {event['type']}\ndata: {json.dumps(event)}\n\n" for event in events).encode())


A_START = {"type": "message_start", "message": {"id": "msg_fixture", "usage": {"input_tokens": 5}}}
A_TEXT = ({"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
          {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "Answer"}})
A_END = ({"type": "content_block_stop", "index": 0},
         {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 1}},
         {"type": "message_stop"})
A_OVERLOADED = {"type": "error", "error": {"type": "overloaded_error", "message": "Overloaded"}}
O_MESSAGE = {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "Answer"}]}
O_CREATED = {"type": "response.created", "response": {"id": "resp_fixture"}}
O_TEXT = ({"type": "response.output_item.added", "output_index": 0, "item": {"type": "message"}},
          {"type": "response.output_text.delta", "output_index": 0, "delta": "Answer"})
O_END = ({"type": "response.output_item.done", "output_index": 0, "item": O_MESSAGE},
         {"type": "response.completed", "response": {"id": "resp_fixture", "output": [O_MESSAGE],
                                                      "usage": {"input_tokens": 5, "output_tokens": 1}}})
O_OVERLOADED = {"type": "response.failed", "response": {"id": "resp_fixture", "error": {
    "code": "rate_limit_exceeded", "message": "Rate limit reached"}}}


def _stream_of(name, *parts):
    """One streamed response in the provider's own format: 'start', 'text', 'end' or 'overload'."""
    anthropic = name in ANTHROPIC_FAMILY
    events = []
    for part in parts:
        events += {"start": [A_START] if anthropic else [O_CREATED],
                   "text": list(A_TEXT if anthropic else O_TEXT),
                   "end": list(A_END if anthropic else O_END),
                   "overload": [A_OVERLOADED if anthropic else O_OVERLOADED]}[part]
    return _anthropic_sse(*events)  # Both are server-sent events.


@pytest.mark.parametrize("status", [503, 500, 429])
@pytest.mark.parametrize("provider", ["anthropic", "anthropic-connection", "openai", "azure"])
def test_a_guarded_api_http_rejection_is_one_generation_attempt(provider, status, monkeypatch):
    rejection = httpx.Response(status, json={"type": "error", "error": {"type": "api_error", "message": "fixture unavailable"}})
    calls, waits, guard, stream = _api_guarded(provider, [rejection], monkeypatch)
    with pytest.raises(ExecutionGuardError):
        list(stream)
    # Only a rate limit, which generated nothing, is waited out and sent again.
    assert len(calls) == (4 if status == 429 else 1)
    assert waits == ([5.0, 10.0, 20.0] if status == 429 else [])
    ends = _ends(guard)
    # A 4xx refusal before any output generated nothing, so its outcome is
    # known; a 5xx may have failed mid-generation and stays uncertain.
    assert len(ends) == 1 and ends[0]["outcome"] == ("completed" if status == 429 else "uncertain")
    assert ends[0]["error"].endswith(f"provider status {status}")
    assert "fixture unavailable" not in ends[0]["error"]


@pytest.mark.parametrize("provider", ANTHROPIC_FAMILY)
def test_an_anthropic_overload_529_is_waited_out_and_settled_as_known(provider, monkeypatch):
    # Anthropic sheds load with 529 before processing a request, as a rate limit does.
    overloaded = httpx.Response(529, json={"type": "error", "error": {"type": "overloaded_error", "message": "Overloaded"}})
    calls, waits, guard, stream = _api_guarded(provider, [overloaded], monkeypatch)
    with pytest.raises(ExecutionGuardError):
        list(stream)
    assert len(calls) == 4 and waits == [5.0, 10.0, 20.0]
    ends = _ends(guard)
    assert ends[0]["outcome"] == "completed"
    assert ends[0]["error"] == "Provider refused the request before generating; provider status 529"


@pytest.mark.parametrize("provider", ANTHROPIC_FAMILY + OPENAI_FAMILY)
def test_a_guarded_api_request_waits_out_a_rate_limit_and_generates_once(provider, monkeypatch):
    limited = httpx.Response(429, headers={"retry-after": "2"}, json={"error": {"message": "slow down"}})
    if provider == "bedrock":
        answer = _bedrock_stream(A_START, *A_TEXT, *A_END)
    else:
        answer = _stream_of(provider, "start", "text", "end")
    calls, waits, guard, stream = _api_guarded(provider, [limited, answer], monkeypatch)
    events = list(stream)
    assert len(calls) == 2 and waits == [2.0]  # Retry-After honoured.
    assert any(kind == "text.delta" and data["delta"] == "Answer" for kind, data in events)
    ends = _ends(guard)
    assert len(ends) == 1 and ends[0]["outcome"] == "completed" and ends[0]["error"] == ""
    assert ends[0]["usage"]["input_tokens"] == 5 and ends[0]["usage"]["output_tokens"] == 1


@pytest.mark.parametrize("provider", ["anthropic", "openai"])
def test_an_api_overload_before_any_output_is_waited_out_then_settled_as_known(provider, monkeypatch):
    calls, waits, guard, stream = _api_guarded(provider, [_stream_of(provider, "start", "overload")], monkeypatch)
    with pytest.raises(ExecutionGuardError):
        list(stream)
    assert len(calls) == 4 and waits == [5.0, 10.0, 20.0]
    ends = _ends(guard)
    assert ends[0]["outcome"] == "completed" and ends[0]["error"] == "Provider refused the request before generating"


@pytest.mark.parametrize("provider", ["anthropic", "openai"])
def test_an_api_overload_then_an_answer_is_one_generation(provider, monkeypatch):
    calls, waits, guard, stream = _api_guarded(provider, [_stream_of(provider, "start", "overload"),
                                                          _stream_of(provider, "start", "text", "end")], monkeypatch)
    events = list(stream)
    assert len(calls) == 2 and waits == [5.0]
    assert any(kind == "text.delta" and data["delta"] == "Answer" for kind, data in events)
    assert _ends(guard)[0]["outcome"] == "completed"


@pytest.mark.parametrize("provider", ["anthropic", "openai"])
def test_an_api_overload_after_output_is_neither_resent_nor_known(provider, monkeypatch):
    # Outside a team the adapter starts the response again; a supervised one can't.
    calls, waits, guard, stream = _api_guarded(provider, [_stream_of(provider, "start", "text", "overload")], monkeypatch)
    with pytest.raises(ExecutionGuardError):
        list(stream)
    assert len(calls) == 1 and waits == []
    assert _ends(guard)[0]["outcome"] == "uncertain"


@pytest.mark.parametrize("provider", ["anthropic", "openai"])
def test_an_api_stream_cut_short_is_uncertain_not_a_finished_turn(provider, monkeypatch):
    calls, waits, guard, stream = _api_guarded(provider, [_stream_of(provider, "start", "text")], monkeypatch)
    with pytest.raises(ExecutionGuardError):
        list(stream)
    assert len(calls) == 1 and _ends(guard)[0]["outcome"] == "uncertain"
    # Outside a team a cut-short stream still ends the turn with what arrived, as before.
    backend = _api_provider(provider, httpx.MockTransport(lambda _: _stream_of(provider, "start", "text")))
    assert [kind for kind, _ in backend.stream("Inspect", [], "Fixture", [])][-1] == "done"


def test_outside_a_team_the_api_adapters_still_retry_server_errors(monkeypatch):
    for module in ("lumi.anthropic_api", "lumi.openai_api"):
        monkeypatch.setattr(f"{module}._wait_with_cancel", lambda *_: False)
    for provider in ("anthropic", "openai"):
        replies = iter([httpx.Response(503, json={"error": {"message": "busy"}}),
                        _stream_of(provider, "start", "text", "end")])
        backend = _api_provider(provider, httpx.MockTransport(lambda _: next(replies)))
        events = list(backend.stream("Inspect", [], "Fixture", []))
        assert events[0][0] == "backend.status" and events[-1][0] == "done"


def _bedrock_stream(*events):
    def chunk(event):
        return encode_event_frame({":event-type": "chunk", ":message-type": "event"}, json.dumps(
            {"bytes": base64.b64encode(json.dumps(event).encode()).decode()}).encode())
    return httpx.Response(200, headers={"content-type": "application/vnd.amazon.eventstream"},
                          content=b"".join(chunk(event) for event in events))


def _exception_frame(kind):
    return encode_event_frame({":message-type": "exception", ":exception-type": kind},
                              json.dumps({"message": f"fixture {kind}"}).encode())


def _bedrock_failure(*events, kind):
    stream = _bedrock_stream(*events)
    return httpx.Response(200, headers=stream.headers, content=stream.content + _exception_frame(kind))


SERVER_ERRORS = {
    "anthropic": lambda: _anthropic_sse(A_START, {"type": "error", "error": {"type": "api_error",
                                                                             "message": "Internal server error"}}),
    "anthropic-connection": lambda: _anthropic_sse(A_START, {"type": "error", "error": {
        "type": "api_error", "message": "Internal server error"}}),
    "openai": lambda: _anthropic_sse(O_CREATED, {"type": "response.failed", "response": {
        "id": "resp_fixture", "error": {"code": "server_error", "message": "The server had an error"}}}),
    "azure": lambda: _anthropic_sse(O_CREATED, {"type": "response.failed", "response": {
        "id": "resp_fixture", "error": {"code": "server_error", "message": "The server had an error"}}}),
    "bedrock": lambda: _bedrock_failure(A_START, kind="internalServerException"),
}


@pytest.mark.parametrize("provider", sorted(SERVER_ERRORS))
def test_an_in_stream_server_error_before_output_stays_uncertain(provider, monkeypatch):
    # Only a refusal to serve the request (an overload, throttling or a rate
    # limit) says nothing was generated. A server error in the stream may have
    # come after generation began, as an HTTP 500 may: it isn't sent again and
    # keeps its allowance until reconciled.
    calls, waits, guard, stream = _api_guarded(provider, [SERVER_ERRORS[provider](), _stream_of(
        "anthropic", "start", "text", "end")], monkeypatch)
    with pytest.raises(ExecutionGuardError):
        list(stream)
    assert len(calls) == 1 and waits == []
    ends = _ends(guard)
    assert len(ends) == 1 and ends[0]["outcome"] == "uncertain"
    assert not ends[0]["error"].startswith("Provider refused")


def test_an_in_stream_error_on_a_chat_completions_connection_is_known_only_when_the_server_was_busy(monkeypatch):
    # The same rule on the Chat Completions adapter (NVIDIA NIM, vLLM, a gateway).
    calls, waits, guard, stream = _guarded([_sse({"error": {"message": "Internal server error"}})], monkeypatch)
    with pytest.raises(ExecutionGuardError):
        list(stream)
    assert len(calls) == 1 and waits == [] and _ends(guard)[0]["outcome"] == "uncertain"


@pytest.mark.parametrize("provider", ANTHROPIC_FAMILY)
def test_an_in_stream_rate_limit_before_output_is_waited_out_and_settled_as_known(provider, monkeypatch):
    if provider == "bedrock":
        limited = _bedrock_failure(A_START, kind="throttlingException")
    else:
        limited = _anthropic_sse(A_START, {"type": "error", "error": {"type": "rate_limit_error",
                                                                     "message": "Number of requests exceeded"}})
    calls, waits, guard, stream = _api_guarded(provider, [limited], monkeypatch)
    with pytest.raises(ExecutionGuardError):
        list(stream)
    assert len(calls) == 4 and waits == [5.0, 10.0, 20.0]
    assert _ends(guard)[0]["outcome"] == "completed"
    assert _ends(guard)[0]["error"] == "Provider refused the request before generating"


OPENAI_OVERLOADED = {"error": {"message": "The engine is currently overloaded, please try again later",
                               "type": "server_error", "param": None, "code": None}}


@pytest.mark.parametrize("provider", OPENAI_FAMILY)
def test_an_openai_503_that_says_it_is_overloaded_is_waited_out_like_anthropics_529(provider, monkeypatch):
    # It comes before any stream starts, so nothing was generated: waited out
    # (Retry-After, else 5, 10, 20 s), then settled as refused, like a 429.
    overloaded = httpx.Response(503, json=OPENAI_OVERLOADED)
    calls, waits, guard, stream = _api_guarded(provider, [overloaded], monkeypatch)
    with pytest.raises(ExecutionGuardError):
        list(stream)
    assert len(calls) == 4 and waits == [5.0, 10.0, 20.0]
    assert _ends(guard)[0]["outcome"] == "completed"
    assert _ends(guard)[0]["error"] == "Provider refused the request before generating; provider status 503"
    # Once it answers, that is still one generation.
    calls, waits, guard, stream = _api_guarded(provider, [httpx.Response(503, headers={"retry-after": "2"},
                                                                         json=OPENAI_OVERLOADED),
                                                          _stream_of(provider, "start", "text", "end")], monkeypatch)
    events = list(stream)
    assert any(kind == "text.delta" for kind, _ in events)
    assert len(calls) == 2 and waits == [2.0] and _ends(guard)[0]["outcome"] == "completed"


def test_a_guarded_bedrock_throttle_before_output_is_waited_out_and_its_api_key_is_the_bearer(monkeypatch):
    throttled = encode_event_frame({":message-type": "exception", ":exception-type": "throttlingException"},
                                   json.dumps({"message": "Too many requests"}).encode())
    calls, waits, guard, stream = _api_guarded("bedrock", [
        httpx.Response(200, headers={"content-type": "application/vnd.amazon.eventstream"}, content=throttled),
        _bedrock_stream(A_START, *A_TEXT, *A_END)], monkeypatch)
    events = list(stream)
    assert len(calls) == 2 and waits == [5.0]
    assert any(kind == "text.delta" and data["delta"] == "Answer" for kind, data in events)
    # The Bedrock API key, never an AWS signature from the person's credentials.
    assert {call.headers["authorization"] for call in calls} == {"Bearer bedrock-api-key"}
    assert _ends(guard)[0]["outcome"] == "completed"
