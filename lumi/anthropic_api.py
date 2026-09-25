"""Claude through the Anthropic Messages API: direct, Amazon Bedrock or Vertex AI.

History is rendered with the same Chat Completions converter as every other
API adapter (``KimiBackend._messages``), which already repairs orphaned tool
calls and moves tool results next to their calls. This module translates that
message list into Messages API content blocks.

Signed thinking blocks travel in a tool call's ``reasoning_details`` and are
replayed only to the model that produced them, as the Messages API requires
within a tool-use loop.
"""

from __future__ import annotations

import base64
import datetime
import hashlib
import hmac
import json
import logging
import os
import re
import shutil
import struct
import subprocess
import threading
import time
import urllib.parse
import zlib
from configparser import ConfigParser
from pathlib import Path
from typing import Any, Callable, Iterator, Tuple

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

DEFAULT_BASE_URL = "https://api.anthropic.com"
API_VERSION = "2023-06-01"
BEDROCK_VERSION = "bedrock-2023-05-31"
VERTEX_VERSION = "vertex-2023-10-16"
DEFAULT_MODELS = (
    "claude-opus-5-5",
    "claude-sonnet-5",
    "claude-fable-5-1",
    "claude-haiku-4-5-20251001",
)
# Extended-thinking budget per Lumi thinking mode; "default" and "off" send none.
THINKING_BUDGETS = {"low": 2048, "med": 6144, "medium": 6144, "high": 12288, "max": 24576}
THINKING_MODES = {"", "default", "off", *THINKING_BUDGETS}
DEFAULT_MAX_TOKENS = 16384
MAX_OUTPUT_TOKENS = 32000
REASONING_PROVIDER = "anthropic"
_EPHEMERAL = {"type": "ephemeral"}
_TOOL_ID = re.compile(r"[^A-Za-z0-9_-]")
_DATA_URL = re.compile(r"data:(image/[A-Za-z0-9.+-]+);base64,(.*)", re.DOTALL)
PLATFORMS = ("direct", "bedrock", "vertex")


def _tool_id(value: Any) -> str:
    """Messages API tool ids allow letters, digits, _ and - only."""
    cleaned = _TOOL_ID.sub("_", str(value or ""))
    return cleaned or "toolu_" + hashlib.sha1(str(value).encode()).hexdigest()[:16]


def _text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n\n".join(
            str(part.get("text") or "")
            for part in content
            if isinstance(part, dict) and part.get("type") in {"text", "input_text", "output_text"}
        )
    return "" if content is None else str(content)


def _content_blocks(content: Any) -> list[dict]:
    """Chat Completions user content (text or parts) as Messages API blocks."""
    if isinstance(content, str):
        return [{"type": "text", "text": content}] if content.strip() else []
    blocks: list[dict] = []
    for part in content or []:
        if not isinstance(part, dict):
            continue
        if part.get("type") == "text" and str(part.get("text") or "").strip():
            blocks.append({"type": "text", "text": str(part["text"])})
        elif part.get("type") == "image_url":
            url = str((part.get("image_url") or {}).get("url") or "")
            match = _DATA_URL.match(url)
            if match:
                blocks.append({
                    "type": "image",
                    "source": {"type": "base64", "media_type": match.group(1), "data": match.group(2)},
                })
    return blocks


def _parse_arguments(raw: Any) -> dict:
    if isinstance(raw, dict):
        return raw
    try:
        parsed = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {"_raw": str(raw)}
    return parsed if isinstance(parsed, dict) else {"_value": parsed}


