"""Durability and paging contracts for the versioned session event ledger."""

from __future__ import annotations

import json

import pytest

from resonant_client.gui import sessions as sessions_mod
from resonant_client.gui.session_ledger import SessionEventLedger
from resonant_client.gui.sessions import ProjectManager, SessionRecord


def _task(index: int) -> list[dict]:
    return [
        {"event": "user_message", "text": f"request {index}"},
        {"event": "step.end", "step": index},
        {
            "event": "tool.call",
            "name": "file_edit",
            "presentation": {
                "kind": "edit",
                "locations": [f"src/file_{index}.py"],
            },
        },
        {"event": "session.end", "outcome": "changed_verified"},
    ]


def test_ledger_appends_and_projects_both_runtime_views(tmp_path):
    ledger = SessionEventLedger(tmp_path / "session.events.jsonl")
    ledger.seed(
        [{"role": "user", "content": "hello"}],
        [{"event": "user_message", "text": "hello"}],
    )
    ledger.sync_conversation([
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "hi"},
    ])
    ledger.append_display([{"event": "text.done", "text": "hi"}])

    assert ledger.project_conversation()[-1]["content"] == "hi"
    assert ledger.project_display_events()[-1]["event"] == "text.done"
    assert [record["seq"] for record in ledger.read_records()] == list(
        range(len(ledger.read_records()))
    )


def test_rewind_uses_clear_and_unique_event_sequences(tmp_path):
    ledger = SessionEventLedger(tmp_path / "session.events.jsonl")
    ledger.append_display(_task(0) + _task(1))
    restored = _task(0)[:2]

    ledger.sync_display(restored)

    page = ledger.display_page(limit=20)
    assert ledger.project_display_events() == restored
    assert [event["_ledger_seq"] for event in page.events] == sorted(
        {event["_ledger_seq"] for event in page.events}
    )
    assert any(record["kind"] == "display.clear" for record in ledger.read_records())


def test_torn_final_write_is_ignored_but_middle_corruption_is_rejected(tmp_path):
    path = tmp_path / "session.events.jsonl"
    ledger = SessionEventLedger(path)
    ledger.append("display.event", {"event": {"event": "user_message"}})
    with path.open("a", encoding="utf-8") as handle:
        handle.write('{"v":1,"seq":1')

    assert len(ledger.read_records()) == 1

    path.write_text(
        path.read_text(encoding="utf-8")
        + "\nnot-json\n"
        + '{"v":1,"seq":1,"ts":0,"kind":"display.clear","data":{}}\n',
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="Corrupt session ledger"):
        ledger.read_records()


def test_append_repairs_a_torn_final_write_before_committing(tmp_path):
    path = tmp_path / "session.events.jsonl"
    ledger = SessionEventLedger(path)
    ledger.append("display.event", {"event": {"event": "user_message"}})
    with path.open("a", encoding="utf-8") as handle:
        handle.write('{"v":1,"seq":1')

    ledger.append("display.clear", {})

    records = ledger.read_records()
    assert [record["seq"] for record in records] == [0, 1]
    assert records[-1]["kind"] == "display.clear"


def test_multiple_ledger_wrappers_share_the_next_sequence(tmp_path):
    path = tmp_path / "session.events.jsonl"
    first = SessionEventLedger(path)
    second = SessionEventLedger(path)

    assert first.append("display.clear", {}) == 0
    assert second.append("display.clear", {}) == 1
    assert [record["seq"] for record in first.read_records()] == [0, 1]


def test_tail_pages_start_at_user_task_boundaries(tmp_path):
    ledger = SessionEventLedger(tmp_path / "session.events.jsonl")
    ledger.append_display(_task(0) + _task(1) + _task(2))

    latest = ledger.display_page(limit=5)
    earlier = ledger.display_page(before_seq=latest.start_seq, limit=5)

    assert latest.events[0]["text"] == "request 2"
    assert earlier.events[0]["text"] == "request 1"
    assert latest.has_more is True
    assert earlier.has_more is True
    assert latest.total_events == 12


def test_a_single_large_task_never_exceeds_the_requested_page_size(tmp_path):
    ledger = SessionEventLedger(tmp_path / "session.events.jsonl")
    events = [{"event": "user_message", "text": "large task"}]
    events.extend({"event": "tool.result", "index": index} for index in range(20))
    ledger.append_display(events)

    page = ledger.display_page(limit=5)

    assert len(page.events) == 5


def test_projection_summarizes_stats_and_changed_files(tmp_path):
    ledger = SessionEventLedger(tmp_path / "session.events.jsonl")
    ledger.append_display(_task(0) + _task(1))

    projection = ledger.projections()

    assert projection["stats"] == {"turns": 2, "steps": 2, "tools": 2}
    assert projection["deliverables"]["changed_files"] == [
        "src/file_0.py",
        "src/file_1.py",
    ]
    assert projection["outcome"]["last"] == "changed_verified"


