"""
JSONL Event Logger for Resonant Sessions.

Writes every session event as a JSON line to a log file.
Enables debugging, replay, dashboard analytics, and audit trails.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Iterator, Optional

logger = logging.getLogger(__name__)


class EventLogger:
    """
    Thread-safe JSONL event logger.

    Writes events to: <log_dir>/<date>/<session_id>.jsonl
    Each line is a complete JSON object with timestamp and session_id added.
    """

    def __init__(
        self,
        log_dir: str | Path | None = None,
        session_id: str = "",
        enabled: bool = True,
    ):
        self.enabled = enabled
        self.session_id = session_id
        self._log_dir = Path(log_dir) if log_dir else Path.home() / ".resonant" / "logs"
        self._file = None
        self._lock = threading.Lock()
        self._event_count = 0

        if self.enabled:
            try:
                date_dir = self._log_dir / datetime.now().strftime("%Y-%m-%d")
                date_dir.mkdir(parents=True, exist_ok=True)
                log_path = date_dir / f"{session_id or 'unknown'}.jsonl"
                self._file = open(log_path, "a", encoding="utf-8")
            except Exception as e:
                logger.warning("Failed to open event log: %s", e)
                self.enabled = False

    def log(self, event: dict) -> None:
        """Append an event as a JSON line to the log file."""
        if not self.enabled or not self._file:
            return

        enriched = {
            "timestamp": datetime.now().isoformat(),
            "session_id": self.session_id,
            **event,
        }

        with self._lock:
            try:
                self._file.write(json.dumps(enriched, default=str) + "\n")
                self._event_count += 1
                # Flush periodically for near-real-time log tailing
                if self._event_count % 5 == 0:
                    self._file.flush()
            except Exception as e:
                logger.warning("Failed to write event log: %s", e)

    def close(self) -> None:
        """Flush and close the log file."""
        if self._file:
            with self._lock:
                try:
                    self._file.flush()
                    self._file.close()
                except Exception:
                    pass
                self._file = None

    def __del__(self):
        self.close()


def replay(path: str | Path) -> Iterator[dict]:
    """Read events from a JSONL log file for debugging/replay."""
    log_path = Path(path)
    if not log_path.exists():
        return

    with open(log_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    continue


def cleanup_old_logs(log_dir: str | Path | None = None, retention_days: int = 7) -> int:
    """
    Delete log directories older than retention_days.
    Returns number of directories removed.
    """
    base = Path(log_dir) if log_dir else Path.home() / ".resonant" / "logs"
    if not base.exists():
        return 0

    cutoff = time.time() - (retention_days * 86400)
    removed = 0

    for entry in base.iterdir():
        if entry.is_dir():
            try:
                # Parse date from directory name
                dir_date = datetime.strptime(entry.name, "%Y-%m-%d")
                if dir_date.timestamp() < cutoff:
                    import shutil
                    shutil.rmtree(entry)
                    removed += 1
                    logger.info("Cleaned up old log directory: %s", entry)
            except (ValueError, OSError):
                continue

    return removed
