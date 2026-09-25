"""GPT models through the Responses API: input items, reasoning replay, streaming, Azure."""

from __future__ import annotations

import json

import httpx
import pytest

from lumi import openai_api
from lumi.backends import EVENT_BACKEND_STATUS, EVENT_DONE, EVENT_ERROR, EVENT_TEXT_DELTA, EVENT_TOOL_CALL
from lumi.openai_api import OpenAIResponsesBackend

TOOLS = [{"type": "function", "function": {
    "name": "grep", "description": "Search files.",
    "parameters": {"type": "object", "properties": {"pattern": {"type": "string"}}}}}]


def _sse(*events: dict) -> bytes:
    return "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events).encode()


def _history(model: str) -> list[dict]:
    reasoning = [{"type": "reasoning", "id": "rs_1", "summary": [], "encrypted_content": "enc-1",
                  "provider": "openai-responses"}]
    return [
        {"role": "user", "content": "Find TODOs"},
        {"role": "tool_call", "name": "grep", "arguments": '{"pattern": "TODO"}', "call_id": "call_1",
         "response_id": "resp_0", "assistant_content": "", "provider_model": model, "reasoning_details": reasoning},
        {"role": "tool_result", "call_id": "call_1", "content": "a.py:3 TODO"},
    ]


def test_history_becomes_input_items_with_reasoning_for_the_same_model():
    backend = OpenAIResponsesBackend("sk", "gpt-5", thinking="high")
    payload = backend._payload("", _history("gpt-5"), "Be terse.", TOOLS, 2048)
    kinds = [item.get("type") or item.get("role") for item in payload["input"]]
    assert kinds == ["user", "reasoning", "function_call", "function_call_output"]
    assert payload["input"][1] == {"type": "reasoning", "id": "rs_1", "summary": [], "encrypted_content": "enc-1"}
    assert payload["input"][3] == {"type": "function_call_output", "call_id": "call_1", "output": "a.py:3 TODO"}
    assert payload["instructions"] == "Be terse."
    assert payload["store"] is False and payload["include"] == ["reasoning.encrypted_content"]
    assert payload["reasoning"] == {"summary": "auto", "effort": "high"}
    assert payload["max_output_tokens"] == 2048
    assert payload["tools"][0] == {"type": "function", "name": "grep", "description": "Search files.",
                                   "parameters": TOOLS[0]["function"]["parameters"], "strict": False}

    other = OpenAIResponsesBackend("sk", "gpt-5-mini")._payload("", _history("gpt-5"), "", TOOLS, None)
    assert [item.get("type") or item.get("role") for item in other["input"]] == [
        "user", "function_call", "function_call_output"]


def test_non_reasoning_models_send_no_reasoning_options():
    payload = OpenAIResponsesBackend("sk", "gpt-4.1")._payload("Hi", [], "", [], None)
    assert "reasoning" not in payload and "include" not in payload


