"""Claude through the Anthropic Messages API: direct, Amazon Bedrock or Vertex AI.

History is rendered with the same Chat Completions converter as every other
API adapter (``KimiBackend._messages``), which already repairs orphaned tool
calls and moves tool results next to their calls. This module translates that
message list into Messages API content blocks.

Lumi's thinking level goes out in the shape the model's family accepts
(``lumi/claude_models.py``): adaptive thinking with an effort level on
current models, a fixed thinking budget on the older ones that only take
that, never a parameter the model refuses. Bedrock and Vertex AI carry the
same body fields.

Signed thinking blocks travel in a tool call's ``reasoning_details`` and are
replayed only to the model that produced them, as the Messages API requires
within a tool-use loop, in the order the model wrote them (``block_order``),
and never once the conversation they're bound to has changed
(``bound_thinking``).
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

from . import dlp, net
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
from .claude_models import claude_model
from .executables import find_program

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
# Lumi's thinking levels ("default" sends nothing; "off" is per family,
# lumi/claude_models.py). Models that take adaptive thinking get the level as
# its effort; models that take only a fixed budget get this many tokens.
EFFORT_LEVELS = {"low": "low", "med": "medium", "medium": "medium", "high": "high", "max": "max"}
THINKING_BUDGETS = {"low": 2048, "med": 6144, "medium": 6144, "high": 12288, "max": 24576}
THINKING_MODES = {"", "default", "off", *THINKING_BUDGETS}
DEFAULT_MAX_TOKENS = 16384
# Thinking counts toward max_tokens, and adaptive thinking has no budget of its
# own, so a request that may think asks for at least this much room at each
# effort (Anthropic suggests 64K at the highest levels). A smaller request (a
# session title, a summary) would otherwise end inside the thinking.
THINKING_ROOM = {"low": 16384, "medium": 16384, "high": 32000, "max": 64000}
REASONING_PROVIDER = "anthropic"
# A reasoning_details entry with what replaying a response's thinking needs:
# where its blocks sat ("order", see block_order) and a digest of the request
# that produced it ("prefix", see request_prefix).
REPLAY_DETAIL = "replay"
_THINKING_BLOCKS = frozenset({"thinking", "redacted_thinking"})
_EPHEMERAL = {"type": "ephemeral"}
_TOOL_ID = re.compile(r"[^A-Za-z0-9_-]")
_DATA_URL = re.compile(r"data:(image/[A-Za-z0-9.+-]+);base64,(.*)", re.DOTALL)
PLATFORMS = ("direct", "bedrock", "vertex")
# A 400 naming these refused the request's thinking or effort settings; one
# naming a signature or a modified block refused replayed thinking instead.
_THINKING_TERMS = ("thinking", "budget_tokens", "output_config", "effort", "between_tools")
_REPLAY_TERMS = ("signature", "cannot be modified")


def describe_thinking(payload: dict) -> str:
    """The thinking settings a request carries, in words, for error messages."""
    thinking = payload.get("thinking") if isinstance(payload.get("thinking"), dict) else {}
    effort = str((payload.get("output_config") or {}).get("effort") or "")
    kind = thinking.get("type")
    if kind == "enabled":
        return f"a {int(thinking.get('budget_tokens') or 0):,}-token thinking budget"
    if kind == "adaptive":
        return f"adaptive thinking at {effort} effort" if effort else "adaptive thinking"
    if kind == "disabled":
        return "thinking disabled"
    if kind == "between_tools":
        return "no thinking before the answer (between_tools)"
    return f"{effort} effort" if effort else ""


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


def block_order(blocks: list[dict]) -> list[list] | None:
    """Where a response's thinking, text and tool calls sat, when replay can't infer it.

    History keeps a response's signed thinking (``reasoning_details``), its
    text (``assistant_content``) and its tool calls apart, and a replayed turn
    is rebuilt as every thinking block, then the text, then the calls: the
    usual order. The API wants the assistant turn back exactly as it came (a
    thinking block is bound to what preceded it), so a response in any other
    order, such as a progress note before each of several calls or text before
    a thinking block, records it: ``["thinking", i]`` is its i-th thinking
    block, ``["text", n]`` the next n characters of its text and
    ``["tool_use", j]`` its j-th call. ``None`` when there's nothing to record.
    """
    order: list[list] = []
    counts = {"thinking": 0, "tool_use": 0}
    for block in blocks:
        kind = block.get("type")
        if kind in _THINKING_BLOCKS:
            order.append(["thinking", counts["thinking"]])
            counts["thinking"] += 1
        elif kind == "tool_use":
            order.append(["tool_use", counts["tool_use"]])
            counts["tool_use"] += 1
        elif kind == "text" and block.get("text"):
            order.append(["text", len(str(block["text"]))])
    shape = "".join({"thinking": "t", "text": "x", "tool_use": "u"}[kind] for kind, _ in order)
    if not counts["thinking"] or re.fullmatch(r"t*x?u*", shape):
        return None
    return order


def _order_fits(order: Any, thinking: list[dict], text: str, tools: list[dict]) -> bool:
    """Whether a recorded order still describes this turn (a redaction or older history may not)."""
    try:
        entries = [(str(kind), int(value)) for kind, value in order]
    except (TypeError, ValueError):
        return False
    return (all(kind in {"thinking", "text", "tool_use"} and value >= 0 for kind, value in entries)
            and [value for kind, value in entries if kind == "thinking"] == list(range(len(thinking)))
            and [value for kind, value in entries if kind == "tool_use"] == list(range(len(tools)))
            and sum(value for kind, value in entries if kind == "text") == len(text))


def _replayed_blocks(thinking: list[dict], text: str, tools: list[dict], order: Any = None) -> list[dict]:
    """An assistant turn's blocks, in the order the model wrote them (``block_order``)."""
    if not order or not _order_fits(order, thinking, text, tools):
        return [*thinking, *([{"type": "text", "text": text}] if text.strip() else []), *tools]
    blocks: list[dict] = []
    position = 0
    for kind, value in order:
        if kind == "thinking":
            blocks.append(thinking[int(value)])
        elif kind == "tool_use":
            blocks.append(tools[int(value)])
        else:
            segment = text[position:position + int(value)]
            position += int(value)
            if segment.strip():
                blocks.append({"type": "text", "text": segment})
    return blocks


