"""A local, tamper-evident audit log of what Lumi and its agent did.

Every record is one JSON line in ``~/.lumi/audit/YYYY-MM-DD.jsonl`` (UTC days)::

    {"v": 1, "seq": 42, "ts": "2026-09-25T04:00:00.123Z", "type": "tool.result",
     "session": "...", "project": "...", "data": {...},
     "prev": "<hash of record 41>", "hash": "<sha256 of prev + this record>"}

The records form one hash chain across files and processes (the GUI, the
terminal UI and the chat gateway share the log through an OS file lock), so an
edited, removed or reordered record fails ``verify``. Removing the newest
records, or whole days older than every remaining one, can't be detected from
the log alone; export to a collector for a copy off the machine.

Event types: ``turn.start``, ``turn.end``, ``model.usage``, ``tool.call``,
``tool.result``, ``file.change``, ``approval``, ``privacy.redaction``,
``settings.change``, ``trust.decision``, ``budget.warning``, ``budget.approval``,
``budget.block``, ``model.fallback`` and ``error``.

**Capture levels** (``privacy.audit_capture``, lockable by policy):

* ``metadata`` (the default): names, outcomes, sizes, file paths and SHA-256
  digests. Never prompts, file contents, command lines or output. A digest of
  short text can be confirmed by guessing it, so this is not anonymization;
* ``redacted``: content too, truncated, with credential formats removed;
* ``full``: content up to a larger limit.

Saved key values are removed at every level.

With ``audit.otlp_endpoint`` set, records are also exported over OTLP/HTTP JSON
as spans with the OpenTelemetry GenAI semantic conventions
(``gen_ai.request.model``, ``gen_ai.usage.input_tokens``, ``gen_ai.tool.name``
…), from a bounded background queue that never blocks the app.

``privacy.audit_log`` turns the local log off; ``privacy.audit_retention_days``
(default 365) deletes older days. Transcript retention doesn't touch the log.
"""

from __future__ import annotations

import hashlib
import json
import logging
import queue
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .file_lock import exclusive

logger = logging.getLogger(__name__)

CAPTURE_LEVELS = ("metadata", "redacted", "full")
_LIMITS = {"redacted": 2_000, "full": 20_000}
_PATH_LIMIT = 1_000
_GENESIS = "0" * 64
_TAIL_BYTES = 64 * 1024


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def digest(text: Any) -> str:
    return hashlib.sha256(str(text or "").encode("utf-8", errors="replace")).hexdigest()


def _line(body: dict) -> str:
    # ASCII escapes keep any text (even lone surrogates from decoded output)
    # writable and byte-for-byte reproducible when verifying.
    return json.dumps(body, ensure_ascii=True, sort_keys=True, default=str)


def _last_record(path: Path) -> dict | None:
    """The newest parseable record in one file, reading from the end."""
    try:
        size = path.stat().st_size
        window = _TAIL_BYTES
        with open(path, "rb") as handle:
            while True:
                start = max(0, size - window)
                handle.seek(start)
                lines = handle.read().decode("utf-8", errors="replace").splitlines()
                if start:
                    lines = lines[1:]  # the first line may be cut
                for line in reversed(lines):
                    try:
                        record = json.loads(line)
                        if isinstance(record, dict) and "seq" in record and "hash" in record:
                            return record
                    except ValueError:
                        continue
                if not start:
                    return None
                window *= 4
    except OSError:
        return None


