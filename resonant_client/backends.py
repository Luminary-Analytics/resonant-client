"""
Backend abstraction for Resonant Client.

Ollama is the local-first default, Kimi connects directly to Moonshot's
OpenAI-compatible API, and Codex delegates to the installed CLI. Every
provider yields the common event stream consumed by the session engine.
"""

import json
import hashlib
import logging
import os
import queue
import re
import shutil
import subprocess
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Tuple

import httpx

from .protocol import build_tool_system_prompt, parse_dsml_tool_calls, parse_tool_calls
from .content import content_text, normalize_content, ollama_message_content, text_fallback
from .capabilities import (
    ModelCapabilities,
    default_context_window,
    extract_reported_context_length,
    infer_model_capabilities,
)

logger = logging.getLogger(__name__)

# Common event types yielded by all backends
EVENT_TEXT_DELTA = "text.delta"           # {"delta": "..."}
EVENT_TOOL_CALL = "tool_call"             # {"name": ..., "arguments": ..., "call_id": ...}
EVENT_DONE = "done"                       # {"cognitive_state": ... or None, "stats": ...}
EVENT_ERROR = "error"                     # {"message": "..."}
# v0.5.6a1 — backend self-reports operational status (retries, slow
# upstream, throttling). Distinct from EVENT_ERROR (terminal) — these
# are recoverable conditions the user wants to know about so a slow
# response feels like "still working" rather than "stalled".
# Payload shape: {"kind": "...", **kind_specific_fields}
#   kind="ollama_retry":     {"status_code", "attempt", "max", "model",
#                             "backoff_seconds", "body_preview"}
#   kind="ollama_timeout":   {"attempt", "max", "model",
#                             "backoff_seconds"} — a connect/read
#                             timeout during the open phase is being
#                             retried. v0.6.4 (F6).
#   kind="ollama_exhausted": {"status_code", "model", "attempts",
#                             "body_preview", ["reason"]} — terminal:
#                             the retry budget is spent. v0.6.4 (F2 +
#                             F6). status_code 0 + reason="timeout"
#                             marks the timeout flavor. The GUI
#                             renders this as a persistent chip (the
#                             per-retry banner auto-fades).
EVENT_BACKEND_STATUS = "backend.status"


def _new_call_id(name: str, arguments: str, ordinal: int = 0) -> str:
    """Return a stable identifier that remains unique within one response."""
    payload = f"{ordinal}\0{name}\0{arguments}".encode("utf-8", errors="replace")
    return f"call_{hashlib.sha256(payload).hexdigest()[:8]}"


def _convert_tools_for_ollama(tools: list) -> list:
    """Convert AGENT_TOOLS (OpenAI format) to Ollama's native tools format."""
    ollama_tools = []
    for tool in tools:
        if isinstance(tool, dict) and tool.get("type") == "function":
            ollama_tools.append(tool)  # Already in the right format
        elif isinstance(tool, dict) and "name" in tool:
            # Wrap bare function defs
            ollama_tools.append({
                "type": "function",
                "function": tool,
            })
    return ollama_tools


def _detect_json_tool_calls(text: str) -> list:
    """
    Detect raw JSON tool calls in model output (fallback).
    Handles: {"name": "bash", "arguments": {"command": "..."}}
    """
    results = []
    for match in re.finditer(r'\{[^{}]*"name"\s*:\s*"[^"]+"\s*,\s*"arguments"\s*:\s*\{', text):
        start = match.start()
        depth = 0
        i = start
        while i < len(text):
            if text[i] == '{':
                depth += 1
            elif text[i] == '}':
                depth -= 1
                if depth == 0:
                    candidate = text[start:i+1]
                    try:
                        parsed = json.loads(candidate)
                        name = parsed.get("name", "")
                        args = parsed.get("arguments", {})
                        if name:
                            args_str = json.dumps(args) if isinstance(args, dict) else str(args)
                            call_id = _new_call_id(name, args_str, len(results))
                            results.append({
                                "name": name,
                                "arguments": args_str,
                                "call_id": call_id,
                            })
                    except (json.JSONDecodeError, ValueError):
                        pass
                    break
            i += 1
    return results


def _detect_text_tool_calls(text: str) -> list:
    """
    Detect tool invocations in model text output.
    Handles multiple formats models use:
      - bash(command_here)
      - bash command_here
      - file_read(path)
      - file_read path
    Only matches when the ENTIRE response (stripped) looks like a tool call,
    not when "bash" appears in a longer explanation.
    """
    text = text.strip()
    if not text:
        return []

    results = []
    known_tools = {"bash", "file_write", "file_read", "file_edit", "glob", "grep"}

    # First: try tool_name(args) format
    for match in re.finditer(r'\b(' + '|'.join(known_tools) + r')\((.+?)\)\s*$', text, re.DOTALL):
        name = match.group(1)
        raw_args = match.group(2).strip()
        tc = _build_simple_tool_call(name, raw_args)
        if tc:
            results.append(tc)

    if results:
        return results

    # Second: if entire response is "tool_name args" on one or few lines
    # Only match if response is short (< 200 chars) to avoid false positives
    if len(text) < 200:
        for tool_name in known_tools:
            if text.startswith(tool_name + " ") or text.startswith(tool_name + "\n"):
                raw_args = text[len(tool_name):].strip()
                if raw_args:
                    tc = _build_simple_tool_call(tool_name, raw_args)
                    if tc:
                        results.append(tc)
                        break

    return results


def _build_simple_tool_call(name: str, raw_args: str) -> dict | None:
    """Build a tool call dict from a name and raw argument string."""
    if name == "bash":
        args = {"command": raw_args}
    elif name in ("file_read", "glob", "grep"):
        args = {"path" if name == "file_read" else "pattern": raw_args}
    else:
        return None  # file_write, file_edit need multiple args

    args_str = json.dumps(args)
    call_id = _new_call_id(name, args_str)
    return {"name": name, "arguments": args_str, "call_id": call_id}


# ---------------------------------------------------------------------------
# Image-payload helpers (shared by Ollama image paths)
# ---------------------------------------------------------------------------

_DATA_URL_RE = re.compile(r"^data:image/[A-Za-z0-9.+-]+;base64,", re.IGNORECASE)


def _clean_image_b64(raw) -> str:
    """Normalize a base64 string for Ollama's `images` field.

    Ollama returns HTTP 400 (`illegal base64 data at input byte N`) if the
    string is empty, contains whitespace/newlines, or carries a `data:image/...;base64,`
    prefix. This helper strips those so we never poison a chat request with a
    payload that the server is guaranteed to reject.

    Returns "" when there's nothing usable.
    """
    if not raw:
        return ""
    if not isinstance(raw, str):
        return ""
    s = raw.strip()
    if not s:
        return ""
    s = _DATA_URL_RE.sub("", s)
    s = "".join(s.split())
    return s


def _safe_image_b64(image_payload) -> str:
    """Pull a clean base64 string out of a tool-result image dict, or return ''."""
    if not isinstance(image_payload, dict):
        return ""
    return _clean_image_b64(image_payload.get("data", ""))


def _message_text(content) -> str:
    """Extract comparable text from a scalar or multimodal history entry."""
    return content_text(content, include_fallbacks=False)


# ---------------------------------------------------------------------------
# Ollama backend
# ---------------------------------------------------------------------------


# v0.5.0a9 — retry config for transient Ollama 5xx responses.
#
# Ollama Cloud returns 503 "Server overloaded, please retry shortly
# (ref: <uuid>)" under capacity pressure. This is exactly the
# situation a thin retry-with-backoff fixes: the upstream is
# temporarily full, a short wait + retry usually succeeds. Found in
# the v0.5.0 GA smoke runs against deepseek-v4-flash:cloud where
# ~1 in 5 sub-mission dispatches initially failed; with these
# retries the success rate climbed to ~95%.
#
# 4 total attempts (1 initial + 3 retries), base 1.5s exponential:
# attempt 1 → 1.5s wait → attempt 2 → 3.0s → attempt 3 → 6.0s →
# attempt 4. Max total wait before give-up: 10.5s. We keep this
# tight so a genuinely-down upstream doesn't make the autonomous
# loop hang for minutes per call.
# v0.6.5 — env-configurable so a flaky multi-day cloud run can widen the
# budget (and a local-Ollama setup can fail fast). Read once at import.
_OLLAMA_MAX_RETRIES = int(os.environ.get("RESONANT_OLLAMA_MAX_RETRIES", "3") or "3")
_OLLAMA_BASE_BACKOFF = float(os.environ.get("RESONANT_OLLAMA_RETRY_BASE_BACKOFF", "1.5") or "1.5")
# v0.6.5 — 403/429 are the cloud rate-limiting our concurrency. Treat them
# as RETRYABLE (with backoff) rather than terminal, and tag them as
# rate-limit signals so the governor can shrink its cap. A persistent 403
# (e.g. real auth failure) is still bounded — the circuit breaker opens
# after repeated failures and the retry budget is small.
_OLLAMA_RATELIMIT_STATUS = frozenset({403, 429})
_OLLAMA_RETRYABLE_STATUS = frozenset({502, 503, 504, 522, 524}) | _OLLAMA_RATELIMIT_STATUS

# v0.6.5 — circuit breaker (long-running hardening). After this many
# CONSECUTIVE terminal failures against one endpoint, the breaker opens:
# subsequent calls fail FAST (skip the retry/backoff storm) for a cooldown
# window, and the backend emits a distinct `ollama_circuit_open` status so
# the daemon/GUI can tell "the endpoint is down" apart from "this one task
# failed". A single success closes it. Set the threshold to 0 to disable.
_OLLAMA_CIRCUIT_THRESHOLD = int(os.environ.get("RESONANT_OLLAMA_CIRCUIT_THRESHOLD", "5") or "5")
_OLLAMA_CIRCUIT_COOLDOWN = float(os.environ.get("RESONANT_OLLAMA_CIRCUIT_COOLDOWN_SEC", "60") or "60")

# v0.6.5 — outbound concurrency governor. Nothing else caps how many
# requests we fire at Ollama at once; under heavy autonomous/parallel load
# (multiple missions, batched sub-agents) that floods ollama.com — which
# proxies the :cloud models — and trips its per-account rate limit
# (403/429). The governor caps simultaneous in-flight requests at an
# ADAPTIVE limit and queues the rest. AIMD: a rate-limit signal
# multiplicatively shrinks the limit; a streak of successes additively
# grows it back toward the ceiling, so it self-finds the max safe
# throughput instead of relying on a hand-picked number. Tune the start /
# ceiling via RESONANT_OLLAMA_CONCURRENCY / RESONANT_OLLAMA_MAX_CONCURRENCY.
_OLLAMA_CONCURRENCY = int(os.environ.get("RESONANT_OLLAMA_CONCURRENCY", "4") or "4")
_OLLAMA_MAX_CONCURRENCY = int(os.environ.get("RESONANT_OLLAMA_MAX_CONCURRENCY", "8") or "8")
_OLLAMA_GOV_INCREASE_AFTER = 8     # consecutive successes before +1 slot
_OLLAMA_GOV_DECREASE_FACTOR = 0.5  # multiplicative shrink on a rate-limit