def to_anthropic_messages(
    messages: list[dict],
    thinking_by_call: dict[str, list[dict]] | None = None,
) -> tuple[str, list[dict]]:
    """Translate Chat Completions messages into (system, Messages API messages).

    Consecutive messages with the same role merge, because the API requires
    strict user/assistant alternation. Tool results become ``tool_result``
    blocks at the front of the following user message.
    """
    thinking_by_call = thinking_by_call or {}
    system_parts: list[str] = []
    out: list[dict] = []

    def add(role: str, blocks: list[dict]) -> None:
        if not blocks:
            return
        if out and out[-1]["role"] == role:
            out[-1]["content"].extend(blocks)
        else:
            out.append({"role": role, "content": list(blocks)})

    for message in messages:
        role = message.get("role")
        if role == "system":
            if message.get("tools") and not message.get("content"):
                continue  # Kimi's in-history catalogue; tools arrive in the request.
            text = _text(message.get("content"))
            if text.strip():
                system_parts.append(text)
        elif role == "user":
            add("user", _content_blocks(message.get("content")))
        elif role == "assistant":
            blocks: list[dict] = []
            calls = [call for call in message.get("tool_calls") or [] if isinstance(call, dict)]
            for call in calls:
                replay = thinking_by_call.get(str(call.get("id") or ""))
                if replay:
                    blocks.extend(replay)
                    break
            text = _text(message.get("content"))
            if text.strip():
                blocks.append({"type": "text", "text": text})
            for call in calls:
                function = call.get("function") if isinstance(call.get("function"), dict) else {}
                blocks.append({
                    "type": "tool_use",
                    "id": _tool_id(call.get("id")),
                    "name": str(function.get("name") or "tool"),
                    "input": _parse_arguments(function.get("arguments")),
                })
            add("assistant", blocks)
        elif role == "tool":
            add("user", [{
                "type": "tool_result",
                "tool_use_id": _tool_id(message.get("tool_call_id")),
                "content": _text(message.get("content")) or "(no output)",
            }])
    if not out or out[0]["role"] != "user":
        out.insert(0, {"role": "user", "content": [{"type": "text", "text": "(The conversation continues.)"}]})
    return "\n\n".join(system_parts), out


def anthropic_tools(tools: list) -> list[dict]:
    converted: list[dict] = []
    seen: set[str] = set()
    for tool in _convert_tools_for_ollama(tools):
        function = tool.get("function") or {}
        name = str(function.get("name") or "")
        if not name or name in seen:
            continue
        seen.add(name)
        schema = function.get("parameters")
        if not isinstance(schema, dict) or schema.get("type") != "object":
            schema = {"type": "object", "properties": {}}
        converted.append({
            "name": name,
            "description": str(function.get("description") or ""),
            "input_schema": schema,
        })
    return converted


# ── Amazon Bedrock: SigV4 signing and the binary event stream ─────────────


def sigv4_headers(
    method: str,
    url: str,
    body: bytes,
    *,
    region: str,
    service: str,
    access_key: str,
    secret_key: str,
    session_token: str = "",
    headers: dict[str, str] | None = None,
    now: datetime.datetime | None = None,
    sign_content_hash: bool = True,
) -> dict[str, str]:
    """Headers that sign a request with AWS Signature Version 4.

    Non-S3 services sign each path segment URI-encoded twice, so a model id
    such as ``anthropic.claude-v1:0`` sent as ``%3A`` is signed as ``%253A``.
    """
    now = now or datetime.datetime.now(datetime.timezone.utc)
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    datestamp = now.strftime("%Y%m%d")
    parts = urllib.parse.urlsplit(url)
    payload_hash = hashlib.sha256(body).hexdigest()
    signed = {k.lower(): str(v).strip() for k, v in (headers or {}).items()}
    signed["host"] = parts.netloc
    signed["x-amz-date"] = amz_date
    if sign_content_hash:
        signed["x-amz-content-sha256"] = payload_hash
    if session_token:
        signed["x-amz-security-token"] = session_token
    canonical_uri = "/".join(
        urllib.parse.quote(segment, safe="-_.~") for segment in (parts.path or "/").split("/")
    ) or "/"
    query = urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
    canonical_query = "&".join(
        f"{urllib.parse.quote(k, safe='-_.~')}={urllib.parse.quote(v, safe='-_.~')}"
        for k, v in sorted(query)
    )
    names = sorted(signed)
    canonical_headers = "".join(f"{name}:{' '.join(signed[name].split())}\n" for name in names)
    signed_headers = ";".join(names)
    canonical_request = "\n".join([
        method.upper(), canonical_uri, canonical_query, canonical_headers, signed_headers, payload_hash,
    ])
    scope = f"{datestamp}/{region}/{service}/aws4_request"
    string_to_sign = "\n".join([
        "AWS4-HMAC-SHA256", amz_date, scope, hashlib.sha256(canonical_request.encode()).hexdigest(),
    ])

    def _hmac(key: bytes, msg: str) -> bytes:
        return hmac.new(key, msg.encode(), hashlib.sha256).digest()

    key = _hmac(("AWS4" + secret_key).encode(), datestamp)
    for part in (region, service, "aws4_request"):
        key = _hmac(key, part)
    signature = hmac.new(key, string_to_sign.encode(), hashlib.sha256).hexdigest()
    result = {name: signed[name] for name in names if name != "host"}
    result["authorization"] = (
        f"AWS4-HMAC-SHA256 Credential={access_key}/{scope}, "
        f"SignedHeaders={signed_headers}, Signature={signature}"
    )
    return result