class AuditLog:
    """Append-only, hash-chained audit records under one folder."""

    def __init__(self, root: str | Path):
        self.root = Path(root)
        self._lock = threading.Lock()
        self.enabled = True
        self.capture = "metadata"
        self.retention_days = 365
        self._known_secrets: tuple[str, ...] = ()
        self._exporter: OtlpExporter | None = None
        self._written: tuple[str, int] | None = None
        self._seq, self._prev = self._resume()

    # ── Configuration ──────────────────────────────────────────────────────
    def configure(self, settings: Any) -> dict:
        """Apply the audit settings (policy-locked values arrive through ``settings.get``)."""
        def get(section: str, key: str, default: Any = None) -> Any:
            return settings.get(section, key, default) if settings is not None else default

        self.enabled = get("privacy", "audit_log", True) is not False
        level = str(get("privacy", "audit_capture", "metadata") or "metadata")
        self.capture = level if level in CAPTURE_LEVELS else "metadata"
        try:
            self.retention_days = max(0, int(get("privacy", "audit_retention_days", 365) or 0))
        except (TypeError, ValueError):
            self.retention_days = 365
        try:
            from .secret_scan import secret_values

            self._known_secrets = tuple(sorted(
                (value for value in secret_values(settings, min_length=8) if value), key=len, reverse=True))
        except Exception:
            logger.debug("Could not load saved key values for the audit log", exc_info=True)
            self._known_secrets = ()
        endpoint = str(get("audit", "otlp_endpoint", "") or "").strip()
        header = str(get("audit", "otlp_auth_header", "Authorization") or "Authorization").strip()
        token = str(get("api_keys", "otlp", "") or "")
        if endpoint:
            headers = {header: token} if token else {}
            if not self._exporter or self._exporter.endpoint != endpoint.rstrip("/") or self._exporter.headers != headers:
                if self._exporter:
                    self._exporter.stop()
                self._exporter = OtlpExporter(endpoint, headers=headers)
        elif self._exporter:
            self._exporter.stop()
            self._exporter = None
        return {"enabled": self.enabled, "capture": self.capture, "otlp": bool(endpoint)}

    # ── Content according to the capture level ─────────────────────────────
    def _without_saved_keys(self, value: str) -> str:
        for secret in self._known_secrets:
            value = value.replace(secret, "[REDACTED saved API key]")
        return value

    def content(self, text: Any) -> dict:
        """How a piece of content is recorded at the current capture level."""
        value = "" if text is None else str(text)
        record: dict[str, Any] = {"chars": len(value), "sha256": digest(value)}
        if self.capture == "metadata" or not value:
            return record
        value = self._without_saved_keys(value)
        if self.capture == "redacted":
            from .secret_scan import redact_text

            value, _ = redact_text(value, patterns=True, known=())
        limit = _LIMITS[self.capture]
        record["text"] = value if len(value) <= limit else value[:limit] + f"…[{len(value) - limit} more]"
        return record

    def name(self, text: Any) -> str:
        """A path or other name, recorded as text at every level (saved keys removed)."""
        value = self._without_saved_keys("" if text is None else str(text))
        return value if len(value) <= _PATH_LIMIT else value[:_PATH_LIMIT] + "…"

    # ── Writing ────────────────────────────────────────────────────────────
    def _file_state(self, path: Path) -> tuple[str, int]:
        try:
            return path.name, path.stat().st_size
        except OSError:
            return path.name, 0

    def record(self, event_type: str, *, session: str = "", project: str = "", **data: Any) -> dict | None:
        """Append one record; returns it (None when the log is off)."""
        if not self.enabled:
            return None
        with self._lock, exclusive(self.root / ".lock"):
            ts = _now_iso()
            path = self.root / f"{ts[:10]}.jsonl"
            # Another process may have written since this one did: continue
            # from the newest record on disk so the chain stays linear.
            if self._file_state(path) != self._written:
                self._seq, self._prev = self._resume()
            body = {
                "v": 1,
                "seq": self._seq + 1,
                "ts": ts,
                "type": event_type,
                "session": session,
                "project": project,
                "data": data,
                "prev": self._prev,
            }
            body["hash"] = hashlib.sha256((self._prev + _line(body)).encode("utf-8")).hexdigest()
            try:
                self.root.mkdir(parents=True, exist_ok=True)
                with open(path, "a", encoding="utf-8", newline="\n") as handle:
                    handle.write(_line(body) + "\n")
            except OSError:
                logger.warning("Could not write the audit log", exc_info=True)
                return None
            self._seq, self._prev = body["seq"], body["hash"]
            self._written = self._file_state(path)
        if self._exporter:
            self._exporter.submit(body)
        return body

    # ── Chain maintenance ──────────────────────────────────────────────────
    def _files(self) -> list[Path]:
        return sorted(self.root.glob("????-??-??.jsonl")) if self.root.is_dir() else []

    def _resume(self) -> tuple[int, str]:
        """Continue the chain from the newest record on disk."""
        files = self._files()
        self._written = self._file_state(files[-1]) if files else None
        for path in reversed(files):
            record = _last_record(path)
            if record is not None:
                try:
                    return int(record["seq"]), str(record["hash"])
                except (TypeError, ValueError):
                    continue
        return 0, _GENESIS

    def purge(self, now: float | None = None) -> int:
        """Delete days older than the audit retention; returns how many were deleted.

        Verification then starts from the oldest remaining record.
        """
        if not self.retention_days:
            return 0
        today = datetime.fromtimestamp(now if now is not None else time.time(), timezone.utc).date()
        cutoff = today - timedelta(days=self.retention_days)
        removed = 0
        with self._lock, exclusive(self.root / ".lock"):
            for path in self._files():
                try:
                    if datetime.strptime(path.stem, "%Y-%m-%d").date() < cutoff:
                        path.unlink()
                        removed += 1
                except (ValueError, OSError):
                    continue
        return removed

    def _check(self) -> tuple[bool, str, int]:
        prev: str | None = None
        count = 0
        for path in self._files():
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
            except (OSError, UnicodeDecodeError):
                return False, f"{path.name} can't be read", count
            for number, line in enumerate(lines, start=1):
                try:
                    record = json.loads(line)
                    claimed = record.pop("hash")
                    link = record["prev"]
                except (ValueError, KeyError, TypeError, AttributeError):
                    return False, f"{path.name}:{number} is not a valid record", count
                if prev is not None and link != prev:
                    return False, f"{path.name}:{number} doesn't follow the previous record", count
                if hashlib.sha256((str(link) + _line(record)).encode("utf-8")).hexdigest() != claimed:
                    return False, f"{path.name}:{number} was changed", count
                prev = claimed
                count += 1
        return True, "", count

    def verify(self) -> tuple[bool, str]:
        """Check the hash chain over every file; returns (ok, problem)."""
        ok, problem, _ = self._check()
        return ok, problem

    def status(self) -> dict:
        """Where the log is, whether it verifies, and how the export is doing."""
        ok, problem, count = self._check()
        exporter = self._exporter
        return {
            "path": str(self.root),
            "enabled": self.enabled,
            "capture": self.capture,
            "retention_days": self.retention_days,
            "records": count,
            "verified": ok,
            "problem": problem,
            "export": exporter.status() if exporter else None,
        }


