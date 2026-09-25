"""Handing work to a teammate or a CI run (lumi/handoff.py): what a hand-off holds, where it's kept,
how the next conversation or `lumi run` gets it, and the app's commands."""

from __future__ import annotations

import asyncio
import io
import json
import subprocess
from types import SimpleNamespace

import pytest

from lumi import handoff, headless, secret_scan
from lumi.cloud import CloudError
from lumi.engine.context_broker import ContextBroker
from lumi.engine.exclusions import ExclusionRules
from lumi.gui import ws_commands
from lumi.gui.sessions import SessionRecord
from tests.streaming_stub import StreamingBackend, done, text_delta, tool_call
from tests.test_connections import _StubWS

EVENTS = [
    {"event": "user_message", "text": "Why does login loop on Safari? Token ghp_abcdefghijklmnopqrstuvwxyz0123456789"},
    {"event": "tool.call", "name": "file_read", "call_id": "c1", "arguments": {"path": "src/auth.py"}},
    {"event": "tool.result", "call_id": "c1", "output": "PRIVATE_SOURCE = 1"},
    {"event": "tool.call", "name": "bash", "call_id": "c2", "arguments": {"command": "pytest -q"}},
    {"event": "tool.result", "call_id": "c2", "output": "1 failed", "is_error": True},
    {"event": "text.done", "text": "The cookie needs SameSite=None."},
]


def git(folder, *args):
    subprocess.run(["git", "-C", str(folder), "-c", "user.email=f@example.com", "-c", "user.name=F", *args],
                   check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path):
    """A clone-like repository: one commit on origin, one more not pushed, and a changed file."""
    folder = tmp_path / "web-app"
    folder.mkdir()
    git(folder, "init", "-q", "-b", "fix/safari-login")
    git(folder, "config", "core.autocrlf", "false")
    (folder / "app.py").write_text("print('hi')\n", encoding="utf-8")
    git(folder, "add", ".")
    git(folder, "commit", "-q", "-m", "start")
    git(folder, "remote", "add", "origin", "https://ada:ghp_secret0123456789@github.com/acme/web.git")
    git(folder, "update-ref", "refs/remotes/origin/fix/safari-login", "HEAD")
    (folder / "app.py").write_text("print('hello')\n", encoding="utf-8")
    git(folder, "commit", "-q", "-am", "greet")
    (folder / "notes.txt").write_text("draft\n", encoding="utf-8")
    return folder


@pytest.fixture(autouse=True)
def scan():
    secret_scan.configure(SimpleNamespace(get=lambda section, key=None, default=None: default,
                                          get_all=lambda: {"api_keys": {"openai": "sk-saved-abcdefghijklmnop0123"}}))
    yield
    secret_scan.reset()


def test_a_hand_off_holds_the_conversation_the_note_and_where_the_work_is(repo):
    data = handoff.package(EVENTS, title="Safari login loop", project_path=str(repo), model="claude-sonnet-5",
                           note="Check Safari; key sk-saved-abcdefghijklmnop0123", sender={"name": "Ada"})
    assert data["project"] == "web-app" and data["from"] == {"name": "Ada"}
    assert data["note"] == "Check Safari; key [REDACTED saved API key]"
    assert data["entries"][0]["text"].endswith("Token [REDACTED GitHub token]")
    assert "PRIVATE_SOURCE" not in json.dumps(data) and "1 failed" not in json.dumps(data)
    head = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    assert data["repo"] == {"remote": "https://github.com/acme/web.git", "branch": "fix/safari-login",
                            "commit": head, "changed_files": 1, "unpushed": 1}
    assert handoff.repo_state(str(repo.parent)) == {}  # not a repository

    text = handoff.render(data)
    assert text.startswith("Hand-off: Safari login loop\n\nAda handed this work over on ")
    assert "not as instructions from the person you're working with now" in text
    assert "Their note: Check Safari" in text
    assert ("The work is on branch fix/safari-login at commit " + head[:12] + " of https://github.com/acme/web.git. "
            "When it was handed off, 1 changed file wasn't committed and 1 commit wasn't pushed") in text
    assert "[Ada] Why does login loop on Safari?" in text and "- Ran `pytest -q` (failed)" in text
    assert "[Lumi] The cookie needs SameSite=None." in text

    with pytest.raises(handoff.HandoffError, match="nothing in this conversation"):
        handoff.package([], title="x", project_path=str(repo), model="", note="")