def _without_cache_control(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _without_cache_control(item) for key, item in value.items() if key != "cache_control"}
    if isinstance(value, list):
        return [_without_cache_control(item) for item in value]
    return value


def _canonical(value: Any) -> bytes:
    """``value`` as bytes that change exactly when the API would see it changed (cache_control may move)."""
    return json.dumps(_without_cache_control(value), sort_keys=True, separators=(",", ":")).encode("ascii")


def _prefix_hash(system: Any, tools: Any) -> Any:
    digest = hashlib.sha256()
    digest.update(_canonical({"system": system or None, "tools": tools or None}))
    return digest


def _thinking_key(block: dict) -> str:
    return str(block.get("signature") or block.get("data") or "")


def request_prefix(payload: dict) -> str:
    """A digest of what a response's thinking is bound to: the request's system prompt, tools and messages."""
    digest = _prefix_hash(payload.get("system"), payload.get("tools"))
    for message in payload.get("messages") or []:
        digest.update(_canonical(message))
    return digest.hexdigest()


def bound_thinking(system: Any, tools: Any, messages: list[dict], prefixes: dict[str, str]) -> list[dict]:
    """``messages`` without the thinking a model with preserved thinking would refuse.

    Claude Opus 5.5, Fable 5.1 and Sonnet 5.5 bind each thinking block to the
    system prompt, the tools and every message before it, as they were sent.
    Lumi's system prompt carries each turn's recalled context, a
    ``search_tools`` call adds tools and a nudge is sent once, so an earlier
    block can be bound to a conversation that has since changed; replaying it
    is a 400 for accounts created on or after 2026-08-31. A response's blocks
    whose request (``prefixes``: its request_prefix digest by signature) no
    longer matches what now precedes them are left out, and the model
    continues without that reasoning. Blocks made after such a change, against
    the conversation as sent since, still match and stay. A block without a
    digest (older history) is left out too.
    """
    digest = _prefix_hash(system, tools)
    checked: list[dict] = []
    for message in messages:
        if message.get("role") == "assistant":
            keys = [_thinking_key(block) for block in message["content"] if block.get("type") in _THINKING_BLOCKS]
            if keys and any(prefixes.get(key) != digest.hexdigest() for key in keys):
                message = {**message, "content": [block for block in message["content"]
                                                  if block.get("type") not in _THINKING_BLOCKS]}
        checked.append(message)
        digest.update(_canonical(message))
    return checked


