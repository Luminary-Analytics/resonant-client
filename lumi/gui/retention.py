"""Delete local transcripts after a set number of days.

Settings > Privacy & security ("Delete transcripts after") or organization
policy sets ``privacy.transcript_retention_days``; 0, the default, keeps
everything. At startup and once a day Lumi deletes whatever holds
conversation content and was last touched longer ago than that:

* saved sessions (``projects/<p>/sessions/<id>.json`` and their
  ``<id>.events.jsonl`` ledgers) and unsent drafts (``drafts/``);
* checkpoints (``checkpoints/<run>/``, full conversation copies and workspace
  snapshots), worker records (``agents/``), artifacts (``artifacts/``: input
  images, large tool outputs, screenshots), traces (``traces/<run>/``) and
  mission audit logs (``intents/<id>/``);
* screen recordings (``recordings/``) and dated logs (``logs/YYYY-MM-DD/``).

The session that is open is never deleted, whatever its age; a run in
progress keeps touching its own folders, so it is never old enough to go.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import shutil
import time
from datetime import date, timedelta
from pathlib import Path
from typing import Iterable

from .sessions import _invalidate_session_summary

logger = logging.getLogger(__name__)

DAY = 86400
_DATED = re.compile(r"^\d{4}-\d{2}-\d{2}$")
# Per-project stores whose entries (files or folders) hold conversation content.
_PROJECT_STORES = ("drafts", "checkpoints", "agents", "artifacts", "traces", "intents")
# Bookkeeping files kept in place; entries they list may be gone.
_KEEP_NAMES = {"manifest.jsonl"}


def _newest_mtime(path: Path) -> float:
    """The latest modification time of a file, or of anything inside a folder."""
    try:
        newest = path.stat().st_mtime
    except OSError:
        return time.time()
    if path.is_dir():
        for root, _dirs, files in os.walk(path):
            for name in files:
                try:
                    newest = max(newest, os.stat(os.path.join(root, name)).st_mtime)
                except OSError:
                    continue
    return newest


def _remove(path: Path) -> bool:
    try:
        if path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink(missing_ok=True)
        return True
    except OSError:
        logger.warning("Could not delete expired %s", path, exc_info=True)
        return False


def _draft_name(session_id: str) -> str:
    return hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:32] + ".json"


def purge_expired(
    days: int,
    state_dir: str | Path,
    *,
    keep_session_ids: Iterable[str] = (),
    now: float | None = None,
) -> dict:
    """Delete transcripts older than ``days``; return how many entries of each kind went."""
    removed = {"sessions": 0, "logs": 0, "recordings": 0, **{store: 0 for store in _PROJECT_STORES}}
    if not days or days <= 0:
        return removed
    now = time.time() if now is None else now
    cutoff = now - days * DAY
    keep = {str(item) for item in keep_session_ids if item}
    keep_drafts = {_draft_name(item) for item in keep}
    root = Path(state_dir)
    projects = root / "projects"

    for session_file in sorted(projects.glob("*/sessions/*.json")):
        ledger = session_file.with_name(session_file.stem + ".events.jsonl")
        if session_file.stem in keep or max(_newest_mtime(session_file), _newest_mtime(ledger)) >= cutoff:
            continue
        if _remove(session_file):
            ledger.unlink(missing_ok=True)
            _invalidate_session_summary(session_file)
            removed["sessions"] += 1

    for store in _PROJECT_STORES:
        for entry in sorted(projects.glob(f"*/{store}/*")):
            if entry.name in _KEEP_NAMES or (store == "drafts" and entry.name in keep_drafts):
                continue
            if _newest_mtime(entry) < cutoff and _remove(entry):
                removed[store] += 1

    for entry in sorted((root / "recordings").glob("*")):
        if _newest_mtime(entry) < cutoff and _remove(entry):
            removed["recordings"] += 1

    oldest_kept = date.fromtimestamp(cutoff)
    logs = root / "logs"
    if logs.is_dir():
        for entry in os.scandir(logs):
            if not entry.is_dir() or not _DATED.match(entry.name):
                continue
            try:
                day = date.fromisoformat(entry.name)
            except ValueError:
                continue
            # A day's folder holds logs up to the end of that day.
            if day + timedelta(days=1) <= oldest_kept:
                shutil.rmtree(entry.path, ignore_errors=True)
                removed["logs"] += 1

    if any(removed.values()):
        logger.info("Retention (%d days) deleted: %s", days, {k: v for k, v in removed.items() if v})
    return removed
