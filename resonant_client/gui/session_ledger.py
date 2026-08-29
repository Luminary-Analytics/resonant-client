"""Versioned append-only storage and projections for saved sessions.

The desktop client historically persisted two large arrays in each session
metadata file: provider-facing conversation history and GUI display events.
This ledger moves both streams into one durable JSONL authority.  Projection
helpers rebuild the two existing runtime views so the engine and UI can migrate
without a flag day.
"""

from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable


SESSION_LEDGER_VERSION = 1
DEFAULT_DISPLAY_PAGE_SIZE = 240
MAX_DISPLAY_PAGE_SIZE = 1000

_LOCKS_GUARD = threading.Lock()
_PATH_LOCKS: dict[str, threading.RLock] = {}
_NEXT_SEQUENCES: dict[str, tuple[int, int, int]] = {}


def _path_lock(path: Path) -> threading.RLock:
    key = os.path.abspath(os.fspath(path))
    with _LOCKS_GUARD:
        return _PATH_LOCKS.setdefault(key, threading.RLock())


@dataclass(frozen=True, slots=True)
class DisplayEventPage:
    """One contiguous page from the display-event projection."""

    events: list[dict[str, Any]]
    start_seq: int | None
    end_seq: int | None
    has_more: bool
    total_events: int
    as_of_seq: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "events": self.events,
            "start_seq": self.start_seq,
            "end_seq": self.end_seq,
            "has_more": self.has_more,
            "total_events": self.total_events,
            "as_of_seq": self.as_of_seq,
        }


