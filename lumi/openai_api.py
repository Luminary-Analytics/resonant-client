"""GPT models through the OpenAI Responses API, directly or on Azure OpenAI.

Requests are stateless (``store: false``). Reasoning models return their
reasoning as encrypted items; those travel in a tool call's
``reasoning_details`` and are replayed only to the model that produced them,
so a tool-use loop keeps its reasoning without OpenAI keeping the
conversation.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from typing import Any, Iterator, Tuple

import httpx

from . import net
from .backends import (
    EVENT_BACKEND_STATUS,
    EVENT_DONE,
    EVENT_ERROR,
    EVENT_TEXT_DELTA,
    EVENT_TOOL_CALL,
    KimiBackend,
    _convert_tools_for_ollama,
    _new_call_id,
    _wait_with_cancel,
)
from .capabilities import ModelCapabilities, infer_model_capabilities

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://api.openai.com/v1"
DEFAULT_MODELS = ("gpt-5", "gpt-5-mini", "gpt-5-codex", "o4-mini", "gpt-4.1")
REASONING_PROVIDER = "openai-responses"
# Lumi thinking mode -> Responses reasoning effort; "default" sends none.
REASONING_EFFORT = {"low": "low", "med": "medium", "medium": "medium", "high": "high", "max": "high"}
THINKING_MODES = {"", "default", "off", *REASONING_EFFORT}
_CHAT_MODEL = re.compile(r"^(gpt-|o\d|chatgpt-|codex)", re.IGNORECASE)
_NOT_CHAT = ("audio", "realtime", "tts", "transcribe", "image", "embedding", "search",
             "dall-e", "whisper", "moderation", "instruct", "davinci", "babbage")


def is_reasoning_model(model: str) -> bool:
    lower = str(model or "").lower()
    return lower.startswith(("o1", "o3", "o4", "gpt-5")) or "codex" in lower


def _content_parts(content: Any, *, role: str) -> list[dict]:
    text_type = "output_text" if role == "assistant" else "input_text"
    if isinstance(content, str):
        return [{"type": text_type, "text": content}] if content.strip() else []
    parts: list[dict] = []
    for part in content or []:
        if not isinstance(part, dict):
            continue
        if part.get("type") == "text" and str(part.get("text") or "").strip():
            parts.append({"type": text_type, "text": str(part["text"])})
        elif part.get("type") == "image_url" and role != "assistant":
            url = str((part.get("image_url") or {}).get("url") or "")
            if url:
                parts.append({"type": "input_image", "image_url": url})
    return parts


def to_response_input(
    messages: list[dict],
    reasoning_by_call: dict[str, list[dict]] | None = None,
) -> tuple[str, list[dict]]:
    """Translate Chat Completions messages into (instructions, Responses input items)."""
    reasoning_by_call = reasoning_by_call or {}
    instructions: list[str] = []
    items: list[dict] = []
    for message in messages:
        role = message.get("role")
        if role == "system":
            if message.get("tools") and not message.get("content"):
                continue
            content = message.get("content")
            text = content if isinstance(content, str) else " ".join(
                str(p.get("text") or "") for p in content or [] if isinstance(p, dict))
            if text.strip():
                instructions.append(text)
        elif role == "user":
            parts = _content_parts(message.get("content"), role="user")
            if parts:
                items.append({"role": "user", "content": parts})
        elif role == "assistant":
            calls = [call for call in message.get("tool_calls") or [] if isinstance(call, dict)]
            for call in calls:
                replay = reasoning_by_call.get(str(call.get("id") or ""))
                if replay:
                    items.extend(replay)
                    break
            parts = _content_parts(message.get("content"), role="assistant")
            if parts:
                items.append({"role": "assistant", "content": parts})
            for call in calls:
                function = call.get("function") if isinstance(call.get("function"), dict) else {}
                arguments = function.get("arguments", "{}")
                if not isinstance(arguments, str):
                    arguments = json.dumps(arguments, ensure_ascii=False)
                items.append({
                    "type": "function_call",
                    "call_id": str(call.get("id") or ""),
                    "name": str(function.get("name") or ""),
                    "arguments": arguments or "{}",
                })
        elif role == "tool":
            content = message.get("content")
            items.append({
                "type": "function_call_output",
                "call_id": str(message.get("tool_call_id") or ""),
                "output": content if isinstance(content, str) else json.dumps(content, ensure_ascii=False),
            })
    return "\n\n".join(instructions), items


def response_tools(tools: list) -> list[dict]:
    converted: list[dict] = []
    seen: set[str] = set()
    for tool in _convert_tools_for_ollama(tools):
        function = tool.get("function") or {}
        name = str(function.get("name") or "")
        if not name or name in seen:
            continue
        seen.add(name)
        converted.append({
            "type": "function",
            "name": name,
            "description": str(function.get("description") or ""),
            "parameters": function.get("parameters") or {"type": "object", "properties": {}},
            "strict": False,
        })
    return converted


class OpenAIResponsesBackend(KimiBackend):
    """OpenAI models through the Responses API (OpenAI or Azure OpenAI)."""

    PROVIDER_LABEL = "OpenAI"
    RETRY_EVENT_KIND = "openai_retry"
    DEFAULT_BASE_URL = DEFAULT_BASE_URL
    DEFAULT_MODEL = DEFAULT_MODELS[0]
    MODELS = DEFAULT_MODELS
    supports_dynamic_tool_catalog = True
    dynamic_tool_catalog_via_history = False
    supports_remote_cancel = False

    def __init__(
        self,
        api_key: str = "",
        model: str = "",
        *,
        base_url: str = "",
        azure: bool = False,
        api_version: str = "",
        name: str = "openai",
        label: str = "OpenAI",
        headers: dict[str, str] | None = None,
        auth_header: str = "",
        thinking: str | None = None,
        capability_overrides: dict[str, Any] | None = None,
        transport=None,
    ):
        self.azure = bool(azure)
        self.model = str(model or "").strip()
        if not self.model:
            raise ValueError("Choose a model first." if not self.azure else "Choose an Azure deployment first.")
        self.api_key = str(api_key or "").strip()
        self.auth_header = auth_header or ("api-key" if self.azure else "authorization")
        if not self.api_key and self.auth_header != "none":
            raise ValueError(
                "Add the Azure OpenAI key for this connection." if self.azure else
                "Add your OpenAI API key in Settings → API keys, or set OPENAI_API_KEY."
            )
        self.base_url = str(base_url or "").rstrip("/") or DEFAULT_BASE_URL
        if self.azure and not str(base_url or "").strip():
            raise ValueError("Set the Azure OpenAI endpoint, such as https://NAME.openai.azure.com/openai/v1.")
        self.api_version = str(api_version or "").strip()
        self.name = name
        self.PROVIDER_LABEL = label or ("Azure OpenAI" if self.azure else "OpenAI")
        self.extra_headers = dict(headers or {})
        self.handles_tools = False
        mode = str(thinking or "").strip().lower()
        if mode not in THINKING_MODES:
            raise ValueError("Reasoning effort must be off, low, med, high or max.")
        self.thinking_mode = mode or "default"
        self._transport = transport
        profile = infer_model_capabilities(self.model)
        overrides = {
            key: value for key, value in (capability_overrides or {}).items()
            if key in {"context_window", "modalities"} and value
        }
        self._capabilities = ModelCapabilities(**{**profile.to_dict(), **overrides})
        self._timeout = httpx.Timeout(
            connect=float(os.environ.get("LUMI_OPENAI_CONNECT_TIMEOUT_SEC", "15")),
            read=float(os.environ.get("LUMI_OPENAI_READ_TIMEOUT_SEC", "600")),
            write=60.0,
            pool=60.0,
        )

    @property
    def effective_context_tokens(self) -> int:
        return self._capabilities.context_window

    @property
    def capability_profile(self) -> ModelCapabilities:
        return self._capabilities

    @classmethod
    def list_available_models(
        cls,
        api_key: str,
        *,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = 5.0,
        transport=None,
    ) -> list[str]:
        """Chat-capable models this key can use, or the documented defaults."""
        if not str(api_key or "").strip():
            return []
        try:
            with httpx.Client(**net.client_options(timeout=timeout, transport=transport)) as client:
                response = client.get(f"{str(base_url or DEFAULT_BASE_URL).rstrip('/')}/models",
                                      headers={"Authorization": f"Bearer {api_key}"})
                response.raise_for_status()
                rows = response.json().get("data") or []
            ids = sorted(
                str(row.get("id")) for row in rows
                if isinstance(row, dict) and row.get("id") and _CHAT_MODEL.match(str(row["id"]))
                and not any(token in str(row["id"]).lower() for token in _NOT_CHAT)
            )
            return ids or list(DEFAULT_MODELS)
        except (httpx.HTTPError, ValueError):
            return list(DEFAULT_MODELS)

    def health(self) -> dict:
        with httpx.Client(**net.client_options(timeout=10.0, transport=self._transport)) as client:
            response = client.get(self._url("models"), headers=self._request_headers())
        if response.status_code >= 400:
            error_type, message = self._error_details(response)
            raise ValueError(self._user_error_message(response.status_code, error_type, message))
        rows = response.json().get("data") or []
        return {"status": "ready", "backend": self.name,
                "models": [row["id"] for row in rows if isinstance(row, dict) and row.get("id")]}

    def _url(self, path: str) -> str:
        url = f"{self.base_url}/{path}"
        if self.api_version:
            url += f"?api-version={self.api_version}"
        return url

    def _request_headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json", "Accept": "text/event-stream", **self.extra_headers}
        if self.api_key and self.auth_header != "none":
            if self.auth_header.lower() == "authorization":
                headers["Authorization"] = f"Bearer {self.api_key}"
            else:
                headers[self.auth_header] = self.api_key
        return headers

    def _reasoning_by_call(self, history: list) -> dict[str, list[dict]]:
        replay: dict[str, list[dict]] = {}
        for turn in history:
            if turn.get("role") != "tool_call" or turn.get("provider_model") != self.model:
                continue
            items = [
                {key: value for key, value in detail.items() if key != "provider"}
                for detail in turn.get("reasoning_details") or []
                if isinstance(detail, dict) and detail.get("provider") == REASONING_PROVIDER
                and detail.get("type") == "reasoning" and detail.get("encrypted_content")
            ]
            if items:
                replay[str(turn.get("call_id") or "")] = items
        return replay

    def _payload(self, user_msg, conversation_history, instructions, tools, max_tokens) -> dict:
        tool_defs = response_tools(tools)
        chat = KimiBackend._messages(
            self, conversation_history, instructions, user_msg,
            declared_tool_names={tool["name"] for tool in tool_defs},
        )
        system, items = to_response_input(chat, self._reasoning_by_call(conversation_history))
        payload: dict[str, Any] = {"model": self.model, "input": items, "stream": True, "store": False}
        if system.strip():
            payload["instructions"] = system
        if tool_defs:
            payload["tools"] = tool_defs
            payload["tool_choice"] = "auto"
            payload["parallel_tool_calls"] = True
        if is_reasoning_model(self.model):
            reasoning: dict[str, Any] = {"summary": "auto"}
            effort = REASONING_EFFORT.get(self.thinking_mode)
            if self.thinking_mode == "off" and self.model.lower().startswith("gpt-5"):
                effort = "minimal"
            if effort:
                reasoning["effort"] = effort
            payload["reasoning"] = reasoning
            payload["include"] = ["reasoning.encrypted_content"]
        if max_tokens:
            payload["max_output_tokens"] = max(16, int(max_tokens))
        return payload

    @staticmethod
    def _error_details(response: httpx.Response) -> tuple[str, str]:
        try:
            body = response.read().decode("utf-8", errors="replace").strip()
            parsed = json.loads(body)
            error = parsed.get("error") if isinstance(parsed, dict) else None
            error = error if isinstance(error, dict) else {}
            error_type = str(error.get("code") or error.get("type") or "").strip().lower()
            return error_type, str(error.get("message") or body or f"HTTP {response.status_code}")[:500]
        except Exception:
            return "", f"HTTP {response.status_code}"

    def _user_error_message(self, status_code: int, error_type: str, message: str) -> str:
        label = self.PROVIDER_LABEL
        if status_code == 429 and self._is_quota_error(error_type, message):
            return f"{label} quota exhausted. Check the account's billing and limits, then retry."
        if status_code == 401:
            return f"{label} rejected the API key. Check it in Settings."
        if status_code == 403:
            return f"{label}: this key can't use {self.model} ({message})."
        if status_code == 404:
            return f"{label} doesn't have {'deployment' if self.azure else 'model'} {self.model}. Check the name."
        if status_code == 400 and "context" in message.lower():
            return (f"The conversation is longer than {self.model}'s context window. "
                    "Compact the session or start a new one.")
        if status_code == 429:
            return f"{label} rate limit reached. Wait a moment and try again."
        return f"{label} request failed ({status_code}): {message}"

    def _http_retry_delay(self, response: httpx.Response, attempt: int) -> float | None:
        try:
            after = float(response.headers.get("retry-after") or 0)
        except ValueError:
            after = 0
        return min(max(after, 1.5 * (2 ** attempt)), 30.0)

    def stream(
        self,
        user_msg: str,
        conversation_history: list,
        instructions: str,
        tools: list,
        max_tokens: int | None = None,
        cancel_event=None,
    ) -> Iterator[Tuple[str, dict]]:
        payload = self._payload(user_msg, conversation_history, instructions, tools, max_tokens)
        headers = self._request_headers()
        items: dict[int, dict] = {}
        final_output: list[dict] = []
        reasoning_text: list[str] = []
        usage: dict[str, Any] = {}
        response_id = ""
        emitted_text = False
        last_status = 0.0
        try:
            with httpx.Client(**net.client_options(timeout=self._timeout, transport=self._transport)) as client:
                attempt = 0
                while True:
                    if cancel_event is not None and cancel_event.is_set():
                        return
                    restart = False
                    with client.stream("POST", self._url("responses"), headers=headers, json=payload) as response:
                        if response.status_code >= 400:
                            error_type, message = self._error_details(response)
                            retryable = self._is_retryable_error(response.status_code, error_type, message)
                            logger.warning("%s request failed: status=%d type=%s retryable=%s model=%s",
                                           self.PROVIDER_LABEL, response.status_code, error_type or "unknown",
                                           retryable, self.model)
                            if retryable and attempt < 2:
                                delay = self._http_retry_delay(response, attempt)
                                yield (EVENT_BACKEND_STATUS, {
                                    "kind": self.RETRY_EVENT_KIND, "status_code": response.status_code,
                                    "attempt": attempt + 1, "max": 3, "model": self.model,
                                    "backoff_seconds": delay, "body_preview": message,
                                })
                                if _wait_with_cancel(delay, cancel_event):
                                    return
                                attempt += 1
                                continue
                            yield (EVENT_ERROR, {"message": self._user_error_message(
                                response.status_code, error_type, message)})
                            return
                        for line in response.iter_lines():
                            if cancel_event is not None and cancel_event.is_set():
                                return
                            line = str(line or "").strip()
                            if not line.startswith("data:"):
                                continue
                            raw = line[5:].strip()
                            if not raw or raw == "[DONE]":
                                continue
                            try:
                                event = json.loads(raw)
                            except ValueError:
                                continue
                            kind = str(event.get("type") or "")
                            phase = ""
                            if kind in {"response.created", "response.in_progress"}:
                                response_id = str((event.get("response") or {}).get("id") or response_id)
                            elif kind == "response.output_item.added":
                                items[int(event.get("output_index") or 0)] = dict(event.get("item") or {})
                            elif kind == "response.output_text.delta":
                                delta = str(event.get("delta") or "")
                                if delta:
                                    emitted_text = True
                                    yield (EVENT_TEXT_DELTA, {"delta": delta})
                            elif kind == "response.function_call_arguments.delta":
                                item = items.setdefault(int(event.get("output_index") or 0),
                                                        {"type": "function_call", "arguments": ""})
                                item["arguments"] = str(item.get("arguments") or "") + str(event.get("delta") or "")
                                phase = "generating_code"
                            elif kind == "response.output_item.done":
                                items[int(event.get("output_index") or 0)] = dict(event.get("item") or {})
                            elif kind == "response.reasoning_summary_text.delta":
                                reasoning_text.append(str(event.get("delta") or ""))
                                phase = "reasoning"
                            elif kind in {"response.completed", "response.incomplete"}:
                                done = event.get("response") or {}
                                usage = done.get("usage") or usage
                                final_output = [item for item in done.get("output") or [] if isinstance(item, dict)]
                                break
                            elif kind in {"response.failed", "error"}:
                                error = (event.get("response") or {}).get("error") if kind == "response.failed" else event
                                error = error if isinstance(error, dict) else {}
                                message = str(error.get("message") or "Unknown provider error")
                                code = str(error.get("code") or "")
                                if code in {"server_error", "rate_limit_exceeded"} and attempt < 2:
                                    delay = 1.5 * (2 ** attempt)
                                    yield (EVENT_BACKEND_STATUS, {
                                        "kind": self.RETRY_EVENT_KIND, "status_code": 0,
                                        "attempt": attempt + 1, "max": 3, "model": self.model,
                                        "backoff_seconds": delay, "message": message[:500],
                                        "reset_partial_output": emitted_text,
                                    })
                                    if _wait_with_cancel(delay, cancel_event):
                                        return
                                    items.clear()
                                    reasoning_text.clear()
                                    emitted_text = False
                                    attempt += 1
                                    restart = True
                                    break
                                yield (EVENT_ERROR, {"message": f"{self.PROVIDER_LABEL} generation failed: {message}"})
                                return
                            now = time.monotonic()
                            if phase and now - last_status >= 2:
                                last_status = now
                                yield (EVENT_BACKEND_STATUS, {"kind": "generation_progress",
                                                              "phase": phase, "model": self.model})
                    if restart:
                        continue
                    break
        except httpx.TimeoutException:
            yield (EVENT_ERROR, {"message": self._timeout_error_message()})
            return
        except httpx.HTTPError as exc:
            yield (EVENT_ERROR, {"message": f"{self.PROVIDER_LABEL} connection failed: {type(exc).__name__}"})
            return
        output = final_output or [items[index] for index in sorted(items)]
        yield from self._finish(output, reasoning_text, usage, response_id)

    def _finish(self, output: list[dict], reasoning_text: list[str], usage: dict,
                response_id: str) -> Iterator[Tuple[str, dict]]:
        assistant_content = ""
        calls: list[dict] = []
        reasoning: list[dict] = []
        summaries: list[str] = []
        for index, item in enumerate(output):
            kind = item.get("type")
            if kind == "message":
                assistant_content += "".join(
                    str(part.get("text") or "") for part in item.get("content") or []
                    if isinstance(part, dict) and part.get("type") == "output_text"
                )
            elif kind == "function_call":
                arguments = str(item.get("arguments") or "{}")
                calls.append({
                    "id": str(item.get("call_id") or _new_call_id(str(item.get("name")), arguments, index)),
                    "type": "function",
                    "function": {"name": str(item.get("name") or ""), "arguments": arguments},
                })
            elif kind == "reasoning":
                entry = {"type": "reasoning", "id": item.get("id"), "summary": item.get("summary") or [],
                         "encrypted_content": item.get("encrypted_content"), "provider": REASONING_PROVIDER}
                reasoning.append({key: value for key, value in entry.items() if value is not None})
                summaries.extend(str(part.get("text") or "") for part in item.get("summary") or []
                                 if isinstance(part, dict))
        reasoning_content = "\n\n".join(text for text in summaries if text) or "".join(reasoning_text)
        stable_id = response_id or _new_call_id(self.model, assistant_content + reasoning_content, 0)
        for call in calls:
            yield (EVENT_TOOL_CALL, {
                "name": call["function"]["name"],
                "arguments": call["function"]["arguments"],
                "call_id": call["id"],
                "reasoning_content": reasoning_content,
                "reasoning_details": reasoning,
                "provider_model": self.model,
                "assistant_content": assistant_content,
                "response_id": stable_id,
                "response_tool_calls": calls,
            })
        details = usage.get("input_tokens_details") or {}
        output_details = usage.get("output_tokens_details") or {}
        yield (EVENT_DONE, {
            "model": self.model,
            "stats": {
                "input_tokens": int(usage.get("input_tokens") or 0),
                "output_tokens": int(usage.get("output_tokens") or 0),
                "cached_tokens": int(details.get("cached_tokens") or 0),
                "reasoning_tokens": int(output_details.get("reasoning_tokens") or 0),
                "provider": self.name,
            },
            "cognitive_state": None,
        })