class ReplayedThinking(list):
    """A response's signed thinking blocks, as sent back.

    ``order`` is where they sat among its text and tool calls (block_order),
    ``prefix`` the digest of the request that produced them (request_prefix).
    """

    order: list | None = None
    prefix: str = ""


def to_anthropic_messages(
    messages: list[dict],
    thinking_by_call: dict[str, list[dict]] | None = None,
) -> tuple[str, list[dict]]:
    """Translate Chat Completions messages into (system, Messages API messages).

    Consecutive messages with the same role merge, because the API requires
    strict user/assistant alternation. Tool results become ``tool_result``
    blocks at the front of the following user message. An assistant turn's
    signed thinking (``thinking_by_call``) goes back in the order the model
    wrote it (``ReplayedThinking.order``, see :func:`block_order`).
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
            calls = [call for call in message.get("tool_calls") or [] if isinstance(call, dict)]
            replay: list[dict] = next((thinking_by_call[str(call.get("id") or "")] for call in calls
                                       if thinking_by_call.get(str(call.get("id") or ""))), [])
            tools: list[dict] = []
            for call in calls:
                function = call.get("function") if isinstance(call.get("function"), dict) else {}
                tools.append({
                    "type": "tool_use",
                    "id": _tool_id(call.get("id")),
                    "name": str(function.get("name") or "tool"),
                    "input": _parse_arguments(function.get("arguments")),
                })
            add("assistant", _replayed_blocks(replay, _text(message.get("content")), tools,
                                              getattr(replay, "order", None)))
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
            # The installed gcloud (a batch file on Windows; its arguments are
            # fixed), never one from Lumi's working folder (lumi/executables.py).
            gcloud = find_program("gcloud", scripts=True)
            if gcloud:
                from .processes import decode_output

                # Bytes: gcloud is a batch file on Windows, and a message in
                # the console's code page failed a text-mode pipe.
                result = subprocess.run(
                    [gcloud, "auth", "application-default", "print-access-token"],
                    capture_output=True, timeout=30,
                )
                token = decode_output(result.stdout).strip() if result.returncode == 0 else ""
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


@dlp.guard_backend
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
        token_provider: Callable[[], str] | None = None,
        tls=None,
    ):
        # OAuth sign-in to a gateway (lumi/auth_tokens.py): a fresh bearer
        # token per request instead of a key.
        self._token_provider = token_provider
        self._tls = tls
        if platform not in PLATFORMS:
            raise ValueError(f"Unknown Claude platform {platform!r}.")
        self.platform = platform
        self.model = str(model or "").strip()
        if not self.model:
            raise ValueError("Choose a Claude model first.")
        self.api_key = str(api_key or "").strip()
        if platform == "direct" and not self.api_key and auth_header != "none" and token_provider is None:
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
        # The tool definitions this conversation last offered (_payload).
        self._offered_tools: list[dict] = []
        self._credentials = credentials or (lambda: aws_credentials(self.aws_profile))
        self._access_token = access_token or google_access_token
        # Every model here is Claude, even one whose id doesn't say so (a
        # Bedrock inference profile ARN, a gateway's own name): an id Lumi
        # doesn't recognize is sent the newest family's shape.
        self._claude = claude_model(self.model)
        profile = infer_model_capabilities(self.model, claude=True)
        overrides = {
            key: value for key, value in (capability_overrides or {}).items()
            if key in {"context_window", "modalities"} and value
        }
        self._capabilities = ModelCapabilities(**{**profile.to_dict(), **overrides})
        # A model with no thinking levels (one that can't think, or an
        # organization's capability override saying "reasoning": false) is sent
        # no thinking settings at any level.
        self._thinking_controls = bool(self._capabilities.reasoning_levels)
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

    @property
    def uses_sign_in(self) -> bool:
        """Whether requests authenticate with a sign-in rather than a key.

        Vertex AI always uses Google credentials; Bedrock without a Bedrock
        API key signs each request with the AWS credential chain; a gateway
        can sign in with OAuth. Team participants use keys only
        (engine/swarming/connections.py).
        """
        if self._token_provider is not None or self.platform == "vertex":
            return True
        return self.platform == "bedrock" and not (self.api_key or os.environ.get("AWS_BEARER_TOKEN_BEDROCK", ""))

    @classmethod
    def list_available_models(
        cls,
        api_key: str,
        *,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = 5.0,
        transport=None,
        token: str = "",
        verify=None,
    ) -> list[str]:
        """Claude models this key (or a gateway's sign-in token) can use, or the documented defaults."""
        if not str(api_key or token or "").strip():
            return []
        credential = {"Authorization": f"Bearer {token}"} if token else {"x-api-key": api_key}
        try:
            with httpx.Client(**net.client_options(timeout=timeout, transport=transport, verify=verify,
                                                   feature="Anthropic")) as client:
                response = client.get(
                    f"{str(base_url or DEFAULT_BASE_URL).rstrip('/')}/v1/models",
                    params={"limit": 100},
                    headers={**credential, "anthropic-version": API_VERSION},
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
        with httpx.Client(**net.client_options(timeout=10.0, transport=self._transport, verify=self._tls,
                                               feature=self.PROVIDER_LABEL)) as client:
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
        if self._token_provider is not None:
            headers["authorization"] = f"Bearer {self._token_provider()}"
        elif self.auth_header != "none" and self.api_key:
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

    def _thinking_by_call(self, history: list) -> dict[str, ReplayedThinking]:
        """Signed thinking this model produced, keyed by the tool call it preceded.

        Each response's blocks carry where they sat (block_order) and the
        digest of the request that produced them (request_prefix).
        """
        replay: dict[str, ReplayedThinking] = {}
        for turn in history:
            if turn.get("role") != "tool_call" or turn.get("provider_model") != self.model:
                continue
            details = [detail for detail in turn.get("reasoning_details") or []
                       if isinstance(detail, dict) and detail.get("provider") == REASONING_PROVIDER]
            blocks = ReplayedThinking({key: value for key, value in detail.items() if key != "provider"}
                                      for detail in details if detail.get("type") in _THINKING_BLOCKS)
            if blocks:
                meta = next((detail for detail in details if detail.get("type") == REPLAY_DETAIL), {})
                blocks.order, blocks.prefix = meta.get("order"), str(meta.get("prefix") or "")
                replay[_tool_id(turn.get("call_id"))] = blocks
                replay[str(turn.get("call_id") or "")] = blocks
        return replay

    def _thinking_request(self, budget: int) -> tuple[dict | None, str, int]:
        """This request's ``thinking`` field, its effort, and the least max_tokens it needs.

        Lumi's level in the shape this Claude family accepts
        (lumi/claude_models.py): adaptive thinking at that effort where the
        model has it, a fixed budget where that's all it takes. "off" sends
        what stops thinking there (nothing, "disabled", or Sonnet 5.5's
        "between_tools"), or on a model that always thinks, the lowest
        effort. "default" sends nothing. Thinking counts toward max_tokens,
        so a request that may think asks for room for it.

        ``budget`` is the level's fixed budget, or 0 for a tool loop that
        began without thinking, which the API refuses to continue with one;
        adaptive thinking has no such rule, so only budget models use it.
        """
        family = self._claude
        mode = self.thinking_mode
        thinking: dict | None = None
        effort = ""
        if not self._thinking_controls or family.thinking == "none" or mode == "default":
            pass
        elif mode == "off":
            if family.off == "low_effort":
                effort = "low"
            elif family.off in {"disabled", "between_tools"}:
                thinking = {"type": family.off}
        elif family.thinking == "budget":
            return ({"type": "enabled", "budget_tokens": budget}, "", budget + 4096) if budget else (None, "", 0)
        else:
            thinking, effort = {"type": "adaptive"}, EFFORT_LEVELS[mode]
        thinks = (thinking or {}).get("type") == "adaptive" or (thinking is None and family.thinks_by_default)
        return thinking, effort, (THINKING_ROOM[effort or family.default_effort or "high"] if thinks else 0)

    def _payload(self, user_msg, conversation_history, instructions, tools, max_tokens) -> dict:
        tool_defs = anthropic_tools(tools)
        chat = KimiBackend._messages(
            self, conversation_history, instructions, user_msg,
            declared_tool_names={tool["name"] for tool in tool_defs},
        )
        system, messages = to_anthropic_messages(chat, self._thinking_by_call(conversation_history))
        tool_choice = None
        if tool_defs:
            self._offered_tools = list(tool_defs)
        elif self._offered_tools and any(block.get("type") in {"tool_use", "tool_result"}
                                         for message in messages for block in message["content"]):
            # A request that offers no tools after this conversation's earlier
            # ones did (a team participant's last request, engine/session.py):
            # the API refuses tool calls in the history without their
            # definitions, and a thinking block's signature binds the tool set.
            # The same definitions go out, and tool_choice none lets none run.
            tool_defs = list(self._offered_tools)
            tool_choice = {"type": "none"}
        budget = THINKING_BUDGETS.get(self.thinking_mode, 0)
        if budget and self._open_tool_loop_lacks_thinking(messages):
            # The API rejects a tool-use loop whose assistant turn started
            # without thinking (for example after switching models mid-loop).
            budget = 0
        system_blocks = [{"type": "text", "text": system}] if system.strip() else []
        if self._claude.binds_thinking:
            prefixes = {_thinking_key(block): replay.prefix
                        for replay in self._thinking_by_call(conversation_history).values() for block in replay}
            messages = bound_thinking(system_blocks, tool_defs, messages, prefixes)
        thinking, effort, room = self._thinking_request(budget)
        limit = max(int(max_tokens or DEFAULT_MAX_TOKENS), room)
        payload: dict[str, Any] = {
            "model": self.model,
            "max_tokens": min(limit, self._claude.max_output),
            "messages": messages,
            "stream": True,
        }
        if system_blocks:
            payload["system"] = [{**system_blocks[0], "cache_control": dict(_EPHEMERAL)}]
        if tool_defs:
            tool_defs[-1] = {**tool_defs[-1], "cache_control": dict(_EPHEMERAL)}
            payload["tools"] = tool_defs
        if tool_choice:
            payload["tool_choice"] = tool_choice
        # Every request of a turn carries the same settings, apart from a
        # budget left out above: the API keeps one thinking mode per assistant
        # turn, and a change restarts the prompt cache. Lumi never sends
        # temperature, top_p or top_k, which current models refuse.
        if thinking:
            payload["thinking"] = thinking
        if effort:
            payload["output_config"] = {"effort": effort}
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

    def _user_error_message(self, status_code: int, error_type: str, message: str, *, sent: str = "") -> str:
        """What to tell the person about a refused request; ``sent`` describes its thinking settings."""
        label = self.PROVIDER_LABEL
        lowered = message.lower()
        if (status_code == 400 and any(term in lowered for term in _THINKING_TERMS)
                and not any(term in lowered for term in _REPLAY_TERMS)):
            # The model refused Lumi's thinking or effort settings: say what was
            # sent and how to get going again, never just "request failed".
            what = f" ({sent})" if sent else ""
            detail = message.strip().rstrip(".")
            if not self._claude.known:
                return (f"{label} refused the thinking settings for {self.model}{what}: {detail}. Lumi doesn't "
                        "recognize this model, so it sent what the newest Claude models take. Set this model's "
                        "thinking level to the provider default, or choose a Claude model Lumi knows.")
            return (f"{label} refused the thinking settings Lumi sent for {self.model}{what}: {detail}. "
                    "Set this model's thinking level to the provider default.")
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
        sent = describe_thinking(payload)
        # What this response's thinking will be bound to (bound_thinking).
        prefix = request_prefix(payload) if self._claude.binds_thinking else ""
        blocks: dict[int, dict] = {}
        usage: dict[str, Any] = {}
        response_id = ""
        emitted_text = False
        last_status = 0.0
        # Under a team's supervision (engine/execution_guard.py) one stream()
        # is one generation (backends.KimiBackend.supervised_requests): only a
        # refusal that generated nothing, a rate limit or an overload before
        # any output, is waited out and sent again, and every error says
        # whether anything was generated. Otherwise overloads and server
        # errors are retried as before.
        supervised = getattr(self, "_supervised_single_request", False)
        attempts = 4 if supervised else 3
        stopped = False
        try:
            with httpx.Client(**net.client_options(timeout=self._timeout, transport=self._transport,
                                                   verify=self._tls, feature=self.PROVIDER_LABEL)) as client:
                attempt = 0
                while True:
                    if cancel_event is not None and cancel_event.is_set():
                        return
                    headers = {**self._request_headers(), **self._auth_headers(body, url)}
                    restart = False
                    with client.stream("POST", url, headers=headers, content=body) as response:
                        if response.status_code >= 400:
                            error_type, message = self._error_details(response)
                            # Anthropic sheds load with 529 (overloaded_error)
                            # before it processes a request, like a rate limit.
                            overloaded = response.status_code == 529 or error_type == "overloaded_error"
                            retryable = self._is_retryable_error(response.status_code, error_type, message) and (
                                not supervised or response.status_code == 429 or overloaded)
                            logger.warning("%s request failed: status=%d type=%s retryable=%s model=%s",
                                           self.PROVIDER_LABEL, response.status_code, error_type or "unknown",
                                           retryable, self.model)
                            if retryable and attempt < attempts - 1:
                                delay = (self._rate_limit_delay(response, attempt) if supervised
                                         else self._http_retry_delay(response, attempt))
                                yield (EVENT_BACKEND_STATUS, {
                                    "kind": self.RETRY_EVENT_KIND, "status_code": response.status_code,
                                    "attempt": attempt + 1, "max": 3, "model": self.model,
                                    "backoff_seconds": delay, "body_preview": message,
                                })
                                if _wait_with_cancel(delay, cancel_event):
                                    return
                                attempt += 1
                                continue
                            yield (EVENT_ERROR, {
                                "message": self._user_error_message(response.status_code, error_type, message,
                                                                    sent=sent),
                                # A number, safe to keep where provider text isn't (the guarded ledger).
                                "status_code": response.status_code,
                                # An overload refused the request before generating anything.
                                **({"before_output": True} if overloaded else {}),
                            })
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
                                stopped = True
                                break
                            elif kind == "error":
                                error = event.get("error") or {}
                                error_type = str(error.get("type") or "")
                                message = str(error.get("message") or "Unknown provider error")
                                lowered = error_type.lower()
                                overload = error_type == "overloaded_error" or "throttl" in lowered or "unavailable" in lowered
                                # Nothing was generated while no content block (text,
                                # thinking or a tool call) has started.
                                before_output = not blocks
                                if supervised:
                                    again = before_output and (overload or self._is_transient_overload(message))
                                    delay = min(30.0, 5.0 * (2 ** attempt))
                                else:
                                    again = overload or error_type == "api_error"
                                    delay = 1.5 * (2 ** attempt)
                                if again and attempt < attempts - 1:
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
                                                     "discard_partial_output": False,
                                                     # The guarded ledger settles an error before
                                                     # any output as known: nothing was generated.
                                                     "before_output": before_output})
                                return
                    if restart:
                        continue
                    break
            if supervised and not stopped:
                # A stream cut short isn't a complete response: whatever it
                # held stays uncertain rather than becoming a finished turn.
                yield (EVENT_ERROR, {"message": f"{self.PROVIDER_LABEL} response ended before it was complete."})
                return
        except httpx.TimeoutException:
            yield (EVENT_ERROR, {"message": self._timeout_error_message()})
            return
        except httpx.HTTPError as exc:
            yield (EVENT_ERROR, {"message": net.offline_message(exc)
                                 or f"{self.PROVIDER_LABEL} connection failed: {type(exc).__name__}"})
            return
        except ValueError as exc:
            yield (EVENT_ERROR, {"message": str(exc)})
            return
        yield from self._finish(blocks, usage, response_id, prefix)

    def _finish(self, blocks: dict[int, dict], usage: dict, response_id: str,
                prefix: str = "") -> Iterator[Tuple[str, dict]]:
        """The response's tool calls, with its signed thinking to replay, then its usage.

        ``prefix``: the request_prefix digest of the request, for a model that
        binds thinking to the conversation before it.
        """
        ordered = [blocks[index] for index in sorted(blocks)]
        thinking = [
            ({"type": "thinking", "thinking": block.get("thinking", ""), "signature": block.get("signature", "")}
             if block.get("type") == "thinking"
             else {"type": "redacted_thinking", "data": block.get("data", "")})
            for block in ordered if block.get("type") in _THINKING_BLOCKS
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
        # Every thinking block goes back, including an empty one (the default
        # display on current models returns no text, only the signature).
        details = [{**item, "provider": REASONING_PROVIDER} for item in thinking]
        replay = {"order": block_order(ordered), "prefix": prefix if thinking else ""}
        if any(replay.values()):
            details.append({"type": REPLAY_DETAIL, "provider": REASONING_PROVIDER,
                            **{key: value for key, value in replay.items() if value}})
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
