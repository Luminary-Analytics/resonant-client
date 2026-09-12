"""SONN's project-scoped Chat Completions transport and model discovery."""

from __future__ import annotations

import copy
import hashlib
import json
import threading
import time
from urllib.parse import urlsplit

import httpx

from .backends import EVENT_ERROR, ExoBackend, KimiBackend, _convert_tools_for_ollama
from .capabilities import ModelCapabilities
from .network_defaults import resolve_sonn_url


class SonnBackend(KimiBackend):
    """Use the standard OpenAI wire format without Moonshot/OpenRouter extensions."""

    PROVIDER_LABEL = "SONN"
    RETRY_EVENT_KIND = "sonn_retry"
    DEFAULT_MODEL = "sonn-auto"
    dynamic_tool_catalog_via_history = False
    # Enables the inherited watcher to close a blocked local stream on Stop.
    # SONN's supplied contract does not define a server-side cancel endpoint.
    supports_remote_cancel = True
    _catalog_lock = threading.Lock()
    _catalogs: dict[tuple[str, str], tuple[float, list[dict]]] = {}

    @staticmethod
    def validate_base_url(value: str) -> str:
        """Preserve the project path and reject credentials embedded in URLs."""
        value = str(value or "").strip().rstrip("/")
        try:
            parsed = urlsplit(value)
            local_http = parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1", "::1"}
            if (not parsed.hostname or not (parsed.scheme == "https" or local_http)
                    or parsed.username or parsed.password or parsed.query or parsed.fragment):
                raise ValueError
            parsed.port  # Validate malformed ports before a request is constructed.
        except ValueError:
            raise ValueError("Set a SONN API base URL in Settings → Network (HTTPS, or HTTP on localhost).") from None
        return value

    @classmethod
    def _cache_key(cls, base_url: str, api_key: str) -> tuple[str, str]:
        return base_url, hashlib.sha256(api_key.encode()).hexdigest()

    @classmethod
    def catalog(cls, api_key: str, *, base_url: str, force: bool = False, transport=None) -> list[dict]:
        """Discover models, caching for five minutes per project URL and credential."""
        base_url = cls.validate_base_url(base_url)
        api_key = str(api_key or "").strip()
        if not api_key or api_key == "YOUR_PRIVATE_INVITATION":
            raise ValueError("Add your SONN API key in Settings → API keys, or set SONN_API_KEY.")
        cache_key = cls._cache_key(base_url, api_key)
        with cls._catalog_lock:
            cached = cls._catalogs.get(cache_key)
            if cached and not force and time.monotonic() - cached[0] < 300:
                return copy.deepcopy(cached[1])
        # Never hold the catalog lock during network I/O or serialize other accounts.
        try:
            with httpx.Client(timeout=5.0, transport=transport) as client:
                response = client.get(f"{base_url}/models", headers={"Authorization": f"Bearer {api_key}"})
                if response.status_code != 200:
                    with cls._catalog_lock:
                        cls._catalogs.pop(cache_key, None)
                    raise ValueError(cls._user_error_message(response.status_code, "", ""))
                body = response.json()
            if not isinstance(body, dict) or not isinstance(body.get("data"), list):
                raise ValueError("SONN returned an invalid model catalog.")
            rows = []
            seen = set()
            for row in body["data"]:
                if not isinstance(row, dict) or not isinstance(row.get("id"), str) or not row["id"].strip():
                    continue
                model = row["id"].strip()
                if model in seen:
                    continue
                seen.add(model)
                # Model discovery has no required capability extensions. Keep only
                # recognized fields; never echo arbitrary server metadata into UI state.
                clean = {"id": model, "name": str(row.get("name") or model)}
                window = row.get("context_length")
                if isinstance(window, int) and not isinstance(window, bool) and window > 0:
                    clean["context_length"] = window
                rows.append(clean)
        except httpx.HTTPError:
            raise ValueError("Cannot reach SONN. Check the API base URL and your connection.") from None
        except (TypeError, KeyError, json.JSONDecodeError):
            raise ValueError("SONN returned an invalid model catalog.") from None
        with cls._catalog_lock:
            if cache_key not in cls._catalogs and len(cls._catalogs) >= 16:
                oldest = min(cls._catalogs, key=lambda key: cls._catalogs[key][0])
                cls._catalogs.pop(oldest)
            cls._catalogs[cache_key] = (time.monotonic(), rows)
        return copy.deepcopy(rows)

    def __init__(self, api_key: str, model: str = DEFAULT_MODEL, *, base_url: str = "", transport=None):
        self.base_url = self.validate_base_url(resolve_sonn_url(base_url))
        self.api_key = str(api_key or "").strip()
        if not self.api_key or self.api_key == "YOUR_PRIVATE_INVITATION":
            raise ValueError("Add your SONN API key in Settings → API keys, or set SONN_API_KEY.")
        self.model = str(model or self.DEFAULT_MODEL).strip()
        self.name = "sonn"
        self.handles_tools = False
        self.thinking_mode = ""
        self._transport = transport
        self._timeout = httpx.Timeout(connect=15, read=180, write=60, pool=60)
        with self._catalog_lock:
            cached = self._catalogs.get(self._cache_key(self.base_url, self.api_key))
            rows = cached[1] if cached and time.monotonic() - cached[0] < 300 else []
            row = next((item for item in rows if item["id"] == self.model), {})
        # Standard /models does not advertise vision, reasoning, or context limits.
        # Try ordinary function tools; retain the engine's text fallback. Do not
        # infer capabilities of the routed model from the sonn-auto alias.
        self._capabilities = ModelCapabilities(
            model=self.model, context_window=row.get("context_length", 32768),
            native_tools=True, parallel_tools=None, modalities=("text",),
            max_safe_concurrency=1, source="fallback",
        )

    @property
    def effective_context_tokens(self) -> int:
        return self._capabilities.context_window

    def _api_content(self, content):
        return ExoBackend._api_content(self, content)

    def _messages(self, conversation_history, instructions, user_msg, **kwargs):
        history = [turn for turn in conversation_history if turn.get("role") != "tool_catalog"]
        messages = super()._messages(history, instructions, user_msg, **kwargs)
        retained = [{"role": "system", "content": str(turn.get("content") or "")}
                    for turn in history if turn.get("role") == "system" and turn.get("content")]
        messages[1:1] = retained
        for message in messages:
            message.pop("reasoning_content", None)
        return messages

    def _payload(self, user_msg, conversation_history, instructions, tools, max_tokens):
        payload = {
            "model": self.model, "messages": self._messages(conversation_history, instructions, user_msg),
            "stream": True, "stream_options": {"include_usage": True},
        }
        converted = _convert_tools_for_ollama(tools)
        if converted:
            payload["tools"] = list({tool["function"]["name"]: tool for tool in converted}.values())
        if max_tokens:
            payload["max_tokens"] = max(1, int(max_tokens))
        return payload

    @classmethod
    def _user_error_message(cls, status_code, error_type, message):
        if status_code in {401, 403}:
            return "SONN rejected the request. Check your API key and access to the configured project."
        if status_code == 404:
            return "SONN endpoint or model not found. Check the project API base URL and model name."
        if status_code in {402, 429}:
            return "SONN usage or rate limit reached. Check your account allowance or retry later."
        return f"SONN request failed (HTTP {status_code}). Check the connection settings and retry."

    @classmethod
    def _error_details(cls, response):
        error_type, detail = KimiBackend._error_details(response)
        quota = "insufficient_quota" if cls._is_quota_error(error_type, detail) else ""
        return quota, cls._user_error_message(response.status_code, quota, "")

    def stream(self, *args, **kwargs):
        for event, data in super().stream(*args, **kwargs):
            if event == EVENT_ERROR and data.get("message", "").startswith("SONN generation failed:"):
                data = {**data, "message": "SONN generation failed. Check your project/model access and retry."}
            yield event, data

    def health(self) -> dict:
        """Check authenticated model discovery without generating tokens."""
        rows = self.catalog(self.api_key, base_url=self.base_url, force=True, transport=self._transport)
        return {"status": "ready", "backend": self.name, "model_count": len(rows),
                "models": [row["id"] for row in rows],
                "model_labels": {row["id"]: row["name"] for row in rows}}