# ── OpenTelemetry export ────────────────────────────────────────────────────


def _attribute(key: str, value: Any) -> dict:
    if isinstance(value, bool):
        return {"key": key, "value": {"boolValue": value}}
    if isinstance(value, int):
        return {"key": key, "value": {"intValue": str(value)}}
    if isinstance(value, float):
        return {"key": key, "value": {"doubleValue": value}}
    return {"key": key, "value": {"stringValue": str(value)}}


def to_span(record: dict) -> dict:
    """One audit record as an OTLP span with GenAI semantic-convention attributes."""
    data = record.get("data") or {}
    kind = record.get("type", "")
    end_ns = int(datetime.fromisoformat(record["ts"].replace("Z", "+00:00")).timestamp() * 1e9)
    try:
        elapsed = max(0.0, float(data.get("elapsed") or 0))
    except (TypeError, ValueError):
        elapsed = 0.0
    attributes = [_attribute("lumi.event", kind), _attribute("lumi.seq", int(record.get("seq", 0)))]
    if record.get("session"):
        attributes.append(_attribute("session.id", record["session"]))
    if record.get("project"):
        attributes.append(_attribute("lumi.project", record["project"]))
    span_kind = 1  # INTERNAL
    if kind == "model.usage":
        provider = str(data.get("provider") or "")
        attributes += [
            _attribute("gen_ai.operation.name", "chat"),
            _attribute("gen_ai.provider.name", provider),
            _attribute("gen_ai.system", provider),  # the name before semconv 1.37
            _attribute("gen_ai.request.model", str(data.get("model") or "")),
            _attribute("gen_ai.usage.input_tokens", int(data.get("input_tokens") or 0)),
            _attribute("gen_ai.usage.output_tokens", int(data.get("output_tokens") or 0)),
        ]
        name = f"chat {data.get('model') or ''}".strip()
        span_kind = 3  # CLIENT
    elif kind in {"tool.call", "tool.result"}:
        attributes.append(_attribute("gen_ai.tool.name", str(data.get("tool") or "")))
        if data.get("call_id"):
            attributes.append(_attribute("gen_ai.tool.call.id", str(data["call_id"])))
        if kind == "tool.result":
            # The execution itself; the call is the model's request for it.
            attributes.append(_attribute("gen_ai.operation.name", "execute_tool"))
            name = f"execute_tool {data.get('tool') or ''}".strip()
        else:
            name = f"tool.call {data.get('tool') or ''}".strip()
    else:
        name = kind
    for key, value in data.items():
        if isinstance(value, (str, int, float, bool)) and key != "elapsed":
            attributes.append(_attribute(f"lumi.{key}", value))
        elif isinstance(value, dict) and {"chars", "sha256"} <= set(value):
            attributes.append(_attribute(f"lumi.{key}.sha256", value["sha256"]))
            if "text" in value:
                attributes.append(_attribute(f"lumi.{key}", value["text"]))
    trace_id = hashlib.sha256(str(record.get("session") or record.get("seq")).encode()).hexdigest()[:32]
    return {
        "traceId": trace_id,
        "spanId": str(record.get("hash") or digest(record.get("seq")))[:16],
        "name": name,
        "kind": span_kind,
        "startTimeUnixNano": str(max(0, end_ns - int(elapsed * 1e9))),
        "endTimeUnixNano": str(end_ns),
        "attributes": attributes,
    }


