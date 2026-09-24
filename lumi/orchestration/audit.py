"""
Append-only audit log per intent.

Captures every decision the orchestrator made — specialist picked, skill
loaded, plan rewrite, confidence change, tool call, floor violation — so the
user can replay any past run forensically. Stored as JSONL so tail/append is
cheap and the file is readable in any text editor.

Layout:
    ~/.resonant/projects/<sha1[:12]>/intents/<intent-id>/audit.jsonl
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from pathlib import Path
from typing import Iterator, Optional

logger = logging.getLogger(__name__)


# ── Categories used by `kind` filter on read ────────────────────────────


KIND_DECISION = "decision"        # specialist picked, skill loaded, plan rewrite
KIND_TOOL_CALL = "tool_call"      # one tool dispatch (call + result)
KIND_PLAN_CHANGE = "plan_change"  # snapshot of node status / confidence change
KIND_FLOOR = "floor_violation"    # an irreversibility check fired
KIND_OTHER = "other"


# ── Path resolution ─────────────────────────────────────────────────────


def _state_home() -> Path:
    return Path(os.environ.get("RESONANT_STATE_HOME") or (Path.home() / ".resonant"))


def _project_hash(project_path: str | Path) -> str:
    return hashlib.sha1(str(project_path).encode("utf-8", errors="replace")).hexdigest()[:12]


def audit_path(project_path: str | Path, intent_id: str) -> Path:
    """Resolve the JSONL path for an intent, creating dirs as needed."""
    if not intent_id:
        raise ValueError("audit_path requires a non-empty intent_id")
    p = (_state_home() / "projects" / _project_hash(project_path)
         / "intents" / intent_id / "audit.jsonl")
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


# ── Append ──────────────────────────────────────────────────────────────


def append_event(
    project_path: str | Path,
    intent_id: str,
    *,
    kind: str,
    payload: Optional[dict] = None,
) -> None:
    """Write one event line. Failures are logged, not raised — audit is best-effort."""
    record = {
        "ts": time.time(),
        "kind": kind,
        "payload": payload or {},
    }
    try:
        target = audit_path(project_path, intent_id)
        with target.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError as exc:
        logger.warning("Failed to append audit event for intent %s: %s", intent_id, exc)


# Convenience wrappers (so call sites don't need to remember the `kind` strings)


def log_decision(project_path, intent_id, *, summary: str, **extra) -> None:
    append_event(project_path, intent_id, kind=KIND_DECISION,
                 payload={"summary": summary, **extra})


def log_tool_call(project_path, intent_id, *,
                  tool_name: str, args: dict, result_summary: str = "",
                  is_error: bool = False, duration_ms: Optional[float] = None) -> None:
    append_event(project_path, intent_id, kind=KIND_TOOL_CALL, payload={
        "tool_name": tool_name,
        "args": _redact_args(args),
        "result_summary": result_summary[:500] if result_summary else "",
        "is_error": is_error,
        "duration_ms": duration_ms,
    })


def log_plan_change(project_path, intent_id, *,
                    node_id: str, change: str, **extra) -> None:
    append_event(project_path, intent_id, kind=KIND_PLAN_CHANGE,
                 payload={"node_id": node_id, "change": change, **extra})


def log_floor_violation(project_path, intent_id, *,
                        rule: str, reason: str, tool_name: str = "") -> None:
    append_event(project_path, intent_id, kind=KIND_FLOOR, payload={
        "rule": rule, "reason": reason, "tool_name": tool_name,
    })


# ── Read / replay ───────────────────────────────────────────────────────


def read_events(
    project_path: str | Path,
    intent_id: str,
    *,
    kind_filter: Optional[set[str]] = None,
    limit: Optional[int] = None,
) -> list[dict]:
    """Load events for an intent (newest first by default).

    Filter by `kind_filter` to scope to e.g. just plan changes or just tool calls.
    `limit` caps the number of events returned (most recent N).
    """
    target = audit_path(project_path, intent_id)
    if not target.is_file():
        return []
    out: list[dict] = []
    try:
        with target.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if kind_filter and rec.get("kind") not in kind_filter:
                    continue
                out.append(rec)
    except OSError as exc:
        logger.warning("Failed to read audit log for %s: %s", intent_id, exc)
        return []
    out.sort(key=lambda e: e.get("ts", 0), reverse=True)
    if limit is not None:
        out = out[:limit]
    return out


def stream_events(project_path: str | Path, intent_id: str) -> Iterator[dict]:
    """Generator over all events, oldest first. Used for replay."""
    target = audit_path(project_path, intent_id)
    if not target.is_file():
        return
    try:
        with target.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    continue
    except OSError as exc:
        logger.warning("Failed to stream audit log for %s: %s", intent_id, exc)


# ── Internals ───────────────────────────────────────────────────────────


_REDACT_KEYS = ("api_key", "password", "secret", "token", "authorization")


def _redact_args(args: dict) -> dict:
    """Strip likely-secret values before persisting. Best-effort, not a guarantee."""
    if not isinstance(args, dict):
        return {"_": str(args)[:200]}
    out: dict = {}
    for k, v in args.items():
        kl = k.lower()
        if any(s in kl for s in _REDACT_KEYS):
            out[k] = "[redacted]"
        elif isinstance(v, str) and len(v) > 1000:
            out[k] = v[:1000] + "...[truncated]"
        else:
            out[k] = v
    return out
