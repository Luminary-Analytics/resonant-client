"""Model connections defined as data: gateways, Azure, Bedrock, Vertex and more.

A connection is a validated settings entry (``settings["connections"]``)
naming a wire protocol, an endpoint, how to authenticate and which models to
offer. Its secret lives with the other API keys under ``conn_<id>``. Each
connection appears in the model picker as the backend ``conn-<id>``, so
adding one never requires code for that provider.
"""

from __future__ import annotations

import ipaddress
import re
import urllib.parse
from typing import Any

import httpx

from . import net
from .backends import KimiBackend
from .capabilities import ModelCapabilities, infer_model_capabilities

CONNECTION_TYPES: dict[str, str] = {
    "openai-compatible": "OpenAI-compatible (Chat Completions)",
    "openai": "OpenAI Responses API",
    "azure-openai": "Azure OpenAI",
    "anthropic": "Anthropic Messages API",
    "anthropic-bedrock": "Claude on Amazon Bedrock",
    "anthropic-vertex": "Claude on Google Vertex AI",
}
AUTH_METHODS = ("bearer", "header", "none", "aws", "google")
BACKEND_PREFIX = "conn-"
SECRET_PREFIX = "conn_"
_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")
_HEADER_NAME = re.compile(r"^[A-Za-z0-9!#$%&'*+.^_`|~-]{1,64}$")
_FORBIDDEN_HEADERS = {"host", "content-length", "transfer-encoding", "connection", "content-type"}
MAX_CONNECTIONS = 50


def backend_key(connection_id: str) -> str:
    return BACKEND_PREFIX + connection_id


def connection_id_from_backend(backend_type: str) -> str:
    return backend_type[len(BACKEND_PREFIX):] if str(backend_type or "").startswith(BACKEND_PREFIX) else ""


def secret_setting(connection_id: str) -> str:
    return SECRET_PREFIX + connection_id


def is_secret_setting(key: str) -> bool:
    return bool(re.fullmatch(r"conn_[a-z0-9][a-z0-9-]{0,39}", str(key or "")))