class _RequestGovernor:
    """Adaptive concurrency limiter shared across all backends/threads
    hitting one Ollama endpoint. Thread-safe. `acquire()` blocks
    (cancel-aware) until an in-flight slot is free under the current
    adaptive limit; `release()` frees one. AIMD adjusts the limit:
    `record_rate_limited()` shrinks it ×factor (down to 1),
    `record_success()` grows it +1 after a success streak (up to max)."""

    def __init__(self, start: int, max_limit: int):
        self._cv = threading.Condition()
        self._max = float(max(1, max_limit))
        self._limit = float(max(1, min(start, max_limit)))
        self._in_flight = 0
        self._ok_streak = 0

    def acquire(self, cancel_event=None, poll: float = 0.25) -> bool:
        """Reserve a slot. Returns True once reserved, or False if
        `cancel_event` fires while queued."""
        with self._cv:
            while self._in_flight >= int(self._limit):
                if cancel_event is not None and cancel_event.is_set():
                    return False
                self._cv.wait(timeout=poll)
                if cancel_event is not None and cancel_event.is_set():
                    return False
            self._in_flight += 1
            return True

    def release(self) -> None:
        with self._cv:
            if self._in_flight > 0:
                self._in_flight -= 1
            self._cv.notify()

    def record_success(self) -> None:
        with self._cv:
            self._ok_streak += 1
            if (self._ok_streak >= _OLLAMA_GOV_INCREASE_AFTER
                    and self._limit < self._max):
                self._limit = min(self._max, self._limit + 1)
                self._ok_streak = 0
                self._cv.notify_all()

    def record_rate_limited(self) -> None:
        with self._cv:
            self._limit = max(1.0, self._limit * _OLLAMA_GOV_DECREASE_FACTOR)
            self._ok_streak = 0

    @property
    def limit(self) -> int:
        with self._cv:
            return int(self._limit)

    @property
    def in_flight(self) -> int:
        with self._cv:
            return self._in_flight


def _wait_with_cancel(seconds: float, cancel_event) -> bool:
    """Sleep for `seconds`, but exit early if `cancel_event` fires.
    Returns True iff cancellation was observed during the wait
    (caller should bail). False on a normal full sleep."""
    if seconds <= 0:
        return cancel_event is not None and cancel_event.is_set()
    if cancel_event is None:
        time.sleep(seconds)
        return False
    # Poll cancel_event ~4x per second so an in-flight stop
    # propagates within ~250ms.
    deadline = time.time() + seconds
    while time.time() < deadline:
        if cancel_event.is_set():
            return True
        time.sleep(min(0.25, max(0.0, deadline - time.time())))
    return False