class OtlpExporter:
    """Sends spans to an OTLP/HTTP JSON collector from a background thread."""

    def __init__(self, endpoint: str, *, headers: dict[str, str] | None = None, transport: Any = None,
                 batch_size: int = 100, interval: float = 5.0, max_queue: int = 5_000):
        self.endpoint = endpoint.rstrip("/")
        self.headers = dict(headers or {})
        self._transport = transport
        self._queue: queue.Queue = queue.Queue(maxsize=max_queue)
        self._batch_size = batch_size
        self._interval = interval
        self._stop = threading.Event()
        self.sent = 0
        self.dropped = 0
        self.last_error = ""
        self._thread = threading.Thread(target=self._run, daemon=True, name="lumi-otlp")
        self._thread.start()

    @property
    def url(self) -> str:
        return self.endpoint if self.endpoint.endswith("/v1/traces") else self.endpoint + "/v1/traces"

    def status(self) -> dict:
        return {"endpoint": self.url, "sent": self.sent, "dropped": self.dropped,
                "queued": self._queue.qsize(), "last_error": self.last_error}

    def submit(self, record: dict) -> None:
        try:
            self._queue.put_nowait(to_span(record))
        except queue.Full:
            self.dropped += 1  # never block the app on a slow collector
        except Exception:
            logger.debug("Could not convert an audit record to a span", exc_info=True)

    def flush(self) -> None:
        """Send everything queued now, on the calling thread (tests and shutdown)."""
        self._send_all()

    def stop(self) -> None:
        """Stop after sending what is queued, without waiting for it."""
        self._stop.set()

    def _drain(self) -> list[dict]:
        spans: list[dict] = []
        while len(spans) < self._batch_size:
            try:
                spans.append(self._queue.get_nowait())
            except queue.Empty:
                break
        return spans

    def _send_all(self) -> None:
        # Batches until the queue is empty; a failed batch ends the round so a
        # collector that is down costs one timeout per interval, not per batch.
        while True:
            spans = self._drain()
            if not spans or not self._send(spans):
                return

    def _send(self, spans: list[dict]) -> bool:
        import httpx

        from . import __version__
        from .net import client_options

        payload = {"resourceSpans": [{
            "resource": {"attributes": [_attribute("service.name", "lumi"),
                                        _attribute("service.version", __version__)]},
            "scopeSpans": [{"scope": {"name": "lumi.audit"}, "spans": spans}],
        }]}
        try:
            with httpx.Client(**client_options(timeout=10.0, transport=self._transport)) as client:
                response = client.post(self.url, json=payload, headers=self.headers)
        except httpx.HTTPError as exc:
            self.last_error = type(exc).__name__
            self.dropped += len(spans)
            return False
        if response.status_code >= 300:
            self.last_error = f"HTTP {response.status_code}"
            self.dropped += len(spans)
            return False
        self.sent += len(spans)
        self.last_error = ""
        return True

    def _run(self) -> None:
        while not self._stop.wait(self._interval):
            self._send_all()
        self._send_all()


# ── The process-wide log ────────────────────────────────────────────────────

_log: AuditLog | None = None
_log_lock = threading.Lock()


def audit_log() -> AuditLog:
    global _log
    with _log_lock:
        if _log is None:
            from .paths import state_home

            _log = AuditLog(state_home() / "audit")
        return _log


def set_for_tests(log: AuditLog | None) -> None:
    """Replace the process-wide log (None: the next use creates the default one)."""
    global _log
    with _log_lock:
        previous, _log = _log, log
    if previous is not None and previous is not log and previous._exporter:
        previous._exporter.stop()


def configure(settings: Any) -> dict:
    return audit_log().configure(settings)


def record(event_type: str, **fields: Any) -> dict | None:
    """Record one event; never raises."""
    try:
        return audit_log().record(event_type, **fields)
    except Exception:
        logger.debug("Audit record failed", exc_info=True)
        return None


def content(text: Any) -> dict:
    return audit_log().content(text)


def name(text: Any) -> str:
    return audit_log().name(text)


_NAME_ARGUMENTS = ("path", "cwd", "working_subdir")
_CONTENT_ARGUMENTS = ("pattern", "query", "url", "command")


def summarize_args(arguments: Any) -> dict:
    """What tool arguments look like in the log: their names, paths as text,
    and commands, patterns and URLs by capture level."""
    if not isinstance(arguments, dict):
        return {"arguments": content(arguments)}
    summary: dict[str, Any] = {"arguments": sorted(str(key) for key in arguments)}
    for key in _NAME_ARGUMENTS:
        if isinstance(arguments.get(key), str):
            summary[key] = name(arguments[key])
    for key in _CONTENT_ARGUMENTS:
        if key in arguments:
            summary[key] = content(arguments[key])
    return summary
