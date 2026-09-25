"""Turn outcomes on this computer, counts only (lumi/activity.py)."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from lumi import activity


@pytest.fixture(autouse=True)
def activity_file(tmp_path):
    path = tmp_path / "activity.jsonl"
    activity.set_path_for_tests(path)
    yield path
    activity.set_path_for_tests(None)


def _result(name: str, *, error: bool = False, denied: bool = False) -> dict:
    return {"event": "tool.result", "name": name, "is_error": error, "denied": denied, "output": "x"}


def test_a_turns_outcome():
    done = {"event": "text.done", "text": "All tests pass."}
    assert activity.classify([_result("file_edit"), _result("check_run"), done]) == {
        "outcome": "completed", "verified": True, "files_changed": 1}
    # A failed or denied check doesn't verify anything; failed edits change nothing.
    assert activity.classify([_result("check_run", error=True), _result("file_write", error=True), done]) == {
        "outcome": "completed", "verified": False, "files_changed": 0}
    assert activity.classify([_result("check_run", denied=True), done])["verified"] is False
    assert activity.classify([_result("check_run"), {"event": "error", "message": "x"}]) == {
        "outcome": "error", "verified": False, "files_changed": 0}
    assert activity.classify([done], cancelled=True)["outcome"] == "cancelled"
    assert activity.classify([_result("glob")])["outcome"] == "cancelled"  # nothing finished


def test_counts_for_a_period_and_nothing_else_is_kept(activity_file):
    start = datetime(2026, 9, 1, 12, tzinfo=timezone.utc)
    activity.record_turn([_result("check_run"), {"event": "text.done", "text": "the secret answer"}], now=start)
    activity.record_turn([{"event": "error", "message": "boom"}], now=start + timedelta(minutes=5))
    activity.record_crash(now=start + timedelta(minutes=10))
    activity.record_turn([{"event": "text.done"}], now=start + timedelta(hours=2))
    assert "secret" not in activity_file.read_text(encoding="utf-8")
    first_hour = activity.summary("2026-09-01T12:00:00Z", "2026-09-01T13:00:00Z")
    assert first_hour == {"turns": 2, "completed": 1, "errors": 1, "cancelled": 0, "verified": 1,
                          "files_changed": 0, "crashes": 1}
    # Half-open: a turn at the end of one period belongs to the next.
    assert activity.summary("2026-09-01T12:00:00Z", "2026-09-01T12:05:00Z")["turns"] == 1


def test_old_lines_are_dropped(activity_file):
    now = datetime.now(timezone.utc)
    lines = [{"t": (now - timedelta(days=200)).strftime("%Y-%m-%dT%H:%M:%SZ"), "kind": "turn",
              "outcome": "completed"} for _ in range(5)]
    activity_file.write_text("".join(json.dumps(line) + "\n" for line in lines) + "not json\n", encoding="utf-8")
    activity.record_turn([{"event": "text.done"}])
    assert activity.month_summary()["turns"] == 1
    assert len(activity_file.read_text(encoding="utf-8").splitlines()) == 1  # pruned


def test_crashes_are_counted_then_handled_as_before(monkeypatch):
    seen = []
    monkeypatch.setattr(activity.sys, "excepthook", lambda *args: seen.append(args[0]))
    monkeypatch.setattr(activity.threading, "excepthook", lambda args: seen.append(args.exc_type))
    activity.install_crash_counter()
    activity.sys.excepthook(ValueError, ValueError("x"), None)
    activity.sys.excepthook(KeyboardInterrupt, KeyboardInterrupt(), None)  # not a crash
    assert seen == [ValueError, KeyboardInterrupt]
    assert activity.month_summary()["crashes"] == 1