class OllamaBackend:
    """Direct connection to Ollama /api/chat with adaptive tool calling.

    Automatically detects whether a model supports native tool calling.
    If it does, tools are passed via the Ollama API. If not, tool definitions
    are injected into the system prompt and responses are parsed for
    <tool_call> XML blocks (same approach as LM Studio).

    Detection runs once per model and is cached for the session.
    """

    # Cache of model -> bool (True = supports native tools)
    _tool_support_cache: dict[str, bool] = {}
    # Cache of model -> bool (True = capabilities include "vision")
    _vision_support_cache: dict[str, bool] = {}
    # v0.6.5 — circuit-breaker state keyed by base_url (NOT model): backends
    # are recreated per session/specialist but all talk to the same Ollama
    # endpoint, so failure state must outlive a single instance.
    # base_url -> {"failures": int, "open_until": float}
    _circuit: dict[str, dict] = {}
    # v0.6.5 — one adaptive concurrency governor per endpoint, shared
    # across all backend instances/threads. base_url -> _RequestGovernor.
    _governors: dict[str, "_RequestGovernor"] = {}
    _governors_lock = threading.Lock()

    @staticmethod
    def _default_num_ctx(model: str) -> int:
        """Return a practical model-aware context target."""
        return default_context_window(model)

    def __init__(self, base_url: str, model: str, *, thinking: str | None = None):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.name = "ollama"
        self._capabilities = infer_model_capabilities(model)
        self._use_native_tools: bool | None = None  # None = not yet detected
        # CRITICAL: Options must be IDENTICAL across ALL requests (warm_up, stream, etc.)
        # for this backend instance. If any option differs between requests, Ollama may
        # UNLOAD and RELOAD the model. Tune via env once per process, not per call.
        #
        # Defaults are tuned for the Mac Studio (256GB unified memory) running deepseek-v4-flash:
        # 32k context (1M is the model's max but is wasteful for typical sessions),
        # 1024 batch size to keep the GPU fed, num_gpu=99 forces all layers onto Metal.
        # Override via RESONANT_OLLAMA_NUM_CTX/NUM_BATCH/NUM_GPU when needed.
        configured_num_ctx = os.environ.get("RESONANT_OLLAMA_NUM_CTX", "").strip()
        try:
            num_ctx = int(configured_num_ctx) if configured_num_ctx else self._default_num_ctx(model)
        except ValueError:
            num_ctx = self._default_num_ctx(model)
        self._ollama_options = {
            "num_gpu": int(os.environ.get("RESONANT_OLLAMA_NUM_GPU", "99")),
            "num_batch": int(os.environ.get("RESONANT_OLLAMA_NUM_BATCH", "1024")),
            "num_ctx": max(4_096, num_ctx),
        }
        # Thinking-mode. Sent as `options.think` (verified to work via
        # that path on glm-5.2:cloud, 2026-06-17). The internal token is
        # low/med/high; "med" is the deepseek spelling of the middle
        # tier. The WIRE value Ollama accepts is MODEL-DEPENDENT:
        # deepseek wants "med", but standard reasoning models (GLM-5.x)
        # want "medium" and reject "med" with HTTP 400. low/high are
        # universal. We keep the internal token stable (for round-trip
        # through the GUI/spec) and translate only on the way to the
        # wire — see `_wire_think_value`.
        raw = (thinking or "").strip().lower()
        self._ollama_think: str | bool | None = None
        if raw in {"", "off"}:
            self.thinking_mode = None
        elif raw in {"low", "med", "medium", "high", "max"}:
            normalized = "med" if raw == "medium" else raw
            self.thinking_mode = normalized
            self._ollama_think = self._wire_think_value(normalized)
        else:
            # Unknown value — drop silently rather than poisoning the dict
            self.thinking_mode = None
        # Keep models warm — first-load on a 284B MoE is several minutes.
        # Pin the vendor-tested sampling distribution. DeepSeek's thinking
        # mode ignores sampling controls, so omit them there.
        model_lower = self.model.lower()
        if "glm-5" in model_lower:
            self._ollama_options.update({"temperature": 1.0, "top_p": 0.95})
        elif "deepseek-v4" in model_lower and self._ollama_think is None:
            self._ollama_options.update({"temperature": 1.0, "top_p": 1.0})

        self._ollama_keep_alive = (os.environ.get("RESONANT_OLLAMA_KEEP_ALIVE", "120m").strip() or "120m")
        # deepseek-v4 with thinking can take a while; raise read timeout.
        # v0.6.4 (F6) — read timeout 240 → 300. A rigorous-grill cold
        # call on deepseek-v4-pro:cloud is legitimately a multi-minute
        # call (long prompt + deep reasoning); the v0.6.3 field run hit
        # the 240s ceiling. 300s gives headroom without waiting absurdly
        # long on a genuinely dead connection — and F6's timeout-retry
        # gives a slow-but-transient call a second chance regardless.
        self._ollama_http_timeout = float(os.environ.get("RESONANT_OLLAMA_HTTP_TIMEOUT_SEC", "360"))
        self._ollama_http_read_timeout = float(
            os.environ.get("RESONANT_OLLAMA_HTTP_READ_TIMEOUT_SEC", "300")
        )

    @property
    def effective_context_tokens(self) -> int:
        """Context window currently sent to Ollama and used by compression."""
        return int(self._ollama_options.get("num_ctx", 32_768) or 32_768)

    @property
    def capability_profile(self) -> ModelCapabilities:
        """Serializable, runtime-enriched model capabilities."""
        return self._capabilities

    def _apply_reported_capabilities(self, info: dict) -> None:
        reported = info.get("capabilities", []) or []
        model_info = info.get("model_info") or {}
        reported_window = extract_reported_context_length(model_info)
        self._capabilities = self._capabilities.with_runtime_metadata(
            reported if isinstance(reported, list) else (),
            context_window=reported_window,
        )
        self._apply_reported_context_length(model_info)

    def _apply_reported_context_length(self, model_info: dict) -> None:
        """Clamp the request window to a model's advertised maximum."""
        reported = extract_reported_context_length(model_info)
        if reported:
            if reported < self.effective_context_tokens:
                self._ollama_options["num_ctx"] = max(4_096, reported)

    def _wire_think_value(self, token: str) -> str:
        """Map the internal thinking token to the value THIS model's
        Ollama endpoint accepts. deepseek uses "med" for the middle
        tier; standard reasoning models (GLM-5.x and others) use
        "medium" and reject "med" with HTTP 400. "low"/"high" are
        universal. Verified live against glm-5.2:cloud (2026-06-17):
        "med" → 400, "medium"/"low"/"high" → accepted."""
        base = self.model.split(":")[0].lower()
        if token == "med" and not base.startswith("deepseek"):
            return "medium"
        return token

    def _with_thinking(self, payload: dict, *, enabled: bool = True) -> dict:
        """Attach Ollama's documented top-level ``think`` request field."""
        if enabled and self._ollama_think is not None:
            payload["think"] = self._ollama_think
        return payload

    def get_runtime_telemetry(self, *, timeout: float = 5.0) -> dict:
        """
        Best-effort runtime info about the loaded model: queries Ollama's
        /api/ps for currently-loaded models, and /api/show for static metadata.

        Returns:
            {
                "loaded_model": str,
                "context_length": int,
                "size_mb": float,
                "expires_at": str,         # ISO timestamp when keep_alive expires
                "raw_ps_entry": dict,
                "model_info": dict,        # selected fields from /api/show
                "supports_thinking": bool,
                "active_thinking": str,    # current think option, if any
            }
        Or {"error": str} if Ollama is unreachable / model not loaded.

        NOTE: MoE expert-utilization counters are not exposed by Ollama as of
        this writing; this method returns context_length + memory + thinking
        state instead, which is the next-most-useful telemetry.
        """
        import urllib.request
        import urllib.error

        result: dict = {
            "loaded_model": "",
            "context_length": 0,
            "size_mb": 0.0,
            "expires_at": "",
            "raw_ps_entry": {},
            "model_info": {},
            "supports_thinking": False,
            "active_thinking": self._ollama_think or "",
            "capability_profile": self._capabilities.to_dict(),
        }

        # /api/ps — what's loaded right now
        try:
            req = urllib.request.Request(
                f"{self.base_url}/api/ps",
                headers={"Accept": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                ps_data = json.loads(resp.read().decode("utf-8", errors="replace"))
        except (urllib.error.URLError, urllib.error.HTTPError, OSError, json.JSONDecodeError) as exc:
            return {"error": f"failed to query /api/ps: {exc}", **result}

        models_loaded = ps_data.get("models", [])
        match = None
        for m in models_loaded:
            if m.get("name") == self.model or m.get("model") == self.model:
                match = m
                break
        if not match and models_loaded:
            match = models_loaded[0]
        if match:
            result["loaded_model"] = match.get("name") or match.get("model") or ""
            result["context_length"] = int(match.get("context_length", 0) or match.get("size_vram", 0) or 0)
            size_bytes = match.get("size", 0) or match.get("size_vram", 0)
            result["size_mb"] = round(int(size_bytes) / 1024 / 1024, 1) if size_bytes else 0.0
            result["expires_at"] = match.get("expires_at", "")
            result["raw_ps_entry"] = match

        # /api/show — static model metadata (parameters, capabilities)
        try:
            payload = json.dumps({"name": self.model}).encode("utf-8")
            req = urllib.request.Request(
                f"{self.base_url}/api/show",
                data=payload,
                headers={"Content-Type": "application/json", "Accept": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                show_data = json.loads(resp.read().decode("utf-8", errors="replace"))
            # Extract a few interesting fields without dragging the whole modelfile
            result["model_info"] = {
                "details": show_data.get("details", {}),
                "model_info": {k: v for k, v in (show_data.get("model_info") or {}).items()
                               if isinstance(v, (str, int, float, bool))},
                "capabilities": show_data.get("capabilities", []),
            }
            params = (show_data.get("parameters") or "").lower()
            caps = " ".join(show_data.get("capabilities", []) or []).lower()
            result["supports_thinking"] = ("think" in params or "thinking" in caps or "reasoning" in caps)
            self._apply_reported_capabilities(show_data)
            result["capability_profile"] = self._capabilities.to_dict()
        except Exception:
            pass

        return result

    # Models known to support/not support native tool calling
    # (avoids the probe request for common models)
    _KNOWN_TOOL_SUPPORT = {
        # Supports native tools
        "llama3.1", "llama3.2", "llama3.3", "llama4",
        "qwen2.5", "qwen2.5-coder", "qwen3", "qwen3.5", "qwen3-coder-next", "qwen3-next",
        "mistral", "mistral-nemo", "mistral-small", "mistral-large",
        "command-r", "command-r-plus",
        "gemma2", "gemma3", "gemma4",
        "phi4", "phi4-mini",
        "deepseek-r1", "deepseek-v3.2", "deepseek-v4-flash", "deepseek-v4-pro", "deepseek-v4",
        # Ollama cloud models (routed, tool-capable)
        "minimax-m2", "minimax-m2.5", "minimax-m2.7",
        "nemotron-3-super", "nemotron-3-nano",
        "kimi-k2.5",
        "glm-4.7", "glm-4.7-flash", "glm-5", "glm-5.1", "glm-5.2",
        "devstral-2", "devstral-small-2",
        "cogito-2.1",
        "gemini-3-flash-preview",
        "ministral-3",
        "rnj-1",
    }

    # Cloud models to offer even if not yet pulled locally.
    # v0.6.5 — glm-5.2:cloud is the flagship (756B, 1M context, native
    # tool calling). Listed first so it surfaces at the top of the
    # picker. The deepseek-v4-pro/flash tiers stay just below as the
    # secondary high-quality option — pro's PLAN_DEEP convergence is
    # well characterized (docs/v0.5.1-smoke-results.md) and the
    # deepseek pair sits on a separate cloud quota, so it doubles as
    # the 503 fallback for the GLM flagship.
    CLOUD_MODELS = [
        "glm-5.2:cloud",
        "deepseek-v4-pro:cloud",
        "deepseek-v4-flash:cloud",
        "deepseek-v3.2:cloud",
        "minimax-m2.7:cloud",
        "minimax-m2.5:cloud",
        "nemotron-3-super:cloud",
        "kimi-k2.5:cloud",
        "glm-5.1:cloud",
        "glm-4.7-flash:cloud",
        "qwen3.5:cloud",
        "gemma4:cloud",
    ]
    _KNOWN_NO_TOOL_SUPPORT = {
        # Text-only, no native tool calling
        "llama2", "llama3",  # Pre-3.1
        "codellama",
        "phi", "phi3",
        "gemma",  # v1
        "starcoder", "starcoder2",
        "deepseek-coder", "deepseek-coder-v2",
        "yi",
    }

    def _detect_tool_support(self) -> bool:
        """Detect whether the current model supports native tool calling.

        Strategy:
        1. Check known model lists first (instant, no network)
        2. Check Ollama model info for 'tools' in template/capabilities
        3. Probe with a tiny tool call as last resort
        """
        # Check class-level cache
        if self.model in OllamaBackend._tool_support_cache:
            result = OllamaBackend._tool_support_cache[self.model]
            logger.info(f"Tool support for {self.model}: {result} (cached)")
            return result

        # Check known model lists (strip tag suffixes like :7b, :latest)
        base_model = self.model.split(":")[0].lower()
        if base_model in self._KNOWN_TOOL_SUPPORT:
            OllamaBackend._tool_support_cache[self.model] = True
            logger.info(f"Tool support for {self.model}: True (known)")
            return True
        if base_model in self._KNOWN_NO_TOOL_SUPPORT:
            OllamaBackend._tool_support_cache[self.model] = False
            logger.info(f"Tool support for {self.model}: False (known)")
            return False

        # Check Ollama model info endpoint for template/capabilities hints
        try:
            resp = httpx.post(
                f"{self.base_url}/api/show",
                json={"model": self.model},
                timeout=10,
            )
            if resp.status_code == 200:
                info = resp.json()
                self._apply_reported_capabilities(info)
                # v0.5.2a3 — check `capabilities` array FIRST. Cloud
                # models (`*:cloud`) typically have an empty template
                # field but DO declare capabilities. Falling through
                # to the template check left pro on text-mode and
                # the daemon was relying on our XML parser instead
                # of native tool calls.
                caps = info.get("capabilities", []) or []
                if isinstance(caps, list) and "tools" in caps:
                    OllamaBackend._tool_support_cache[self.model] = True
                    logger.info(f"Tool support for {self.model}: True (capabilities)")
                    return True
                template = info.get("template", "")
                # Models with tool support typically have {{.Tools}} in their template
                if "{{.Tools}}" in template or "{{ .Tools }}" in template:
                    OllamaBackend._tool_support_cache[self.model] = True
                    logger.info(f"Tool support for {self.model}: True (template)")
                    return True
                # If template exists but no tools placeholder, likely no support
                if template and "{{.Tools}}" not in template:
                    OllamaBackend._tool_support_cache[self.model] = False
                    logger.info(f"Tool support for {self.model}: False (no tools in template)")
                    return False
                # Empty template AND no tools-capability — defer to probe.
                if not template and (not isinstance(caps, list) or "tools" not in caps):
                    pass  # fall through to probe below
        except Exception as e:
            logger.debug(f"Could not check model info for {self.model}: {e}")

        # Probe: send a minimal request with a simple tool and check response format
        try:
            opts = dict(self._ollama_options)
            probe_tool = [{
                "type": "function",
                "function": {
                    "name": "test_tool",
                    "description": "Test tool",
                    "parameters": {
                        "type": "object",
                        "properties": {"value": {"type": "string"}},
                        "required": ["value"],
                    },
                },
            }]
            resp = httpx.post(
                f"{self.base_url}/api/chat",
                json=self._with_thinking({
                    "model": self.model,
                    "messages": [{"role": "user", "content": "Call test_tool with value 'hello'"}],
                    "tools": probe_tool,
                    "stream": False,
                    "keep_alive": self._ollama_keep_alive,
                    "options": opts,
                }),
                timeout=30,
            )
            if resp.status_code == 200:
                data = resp.json()
                msg = data.get("message", {})
                has_native = bool(msg.get("tool_calls"))
                OllamaBackend._tool_support_cache[self.model] = has_native
                logger.info(f"Tool support for {self.model}: {has_native} (probed)")
                return has_native
        except Exception as e:
            logger.debug(f"Tool probe failed for {self.model}: {e}")

        # Default: assume native support (will fall back to text parsing anyway)
        OllamaBackend._tool_support_cache[self.model] = True
        logger.info(f"Tool support for {self.model}: True (default)")
        return True

    @property
    def tool_mode(self) -> str:
        """Return the current tool calling mode: 'native', 'text', or 'unknown'."""
        if self._use_native_tools is None:
            return "unknown"
        return "native" if self._use_native_tools else "text"

    def supports_vision(self) -> bool:
        """Whether the current model accepts image attachments via /api/chat.

        Critical because text-only models (deepseek-v4-flash:cloud is a common one)
        will return HTTP 400 if `images: [...]` is included on any message — and once
        an image lands in conversation_history, every follow-up turn 400s too.

        Strategy:
        1. Cache hit → return cached result.
        2. Hit /api/show, look for "vision" in `capabilities`.
        3. Default to False on any failure (safer to drop images than poison the chat).
        """
        if self.model in OllamaBackend._vision_support_cache:
            return OllamaBackend._vision_support_cache[self.model]
        try:
            resp = httpx.post(
                f"{self.base_url}/api/show",
                json={"model": self.model},
                timeout=5,
            )
            if resp.status_code == 200:
                info = resp.json()
                self._apply_reported_capabilities(info)
                caps = info.get("capabilities", []) or []
                supported = any(str(c).lower() == "vision" for c in caps)
                OllamaBackend._vision_support_cache[self.model] = supported
                logger.info(f"Vision support for {self.model}: {supported} (capabilities={caps})")
                return supported
        except Exception as e:
            logger.debug(f"Vision-capability probe failed for {self.model}: {e}")
        # Conservative default: assume no vision so we never poison /api/chat with
        # images for a text-only model. Vision-capable models can opt in by listing
        # "vision" in their /api/show capabilities.
        OllamaBackend._vision_support_cache[self.model] = False
        logger.info(f"Vision support for {self.model}: False (default)")
        return False

    def warm_up(self):
        """Pre-load the model into Ollama's memory so the first request is fast."""
        try:
            # Use EXACT same options as stream() to prevent Ollama from reloading
            opts = dict(self._ollama_options)
            httpx.post(
                f"{self.base_url}/api/chat",
                json=self._with_thinking({
                    "model": self.model,
                    "messages": [{"role": "user", "content": "hi"}],
                    "stream": False,
                    "keep_alive": self._ollama_keep_alive,
                    "options": opts,
                }),
                timeout=max(120.0, self._ollama_http_timeout),
            )
            logger.info("Model %s warmed up", self.model)
        except Exception:
            pass  # Non-critical

    def health(self) -> dict:
        """Check Ollama is reachable and return model info."""
        try:
            resp = httpx.get(f"{self.base_url}/api/tags", timeout=5)
            resp.raise_for_status()
            data = resp.json()
            models = [m["name"] for m in data.get("models", [])]
            return {
                "status": "ready",
                "backend": "ollama",
                "model": self.model,
                "available_models": models,
            }
        except Exception as e:
            return {"status": "error", "message": str(e)}

    def generate_structured(
        self,
        user_msg: str,
        schema: dict,
        *,
        instructions: str = "Return only the requested JSON value.",
        max_tokens: int | None = None,
    ) -> dict | list:
        """Generate JSON with Ollama's schema-constrained ``format`` mode.

        Keep this separate from the conversational tool loop: it is intended
        for final machine-readable envelopes after free-form reasoning.
        """
        if self.model.lower().endswith(":cloud"):
            raise NotImplementedError(
                "Ollama Cloud does not currently support structured outputs"
            )
        if not isinstance(schema, dict) or not schema:
            raise ValueError("schema must be a non-empty JSON Schema object")
        options = dict(self._ollama_options)
        options["temperature"] = 0
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": instructions},
                {"role": "user", "content": user_msg},
            ],
            "format": schema,
            "stream": False,
            "keep_alive": self._ollama_keep_alive,
            "options": options,
        }
        governor = self._governor_for(self.base_url)
        if not governor.acquire():
            raise RuntimeError("structured output request was cancelled")
        try:
            response = httpx.post(
                f"{self.base_url}/api/chat",
                json=payload,
                timeout=httpx.Timeout(
                    self._ollama_http_timeout,
                    connect=10.0,
                    read=self._ollama_http_read_timeout,
                ),
            )
            if response.status_code in _OLLAMA_RATELIMIT_STATUS:
                governor.record_rate_limited()
            response.raise_for_status()
            raw = (response.json().get("message") or {}).get("content", "")
            parsed = json.loads(raw)
            expected = schema.get("type")
            if expected == "object" and not isinstance(parsed, dict):
                raise ValueError("structured response root was not an object")
            if expected == "array" and not isinstance(parsed, list):
                raise ValueError("structured response root was not an array")
            governor.record_success()
            self._circuit_record_success()
            return parsed
        finally:
            governor.release()

    def list_models(self) -> list:
        """Return list of available model names, including cloud models."""
        local: list[str] = []
        try:
            resp = httpx.get(f"{self.base_url}/api/tags", timeout=5)
            resp.raise_for_status()
            local = [m["name"] for m in resp.json().get("models", [])
                     if not any(kw in m["name"].lower() for kw in ("embed", "bert", "bge", "nomic"))]
        except Exception:
            pass
        local_set = {m.lower() for m in local}
        for cloud in self.CLOUD_MODELS:
            if cloud.lower() not in local_set:
                local.append(cloud)
        return local

    def classify(self, prompt: str, max_tokens: int = 50) -> str:
        """Quick non-streaming LLM call for classification. Returns plain text."""
        opts = dict(self._ollama_options)
        # v0.6.5 — go through the concurrency governor like stream() so a
        # burst of classification calls doesn't bypass the cap.
        gov = OllamaBackend._governor_for(self.base_url)
        gov.acquire()
        try:
            resp = httpx.post(
                f"{self.base_url}/api/chat",
                json={
                    "model": self.model,
                    "messages": [{"role": "user", "content": prompt}],
                    "stream": False,
                    "think": False,
                    "keep_alive": self._ollama_keep_alive,
                    "options": opts,
                },
                timeout=30,
            )
        finally:
            gov.release()
        resp.raise_for_status()
        return resp.json().get("message", {}).get("content", "").strip()

    @contextmanager
    def _open_chat_stream_with_retry(
        self,
        payload: dict,
        stream_timeout,
        cancel_event,
        notify_retry=None,
    ):
        """Context manager that opens a streaming POST to /api/chat,
        retrying on transient 5xx responses (especially Ollama
        Cloud's 503 "Server overloaded") with exponential backoff.

        Yields `(client, response)` on success. Yields `(None, None)`
        if `cancel_event` fires during a backoff sleep — the caller
        should bail cleanly.

        On non-retryable status (or after retries exhausted), still
        yields the `(client, response)` pair so the caller's existing
        rich-error path can read the body and raise its descriptive
        HTTPStatusError.

        v0.5.6a1: `notify_retry` is an optional callback invoked just
        before each backoff sleep. Receives a dict describing the retry
        — the caller (typically `stream()`) buffers these so the engine
        layer can surface them as `EVENT_BACKEND_STATUS` events. Without
        this, retries are silent: the GUI's "thinking N s" counter just
        keeps climbing during an upstream 503 storm with no signal that
        the backend is alive and retrying.

        Cleanup of both the client and the underlying stream is
        handled by this context manager — the caller doesn't need
        to wrap in additional `with` blocks.
        """
        attempt = 0
        while True:
            client = httpx.Client(timeout=stream_timeout)
            # v0.6.4 (F6) — once we've yielded the response to the
            # caller, a timeout is the caller's stream-consumption
            # timing out and gets thrown back in at the `yield`. We
            # are committed at that point — must NOT retry. The flag
            # distinguishes an open-phase timeout (retryable) from a
            # mid-stream one (terminal).
            opened_and_yielded = False
            try:
                with client.stream(
                    "POST", f"{self.base_url}/api/chat", json=payload,
                ) as resp:
                    if (resp.status_code in _OLLAMA_RETRYABLE_STATUS
                            and attempt < _OLLAMA_MAX_RETRIES):
                        try:
                            body_preview = resp.read().decode(
                                "utf-8", errors="replace",
                            ).strip()[:200]
                        except Exception:
                            body_preview = "<unreadable>"
                        backoff = (
                            _OLLAMA_BASE_BACKOFF * (2 ** attempt)
                        )
                        logger.warning(
                            "Ollama %d on /api/chat (attempt %d/%d, "
                            "model=%s) — retrying in %.1fs. body=%s",
                            resp.status_code, attempt + 1,
                            _OLLAMA_MAX_RETRIES + 1, self.model,
                            backoff, body_preview,
                        )
                        # v0.5.6a1 — surface the retry to the engine
                        # layer so it can emit a status event the GUI
                        # renders inline. Defensive: a callback raise
                        # must not break the retry path.
                        if notify_retry is not None:
                            try:
                                notify_retry({
                                    "kind": "ollama_retry",
                                    "status_code": resp.status_code,
                                    "attempt": attempt + 1,
                                    "max": _OLLAMA_MAX_RETRIES + 1,
                                    "model": self.model,
                                    "backoff_seconds": backoff,
                                    "body_preview": body_preview,
                                })
                            except Exception:
                                logger.debug(
                                    "notify_retry callback raised; ignoring",
                                    exc_info=True,
                                )
                        # Fall through to retry; the response and client close
                        # when their context managers exit.
                    else:
                        # Success path (2xx) OR final attempt with a
                        # non-2xx — hand to caller. cleanup handled
                        # by both `with`s exiting after caller's
                        # `with ...:` block ends.
                        opened_and_yielded = True
                        yield client, resp
                        return
            except httpx.TimeoutException as exc:
                # v0.6.4 (F6) — a connect/read timeout during the
                # OPEN phase is as transient as a 503: the v0.6.3
                # field run found a slow Ollama Cloud cold-call hit
                # the read ceiling. Retry it on the same backoff
                # curve. A timeout AFTER we yielded is the caller's
                # mid-stream consumption — we're committed; re-raise.
                if opened_and_yielded or attempt >= _OLLAMA_MAX_RETRIES:
                    raise
                backoff = _OLLAMA_BASE_BACKOFF * (2 ** attempt)
                logger.warning(
                    "Ollama %s on /api/chat (attempt %d/%d, model=%s) "
                    "— retrying in %.1fs.",
                    type(exc).__name__, attempt + 1,
                    _OLLAMA_MAX_RETRIES + 1, self.model, backoff,
                )
                if notify_retry is not None:
                    try:
                        notify_retry({
                            "kind": "ollama_timeout",
                            "attempt": attempt + 1,
                            "max": _OLLAMA_MAX_RETRIES + 1,
                            "model": self.model,
                            "backoff_seconds": backoff,
                        })
                    except Exception:
                        logger.debug(
                            "notify_retry callback raised; ignoring",
                            exc_info=True,
                        )
                # Fall through to the backoff sleep + retry.
            finally:
                client.close()

            # If we reach here, retry is required. The previous
            # `with` blocks have exited cleanly — we just need to
            # back off and start a fresh client+stream.
            if _wait_with_cancel(backoff, cancel_event):
                yield None, None
                return
            attempt += 1

    # ── Circuit breaker (v0.6.5 long-running hardening) ──────────────
    #
    # Tracks consecutive terminal failures per endpoint. After
    # `_OLLAMA_CIRCUIT_THRESHOLD` in a row the breaker opens for
    # `_OLLAMA_CIRCUIT_COOLDOWN` seconds; while open, `stream()` fails
    # fast instead of burning the retry/backoff budget (and cold-start
    # time) on an endpoint that's clearly down. A single success closes
    # it, and it half-opens automatically after the cooldown.

    def _circuit_open(self) -> bool:
        """True iff the breaker for this endpoint is currently open."""
        if _OLLAMA_CIRCUIT_THRESHOLD <= 0:
            return False
        c = OllamaBackend._circuit.get(self.base_url)
        return bool(c) and c.get("open_until", 0.0) > time.time()

    def _circuit_record_failure(self) -> bool:
        """Record a terminal failure; return True if it just tripped open."""
        if _OLLAMA_CIRCUIT_THRESHOLD <= 0:
            return False
        c = OllamaBackend._circuit.setdefault(
            self.base_url, {"failures": 0, "open_until": 0.0})
        c["failures"] += 1
        if c["failures"] >= _OLLAMA_CIRCUIT_THRESHOLD:
            c["open_until"] = time.time() + _OLLAMA_CIRCUIT_COOLDOWN
            return True
        return False

    def _circuit_record_success(self) -> None:
        """A success closes the breaker for this endpoint."""
        OllamaBackend._circuit.pop(self.base_url, None)

    @classmethod
    def _governor_for(cls, base_url: str) -> "_RequestGovernor":
        """The shared adaptive concurrency governor for an endpoint
        (lazily created). All requests to the same base_url share one."""
        with cls._governors_lock:
            gov = cls._governors.get(base_url)
            if gov is None:
                gov = _RequestGovernor(_OLLAMA_CONCURRENCY, _OLLAMA_MAX_CONCURRENCY)
                cls._governors[base_url] = gov
            return gov

    def stream(
        self,
        user_msg: str,
        conversation_history: list,
        instructions: str,
        tools: list,
        max_tokens: int | None = None,
        cancel_event=None,
    ) -> Iterator[Tuple[str, dict]]:
        """
        Stream a chat completion from Ollama with adaptive tool calling.

        Detects whether the model supports native tool calling:
        - Native mode: tools passed via Ollama API, model returns structured tool_calls
        - Text mode: tool definitions injected into system prompt, model returns
          <tool_call> XML blocks which are parsed from the response

        Both modes fall through to JSON/XML/text detection as a final safety net.
        """
        # v0.6.5 — circuit breaker: if this endpoint just failed repeatedly,
        # fail fast rather than burning the retry budget + cold-start time on
        # a backend that's almost certainly down. The distinct status lets the
        # daemon/GUI tell "backend down" apart from a one-off task failure;
        # the breaker half-opens after the cooldown.
        if self._circuit_open():
            yield (EVENT_BACKEND_STATUS, {
                "kind": "ollama_circuit_open",
                "model": self.model,
                "base_url": self.base_url,
            })
            yield (EVENT_ERROR, {
                "message": (
                    "Ollama endpoint circuit-open after repeated failures; "
                    "failing fast (will retry after a short cooldown)."
                ),
            })
            return

        # Detect tool support on first use
        if self._use_native_tools is None and tools:
            self._use_native_tools = self._detect_tool_support()

        use_native = self._use_native_tools if self._use_native_tools is not None else True
        # Resolve vision support once per stream call (cached after first hit). When
        # the model can't see images, we silently drop them from outgoing messages so
        # one screenshot doesn't 400 the whole conversation.
        allow_images = self.supports_vision()

        # Build messages array
        messages = []

        # System message — inject tool definitions for text-based mode
        sys_content = instructions
        if not use_native and tools:
            sys_content += build_tool_system_prompt(tools)

        messages.append({"role": "system", "content": sys_content})

        # Conversation history — format depends on tool mode
        for turn in conversation_history:
            role = turn["role"]
            content = turn["content"]
            if role == "tool_call":
                if use_native:
                    # Native mode: structured tool call
                    args = turn.get("arguments", "{}")
                    if isinstance(args, str):
                        try:
                            args = json.loads(args)
                        except json.JSONDecodeError:
                            args = {}
                    assistant_message = {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [{
                            "function": {
                                "name": turn.get("name", ""),
                                "arguments": args,
                            }
                        }],
                    }
                    reasoning = turn.get("reasoning_content") or turn.get("thinking")
                    if reasoning:
                        assistant_message["thinking"] = reasoning
                    messages.append(assistant_message)
                else:
                    # Text mode: reconstruct XML tool call in assistant message
                    args = turn.get("arguments", "{}")
                    if isinstance(args, dict):
                        args = json.dumps(args)
                    messages.append({
                        "role": "assistant",
                        "content": f'<tool_call>\n{{"name": "{turn.get("name", "")}", "arguments": {args}}}\n</tool_call>',
                    })
            elif role == "tool_result":
                if use_native:
                    # Check for screenshot image in tool result (computer use loop).
                    # Drop images entirely for non-vision models — Ollama 400s the
                    # request and the image stays in history poisoning every retry.
                    image_payload = turn.get("image")
                    img_b64 = _safe_image_b64(image_payload) if allow_images else ""
                    if img_b64:
                        # Ollama: include screenshot as user message with image
                        # since tool role doesn't support images in Ollama
                        messages.append({
                            "role": "tool",
                            "content": content,
                        })
                        messages.append({
                            "role": "user",
                            "content": "[Screenshot from tool result]",
                            "images": [img_b64],
                        })
                    else:
                        fallback = text_fallback({
                            "type": "image",
                            "media_type": (image_payload or {}).get("media_type", "image/png")
                            if isinstance(image_payload, dict) else "image/png",
                            "name": f"{turn.get('name', 'tool')} screenshot",
                        }) if image_payload else ""
                        messages.append({
                            "role": "tool",
                            "content": f"{content}\n\n{fallback}".strip(),
                        })
                else:
                    # Text mode: tool results become user messages
                    tool_name = turn.get("name", "tool")
                    image_payload = turn.get("image")
                    img_b64 = _safe_image_b64(image_payload) if allow_images else ""
                    if img_b64:
                        messages.append({
                            "role": "user",
                            "content": f"[{tool_name} result]\n{content}",
                            "images": [img_b64],
                        })
                    else:
                        fallback = text_fallback({
                            "type": "image",
                            "media_type": (image_payload or {}).get("media_type", "image/png")
                            if isinstance(image_payload, dict) else "image/png",
                            "name": f"{tool_name} screenshot",
                        }) if image_payload else ""
                        messages.append({
                            "role": "user",
                            "content": f"[{tool_name} result]\n{content}\n\n{fallback}".strip(),
                        })
            elif role in ("user", "assistant"):
                # Handle multimodal content (images + text)
                if isinstance(content, list):
                    text_value, raw_images = ollama_message_content(content, allow_images=allow_images)
                    images = [_clean_image_b64(value) for value in raw_images]
                    images = [value for value in images if value]
                    msg = {"role": role, "content": text_value}
                    if images:
                        msg["images"] = images
                    messages.append(msg)
                else:
                    messages.append({"role": role, "content": content})

        # Session.run records the current user turn before calling the backend.
        # Avoid duplicating it on the first inference step.
        history_has_current_user = bool(
            conversation_history
            and conversation_history[-1].get("role") == "user"
            and _message_text(conversation_history[-1].get("content")) == str(user_msg or "")
        )
        if not history_has_current_user:
            messages.append({"role": "user", "content": user_msg})

        # Use fixed options — NEVER change num_ctx or other params between requests,
        # or Ollama will reload the entire model from disk (30-120s penalty)
        opts = dict(self._ollama_options)

        payload = {
            "model": self.model,
            "messages": messages,
            "stream": True,
            "keep_alive": self._ollama_keep_alive,
            "options": opts,
        }
        self._with_thinking(payload)

        # Pass tools via Ollama API only in native mode
        # In text mode, tools are already in the system prompt
        if tools and use_native:
            payload["tools"] = _convert_tools_for_ollama(tools)

        collected_tokens = []      # Text tokens (buffered initially)
        collected_thinking = []    # Replayed on assistant tool-call messages
        native_tool_calls = []     # Tool calls from Ollama's native API
        streaming = False          # True once we've started streaming text to TUI
        has_tools = bool(tools)    # Whether tools are available (determines buffering)
        text_mode = has_tools and not use_native  # Text-based tool calling active

        # Buffering strategy:
        # Native mode:
        #   - If response starts with '{' or '<tool' → always buffer (tool call JSON/XML)
        #   - If response is short text starting with a tool name → buffer briefly
        #   - Long text → stream immediately
        # Text mode:
        #   - Always buffer until we can determine if <tool_call> is present
        #   - Stream text outside of tool_call blocks at end
        force_buffer = False   # Always buffer (JSON/XML tool call detected)
        known_tool_names = {"bash", "file_write", "file_read", "file_edit", "glob", "grep"}

        # v0.6.5 — reserve a governor slot before opening the request so total
        # in-flight requests across all sessions/missions/sub-agents stay under
        # the adaptive cap (queue here otherwise). Released in the finally
        # below. Cancel-aware: bail cleanly if cancelled while queued.
        _gov = OllamaBackend._governor_for(self.base_url)
        if not _gov.acquire(cancel_event):
            return

        try:
            stream_timeout = httpx.Timeout(
                self._ollama_http_timeout,
                connect=10.0,
                read=self._ollama_http_read_timeout,
            )
            # v0.5.0a9 — retry transient 5xx (especially Ollama
            # Cloud's 503 "Server overloaded, please retry shortly
            # (ref: ...)") with exponential backoff. The retry only
            # fires BEFORE we start consuming chunks (status-check
            # phase) — once streaming begins we're committed.
            # v0.5.6a1 — buffer retry events so we can yield them
            # as EVENT_BACKEND_STATUS before the response chunks.
            # Without this the GUI's "thinking N s" counter has no
            # signal that an upstream 503 storm is being silently
            # weathered — users assumed the daemon hung.
            _pending_status: list[Tuple[str, dict]] = []
            def _on_retry(p):
                _pending_status.append((EVENT_BACKEND_STATUS, p))
                # A 403/429 retry means the cloud is rate-limiting us → shrink
                # the governor's concurrency cap (AIMD) so we back off the
                # whole fan-out, not just this one call.
                if p.get("status_code") in _OLLAMA_RATELIMIT_STATUS:
                    _gov.record_rate_limited()
            with self._open_chat_stream_with_retry(
                payload, stream_timeout, cancel_event,
                notify_retry=_on_retry,
            ) as (client, resp):
                # Drain any retry events accumulated during open
                # before we touch the stream itself.
                while _pending_status:
                    yield _pending_status.pop(0)
                if client is None:
                    # cancel_event fired during a backoff sleep
                    return
                if resp.status_code >= 400:
                    # Read the error body before raising so the user/log can see
                    # the actual reason (e.g. "illegal base64 data at input byte N").
                    try:
                        err_body = resp.read().decode("utf-8", errors="replace").strip()
                    except Exception:
                        err_body = "<unreadable error body>"
                    # v0.6.4 (F2) — if we land here with a RETRYABLE status,
                    # it means the retry helper exhausted its budget (it only
                    # yields a non-2xx response after the last attempt). The
                    # per-retry `ollama_retry` events faded from the GUI; emit
                    # a distinct terminal `ollama_exhausted` status so the
                    # frontend can render a PERSISTENT chip instead of leaving
                    # the user staring at a silent, stalled chat.
                    if resp.status_code in _OLLAMA_RETRYABLE_STATUS:
                        yield (EVENT_BACKEND_STATUS, {
                            "kind": "ollama_exhausted",
                            "status_code": resp.status_code,
                            "model": self.model,
                            "attempts": _OLLAMA_MAX_RETRIES + 1,
                            "body_preview": err_body[:200],
                        })
                        # v0.6.5 — a transient-5xx exhaustion counts toward
                        # the circuit breaker (endpoint looks overloaded/down).
                        self._circuit_record_failure()
                    # Log a redacted summary of the offending payload to help
                    # diagnose recurrences without dumping screenshots into logs.
                    try:
                        redacted_msgs = []
                        for m in payload.get("messages", []):
                            rm = {"role": m.get("role"), "content_len": len(str(m.get("content", "")))}
                            if m.get("images"):
                                rm["images"] = [
                                    {"len": len(img) if isinstance(img, str) else 0,
                                     "head": (img[:24] + "...") if isinstance(img, str) and len(img) > 24 else (img or "<empty>")}
                                    for img in m["images"]
                                ]
                            if m.get("tool_calls"):
                                rm["tool_calls"] = len(m["tool_calls"])
                            redacted_msgs.append(rm)
                        logger.error(
                            "Ollama %d on /api/chat (model=%s): %s | messages=%s | options=%s",
                            resp.status_code,
                            self.model,
                            err_body[:500],
                            redacted_msgs,
                            {k: v for k, v in payload.get("options", {}).items()
                             if k in ("num_ctx", "num_batch", "num_gpu", "think")},
                        )
                    except Exception:
                        logger.exception("failed to log Ollama %d diagnostics", resp.status_code)
                    # Re-raise with the actual body for the caller's UI to surface
                    raise httpx.HTTPStatusError(
                        f"Ollama {resp.status_code}: {err_body[:400]}",
                        request=resp.request,
                        response=resp,
                    )
                buf = b""
                for chunk in resp.iter_raw():
                    if cancel_event is not None and cancel_event.is_set():
                        return
                    buf += chunk
                    while b"\n" in buf:
                        line_bytes, buf = buf.split(b"\n", 1)
                        line = line_bytes.decode("utf-8", errors="replace").strip()
                        if not line:
                            continue
                        try:
                            data = json.loads(line)
                        except json.JSONDecodeError:
                            continue

                        if data.get("done"):
                            # --- End of response ---
                            full_text = "".join(collected_tokens)
                            # Strip model control tokens
                            clean_text = re.sub(r'<\|im_\w+\|>', '', full_text).strip()

                            # Detect tool calls from ALL sources
                            detected_calls = list(native_tool_calls)

                            plain_text = ""  # Text outside tool_call blocks
                            if not detected_calls:
                                plain_text, dsml_calls = parse_dsml_tool_calls(clean_text)
                                for tc in dsml_calls:
                                    detected_calls.append({
                                        "name": tc["name"],
                                        "arguments": tc["arguments"],
                                        "call_id": _new_call_id(
                                            tc["name"], tc["arguments"], len(detected_calls)
                                        ),
                                    })

                            if not detected_calls:
                                # Try XML tags — returns (plain_text, tool_calls)
                                plain_text, xml_calls = parse_tool_calls(clean_text)
                                for tc in xml_calls:
                                    call_id = _new_call_id(
                                        tc["name"], tc["arguments"], len(detected_calls)
                                    )
                                    detected_calls.append({
                                        "name": tc["name"],
                                        "arguments": tc["arguments"],
                                        "call_id": call_id,
                                    })

                            if not detected_calls:
                                # Try raw JSON (in buffered OR streamed text)
                                detected_calls = _detect_json_tool_calls(clean_text)

                            if not detected_calls:
                                # Try text format: bash(cmd) or bash cmd
                                detected_calls = _detect_text_tool_calls(clean_text)

                            if detected_calls:
                                # In text mode, emit any plain text before the tool calls
                                # (model may explain what it's doing before the <tool_call>)
                                if text_mode and plain_text:
                                    yield (EVENT_TEXT_DELTA, {"delta": plain_text})
                                # Yield tool calls
                                for tc in detected_calls:
                                    yield (EVENT_TOOL_CALL, tc)
                            elif not streaming and collected_tokens:
                                # Was buffering text, no tool calls found — flush as text
                                for t in collected_tokens:
                                    yield (EVENT_TEXT_DELTA, {"delta": t})

                            # Stats
                            stats = {}
                            for key in ("total_duration", "eval_count", "eval_duration",
                                         "prompt_eval_count", "prompt_eval_duration",
                                         "done_reason"):
                                if key in data:
                                    stats[key] = data[key]

                            # v0.6.5 — a completed stream closes the circuit
                            # breaker AND rewards the governor (AIMD additive
                            # increase toward the concurrency ceiling).
                            self._circuit_record_success()
                            _gov.record_success()
                            yield (EVENT_DONE, {
                                "cognitive_state": None,
                                "stats": stats,
                                "model": self.model,
                            })
                            return

                        # Check for native tool calls
                        msg = data.get("message", {})
                        reasoning_token = msg.get("thinking") or msg.get("reasoning_content")
                        if reasoning_token:
                            collected_thinking.append(str(reasoning_token))
                        native_tc = msg.get("tool_calls", [])
                        if native_tc:
                            for tc in native_tc:
                                fn = tc.get("function", {})
                                name = fn.get("name", "")
                                args = fn.get("arguments", {})
                                args_str = json.dumps(args) if isinstance(args, dict) else str(args)
                                call_id = _new_call_id(name, args_str, len(native_tool_calls))
                                native_tool_calls.append({
                                    "name": name,
                                    "arguments": args_str,
                                    "call_id": call_id,
                                    "reasoning_content": "".join(collected_thinking),
                                })
                            continue

                        # Streaming text token
                        token = msg.get("content", "")
                        if token:
                            collected_tokens.append(token)

                            if text_mode:
                                # Text-based tool calling: always buffer the full
                                # response since <tool_call> blocks can appear
                                # anywhere (after explanatory text, at the end, etc.)
                                # We parse everything at done time.
                                continue
                            elif streaming:
                                # Already streaming — send immediately
                                yield (EVENT_TEXT_DELTA, {"delta": token})
                            elif not has_tools:
                                # No tools available — stream immediately
                                streaming = True
                                yield (EVENT_TEXT_DELTA, {"delta": token})
                            else:
                                # Native tool mode — check first non-whitespace char
                                acc = "".join(collected_tokens).lstrip()
                                if not acc:
                                    continue  # Only whitespace so far
                                if not force_buffer:
                                    if acc[0] == '{' or acc.startswith('<tool'):
                                        force_buffer = True
                                    elif any(acc.startswith(t + " ") or acc.startswith(t + "(") or acc.startswith(t + "\n") for t in known_tool_names):
                                        # Might be "bash ls" or "bash(ls)" — buffer if short
                                        total_chars = sum(len(t) for t in collected_tokens)
                                        if total_chars < 200:
                                            continue  # Keep buffering, check at done
                                if force_buffer:
                                    continue  # Keep buffering until done
                                # Doesn't look like a tool call → stream
                                streaming = True
                                for t in collected_tokens:
                                    yield (EVENT_TEXT_DELTA, {"delta": t})

        except httpx.TimeoutException as e:
            # v0.6.4 (F6) — the open-phase timeout retries exhausted.
            # `_open_chat_stream_with_retry` raised (rather than
            # yielding), so the per-retry `ollama_timeout` events it
            # buffered into `_pending_status` were never drained by
            # the `with` body. Flush them now so the GUI still sees
            # the retry sequence, THEN emit a terminal
            # `ollama_exhausted` status (timeout flavor, status_code 0)
            # so the GUI renders the persistent chip — same surface as
            # the 503-exhausted path. Then fall through to the error.
            try:
                while _pending_status:
                    yield _pending_status.pop(0)
            except NameError:
                # Timeout fired before _pending_status was bound —
                # impossible in practice, but cheap to guard.
                pass
            yield (EVENT_BACKEND_STATUS, {
                "kind": "ollama_exhausted",
                "status_code": 0,
                "reason": "timeout",
                "model": self.model,
                "attempts": _OLLAMA_MAX_RETRIES + 1,
                "body_preview": type(e).__name__,
            })
            # v0.6.5 — repeated open-phase timeouts count toward the breaker.
            self._circuit_record_failure()
            yield (EVENT_ERROR, {"message": str(e) or type(e).__name__})
        except Exception as e:
            yield (EVENT_ERROR, {"message": str(e)})
        finally:
            # Always free the governor slot — on completion, error, timeout,
            # or the generator being closed early by the consumer.
            _gov.release()



# ---------------------------------------------------------------------------
# Codex CLI backend
# ---------------------------------------------------------------------------


_CODEX_DEFAULT_MODELS = [
    "gpt-5.5",
    "gpt-5.4-mini",
    "gpt-5.3-codex-spark",
]

_CODEX_MODEL_LABELS = {
    "gpt-5.5": "gpt-5.5",
    "gpt-5.4-mini": "gpt-5.4-mini",
    "gpt-5.3-codex-spark": "gpt-5.3-codex-spark",
}


def _split_model_list(raw: str) -> list[str]:
    result: list[str] = []
    for item in re.split(r"[,;\n]", raw or ""):
        value = item.strip()
        if value and value not in result:
            result.append(value)
    return result


def _load_codex_config() -> dict:
    try:
        import tomllib

        path = Path.home() / ".codex" / "config.toml"
        if not path.exists():
            return {}
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _codex_configured_model(config: dict | None = None) -> str:
    data = config if config is not None else _load_codex_config()
    try:
        return str(data.get("model", "") or "").strip()
    except Exception:
        return ""


def codex_cli_models(config: dict | None = None) -> list[str]:
    """Return the Codex model list Resonant should expose.

    Codex CLI does not currently expose a stable "list models" command. Keep a
    small official-docs-backed default list, prepend the user's configured
    Codex model if present, and allow early/new rollouts via
    RESONANT_CODEX_MODELS.
    """
    configured = _codex_configured_model(config)
    env_models = _split_model_list(os.environ.get("RESONANT_CODEX_MODELS", ""))
    candidates = env_models or list(_CODEX_DEFAULT_MODELS)
    if configured and configured not in candidates:
        candidates.insert(0, configured)
    return candidates


def codex_cli_model_labels() -> dict[str, str]:
    labels = dict(_CODEX_MODEL_LABELS)
    for model in codex_cli_models():
        labels.setdefault(model, model)
    return labels


def _codex_configured_cli_path(config: dict | None = None) -> str:
    data = config if config is not None else _load_codex_config()
    try:
        servers = data.get("mcp_servers", {}) or {}
        node_repl = servers.get("node_repl", {}) or {}
        env = node_repl.get("env", {}) or {}
        return str(env.get("CODEX_CLI_PATH", "") or "").strip()
    except Exception:
        return ""


def resolve_codex_cli_path() -> str:
    """Resolve the best Codex CLI executable for subscription-backed runs."""
    config = _load_codex_config()
    candidates = [
        os.environ.get("RESONANT_CODEX_CLI", "").strip(),
        os.environ.get("CODEX_CLI_PATH", "").strip(),
        _codex_configured_cli_path(config),
        shutil.which("codex") or "",
    ]
    seen: set[str] = set()
    for raw in candidates:
        if not raw:
            continue
        expanded = os.path.expandvars(os.path.expanduser(raw))
        key = os.path.normcase(os.path.normpath(expanded))
        if key in seen:
            continue
        seen.add(key)
        if os.path.isfile(expanded):
            return expanded
        if shutil.which(expanded):
            return expanded
    return ""


def _codex_context_blocks(instructions: str) -> str:
    blocks: list[str] = []
    patterns = [
        r"--- PROJECT INSTRUCTIONS.*?--- END PROJECT INSTRUCTIONS ---",
        r"--- RECALLED MEMORIES ---.*?--- END MEMORIES ---",
        r"--- RELEVANT FILES ---.*?--- END RELEVANT FILES ---",
    ]
    for pattern in patterns:
        match = re.search(pattern, instructions or "", flags=re.DOTALL)
        if match:
            blocks.append(match.group(0).strip())
    return "\n\n".join(blocks)


def _shorten_for_codex_prompt(value, limit: int) -> str:
    if isinstance(value, list):
        text_parts: list[str] = []
        for part in value:
            if isinstance(part, dict):
                if part.get("type") == "text":
                    text_parts.append(str(part.get("text", "")))
                elif part.get("type") == "image":
                    text_parts.append("[image attachment omitted from Codex CLI handoff]")
            else:
                text_parts.append(str(part))
        text = "\n".join(p for p in text_parts if p)
    else:
        text = str(value or "")
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 80)] + "\n...[truncated for Codex CLI handoff]..."


