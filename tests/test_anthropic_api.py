"""Claude through the Messages API: history translation, streaming, Bedrock and Vertex."""

from __future__ import annotations

import base64
import datetime
import json

import httpx
import pytest

from lumi import anthropic_api
from lumi.anthropic_api import (
    AnthropicBackend,
    EventStreamDecoder,
    encode_event_frame,
    sigv4_headers,
)
from lumi.backends import EVENT_BACKEND_STATUS, EVENT_DONE, EVENT_ERROR, EVENT_TEXT_DELTA, EVENT_TOOL_CALL

TOOLS = [{"type": "function", "function": {
    "name": "file_read", "description": "Read a file.",
    "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}}}]


def _sse(*events: dict) -> bytes:
    return "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events).encode()


def _text_response(text: str = "Hello") -> bytes:
    return _sse(
        {"type": "message_start", "message": {"id": "msg_1", "usage": {"input_tokens": 3}}},
        {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": text}},
        {"type": "content_block_stop", "index": 0},
        {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 2}},
        {"type": "message_stop"},
    )


def _tool_loop_history(provider_model: str = "claude-a", with_thinking: bool = True) -> list[dict]:
    details = [{"type": "thinking", "thinking": "Look first.", "signature": "sig-1", "provider": "anthropic"}]
    return [
        {"role": "user", "content": "Read a.py"},
        {"role": "tool_call", "name": "file_read", "arguments": '{"path": "a.py"}', "call_id": "call:1",
         "response_id": "msg_0", "assistant_content": "Checking.",
         "provider_model": provider_model, **({"reasoning_details": details} if with_thinking else {})},
        {"role": "tool_result", "call_id": "call:1", "content": "print('hi')"},
    ]


def test_tool_loop_translates_to_alternating_blocks_with_sanitized_ids():
    backend = AnthropicBackend("key", "claude-a")
    payload = backend._payload("", _tool_loop_history(), "Be helpful.", TOOLS, None)

    roles = [m["role"] for m in payload["messages"]]
    assert roles == ["user", "assistant", "user"]
    assistant = payload["messages"][1]["content"]
    assert [b["type"] for b in assistant] == ["thinking", "text", "tool_use"]
    assert assistant[0] == {"type": "thinking", "thinking": "Look first.", "signature": "sig-1"}
    assert assistant[2]["id"] == "call_1" and assistant[2]["input"] == {"path": "a.py"}
    result = payload["messages"][2]["content"][0]
    assert result["type"] == "tool_result" and result["tool_use_id"] == "call_1"
    # Prompt caching: the system prompt, the last tool and the latest user block.
    assert payload["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert payload["tools"][-1]["cache_control"] == {"type": "ephemeral"}
    assert payload["messages"][-1]["content"][-1]["cache_control"] == {"type": "ephemeral"}
    assert payload["tools"][0]["input_schema"]["required"] == ["path"]


def test_thinking_is_replayed_only_to_the_model_that_produced_it():
    other = AnthropicBackend("key", "claude-b")
    payload = other._payload("", _tool_loop_history("claude-a"), "", TOOLS, None)
    assert [b["type"] for b in payload["messages"][1]["content"]] == ["text", "tool_use"]


def test_thinking_budget_follows_mode_and_drops_for_an_open_loop_without_thinking():
    fresh = AnthropicBackend("key", "claude-a", thinking="high")
    payload = fresh._payload("Plan the change", [], "", TOOLS, None)
    assert payload["thinking"] == {"type": "enabled", "budget_tokens": 12288}
    assert payload["max_tokens"] > 12288

    switched = AnthropicBackend("key", "claude-a", thinking="high")
    payload = switched._payload("", _tool_loop_history(with_thinking=False), "", TOOLS, None)
    assert "thinking" not in payload

    with pytest.raises(ValueError):
        AnthropicBackend("key", "claude-a", thinking="extreme")


def test_stream_emits_text_tool_call_signed_thinking_and_cache_usage():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=_sse(
            {"type": "message_start", "message": {"id": "msg_9", "usage": {
                "input_tokens": 10, "cache_read_input_tokens": 5, "cache_creation_input_tokens": 3}}},
            {"type": "content_block_start", "index": 0, "content_block": {"type": "thinking", "thinking": ""}},
            {"type": "content_block_delta", "index": 0, "delta": {"type": "thinking_delta", "thinking": "Need a.py"}},
            {"type": "content_block_delta", "index": 0, "delta": {"type": "signature_delta", "signature": "sig-9"}},
            {"type": "content_block_start", "index": 1, "content_block": {"type": "text", "text": ""}},
            {"type": "content_block_delta", "index": 1, "delta": {"type": "text_delta", "text": "Reading."}},
            {"type": "content_block_start", "index": 2, "content_block": {
                "type": "tool_use", "id": "toolu_1", "name": "file_read", "input": {}}},
            {"type": "content_block_delta", "index": 2, "delta": {"type": "input_json_delta", "partial_json": '{"pa'}},
            {"type": "content_block_delta", "index": 2, "delta": {"type": "input_json_delta", "partial_json": 'th": "a.py"}'}},
            {"type": "message_delta", "delta": {"stop_reason": "tool_use"}, "usage": {"output_tokens": 7}},
            {"type": "message_stop"},
        ))

    backend = AnthropicBackend("sk-test", "claude-a", thinking="low", transport=httpx.MockTransport(handler))
    events = list(backend.stream("Read a.py", [], "System.", TOOLS))

    assert requests[0].url.path == "/v1/messages"
    assert requests[0].headers["x-api-key"] == "sk-test"
    assert requests[0].headers["anthropic-version"] == anthropic_api.API_VERSION
    assert (EVENT_TEXT_DELTA, {"delta": "Reading."}) in events
    call = next(payload for kind, payload in events if kind == EVENT_TOOL_CALL)
    assert call["arguments"] == '{"path": "a.py"}' and call["call_id"] == "toolu_1"
    assert call["reasoning_details"] == [{"type": "thinking", "thinking": "Need a.py", "signature": "sig-9",
                                          "provider": "anthropic"}]
    assert call["provider_model"] == "claude-a" and call["assistant_content"] == "Reading."
    done = next(payload for kind, payload in events if kind == EVENT_DONE)
    assert done["stats"] == {"input_tokens": 18, "output_tokens": 7, "cached_tokens": 5,
                             "cache_write_tokens": 3, "provider": "anthropic"}


def test_overloaded_responses_retry_then_succeed(monkeypatch):
    monkeypatch.setattr(anthropic_api, "_wait_with_cancel", lambda delay, cancel: False)
    calls = iter([
        httpx.Response(529, json={"type": "error", "error": {"type": "overloaded_error", "message": "busy"}}),
        httpx.Response(200, content=_text_response("Done")),
    ])
    backend = AnthropicBackend("k", "claude-a", transport=httpx.MockTransport(lambda request: next(calls)))
    events = list(backend.stream("Hi", [], "", []))
    assert events[0][0] == EVENT_BACKEND_STATUS and events[0][1]["status_code"] == 529
    assert (EVENT_TEXT_DELTA, {"delta": "Done"}) in events
    assert events[-1][0] == EVENT_DONE


@pytest.mark.parametrize(("status", "body", "expected"), [
    (401, {"error": {"type": "authentication_error", "message": "bad key"}}, "rejected the credentials"),
    (404, {"error": {"type": "not_found_error", "message": "model"}}, "doesn't know the model claude-a"),
    (400, {"error": {"type": "invalid_request_error", "message": "prompt is too long: 250000 tokens"}},
     "longer than claude-a's context window"),
])
def test_errors_explain_the_fix(status, body, expected):
    backend = AnthropicBackend("k", "claude-a", transport=httpx.MockTransport(
        lambda request: httpx.Response(status, json=body)))
    kind, payload = list(backend.stream("Hi", [], "", []))[-1]
    assert kind == EVENT_ERROR and expected in payload["message"]


def test_missing_key_is_a_clear_error():
    with pytest.raises(ValueError, match="Anthropic API key"):
        AnthropicBackend("", "claude-a")


# ── Bedrock ──────────────────────────────────────────────────────────────


def test_sigv4_matches_botocore_for_a_bedrock_model_path():
    botocore = pytest.importorskip("botocore")
    from botocore.auth import SigV4Auth
    from botocore.awsrequest import AWSRequest
    from botocore.credentials import Credentials

    url = "https://bedrock-runtime.us-east-1.amazonaws.com/model/anthropic.claude-v1%3A0/invoke-with-response-stream"
    body = b'{"anthropic_version": "bedrock-2023-05-31"}'
    now = datetime.datetime(2026, 9, 24, 12, 0, 0, tzinfo=datetime.timezone.utc)
    ours = sigv4_headers("POST", url, body, region="us-east-1", service="bedrock", access_key="AKIDEXAMPLE",
                         secret_key="wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY", session_token="tok",
                         headers={"content-type": "application/json"}, now=now)

    # Sign the same request with botocore at the same instant.
    auth = SigV4Auth(Credentials("AKIDEXAMPLE", "wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY", "tok"),
                     "bedrock", "us-east-1")
    request = AWSRequest(method="POST", url=url, data=body, headers={"content-type": "application/json"})
    request.context["timestamp"] = now.strftime("%Y%m%dT%H%M%SZ")
    request.headers["X-Amz-Date"] = request.context["timestamp"]
    request.headers["X-Amz-Security-Token"] = "tok"
    request.headers["X-Amz-Content-SHA256"] = ours["x-amz-content-sha256"]
    canonical = auth.canonical_request(request)
    assert "/model/anthropic.claude-v1%253A0/invoke-with-response-stream" in canonical
    expected = auth.signature(auth.string_to_sign(request, canonical), request)
    assert ours["authorization"].endswith("Signature=" + expected)
    assert botocore  # imported for the skip


def test_event_stream_frames_round_trip_and_reject_corruption():
    event = {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "hi"}}
    payload = json.dumps({"bytes": base64.b64encode(json.dumps(event).encode()).decode()}).encode()
    frame = encode_event_frame({":event-type": "chunk", ":message-type": "event"}, payload)
    decoder = EventStreamDecoder()
    halves = list(decoder.feed(frame[:10])) + list(decoder.feed(frame[10:]))
    assert len(halves) == 1
    headers, body = halves[0]
    assert headers[":event-type"] == "chunk" and json.loads(body)["bytes"]

    corrupt = frame[:-1] + bytes([frame[-1] ^ 0xFF])
    with pytest.raises(ValueError, match="corrupt"):
        list(EventStreamDecoder().feed(corrupt))


def test_bedrock_stream_is_signed_and_decoded():
    seen: list[httpx.Request] = []

    def chunk(event: dict) -> bytes:
        return encode_event_frame({":event-type": "chunk", ":message-type": "event"}, json.dumps(
            {"bytes": base64.b64encode(json.dumps(event).encode()).decode()}).encode())

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        body = b"".join(chunk(e) for e in (
            {"type": "message_start", "message": {"id": "m", "usage": {"input_tokens": 4}}},
            {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
            {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "From Bedrock"}},
            {"type": "message_delta", "delta": {}, "usage": {"output_tokens": 2}},
            {"type": "message_stop"},
        ))
        return httpx.Response(200, headers={"content-type": "application/vnd.amazon.eventstream"}, content=body)

    backend = AnthropicBackend(
        "", "anthropic.claude-v1:0", platform="bedrock", region="us-west-2",
        credentials=lambda: ("AKID", "secret", ""), transport=httpx.MockTransport(handler))
    events = list(backend.stream("Hi", [], "", []))
    request = seen[0]
    assert request.url.host == "bedrock-runtime.us-west-2.amazonaws.com"
    assert request.url.raw_path.startswith(b"/model/anthropic.claude-v1%3A0/invoke-with-response-stream")
    assert request.headers["authorization"].startswith("AWS4-HMAC-SHA256 Credential=AKID/")
    body = json.loads(request.content)
    assert body["anthropic_version"] == anthropic_api.BEDROCK_VERSION and "model" not in body and "stream" not in body
    assert (EVENT_TEXT_DELTA, {"delta": "From Bedrock"}) in events


def test_bedrock_exception_frames_become_errors():
    frame = encode_event_frame({":message-type": "exception", ":exception-type": "validationException"},
                               json.dumps({"message": "bad input"}).encode())
    backend = AnthropicBackend("bedrock-key", "m", platform="bedrock", region="us-east-1",
                               transport=httpx.MockTransport(lambda r: httpx.Response(200, content=frame)))
    kind, payload = list(backend.stream("Hi", [], "", []))[-1]
    assert kind == EVENT_ERROR and "bad input" in payload["message"]


# ── Vertex AI ────────────────────────────────────────────────────────────


def test_vertex_uses_the_project_region_endpoint_and_a_google_token():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, content=_text_response("From Vertex"))

    backend = AnthropicBackend("", "claude-a@20260101", platform="vertex", region="us-east5", project="acme-ai",
                               access_token=lambda: "ya29.token", transport=httpx.MockTransport(handler))
    events = list(backend.stream("Hi", [], "", []))
    request = seen[0]
    assert str(request.url) == ("https://us-east5-aiplatform.googleapis.com/v1/projects/acme-ai/locations/us-east5"
                                "/publishers/anthropic/models/claude-a@20260101:streamRawPredict")
    assert request.headers["authorization"] == "Bearer ya29.token"
    body = json.loads(request.content)
    assert body["anthropic_version"] == anthropic_api.VERTEX_VERSION and body["stream"] is True
    assert "model" not in body
    assert (EVENT_TEXT_DELTA, {"delta": "From Vertex"}) in events


def test_vertex_needs_a_project():
    with pytest.raises(ValueError, match="project"):
        AnthropicBackend("", "claude-a", platform="vertex", region="us-east5")
