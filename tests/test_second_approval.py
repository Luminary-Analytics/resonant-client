"""A second person approves risky commands before they run (lumi/engine/second_approval.py)."""

from __future__ import annotations

import json
import sys
import threading
from types import SimpleNamespace

import pytest

from lumi import policy
from lumi.approvals import ApprovalRequester
from lumi.engine import second_approval
from tests.streaming_stub import StreamingBackend, done, text_delta, tool_call

WRITE = f'{sys.executable} -c "open(\'ran.txt\', \'w\').write(\'x\')"'


@pytest.fixture(autouse=True)
def approvals():
    policy.set_for_tests(policy.parse({"schema": "lumi.policy/v1", "organization": "Acme", "approvals": {
        "commands": ["git push --force*", "terraform apply*", "* -c *open(*"], "wait_minutes": 5}}, source="test"))
    yield
    second_approval.set_requester(None)


class Approvers:
    """Lumi Cloud's approvals API: answers come from a script, one per poll."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.requests = []

    def request(self, payload):
        self.requests.append(payload)
        return {"id": "apr_1", "organization": "Acme", "approvers": ["Bob"]}

    def status(self, request_id):
        answer = self.answers.pop(0) if self.answers else {"status": "waiting"}
        if isinstance(answer, Exception):
            raise answer
        return answer


def test_which_commands_need_a_second_person():
    assert second_approval.needed("bash", {"command": "git push  --force origin main"}) == "git push --force*"
    assert second_approval.needed("job_start", {"command": ["terraform", "apply", "-auto-approve"]}) == \
        "terraform apply*"
    assert second_approval.needed("bash", {"command": "git push origin feature"}) == ""
    assert second_approval.needed("file_write", {"path": "x"}) == ""
    policy.set_for_tests(None)
    assert second_approval.needed("bash", {"command": "git push --force"}) == ""


def test_waiting_for_an_answer():
    clock = SimpleNamespace(now=0.0)
    sleep = lambda seconds: setattr(clock, "now", clock.now + seconds)  # noqa: E731

    def run(*answers, cancel=None):
        second_approval.set_requester(Approvers(*answers))
        request = second_approval.ask("bash", {"command": "git push --force origin main"}, "git push --force*")
        return second_approval.await_decision(request, cancel_event=cancel, clock=lambda: clock.now, sleep=sleep)

    approved = run({"status": "waiting"}, ConnectionError("dropped"), {"status": "approved", "approver": "Bob"})
    assert (approved.state, approved.approver, approved.message) == ("approved", "Bob", "")
    denied = run({"status": "denied", "approver": "Bob"})
    assert denied.state == "denied" and denied.message.startswith("Bob in Acme denied running this (git push --force*)")
    clock.now = 0.0
    expired = run()  # nobody answers within the policy's 5 minutes
    assert expired.state == "expired" and "within 5 minutes" in expired.message and clock.now >= 300
    stop = threading.Event()
    stop.set()
    assert run(cancel=stop).state == "cancelled"

    second_approval.set_requester(None)
    unavailable = second_approval.ask("bash", {"command": "git push --force"}, "git push --force*")
    assert unavailable.state == "unavailable" and "isn't signed in" in unavailable.message


def test_the_command_sent_has_secrets_removed():
    approvers = Approvers()
    second_approval.set_requester(approvers)
    second_approval.ask("bash", {"command": "GH_TOKEN=ghp_abcdefghijklmnopqrstuvwxyz0123456789 git push --force"},
                        "git push --force*", project="web")
    [sent] = approvers.requests
    assert "ghp_" not in sent["command"] and "[REDACTED GitHub token]" in sent["command"]
    assert (sent["pattern"], sent["project"], sent["wait_minutes"]) == ("git push --force*", "web", 5)


def _session(tmp_path, backend):
    from lumi.engine.session import Session

    session = Session(backend, max_steps=3, auto_approve=True)
    session.project_path = str(tmp_path)
    return session


@pytest.mark.parametrize("answer, runs", [("approved", True), ("denied", False)])
def test_a_session_runs_the_command_only_once_approved(tmp_path, monkeypatch, answer, runs):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(second_approval, "await_decision", lambda request, **kwargs: (
        setattr(request, "state", answer) or setattr(request, "approver", "Bob")
        or setattr(request, "message", "" if answer == "approved" else "Bob in Acme denied running this.") or request))
    second_approval.set_requester(Approvers())
    backend = StreamingBackend(scripts=[[tool_call("bash", {"command": WRITE}), done()],
                                        [text_delta("Done."), done()]])
    events = list(_session(tmp_path, backend).run("Write the file"))
    waits = [event for event in events if event.get("event") == "approval.wait"]
    assert [event["state"] for event in waits] == ["waiting", answer]
    assert waits[0]["approvers"] == ["Bob"] and waits[0]["organization"] == "Acme"
    [result] = [event for event in events if event.get("event") == "tool.result"]
    assert (tmp_path / "ran.txt").exists() is runs
    assert bool(result.get("denied")) is not runs
    if not runs:
        assert result["output"] == "Bob in Acme denied running this."


def test_the_requester_asks_the_right_organization():
    calls = []

    class Cloud:
        def __init__(self, status):
            self._status = status

        def status(self):
            return self._status

        def account_call(self, method, path, **kwargs):
            calls.append((method, path, json.dumps(kwargs.get("json") or {}, sort_keys=True)))
            return {"id": "apr_1", "approvers": ["Bob"]} if method == "POST" else {"status": "approved"}

    enrolled = Cloud({"signed_in": True, "device": {"organization_id": "org_dev", "organization_name": "Acme"},
                      "account": {"organizations": [{"id": "org_other", "name": "Other"}]}})
    assert ApprovalRequester(enrolled).request({"command": "x"}) == {"id": "apr_1", "organization": "Acme",
                                                                     "approvers": ["Bob"]}
    assert '"organization_id": "org_dev"' in calls[-1][2]
    assert ApprovalRequester(enrolled).status("apr_1") == {"status": "approved"}
    with pytest.raises(RuntimeError, match="isn't signed in"):
        ApprovalRequester(Cloud({"signed_in": False})).request({})


def test_nobody_is_asked_to_approve_what_the_floor_refuses(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    approvers = Approvers()
    second_approval.set_requester(approvers)
    backend = StreamingBackend(scripts=[[tool_call("bash", {"command": "git push --force origin main"}), done()],
                                        [text_delta("Blocked."), done()]])
    events = list(_session(tmp_path, backend).run("Force-push main"))
    assert approvers.requests == [] and not [e for e in events if e.get("event") == "approval.wait"]
    [result] = [event for event in events if event.get("event") == "tool.result"]
    assert "protected_branch_force_push" in result["output"]