def _format_codex_history(history: list) -> str:
    try:
        max_turns = int(os.environ.get("RESONANT_CODEX_HISTORY_TURNS", "20") or "20")
    except ValueError:
        max_turns = 20
    try:
        per_item_limit = int(os.environ.get("RESONANT_CODEX_HISTORY_ITEM_CHARS", "4000") or "4000")
    except ValueError:
        per_item_limit = 4000
    selected = list(history or [])[-max(0, max_turns):]
    lines: list[str] = []
    for turn in selected:
        role = str(turn.get("role", "") or "unknown")
        if role == "tool_call":
            name = str(turn.get("name", "") or "tool")
            content = f"called {name} with {turn.get('arguments', {})}"
        elif role == "tool_result":
            name = str(turn.get("name", "") or "tool")
            content = f"{name} result: {turn.get('content', '')}"
        else:
            content = turn.get("content", "")
        text = _shorten_for_codex_prompt(content, per_item_limit).strip()
        if text:
            lines.append(f"{role.upper()}:\n{text}")
    return "\n\n".join(lines)


def _build_codex_prompt(
    *,
    user_msg: str,
    conversation_history: list,
    instructions: str,
    cwd: str,
) -> str:
    is_plan = "You are in PLAN MODE" in (instructions or "")
    context = _codex_context_blocks(instructions or "")
    history_items = list(conversation_history or [])
    if (
        history_items
        and history_items[-1].get("role") == "user"
        and _message_text(history_items[-1].get("content")) == str(user_msg or "")
    ):
        history_items = history_items[:-1]
    history = _format_codex_history(history_items)

    parts = [
        "You are being invoked by Resonant through Codex CLI.",
        "Use Codex's native tools and normal CLI behavior. Do not emit Resonant <tool_call> XML.",
        f"Working directory: {cwd}",
    ]
    if is_plan:
        parts.append("This is a planning turn. Return a concise plan and do not modify files.")
    if context:
        parts.append(context)
    if history:
        parts.append("--- CONVERSATION HISTORY ---\n" + history + "\n--- END CONVERSATION HISTORY ---")
    parts.append("--- CURRENT USER REQUEST ---\n" + str(user_msg or "").strip())
    return "\n\n".join(parts).strip() + "\n"


