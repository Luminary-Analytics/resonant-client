"""Transcript retention: expired sessions, stores with conversation content
and dated logs are deleted; the open session and recent activity are kept."""

from __future__ import annotations

import hashlib
import os
import time
from datetime import date, timedelta

from lumi.gui.retention import DAY, purge_expired

NOW = time.time()


def _age(path, days):
    stamp = NOW - days * DAY
    os.utime(path, (stamp, stamp))


def _session(state, project, session_id, age_days, *, ledger_age=None):
    folder = state / "projects" / project / "sessions"
    folder.mkdir(parents=True, exist_ok=True)
    record = folder / f"{session_id}.json"
    ledger = folder / f"{session_id}.events.jsonl"
    record.write_text("{}", encoding="utf-8")
    ledger.write_text("", encoding="utf-8")
    _age(record, age_days)
    _age(ledger, age_days if ledger_age is None else ledger_age)
    return record, ledger


def _file(path, age_days, text="x"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    _age(path, age_days)
    return path


def _log_day(state, days_ago):
    folder = state / "logs" / (date.fromtimestamp(NOW) - timedelta(days=days_ago)).isoformat()
    folder.mkdir(parents=True)
    (folder / "s.jsonl").write_text("{}", encoding="utf-8")
    return folder


def test_zero_days_keeps_everything(tmp_path):
    record, _ = _session(tmp_path, "p1", "old", 400)
    assert not any(purge_expired(0, tmp_path, now=NOW).values())
    assert record.exists()


def test_expired_sessions_and_logs_are_deleted(tmp_path):
    old, old_ledger = _session(tmp_path, "p1", "old", 40)
    recent, _ = _session(tmp_path, "p1", "recent", 5)
    other, _ = _session(tmp_path, "p2", "other-old", 31)
    old_day, recent_day = _log_day(tmp_path, 45), _log_day(tmp_path, 2)
    startup = _file(tmp_path / "logs" / "lumi-startup.log", 90, "keep")

    removed = purge_expired(30, tmp_path, now=NOW)
    assert (removed["sessions"], removed["logs"]) == (2, 1)
    assert not old.exists() and not old_ledger.exists() and not other.exists()
    assert recent.exists() and recent_day.exists() and not old_day.exists()
    assert startup.exists()


def test_recent_ledger_activity_keeps_a_session(tmp_path):
    record, _ = _session(tmp_path, "p1", "active-ledger", 90, ledger_age=1)
    assert purge_expired(30, tmp_path, now=NOW)["sessions"] == 0
    assert record.exists()


def test_the_open_session_and_its_draft_are_never_deleted(tmp_path):
    record, _ = _session(tmp_path, "p1", "open-one", 400)
    draft_name = hashlib.sha256(b"open-one").hexdigest()[:32] + ".json"
    draft = _file(tmp_path / "projects" / "p1" / "drafts" / draft_name, 400)
    other_draft = _file(tmp_path / "projects" / "p1" / "drafts" / "someone-else.json", 400)
    removed = purge_expired(7, tmp_path, keep_session_ids=["open-one"], now=NOW)
    assert (removed["sessions"], removed["drafts"]) == (0, 1)
    assert record.exists() and draft.exists() and not other_draft.exists()


def test_other_stores_with_conversation_content(tmp_path):
    project = tmp_path / "projects" / "abcd"
    old_checkpoint = _file(project / "checkpoints" / "run-old" / "cp_1.conversation.json", 60)
    _age(old_checkpoint.parent, 60)
    live_checkpoint = project / "checkpoints" / "run-live"
    _file(live_checkpoint / "cp_1.conversation.json", 60)
    _file(live_checkpoint / "timeline.jsonl", 0)  # a run still writing keeps its folder
    _age(live_checkpoint, 60)
    agent = _file(project / "agents" / "agt_1.json", 60)
    artifact = _file(project / "artifacts" / "image.png", 60)
    manifest = _file(project / "artifacts" / "manifest.jsonl", 60)
    recording = _file(tmp_path / "recordings" / "demo.mp4", 60)
    fresh_recording = _file(tmp_path / "recordings" / "today.mp4", 0)

    removed = purge_expired(30, tmp_path, now=NOW)
    assert (removed["checkpoints"], removed["agents"], removed["artifacts"], removed["recordings"]) == (1, 1, 1, 1)
    assert not old_checkpoint.parent.exists() and live_checkpoint.exists()
    assert not agent.exists() and not artifact.exists() and manifest.exists()
    assert not recording.exists() and fresh_recording.exists()
