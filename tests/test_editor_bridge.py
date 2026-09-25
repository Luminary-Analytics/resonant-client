"""The editor bridge (gui/editor_bridge.py): what VS Code and JetBrains may send and see."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from lumi.engine.checkpoint_timeline import SessionCheckpointStore
from lumi.engine.context_broker import ContextBroker
from lumi.engine.exclusions import ExclusionRules
from lumi.gui import editor_bridge
from lumi.gui.editor_bridge import (
    BridgeError,
    EditorBridge,
    attach,
    before_text,
    latest_changing_turn,
    read_bridge,
    turn_changes,
)


def _git(folder: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(folder), *args], check=True, capture_output=True, text=True).stdout


def _repository(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    (project / "src").mkdir(parents=True)
    (project / "src" / "app.py").write_bytes(b"one\ntwo\nthree\n")
    (project / "notes.txt").write_bytes(b"keep\n")
    (project / "old_name.txt").write_bytes(b"same content for the rename check\n" * 5)
    (project / ".env").write_bytes(b"TOKEN=before\n")
    _git(project, "init", "-q")
    _git(project, "config", "core.autocrlf", "false")
    _git(project, "add", ".")
    _git(project, "-c", "user.email=t@example.com", "-c", "user.name=T", "commit", "-q", "-m", "start")
    return project


def _exclusions(project: Path) -> ExclusionRules:
    return ExclusionRules(str(project), [(".env", "Settings")])


def _turn(checkpoint: str = "", changed: tuple[str, ...] = (), prompt: str = "Fix the bug") -> list[dict]:
    events: list[dict] = [{"event": "user_message", "text": prompt}]
    if checkpoint:
        events.append({"event": "checkpoint.created", "checkpoint_id": checkpoint})
    events.append({"event": "session.end", "evidence": {"changed_files": list(changed)}})
    return events


def test_the_bridge_file_belongs_to_this_launch(monkeypatch):
    first = EditorBridge()
    path = first.publish("http://127.0.0.1:8123/")
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data == {"version": 1, "url": "http://127.0.0.1:8123", "token": first._token, "pid": os.getpid()}
    assert read_bridge() == data
    if sys.platform != "win32":
        assert path.stat().st_mode & 0o777 == 0o600
    first.withdraw()
    assert not path.exists() and read_bridge() is None

    # A later launch's file is left alone.
    first.publish("http://127.0.0.1:8123")
    later = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        path.write_text(json.dumps({**data, "pid": later.pid}), encoding="utf-8")
        first.withdraw()
        assert path.exists() and read_bridge()["pid"] == later.pid
    finally:
        later.kill()
        later.wait()
    # Once that launch is gone, its leftover file doesn't count.
    assert read_bridge() is None
    path.unlink()

    # Turned off and on again from Settings.
    second = EditorBridge()
    second.start("http://127.0.0.1:9000", enabled=False)
    assert not path.exists()
    second.sync(True)
    assert read_bridge()["url"] == "http://127.0.0.1:9000"
    second.sync(False)
    assert not path.exists()


def test_only_the_bridge_token_from_outside_a_browser_is_accepted():
    bridge = EditorBridge()
    token = bridge._token
    assert bridge.authorized({"authorization": f"Bearer {token}"})
    assert bridge.authorized({"authorization": f"bearer  {token} "})
    assert not bridge.authorized({})
    assert not bridge.authorized({"authorization": "Bearer wrong"})
    assert not bridge.authorized({"authorization": token})
    # A web page sends Origin; it is refused even with the token.
    assert not bridge.authorized({"authorization": f"Bearer {token}", "origin": "http://127.0.0.1:8123"})
    assert not EditorBridge().authorized({"authorization": f"Bearer {token}"})  # another launch's token


def test_editor_files_become_file_attachments(tmp_path):
    project = _repository(tmp_path)
    (project / "src" / "with space.py").write_bytes(b"x = 1\n")
    (project / "big.log").write_bytes(b"x" * (editor_bridge.MAX_WHOLE_FILE + 1))
    outside = tmp_path / "outside.py"
    outside.write_bytes(b"print()\n")
    app = str(project / "src" / "app.py")

    result = attach({"items": [
        {"path": app, "start_line": 3, "end_line": 2},
        {"path": app, "start_line": 2, "end_line": 3},  # the same range again
        {"path": str(project / "src" / "with space.py")},
        {"path": str(project / ".env")},
        {"path": str(outside)},
        {"path": str(project / "big.log")},
        {"path": str(project / "big.log"), "start_line": 1},
        {"path": "src/app.py"},
    ], "text": "  Why does this fail?  "}, project=str(project), exclusions=_exclusions(project))
    assert result["text"] == 'Why does this fail?\n@file:src/app.py#L2-3 @file:"src/with space.py" @file:big.log#L1-1'
    assert result["attached"] == ["src/app.py lines 2-3", "src/with space.py", "big.log lines 1-1"]
    reasons = {Path(item["path"]).name: item["reason"] for item in result["skipped"]}
    assert "privacy settings exclude" in reasons[".env"]
    assert "isn't in the project" in reasons["outside.py"]
    assert "too large" in reasons["big.log"]
    assert "full path" in reasons["app.py"]

    with pytest.raises(BridgeError) as outside_only:
        attach({"items": [{"path": str(outside)}]}, project=str(project))
    assert outside_only.value.status == 409 and "isn't in the project Lumi has open" in str(outside_only.value)
    for body, status in (({}, 400), ({"items": [{"path": app, "start_line": 0}]}, 400),
                         ({"items": [{"path": app, "start_line": "x"}]}, 400), ({"items": [{}]}, 400),
                         ({"items": [{"path": app}] * 51}, 413), ({"items": [{"path": app}], "text": 5}, 400),
                         ([], 400)):
        with pytest.raises(BridgeError) as refused:
            attach(body, project=str(project))
        assert refused.value.status == status
    with pytest.raises(BridgeError) as no_project:
        attach({"items": [{"path": app}]}, project="")
    assert no_project.value.status == 409


def test_file_attachments_can_name_lines(tmp_path):
    project = _repository(tmp_path)
    (project / "src" / "with space.py").write_bytes(b"a\nb\nc\n")
    broker = ContextBroker(project, exclusions=_exclusions(project))
    [lines] = broker.resolve_mentions("look at @file:src/app.py#L2-3")
    assert lines.content == "two\nthree\n" and lines.label == "src/app.py lines 2-3"
    assert lines.provenance.endswith("app.py#L2-3")
    [one] = broker.resolve_mentions("@file:src/app.py#L3")
    assert one.content == "three\n"
    [clamped] = broker.resolve_mentions("@file:src/app.py#L2-99")
    assert clamped.content == "two\nthree\n" and clamped.label == "src/app.py lines 2-3"
    [past_end] = broker.resolve_mentions("@file:src/app.py#L9-12")
    assert past_end.content == "src/app.py has only 3 lines."
    [quoted] = broker.resolve_mentions('and @file:"src/with space.py#L2-2"')
    assert quoted.content == "b\n"
    [excluded] = broker.resolve_mentions("@file:.env#L1")
    assert "TOKEN" not in excluded.content and excluded.provenance == "excluded"
    [whole] = broker.resolve_mentions("@file:src/app.py")
    assert whole.content == "one\ntwo\nthree\n"


def test_the_latest_turn_that_changed_files_is_found():
    events = [
        *_turn("cp_00001_aaaaaaaa", ("a.py",), prompt="First change"),
        *_turn(prompt="Explain what you did"),  # no changes: skipped
    ]
    turn = latest_changing_turn(events)
    assert turn == {"checkpoint": "cp_00001_aaaaaaaa", "changed": ["a.py"], "finished": True, "prompt": "First change"}
    running = [*events, {"event": "user_message", "text": "Now the tests"},
               {"event": "checkpoint.created", "checkpoint_id": "cp_00002_bbbbbbbb"}]
    assert latest_changing_turn(running)["finished"] is False
    assert latest_changing_turn(running)["checkpoint"] == "cp_00002_bbbbbbbb"
    assert latest_changing_turn(_turn(changed=("b.py",)))["checkpoint"] == ""  # a CLI backend's turn
    assert latest_changing_turn(_turn()) is None and latest_changing_turn([]) is None


def test_changes_compare_with_the_snapshot_before_the_turn(tmp_path):
    project = _repository(tmp_path)
    (project / "untracked_before.txt").write_bytes(b"was here\n")
    store = SessionCheckpointStore(project, session_id="s1")
    checkpoint = store.create(conversation_history=[], reason="Before file_write")
    assert checkpoint.workspace_ref
    # The turn: an edit, a new file, a deletion, a rename, and an excluded file.
    (project / "src" / "app.py").write_bytes(b"one\nTWO\nthree\n")
    (project / "src" / "new.py").write_bytes(b"print('new')\n")
    (project / "notes.txt").unlink()
    (project / "old_name.txt").rename(project / "new_name.txt")
    (project / "untracked_before.txt").write_bytes(b"changed\n")
    (project / ".env").write_bytes(b"TOKEN=after\n")
    status_before = _git(project, "status", "--porcelain")

    events = _turn(checkpoint.id, ("src/app.py", "src/new.py"))
    data = turn_changes(str(project), events, _exclusions(project))
    assert _git(project, "status", "--porcelain") == status_before  # the index and files are untouched
    assert data["turn"]["before"] == checkpoint.id and data["turn"]["finished"]
    assert "snapshot" in data["turn"]["compared_with"]
    assert [(item["path"], item["status"], item["before_path"]) for item in data["files"]] == [
        ("new_name.txt", "renamed", "old_name.txt"),
        ("notes.txt", "deleted", "notes.txt"),
        ("src/app.py", "modified", "src/app.py"),
        ("src/new.py", "added", ""),
        ("untracked_before.txt", "modified", "untracked_before.txt"),
    ]

    rules = _exclusions(project)
    assert before_text(str(project), checkpoint.id, "src/app.py", rules) == b"one\ntwo\nthree\n"
    assert before_text(str(project), checkpoint.id, "untracked_before.txt", rules) == b"was here\n"
    assert before_text(str(project), checkpoint.id, "src/new.py", rules) is None
    for key, path, status in ((checkpoint.id, "../outside.txt", 400), (checkpoint.id, str(project / "notes.txt"), 400),
                              (checkpoint.id, ".env", 403), ("HEAD~1", "notes.txt", 400),
                              ("cp_99999_00000000", "notes.txt", 404)):
        with pytest.raises(BridgeError) as refused:
            before_text(str(project), key, path, rules)
        assert refused.value.status == status, (key, path)


def test_reported_files_compare_with_the_last_commit_when_there_is_no_snapshot(tmp_path):
    project = _repository(tmp_path)
    (project / "src" / "app.py").write_bytes(b"changed by codex\n")
    (project / "created.py").write_bytes(b"x\n")
    events = _turn(changed=("src/app.py", "created.py", "notes.txt", str(tmp_path / "elsewhere.py")))
    data = turn_changes(str(project), events)
    assert data["turn"]["before"] == "HEAD" and data["turn"]["compared_with"] == "the last commit"
    # notes.txt was reported but is as committed; a file outside the project is left out.
    assert [(item["path"], item["status"]) for item in data["files"]] == [("created.py", "added"),
                                                                        ("src/app.py", "modified")]
    assert before_text(str(project), "HEAD", "src/app.py") == b"one\ntwo\nthree\n"
    assert turn_changes(str(project), _turn()) == {"project": str(project.resolve()), "turn": None, "files": []}


def test_projects_without_git_use_the_snapshot_archive(tmp_path):
    project = tmp_path / "plain"
    project.mkdir()
    (project / "a.txt").write_bytes(b"before\n")
    checkpoint = SessionCheckpointStore(project, session_id="s2").create(conversation_history=[], reason="Before edit")
    assert checkpoint.workspace_archive and not checkpoint.workspace_ref
    (project / "a.txt").write_bytes(b"after\n")
    (project / "b.txt").write_bytes(b"new\n")
    data = turn_changes(str(project), _turn(checkpoint.id, ("a.txt", "b.txt")))
    assert [(item["path"], item["status"]) for item in data["files"]] == [("a.txt", "modified"), ("b.txt", "added")]
    assert before_text(str(project), checkpoint.id, "a.txt") == b"before\n"
    with pytest.raises(BridgeError) as no_commits:
        turn_changes(str(project), _turn(changed=("a.txt",)))
    assert no_commits.value.status == 404


def test_line_endings_a_checkout_converts_are_not_changes(tmp_path):
    project = tmp_path / "crlf"
    project.mkdir()
    _git(project, "init", "-q")
    _git(project, "config", "core.autocrlf", "true")
    (project / "a.txt").write_bytes(b"one\r\ntwo\r\n")
    (project / "b.txt").write_bytes(b"still LF on disk\n")
    _git(project, "add", ".")
    _git(project, "-c", "user.email=t@example.com", "-c", "user.name=T", "commit", "-q", "-m", "start")
    assert turn_changes(str(project), _turn(changed=("a.txt", "b.txt")))["files"] == []
    # As a checkout writes it, so an editor's diff lines up with the file on disk.
    assert before_text(str(project), "HEAD", "a.txt") == b"one\r\ntwo\r\n"


class _Settings:
    def __init__(self, enabled: bool = True):
        self.enabled = enabled

    def get(self, section, key, default=None):
        return self.enabled if (section, key) == ("security", "editor_bridge") else default


def _client(state, monkeypatch) -> tuple[TestClient, str]:
    bridge = EditorBridge()
    monkeypatch.setattr(editor_bridge, "bridge", bridge)

    async def endpoint(request):
        return await editor_bridge.handle(request, state)

    app = Starlette(routes=[Route("/api/editor/{action}", endpoint, methods=["GET", "POST"])])
    return TestClient(app), bridge._token


def test_the_endpoints(tmp_path, monkeypatch):
    project = _repository(tmp_path)
    store = SessionCheckpointStore(project, session_id="s3")
    checkpoint = store.create(conversation_history=[], reason="Before file_edit")
    (project / "src" / "app.py").write_bytes(b"one\nTWO\nthree\n")
    ledger = SimpleNamespace(project_display_events=lambda: _turn(checkpoint.id, ("src/app.py",)))
    pushed: list[dict] = []
    state = SimpleNamespace(
        settings=_Settings(), _ws_ref=object(), _push_ws_event=pushed.append,
        project=SimpleNamespace(project_path=str(project), current_session=SimpleNamespace(ledger=ledger)),
        exclusions_for=_exclusions,
    )
    client, token = _client(state, monkeypatch)
    auth = {"Authorization": f"Bearer {token}"}

    assert client.get("/api/editor/status").status_code == 403
    assert client.get("/api/editor/status", headers={"Authorization": "Bearer nope"}).status_code == 403
    assert client.get("/api/editor/status", headers={**auth, "Origin": "http://evil.example"}).status_code == 403
    status = client.get("/api/editor/status", headers=auth)
    assert status.status_code == 200 and status.json()["project"] == str(project) and status.json()["window"]
    assert status.headers["cache-control"] == "no-store"

    sent = client.post("/api/editor/context", headers=auth, json={
        "items": [{"path": str(project / "src" / "app.py"), "start_line": 1, "end_line": 2}],
        "text": "Explain", "source": "VS Code"})
    assert sent.status_code == 200 and sent.json()["attached"] == ["src/app.py lines 1-2"]
    assert pushed == [{"event": "editor_context", "text": "Explain\n@file:src/app.py#L1-2",
                       "attached": ["src/app.py lines 1-2"], "source": "VS Code", "project": str(project)}]
    assert client.post("/api/editor/context", headers=auth, content=b"{}").status_code == 415
    assert client.post("/api/editor/context", headers={**auth, "Content-Type": "application/json"},
                       content=b"{nope").status_code == 400

    changes = client.get("/api/editor/changes", headers=auth).json()
    assert [item["path"] for item in changes["files"]] == ["src/app.py"]
    before = client.get("/api/editor/before", headers=auth,
                        params={"before": changes["turn"]["before"], "path": "src/app.py"})
    assert before.status_code == 200 and before.content == b"one\ntwo\nthree\n"
    missing = client.get("/api/editor/before", headers=auth, params={"before": checkpoint.id, "path": "none.py"})
    assert missing.status_code == 404 and "didn't exist" in missing.json()["error"]
    assert client.get("/api/editor/unknown", headers=auth).status_code == 404

    state._ws_ref = None
    closed = client.post("/api/editor/context", headers=auth, json={"items": [{"path": str(project / "notes.txt")}]})
    assert closed.status_code == 409 and "window isn't open" in closed.json()["error"]
    assert len(pushed) == 1
    state.project.current_session = None
    assert client.get("/api/editor/changes", headers=auth).status_code == 404
    state.settings = _Settings(enabled=False)
    off = client.get("/api/editor/status", headers=auth)
    assert off.status_code == 403 and "turned off" in off.json()["error"]


def test_the_app_serves_the_editor_endpoints():
    from lumi.gui.app import app

    assert "/api/editor/{action}" in {getattr(route, "path", "") for route in app.routes}