_CODEX_PERMISSION_PROFILES = {
    # ``codex exec`` cannot relay an interactive approval request back through
    # Resonant yet. Keep Ask and Plan genuinely non-mutating instead of letting
    # a subprocess silently approve its own changes.
    "ask": ("read-only", "never"),
    "plan": ("read-only", "never"),
    # Auto-edit lets Codex use its patching tools while untrusted shell actions
    # are refused by the non-interactive CLI.
    "auto-edit": ("workspace-write", "untrusted"),
    # Resonant's Full-auto remains sandboxed to the selected project.
    "bypass": ("workspace-write", "never"),
}


class KimiBackend:
    """Kimi K3 through Moonshot's OpenAI-compatible streaming API."""

    DEFAULT_BASE_URL = "https://api.moonshot.ai/v1"
    DEFAULT_MODEL = "kimi-k3"
    MODELS = (DEFAULT_MODEL,)
    RETRYABLE_STATUS = {408, 409, 429, 500, 502, 503, 504}
    _QUOTA_ERROR_TYPES = {
        "billing_not_active",
        "exceeded_current_quota_error",
        "insufficient_quota",
        "insufficient_quota_error",
    }
    _QUOTA_MESSAGE_MARKERS = (
        "billing",
        "insufficient balance",
        "insufficient credit",
        "quota",
        "recharge",
        "suspended",
    )

    def __init__(
        self,
        api_key: str,
        model: str = DEFAULT_MODEL,
        *,
        base_url: str = DEFAULT_BASE_URL,
        transport=None,
    ):
        if not str(api_key or "").strip():
            raise ValueError(
                "Kimi API key required. Add it in Settings -> Kimi API or set "
                "MOONSHOT_API_KEY."
            )
        self.api_key = str(api_key).strip()
        self.model = str(model or self.DEFAULT_MODEL).strip()
        self.base_url = str(base_url or self.DEFAULT_BASE_URL).rstrip("/")
        self.name = "kimi"
        self.handles_tools = False
        self.thinking_mode = "max"
        self._transport = transport
        self._capabilities = ModelCapabilities(
            model=self.model,
            context_window=1_048_576,
            modalities=("text", "image"),
            native_tools=True,
            parallel_tools=True,
            structured_output=None,
            reasoning_levels=("max",),
            prompt_caching=True,
            native_continuation=True,
            max_safe_concurrency=4,
            source="provider",
        )
        self._timeout = httpx.Timeout(
            connect=float(os.environ.get("RESONANT_KIMI_CONNECT_TIMEOUT_SEC", "15")),
            read=float(os.environ.get("RESONANT_KIMI_READ_TIMEOUT_SEC", "600")),
            write=60.0,
            pool=60.0,
        )

    @property
    def effective_context_tokens(self) -> int:
        return 1_048_576

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
    ) -> list[str]:
        """Return Kimi chat models, falling back to the documented K3 ID."""
        if not str(api_key or "").strip():
            return []
        try:
            response = httpx.get(
                f"{str(base_url or cls.DEFAULT_BASE_URL).rstrip('/')}/models",
                headers={"Authorization": f"Bearer {api_key}"},
                timeout=timeout,
            )
            response.raise_for_status()
            model_ids = [
                str(item.get("id") or "").strip()
                for item in response.json().get("data", [])
                if isinstance(item, dict)
            ]
            models = [model_id for model_id in model_ids if model_id.startswith("kimi-k3")]
            return models or list(cls.MODELS)
        except Exception:
            return list(cls.MODELS)

    @staticmethod
    def _api_content(content) -> str | list[dict]:
        parts: list[dict] = []
        for part in normalize_content(content):
            part_type = part.get("type")
            if part_type == "text":
                text = str(part.get("text") or "")
                if text:
                    parts.append({"type": "text", "text": text})
                continue
            if part_type == "image":
                data = _clean_image_b64(part.get("data", ""))
                if data:
                    media_type = str(part.get("media_type") or "image/png")
                    parts.append({
                        "type": "image_url",
                        "image_url": {"url": f"data:{media_type};base64,{data}"},
                    })
                    description = str(
                        part.get("description") or part.get("caption") or ""
                    ).strip()
                    if description:
                        parts.append({"type": "text", "text": f"Image description: {description}"})
                    continue
            parts.append({"type": "text", "text": text_fallback(part)})
        if not parts:
            return ""
        if all(part.get("type") == "text" for part in parts):
            return "\n\n".join(str(part.get("text") or "") for part in parts)
        return parts

    def _messages(self, conversation_history: list, instructions: str, user_msg: str) -> list[dict]:
        messages: list[dict] = [{"role": "system", "content": instructions}]
        emitted_responses: set[str] = set()

        for index, turn in enumerate(conversation_history):
            role = str(turn.get("role") or "")
            content = turn.get("content", "")
            if role == "assistant":
                next_turn = conversation_history[index + 1] if index + 1 < len(conversation_history) else {}
                if (
                    next_turn.get("role") == "tool_call"
                    and next_turn.get("assistant_content") == content
                ):
                    continue
                messages.append({"role": "assistant", "content": content})
            elif role == "user":
                messages.append({"role": "user", "content": self._api_content(content)})
            elif role == "tool_catalog":
                definitions = _convert_tools_for_ollama(turn.get("tools") or [])
                if definitions:
                    messages.append({"role": "system", "tools": definitions})
            elif role == "tool_call":
                response_id = str(turn.get("response_id") or "")
                if response_id:
                    if response_id in emitted_responses:
                        continue
                    emitted_responses.add(response_id)
                response_calls = turn.get("response_tool_calls")
                if not isinstance(response_calls, list) or not response_calls:
                    response_calls = [{
                        "id": turn.get("call_id") or _new_call_id(
                            str(turn.get("name") or ""),
                            str(turn.get("arguments") or "{}"),
                        ),
                        "type": "function",
                        "function": {
                            "name": turn.get("name", ""),
                            "arguments": turn.get("arguments", "{}"),
                        },
                    }]
                tool_calls = []
                for call in response_calls:
                    function = call.get("function") if isinstance(call, dict) else {}
                    function = function if isinstance(function, dict) else {}
                    arguments = function.get("arguments", "{}")
                    if not isinstance(arguments, str):
                        arguments = json.dumps(arguments, ensure_ascii=False)
                    tool_calls.append({
                        "id": str(call.get("id") or ""),
                        "type": "function",
                        "function": {
                            "name": str(function.get("name") or ""),
                            "arguments": arguments,
                        },
                    })
                assistant_message = {
                    "role": "assistant",
                    "content": turn.get("assistant_content", ""),
                    "tool_calls": tool_calls,
                }
                reasoning = turn.get("reasoning_content") or turn.get("thinking")
                if reasoning:
                    assistant_message["reasoning_content"] = reasoning
                messages.append(assistant_message)
            elif role == "tool_result":
                messages.append({
                    "role": "tool",
                    "tool_call_id": str(turn.get("call_id") or ""),
                    "content": str(content or ""),
                })
                image = turn.get("image")
                image_data = _safe_image_b64(image)
                if image_data:
                    media_type = str((image or {}).get("media_type") or "image/png")
                    messages.append({
                        "role": "user",
                        "content": [{
                            "type": "image_url",
                            "image_url": {"url": f"data:{media_type};base64,{image_data}"},
                        }],
                    })

        history_has_current_user = any(
            turn.get("role") == "user"
            and _message_text(turn.get("content", "")).strip() == _message_text(user_msg).strip()
            for turn in conversation_history[-3:]
        )
        if not history_has_current_user:
            messages.append({"role": "user", "content": self._api_content(user_msg)})
        return messages

    def _payload(
        self,
        user_msg: str,
        conversation_history: list,
        instructions: str,
        tools: list,
        max_tokens: int | None,
    ) -> dict:
        payload = {
            "model": self.model,
            "messages": self._messages(conversation_history, instructions, user_msg),
            "stream": True,
            "stream_options": {"include_usage": True},
            "reasoning_effort": "max",
        }
        if tools:
            payload["tools"] = _convert_tools_for_ollama(tools)
        if max_tokens:
            payload["max_completion_tokens"] = min(max(1, int(max_tokens)), 1_048_576)
        return payload

    @staticmethod
    def _error_details(response: httpx.Response) -> tuple[str, str]:
        try:
            body = response.read().decode("utf-8", errors="replace").strip()
            parsed = json.loads(body)
            error = parsed.get("error", {}) if isinstance(parsed, dict) else {}
            error = error if isinstance(error, dict) else {}
            error_type = str(error.get("type") or "").strip().lower()
            detail = str(error.get("message") or body or f"HTTP {response.status_code}")
            return error_type, detail[:500]
        except Exception:
            return "", f"HTTP {response.status_code}"

    @classmethod
    def _is_quota_error(cls, error_type: str, message: str) -> bool:
        normalized_type = str(error_type or "").strip().lower()
        normalized_message = str(message or "").strip().lower()
        return (
            normalized_type in cls._QUOTA_ERROR_TYPES
            or any(marker in normalized_message for marker in cls._QUOTA_MESSAGE_MARKERS)
        )

    @classmethod
    def _is_retryable_error(cls, status_code: int, error_type: str, message: str) -> bool:
        if status_code not in cls.RETRYABLE_STATUS:
            return False
        return status_code != 429 or not cls._is_quota_error(error_type, message)

    @classmethod
    def _user_error_message(
        cls,
        status_code: int,
        error_type: str,
        message: str,
    ) -> str:
        if status_code == 429 and cls._is_quota_error(error_type, message):
            return (
                "Kimi account billing is inactive or has insufficient balance. "
                "Recharge the account or review its plan and billing details, then retry."
            )
        if status_code in {401, 403}:
            return "Kimi rejected the API key. Check the key in Settings -> Kimi API."
        return f"Kimi API request failed ({status_code}): {message}"

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
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        }
        reasoning_parts: list[str] = []
        content_parts: list[str] = []
        tool_calls: dict[int, dict] = {}
        usage: dict = {}
        response_id = ""

        try:
            with httpx.Client(timeout=self._timeout, transport=self._transport) as client:
                for attempt in range(3):
                    if cancel_event is not None and cancel_event.is_set():
                        return
                    with client.stream(
                        "POST",
                        f"{self.base_url}/chat/completions",
                        headers=headers,
                        json=payload,
                    ) as response:
                        if response.status_code >= 400:
                            error_type, message = self._error_details(response)
                            retryable = self._is_retryable_error(
                                response.status_code, error_type, message
                            )
                            logger.warning(
                                "Kimi API request failed: status=%d type=%s retryable=%s model=%s",
                                response.status_code,
                                error_type or "unknown",
                                retryable,
                                self.model,
                            )
                            if retryable and attempt < 2:
                                delay = 1.5 * (2 ** attempt)
                                yield (EVENT_BACKEND_STATUS, {
                                    "kind": "kimi_retry",
                                    "status_code": response.status_code,
                                    "attempt": attempt + 1,
                                    "max": 3,
                                    "model": self.model,
                                    "backoff_seconds": delay,
                                    "body_preview": message,
                                })
                                if _wait_with_cancel(delay, cancel_event):
                                    return
                                continue
                            yield (EVENT_ERROR, {
                                "message": self._user_error_message(
                                    response.status_code, error_type, message
                                )
                            })
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
                            except json.JSONDecodeError:
                                continue
                            response_id = str(event.get("id") or response_id)
                            if isinstance(event.get("usage"), dict):
                                usage = event["usage"]
                            choices = event.get("choices") or []
                            if not choices:
                                continue
                            delta = choices[0].get("delta") or {}
                            reasoning = str(delta.get("reasoning_content") or "")
                            if reasoning:
                                reasoning_parts.append(reasoning)
                            text_delta = str(delta.get("content") or "")
                            if text_delta:
                                content_parts.append(text_delta)
                                yield (EVENT_TEXT_DELTA, {"delta": text_delta})
                            for fragment in delta.get("tool_calls") or []:
                                index = int(fragment.get("index", 0) or 0)
                                current = tool_calls.setdefault(index, {
                                    "id": "", "name": "", "arguments": "",
                                })
                                if fragment.get("id"):
                                    current["id"] = str(fragment["id"])
                                function = fragment.get("function") or {}
                                if function.get("name"):
                                    current["name"] += str(function["name"])
                                if function.get("arguments"):
                                    current["arguments"] += str(function["arguments"])
                        break

            complete_calls = []
            for index, call in sorted(tool_calls.items()):
                arguments = call["arguments"] or "{}"
                call_id = call["id"] or _new_call_id(call["name"], arguments, index)
                complete_calls.append({
                    "id": call_id,
                    "type": "function",
                    "function": {"name": call["name"], "arguments": arguments},
                })
            reasoning_content = "".join(reasoning_parts)
            assistant_content = "".join(content_parts)
            stable_response_id = response_id or _new_call_id(
                self.model, assistant_content + reasoning_content, 0
            )
            for call in complete_calls:
                yield (EVENT_TOOL_CALL, {
                    "name": call["function"]["name"],
                    "arguments": call["function"]["arguments"],
                    "call_id": call["id"],
                    "reasoning_content": reasoning_content,
                    "assistant_content": assistant_content,
                    "response_id": stable_response_id,
                    "response_tool_calls": complete_calls,
                })

            prompt_details = usage.get("prompt_tokens_details") or {}
            stats = {
                "input_tokens": int(usage.get("prompt_tokens") or 0),
                "output_tokens": int(usage.get("completion_tokens") or 0),
                "cached_tokens": int(prompt_details.get("cached_tokens") or 0),
            }
            yield (EVENT_DONE, {
                "model": self.model,
                "stats": stats,
                "cognitive_state": None,
            })
        except httpx.TimeoutException:
            yield (EVENT_ERROR, {"message": "Kimi API request timed out."})
        except httpx.HTTPError as exc:
            yield (EVENT_ERROR, {"message": f"Kimi API connection failed: {type(exc).__name__}"})
        except Exception as exc:
            logger.exception("Kimi stream failed")
            yield (EVENT_ERROR, {"message": f"Kimi stream failed: {type(exc).__name__}"})


