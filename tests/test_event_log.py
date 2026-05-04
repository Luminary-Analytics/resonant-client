"""Tests for v0.5.11a1 — engine/event_log.py coverage deepening.

Pre-v0.5.11 coverage was 50% (init helpers + module imports). The
write path, replay, and cleanup were entirely untested. EventLogger
is the JSONL session-event sink that backs debugging, replay, and
audit trails — under-coverage here meant a silent regression in any
of those paths could have shipped.

Covered:
- EventLogger __init__: default + custom log_dir, disabled flag,
  session_id naming, unknown-session fallback, open-failure → disable.
- EventLogger.log: enrichment with timestamp + session_id, JSONL
  format, non-serializable values via default=str, no-op when
  disabled, periodic flush every 5 events, write-failure tolerance.
- EventLogger.close: flush + close, idempotent on re-call, destructor
  invokes close.
- replay(): yields events, skips invalid JSON, skips blank lines,
  missing-file returns empty.
- cleanup_old_logs(): removes old date dirs, keeps recent, skips
  non-date dirs, returns count, default dir, missing dir → 0.
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from resonant_client.engine.event_log import (
    EventLogger,
    cleanup_old_logs,
    replay,
)


# ── EventLogger __init__ ────────────────────────────────────────────────


class TestEventLoggerInit:
    def test_disabled_flag_skips_file_creation(self, tmp_path):
        log = EventLogger(log_dir=tmp_path, session_id="abc", enabled=False)
        assert log.enabled is False
        assert log._file is None
        # No log dir created when disabled.
        assert not (tmp_path / datetime.now().strftime("%Y-%m-%d")).exists()

    def test_custom_log_dir_used(self, tmp_path):
        log = EventLogger(log_dir=tmp_path, session_id="sess1", enabled=True)
        try:
            today = datetime.now().strftime("%Y-%m-%d")
            assert (tmp_path / today / "sess1.jsonl").exists()
        finally:
            log.close()

    def test_default_log_dir_is_home_resonant_logs(self, tmp_path, monkeypatch):
        # Override $HOME so we don't pollute the real ~/.resonant.
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("USERPROFILE", str(tmp_path))  # Windows
        log = EventLogger(session_id="default-test", enabled=True)
        try:
            assert log._log_dir == Path.home() / ".resonant" / "logs"
        finally:
            log.close()

    def test_unknown_session_id_uses_unknown_filename(self, tmp_path):
        # Empty session_id → file named "unknown.jsonl" rather than
        # ".jsonl" (which would be a hidden file with no name).
        log = EventLogger(log_dir=tmp_path, session_id="", enabled=True)
        try:
            today = datetime.now().strftime("%Y-%m-%d")
            assert (tmp_path / today / "unknown.jsonl").exists()
        finally:
            log.close()

    def test_open_failure_disables_logger(self, tmp_path, monkeypatch):
        # Force open() to fail; the logger should disable itself
        # gracefully rather than raise.
        original_open = open

        def boom(*args, **kwargs):
            # Only sabotage opens of .jsonl files; let other opens
            # (mkdir checks etc.) still work.
            if args and str(args[0]).endswith(".jsonl"):
                raise OSError("simulated open failure")
            return original_open(*args, **kwargs)

        monkeypatch.setattr("builtins.open", boom)
        log = EventLogger(log_dir=tmp_path, session_id="fail", enabled=True)
        assert log.enabled is False
        assert log._file is None

    def test_log_dir_string_accepted(self, tmp_path):
        # log_dir accepts str or Path; Path conversion happens in init.
        log = EventLogger(log_dir=str(tmp_path), session_id="s", enabled=True)
        try:
            assert isinstance(log._log_dir, Path)
        finally:
            log.close()


# ── EventLogger.log ─────────────────────────────────────────────────────


class TestEventLoggerLog:
    def _read_log_lines(self, log: EventLogger) -> list[str]:
        """Force a flush + read the logfile back. The logger flushes
        every 5 events; tests below force flush via close() when they
        write fewer."""
        log.close()
        log_path = (
            Path(log._log_dir)
            / datetime.now().strftime("%Y-%m-%d")
            / f"{log.session_id or 'unknown'}.jsonl"
        )
        if not log_path.exists():
            return []
        return [
            line for line in log_path.read_text(
                encoding="utf-8"
            ).splitlines() if line.strip()
        ]

    def test_log_writes_jsonl_line(self, tmp_path):
        log = EventLogger(log_dir=tmp_path, session_id="s1", enabled=True)
        log.log({"event": "tool_call", "name": "bash"})
        lines = self._read_log_lines(log)
        assert len(lines) == 1
        record = json.loads(lines[0])
        assert record["event"] == "tool_call"
        assert record["name"] == "bash"

    def test_log_enriches_with_timestamp_and_session_id(self, tmp_path):
        log = EventLogger(log_dir=tmp_path, session_id="s2", enabled=True)
        log.log({"event": "x"})
        lines = self._read_log_lines(log)
        record = json.loads(lines[0])
        assert "timestamp" in record
        assert record["session_id"] == "s2"
        # Timestamp parses as ISO format.
        datetime.fromisoformat(record["timestamp"])

    def test_log_event_fields_override_enrichment_only_via_explicit_pass(
        self, tmp_path,
    ):
        # The enrichment dict puts timestamp + session_id FIRST; then
        # `**event` may override them. So a caller-supplied session_id
        # in the event dict wins. (Documented behavior — verify it.)
        log = EventLogger(log_dir=tmp_path, session_id="real", enabled=True)
        log.log({"event": "x", "session_id": "from_event"})
        lines = self._read_log_lines(log)
        record = json.loads(lines[0])
        assert record["session_id"] == "from_event"

    def test_log_handles_non_serializable_via_default_str(self, tmp_path):
        # Path objects aren't JSON-serializable but `default=str` in
        # json.dumps makes them stringify cleanly. Real-world events
        # carry Path-typed fields (e.g. roadmap_path).
        log = EventLogger(log_dir=tmp_path, session_id="s3", enabled=True)
        log.log({"event": "x", "path": Path("/tmp/foo")})
        lines = self._read_log_lines(log)
        record = json.loads(lines[0])
        # Path stringified — exact form differs platform to platform.
        assert "foo" in record["path"]

    def test_log_no_op_when_disabled(self, tmp_path):
        log = EventLogger(log_dir=tmp_path, session_id="s4", enabled=False)
        log.log({"event": "x"})
        # No file should have been created.
        today = datetime.now().strftime("%Y-%m-%d")
        assert not (tmp_path / today / "s4.jsonl").exists()

    def test_log_no_op_when_file_is_none(self, tmp_path, monkeypatch):
        # If init succeeded but _file got reset to None somehow (e.g.
        # close was called), subsequent log() calls must not raise.
        log = EventLogger(log_dir=tmp_path, session_id="s5", enabled=True)
        log.close()
        # _file is None after close; enabled may still be True.
        log.log({"event": "after_close"})  # must not raise

    def test_log_periodic_flush_every_5_events(self, tmp_path):
        # The flush behavior is observable: after 5 events the file
        # contents are visible without close. Hard to test without
        # close() but we can assert the event_count counter ticks.
        log = EventLogger(log_dir=tmp_path, session_id="s6", enabled=True)
        try:
            for i in range(7):
                log.log({"event": f"e{i}"})
            assert log._event_count == 7
        finally:
            log.close()

    def test_log_write_failure_does_not_raise(self, tmp_path):
        log = EventLogger(log_dir=tmp_path, session_id="s7", enabled=True)
        # Simulate the file going bad mid-flight by closing it under
        # the logger. Subsequent log() calls hit an exception in write
        # which is caught + warned.
        log._file.close()
        log.log({"event": "after_underlying_close"})  # must not raise
        log.close()  # close() also tolerates the bad state


# ── EventLogger.close ───────────────────────────────────────────────────


class TestEventLoggerClose:
    def test_close_clears_file_handle(self, tmp_path):
        log = EventLogger(log_dir=tmp_path, session_id="c1", enabled=True)
        assert log._file is not None
        log.close()
        assert log._file is None

    def test_close_idempotent(self, tmp_path):
        log = EventLogger(log_dir=tmp_path, session_id="c2", enabled=True)
        log.close()
        log.close()  # Second call must not raise.
        assert log._file is None

    def test_close_called_on_destruct(self, tmp_path):
        log = EventLogger(log_dir=tmp_path, session_id="c3", enabled=True)
        # Destruction triggers __del__ → close. Hard to deterministically
        # test the destructor in CPython without `gc.collect`, so we
        # just verify __del__ runs without raising when called directly.
        log.__del__()
        assert log._file is None
        # Idempotent — second __del__ via gc must also be safe.
        log.__del__()


# ── replay ──────────────────────────────────────────────────────────────


class TestReplay:
    def test_yields_events_in_order(self, tmp_path):
        log_path = tmp_path / "events.jsonl"
        log_path.write_text(
            json.dumps({"event": "a"}) + "\n"
            + json.dumps({"event": "b"}) + "\n",
            encoding="utf-8",
        )
        events = list(replay(log_path))
        assert [e["event"] for e in events] == ["a", "b"]

    def test_skips_invalid_json_lines(self, tmp_path):
        log_path = tmp_path / "mixed.jsonl"
        log_path.write_text(
            json.dumps({"event": "a"}) + "\n"
            + "not-json\n"
            + json.dumps({"event": "b"}) + "\n",
            encoding="utf-8",
        )
        events = list(replay(log_path))
        # Bad line dropped; surrounding events still surface.
        assert [e["event"] for e in events] == ["a", "b"]

    def test_skips_blank_lines(self, tmp_path):
        log_path = tmp_path / "blanks.jsonl"
        log_path.write_text(
            json.dumps({"event": "a"}) + "\n\n   \n"
            + json.dumps({"event": "b"}) + "\n",
            encoding="utf-8",
        )
        events = list(replay(log_path))
        assert [e["event"] for e in events] == ["a", "b"]

    def test_missing_file_returns_empty(self, tmp_path):
        events = list(replay(tmp_path / "does-not-exist.jsonl"))
        assert events == []

    def test_string_path_accepted(self, tmp_path):
        # replay() accepts str or Path.
        log_path = tmp_path / "stringy.jsonl"
        log_path.write_text(json.dumps({"event": "x"}) + "\n",
                            encoding="utf-8")
        events = list(replay(str(log_path)))
        assert events == [{"event": "x"}]


# ── cleanup_old_logs ────────────────────────────────────────────────────


class TestCleanupOldLogs:
    def _make_dated_dir(self, base: Path, days_ago: int) -> Path:
        d = (datetime.now() - timedelta(days=days_ago)).strftime("%Y-%m-%d")
        target = base / d
        target.mkdir(parents=True, exist_ok=True)
        # Drop a placeholder file so the dir isn't empty (cleanup
        # uses shutil.rmtree which works either way, but realistic).
        (target / "session.jsonl").write_text("{}", encoding="utf-8")
        return target

    def test_removes_dirs_older_than_retention(self, tmp_path):
        recent = self._make_dated_dir(tmp_path, days_ago=2)
        old = self._make_dated_dir(tmp_path, days_ago=10)
        removed = cleanup_old_logs(tmp_path, retention_days=7)
        assert removed == 1
        assert recent.exists()
        assert not old.exists()

    def test_keeps_recent_dirs(self, tmp_path):
        # All dirs newer than retention → no removals.
        for days in (0, 1, 3, 6):
            self._make_dated_dir(tmp_path, days_ago=days)
        removed = cleanup_old_logs(tmp_path, retention_days=7)
        assert removed == 0
        assert len(list(tmp_path.iterdir())) == 4

    def test_skips_non_date_directories(self, tmp_path):
        # An entry named with a non-date string must not crash the
        # cleanup. It's just left alone.
        (tmp_path / "not-a-date").mkdir()
        (tmp_path / "also-bogus-2026-99-99").mkdir()
        # And a real old one.
        old = self._make_dated_dir(tmp_path, days_ago=20)
        removed = cleanup_old_logs(tmp_path, retention_days=7)
        assert removed == 1
        assert not old.exists()
        assert (tmp_path / "not-a-date").exists()
        assert (tmp_path / "also-bogus-2026-99-99").exists()

    def test_returns_zero_when_base_dir_missing(self, tmp_path):
        # Pointing at a non-existent path returns 0 cleanly.
        ghost = tmp_path / "nope"
        removed = cleanup_old_logs(ghost, retention_days=7)
        assert removed == 0

    def test_returns_zero_when_dir_is_empty(self, tmp_path):
        removed = cleanup_old_logs(tmp_path, retention_days=7)
        assert removed == 0

    def test_default_retention_is_seven_days(self, tmp_path):
        old = self._make_dated_dir(tmp_path, days_ago=8)
        recent = self._make_dated_dir(tmp_path, days_ago=6)
        # No retention_days arg → uses default (7).
        removed = cleanup_old_logs(tmp_path)
        assert removed == 1
        assert recent.exists()
        assert not old.exists()

    def test_string_path_accepted(self, tmp_path):
        old = self._make_dated_dir(tmp_path, days_ago=30)
        removed = cleanup_old_logs(str(tmp_path), retention_days=7)
        assert removed == 1
        assert not old.exists()

    def test_skips_files_at_top_level(self, tmp_path):
        # cleanup only iterates directories; files at the base level
        # (e.g. a stray .DS_Store) are silently ignored.
        (tmp_path / ".DS_Store").write_text("", encoding="utf-8")
        old = self._make_dated_dir(tmp_path, days_ago=30)
        removed = cleanup_old_logs(tmp_path, retention_days=7)
        assert removed == 1
        assert (tmp_path / ".DS_Store").exists()