def aws_credentials(profile: str = "") -> tuple[str, str, str]:
    """(access key, secret key, session token) from the standard AWS sources.

    Environment variables first, then botocore when installed (profiles,
    SSO, assumed roles), then static keys in ``~/.aws/credentials``.
    """
    if not profile:
        access = os.environ.get("AWS_ACCESS_KEY_ID", "")
        secret = os.environ.get("AWS_SECRET_ACCESS_KEY", "")
        if access and secret:
            return access, secret, os.environ.get("AWS_SESSION_TOKEN", "")
    try:
        import botocore.session  # type: ignore[import-not-found]

        credentials = botocore.session.Session(profile=profile or None).get_credentials()
        if credentials is not None:
            frozen = credentials.get_frozen_credentials()
            if frozen.access_key and frozen.secret_key:
                return frozen.access_key, frozen.secret_key, frozen.token or ""
    except ImportError:
        pass
    except Exception as exc:  # botocore raises its own profile/SSO errors
        raise ValueError(f"AWS credentials unavailable: {exc}") from exc
    path = Path(os.environ.get("AWS_SHARED_CREDENTIALS_FILE") or Path.home() / ".aws" / "credentials")
    parser = ConfigParser()
    if path.is_file():
        parser.read(path, encoding="utf-8")
    section = profile or os.environ.get("AWS_PROFILE") or "default"
    if parser.has_section(section):
        access = parser.get(section, "aws_access_key_id", fallback="")
        secret = parser.get(section, "aws_secret_access_key", fallback="")
        if access and secret:
            return access, secret, parser.get(section, "aws_session_token", fallback="")
    raise ValueError(
        "No AWS credentials found. Set AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY, "
        "configure a profile, or use a Bedrock API key."
    )


class EventStreamDecoder:
    """Incremental decoder for the ``application/vnd.amazon.eventstream`` framing."""

    def __init__(self) -> None:
        self._buffer = b""

    def feed(self, chunk: bytes) -> Iterator[tuple[dict[str, Any], bytes]]:
        self._buffer += chunk
        while len(self._buffer) >= 12:
            total, header_length, prelude_crc = struct.unpack(">III", self._buffer[:12])
            if zlib.crc32(self._buffer[:8]) & 0xFFFFFFFF != prelude_crc:
                raise ValueError("Bedrock event stream is corrupt (prelude checksum).")
            if len(self._buffer) < total:
                return
            frame, self._buffer = self._buffer[:total], self._buffer[total:]
            (message_crc,) = struct.unpack(">I", frame[-4:])
            if zlib.crc32(frame[:-4]) & 0xFFFFFFFF != message_crc:
                raise ValueError("Bedrock event stream is corrupt (message checksum).")
            headers = self._headers(frame[12:12 + header_length])
            yield headers, frame[12 + header_length:-4]

    @staticmethod
    def _headers(raw: bytes) -> dict[str, Any]:
        headers: dict[str, Any] = {}
        index = 0
        sizes = {2: 1, 3: 2, 4: 4, 5: 8, 8: 8, 9: 16}
        while index < len(raw):
            name_length = raw[index]
            name = raw[index + 1:index + 1 + name_length].decode()
            index += 1 + name_length
            value_type = raw[index]
            index += 1
            if value_type in (0, 1):
                headers[name] = value_type == 0
            elif value_type in (6, 7):
                (length,) = struct.unpack(">H", raw[index:index + 2])
                value = raw[index + 2:index + 2 + length]
                headers[name] = value.decode() if value_type == 7 else value
                index += 2 + length
            elif value_type in sizes:
                headers[name] = raw[index:index + sizes[value_type]]
                index += sizes[value_type]
            else:
                raise ValueError(f"Unknown event stream header type {value_type}.")
        return headers


