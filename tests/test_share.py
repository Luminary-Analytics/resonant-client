"""Sharing a conversation (lumi/share.py): what the copy holds, and the Share dialog's commands."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from lumi import secret_scan, share
from lumi.cloud import CloudError
from lumi.gui import ws_commands
from lumi.gui.sessions import SessionRecord
from tests.test_connections import _StubWS

EVENTS = [
    {"event": "user_message", "text": "Fix the login loop; my key is sk-live-abcdefghijklmnopqrstuvwxyz0123"},
    {"event": "tool.call", "name": "file_read", "call_id": "c1", "arguments": {"path": "src/auth.py"}},
    {"event": "tool.result", "call_id": "c1", "output": "SECRET_FILE_CONTENTS = 1", "is_error": False},
    {"event": "tool.call", "name": "bash", "call_id": "c2", "arguments": {"command": "pytest  -q\ttests"}},
    {"event": "tool.result", "call_id": "c2", "output": "1 failed", "is_error": True},
    {"event": "tool.call", "name": "file_edit", "call_id": "c3", "arguments": {"path": "src/auth.py"}},
    {"event": "tool.result", "call_id": "c3", "output": "", "denied": True},
    {"event": "text.delta", "text": "partial"},
    {"event": "text.done", "text": "The cookie needed SameSite=None. Token: ghp_abcdefghijklmnopqrstuvwxyz0123456789"},
    {"event": "session.end", "outcome": "answered"},
]


@pytest.fixture(autouse=True)
def scan():
    secret_scan.configure(SimpleNamespace(get=lambda section, key=None, default=None: default,
                                          get_all=lambda: {"api_keys": {"openai": "sk-live-abcdefghijklmnopqrstuvwxyz0123"}}))
    yield
    secret_scan.reset()


def test_the_copy_holds_messages_replies_and_actions_only():
    copy = share.export(EVENTS, title="Login loop", project_path="/home/ada/code/web-app", model="claude-sonnet-5")
    assert copy["title"] == "Login loop" and copy["project"] == "web-app" and copy["model"] == "claude-sonnet-5"
    assert copy["entries"] == [
        {"role": "user", "text": "Fix the login loop; my key is [REDACTED saved API key]"},
        {"role": "action", "text": "Read src/auth.py"},
        {"role": "action", "text": "Ran `pytest -q tests`", "error": True},
        {"role": "action", "text": "Edited src/auth.py", "error": True},
        {"role": "assistant", "text": "The cookie needed SameSite=None. Token: [REDACTED GitHub token]"},
    ]
    text = json.dumps(copy)
    assert "SECRET_FILE_CONTENTS" not in text and "1 failed" not in text and "partial" not in text
    assert "/home/ada" not in text  # only the project folder's name


class FakeCloud:
    """The account side of Lumi Cloud: sign-in state, and the shares API."""

    def __init__(self, signed_in=True):
        self.signed_in = signed_in
        self.calls: list[tuple[str, str, dict]] = []
        self.refuse = ""

    def status(self):
        return {"signed_in": self.signed_in,
                "account": {"organizations": [{"id": "org_acme", "name": "Acme", "role": "owner"}]}}

    def account_call(self, method, path, **kwargs):
        self.calls.append((method, path, kwargs.get("json") or {}))
        if self.refuse:
            raise CloudError(self.refuse)
        if method == "POST":
            return {"id": "shr_1", "url": "https://cloud.test/shared/tok", "visibility": kwargs["json"]["visibility"]}
        return {"revoked": True}


def _run(state, command, **msg):
    ctx = ws_commands.CommandContext(ws=_StubWS(), state=state, msg={"command": command, **msg},
                                     runs=SimpleNamespace(busy=False))
    asyncio.run(ws_commands.HANDLERS[command](ctx))
    return ctx.ws.sent


def test_the_share_dialog(tmp_path):
    project = str(tmp_path / "web-app")
    record = SessionRecord(session_id="s-share", title="Login loop", project_path=project, model="claude-sonnet-5",
                           display_events=EVENTS, message_count=1)
    record.save()
    cloud = FakeCloud()
    state = SimpleNamespace(cloud=cloud, project=SimpleNamespace(project_path=str(tmp_path / "other"),
                                                                 get_recent_projects=lambda: [{"path": project}]))

    # Replay finds saved conversations the same way (from any recent project).
    [replay] = _run(state, "get_session_replay_events", session_id="s-share")
    assert replay["title"] == "Login loop" and replay["events"] == EVENTS

    [status] = _run(state, "session_share_status", session_id="s-share")
    assert status == {"event": "session_share", "session_id": "s-share", "share": None, "signed_in": True,
                      "organizations": [{"id": "org_acme", "name": "Acme"}]}

    [shared] = _run(state, "session_share", session_id="s-share", organization_id="org_acme", visibility="organization")
    assert shared["share"]["url"] == "https://cloud.test/shared/tok" and "error" not in shared
    method, path, body = cloud.calls[-1]
    assert (method, path, body["organization_id"], body["visibility"]) == ("POST", "/api/v1/shares", "org_acme",
                                                                          "organization")
    assert body["title"] == "Login loop" and body["project"] == "web-app" and len(body["entries"]) == 5
    assert share.remembered()["s-share"]["id"] == "shr_1"  # found in a recent project, remembered for next time

    [stopped] = _run(state, "session_share_stop", session_id="s-share")
    assert stopped["share"] is None and cloud.calls[-1][:2] == ("DELETE", "/api/v1/shares/shr_1")

    cloud.refuse = "Acme shares sessions only with its members."
    [refused] = _run(state, "session_share", session_id="s-share", organization_id="org_acme", visibility="link")
    assert refused["error"] == "Acme shares sessions only with its members." and refused["share"] is None
    [missing] = _run(state, "session_share", session_id="nope", organization_id="org_acme")
    assert missing["error"] == "That conversation isn't saved yet."
    cloud.signed_in = False
    assert _run(state, "session_share_status", session_id="s-share")[0]["signed_in"] is False