def test_stream_emits_text_function_calls_reasoning_and_usage():
    requests: list[httpx.Request] = []
    reasoning_item = {"type": "reasoning", "id": "rs_9", "summary": [{"type": "summary_text", "text": "Plan"}],
                      "encrypted_content": "enc-9"}
    call_item = {"type": "function_call", "id": "fc_1", "call_id": "call_9", "name": "grep",
                 "arguments": '{"pattern": "x"}'}
    message_item = {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "Hi"}]}

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, content=_sse(
            {"type": "response.created", "response": {"id": "resp_9"}},
            {"type": "response.output_item.added", "output_index": 0, "item": {"type": "reasoning", "id": "rs_9"}},
            {"type": "response.reasoning_summary_text.delta", "output_index": 0, "delta": "Plan"},
            {"type": "response.output_item.done", "output_index": 0, "item": reasoning_item},
            {"type": "response.output_item.added", "output_index": 1, "item": {"type": "message"}},
            {"type": "response.output_text.delta", "output_index": 1, "delta": "Hi"},
            {"type": "response.output_item.done", "output_index": 1, "item": message_item},
            {"type": "response.output_item.added", "output_index": 2,
             "item": {"type": "function_call", "call_id": "call_9", "name": "grep", "arguments": ""}},
            {"type": "response.function_call_arguments.delta", "output_index": 2, "delta": '{"pattern": "x"}'},
            {"type": "response.output_item.done", "output_index": 2, "item": call_item},
            {"type": "response.completed", "response": {
                "id": "resp_9", "output": [reasoning_item, message_item, call_item],
                "usage": {"input_tokens": 50, "input_tokens_details": {"cached_tokens": 20},
                          "output_tokens": 9, "output_tokens_details": {"reasoning_tokens": 4}}}},
        ))

    backend = OpenAIResponsesBackend("sk-test", "gpt-5", transport=httpx.MockTransport(handler))
    events = list(backend.stream("Find x", [], "", TOOLS))

    assert requests[0].url.path == "/v1/responses"
    assert requests[0].headers["authorization"] == "Bearer sk-test"
    assert (EVENT_TEXT_DELTA, {"delta": "Hi"}) in events
    call = next(payload for kind, payload in events if kind == EVENT_TOOL_CALL)
    assert call["name"] == "grep" and call["arguments"] == '{"pattern": "x"}' and call["call_id"] == "call_9"
    assert call["reasoning_details"][0]["encrypted_content"] == "enc-9"
    assert call["reasoning_details"][0]["provider"] == "openai-responses"
    assert call["reasoning_content"] == "Plan" and call["provider_model"] == "gpt-5"
    done = next(payload for kind, payload in events if kind == EVENT_DONE)
    assert done["stats"] == {"input_tokens": 50, "output_tokens": 9, "cached_tokens": 20,
                             "reasoning_tokens": 4, "provider": "openai"}


def test_azure_uses_the_deployment_endpoint_and_api_key_header():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, content=_sse(
            {"type": "response.output_text.delta", "output_index": 0, "delta": "ok"},
            {"type": "response.completed", "response": {"output": [], "usage": {}}}))

    backend = OpenAIResponsesBackend("azure-key", "my-gpt5-deployment", azure=True,
                                     base_url="https://acme.openai.azure.com/openai/v1",
                                     api_version="2025-04-01-preview", transport=httpx.MockTransport(handler))
    list(backend.stream("Hi", [], "", []))
    assert str(seen[0].url) == "https://acme.openai.azure.com/openai/v1/responses?api-version=2025-04-01-preview"
    assert seen[0].headers["api-key"] == "azure-key" and "authorization" not in seen[0].headers
    with pytest.raises(ValueError, match="Azure OpenAI endpoint"):
        OpenAIResponsesBackend("k", "d", azure=True)


def test_quota_errors_are_not_retried(monkeypatch):
    monkeypatch.setattr(openai_api, "_wait_with_cancel", lambda delay, cancel: False)
    count = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        count["n"] += 1
        return httpx.Response(429, json={"error": {"code": "insufficient_quota", "message": "You exceeded your quota"}})

    backend = OpenAIResponsesBackend("sk", "gpt-5", transport=httpx.MockTransport(handler))
    kind, payload = list(backend.stream("Hi", [], "", []))[-1]
    assert kind == EVENT_ERROR and "quota exhausted" in payload["message"] and count["n"] == 1


def test_rate_limits_retry_then_succeed(monkeypatch):
    monkeypatch.setattr(openai_api, "_wait_with_cancel", lambda delay, cancel: False)
    responses = iter([
        httpx.Response(429, json={"error": {"code": "rate_limit_exceeded", "message": "slow down"}}),
        httpx.Response(200, content=_sse(
            {"type": "response.output_text.delta", "output_index": 0, "delta": "done"},
            {"type": "response.completed", "response": {"output": [], "usage": {}}})),
    ])
    backend = OpenAIResponsesBackend("sk", "gpt-5", transport=httpx.MockTransport(lambda r: next(responses)))
    events = list(backend.stream("Hi", [], "", []))
    assert events[0][0] == EVENT_BACKEND_STATUS and (EVENT_TEXT_DELTA, {"delta": "done"}) in events


def test_model_discovery_keeps_chat_models_only():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": [{"id": i} for i in (
            "gpt-5", "gpt-5-mini", "text-embedding-3-large", "gpt-4o-realtime-preview", "o4-mini", "dall-e-3")]})

    models = OpenAIResponsesBackend.list_available_models("sk", transport=httpx.MockTransport(handler))
    assert models == ["gpt-5", "gpt-5-mini", "o4-mini"]
