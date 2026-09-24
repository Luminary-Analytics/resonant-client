"""OpenRouter transport, catalog, and provider-specific wire normalization."""

from __future__ import annotations

import threading
import time

import httpx

from .backends import ExoBackend, KimiBackend, _convert_tools_for_ollama
from .capabilities import ModelCapabilities


class OpenRouterBackend(KimiBackend):
    PROVIDER_LABEL = "OpenRouter"
    RETRY_EVENT_KIND = "openrouter_retry"
    DEFAULT_BASE_URL = "https://openrouter.ai/api/v1"
    dynamic_tool_catalog_via_history = False
    # Closing our request is supported; OpenRouter has no remote cancel endpoint.
    supports_remote_cancel = True
    _catalog_lock = threading.Lock()
    _catalog: list[dict] = []
    _catalog_at = 0.0

    @classmethod
    def catalog(cls, *, force=False, transport=None) -> list[dict]:
        with cls._catalog_lock:
            if not force and time.monotonic() - cls._catalog_at < 300 and cls._catalog:
                return list(cls._catalog)
            with httpx.Client(timeout=5.0, transport=transport) as client:
                response = client.get(f"{cls.DEFAULT_BASE_URL}/models")
                response.raise_for_status()
                rows = response.json().get("data", [])
            cls._catalog = [row for row in rows if isinstance(row, dict)
                            and row.get("id") and not row["id"].endswith(":batch")
                            and "text" in ((row.get("architecture") or {}).get("output_modalities") or ["text"])
                            and "tools" in (row.get("supported_parameters") or [])]
            cls._catalog_at = time.monotonic()
            return list(cls._catalog)

    def __init__(self, api_key: str, model: str, *, transport=None, thinking=None):
        if not str(api_key or "").strip():
            raise ValueError("Add your OpenRouter API key in Settings → API keys, or set OPENROUTER_API_KEY.")
        if not str(model or "").strip():
            raise ValueError("Choose an OpenRouter model first.")
        self.api_key = api_key.strip()
        self.model = model.strip()
        self.name = "openrouter"
        self.base_url = self.DEFAULT_BASE_URL
        self.handles_tools = False
        self.thinking_mode = thinking or "default"
        self._transport = transport
        self._timeout = httpx.Timeout(connect=15, read=180, write=60, pool=60)
        row = next((row for row in self._catalog if row["id"] == self.model), {})
        self._model_info = row
        self._capabilities = ModelCapabilities(
            model=self.model, context_window=int(row.get("context_length") or 32768),
            modalities=tuple((row.get("architecture") or {}).get("input_modalities") or ["text"]),
            native_tools=True, parallel_tools=True, structured_output=None,
            reasoning_levels=(), prompt_caching=True, native_continuation=True,
            max_safe_concurrency=4, source="provider" if row else "fallback",
        )

    @property
    def effective_context_tokens(self):
        return self._capabilities.context_window

    def _cancel_remote_generation(self, response_id):
        return False

    def _request_headers(self):
        return {**super()._request_headers(), "X-OpenRouter-Title": "Resonant"}

    def _api_content(self, content):
        return ExoBackend._api_content(self, content)

    def _messages(self, conversation_history, instructions, user_msg, **kwargs):
        messages = super()._messages(conversation_history, instructions, user_msg, **kwargs)
        messages = [m for m in messages if not ("tools" in m and not m.get("content"))]
        details = {turn.get("call_id"): turn.get("reasoning_details")
                   for turn in conversation_history if turn.get("reasoning_details")
                   and turn.get("provider_model") == self.model}
        raw_reasoning = {turn.get("call_id"): turn.get("reasoning_content")
                         for turn in conversation_history if turn.get("reasoning_content")
                         and turn.get("provider_model") == self.model}
        for message in messages:
            # Moonshot's system.tools extension is not OpenRouter's tool API.
            message.pop("tools", None)
            message.pop("reasoning_content", None)
            for call in message.get("tool_calls", []):
                if details.get(call.get("id")):
                    message["reasoning_details"] = details[call["id"]]
                    break
                if raw_reasoning.get(call.get("id")):
                    message["reasoning"] = raw_reasoning[call["id"]]
                    break
        return messages

    def _payload(self, user_msg, conversation_history, instructions, tools, max_tokens):
        payload = {
            "model": self.model, "messages": self._messages(conversation_history, instructions, user_msg),
            "stream": True, "stream_options": {"include_usage": True},
        }
        converted = _convert_tools_for_ollama(tools)
        if converted:
            payload["tools"] = list({t["function"]["name"]: t for t in converted}.values())
            payload["provider"] = {"require_parameters": True}
        if max_tokens:
            limit = (self._model_info.get("top_provider") or {}).get("max_completion_tokens")
            payload["max_tokens"] = min(int(max_tokens), int(limit)) if limit else int(max_tokens)
        return payload

    @classmethod
    def _user_error_message(cls, status_code, error_type, message):
        if status_code in {401, 403}:
            return "OpenRouter rejected the request. Check your API key and model access in Settings → API keys."
        if status_code == 402:
            return "OpenRouter credits are exhausted. Add credits or select another provider."
        return f"OpenRouter request failed ({status_code}): {message}"

    def health(self):
        with httpx.Client(timeout=5.0, transport=self._transport) as client:
            response = client.get(f"{self.base_url}/key", headers=self._request_headers())
            if response.status_code != 200:
                raise ValueError(self._user_error_message(response.status_code, "", "Connection failed"))
            data = response.json().get("data") or {}
        return {"status": "ready", "backend": self.name,
                "usage": data.get("usage"), "limit_remaining": data.get("limit_remaining")}
