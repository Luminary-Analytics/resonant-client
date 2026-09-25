"""The organization's library (lumi/team_library.py): syncing it, offering its skills, its prompts,
and project notes shared with the team."""

from __future__ import annotations

import asyncio
import hashlib
import subprocess
from types import SimpleNamespace

import pytest

from lumi import team_library
from lumi.cloud import CloudError
from lumi.engine.tools import execute_tool
from lumi.gui import ws_commands
from tests.test_connections import _StubWS

RELEASE = {"id": "lib_1", "slug": "cut-a-release", "kind": "skill", "name": "Cut a release",
           "description": "How we ship a version", "triggers": ["release", "version bump"],
           "body": "1. Check that main is green.\n2. Update CHANGELOG.md.", "version": 3,
           "updated_at": "2026-09-25T10:00:00Z", "updated_by": "Ada"}
MIGRATION = {**RELEASE, "id": "lib_2", "slug": "database-migrations", "name": "Database migrations",
             "description": "Write and check a schema migration", "triggers": ["migration"], "version": 1}
REVIEW = {"id": "lib_3", "slug": "review-checklist", "kind": "prompt", "name": "Review checklist",
          "description": "Our review checklist", "triggers": [], "body": "Review this change against: tests, docs.",
          "version": 2, "updated_at": "2026-09-25T11:00:00Z", "updated_by": "Bob"}


class FakeCloud:
    def __init__(self):
        self.calls = 0
        self.refuse = ""
        self.signed_in = True
        self.library = [{"id": "org_acme", "name": "Acme", "items": [
            RELEASE, MIGRATION, REVIEW, {"kind": "skill", "slug": "../escape", "name": "bad"}, {"kind": "recipe"}]}]

    def status(self):
        return {"signed_in": self.signed_in}

    def account_call(self, method, path, **kwargs):
        assert (method, path) == ("GET", "/api/v1/library")
        self.calls += 1
        if self.refuse:
            raise CloudError(self.refuse)
        return {"organizations": self.library}

    def sign_out(self):
        self.signed_in = False


def test_syncing_offering_skills_and_reading_them():
    cloud = FakeCloud()
    assert team_library.sync(cloud)["organizations"] == [{"id": "org_acme", "name": "Acme", "skills": 2,
                                                          "prompts": 1, "notes": 0}]
    assert [item["ref"] for item in team_library.items()] == [
        "team:org_acme/cut-a-release", "team:org_acme/database-migrations", "team:org_acme/review-checklist"]

    # A trigger phrase outranks shared words, and nothing matches an unrelated request.
    assert [s["slug"] for s in team_library.matching_skills("Prepare the version bump for the database")] == [
        "cut-a-release", "database-migrations"]
    assert team_library.matching_skills("Fix the login page") == []
    block = team_library.skill_context("Cut the 2.0 release")
    assert "## Your organization's library skills" in block and "Acme: Cut a release (version 3)" in block
    assert "skill_view skill_id=team:org_acme/cut-a-release" in block and "Database migrations" not in block

    viewed = execute_tool("skill_view", {"skill_id": "team:org_acme/cut-a-release"})
    assert viewed.output.startswith("# Cut a release\n\nFrom Acme's library, version 3, by Ada.")
    assert "Update CHANGELOG.md" in viewed.output and viewed.metadata["scope"] == "team"
    assert "No team skill" in execute_tool("skill_view", {"skill_id": "team:org_acme/review-checklist"}).output

    # An archived item disappears at the next sync; a failed sync keeps the copy.
    cloud.library[0]["items"] = [MIGRATION]
    team_library.sync(cloud)
    assert "archived" in team_library.read_skill("team:org_acme/cut-a-release")
    cloud.refuse = "Lumi Cloud couldn't be reached (ConnectError)."
    with pytest.raises(team_library.LibraryError, match="couldn't be reached"):
        team_library.sync(cloud)
    assert [item["slug"] for item in team_library.items()] == ["database-migrations"]


def _run(state, command, **msg):
    ctx = ws_commands.CommandContext(ws=_StubWS(), state=state, msg={"command": command, **msg},
                                     runs=SimpleNamespace(busy=False))
    asyncio.run(ws_commands.HANDLERS[command](ctx))
    return ctx.ws.sent


def test_the_apps_command_syncs_when_old_or_asked_and_sign_out_forgets(monkeypatch):
    cloud = FakeCloud()
    state = SimpleNamespace(cloud=cloud)
    [first] = _run(state, "team_library")
    assert cloud.calls == 1 and first["signed_in"] and first["organizations"][0]["prompts"] == 1
    assert first["prompts"] == [{"ref": "team:org_acme/review-checklist", "name": "Review checklist",
                                 "description": "Our review checklist", "body": REVIEW["body"], "version": 2,
                                 "organization": {"id": "org_acme", "name": "Acme"}, "updated_by": "Bob"}]
    _run(state, "team_library")
    assert cloud.calls == 1  # fresh enough: the copy is used
    cloud.refuse = "Sign in again."
    [refused] = _run(state, "team_library", sync=True)
    assert cloud.calls == 2 and refused["error"] == "Sign in again." and refused["prompts"]

    monkeypatch.setattr(ws_commands, "_cloud_run", lambda ctx, work: _call(work, ctx.state.cloud))
    _run(state, "cloud_sign_out")
    assert team_library.cached() == {"synced_at": 0, "organizations": []} and not cloud.signed_in
    assert _run(state, "team_library")[0] == {"event": "team_library", "signed_in": False, "synced_at": 0,
                                              "organizations": [], "prompts": []}