def test_long_conversations_keep_the_first_message_and_the_latest():
    entries = [{"role": "user", "text": "The goal"}] + [{"role": "assistant", "text": f"step {n} " + "x" * 200}
                                                         for n in range(100)]
    text = handoff.render({"title": "Long", "entries": entries}, limit=3000)
    assert "[A teammate] The goal" in text and "step 99" in text and "step 1 " not in text
    assert "earlier entries left out" in text and len(text) < 3000


def test_addresses_and_repositories():
    assert handoff.without_credentials("https://ada:tok@github.com/acme/web.git") == "https://github.com/acme/web.git"
    assert handoff.without_credentials("https://tok@github.com/acme/web") == "https://github.com/acme/web"
    assert handoff.without_credentials("ssh://git@github.com/acme/web.git") == "ssh://git@github.com/acme/web.git"
    assert handoff.without_credentials("git@github.com:acme/web.git") == "git@github.com:acme/web.git"
    assert handoff.same_repository("git@github.com:Acme/web.git", "https://github.com/acme/web")
    assert not handoff.same_repository("https://github.com/acme/web", "https://github.com/acme/api")


def test_files_ids_and_what_load_refuses(repo, tmp_path):
    data = handoff.package(EVENTS, title="Safari login loop!", project_path=str(repo), model="m", note="n")
    path = handoff.save_for_ci(str(repo), data)
    assert path.parent == repo / ".lumi" / "handoffs" and path.name.startswith("safari-login-loop-")
    assert handoff.save_for_ci(str(repo), data) != path  # never overwrites
    loaded, source = handoff.load(path.relative_to(repo).as_posix(), str(repo))
    assert loaded["title"] == "Safari login loop!" and source == str(path)

    with pytest.raises(handoff.HandoffError, match="inside the project"):
        handoff.load("../outside.json", str(repo))
    rules = ExclusionRules(str(repo), [(".lumi/handoffs/**", "Settings")])
    with pytest.raises(handoff.HandoffError):
        handoff.load(path.relative_to(repo).as_posix(), str(repo), exclusions=rules)
    (repo / "not-a-handoff.json").write_text('{"hello": 1}', encoding="utf-8")
    with pytest.raises(handoff.HandoffError, match="isn't a Lumi hand-off"):
        handoff.load("not-a-handoff.json", str(repo))
    with pytest.raises(handoff.HandoffError, match="Pick it up"):
        handoff.load("hof_0123456789abcdef")
    handoff.save_local({**data, "id": "hof_0123456789abcdef"})
    assert handoff.load("hof_0123456789abcdef")[0]["title"] == "Safari login loop!"


def test_checking_the_folder_to_continue_in(repo, tmp_path):
    head = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    work = {"remote": "https://github.com/acme/web.git", "branch": "fix/safari-login", "commit": head}
    assert handoff.check_folder(work, str(repo)) == {
        "ok": True, "message": "This folder is at the handed-off commit. It has changes that aren't committed."}
    git(repo, "switch", "-q", "-c", "main", "HEAD~1")
    moved = handoff.check_folder(work, str(repo))
    assert not moved["ok"] and moved["message"].startswith("This folder is on branch main. The work is on branch "
                                                           "fix/safari-login at " + head[:12] + ": switch to it")
    missing = handoff.check_folder({**work, "commit": "0" * 40}, str(repo))
    assert "isn't here yet: fetch it" in missing["message"]
    other = handoff.check_folder({**work, "remote": "https://github.com/acme/api.git"}, str(repo))
    assert other == {"ok": False, "message": "This folder is a clone of https://github.com/acme/web.git, but the "
                                             "work is in https://github.com/acme/api.git."}
    plain = tmp_path / "plain"
    plain.mkdir()
    assert "isn't a git repository" in handoff.check_folder(work, str(plain))["message"]
    assert handoff.suggest_project(work, [str(plain), str(repo)]) == str(repo)