def _slug(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", str(text or "").lower()).strip("-")[:40].strip("-")
    return slug or "connection"


def _private_host(host: str) -> bool:
    host = host.strip("[]").lower()
    if host in {"localhost"} or host.endswith((".localhost", ".local", ".internal", ".lan")):
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    return address.is_loopback or address.is_private or address.is_link_local


def _base_url(value: Any, *, required: bool) -> str:
    url = str(value or "").strip().rstrip("/")
    if not url:
        if required:
            raise ValueError("Enter the endpoint URL for this connection.")
        return ""
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in {"http", "https"} or not parts.netloc:
        raise ValueError("The endpoint must be an http(s) URL.")
    if parts.username or parts.password:
        raise ValueError("Put credentials in the key field, not in the URL.")
    if parts.scheme == "http" and not _private_host(parts.hostname or ""):
        raise ValueError("Use https for endpoints outside your machine or private network.")
    return url


def normalize_connection(raw: Any, existing_ids: set[str] | None = None) -> dict[str, Any]:
    """Validate a connection from Settings or settings.json; raise ValueError with a fix."""
    if not isinstance(raw, dict):
        raise ValueError("A connection must be an object.")
    kind = str(raw.get("type") or "").strip()
    if kind not in CONNECTION_TYPES:
        raise ValueError(f"Choose a connection type: {', '.join(CONNECTION_TYPES)}.")
    name = " ".join(str(raw.get("name") or "").split())[:60]
    if not name:
        raise ValueError("Give the connection a name.")
    connection_id = str(raw.get("id") or "").strip().lower() or _slug(name)
    if not _ID.match(connection_id):
        raise ValueError("Connection ids use lowercase letters, digits and dashes (up to 40).")
    if existing_ids is not None and connection_id in existing_ids:
        raise ValueError(f"A connection called {connection_id!r} already exists.")

    needs_url = kind in {"openai-compatible", "azure-openai"}
    connection: dict[str, Any] = {
        "id": connection_id,
        "name": name,
        "type": kind,
        "base_url": _base_url(raw.get("base_url"), required=needs_url),
    }
    default_auth = {"anthropic-bedrock": "aws", "anthropic-vertex": "google"}.get(kind, "bearer")
    if kind == "azure-openai" or kind == "anthropic":
        default_auth = "header"
    auth = str(raw.get("auth") or default_auth).strip()
    if auth not in AUTH_METHODS:
        raise ValueError(f"Authentication must be one of: {', '.join(AUTH_METHODS)}.")
    if auth == "aws" and kind != "anthropic-bedrock":
        raise ValueError("AWS sign-in only applies to Bedrock connections.")
    if auth == "google" and kind != "anthropic-vertex":
        raise ValueError("Google sign-in only applies to Vertex AI connections.")
    connection["auth"] = auth
    default_header = {"azure-openai": "api-key", "anthropic": "x-api-key"}.get(kind, "Authorization")
    header = str(raw.get("auth_header") or default_header).strip()
    if auth == "header" and not _HEADER_NAME.match(header):
        raise ValueError("The key header name contains characters HTTP headers can't use.")
    connection["auth_header"] = header if auth == "header" else ""

    headers = raw.get("headers") or {}
    if not isinstance(headers, dict) or len(headers) > 20:
        raise ValueError("Extra headers must be a list of up to 20 name/value pairs.")
    clean_headers: dict[str, str] = {}
    for key, value in headers.items():
        key = str(key).strip()
        if not _HEADER_NAME.match(key) or key.lower() in _FORBIDDEN_HEADERS:
            raise ValueError(f"The header {key!r} can't be set on a connection.")
        clean_headers[key] = str(value)[:1000]
    connection["headers"] = clean_headers

    models = raw.get("models") or []
    if isinstance(models, str):
        models = [item for item in re.split(r"[\s,]+", models) if item]
    if not isinstance(models, list):
        raise ValueError("Models must be a list of model ids.")
    connection["models"] = list(dict.fromkeys(str(m).strip()[:200] for m in models if str(m).strip()))[:100]
    if kind in {"azure-openai", "anthropic-bedrock", "anthropic-vertex"} and not connection["models"]:
        raise ValueError("List at least one model or deployment name for this connection.")

    for key in ("region", "project", "api_version", "aws_profile"):
        value = str(raw.get(key) or "").strip()[:120]
        if value and not re.fullmatch(r"[A-Za-z0-9._@:/-]+", value):
            raise ValueError(f"The {key.replace('_', ' ')} contains unexpected characters.")
        connection[key] = value
    if kind in {"anthropic-bedrock", "anthropic-vertex"} and not connection["region"]:
        raise ValueError("Set the region for this connection.")
    if kind == "anthropic-vertex" and not connection["project"]:
        raise ValueError("Set the Google Cloud project for this connection.")

    window = raw.get("context_window")
    if window not in (None, "", 0):
        try:
            window = int(window)
        except (TypeError, ValueError):
            raise ValueError("The context window must be a number of tokens.") from None
        if not 1024 <= window <= 10_000_000:
            raise ValueError("The context window must be between 1,024 and 10,000,000 tokens.")
        connection["context_window"] = window
    if raw.get("vision") is not None:
        connection["vision"] = bool(raw.get("vision"))
    max_param = str(raw.get("max_tokens_param") or "max_tokens")
    if max_param not in {"max_tokens", "max_completion_tokens"}:
        raise ValueError("The output limit parameter must be max_tokens or max_completion_tokens.")
    connection["max_tokens_param"] = max_param
    effort = str(raw.get("reasoning_effort") or "")
    if effort not in {"", "low", "medium", "high"}:
        raise ValueError("Reasoning effort must be empty, low, medium or high.")
    connection["reasoning_effort"] = effort
    return connection


def list_connections(settings) -> list[dict[str, Any]]:
    """Valid connections from settings; malformed entries are skipped, not fatal."""
    raw = settings.get("connections") if settings is not None else None
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for entry in raw if isinstance(raw, list) else []:
        try:
            connection = normalize_connection(entry)
        except ValueError:
            continue
        if connection["id"] not in seen:
            seen.add(connection["id"])
            result.append(connection)
    return result


def find_connection(settings, connection_id: str) -> dict[str, Any] | None:
    return next((c for c in list_connections(settings) if c["id"] == connection_id), None)


def capability_overrides(connection: dict[str, Any]) -> dict[str, Any]:
    overrides: dict[str, Any] = {}
    if connection.get("context_window"):
        overrides["context_window"] = int(connection["context_window"])
    if connection.get("vision") is not None:
        overrides["modalities"] = ("text", "image") if connection["vision"] else ("text",)
    return overrides


class OpenAICompatibleBackend(KimiBackend):
    """Any Chat Completions endpoint: LiteLLM, vLLM, an internal gateway."""

    RETRY_EVENT_KIND = "connection_retry"
    supports_dynamic_tool_catalog = True
    dynamic_tool_catalog_via_history = False

    def __init__(self, connection: dict[str, Any], model: str, api_key: str = "", *,
                 thinking: str | None = None, transport=None):
        self.connection = connection
        self.model = str(model or "").strip()
        if not self.model:
            raise ValueError(f"Choose a model for {connection['name']}.")
        self.api_key = str(api_key or "").strip()
        if connection["auth"] in {"bearer", "header"} and not self.api_key:
            raise ValueError(f"Add the key for {connection['name']} in Settings → Connections.")
        self.base_url = connection["base_url"]
        self.name = backend_key(connection["id"])
        self.PROVIDER_LABEL = connection["name"]
        self.handles_tools = False
        self.thinking_mode = thinking or "default"
        self._transport = transport
        profile = infer_model_capabilities(self.model)
        self._capabilities = ModelCapabilities(**{**profile.to_dict(), **capability_overrides(connection)})
        self._timeout = httpx.Timeout(connect=15.0, read=600.0, write=60.0, pool=60.0)

    @property
    def effective_context_tokens(self) -> int:
        return self._capabilities.context_window

    @property
    def capability_profile(self) -> ModelCapabilities:
        return self._capabilities

    def _request_headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json", "Accept": "text/event-stream",
                   **self.connection.get("headers", {})}
        auth = self.connection["auth"]
        if auth == "bearer" and self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        elif auth == "header" and self.api_key:
            headers[self.connection["auth_header"]] = self.api_key
        return headers

    def _messages(self, conversation_history, instructions, user_msg, **kwargs):
        messages = super()._messages(conversation_history, instructions, user_msg, **kwargs)
        # Only Kimi understands in-history tool catalogues; strip them here.
        cleaned = [m for m in messages if not (m.get("tools") and not m.get("content"))]
        for message in cleaned:
            message.pop("reasoning_content", None)
        return cleaned

    def _payload(self, user_msg, conversation_history, instructions, tools, max_tokens) -> dict:
        payload = super()._payload(user_msg, conversation_history, instructions, tools, max_tokens)
        # Many compatible servers reject unknown parameters, so reasoning
        # effort is sent only when the connection says the endpoint takes it.
        payload.pop("reasoning_effort", None)
        if self.connection.get("reasoning_effort"):
            payload["reasoning_effort"] = self.connection["reasoning_effort"]
        limit = payload.pop("max_completion_tokens", None)
        if limit:
            payload[self.connection.get("max_tokens_param") or "max_tokens"] = limit
        return payload

    def _user_error_message(self, status_code, error_type, message):
        label = self.PROVIDER_LABEL
        if status_code in {401, 403}:
            return f"{label} rejected the request ({status_code}). Check the connection's key and headers."
        if status_code == 404:
            return f"{label} has no model {self.model}, or the endpoint URL is wrong."
        return f"{label} request failed ({status_code}): {message}"