def encode_event_frame(headers: dict[str, str], payload: bytes) -> bytes:
    """One event stream frame with string headers (used by tests and tools)."""
    header_bytes = b"".join(
        bytes([len(name)]) + name.encode() + b"\x07" + struct.pack(">H", len(value.encode())) + value.encode()
        for name, value in headers.items()
    )
    total = 12 + len(header_bytes) + len(payload) + 4
    prelude = struct.pack(">II", total, len(header_bytes))
    prelude += struct.pack(">I", zlib.crc32(prelude) & 0xFFFFFFFF)
    body = prelude + header_bytes + payload
    return body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)


# ── Google Vertex AI access tokens ──────────────────────────────────────

_token_lock = threading.Lock()
_token_cache: dict[str, Any] = {"token": "", "expires": 0.0}


def google_access_token() -> str:
    """An OAuth access token from Application Default Credentials.

    Uses google-auth when installed, otherwise the gcloud CLI.
    """
    with _token_lock:
        if _token_cache["token"] and time.time() < _token_cache["expires"]:
            return _token_cache["token"]
        token = ""
        try:
            import google.auth  # type: ignore[import-not-found]
            import google.auth.transport.requests  # type: ignore[import-not-found]

            credentials, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
            credentials.refresh(google.auth.transport.requests.Request())
            token = credentials.token or ""
        except ImportError:
            gcloud = shutil.which("gcloud")
            if gcloud:
                result = subprocess.run(
                    [gcloud, "auth", "application-default", "print-access-token"],
                    capture_output=True, text=True, timeout=30,
                )
                token = result.stdout.strip() if result.returncode == 0 else ""
        except Exception as exc:
            raise ValueError(f"Google credentials unavailable: {exc}") from exc
        if not token:
            raise ValueError(
                "No Google credentials found. Run `gcloud auth application-default login`, "
                "or set GOOGLE_APPLICATION_CREDENTIALS."
            )
        _token_cache.update(token=token, expires=time.time() + 45 * 60)
        return token


# ── Backend ──────────────────────────────────────────────────────────────