def test_the_next_conversation_keeps_the_hand_off(repo):
    data = handoff.package(EVENTS, title="Safari login loop", project_path=str(repo), model="m", note="Add a test")
    handoff.save_local({**data, "id": "hof_0123456789abcdef", "from": {"name": "Ada"}})
    broker = ContextBroker(repo)
    [item] = broker.resolve_mentions("@handoff:hof_0123456789abcdef Continue")
    assert item.provider == "handoff" and item.label == "Safari login loop" and item.pinned
    assert "Their note: Add a test" in item.content
    # It stays for later messages that don't mention it.
    assert [i.id for i in broker.resolve_mentions("And the tests?")] == [item.id]
    [missing] = ContextBroker(repo).resolve_mentions("@handoff:hof_ffffffffffffffff")
    assert missing.provenance == "error" and "Pick it up" in missing.content and not missing.pinned

    # Reopened after a restart: the mention in the history attaches it again.
    from lumi.engine.session import Session

    class Recorder(StreamingBackend):
        def stream(self, **kwargs):
            self.instructions = kwargs["instructions"]
            yield from super().stream(**kwargs)

    backend = Recorder(scripts=[[text_delta("On it."), done()]])
    session = Session(backend, max_steps=2, auto_approve=True)
    session.context_broker = ContextBroker(repo)
    session.conversation_history = [{"role": "user", "content": "@handoff:hof_0123456789abcdef Continue"},
                                    {"role": "assistant", "content": "Looking."}]
    list(session.run("And the tests?"))
    assert "Hand-off: Safari login loop" in backend.instructions


def test_lumi_run_continues_from_a_hand_off_file(monkeypatch, repo):
    data = handoff.package(EVENTS, title="Safari login loop", project_path=str(repo), model="m", note="Add a test")
    path = handoff.save_for_ci(str(repo), data)
    # Continuing work means changing something: a reply alone would be "incomplete".
    backend = StreamingBackend(name="anthropic", model="claude-haiku-4-5", scripts=[
        [tool_call("file_write", {"path": "test_safari.py", "content": "def test_cookie():\n    pass\n"}), done()],
        [text_delta("Added the test."), done()]])
    monkeypatch.setattr(headless, "build_spec", lambda settings, provider, model, project: SimpleNamespace(
        create_backend=lambda settings: backend, permission_mode=""))

    def run(*args):
        out, err = io.StringIO(), io.StringIO()
        code = headless.main(["--provider", "anthropic", "--model", "claude-haiku-4-5", "--project", str(repo), *args],
                             stdin=io.StringIO(""), stdout=out, stderr=err)
        return code, err.getvalue()

    code, err = run("--handoff", path.relative_to(repo).as_posix())
    assert code == 0, err
    prompt = backend.stream_calls[0]["user_msg"]
    assert prompt.startswith("Continue the work in this hand-off.\n\n--- HAND-OFF ---\nHand-off: Safari login loop")
    assert "Their note: Add a test" in prompt and prompt.endswith("--- END HAND-OFF ---")
    assert run("--handoff", "missing.json") == (2, "lumi run: There's no hand-off file at missing.json.\n")


class FakeCloud:
    """Lumi Cloud's hand-off API, as the signed-in person sees it."""

    def __init__(self, repo_commit=""):
        self.calls: list[tuple[str, str, dict]] = []
        self.refuse = ""
        self.signed_in = True
        self.commit = repo_commit

    def status(self):
        return {"signed_in": self.signed_in, "account": {"name": "Ada", "email": "ada@example.com",
                                                         "organizations": [{"id": "org_acme", "name": "Acme"}]}}

    def account_call(self, method, path, **kwargs):
        self.calls.append((method, path, kwargs.get("json") or kwargs.get("params") or {}))
        if self.refuse:
            raise CloudError(self.refuse)
        if path == "/api/v1/handoffs/recipients":
            return {"recipients": [{"id": "usr_bob", "name": "Bob", "email": "bob@example.com"}]}
        if method == "POST" and path == "/api/v1/handoffs":
            return {"id": "hof_1111111111111111", "status": "open", "to": {"name": "Bob", "email": "bob@example.com"}}
        waiting = {"id": "hof_2222222222222222", "title": "API rename", "note": "Finish the callers",
                   "from": {"name": "Carol"}, "organization": {"id": "org_acme", "name": "Acme"},
                   "repo": {"remote": "git@github.com:acme/web.git", "branch": "fix/safari-login", "commit": self.commit},
                   "created_at": "2026-09-25T10:00:00Z"}
        if path == "/api/v1/handoffs":
            return {"to_me": [waiting], "from_me": []}
        if path.endswith("/pick-up"):
            return {**waiting, "status": "picked_up", "entries": [{"role": "user", "text": "Rename the API"}]}
        return {**waiting, "status": "dismissed"}