async def _call(work, cloud):
    work(cloud)


# ── Project notes ────────────────────────────────────────────────────────────


def _git(folder, *args):
    subprocess.run(["git", "-C", str(folder), "-c", "user.email=f@example.com", "-c", "user.name=F", *args],
                   check=True, capture_output=True)


@pytest.fixture
def clone(tmp_path):
    """A project whose origin is github.com/acme/web, with a Makefile written with Windows line endings."""
    folder = tmp_path / "web"
    folder.mkdir()
    _git(folder, "init", "-q")
    _git(folder, "remote", "add", "origin", "git@github.com:Acme/web.git")
    (folder / "Makefile").write_bytes(b"check:\r\n\tpytest -m 'not slow'\r\n")
    team_library._repositories.clear()
    return folder


def _synced_note(cloud, **overrides):
    note = {"id": "tnt_1", "repository": "github.com/acme/web", "kind": "build_command",
            "text": "Run the tests with make check; plain pytest skips the slow suite.",
            "source": "Makefile, check target", "author": "Bob", "approved_by": "Ada",
            # The hash of the Makefile with Unix line endings: a Windows checkout agrees.
            "fingerprints": {"Makefile": hashlib.sha256(b"check:\n\tpytest -m 'not slow'\n").hexdigest()},
            **overrides}
    cloud.library[0]["notes"] = [note, {**note, "id": "tnt_2", "repository": "github.com/acme/api",
                                        "text": "Another repository's note."}, {"kind": "rumor", "text": "x"}]
    team_library.sync(cloud)


def test_team_notes_are_recalled_for_the_same_repository_while_their_files_hold(clone):
    cloud = FakeCloud()
    _synced_note(cloud)
    assert team_library.repository_of(str(clone)) == "github.com/acme/web"
    [note] = team_library.notes_for(str(clone))
    assert note["stale"] is False and note["organization"]["name"] == "Acme"
    context = team_library.team_notes_context(str(clone), "How do I run the tests?")
    assert "[team; build_command; by Bob, approved by Ada; source: Makefile, check target]" in context
    assert "Another repository" not in context
    assert team_library.team_notes_context(str(clone), "Rename the login page") == ""  # nothing relevant

    (clone / "Makefile").write_bytes(b"check:\n\tpytest\n")
    assert team_library.notes_for(str(clone))[0]["stale"] is True
    assert team_library.team_notes_context(str(clone), "How do I run the tests?") == ""


def test_sharing_a_note_sends_its_provenance(clone, tmp_path):
    cloud = FakeCloud()
    sent = []
    cloud.account_call = lambda method, path, **kwargs: sent.append((method, path, kwargs["json"])) or {
        "id": "tnt_9", "status": "pending", "organization": {"id": "org_acme", "name": "Acme"}}
    note = {"text": "Run the tests with make check", "kind": "build_command", "source": "Makefile",
            "sources": ["Makefile"], "stale": False}
    team_library.share_note(cloud, str(clone), note, "org_acme")
    method, path, body = sent[-1]
    assert (method, path, body["repository"], body["organization_id"]) == (
        "POST", "/api/v1/library/notes", "github.com/acme/web", "org_acme")
    assert body["fingerprints"] == {"Makefile": team_library.fingerprint(clone / "Makefile")}

    with pytest.raises(team_library.LibraryError, match="changed since it was saved"):
        team_library.share_note(cloud, str(clone), {**note, "stale": True}, "org_acme")
    with pytest.raises(team_library.LibraryError, match="isn't a file"):
        team_library.share_note(cloud, str(clone), {**note, "sources": ["../outside.txt"]}, "org_acme")
    plain = tmp_path / "plain"
    plain.mkdir()
    with pytest.raises(team_library.LibraryError, match="origin remote"):
        team_library.share_note(cloud, str(plain), note, "org_acme")


def test_the_notes_dialog_and_a_turn_get_team_notes(clone):
    cloud = FakeCloud()
    _synced_note(cloud)
    cloud.status = lambda: {"signed_in": True, "account": {"organizations": [{"id": "org_acme", "name": "Acme"}]}}
    shared = []
    cloud.account_call = lambda method, path, **kwargs: shared.append(kwargs["json"]) or {
        "id": "tnt_9", "status": "pending", "organization": {"id": "org_acme", "name": "Acme"}}
    state = SimpleNamespace(cloud=cloud, project=SimpleNamespace(project_path=str(clone)))
    [saved] = _run(state, "memory_save", text="Deploys go through the release workflow", source="docs/deploy.md",
                   kind="constraint", sources=[])
    assert saved["share_to"] == [{"id": "org_acme", "name": "Acme"}] and saved["team_notes"][0]["id"] == "tnt_1"
    [after] = _run(state, "memory_share", id=saved["memories"][0]["id"], organization_id="org_acme")
    assert after["shared"].startswith("Sent to Acme for review.") and shared[0]["kind"] == "constraint"

    from lumi.engine.session import Session
    from tests.streaming_stub import StreamingBackend, done, text_delta

    class Recorder(StreamingBackend):
        def stream(self, **kwargs):
            self.instructions = kwargs["instructions"]
            yield from super().stream(**kwargs)

    backend = Recorder(scripts=[[text_delta("Use make check."), done()]])
    session = Session(backend, max_steps=2, auto_approve=True)
    session.project_path = str(clone)
    list(session.run("How do I run the tests?"))
    assert "--- TEAM PROJECT NOTES ---" in backend.instructions and "by Bob, approved by Ada" in backend.instructions