class SessionEventLedger:
    """Append-only JSONL ledger for one saved Resonant session."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        # SessionRecord.ledger is intentionally a cheap property, so several
        # wrapper instances can target the same file. Lock by canonical path,
        # not by wrapper identity.
        self._lock = _path_lock(self.path)
        self._path_key = os.path.abspath(os.fspath(self.path))

    def read_records(self) -> list[dict[str, Any]]:
        """Return the valid committed prefix, ignoring only a torn final line."""
        if not self.path.exists():
            return []
        records: list[dict[str, Any]] = []
        raw_lines = self.path.read_text(encoding="utf-8").splitlines()
        for index, raw in enumerate(raw_lines):
            if not raw.strip():
                continue
            try:
                record = json.loads(raw)
            except json.JSONDecodeError:
                if index == len(raw_lines) - 1:
                    break
                raise ValueError(
                    f"Corrupt session ledger record {index + 1}: {self.path}"
                )
            if not isinstance(record, dict):
                raise ValueError(f"Invalid session ledger record {index + 1}: {self.path}")
            if record.get("v") != SESSION_LEDGER_VERSION:
                raise ValueError(
                    f"Unsupported session ledger version {record.get('v')!r}: {self.path}"
                )
            expected = len(records)
            if record.get("seq") != expected:
                raise ValueError(
                    f"Non-contiguous session ledger sequence at {index + 1}: {self.path}"
                )
            records.append(record)
        return records

    def append(self, kind: str, data: dict[str, Any]) -> int:
        return self.append_many([(kind, data)])[0]

    def append_many(self, entries: Iterable[tuple[str, dict[str, Any]]]) -> list[int]:
        prepared = list(entries)
        if not prepared:
            return []
        with self._lock:
            cached = _NEXT_SEQUENCES.get(self._path_key)
            try:
                current_stat = self.path.stat()
                current_size = current_stat.st_size
                current_mtime = current_stat.st_mtime_ns
            except OSError:
                current_size = -1
                current_mtime = -1
            if (
                cached is not None
                and cached[1] == current_size
                and cached[2] == current_mtime
            ):
                next_seq = cached[0]
            else:
                records = self.read_records()
                self._truncate_torn_tail()
                next_seq = len(records)
            now = time.time()
            lines: list[str] = []
            seqs: list[int] = []
            for offset, (kind, data) in enumerate(prepared):
                seq = next_seq + offset
                record = {
                    "v": SESSION_LEDGER_VERSION,
                    "seq": seq,
                    "ts": now,
                    "kind": str(kind),
                    "data": data,
                }
                # Validate before opening the file so one bad entry cannot
                # partially commit an otherwise valid batch.
                lines.append(json.dumps(record, ensure_ascii=False, separators=(",", ":")))
                seqs.append(seq)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            try:
                with self.path.open("a", encoding="utf-8", newline="\n") as handle:
                    handle.write("\n".join(lines) + "\n")
                    handle.flush()
                    os.fsync(handle.fileno())
            except Exception:
                # Force a validating read before the next append. This covers
                # an exceptional partial write in the current process; a
                # process restart naturally begins with an empty cache.
                _NEXT_SEQUENCES.pop(self._path_key, None)
                raise
            committed_stat = self.path.stat()
            _NEXT_SEQUENCES[self._path_key] = (
                next_seq + len(lines),
                committed_stat.st_size,
                committed_stat.st_mtime_ns,
            )
            return seqs

    def _truncate_torn_tail(self) -> None:
        """Remove an incomplete final record before the next append.

        ``read_records`` deliberately tolerates a crash-torn last line. If we
        simply appended after it, that harmless tail would become permanent
        middle corruption. Truncating only that invalid suffix makes recovery
        durable while preserving every committed byte.
        """
        if not self.path.exists():
            return
        valid_end = 0
        offset = 0
        raw_lines = self.path.read_bytes().splitlines(keepends=True)
        for index, raw in enumerate(raw_lines):
            offset += len(raw)
            if not raw.strip():
                valid_end = offset
                continue
            try:
                json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                if index != len(raw_lines) - 1:
                    return  # read_records already reports committed corruption
                with self.path.open("r+b") as handle:
                    handle.truncate(valid_end)
                    handle.flush()
                    os.fsync(handle.fileno())
                return
            valid_end = offset

    def seed(
        self,
        conversation_history: list[dict[str, Any]],
        display_events: list[dict[str, Any]],
    ) -> None:
        """Create the ledger from a legacy session if no ledger exists yet."""
        with self._lock:
            if self.path.exists() and self.path.stat().st_size:
                return
            entries: list[tuple[str, dict[str, Any]]] = []
            if conversation_history:
                entries.append(("conversation.reset", {"messages": conversation_history}))
            entries.extend(("display.event", {"event": event}) for event in display_events)
            self.append_many(entries)

    @staticmethod
    def _conversation_entries(
        current: list[dict[str, Any]], messages: list[dict[str, Any]]
    ) -> list[tuple[str, dict[str, Any]]]:
        if current == messages:
            return []
        if len(messages) >= len(current) and messages[: len(current)] == current:
            return [
                ("conversation.message", {"message": message})
                for message in messages[len(current) :]
            ]
        return [("conversation.reset", {"messages": messages})]

    @staticmethod
    def _display_entries(
        current: list[dict[str, Any]], events: list[dict[str, Any]]
    ) -> list[tuple[str, dict[str, Any]]]:
        if current == events:
            return []
        if len(events) >= len(current) and events[: len(current)] == current:
            return [("display.event", {"event": event}) for event in events[len(current) :]]
        # Give every projected event its own sequence number. Earlier builds
        # wrote one display.reset record containing an array, which made every
        # restored event share the same paging cursor.
        return [
            ("display.clear", {}),
            *(("display.event", {"event": event}) for event in events),
        ]

    def sync_conversation(self, messages: list[dict[str, Any]]) -> None:
        """Append a suffix when possible; log a reset after compaction/rewind."""
        self.append_many(self._conversation_entries(self.project_conversation(), messages))

    def sync_display(self, events: list[dict[str, Any]]) -> None:
        """Append a display suffix, or record an explicit reset after a rewind."""
        self.append_many(self._display_entries(self.project_display_events(), events))

    def sync(
        self,
        messages: list[dict[str, Any]],
        events: list[dict[str, Any]],
    ) -> int:
        """Synchronize both projections with one ledger read and one append."""
        with self._lock:
            records = self.read_records()
            entries = self._conversation_entries(
                self.project_conversation(records=records), messages
            )
            entries.extend(
                self._display_entries(self.project_display_events(records=records), events)
            )
            appended = self.append_many(entries)
            return appended[-1] + 1 if appended else len(records)

    def append_display(self, events: Iterable[dict[str, Any]]) -> list[int]:
        return self.append_many(("display.event", {"event": event}) for event in events)

    def project_conversation(
        self, *, records: list[dict[str, Any]] | None = None
    ) -> list[dict[str, Any]]:
        messages: list[dict[str, Any]] = []
        for record in records if records is not None else self.read_records():
            data = record.get("data") or {}
            if record.get("kind") == "conversation.reset":
                messages = [dict(item) for item in data.get("messages") or []]
            elif record.get("kind") == "conversation.message":
                message = data.get("message")
                if isinstance(message, dict):
                    messages.append(dict(message))
        return messages

    def _display_rows(
        self, *, records: list[dict[str, Any]] | None = None
    ) -> list[tuple[int, dict[str, Any]]]:
        rows: list[tuple[int, dict[str, Any]]] = []
        for record in records if records is not None else self.read_records():
            kind = record.get("kind")
            data = record.get("data") or {}
            if kind == "display.clear":
                rows = []
            elif kind == "display.reset":
                # Backward compatibility for the brief pre-release format.
                rows = [
                    (int(record["seq"]), dict(event))
                    for event in data.get("events") or []
                    if isinstance(event, dict)
                ]
            elif kind == "display.event" and isinstance(data.get("event"), dict):
                rows.append((int(record["seq"]), dict(data["event"])))
        return rows

    def project_display_events(
        self, *, records: list[dict[str, Any]] | None = None
    ) -> list[dict[str, Any]]:
        return [event for _, event in self._display_rows(records=records)]

    def display_page(
        self,
        *,
        before_seq: int | None = None,
        limit: int = DEFAULT_DISPLAY_PAGE_SIZE,
        records: list[dict[str, Any]] | None = None,
    ) -> DisplayEventPage:
        """Return a tail page aligned to a user-task boundary when practical."""
        committed = records if records is not None else self.read_records()
        size = max(1, min(int(limit), MAX_DISPLAY_PAGE_SIZE))
        all_rows = self._display_rows(records=committed)
        eligible = [row for row in all_rows if before_seq is None or row[0] < before_seq]
        if not eligible:
            as_of = len(committed) - 1
            return DisplayEventPage([], None, None, False, len(all_rows), as_of)

        start_index = max(0, len(eligible) - size)
        # Prefer a clean task boundary inside the requested window. The limit
        # remains hard: an unusually large task must not defeat bounded DOM.
        if start_index > 0:
            for index in range(start_index, len(eligible)):
                if eligible[index][1].get("event") == "user_message":
                    start_index = index
                    break

        page_rows = eligible[start_index:]
        events: list[dict[str, Any]] = []
        for seq, event in page_rows:
            projected = dict(event)
            projected["_ledger_seq"] = seq
            events.append(projected)
        as_of = len(committed) - 1
        return DisplayEventPage(
            events=events,
            start_seq=page_rows[0][0],
            end_seq=page_rows[-1][0],
            has_more=start_index > 0,
            total_events=len(all_rows),
            as_of_seq=as_of,
        )

    def projections(
        self, *, records: list[dict[str, Any]] | None = None
    ) -> dict[str, Any]:
        """Build small whole-session read models from the committed ledger."""
        committed = records if records is not None else self.read_records()
        rows = self._display_rows(records=committed)
        turns = steps = tools = 0
        changed_files: list[str] = []
        seen_files: set[str] = set()
        last_outcome = ""
        for _, event in rows:
            event_type = event.get("event")
            if event_type == "user_message":
                turns += 1
            elif event_type == "step.end":
                steps += 1
            elif event_type == "tool.call":
                tools += 1
                presentation = event.get("presentation") or {}
                if presentation.get("kind") in {"edit", "diff", "write"}:
                    for location in presentation.get("locations") or []:
                        path = str(location).strip()
                        if path and path not in seen_files:
                            seen_files.add(path)
                            changed_files.append(path)
            elif event_type == "session.end":
                last_outcome = str(event.get("outcome") or last_outcome)
                for path in (event.get("evidence") or {}).get("changed_files") or []:
                    value = str(path).strip()
                    if value and value not in seen_files:
                        seen_files.add(value)
                        changed_files.append(value)
        return {
            "as_of_seq": len(committed) - 1,
            "stats": {"turns": turns, "steps": steps, "tools": tools},
            "deliverables": {"changed_files": changed_files},
            "outcome": {"last": last_outcome},
        }

    def history_snapshot(
        self,
        *,
        before_seq: int | None = None,
        limit: int = DEFAULT_DISPLAY_PAGE_SIZE,
        records: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Return paging and summary projections from one committed snapshot."""
        records = self.read_records() if records is None else records
        return {
            "page": self.display_page(
                before_seq=before_seq, limit=limit, records=records
            ).to_dict(),
            "projections": self.projections(records=records),
        }