def discover_models(connection: dict[str, Any], api_key: str = "", *, timeout: float = 5.0,
                    transport=None) -> list[str]:
    """Models to offer: the configured list, else what the endpoint reports."""
    if connection.get("models"):
        return list(connection["models"])
    kind = connection["type"]
    if kind in {"anthropic-bedrock", "anthropic-vertex", "azure-openai"}:
        return []
    if kind == "anthropic":
        from .anthropic_api import AnthropicBackend

        return AnthropicBackend.list_available_models(
            api_key, base_url=connection.get("base_url") or "https://api.anthropic.com",
            timeout=timeout, transport=transport)
    if kind == "openai":
        from .openai_api import OpenAIResponsesBackend

        return OpenAIResponsesBackend.list_available_models(
            api_key, base_url=connection.get("base_url") or "https://api.openai.com/v1",
            timeout=timeout, transport=transport)
    headers = {**connection.get("headers", {})}
    if connection["auth"] == "bearer" and api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    elif connection["auth"] == "header" and api_key:
        headers[connection["auth_header"]] = api_key
    try:
        with httpx.Client(**net.client_options(timeout=timeout, transport=transport)) as client:
            response = client.get(f"{connection['base_url']}/models", headers=headers)
            response.raise_for_status()
            rows = response.json().get("data") or []
    except (httpx.HTTPError, ValueError):
        return []
    return [str(row.get("id")) for row in rows if isinstance(row, dict) and row.get("id")][:200]


def create_connection_backend(connection: dict[str, Any], model: str, api_key: str = "", *,
                              thinking: str | None = None, transport=None):
    """The backend for one connection and model."""
    kind = connection["type"]
    overrides = capability_overrides(connection)
    common_headers = connection.get("headers") or {}
    if kind == "openai-compatible":
        return OpenAICompatibleBackend(connection, model, api_key, thinking=thinking, transport=transport)
    if kind in {"openai", "azure-openai"}:
        from .openai_api import OpenAIResponsesBackend

        auth = connection["auth"]
        header = "none" if auth == "none" else (
            connection["auth_header"] if auth == "header" else "authorization")
        return OpenAIResponsesBackend(
            api_key, model, base_url=connection.get("base_url") or "", azure=kind == "azure-openai",
            api_version=connection.get("api_version") or "", name=backend_key(connection["id"]),
            label=connection["name"], headers=common_headers, auth_header=header, thinking=thinking,
            capability_overrides=overrides, transport=transport,
        )
    from .anthropic_api import AnthropicBackend

    platform = {"anthropic": "direct", "anthropic-bedrock": "bedrock", "anthropic-vertex": "vertex"}[kind]
    auth = connection["auth"]
    header = "none" if auth == "none" else (
        connection["auth_header"] if auth == "header" else "authorization")
    return AnthropicBackend(
        api_key, model, base_url=connection.get("base_url") or "", platform=platform,
        region=connection.get("region") or "", project=connection.get("project") or "",
        aws_profile=connection.get("aws_profile") or "", name=backend_key(connection["id"]),
        label=connection["name"], headers=common_headers, auth_header=header, thinking=thinking,
        capability_overrides=overrides, transport=transport,
    )