def _run(state, command, **msg):
    ctx = ws_commands.CommandContext(ws=_StubWS(), state=state, msg={"command": command, **msg},
                                     runs=SimpleNamespace(busy=False))
    asyncio.run(ws_commands.HANDLERS[command](ctx))
    return ctx.ws.sent


def test_the_app_hands_off_and_picks_up(repo, tmp_path):
    SessionRecord(session_id="s-handoff", title="Safari login loop", project_path=str(repo), model="claude-sonnet-5",
                  display_events=EVENTS, message_count=1).save()
    head = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    cloud = FakeCloud(repo_commit=head)
    state = SimpleNamespace(cloud=cloud, settings=SimpleNamespace(get=lambda *args: ""),
                            project=SimpleNamespace(project_path=str(repo), get_recent_projects=lambda: [
                                {"path": str(tmp_path)}, {"path": str(repo)}]))

    [status] = _run(state, "session_handoff_status", session_id="s-handoff")
    assert status["signed_in"] and status["saved"] and status["repo"]["branch"] == "fix/safari-login"
    assert status["organizations"] == [{"id": "org_acme", "name": "Acme", "recipients": [
        {"id": "usr_bob", "name": "Bob", "email": "bob@example.com"}]}]

    [sent] = _run(state, "session_handoff", session_id="s-handoff", note="Add a test", target="teammate",
                  organization_id="org_acme", to="usr_bob")
    assert sent == {"event": "session_handoff_done", "session_id": "s-handoff", "kind": "teammate",
                    "id": "hof_1111111111111111", "to": {"name": "Bob", "email": "bob@example.com"}}
    method, path, body = cloud.calls[-1]
    assert (method, path, body["to"], body["note"], body["repo"]["branch"]) == (
        "POST", "/api/v1/handoffs", "usr_bob", "Add a test", "fix/safari-login")
    assert "from" not in body and len(body["entries"]) == 4

    [ci] = _run(state, "session_handoff", session_id="s-handoff", note="For CI", target="ci")
    assert ci["kind"] == "ci" and ci["path"].startswith(".lumi/handoffs/safari-login-loop-")
    assert ci["command"] == f"lumi run --handoff {ci['path']}"
    saved = json.loads((repo / ci["path"]).read_text(encoding="utf-8"))
    assert saved["from"] == {"name": "Ada"} and saved["note"] == "For CI"  # a name, never an email address

    cloud.refuse = "Hand off to someone in Acme."
    [refused] = _run(state, "session_handoff", session_id="s-handoff", target="teammate", to="usr_eve")
    assert refused["event"] == "session_handoff" and refused["error"] == "Hand off to someone in Acme."
    cloud.refuse = ""

    [box] = _run(state, "handoffs_inbox")
    assert [item["id"] for item in box["to_me"]] == ["hof_2222222222222222"]
    assert box["to_me"][0]["suggested_project"] == str(repo)  # the recent project that is a clone of it
    [check] = _run(state, "handoff_check", id="hof_2222222222222222", repo=box["to_me"][0]["repo"],
                   project_path=str(repo))
    assert check["ok"] and check["message"].startswith("This folder is at the handed-off commit.")

    [picked] = _run(state, "handoff_pick_up", id="hof_2222222222222222", project_path=str(repo))
    assert picked == {"event": "handoff_picked_up", "id": "hof_2222222222222222", "project_path": str(repo),
                      "title": "API rename",
                      "draft": "@handoff:hof_2222222222222222 Continue the work Carol handed off: API rename."}
    assert handoff.load("hof_2222222222222222")[0]["entries"] == [{"role": "user", "text": "Rename the API"}]

    [after] = _run(state, "handoff_close", id="hof_2222222222222222")
    assert after["event"] == "handoffs" and cloud.calls[-2][:2] == ("DELETE", "/api/v1/handoffs/hof_2222222222222222")
    cloud.signed_in = False
    assert _run(state, "handoffs_inbox")[0] == {"event": "handoffs", "signed_in": False, "to_me": [], "from_me": []}