class AnthropicBackend(KimiBackend):
    """Claude via the Messages API, directly or through Bedrock or Vertex AI."""

    PROVIDER_LABEL = "Anthropic"
    RETRY_EVENT_KIND = "anthropic_retry"
    RETRYABLE_STATUS = {408, 409, 429, 500, 502, 503, 504, 529}
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
        platform: str = "direct",
        region: str = "",
        project: str = "",
        aws_profile: str = "",
        name: str = "anthropic",
        label: str = "Anthropic",
        headers: dict[str, str] | None = None,
        auth_header: str = "x-api-key",
        thinking: str | None = None,
        capability_overrides: dict[str, Any] | None = None,
        transport=None,
        credentials: Callable[[], tuple[str, str, str]] | None = None,
        access_token: Callable[[], str] | None = None,
    ):
        if platform not in PLATFORMS:
            raise ValueError(f"Unknown Claude platform {platform!r}.")
        self.platform = platform
        self.model = str(model or "").strip()
        if not self.model:
            raise ValueError("Choose a Claude model first.")
        self.api_key = str(api_key or "").strip()
        if platform == "direct" and not self.api_key and auth_header != "none":
            raise ValueError(
                "Add your Anthropic API key in Settings → API keys, or set ANTHROPIC_API_KEY."
            )
        if platform in {"bedrock", "vertex"} and not str(region or "").strip():
            raise ValueError(f"Set the {platform.title()} region for this connection.")
        if platform == "vertex" and not str(project or "").strip():
            raise ValueError("Set the Google Cloud project for this Vertex AI connection.")
        self.region = str(region or "").strip()
        self.project = str(project or "").strip()
        self.aws_profile = str(aws_profile or "").strip()
        self.base_url = str(base_url or "").rstrip("/") or self._default_base_url()
        self.name = name
        self.PROVIDER_LABEL = label or "Anthropic"
        self.extra_headers = dict(headers or {})
        self.auth_header = auth_header or "x-api-key"
        self.handles_tools = False
        mode = str(thinking or "").strip().lower()
        if mode not in THINKING_MODES:
            raise ValueError("Claude thinking must be off, low, med, high or max.")
        self.thinking_mode = mode or "default"
        self._transport = transport
        self._credentials = credentials or (lambda: aws_credentials(self.aws_profile))
        self._access_token = access_token or google_access_token
        profile = infer_model_capabilities(self.model)
        overrides = {
            key: value for key, value in (capability_overrides or {}).items()
            if key in {"context_window", "modalities"} and value
        }
        self._capabilities = ModelCapabilities(**{**profile.to_dict(), **overrides})
        self._timeout = httpx.Timeout(
            connect=float(os.environ.get("LUMI_ANTHROPIC_CONNECT_TIMEOUT_SEC", "15")),
            read=float(os.environ.get("LUMI_ANTHROPIC_READ_TIMEOUT_SEC", "600")),
            write=60.0,
            pool=60.0,
        )

    # ── Identity and capability ─────────────────────────────────────────

    def _default_base_url(self) -> str:
        if self.platform == "bedrock":
            return f"https://bedrock-runtime.{self.region}.amazonaws.com"
        if self.platform == "vertex":
            host = "aiplatform.googleapis.com" if self.region == "global" else f"{self.region}-aiplatform.googleapis.com"
            return f"https://{host}/v1/projects/{self.project}/locations/{self.region}"
        return DEFAULT_BASE_URL

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
        """Claude models this key can use, or the documented defaults."""
        if not str(api_key or "").strip():
            return []
        try:
            with httpx.Client(**net.client_options(timeout=timeout, transport=transport)) as client:
                response = client.get(
                    f"{str(base_url or DEFAULT_BASE_URL).rstrip('/')}/v1/models",
                    params={"limit": 100},
                    headers={"x-api-key": api_key, "anthropic-version": API_VERSION},
                )
                response.raise_for_status()
                rows = response.json().get("data") or []
            models = [str(row.get("id")) for row in rows if isinstance(row, dict) and row.get("id")]
            return models or list(DEFAULT_MODELS)
        except (httpx.HTTPError, ValueError):
            return list(DEFAULT_MODELS)

    def health(self) -> dict:
        if self.platform != "direct":
            self._auth_headers(b"{}", self._endpoint())  # proves credentials resolve
            return {"status": "ready", "backend": self.name, "models": [self.model]}
        with httpx.Client(**net.client_options(timeout=10.0, transport=self._transport)) as client:
            response = client.get(f"{self.base_url}/v1/models", params={"limit": 100},
                                  headers=self._request_headers())
        if response.status_code >= 400:
            error_type, message = self._error_details(response)
            raise ValueError(self._user_error_message(response.status_code, error_type, message))
        rows = response.json().get("data") or []
        return {"status": "ready", "backend": self.name,
                "models": [row["id"] for row in rows if isinstance(row, dict) and row.get("id")]}

    # ── Request ─────────────────────────────────────────────────────────

    def _endpoint(self) -> str:
        if self.platform == "bedrock":
            model = urllib.parse.quote(self.model, safe="")
            return f"{self.base_url}/model/{model}/invoke-with-response-stream"
        if self.platform == "vertex":
            return f"{self.base_url}/publishers/anthropic/models/{self.model}:streamRawPredict"
        return f"{self.base_url}/v1/messages"

    def _request_headers(self) -> dict[str, str]:
        if self.platform == "bedrock":
            # The API version travels in the body; the response is AWS event stream.
            return {"content-type": "application/json",
                    "accept": "application/vnd.amazon.eventstream", **self.extra_headers}
        if self.platform == "vertex":
            return {"content-type": "application/json", "accept": "text/event-stream", **self.extra_headers}
        headers = {"content-type": "application/json", "accept": "text/event-stream",
                   "anthropic-version": API_VERSION, **self.extra_headers}
        if self.auth_header != "none" and self.api_key:
            if self.auth_header.lower() == "authorization":
                headers["authorization"] = f"Bearer {self.api_key}"
            else:
                headers[self.auth_header] = self.api_key
        return headers

    def _auth_headers(self, body: bytes, url: str) -> dict[str, str]:
        if self.platform == "vertex":
            return {"authorization": f"Bearer {self._access_token()}"}
        if self.platform == "bedrock":
            bearer = self.api_key or os.environ.get("AWS_BEARER_TOKEN_BEDROCK", "")
            if bearer:
                return {"authorization": f"Bearer {bearer}"}
            access, secret, token = self._credentials()
            return sigv4_headers(
                "POST", url, body, region=self.region, service="bedrock",
                access_key=access, secret_key=secret, session_token=token,
                headers={"content-type": "application/json"},
            )
        return {}

    def _thinking_by_call(self, history: list) -> dict[str, list[dict]]:
        """Signed thinking this model produced, keyed by the tool call it preceded."""
        replay: dict[str, list[dict]] = {}
        for turn in history:
            if turn.get("role") != "tool_call" or turn.get("provider_model") != self.model:
                continue
            blocks = [
                {key: value for key, value in detail.items() if key != "provider"}
                for detail in turn.get("reasoning_details") or []
                if isinstance(detail, dict) and detail.get("provider") == REASONING_PROVIDER
                and detail.get("type") in {"thinking", "redacted_thinking"}
            ]
            if blocks:
                replay[_tool_id(turn.get("call_id"))] = blocks
                replay[str(turn.get("call_id") or "")] = blocks
        return replay

    def _payload(self, user_msg, conversation_history, instructions, tools, max_tokens) -> dict:
        tool_defs = anthropic_tools(tools)
        chat = KimiBackend._messages(
            self, conversation_history, instructions, user_msg,
            declared_tool_names={tool["name"] for tool in tool_defs},
        )
        system, messages = to_anthropic_messages(chat, self._thinking_by_call(conversation_history))
        budget = THINKING_BUDGETS.get(self.thinking_mode, 0)
        if budget and self._open_tool_loop_lacks_thinking(messages):
            # The API rejects a tool-use loop whose assistant turn started
            # without thinking (for example after switching models mid-loop).
            budget = 0
        limit = int(max_tokens or DEFAULT_MAX_TOKENS)
        if budget:
            limit = max(limit, budget + 4096)
        payload: dict[str, Any] = {
            "model": self.model,
            "max_tokens": min(limit, MAX_OUTPUT_TOKENS),
            "messages": messages,
            "stream": True,
        }
        if system.strip():
            payload["system"] = [{"type": "text", "text": system, "cache_control": dict(_EPHEMERAL)}]
        if tool_defs:
            tool_defs[-1] = {**tool_defs[-1], "cache_control": dict(_EPHEMERAL)}
            payload["tools"] = tool_defs
        if budget:
            payload["thinking"] = {"type": "enabled", "budget_tokens": budget}
        last_user = next((m for m in reversed(messages) if m["role"] == "user"), None)
        if last_user and last_user["content"]:
            last_user["content"][-1] = {**last_user["content"][-1], "cache_control": dict(_EPHEMERAL)}
        if self.platform != "direct":
            payload.pop("model")
            payload["anthropic_version"] = BEDROCK_VERSION if self.platform == "bedrock" else VERTEX_VERSION
            if self.platform == "bedrock":
                payload.pop("stream")
        return payload

    @staticmethod
    def _open_tool_loop_lacks_thinking(messages: list[dict]) -> bool:
        for message in reversed(messages):
            if message["role"] != "assistant":
                continue
            types = [block.get("type") for block in message["content"]]
            return "tool_use" in types and types[0] not in {"thinking", "redacted_thinking"}
        return False

    # ── Errors ──────────────────────────────────────────────────────────

    @staticmethod
    def _error_details(response: httpx.Response) -> tuple[str, str]:
        try:
            body = response.read().decode("utf-8", errors="replace").strip()
        except Exception:
            return "", f"HTTP {response.status_code}"
        error_type = str(response.headers.get("x-amzn-errortype") or "").split(":")[0].lower()
        try:
            parsed = json.loads(body)
        except ValueError:
            return error_type, (body or f"HTTP {response.status_code}")[:500]
        error = parsed.get("error") if isinstance(parsed, dict) else None
        if isinstance(error, dict):
            error_type = str(error.get("type") or error.get("status") or error_type).lower()
            message = str(error.get("message") or body)
        else:
            message = str((parsed or {}).get("message") or body) if isinstance(parsed, dict) else body
        return error_type, message[:500]

    def _is_retryable_error(self, status_code: int, error_type: str, message: str) -> bool:
        return status_code in self.RETRYABLE_STATUS

    def _http_retry_delay(self, response: httpx.Response, attempt: int) -> float | None:
        try:
            after = float(response.headers.get("retry-after") or 0)
        except ValueError:
            after = 0
        return min(max(after, 1.5 * (2 ** attempt)), 30.0)

    def _user_error_message(self, status_code: int, error_type: str, message: str) -> str:
        label = self.PROVIDER_LABEL
        lowered = message.lower()
        if status_code == 401 or "unrecognizedclient" in error_type or "authentication" in error_type:
            return f"{label} rejected the credentials. Check the key or sign-in for this connection."
        if status_code == 403:
            return f"{label}: this account can't use {self.model} ({message})."
        if status_code == 404:
            return f"{label} doesn't know the model {self.model}. Check the model name."
        if status_code == 400 and ("prompt is too long" in lowered or "too many tokens" in lowered):
            return (f"The conversation is longer than {self.model}'s context window. "
                    "Compact the session or start a new one.")
        if status_code == 413:
            return f"{label} rejected the request as too large. Remove large attachments and retry."
        if status_code == 429:
            return f"{label} rate limit reached. Wait a moment and try again."
        if status_code in {500, 502, 503, 504, 529}:
            return f"{label} is temporarily unavailable ({status_code}). Try again shortly."
        return f"{label} request failed ({status_code}): {message}"

    # ── Streaming ────────────────────────────────────────────────────────

    def _events(self, response: httpx.Response) -> Iterator[dict]:
        if self.platform == "bedrock":
            decoder = EventStreamDecoder()
            for chunk in response.iter_bytes():
                for headers, payload in decoder.feed(chunk):
                    if headers.get(":message-type") == "exception":
                        try:
                            detail = json.loads(payload or b"{}").get("message", "")
                        except ValueError:
                            detail = payload.decode(errors="replace")
                        yield {"type": "error", "error": {
                            "type": str(headers.get(":exception-type") or "bedrock_error"),
                            "message": str(detail),
                        }}
                        continue
                    try:
                        wrapper = json.loads(payload or b"{}")
                        yield json.loads(base64.b64decode(wrapper.get("bytes") or ""))
                    except (ValueError, TypeError):
                        continue
            return
        for line in response.iter_lines():
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
            if isinstance(event, dict):
                yield event

    def stream(
        self,
        user_msg: str,
        conversation_history: list,
        instructions: str,
        tools: list,
        max_tokens: int | None = None,
        cancel_event=None,
    ) -> Iterator[Tuple[str, dict]]:
        try:
            payload = self._payload(user_msg, conversation_history, instructions, tools, max_tokens)
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            url = self._endpoint()
        except ValueError as exc:
            yield (EVENT_ERROR, {"message": str(exc)})
            return
        blocks: dict[int, dict] = {}
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
                    headers = {**self._request_headers(), **self._auth_headers(body, url)}
                    restart = False
                    with client.stream("POST", url, headers=headers, content=body) as response:
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
                        for event in self._events(response):
                            if cancel_event is not None and cancel_event.is_set():
                                return
                            kind = event.get("type")
                            if kind == "message_start":
                                message = event.get("message") or {}
                                response_id = str(message.get("id") or response_id)
                                usage.update(message.get("usage") or {})
                            elif kind == "content_block_start":
                                block = dict(event.get("content_block") or {})
                                block.setdefault("text", "")
                                if block.get("type") == "tool_use":
                                    block["_json"] = ""
                                blocks[int(event.get("index") or 0)] = block
                            elif kind == "content_block_delta":
                                block = blocks.setdefault(int(event.get("index") or 0), {"type": "text", "text": ""})
                                delta = event.get("delta") or {}
                                delta_type = delta.get("type")
                                phase = ""
                                if delta_type == "text_delta":
                                    text = str(delta.get("text") or "")
                                    block["text"] = block.get("text", "") + text
                                    if text:
                                        emitted_text = True
                                        yield (EVENT_TEXT_DELTA, {"delta": text})
                                elif delta_type == "thinking_delta":
                                    block["thinking"] = str(block.get("thinking") or "") + str(delta.get("thinking") or "")
                                    phase = "reasoning"
                                elif delta_type == "signature_delta":
                                    block["signature"] = str(delta.get("signature") or "")
                                elif delta_type == "input_json_delta":
                                    block["_json"] = str(block.get("_json") or "") + str(delta.get("partial_json") or "")
                                    phase = "generating_code"
                                now = time.monotonic()
                                if phase and now - last_status >= 2:
                                    last_status = now
                                    yield (EVENT_BACKEND_STATUS, {"kind": "generation_progress",
                                                                  "phase": phase, "model": self.model})
                            elif kind == "message_delta":
                                usage.update(event.get("usage") or {})
                            elif kind == "message_stop":
                                break
                            elif kind == "error":
                                error = event.get("error") or {}
                                error_type = str(error.get("type") or "")
                                message = str(error.get("message") or "Unknown provider error")
                                transient = error_type in {"overloaded_error", "api_error"} or "throttl" in error_type.lower() \
                                    or "unavailable" in error_type.lower()
                                if transient and attempt < 2:
                                    delay = 1.5 * (2 ** attempt)
                                    yield (EVENT_BACKEND_STATUS, {
                                        "kind": self.RETRY_EVENT_KIND, "status_code": 0,
                                        "attempt": attempt + 1, "max": 3, "model": self.model,
                                        "backoff_seconds": delay, "message": message[:500],
                                        "reset_partial_output": emitted_text,
                                    })
                                    if _wait_with_cancel(delay, cancel_event):
                                        return
                                    blocks.clear()
                                    usage.clear()
                                    emitted_text = False
                                    attempt += 1
                                    restart = True
                                    break
                                yield (EVENT_ERROR, {"message": f"{self.PROVIDER_LABEL} generation failed: {message}",
                                                     "discard_partial_output": False})
                                return
                    if restart:
                        continue
                    break
        except httpx.TimeoutException:
            yield (EVENT_ERROR, {"message": self._timeout_error_message()})
            return
        except httpx.HTTPError as exc:
            yield (EVENT_ERROR, {"message": f"{self.PROVIDER_LABEL} connection failed: {type(exc).__name__}"})
            return
        except ValueError as exc:
            yield (EVENT_ERROR, {"message": str(exc)})
            return
        yield from self._finish(blocks, usage, response_id)

    def _finish(self, blocks: dict[int, dict], usage: dict, response_id: str) -> Iterator[Tuple[str, dict]]:
        ordered = [blocks[index] for index in sorted(blocks)]
        thinking = [
            ({"type": "thinking", "thinking": block.get("thinking", ""), "signature": block.get("signature", "")}
             if block.get("type") == "thinking"
             else {"type": "redacted_thinking", "data": block.get("data", "")})
            for block in ordered if block.get("type") in {"thinking", "redacted_thinking"}
        ]
        assistant_content = "".join(block.get("text", "") for block in ordered if block.get("type") == "text")
        calls = []
        for index, block in enumerate(ordered):
            if block.get("type") != "tool_use":
                continue
            raw = str(block.get("_json") or "").strip()
            if raw:
                arguments = raw
            else:
                arguments = json.dumps(block.get("input") or {}, ensure_ascii=False)
            calls.append({
                "id": str(block.get("id") or _new_call_id(str(block.get("name")), arguments, index)),
                "type": "function",
                "function": {"name": str(block.get("name") or ""), "arguments": arguments},
            })
        reasoning_text = "\n\n".join(item.get("thinking", "") for item in thinking if item.get("thinking"))
        stable_id = response_id or _new_call_id(self.model, assistant_content + reasoning_text, 0)
        details = [{**item, "provider": REASONING_PROVIDER} for item in thinking]
        for call in calls:
            yield (EVENT_TOOL_CALL, {
                "name": call["function"]["name"],
                "arguments": call["function"]["arguments"],
                "call_id": call["id"],
                "reasoning_content": reasoning_text,
                "reasoning_details": details,
                "provider_model": self.model,
                "assistant_content": assistant_content,
                "response_id": stable_id,
                "response_tool_calls": calls,
            })
        input_tokens = int(usage.get("input_tokens") or 0)
        cache_read = int(usage.get("cache_read_input_tokens") or 0)
        cache_write = int(usage.get("cache_creation_input_tokens") or 0)
        yield (EVENT_DONE, {
            "model": self.model,
            "stats": {
                "input_tokens": input_tokens + cache_read + cache_write,
                "output_tokens": int(usage.get("output_tokens") or 0),
                "cached_tokens": cache_read,
                "cache_write_tokens": cache_write,
                "provider": self.name,
            },
            "cognitive_state": None,
        })
