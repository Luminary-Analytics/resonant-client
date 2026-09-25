"""What the agent got done on this computer: turns, their outcomes, verified checks.

Each finished turn adds one line to ``~/.lumi/activity.jsonl``: when it ended,
its outcome (``completed``, ``error`` or ``cancelled``), whether a check the
agent ran passed during it (``check_run``), and how many files it changed. A
crash of the app adds a ``crash`` line. Nothing else is kept: no prompts,
answers, file names, paths or titles.

These counts give:

* **Cost per verified task** on Settings > Usage & cost (this month's spend
  divided by turns with a passing check);
* **Lumi Cloud check-ins** (``lumi/cloud.py``): the counts since the last
  check-in, for the organization's fleet health and adoption pages.

Lines older than ``KEEP_DAYS`` are dropped.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from .paths import state_home

logger = logging.getLogger(__name__)

KEEP_DAYS = 90
OUTCOMES = ("completed", "error", "cancelled")
WRITE_TOOLS = frozenset({"file_edit", "file_write", "file_replace"})

_lock = threading.Lock()
_path_override: Path | None = None


def _path() -> Path:
    return _path_override or state_home() / "activity.jsonl"


def set_path_for_tests(path: Path | None) -> None:
    global _path_override
    _path_override = path


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _moment(text: str) -> datetime | None:
    try:
        moment = datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def classify(events: Iterable[Any], *, cancelled: bool = False) -> dict:
    """A turn's outcome from its display events (the same events the chat shows)."""
    kinds, verified, files = [], False, 0
    for event in events:
        if not isinstance(event, dict):
            continue
        kind = event.get("event")
        kinds.append(kind)
        if kind == "tool.result" and not event.get("is_error") and not event.get("denied"):
            if event.get("name") == "check_run":
                verified = True
            elif event.get("name") in WRITE_TOOLS:
                files += 1
    if cancelled:
        outcome = "cancelled"
    elif "error" in kinds:
        outcome = "error"
    elif any(kind in ("text.done", "session.end") for kind in kinds):
        outcome = "completed"
    else:
        outcome = "cancelled"  # nothing finished: stopped or interrupted
    return {"outcome": outcome, "verified": verified and outcome == "completed", "files_changed": files}


def _append(entry: dict) -> None:
    path = _path()
    try:
        with _lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(entry, separators=(",", ":")) + "\n")
    except OSError:
        logger.debug("Couldn't record activity", exc_info=True)


def record_turn(events: Iterable[Any], *, cancelled: bool = False, now: datetime | None = None) -> dict:
    entry = {"t": _iso(now or _now()), "kind": "turn", **classify(events, cancelled=cancelled)}
    _append(entry)
    return entry


def record_crash(now: datetime | None = None) -> None:
    _append({"t": _iso(now or _now()), "kind": "crash"})


def _entries() -> list[dict]:
    path = _path()
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    cutoff = _now() - timedelta(days=KEEP_DAYS)
    entries, stale = [], 0
    for line in lines:
        try:
            entry = json.loads(line)
        except ValueError:
            stale += 1
            continue
        moment = _moment(entry.get("t", "")) if isinstance(entry, dict) else None
        if moment is None or moment < cutoff:
            stale += 1
            continue
        entries.append(entry)
    if stale > 1000 or (stale and stale * 4 > len(lines)):
        _rewrite(entries)
    return entries


def _rewrite(entries: list[dict]) -> None:
    path = _path()
    temporary = path.with_suffix(".jsonl.tmp")
    try:
        with _lock:
            temporary.write_text("".join(json.dumps(e, separators=(",", ":")) + "\n" for e in entries),
                                 encoding="utf-8")
            os.replace(temporary, path)
    except OSError:
        logger.debug("Couldn't prune the activity file", exc_info=True)


def summary(since: str, until: str) -> dict:
    """Counts for ``since`` <= time < ``until`` (ISO 8601, UTC)."""
    counts = {"turns": 0, "completed": 0, "errors": 0, "cancelled": 0, "verified": 0, "files_changed": 0,
              "crashes": 0}
    start, end = _moment(since), _moment(until)
    if start is None or end is None:
        return counts
    for entry in _entries():
        moment = _moment(entry.get("t", ""))
        if moment is None or not (start <= moment < end):
            continue
        if entry.get("kind") == "crash":
            counts["crashes"] += 1
            continue
        counts["turns"] += 1
        outcome = entry.get("outcome")
        counts[{"completed": "completed", "error": "errors"}.get(outcome, "cancelled")] += 1
        counts["verified"] += 1 if entry.get("verified") else 0
        counts["files_changed"] += int(entry.get("files_changed") or 0)
    return counts


def month_summary(now: datetime | None = None) -> dict:
    now = now or _now()
    start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return summary(_iso(start), _iso(now + timedelta(seconds=1)))


def install_crash_counter() -> None:
    """Count unhandled exceptions (the app's own crashes), then let the usual handlers run."""
    previous, previous_thread = sys.excepthook, threading.excepthook

    def on_crash(kind, value, traceback):
        if not issubclass(kind, KeyboardInterrupt):
            record_crash()
        previous(kind, value, traceback)

    def on_thread_crash(args):
        if not issubclass(args.exc_type, SystemExit):
            record_crash()
        previous_thread(args)

    sys.excepthook = on_crash
    threading.excepthook = on_thread_crash