class CodexCliBackend:
    """Subscription/API-auth backed Codex CLI execution."""

    def __init__(
        self,
        model: str,
        *,
        cwd: str | None = None,
        cli_path: str | None = None,
        sandbox: str | None = None,
        permission_mode: str | None = None,
    ):
        if not model:
            raise ValueError("Model name required for Codex backend")
        self.model = model
        self.name = "codex"
        self.handles_tools = True
        self.cwd = os.path.abspath(cwd or os.getcwd())
        self.cli_path = cli_path or resolve_codex_cli_path()
        if not self.cli_path:
            raise ValueError(
                "Codex CLI was not found. Install/sign in to Codex, or set "
                "RESONANT_CODEX_CLI to the codex executable."
            )
        self._sandbox_override = (sandbox or "").strip()
        if self._sandbox_override not in {"read-only", "workspace-write", "danger-full-access"}:
            self._sandbox_override = ""
        self.permission_mode = "bypass"
        self.sandbox = "workspace-write"
        self.approval_policy = "never"
        self.configure_permission_mode(permission_mode or "bypass")

    def configure_permission_mode(self, mode: str) -> None:
        """Apply Resonant's permission mode to the non-interactive CLI run."""
        normalized = mode if mode in _CODEX_PERMISSION_PROFILES else "bypass"
        sandbox, approval = _CODEX_PERMISSION_PROFILES[normalized]
        self.permission_mode = normalized
        self.sandbox = self._sandbox_override or sandbox
        self.approval_policy = approval

    @staticmethod
    def list_available_models() -> list[str]:
        return codex_cli_models()

    @classmethod
    def is_available(cls) -> bool:
        return bool(resolve_codex_cli_path())

    def list_models(self) -> list[str]:
        return self.list_available_models()

    def health(self) -> dict:
        try:
            proc = subprocess.run(
                [self.cli_path, "login", "status"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=10,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            status = "ready" if proc.returncode == 0 else "error"
            message = (proc.stdout or proc.stderr or "").strip()
            return {
                "status": status,
                "backend": "codex",
                "model": self.model,
                "available_models": self.list_models(),
                "message": message,
                "cli_path": self.cli_path,
            }
        except Exception as exc:
            return {"status": "error", "backend": "codex", "message": str(exc)}

    def classify(self, prompt: str, max_tokens: int = 50) -> str:
        events = list(self.stream(
            user_msg=prompt,
            conversation_history=[],
            instructions="Return a short answer.",
            tools=[],
            max_tokens=max_tokens,
        ))
        return "".join(data.get("delta", "") for event, data in events if event == EVENT_TEXT_DELTA).strip()

    def _command(self) -> list[str]:
        cmd = [
            self.cli_path,
            "exec",
            "--json",
            "-c",
            f'approval_policy="{self.approval_policy}"',
            "--sandbox",
            self.sandbox,
            "--skip-git-repo-check",
            "--model",
            self.model,
            "-C",
            self.cwd,
        ]
        cmd.append("-")
        return cmd

    def stream(
        self,
        user_msg: str,
        conversation_history: list,
        instructions: str,
        tools: list,
        max_tokens: int = 4096,
        cancel_event=None,
    ) -> Iterator[Tuple[str, dict]]:
        prompt = _build_codex_prompt(
            user_msg=user_msg,
            conversation_history=conversation_history,
            instructions=instructions,
            cwd=self.cwd,
        )
        try:
            proc = subprocess.Popen(
                self._command(),
                cwd=self.cwd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except Exception as exc:
            yield (EVENT_ERROR, {"message": f"Failed to start Codex CLI: {exc}"})
            return

        try:
            assert proc.stdin is not None
            proc.stdin.write(prompt)
            proc.stdin.close()
        except Exception:
            pass

        output_q: "queue.Queue[tuple[str, str]]" = queue.Queue()

        def _reader(stream, stream_name: str) -> None:
            try:
                for line in iter(stream.readline, ""):
                    output_q.put((stream_name, line))
            finally:
                try:
                    stream.close()
                except Exception:
                    pass

        threads = []
        for stream, name in ((proc.stdout, "stdout"), (proc.stderr, "stderr")):
            if stream is None:
                continue
            t = threading.Thread(target=_reader, args=(stream, name), daemon=True)
            t.start()
            threads.append(t)

        agent_messages: list[str] = []
        stderr_tail: list[str] = []
        usage: dict = {}
        error_message = ""

        while True:
            if cancel_event is not None and cancel_event.is_set() and proc.poll() is None:
                try:
                    proc.kill()
                except Exception:
                    pass
                yield (EVENT_ERROR, {"message": "Codex CLI run cancelled"})
                return

            try:
                stream_name, line = output_q.get(timeout=0.1)
            except queue.Empty:
                if proc.poll() is not None and output_q.empty():
                    break
                continue

            if stream_name == "stderr":
                text = line.strip()
                if text:
                    stderr_tail.append(text)
                    stderr_tail = stderr_tail[-12:]
                continue

            raw = line.strip()
            if not raw:
                continue
            try:
                event = json.loads(raw)
            except json.JSONDecodeError:
                continue
            etype = event.get("type", "")
            if etype == "item.completed":
                item = event.get("item") or {}
                if item.get("type") == "agent_message":
                    text = str(item.get("text", "") or "")
                    if text:
                        agent_messages.append(text)
            elif etype == "turn.completed":
                usage = event.get("usage") or {}
            elif etype == "error":
                error_message = str(event.get("message", "") or "")
            elif etype == "turn.failed":
                err = event.get("error") or {}
                error_message = str(err.get("message", "") or error_message)

        for t in threads:
            t.join(timeout=0.2)

        returncode = proc.poll()
        final_text = "\n\n".join(m.strip() for m in agent_messages if m.strip()).strip()
        if final_text:
            yield (EVENT_TEXT_DELTA, {"delta": final_text})
            yield (EVENT_DONE, {"model": self.model, "stats": usage or None, "cognitive_state": None})
            return

        if error_message:
            yield (EVENT_ERROR, {"message": error_message})
            return
        if returncode:
            detail = "\n".join(stderr_tail).strip()
            yield (EVENT_ERROR, {"message": detail or f"Codex CLI exited with code {returncode}"})
            return
        yield (EVENT_DONE, {"model": self.model, "stats": usage or None, "cognitive_state": None})


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

def create_backend(
    backend_type: str,
    url: str = None,
    model: str = None,
    api_key: str = None,
    base_url: str = None,
    cwd: str = None,
    permission_mode: str = None,
    local_root: str = None,
    thinking: str | None = None,
):
    """Create a backend instance.

    Ollama remains the local-first default. Kimi uses Moonshot's direct,
    OpenAI-compatible endpoint; Codex delegates to the installed CLI.
    """
    if backend_type == "codex":
        return CodexCliBackend(
            model or codex_cli_models()[0],
            cwd=cwd,
            sandbox=os.environ.get("RESONANT_CODEX_SANDBOX") or None,
            permission_mode=permission_mode,
        )
    if backend_type == "kimi":
        return KimiBackend(
            api_key=api_key or "",
            model=model or KimiBackend.DEFAULT_MODEL,
            base_url=base_url or KimiBackend.DEFAULT_BASE_URL,
        )
    if backend_type != "ollama":
        raise ValueError(
            f"Unsupported backend {backend_type!r}. Resonant Client supports "
            f"Ollama, Kimi, and Codex."
        )
    if not model:
        raise ValueError("Model name required for Ollama backend")
    return OllamaBackend(url, model, thinking=thinking)