def test_history_snapshot_parses_the_ledger_once(tmp_path, monkeypatch):
    ledger = SessionEventLedger(tmp_path / "session.events.jsonl")
    ledger.append_display(_task(0) + _task(1))
    original_read = ledger.read_records
    calls = 0

    def counted_read():
        nonlocal calls
        calls += 1
        return original_read()

    monkeypatch.setattr(ledger, "read_records", counted_read)
    snapshot = ledger.history_snapshot(limit=5)

    assert calls == 1
    assert snapshot["page"]["events"]
    assert snapshot["projections"]["stats"]["turns"] == 2


def test_hydrated_history_snapshot_restores_engine_history_with_one_parse(
    isolated_home, monkeypatch
):
    project = str(isolated_home / "hydrated-snapshot-project")
    manager = ProjectManager(project)
    original = manager.create_session()
    original.conversation_history = [{"role": "user", "content": "hello"}]
    original.display_events = _task(0)
    original.save()

    loaded = manager.load_session(original.id, hydrate=False)
    assert loaded is not None
    original_read = SessionEventLedger.read_records
    calls = 0

    def counted_read(ledger):
        nonlocal calls
        calls += 1
        return original_read(ledger)

    monkeypatch.setattr(SessionEventLedger, "read_records", counted_read)
    snapshot = loaded.history_snapshot(hydrate=True)

    assert calls == 1
    assert loaded.conversation_history == original.conversation_history
    assert loaded.display_events == original.display_events
    assert snapshot["page"]["events"]


@pytest.fixture
def isolated_home(tmp_path, monkeypatch):
    monkeypatch.setattr(sessions_mod.Path, "home", staticmethod(lambda: tmp_path))
    return tmp_path


def test_session_metadata_stays_small_and_round_trips_through_ledger(isolated_home):
    project = str(isolated_home / "project")
    record = SessionRecord(
        session_id="ledger-roundtrip",
        project_path=project,
        conversation_history=[{"role": "user", "content": "hello"}],
        display_events=_task(0),
        message_count=1,
    )
    record.save()

    metadata_path = sessions_mod._sessions_dir(project) / "ledger-roundtrip.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    assert "conversation_history" not in metadata
    assert "display_events" not in metadata
    assert metadata["event_log"] == "ledger-roundtrip.events.jsonl"

    loaded = ProjectManager(project).load_session(record.id)
    assert loaded is not None
    assert loaded.conversation_history == record.conversation_history
    assert loaded.display_events == record.display_events


def test_metadata_only_load_skips_full_ledger_hydration(isolated_home, monkeypatch):
    project = str(isolated_home / "metadata-project")
    manager = ProjectManager(project)
    record = manager.create_session()
    record.append_display_events(_task(0))
    record.save()

    def fail_hydration(self):
        raise AssertionError("history paging should not hydrate compatibility arrays")

    monkeypatch.setattr(SessionRecord, "load_ledger", fail_hydration)
    loaded = manager.load_session(record.id, activate=False, hydrate=False)

    assert loaded is not None
    assert loaded.display_events == []
    assert loaded.history_snapshot()["page"]["events"]


def test_end_of_turn_compatibility_save_does_not_duplicate_streamed_events(isolated_home):
    project = str(isolated_home / "stream-project")
    manager = ProjectManager(project)
    record = manager.create_session()
    batch = _task(0)
    for event in batch:
        record.append_display_events([event])

    manager.save_current_session(display_events=batch)

    assert record.ledger.project_display_events() == batch


def test_legacy_session_is_migrated_on_first_load(isolated_home):
    project = str(isolated_home / "legacy-project")
    manager = ProjectManager(project)
    metadata_path = sessions_mod._sessions_dir(project) / "legacy.json"
    metadata_path.write_text(
        json.dumps({
            "id": "legacy",
            "project_path": project,
            "conversation_history": [{"role": "user", "content": "old"}],
            "display_events": [{"event": "user_message", "text": "old"}],
        }),
        encoding="utf-8",
    )

    loaded = manager.load_session("legacy")

    assert loaded is not None
    assert loaded.ledger.path.exists()
    migrated = json.loads(metadata_path.read_text(encoding="utf-8"))
    assert "conversation_history" not in migrated
    assert loaded.display_events[0]["text"] == "old"


def test_session_storage_rejects_traversal_ids_and_event_logs(isolated_home):
    project = str(isolated_home / "safe-project")
    manager = ProjectManager(project)

    assert manager.load_session("../outside") is None
    with pytest.raises(ValueError, match="Invalid session id"):
        SessionRecord(session_id="../outside", project_path=project)

    record = SessionRecord.from_dict({
        "id": "safe",
        "project_path": str(isolated_home / "wrong-project"),
        "event_log": "../../outside.events.jsonl",
    })
    assert record.event_log == "safe.events.jsonl"


def test_fork_slices_display_history_at_the_exact_next_user_boundary(isolated_home):
    project = str(isolated_home / "fork-project")
    manager = ProjectManager(project)
    record = manager.create_session()
    record.conversation_history = [
        {"role": "user", "content": "one"},
        {"role": "assistant", "content": "done one"},
        {"role": "user", "content": "two"},
        {"role": "assistant", "content": "done two"},
    ]
    record.display_events = _task(0) + _task(1)
    record.save()

    forked = manager.fork_session(record.id, 0)

    assert forked is not None
    assert forked.display_events == _task(0)
