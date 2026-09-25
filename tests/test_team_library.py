"""The organization's library (lumi/team_library.py): syncing it, offering its skills, and its prompts."""

from __future__ import annotations

import asyncio
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
                                                          "prompts": 1}]
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
