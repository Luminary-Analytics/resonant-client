"""Send feedback (lumi/feedback.py): the report, what never leaves, the queue, and the dialog's commands.

Lumi Cloud is an ``httpx.MockTransport`` here (``feedback.set_transport_for_tests``),
never a real server, answering as the feedback contract says (docs/feedback.md):
201 with the report's ``Idempotency-Key`` echoed. tests/feedback_view.test.cjs
covers the dialog's own logic, and tests/feedback.browser.cjs the dialog in a
real browser.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from lumi import __version__, audit, dlp, feedback, offline, policy as lumi_policy
from lumi.audit import AuditLog
from lumi.cloud import CloudError
from lumi.gui.settings import SettingsManager
from lumi.paths import state_home
from tests.test_cloud import fake  # noqa: F401 (the fixture)

URL = "https://cloud.example.test"
OTHER = "https://other.example.test"
ENDPOINT = URL + "/api/v1/feedback"
TOKEN = "ghp_" + "a1B2" * 9  # a GitHub token's shape
SAVED_KEY = "sk-test-saved-provider-key-0123456789"
FORM = {"kind": "bug", "message": "The build button does nothing.", "reply_to": "", "include_diagnostics": False}
NOW = 1_800_000_000.0
UUID4 = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}")


class Cloud:
    """The parts of CloudClient feedback uses: its address, the sign-in's, as whom, and the token."""

    def __init__(self, url: str = URL, *, user_id: str = "", email: str = "ada@example.com",
                 account_url: str | None = None) -> None:
        self.url = url
        self.user_id = user_id
        self.email = email
        self.account_url = url if account_url is None else account_url
        self.token_calls = 0
        self.refreshes = 0
        self.token = f"access-for-{user_id}"
        self.refreshed_at = "2026-09-27T10:00:00.000Z"
        self.destinations: list[str] = []  # where each token asked for was to go

    def status(self) -> dict:
        signed_in = bool(self.user_id)
        account = {"user_id": self.user_id, "email": self.email, "refreshed_at": self.refreshed_at}
        return {"signed_in": signed_in, "account": account if signed_in else {}}

    def account_token(self, destination: str, *, user_id: str = "", refused: str = "") -> str:
        """As CloudClient's: only for the Lumi Cloud that issued the sign-in, and only as whom is signed in."""
        from lumi.cloud import same_address

        self.token_calls += 1
        self.destinations.append(destination)
        if not self.user_id or not same_address(destination, self.account_url):
            raise CloudError("Sign in to Lumi Cloud first.", code="signed_out")
        if user_id and user_id != self.user_id:
            raise CloudError("Someone else is signed in to that Lumi Cloud now.", code="signed_out")
        if refused and refused == self.token:
            self.refreshes += 1
            self.token = f"access-for-{self.user_id}-{self.refreshes}"
        return self.token


class Inbox:
    """Lumi Cloud's feedback endpoint: 201 acknowledging the report's key, unless an answer is lined up."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.answers: list = []
        self.info_requests: list[httpx.Request] = []
        self.info = {"accepting": True, "operator": "Luminary Analytics"}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/feedback/info"):
            self.info_requests.append(request)
            return httpx.Response(200, json=self.info)
        self.requests.append(request)
        if self.answers:
            answer = self.answers.pop(0)
            if isinstance(answer, BaseException):
                raise answer
            if callable(answer):
                return answer(request)
            status, body, headers = answer
            if body == "ack":
                body = self.ack(request)
            return httpx.Response(status, json=body, headers=headers or {}) if not isinstance(body, str) else \
                httpx.Response(status, text=body, headers=headers or {})
        return httpx.Response(201, json=self.ack(request))

    def ack(self, request: httpx.Request) -> dict:
        return {"id": f"fbk_{len(self.requests):04d}", "report": request.headers.get("idempotency-key", ""),
                "account": "authorization" in request.headers}

    def bodies(self) -> list[dict]:
        return [json.loads(request.content) for request in self.requests]

    def keys(self) -> list[str]:
        return [request.headers.get("idempotency-key", "") for request in self.requests]


class Clock:
    """Monotonic time for backoff and previews, turned by hand."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def inbox():
    answering = Inbox()
    feedback.set_transport_for_tests(httpx.MockTransport(answering))
    return answering


@pytest.fixture
def clock():
    turned = Clock()
    feedback.set_clock_for_tests(turned, run="run-1")
    return turned


@pytest.fixture
def audit_log(tmp_path):
    log = AuditLog(tmp_path / "audit")
    audit.set_for_tests(log)
    return log


@pytest.fixture
def settings():
    manager = SettingsManager(path=state_home() / "settings.json")
    manager.set("api_keys", "openai", SAVED_KEY)
    return manager


def records(log: AuditLog, prefix: str = "feedback.") -> list[dict]:
    return [record for path in sorted(log.root.glob("*.jsonl")) for line in path.read_text(encoding="utf-8").splitlines()
            if (record := json.loads(line))["type"].startswith(prefix)]


def form(**changes) -> dict:
    return {**FORM, **changes}


def queue() -> list[dict]:
    path = state_home() / "feedback" / "queue.json"
    return json.loads(path.read_text(encoding="utf-8"))["items"] if path.exists() else []


def edit_queue(change) -> None:
    path = state_home() / "feedback" / "queue.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    change(data["items"])
    path.write_text(json.dumps(data), encoding="utf-8")


def down() -> httpx.ConnectError:
    return httpx.ConnectError("connection refused")


def _policy(settings: dict | None = None, **sections) -> None:
    lumi_policy.set_for_tests(lumi_policy.parse({"schema": "lumi.policy/v1", "organization": "Acme",
                                                 "settings": settings or {}, **sections}, source="test policy"))


def _dlp(**detectors) -> None:
    lumi_policy.set_for_tests(lumi_policy.parse({
        "schema": "lumi.policy/v1", "organization": "Acme",
        "dlp": {"version": 1, "detectors": detectors,
                "rules": [{"name": "falcon", "keywords": ["Project Falcon"], "action": "block", "scope": ["prompt"]}]},
    }, source="test policy"))


# ── The report and the contract ────────────────────────────────────────────


def test_a_report_carries_exactly_what_the_contract_names(inbox, audit_log):
    outcome = feedback.submit(Cloud(), form(reply_to="Ada@Example.COM"), now=NOW)

    assert (outcome.status, outcome.id) == ("sent", "fbk_0001")
    assert "fbk_0001" in outcome.message
    [request] = inbox.requests
    assert (request.method, str(request.url)) == ("POST", ENDPOINT)
    assert request.headers["content-type"] == "application/json"
    assert UUID4.fullmatch(request.headers["idempotency-key"])
    assert "authorization" not in request.headers  # nobody is signed in
    body = json.loads(request.content)
    assert set(body) == {"kind", "message", "reply_to", "app", "install_id", "diagnostics"}
    assert (body["kind"], body["message"], body["diagnostics"]) == ("bug", "The build button does nothing.", None)
    assert body["reply_to"] == "Ada@example.com"  # the domain in lower case, the name as written
    assert set(body["app"]) == {"version", "channel", "os", "arch"}
    assert body["app"]["version"] == __version__ and body["app"]["channel"] in ("stable", "beta")
    assert re.fullmatch(r"[A-Za-z0-9 ._()-]{1,60}", body["app"]["os"])
    assert re.fullmatch(r"[a-z0-9_.-]{1,20}", body["app"]["arch"])
    assert feedback.INSTALL_ID.fullmatch(body["install_id"])
    [sent] = records(audit_log)
    assert sent["type"] == "feedback.sent"
    assert sent["data"] == {"kind": "bug", "size": len(request.content), "diagnostics": False, "queued": False,
                            "attributed": False}


@pytest.mark.parametrize("answer", [
    (200, "<html><body>Sign in to the hotel wifi</body></html>", None),  # a captive portal's page
    (200, {"id": "fbk_1"}, None),  # no acknowledgment of this report
    (201, {"id": "fbk_1", "report": "someone-elses"}, None),
    (202, "ack", None), (204, None, None), (302, {}, {"Location": "https://elsewhere.example/"}),
])
def test_only_lumi_clouds_acknowledgment_of_this_report_counts_as_delivered(inbox, clock, answer):
    inbox.answers = [answer]
    outcome = feedback.submit(Cloud(), form(), now=NOW)
    assert (outcome.status, outcome.reason) == ("queued", "unconfirmed")
    [item] = queue()
    assert item["state"] == "waiting" and len(inbox.requests) == 1  # no redirect followed
    # Tried again with the same key, which Lumi Cloud keeps once: a replay's 200 acknowledges it.
    inbox.answers = [(200, "ack", None)]
    assert feedback.flush(Cloud(), force=True, now=NOW + 1)["sent"] == 1
    assert inbox.keys()[0] == inbox.keys()[1] == item["id"] and queue() == []


def test_each_report_has_its_own_key(inbox):
    feedback.submit(Cloud(), form(), now=NOW)
    feedback.submit(Cloud(), form(), now=NOW)
    first, second = inbox.keys()
    assert first != second and UUID4.fullmatch(first) and UUID4.fullmatch(second)


def test_install_ids_are_random_and_never_link_signed_out_reports_to_an_account(tmp_path, monkeypatch):
    secret = feedback._install_secret()
    assert (state_home() / "feedback" / "install-id").read_text(encoding="ascii") == secret
    anonymous = feedback.install_id(URL, "")
    # Stable for one destination signed out, so staff can tell reports came from one install...
    assert feedback.install_id(URL, "") == anonymous and feedback.INSTALL_ID.fullmatch(anonymous)
    # ...but another for each account, and for each destination: nothing ties them together without the secret.
    ids = {anonymous, feedback.install_id(URL, "usr_ada"), feedback.install_id(URL, "usr_bob"),
           feedback.install_id(OTHER, ""), feedback.install_id("", "")}
    assert len(ids) == 5 and secret not in ids
    # Another install (another home) has another secret.
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path / "other"))
    assert feedback.install_id(URL, "") != anonymous
    # A damaged secret is replaced, never sent.
    (state_home() / "feedback" / "install-id").write_text("not a secret!", encoding="ascii")
    assert feedback.INSTALL_ID.fullmatch(feedback._install_secret())


def test_the_signed_in_and_signed_out_reports_of_one_install_carry_different_ids(inbox):
    feedback.submit(Cloud(), form(), now=NOW)
    feedback.submit(Cloud(user_id="usr_ada"), form(), now=NOW)
    anonymous, signed_in = (body["install_id"] for body in inbox.bodies())
    assert anonymous != signed_in
    assert "authorization" not in inbox.requests[0].headers and "authorization" in inbox.requests[1].headers


@pytest.mark.parametrize("changes, field, complaint", [
    ({"kind": "praise"}, "kind", "Choose Bug, Idea or Other."),
    ({"kind": None}, "kind", "Choose Bug, Idea or Other."),
    ({"message": ""}, "message", "Write what happened"),
    ({"message": " \n\t "}, "message", "Write what happened"),
    ({"message": chr(0) + chr(0x202E)}, "message", "Write what happened"),
    ({"message": "x" * 5001}, "message", "Keep the message to 5,000 characters."),
    ({"reply_to": "ada"}, "reply_to", "Enter an email address"),
    ({"reply_to": "ada@example"}, "reply_to", "Enter an email address"),
    ({"reply_to": "a..b@example.com"}, "reply_to", "Enter an email address"),
    ({"reply_to": ".ada@example.com"}, "reply_to", "Enter an email address"),
    ({"reply_to": "ada.@example.com"}, "reply_to", "Enter an email address"),
    ({"reply_to": "ada@example.com?cc=eve@example.com"}, "reply_to", "Enter an email address"),
    ({"reply_to": "ada eve@example.com"}, "reply_to", "Enter an email address"),
    ({"reply_to": "ada@@example.com"}, "reply_to", "Enter an email address"),
    ({"reply_to": "ad" + chr(0xE0) + "@example.com"}, "reply_to", "Enter an email address"),
    ({"reply_to": "a" * 65 + "@example.com"}, "reply_to", "Enter an email address"),
    ({"reply_to": "ada@" + "e" * 250 + ".com"}, "reply_to", "Enter an email address"),
])
def test_the_form_is_checked_before_anything_is_built(inbox, changes, field, complaint):
    with pytest.raises(feedback.FeedbackError) as refused:
        feedback.submit(Cloud(), form(**changes), now=NOW)
    assert (refused.value.code, refused.value.field) == ("invalid", field)
    assert complaint in refused.value.message
    assert inbox.requests == [] and queue() == []


def test_characters_are_counted_as_sent():
    emoji = chr(0x1F600)
    # 5,000 emoji are 5,000 characters (not 10,000 UTF-16 units); control characters and edge space don't count.
    assert feedback.Form.read(form(message=emoji * 5000)).message == emoji * 5000
    assert feedback.Form.read(form(message="  " + "x" * 5000 + chr(0x202E) + "\n ")).message == "x" * 5000


def test_text_is_sent_as_seen():
    rlo, lri, pdi, lrm = chr(0x202E), chr(0x2066), chr(0x2069), chr(0x200E)
    text = f"  Line one\r\nline{chr(0)} two{chr(7)}\rtabs\tstay {rlo}evil{pdi} {lri}x {lrm}mark{chr(0x2028)}end \n"
    assert feedback.clean_text(text) == f"Line one\nline two\ntabs\tstay evil x {lrm}mark\nend"
    # A lone surrogate can't be encoded; it's dropped rather than breaking the report.
    assert feedback.clean_text("a" + chr(0xD800) + "b") == "ab"
    assert feedback.Form.read(form(include_diagnostics="true")).diagnostics is False  # only a real true


def test_reports_are_kept_within_lumi_clouds_byte_limits(settings):
    # Three-byte characters: the log tail as written would be far over 32 KB of diagnostics.
    wide = chr(0x4E2D)
    body = {"kind": "bug", "message": "m", "reply_to": None, "app": feedback.app_info(), "install_id": "i" * 32,
            "diagnostics": {"log_tail": "\n".join(wide * 200 for _ in range(60))}}
    checked = feedback._checked(body, settings)
    diagnostics = checked.body["diagnostics"]
    assert feedback._diagnostics_bytes(diagnostics) <= feedback.MAX_DIAGNOSTICS_BYTES
    assert feedback._size(checked.body) <= feedback.MAX_BODY_BYTES
    assert diagnostics["log_tail"].endswith(wide * 200)  # the newest lines stay
    assert any("oldest lines of the log" in notice for notice in checked.notices)
    # A message that is too large on its own isn't sent at all.
    with pytest.raises(feedback.FeedbackError) as refused:
        feedback._fit({**body, "message": wide * 30000, "diagnostics": None})
    assert (refused.value.code, refused.value.field) == ("invalid", "message")


# ── What never leaves ──────────────────────────────────────────────────────


def test_secrets_are_removed_even_with_the_scan_off_in_settings(inbox, settings):
    assert settings.get("privacy", "secret_scan", False) is False
    message = f"It failed with {TOKEN}, and my key {SAVED_KEY} is in the log."
    outcome = feedback.submit(Cloud(), form(message=message), settings=settings, now=NOW)
    assert outcome.status == "sent"
    sent = inbox.requests[0].content.decode()
    assert TOKEN not in sent and SAVED_KEY not in sent
    assert "[REDACTED GitHub token]" in sent and "[REDACTED saved API key]" in sent
    assert outcome.notices[0].startswith("Removed 2 secrets")  # said with the outcome, not only in a preview


def test_a_reply_to_address_that_looks_like_a_secret_is_left_out(inbox, settings):
    outcome = feedback.submit(Cloud(), form(message=f"key {SAVED_KEY}", reply_to=f"{SAVED_KEY}@example.com"),
                              settings=settings, now=NOW)
    assert inbox.bodies()[-1]["reply_to"] is None
    assert any("reply-to address looked like a secret" in notice for notice in outcome.notices)
    feedback.submit(Cloud(), form(reply_to=f"{TOKEN}@example.com"), settings=settings, now=NOW + 1)
    assert inbox.bodies()[-1]["reply_to"] is None
    assert TOKEN not in json.dumps(inbox.bodies()) and SAVED_KEY not in json.dumps(inbox.bodies())


def test_a_message_redaction_made_too_long_isnt_sent(inbox, monkeypatch):
    monkeypatch.setattr(feedback, "MAX_SENT_MESSAGE", 30)
    with pytest.raises(feedback.FeedbackError) as refused:
        feedback.submit(Cloud(), form(message="x" * 25 + " " + TOKEN), now=NOW)
    assert (refused.value.code, refused.value.field) == ("invalid", "message")
    assert "too long to send" in refused.value.message and inbox.requests == []


def test_dlp_blocks_redacts_and_keeps_out_what_it_would_change(inbox, audit_log):
    _dlp(credit_card="redact", email="redact")
    with pytest.raises(feedback.FeedbackError) as refused:
        feedback.submit(Cloud(), form(message="Project Falcon broke"), now=NOW)
    assert refused.value.code == "dlp" and "falcon in your message" in refused.value.message
    assert "Project Falcon" not in refused.value.message and refused.value.copy == ""
    assert inbox.requests == []

    outcome = feedback.submit(Cloud(), form(message="Card 4111 1111 1111 1111 was charged twice",
                                            reply_to="ada@example.com"), now=NOW)
    assert outcome.status == "sent"
    body = inbox.bodies()[0]
    assert body["message"] == "Card [REDACTED:credit_card] was charged twice"
    assert body["reply_to"] is None  # DLP would change the address, so it stays here
    # Said with the outcome: the reply-to left out, and the redaction.
    assert any("reply-to address leave this computer" in notice for notice in outcome.notices)
    assert any("redacted part of the report" in notice for notice in outcome.notices)

    audit_text = json.dumps(records(audit_log, ""))
    assert "4111" not in audit_text and "Project Falcon" not in audit_text and "ada@example.com" not in audit_text
    findings = [r["data"] for r in records(audit_log, "dlp.finding")]
    assert {(f["rule"], f["action"], f["purpose"]) for f in findings} >= {
        ("falcon", "block", "feedback"), ("credit_card", "redact", "feedback")}
    assert [r["data"]["reason"] for r in records(audit_log, "feedback.refused")] == ["dlp"]


def test_dlp_checks_diagnostics_as_mixed_content_every_rule_sees(inbox, settings):
    # A rule scoped to prompts still checks the log: it can quote the conversation.
    _dlp(credit_card="block")
    _write_log("Charged card 4111 1111 1111 1111 at startup\n")
    with pytest.raises(feedback.FeedbackError) as refused:
        feedback.preview(Cloud(), form(include_diagnostics=True), settings=settings)
    assert refused.value.code == "dlp" and "credit_card in the conversation" in refused.value.message
    _write_log("Project Falcon was mentioned in a reply\n")
    with pytest.raises(feedback.FeedbackError, match="falcon in the conversation"):
        feedback.preview(Cloud(), form(include_diagnostics=True), settings=settings)
    assert inbox.requests == []


def test_a_policy_lumi_cant_use_refuses_feedback_too(inbox, monkeypatch):
    monkeypatch.setattr(lumi_policy, "blocked_reason", lambda: "Your organization's policy can't be used.")
    with pytest.raises(feedback.FeedbackError) as refused:
        feedback.submit(Cloud(), form(), now=NOW)
    assert refused.value.code == "dlp" and "can't be used" in refused.value.message
    assert inbox.requests == []


def test_send_checks_the_reviewed_report_again_with_the_rules_in_force(inbox, settings, audit_log):
    shown = feedback.preview(Cloud(), form(message="Project Falcon broke", include_diagnostics=True),
                             settings=settings)
    _dlp()  # the organization's rules arrive after the preview
    with pytest.raises(feedback.FeedbackError) as refused:
        feedback.submit(Cloud(), form(message="Project Falcon broke", include_diagnostics=True),
                        settings=settings, preview_id=shown["preview_id"], now=NOW)
    assert refused.value.code == "dlp" and inbox.requests == []
    assert [r["data"]["reason"] for r in records(audit_log, "feedback.refused")] == ["dlp"]


def test_a_policy_that_became_unusable_after_the_review_stops_it(inbox, settings, monkeypatch):
    shown = feedback.preview(Cloud(), form(include_diagnostics=True), settings=settings)
    monkeypatch.setattr(lumi_policy, "blocked_reason", lambda: "Acme's policy expired.")
    with pytest.raises(feedback.FeedbackError, match="expired") as refused:
        feedback.submit(Cloud(), form(include_diagnostics=True), settings=settings,
                        preview_id=shown["preview_id"], now=NOW)
    assert refused.value.code == "dlp" and inbox.requests == []


def _dlp_service(answer) -> list[dict]:
    seen: list[dict] = []

    def service(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(200, json=answer(seen[-1]) if callable(answer) else answer)

    dlp.set_transport_for_tests(httpx.MockTransport(service))
    lumi_policy.set_for_tests(lumi_policy.parse({"schema": "lumi.policy/v1", "organization": "Acme", "dlp": {
        "version": 1, "service": {"url": "https://dlp.acme.test/check"}}}, source="test"))
    return seen


def test_the_dlp_service_sees_a_report_only_when_it_is_sent(inbox, settings):
    seen = _dlp_service({"action": "allow"})
    _write_log("startup MARKER-7\n")
    shown = feedback.preview(Cloud(), form(message="draft one", include_diagnostics=True), settings=settings)
    assert seen == [] and shown["provisional"] is True  # a draft: only the rules on this computer
    assert feedback.submit(Cloud(), form(message="draft one", include_diagnostics=True), settings=settings,
                           preview_id=shown["preview_id"], now=NOW).status == "sent"
    texts = [item["text"] for payload in seen for item in payload["items"]]
    assert "draft one" in texts and any("MARKER-7" in text for text in texts)
    assert {payload["purpose"] for payload in seen} == {"feedback"}


def test_a_report_the_dlp_service_changes_at_send_is_reviewed_again(inbox, settings):
    _dlp_service(lambda payload: {"action": "redact", "redactions": ["Project Falcon"]}
                 if any("Project Falcon" in item["text"] for item in payload["items"]) else {"action": "allow"})
    typed = form(message="Project Falcon broke", include_diagnostics=True)
    shown = feedback.preview(Cloud(), typed, settings=settings)
    assert shown["body"]["message"] == "Project Falcon broke"
    with pytest.raises(feedback.FeedbackError) as changed:
        feedback.submit(Cloud(), typed, settings=settings, preview_id=shown["preview_id"], now=NOW)
    assert changed.value.code == "review" and inbox.requests == []
    again = changed.value.preview
    assert "Project Falcon" not in again["body"]["message"] and again["provisional"] is False
    # What was shown the second time is what goes.
    assert feedback.submit(Cloud(), typed, settings=settings, preview_id=again["preview_id"],
                           now=NOW + 1).status == "sent"
    assert inbox.bodies()[0] == again["body"]


def test_offline_mode_refuses_unless_the_host_is_allowed(inbox, audit_log):
    offline.set_for_tests(enabled=True)
    with pytest.raises(feedback.FeedbackError) as refused:
        feedback.submit(Cloud(), form(), now=NOW)
    assert refused.value.code == "offline"
    assert refused.value.message.startswith("Offline mode: sending feedback needs cloud.example.test")
    assert inbox.requests == [] and queue() == []
    # Nowhere to send it offline: refused, not kept to leave later.
    with pytest.raises(feedback.FeedbackError) as nowhere:
        feedback.submit(Cloud(url=""), form(), now=NOW)
    assert nowhere.value.code == "offline" and queue() == []
    assert feedback.status(Cloud())["offline"].startswith("Offline mode:")
    with pytest.raises(feedback.FeedbackError) as shown:
        feedback.preview(Cloud(), form(include_diagnostics=True))
    assert shown.value.code == "offline"

    offline.set_for_tests(enabled=True, allowed_hosts=("cloud.example.test",))
    assert feedback.submit(Cloud(), form(), now=NOW).status == "sent"
    assert [r["data"]["reason"] for r in records(audit_log, "feedback.refused")] == ["offline", "offline"]


def test_offline_mode_refuses_before_anything_looks_and_the_copy_is_only_what_was_typed(inbox, settings, audit_log):
    seen = _dlp_service({"action": "allow"})
    _write_log("startup MARKER-4242\n")
    offline.set_for_tests(enabled=True, allowed_hosts=("dlp.acme.test",))
    typed = form(message=f"Crashed with {TOKEN}", reply_to="ada@example.com", include_diagnostics=True)
    [shown] = _command("feedback_preview", Cloud(), settings, request=1, form=typed)
    [sent, _status] = _command("feedback_send", Cloud(), settings, form=typed)
    for answer in (shown, sent):
        copy = answer["copy_text"]
        assert copy.startswith("Lumi feedback: Bug\n\nCrashed with [REDACTED GitHub token]\n")
        assert "Reply to: ada@example.com" in copy
        assert "Diagnostics" not in copy and "MARKER-4242" not in copy and TOKEN not in copy
    assert (shown["error"]["code"], sent["code"]) == ("offline", "offline")
    assert seen == [] and records(audit_log, "dlp.") == []  # neither the service nor a rule looked
    assert inbox.requests == []


def test_offline_mode_turned_on_while_reports_wait_keeps_them(inbox, clock):
    inbox.answers = [down()]
    feedback.submit(Cloud(), form(), now=NOW)
    offline.set_for_tests(enabled=True)
    assert feedback.flush(Cloud(), force=True, now=NOW + 1) == {"sent": 0, "held": 0, "waiting": 1}
    assert len(inbox.requests) == 1
    offline.set_for_tests(enabled=False)
    assert feedback.flush(Cloud(), force=True, now=NOW + 2)["sent"] == 1


# ── The organization's switches ────────────────────────────────────────────


def test_an_organization_can_turn_feedback_off(inbox, settings, audit_log, clock):
    inbox.answers = [down()]
    feedback.submit(Cloud(), form(), settings=settings, now=NOW)  # one waits from before
    _policy({"privacy.feedback": "off"})
    status = feedback.status(Cloud(), settings)
    assert status["disabled"] == "Your organization turned off sending feedback from Lumi."
    for attempt in (lambda: feedback.submit(Cloud(), form(), settings=settings, now=NOW),
                    lambda: feedback.preview(Cloud(), form(include_diagnostics=True), settings=settings)):
        with pytest.raises(feedback.FeedbackError) as refused:
            attempt()
        assert refused.value.code == "disabled"
    # Waiting reports don't go either; the person can still discard them.
    assert feedback.flush(Cloud(), settings, force=True, now=NOW + 1)["disabled"]
    assert len(inbox.requests) == 1 and feedback.discard() == 1
    assert feedback.info(Cloud(), settings) == {} and inbox.info_requests == []
    # A person can turn it off for themselves too.
    lumi_policy.set_for_tests(None)
    settings.set("privacy", "feedback", "off")
    assert "privacy.feedback" in feedback.status(Cloud(), settings)["disabled"]


def test_an_organization_can_keep_diagnostics_out(inbox, settings):
    _policy({"privacy.feedback_diagnostics": "never"})
    status = feedback.status(Cloud(), settings)
    assert status["diagnostics"] == {"allowed": False,
                                     "reason": "Your organization doesn't allow diagnostics in feedback."}
    with pytest.raises(feedback.FeedbackError) as refused:
        feedback.preview(Cloud(), form(include_diagnostics=True), settings=settings)
    assert (refused.value.code, refused.value.field) == ("diagnostics_off", "include_diagnostics")
    assert feedback.submit(Cloud(), form(), settings=settings, now=NOW).status == "sent"  # without them it goes


@pytest.mark.parametrize("name, value, complaint", [
    ("privacy.feedback", "no", "\"on\" or \"off\""),
    ("privacy.feedback", False, "\"on\" or \"off\""),
    ("privacy.feedback_diagnostics", "sometimes", "\"allowed\" or \"never\""),
    ("privacy.feedback_url", "http://feedback.example.com", "https"),
    ("privacy.feedback_url", "https://ada:pw@feedback.example.com", "user name or password"),
    ("privacy.feedback_url", 7, "a Lumi Cloud address"),
])
def test_the_feedback_settings_a_policy_locks_are_checked(name, value, complaint):
    with pytest.raises(lumi_policy.PolicyError, match=re.escape(complaint)):
        _policy({name: value})


# ── Where a report goes, and as whom ───────────────────────────────────────


def test_the_feedback_address_the_build_the_person_or_the_organization_chose(inbox, settings, monkeypatch):
    assert feedback.destination(Cloud(), settings) == feedback.Destination(URL, "cloud")
    monkeypatch.setattr(feedback, "BUILD_DESTINATION", "https://feedback.luminary.test")
    assert feedback.destination(Cloud(), settings) == feedback.Destination("https://feedback.luminary.test", "build")
    settings.set("privacy", "feedback_url", OTHER)
    assert feedback.destination(Cloud(), settings) == feedback.Destination(OTHER, "setting")
    _policy({"privacy.feedback_url": "https://inbox.acme.test"})
    assert feedback.destination(Cloud(), settings) == feedback.Destination("https://inbox.acme.test", "policy")
    feedback.submit(Cloud(user_id="usr_ada"), form(), settings=settings, now=NOW)
    request = inbox.requests[-1]
    assert str(request.url) == "https://inbox.acme.test/api/v1/feedback"
    assert "authorization" not in request.headers  # the sign-in is cloud.example.test's
    assert feedback.status(Cloud(user_id="usr_ada"), settings)["source"] == "policy"


def test_the_account_goes_only_while_the_person_is_signed_in(inbox, audit_log, monkeypatch, clock):
    monkeypatch.setattr(feedback, "MAX_RECENT", 100)
    anonymous = Cloud()
    feedback.submit(anonymous, form(), now=NOW)
    assert "authorization" not in inbox.requests[-1].headers and anonymous.token_calls == 0

    ada = Cloud(user_id="usr_ada")
    feedback.submit(ada, form(), now=NOW)
    assert inbox.requests[-1].headers["authorization"] == "Bearer access-for-usr_ada"
    assert records(audit_log)[-1]["data"]["attributed"] is True

    # Written while signed out, while Lumi Cloud was down: never attributed later, even once someone signs in.
    inbox.answers = [down()]
    feedback.submit(anonymous, form(), now=NOW)
    feedback.flush(ada, force=True, now=NOW + 1)
    assert "authorization" not in inbox.requests[-1].headers
    # Written as Ada: sent only as Ada. While Bob is the one signed in, it waits for her, and never goes as Bob
    # or without an account.
    inbox.answers = [down()]
    feedback.submit(ada, form(), now=NOW)
    sent_before = len(inbox.requests)
    assert feedback.flush(Cloud(user_id="usr_bob"), force=True, now=NOW + 2)["sent"] == 0
    assert len(inbox.requests) == sent_before and queue()[-1]["state"] == "sign_in"
    assert feedback.flush(ada, force=True, now=NOW + 3)["sent"] == 1
    assert inbox.requests[-1].headers["authorization"] == "Bearer access-for-usr_ada"


def test_written_signed_in_it_waits_for_its_writer_and_goes_without_the_account_only_by_choice(inbox, clock):
    ada = Cloud(user_id="usr_ada")
    inbox.answers = [down()]
    feedback.submit(ada, form(), now=NOW)
    ada.user_id = ""  # signed out meanwhile
    assert feedback.flush(ada, force=True, now=NOW + 1)["sent"] == 0  # Send now: it waits for Ada
    [item] = queue()
    assert (item["state"], item["account"], item["email"]) == ("sign_in", "usr_ada", "ada@example.com")
    [report] = feedback.status(ada)["reports"]
    assert (report["writer"], report["without_account"], report["here"]) == ("ada@example.com", True, True)
    assert feedback.status(ada)["sendable"] == 0
    # The person's choice, labelled as such: it then goes without the account, with the signed-out install id.
    assert feedback.send_without_account(ada, None, item["id"], now=NOW + 2)["sent"] == 1
    request = inbox.requests[-1]
    assert "authorization" not in request.headers
    assert json.loads(request.content)["install_id"] == feedback.install_id(URL, "")
    assert json.loads(inbox.requests[0].content)["install_id"] == feedback.install_id(URL, "usr_ada")
    assert request.headers["idempotency-key"] == item["id"] and queue() == []
    with pytest.raises(feedback.FeedbackError) as gone:
        feedback.send_without_account(ada, None, item["id"])
    assert gone.value.code == "gone"


def test_sent_without_the_account_after_a_try_with_it_arrived_is_the_same_report(inbox, monkeypatch):
    """Lumi Cloud keeps one report per key, whatever install id a try carries: the choice makes no second copy."""
    ada = Cloud(user_id="usr_ada")
    inbox.answers = [httpx.ReadTimeout("the report arrived; its answer didn't")]
    feedback.submit(ada, form(), now=NOW)
    [item] = queue()
    ada.user_id = ""
    # Lumi Cloud's answer to the key it has: the first report's id, and no account for another install id.
    inbox.answers = [(200, {"id": "fbk_first", "report": item["id"], "account": False}, None)]
    records = []
    monkeypatch.setattr(feedback, "_record", lambda event, body, **extra: records.append((event, extra)))
    assert feedback.send_without_account(ada, None, item["id"], now=NOW + 2)["sent"] == 1
    first, again = inbox.requests
    assert first.headers["idempotency-key"] == again.headers["idempotency-key"] == item["id"]
    assert json.loads(first.content)["install_id"] != json.loads(again.content)["install_id"]
    assert queue() == [] and ("feedback.sent", {"queued": True, "attributed": False}) in records


def test_a_sign_in_that_ended_meanwhile_keeps_the_report_for_its_writer(inbox, audit_log):
    class Ended(Cloud):
        def account_token(self, destination: str, *, user_id: str = "", refused: str = "") -> str:
            raise CloudError("Your Lumi Cloud sign-in ended. Sign in again.", code="signed_out")

    outcome = feedback.submit(Ended(user_id="usr_ada"), form(), now=NOW)
    assert (outcome.status, outcome.reason) == ("queued", "sign_in")
    assert inbox.requests == []  # never sent without the account the person wrote it with
    assert queue()[0]["account"] == "usr_ada"


def test_a_token_that_cant_be_had_now_keeps_the_report(inbox):
    class Flaky(Cloud):
        def account_token(self, destination: str, *, user_id: str = "", refused: str = "") -> str:
            raise CloudError("Lumi Cloud answered 503.", code="503")

    outcome = feedback.submit(Flaky(user_id="usr_ada"), form(), now=NOW)
    assert (outcome.status, outcome.reason) == ("queued", "unreachable")
    assert inbox.requests == []  # never sent without the account the person expected
    assert queue()[0]["account"] == "usr_ada"


def test_a_refused_token_is_refreshed_once_where_it_was_issued(inbox, audit_log):
    ada = Cloud(user_id="usr_ada")
    inbox.answers = [(401, {"error": "invalid_token"}, {"WWW-Authenticate": 'Bearer error="invalid_token"'})]
    assert feedback.submit(ada, form(), now=NOW).status == "sent"
    assert ada.refreshes == 1
    assert [request.headers["authorization"] for request in inbox.requests] == [
        "Bearer access-for-usr_ada", "Bearer access-for-usr_ada-1"]
    assert inbox.keys()[0] == inbox.keys()[1]  # the same report, tried again
    assert records(audit_log)[-1]["data"]["attributed"] is True


def test_a_token_refused_even_after_a_refresh_waits_for_a_new_sign_in(inbox, clock):
    ada = Cloud(user_id="usr_ada")
    refused = (401, {"error": "invalid_token"}, None)
    inbox.answers = [refused, refused]
    outcome = feedback.submit(ada, form(), now=NOW)
    assert (outcome.status, outcome.reason) == ("queued", "sign_in") and "Sign in again" in outcome.message
    assert all("authorization" in request.headers for request in inbox.requests)  # never sent anonymously instead
    [item] = queue()
    assert item["state"] == "sign_in"
    # Not before the person signs in again...
    assert feedback.flush(ada, now=NOW + 1)["sent"] == 0 and len(inbox.requests) == 2
    clock.advance(100_000)
    assert feedback.flush(ada, now=NOW + 2)["sent"] == 0
    # ...then at once.
    ada.refreshed_at = "2026-09-28T09:00:00.000Z"
    assert feedback.flush(ada, now=NOW + 3)["sent"] == 1
    assert inbox.requests[-1].headers["authorization"].startswith("Bearer access-for-usr_ada")


def test_the_account_goes_only_to_the_lumi_cloud_that_issued_the_sign_in(inbox):
    # A policy has since named another Lumi Cloud: reports go there, without the account or its token.
    moved = Cloud(url=OTHER, user_id="usr_ada", account_url=URL)
    feedback.submit(moved, form(), now=NOW)
    assert str(inbox.requests[-1].url) == OTHER + "/api/v1/feedback"
    assert "authorization" not in inbox.requests[-1].headers and moved.token_calls == 0
    assert feedback.status(moved)["account"] == ""


def test_the_real_cloud_client_sends_its_access_token_only_when_signed_in(inbox, request):
    from tests.test_cloud import _client, _sign_in

    cloud_fake = request.getfixturevalue("fake")  # test_cloud's isolation: no machine policy
    client = _client(cloud_fake)
    client.settings.update_section("cloud", {"url": URL})
    feedback.submit(client, form(), now=NOW)
    assert "authorization" not in inbox.requests[-1].headers
    _sign_in(client, cloud_fake)
    assert client.account_url == URL
    feedback.submit(client, form(), now=NOW + 1)
    token = inbox.requests[-1].headers["authorization"][len("Bearer "):]
    assert token in cloud_fake.access_tokens
    assert client.settings.get("api_keys", "lumi_cloud_refresh") not in inbox.requests[-1].content.decode()


@pytest.mark.parametrize("address", ["http://cloud.example.test", "https://cloud.example.test:abc",
                                     "https://ada:pw@cloud.example.test"])
def test_an_address_lumi_wouldnt_use_isnt_used(inbox, address):
    # Plain http to another computer would carry the report in the clear; a port that isn't one can't
    # connect; a user name would go with every request.
    outcome = feedback.submit(Cloud(url=address), form(), now=NOW)
    assert (outcome.status, outcome.reason) == ("queued", "no_destination") and inbox.requests == []


# ── Waiting on this computer ───────────────────────────────────────────────


def test_a_report_written_with_no_address_waits_until_the_person_sends_it_somewhere(inbox, audit_log, clock):
    outcome = feedback.submit(Cloud(url=""), form(), now=NOW)
    assert (outcome.status, outcome.reason) == ("queued", "no_destination")
    assert "goes only when you send it there" in outcome.message
    [item] = queue()
    assert (item["state"], item["url"]) == ("held", "")
    # An address appears later (an employer's policy, say): the report doesn't go there by itself.
    assert feedback.flush(Cloud(), force=True, now=NOW + 1)["sent"] == 0 and inbox.requests == []
    [shown] = feedback.status(Cloud())["reports"]
    assert (shown["reason"], shown["send"], shown["here"]) == ("no_destination", True, False)
    # Sent only to the address the person was shown, and only while it's still the one.
    with pytest.raises(feedback.FeedbackError) as moved:
        feedback.send_held(Cloud(), None, item["id"], OTHER, now=NOW + 2)
    assert moved.value.code == "preview" and inbox.requests == []
    assert feedback.send_held(Cloud(), None, item["id"], URL, now=NOW + 3)["sent"] == 1
    assert inbox.keys() == [item["id"]] and queue() == []
    assert [r["type"] for r in records(audit_log)] == ["feedback.held", "feedback.sent"]


def test_an_unreachable_lumi_cloud_is_retried_with_backoff(inbox, clock):
    inbox.answers = [down()]
    outcome = feedback.submit(Cloud(), form(), now=NOW)
    assert (outcome.status, outcome.reason) == ("queued", "unreachable")
    assert "couldn't be reached" in outcome.message
    [item] = queue()
    assert (item["state"], item["url"], item["run"]) == ("waiting", URL, "run-1")
    assert item["next_mono"] == clock.now + feedback.RETRY_SECONDS

    assert feedback.flush(Cloud(), now=NOW + 10)["sent"] == 0 and len(inbox.requests) == 1  # not due yet
    clock.advance(601)
    inbox.answers = [down()]
    feedback.flush(Cloud(), now=NOW + 601)
    [item] = queue()
    assert item["attempts"] == 2 and item["next_mono"] == clock.now + 2 * feedback.RETRY_SECONDS
    clock.advance(600)
    assert feedback.flush(Cloud(), now=NOW + 1300)["sent"] == 0 and len(inbox.requests) == 2  # not due yet
    # The wait stops doubling at six hours.
    for _ in range(8):
        inbox.answers = [down()]
        feedback.flush(Cloud(), force=True, now=NOW + 2000)
    assert queue()[0]["next_mono"] == clock.now + feedback.MAX_BACKOFF_SECONDS
    assert feedback.flush(Cloud(), force=True, now=NOW + 2001)["sent"] == 1  # Send now doesn't wait
    assert queue() == [] and len(set(inbox.keys())) == 1  # one report, one key, however many tries


def test_lumi_clouds_retry_after_is_clamped_given_jitter_and_honoured_even_by_send_now(inbox, clock, monkeypatch):
    monkeypatch.setattr(feedback.random, "uniform", lambda low, high: high)  # the most jitter it adds
    inbox.answers = [(503, {"error": "down"}, None), (429, {"error": "slow_down"}, {"Retry-After": "120"}),
                     (429, {"error": "slow_down"}, {"Retry-After": "99999"})]
    assert feedback.submit(Cloud(), form(), now=NOW).reason == "unreachable"
    outcome = feedback.submit(Cloud(), form(), now=NOW)
    assert (outcome.status, outcome.reason) == ("queued", "busy") and "busy" in outcome.message
    assert queue()[1]["next_mono"] == clock.now + 120 + 24  # 120 s and up to a fifth more
    feedback.submit(Cloud(), form(), now=NOW)
    assert queue()[2]["next_mono"] == clock.now + 3600 + 60  # at most an hour, and at most a minute more
    # Send now doesn't wait out backoff, but does wait out the server's Retry-After.
    assert feedback.flush(Cloud(), force=True, now=NOW + 1) == {"sent": 1, "held": 0, "waiting": 2}
    clock.advance(145)
    assert feedback.flush(Cloud(), force=True, now=NOW + 2)["sent"] == 1


@pytest.mark.parametrize("value", ["abc", chr(0xB2).encode("latin-1"), "".join(map(chr, (0x661, 0x662, 0x660))).encode(),
                                   "1" * 5000, "-5", "Wed, 21 Oct 2026 07:28:00 GMT"])
def test_a_retry_after_that_isnt_seconds_means_the_usual_wait(inbox, clock, value):
    # Digits other than 0-9 (a superscript two, Arabic-Indic 120) are digits to str.isdigit() and even int().
    inbox.answers = [(429, {"error": "slow_down"}, {"Retry-After": value})]
    outcome = feedback.submit(Cloud(), form(), now=NOW)
    assert outcome.reason == "busy" and queue()[0]["next_mono"] == clock.now + feedback.RETRY_SECONDS


def test_one_unreachable_report_stops_the_round(inbox, clock):
    for _ in range(3):
        inbox.answers = [down()]
        feedback.submit(Cloud(), form(), now=NOW)
    inbox.answers = [down()]
    assert feedback.flush(Cloud(), force=True, now=NOW + 1) == {"sent": 0, "held": 0, "waiting": 3}
    assert len(inbox.requests) == 4


def test_an_unexpected_failure_mid_round_keeps_what_was_already_sent(inbox, clock):
    for n in range(3):
        inbox.answers = [down()]
        feedback.submit(Cloud(), form(message=f"report {n}"), now=NOW)
    inbox.answers = [(201, "ack", None), RuntimeError("a bug")]
    assert feedback.flush(Cloud(), force=True, now=NOW + 1) == {"sent": 1, "held": 0, "waiting": 2}
    # The first isn't sent twice: the next round sends only the other two.
    assert feedback.flush(Cloud(), force=True, now=NOW + 2)["sent"] == 2
    assert [body["message"] for body in inbox.bodies()[3:]] == ["report 0", "report 1", "report 1", "report 2"]


def test_a_damaged_entry_is_dropped_and_never_stops_the_others(inbox, audit_log, clock):
    inbox.answers = [down()]
    feedback.submit(Cloud(), form(message="good report"), now=NOW)
    edit_queue(lambda items: items.insert(0, {"id": "bad", "body": {"kind": "bug"}, "queued_at": "yesterday"}))
    edit_queue(lambda items: items.insert(0, "not even an object"))
    assert feedback.flush(Cloud(), force=True, now=NOW + 1) == {"sent": 1, "held": 0, "waiting": 0}
    assert inbox.bodies()[-1]["message"] == "good report"
    assert [r["data"]["reason"] for r in records(audit_log, "feedback.dropped")] == ["damaged", "damaged"]
    # A file that isn't JSON is set aside, not a crash.
    (state_home() / "feedback" / "queue.json").write_text("{not json", encoding="utf-8")
    assert feedback.waiting() == 0 and list((state_home() / "feedback").glob("queue.damaged.*.json"))


@pytest.mark.parametrize("answer, reason, detail", [
    ((400, {"error": "invalid_request", "error_description": "Keep the message to 8,000 characters."}, None),
     "refused", "Keep the message to 8,000 characters."),
    ((413, {"error": "too_large"}, None), "too_large", "too large"),
    ((404, {"detail": "Not Found"}, None), "not_accepting", "doesn't accept feedback"),
    ((422, {"error": "nope"}, None), "refused", "it answered 422"),
])
def test_what_lumi_cloud_wont_take_is_held_where_the_person_sees_it(inbox, audit_log, clock, answer, reason, detail):
    inbox.answers = [down()]
    feedback.submit(Cloud(), form(message="Kept for you"), now=NOW)
    inbox.answers = [answer]
    assert feedback.flush(Cloud(), force=True, now=NOW + 1) == {"sent": 0, "held": 1, "waiting": 1}
    [item] = queue()
    assert (item["state"], item["reason"]) == ("held", reason) and detail in item["detail"]
    [shown] = feedback.status(Cloud())["reports"]
    assert (shown["reason"], shown["copy"]) == (reason, True)
    assert feedback.held_copy(item["id"]).startswith("Lumi feedback: Bug\n\nKept for you\n")
    # Held: not tried again by itself, nor by Send now, unless the Lumi Cloud may take feedback now (404).
    inbox.answers = [answer]
    feedback.flush(Cloud(), force=True, now=NOW + 2)
    assert len(inbox.requests) == (3 if reason == "not_accepting" else 2)
    assert records(audit_log, "feedback.held")[-1]["data"]["reason"] == reason
    assert feedback.discard([item["id"]]) == 1 and queue() == []


@pytest.mark.parametrize("answer, code, complaint", [
    ((400, {"error": "invalid_request", "error_description": "Keep the message to 8,000 characters."}, None),
     "refused", "Lumi Cloud couldn't take the report: Keep the message to 8,000 characters."),
    ((413, {"error": "too_large"}, None), "too_large", "too large for this Lumi Cloud"),
    ((404, {"detail": "Not Found"}, None), "not_accepting", "This Lumi Cloud doesn't accept feedback."),
])
def test_what_lumi_cloud_wont_take_now_is_said_with_a_copy(inbox, audit_log, answer, code, complaint):
    inbox.answers = [answer]
    with pytest.raises(feedback.FeedbackError) as refused:
        feedback.submit(Cloud(), form(), now=NOW)
    assert refused.value.code == code and complaint in refused.value.message
    assert refused.value.copy.startswith("Lumi feedback: Bug")  # the form keeps it, and it can be copied
    [record] = records(audit_log)
    assert record["type"] == "feedback.refused" and record["data"]["status"] == answer[0]


@pytest.mark.parametrize("status", [401, 403])
def test_a_lumi_cloud_that_wants_a_sign_in_keeps_the_report(inbox, clock, status):
    inbox.answers = [(status, {"error": "invalid_token"}, None)]
    outcome = feedback.submit(Cloud(), form(), now=NOW)
    assert (outcome.status, outcome.reason) == ("queued", "unauthorized")
    assert queue()[0]["state"] == "sign_in"
    inbox.answers = [(status, {"error": "invalid_token"}, None)]
    assert feedback.flush(Cloud(), force=True, now=NOW + 1) == {"sent": 0, "held": 0, "waiting": 1}


def test_waiting_reports_dlp_now_blocks_are_held_not_sent(inbox, audit_log, clock):
    for message in ("fine", "Project Falcon later"):
        inbox.answers = [down()]
        feedback.submit(Cloud(), form(message=message), now=NOW)
    _dlp()  # the organization's rules arrived after these were written
    result = feedback.flush(Cloud(), force=True, now=NOW + 1)
    assert (result["sent"], result["held"]) == (1, 1)
    [item] = queue()
    assert item["reason"] == "dlp" and feedback.held_copy(item["id"]) == ""
    assert feedback.status(Cloud())["reports"][0]["copy"] is False


def test_moving_the_clock_forward_doesnt_wipe_the_queue_and_expiry_holds(inbox, audit_log, clock):
    inbox.answers = [down()]
    feedback.submit(Cloud(), form(), now=NOW)
    inbox.answers = [down()]
    feedback.flush(Cloud(), force=True, now=NOW + 400 * 86400)  # the clock jumped a year and more
    [item] = queue()
    assert item["state"] == "waiting" and item["age"] == feedback.MAX_AGE_STEP  # the jump counts as ten minutes
    # Thirty days of Lumi running (a round every minute) do expire it: held, not deleted.
    edit_queue(lambda items: items[0].update(age=feedback.QUEUE_DAYS * 86400 - 30))
    feedback.flush(Cloud(), force=True, now=NOW + 400 * 86400 + 60)
    [item] = queue()
    assert (item["state"], item["reason"]) == ("held", "expired")
    assert records(audit_log, "feedback.held")[-1]["data"]["reason"] == "expired"


def test_moving_the_clock_back_or_restarting_never_strands_a_busy_report(inbox, clock):
    inbox.answers = [(429, {"error": "slow_down"}, {"Retry-After": "3600"})]
    feedback.submit(Cloud(), form(), now=NOW)
    feedback.flush(Cloud(), force=True, now=NOW - 7 * 86400)  # the clock moved back a week
    assert len(inbox.requests) == 1  # still waiting out Retry-After, by monotonic time
    clock.advance(3700)
    assert feedback.flush(Cloud(), now=NOW - 7 * 86400)["sent"] == 1
    # After a restart (another run), a busy report is tried at the first round, whatever the old deadline.
    inbox.answers = [(429, {"error": "slow_down"}, {"Retry-After": "3600"})]
    feedback.submit(Cloud(), form(), now=NOW)
    feedback.set_clock_for_tests(clock, run="run-2")
    assert feedback.flush(Cloud(), now=NOW)["sent"] == 1


def test_the_queue_is_bounded(inbox, monkeypatch, audit_log):
    monkeypatch.setattr(feedback, "MAX_RECENT", 100)
    for index in range(feedback.MAX_QUEUED):
        feedback.submit(Cloud(url=""), form(message=f"report {index}"), now=NOW)
    with pytest.raises(feedback.FeedbackError) as full:
        feedback.submit(Cloud(url=""), form(), now=NOW)
    assert full.value.code == "queue_full" and full.value.copy.startswith("Lumi feedback: Bug")
    assert full.value.message.startswith("No feedback address is set, and 20 reports are already waiting")
    assert len(queue()) == feedback.MAX_QUEUED
    assert records(audit_log)[-1]["data"]["reason"] == "queue_full"

    assert feedback.discard() == feedback.MAX_QUEUED and queue() == []
    monkeypatch.setattr(feedback, "MAX_QUEUE_BYTES", 600)
    feedback.submit(Cloud(url=""), form(), now=NOW)
    with pytest.raises(feedback.FeedbackError, match="1 report is already waiting"):
        feedback.submit(Cloud(url=""), form(message="x" * 400), now=NOW)


def test_a_person_sends_at_most_five_reports_in_ten_minutes_queued_ones_included(inbox, audit_log):
    for offset in range(4):
        assert feedback.submit(Cloud(), form(), now=NOW + offset).status == "sent"
    assert feedback.submit(Cloud(url=""), form(), now=NOW + 5).status == "queued"  # counts too
    with pytest.raises(feedback.FeedbackError) as refused:
        feedback.submit(Cloud(), form(), now=NOW + 10)
    assert refused.value.code == "rate_limited" and "5 reports in the last 10 minutes" in refused.value.message
    assert refused.value.copy.startswith("Lumi feedback: Bug")
    with pytest.raises(feedback.FeedbackError):
        feedback.submit(Cloud(url=""), form(), now=NOW + 11)  # and the queue can't be filled faster
    assert len(inbox.requests) == 4 and len(queue()) == 1
    assert feedback.submit(Cloud(), form(), now=NOW + feedback.RECENT_SECONDS + 6).status == "sent"
    assert [r["data"]["reason"] for r in records(audit_log, "feedback.refused")] == ["rate_limited"] * 2


def test_discarding_deletes_the_waiting_reports(inbox, audit_log):
    feedback.submit(Cloud(url=""), form(), now=NOW)
    feedback.submit(Cloud(url=""), form(kind="idea"), now=NOW)
    first = queue()[0]["id"]
    assert feedback.discard([first]) == 1 and [item["body"]["kind"] for item in queue()] == ["idea"]
    assert feedback.discard() == 1
    assert not (state_home() / "feedback" / "queue.json").exists()
    assert [(r["data"]["kind"], r["data"]["reason"]) for r in records(audit_log, "feedback.dropped")] == [
        ("bug", "discarded"), ("idea", "discarded")]


def test_discarding_during_a_round_stops_what_hasnt_gone(inbox, audit_log, clock):
    for n in range(3):
        inbox.answers = [down()]
        feedback.submit(Cloud(), form(message=f"report {n}"), now=NOW)
    arrived, release = threading.Event(), threading.Event()

    def slow(request):  # the first report is on its way when the person chooses Discard
        arrived.set()
        release.wait(10)
        return httpx.Response(201, json=inbox.ack(request))

    inbox.answers = [slow]
    round_ = threading.Thread(target=lambda: feedback.flush(Cloud(), force=True, now=NOW + 1))
    round_.start()
    assert arrived.wait(10)
    assert feedback.discard() == 2  # the one being sent can't be taken back
    assert [item["body"]["message"] for item in queue()] == ["report 0"]
    release.set()
    round_.join(10)
    assert [body["message"] for body in inbox.bodies()[3:]] == ["report 0"] and queue() == []
    types = [(r["type"], r["data"].get("reason")) for r in records(audit_log) if r["type"] != "feedback.queued"]
    assert types == [("feedback.dropped", "discarded"), ("feedback.dropped", "discarded"), ("feedback.sent", None)]


def test_two_lumi_processes_queueing_at_once_lose_nothing(tmp_path):
    # The GUI and the terminal UI (or two windows) share one state folder: every read-modify-write of
    # the queue takes the lock all Lumi processes take, and the install's secret is created exclusively.
    script = (
        "import sys, time\n"
        "from lumi import feedback\n"
        "feedback.MAX_RECENT = feedback.MAX_QUEUED = 10_000\n"
        "class NoCloud:\n"
        "    url = ''\n"
        "    account_url = ''\n"
        "    def status(self): return {'signed_in': False, 'account': {}}\n"
        "start = float(sys.argv[2])\n"
        "while time.time() < start: pass\n"
        "for n in range(15):\n"
        "    feedback.submit(NoCloud(), {'kind': 'bug', 'message': f'p{sys.argv[1]} report {n}'})\n"
    )
    home = tmp_path / "home"
    env = {**os.environ, "USERPROFILE": str(home), "HOME": str(home), "LUMI_STATE_HOME": str(tmp_path / "state"),
           "LUMI_KEYCHAIN": "off", "PYTHONPATH": str(Path(__file__).resolve().parent.parent),
           "PYTHONDONTWRITEBYTECODE": "1"}
    start = time.time() + 3
    processes = [subprocess.Popen([sys.executable, "-c", script, str(index), str(start)], env=env,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for index in (1, 2)]
    outputs = [process.communicate(timeout=120) for process in processes]
    assert [process.returncode for process in processes] == [0, 0], outputs
    folder = tmp_path / "state" / "feedback"
    items = json.loads((folder / "queue.json").read_text(encoding="utf-8"))["items"]
    assert sorted(item["body"]["message"] for item in items) == sorted(
        f"p{index} report {n}" for index in (1, 2) for n in range(15))
    assert len({item["body"]["install_id"] for item in items}) == 1  # one secret, made once
    assert list(folder.glob("*.tmp")) == []


def test_the_background_thread_sends_when_woken(inbox, monkeypatch, clock):
    monkeypatch.setattr(feedback, "LOOP_SECONDS", 0.05)
    cloud = Cloud()
    inbox.answers = [down()]
    feedback.submit(cloud, form(), now=time.time())
    stop = threading.Event()
    thread = feedback.start_background(cloud, first_delay=0.05, stop=stop)
    try:
        time.sleep(0.3)
        assert len(inbox.requests) == 1 and feedback.waiting() == 1  # not due yet
        clock.advance(feedback.RETRY_SECONDS + 1)
        feedback.wake()
        deadline = time.monotonic() + 10
        while feedback.waiting() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert feedback.waiting() == 0 and len(inbox.requests) == 2
    finally:
        stop.set()
        feedback.wake()
        thread.join(5)
    assert not thread.is_alive()


# ── Diagnostics and the preview ─────────────────────────────────────────────


def _write_log(text: str) -> Path:
    path = state_home() / "logs" / "lumi-startup.log"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_diagnostics_are_shown_exactly_as_sent(inbox, settings, audit_log):
    home = str(Path.home())
    log = _write_log("".join(f"line {n}\n" for n in range(200))
                     + f"Traceback in {home}\\projects\\app.py with {TOKEN}\n"
                     + f"Authorization: Bearer abcdefghijklmnop and {SAVED_KEY}\n")
    shown = feedback.preview(Cloud(user_id="usr_ada"), form(include_diagnostics=True), settings=settings,
                             provider="conn-8f3a1c", model="acme-large")
    details = shown["body"]["diagnostics"]
    assert set(details) == {"python", "platform", "packaged", "provider", "model", "offline_mode", "log_tail"}
    assert (details["provider"], details["model"], details["offline_mode"]) == ("connection", "acme-large", False)
    tail = details["log_tail"]
    assert "line 199" in tail and "line 100" not in tail and len(tail.splitlines()) <= feedback.LOG_TAIL_LINES
    for secret in (TOKEN, SAVED_KEY, "abcdefghijklmnop", home):
        assert secret not in json.dumps(shown["body"])
    assert "~\\projects\\app.py" in tail
    assert (shown["destination"], shown["account"], shown["provisional"]) == ("cloud.example.test",
                                                                               "ada@example.com", False)
    assert shown["body"]["install_id"] == feedback.install_id(URL, "usr_ada")

    # The log grows after the preview: what was shown is what goes.
    log.write_text(log.read_text(encoding="utf-8") + "a later line\n", encoding="utf-8")
    outcome = feedback.submit(Cloud(user_id="usr_ada"), form(include_diagnostics=True), settings=settings,
                              preview_id=shown["preview_id"], now=NOW + 60)
    assert outcome.status == "sent"
    assert inbox.bodies()[0] == shown["body"]
    assert records(audit_log)[-1]["data"]["diagnostics"] is True


def test_diagnostics_go_only_after_the_person_saw_that_report(inbox, settings, clock):
    shown = feedback.preview(Cloud(), form(include_diagnostics=True), settings=settings)
    for preview_id, changed in (("", {}), ("made-up", {}), (shown["preview_id"], {"message": "Something else"}),
                                (shown["preview_id"], {"kind": "idea"})):
        with pytest.raises(feedback.FeedbackError) as refused:
            feedback.submit(Cloud(), form(include_diagnostics=True, **changed), settings=settings,
                            preview_id=preview_id)
        assert refused.value.code == "preview"
    # Where it would go, or as whom, changed since: reviewed again first.
    for moved in (Cloud(url=OTHER), Cloud(user_id="usr_ada")):
        with pytest.raises(feedback.FeedbackError) as refused:
            feedback.submit(moved, form(include_diagnostics=True), settings=settings, preview_id=shown["preview_id"])
        assert refused.value.code == "preview" and "changed since you reviewed it" in refused.value.message
    assert inbox.requests == []
    # Too old to count as what the person just saw.
    clock.advance(feedback.PREVIEW_SECONDS + 1)
    with pytest.raises(feedback.FeedbackError):
        feedback.submit(Cloud(), form(include_diagnostics=True), settings=settings, preview_id=shown["preview_id"])
    # Each preview sends once.
    fresh = feedback.preview(Cloud(), form(include_diagnostics=True), settings=settings)
    feedback.submit(Cloud(), form(include_diagnostics=True), settings=settings, preview_id=fresh["preview_id"])
    with pytest.raises(feedback.FeedbackError):
        feedback.submit(Cloud(), form(include_diagnostics=True), settings=settings, preview_id=fresh["preview_id"])


def test_diagnostic_texts_hold_nothing_lumi_cloud_refuses(settings):
    esc, rlo = chr(0x1B), chr(0x202E)
    _write_log(f"{esc}[31mred{esc}[0m and {rlo}reversed\nnext\x00line\n")
    tail = feedback.diagnostics(settings)["log_tail"]
    assert tail == "[31mred[0m and reversed\nnextline"
    # A text made long by redaction markers keeps its end, within what Lumi Cloud takes.
    body = {"kind": "bug", "message": "x", "reply_to": None, "diagnostics": {"log_tail": "a" * 13000 + "END"}}
    checked = feedback._checked(body, None).body["diagnostics"]["log_tail"]
    assert len(checked) == feedback.MAX_DIAGNOSTIC_TEXT and checked.endswith("END")


def test_the_log_is_redacted_before_it_is_cut(settings, monkeypatch):
    monkeypatch.setattr(feedback, "LOG_TAIL_CHARS", 40)
    # Cut first, the last 40 characters would be most of the token without the prefix that marks it as one.
    _write_log(f"{'z' * 30} {TOKEN} end\n")
    assert feedback.log_tail(settings) == "z" * 21 + " ghp_[REDACTED] end"
    # Whole lines: one that doesn't fit is left out rather than cut.
    _write_log(f"first {TOKEN} middle {SAVED_KEY}\nshort line\nlast line\n")
    assert feedback.log_tail(settings) == "short line\nlast line"


def test_without_a_log_the_tail_is_empty(settings):
    assert feedback.diagnostics(settings)["log_tail"] == ""
    assert feedback.provider_type("anthropic") == "anthropic" and feedback.provider_type("") == ""


@pytest.mark.skipif(os.name != "nt", reason="Windows paths")
def test_the_home_folder_is_written_as_a_tilde(monkeypatch):
    monkeypatch.setattr(Path, "home", staticmethod(lambda: Path("C:/Users/ann")))
    monkeypatch.setattr(os.path, "expanduser", lambda path: "C:\\Users\\ann")
    text = "C:\\Users\\ann\\x.py c:/users/ANN/y.py C:\\Users\\anna\\z.py C:\\Users\\ann C:\\\\Users\\\\ann\\\\w.py"
    assert feedback._without_home(text) == "~\\x.py ~/y.py C:\\Users\\anna\\z.py ~ ~\\\\w.py"


def test_the_copies(settings):
    typed = feedback.typed_copy(form(message=f"Broke with {TOKEN}", reply_to="ada@example.com",
                                     include_diagnostics=True), settings)
    assert typed == "Lumi feedback: Bug\n\nBroke with [REDACTED GitHub token]\n\nReply to: ada@example.com\n"
    assert feedback.typed_copy(form(message=""), settings) == ""
    body = {"kind": "idea", "message": "m", "reply_to": None, "app": feedback.app_info(), "install_id": "x",
            "diagnostics": {"python": "3.13"}}
    text = feedback._copy_from_body(body)
    assert text.startswith("Lumi feedback: Idea\n\nm\n") and f"Lumi {__version__}" in text and "Diagnostics:" in text


def test_who_reads_reports_is_asked_only_when_it_may_be(inbox, settings):
    about = feedback.info(Cloud(), settings)
    assert about == {"destination": "cloud.example.test", "accepting": True, "operator": "Luminary Analytics"}
    assert len(inbox.info_requests) == 1 and feedback.info(Cloud(), settings) == about  # kept a while
    assert len(inbox.info_requests) == 1 and inbox.requests == []
    feedback.reset_for_tests()
    feedback.set_transport_for_tests(httpx.MockTransport(inbox))
    inbox.info = {"accepting": False, "operator": f"  Acme{chr(0x202E)}\nFeedback  Team " + "x" * 200}
    about = feedback.info(Cloud(), settings)
    assert about["accepting"] is False and about["operator"].startswith("Acme Feedback Team x")
    assert len(about["operator"]) <= feedback.MAX_OPERATOR
    # Not asked: nowhere to ask, offline mode, or feedback turned off.
    assert feedback.info(Cloud(url=""), settings) == {}
    feedback.reset_for_tests()
    feedback.set_transport_for_tests(httpx.MockTransport(inbox))
    offline.set_for_tests(enabled=True)
    assert feedback.info(Cloud(), settings) == {} and len(inbox.info_requests) == 2


# ── The dialog's commands ──────────────────────────────────────────────────


def _command(name: str, cloud, settings=None, **msg) -> list[dict]:
    from lumi.gui import ws_commands
    from tests.test_connections import _StubWS

    state = SimpleNamespace(cloud=cloud, settings=settings,
                            backend_spec=SimpleNamespace(backend_type="ollama", model="qwen3:8b"))
    ctx = ws_commands.CommandContext(ws=_StubWS(), state=state, msg={"command": name, **msg},
                                     runs=SimpleNamespace(busy=False))

    async def run() -> None:
        await ws_commands.HANDLERS[name](ctx)
        # Sending runs as its own task, so the socket stays live meanwhile.
        while ws_commands._FEEDBACK_TASKS:
            await asyncio.gather(*list(ws_commands._FEEDBACK_TASKS))

    asyncio.run(run())
    return ctx.ws.sent


def test_the_dialogs_commands(inbox, settings):
    [status] = _command("feedback_status", Cloud(user_id="usr_ada"), settings)
    assert status["event"] == "feedback_status"
    assert {k: status["data"][k] for k in ("destination", "configured", "account", "offline", "waiting",
                                           "disabled")} == {
        "destination": "cloud.example.test", "configured": True, "account": "ada@example.com", "offline": "",
        "waiting": 0, "disabled": ""}
    assert status["data"]["app"]["version"] == __version__
    status, about = _command("feedback_status", Cloud(), settings, open=True)
    assert (about["event"], about["data"]["operator"]) == ("feedback_info", "Luminary Analytics")

    [preview] = _command("feedback_preview", Cloud(), settings, request=7, form=form(include_diagnostics=True))
    assert preview["request"] == 7 and preview["data"]["body"]["diagnostics"]["provider"] == "ollama"
    assert preview["data"]["body"]["diagnostics"]["model"] == "qwen3:8b"

    result, after = _command("feedback_send", Cloud(), settings, form=form(include_diagnostics=True),
                             preview_id=preview["data"]["preview_id"])
    assert (result["event"], result["ok"], result["status"]) == ("feedback_result", True, "sent")
    assert result["notices"] == [] and after["event"] == "feedback_status"

    [invalid, _status] = _command("feedback_send", Cloud(), settings, form=form(message=""))
    assert (invalid["ok"], invalid["code"], invalid["field"], invalid["copy_text"]) == (False, "invalid", "message", "")

    _dlp()
    [blocked, _status] = _command("feedback_send", Cloud(), settings, form=form(message="Project Falcon broke"))
    assert (blocked["code"], blocked["copy_text"]) == ("dlp", "")  # no copy of what the rules keep here
    lumi_policy.set_for_tests(None)

    feedback.submit(Cloud(url=""), form(), now=time.time())
    [held] = _command("feedback_status", Cloud(), settings)[0]["data"]["reports"]
    [copied] = _command("feedback_copy_held", Cloud(), settings, id=held["id"])
    assert (copied["event"], copied["id"]) == ("feedback_copy", held["id"]) and copied["text"].startswith("Lumi")
    [sent] = _command("feedback_send_held", Cloud(), settings, id=held["id"], destination=URL)
    assert sent["data"]["flushed"]["sent"] == 1
    inbox.answers = [down()]
    feedback.submit(Cloud(), form(), now=time.time())
    [flushed] = _command("feedback_flush", Cloud(), settings)
    assert flushed["data"]["flushed"] == {"sent": 1, "held": 0, "waiting": 0}
    feedback.submit(Cloud(url=""), form(), now=time.time())
    [discarded] = _command("feedback_discard", Cloud(), settings)
    assert (discarded["data"]["discarded"], discarded["data"]["waiting"]) == (1, 0)


def test_a_report_the_dlp_service_changes_comes_back_to_the_dialog_to_review(inbox, settings):
    _dlp_service(lambda payload: {"action": "redact", "redactions": ["Falcon"]}
                 if any("Falcon" in item["text"] for item in payload["items"]) else {"action": "allow"})
    typed = form(message="Falcon broke", include_diagnostics=True)
    [shown] = _command("feedback_preview", Cloud(), settings, request=1, form=typed)
    [changed, _status] = _command("feedback_send", Cloud(), settings, form=typed,
                                  preview_id=shown["data"]["preview_id"])
    assert (changed["ok"], changed["code"]) == (False, "review")
    assert "Falcon" not in changed["preview"]["body"]["message"] and inbox.requests == []


def test_an_unexpected_failure_still_answers_the_dialog(inbox, settings, monkeypatch):
    def broken(*args, **kwargs):
        raise OSError("disk full")

    feedback._install_secret()
    monkeypatch.setattr(feedback, "_write", broken)  # the disk filled up: the report can't be kept to send later
    [result, status] = _command("feedback_send", Cloud(url=""), settings, form=form(message=f"x {TOKEN}"))
    assert (result["event"], result["ok"], result["code"]) == ("feedback_result", False, "error")
    assert "couldn't send or save" in result["message"]
    assert result["copy_text"] == "Lumi feedback: Bug\n\nx [REDACTED GitHub token]\n"
    assert status["event"] == "feedback_status"
    monkeypatch.setattr(feedback, "flush", broken)
    [flushed] = _command("feedback_flush", Cloud(), settings)
    assert flushed["data"]["flushed"]["failed"] is True


def test_the_dialogs_long_commands_leave_the_socket_live(inbox, settings):
    from lumi.gui import ws_commands
    from tests.test_connections import _StubWS

    release = threading.Event()
    inbox.answers = [lambda request: (release.wait(10), httpx.Response(201, json=inbox.ack(request)))[1]]
    state = SimpleNamespace(cloud=Cloud(), settings=settings, backend_spec=None)
    ctx = ws_commands.CommandContext(ws=_StubWS(), state=state, msg={"command": "feedback_send", "form": form()},
                                     runs=SimpleNamespace(busy=False))

    async def run() -> list:
        await asyncio.wait_for(ws_commands.HANDLERS["feedback_send"](ctx), 2)  # returns before Lumi Cloud answers
        answered_before = list(ctx.ws.sent)
        release.set()
        while ws_commands._FEEDBACK_TASKS:
            await asyncio.gather(*list(ws_commands._FEEDBACK_TASKS))
        return answered_before

    assert asyncio.run(run()) == []
    assert ctx.ws.sent[0]["ok"] is True


def test_nothing_the_dialog_is_told_holds_a_token(inbox, settings):
    cloud = Cloud(user_id="usr_ada")
    sent = [*_command("feedback_status", cloud, settings, open=True),
            *_command("feedback_preview", cloud, settings, request=1, form=form(include_diagnostics=True)),
            *_command("feedback_send", cloud, settings, form=form())]
    text = json.dumps(sent)
    assert "access-for-usr_ada" not in text and SAVED_KEY not in text
    assert inbox.requests[-1].headers["authorization"] == "Bearer access-for-usr_ada"


def test_report_ids_are_uuid4():
    for _ in range(5):
        assert feedback.REPORT_ID.fullmatch(str(uuid.uuid4()))


# ── The second review of #101 ───────────────────────────────────────────────


def _redact_rule(name: str, keyword: str) -> None:
    lumi_policy.set_for_tests(lumi_policy.parse({
        "schema": "lumi.policy/v1", "organization": "Acme",
        "dlp": {"version": 1, "rules": [{"name": name, "keywords": [keyword], "action": "redact"}]},
    }, source="test policy"))


def test_a_redact_rule_named_after_its_keyword_is_reviewed_once_then_sent(inbox, settings):
    """The reviewer's n3: the check at Send found the rule's own marker, and the report could never be sent."""
    _redact_rule("confidential", "confidential")
    message = "the confidential plan broke"
    shown = feedback.preview(Cloud(), form(message=message, include_diagnostics=True), settings=settings)
    assert shown["body"]["message"] == "the [REDACTED:confidential] plan broke"
    outcome = feedback.submit(Cloud(), form(message=message, include_diagnostics=True), settings=settings,
                              preview_id=shown["preview_id"], now=NOW)
    assert outcome.status == "sent"
    [body] = inbox.bodies()
    assert body == shown["body"] and body["message"].count("[REDACTED:") == 1


def test_a_waiting_report_is_checked_again_only_when_the_rules_changed(inbox, clock):
    _redact_rule("confidential", "confidential")
    inbox.answers = [down()]
    feedback.submit(Cloud(), form(message="the confidential plan broke"), now=NOW)
    [item] = queue()
    assert item["body"]["message"] == "the [REDACTED:confidential] plan broke" and item["rules"]
    feedback.flush(Cloud(), force=True, now=NOW + 1)  # the same rules: sent as it was checked, markers and all
    assert inbox.bodies()[-1]["message"] == "the [REDACTED:confidential] plan broke"
    # Written under these rules, sent under stricter ones: they apply to what was reviewed.
    inbox.answers = [down()]
    feedback.submit(Cloud(), form(message="the confidential plan and Falcon broke"), now=NOW + 2)
    lumi_policy.set_for_tests(lumi_policy.parse({
        "schema": "lumi.policy/v1", "organization": "Acme",
        "dlp": {"version": 1, "rules": [{"name": "falcon", "keywords": ["Falcon"], "action": "redact"},
                                        {"name": "confidential", "keywords": ["confidential"], "action": "redact"}]},
    }, source="test policy"))
    feedback.flush(Cloud(), force=True, now=NOW + 3)
    sent = inbox.bodies()[-1]["message"]
    assert "Falcon" not in sent and "[REDACTED:falcon]" in sent and "plan" in sent


@pytest.mark.parametrize("b_user", ["usr_b", "usr_1"], ids=["someone else", "the same user id"])
def test_a_sign_in_elsewhere_during_a_round_never_sends_its_token_to_the_old_destination(inbox, request, b_user):
    """The reviewer's n1: the round decided the account at its start, and asked for the token per report.

    User ids are each Lumi Cloud's own, so B can have given the same one: only the destination stops that.
    """
    from tests.test_cloud import B_URL, _client, _sign_in

    cloud_fake = request.getfixturevalue("fake")
    client = _client(cloud_fake)
    _sign_in(client, cloud_fake)  # signed in at A, where reports go
    access_a = client._access[0]
    inbox.answers = [down(), down()]
    for n in range(2):
        feedback.submit(client, form(message=f"report {n}"), now=NOW + n)  # A unreachable: both wait, as Ada
    assert [item["account"] for item in queue()] == ["usr_1", "usr_1"]

    def sign_in_completes_at_b(sent: httpx.Request) -> httpx.Response:
        # While the round sends the first report, a sign-in at B completes (what _finish_sign_in does).
        client._adopt(B_URL, "access-issued-by-B", "refresh-issued-by-B", time.monotonic() + 3000,
                      {"user_id": b_user, "email": "ben@b.example", "name": "Ben", "organizations": []})
        return httpx.Response(201, json=inbox.ack(sent))

    inbox.answers = [sign_in_completes_at_b]
    result = feedback.flush(client, force=True, now=NOW + 10)
    tokens = [sent.headers.get("authorization", "") for sent in inbox.requests]
    assert tokens[-1] == f"Bearer {access_a}" and "Bearer access-issued-by-B" not in tokens
    assert result["sent"] == 1
    # The second is Ada's: it waits for her, never goes with Ben's token or without an account.
    [item] = queue()
    assert (item["state"], item["account"]) == ("sign_in", "usr_1")


def test_a_report_held_for_a_sign_in_never_goes_anonymously_or_as_someone_else(inbox):
    """The reviewer's n6: after two 401s, Send now sent it without the account when nobody was signed in."""
    ada = Cloud(user_id="usr_ada")
    inbox.answers = [(401, {"error": "invalid_token"}, None), (401, {"error": "invalid_token"}, None)]
    outcome = feedback.submit(ada, form(), now=NOW)
    assert outcome.reason == "sign_in" and queue()[0]["state"] == "sign_in"
    sent = len(inbox.requests)
    assert feedback.flush(Cloud(user_id=""), force=True, now=NOW + 1)["sent"] == 0  # Send now, signed out
    assert feedback.flush(Cloud(user_id="usr_bob", email="bob@example.com"), force=True, now=NOW + 2)["sent"] == 0
    assert len(inbox.requests) == sent and queue()[0]["state"] == "sign_in"
    # Only the person's labelled choice sends it without the account.
    status = feedback.status(Cloud(user_id=""))
    [report] = status["reports"]
    assert report["without_account"] and report["writer"] == "ada@example.com"
    assert feedback.send_without_account(Cloud(user_id=""), None, report["id"], now=NOW + 3)["sent"] == 1
    assert "authorization" not in inbox.requests[-1].headers and queue() == []


def test_a_report_written_without_an_address_goes_as_its_button_says(inbox):
    feedback.submit(Cloud(url=""), form(), now=NOW)
    [item] = queue()
    [report] = feedback.status(Cloud(user_id="usr_ada"))["reports"]
    assert report["send"] and report["send_as"] == "ada@example.com"
    # The button named Ada; Bob is signed in by the time it's pressed: nothing is sent.
    with pytest.raises(feedback.FeedbackError) as changed:
        feedback.send_held(Cloud(user_id="usr_bob", email="bob@example.com"), None, item["id"], URL,
                           shown_as="ada@example.com")
    assert changed.value.code == "preview" and inbox.requests == []
    # As shown: with Ada's account, and only Ada's.
    assert feedback.send_held(Cloud(user_id="usr_ada"), None, item["id"], URL, shown_as="ada@example.com")["sent"] == 1
    assert inbox.requests[-1].headers["authorization"] == "Bearer access-for-usr_ada"
    # Without an account, when the button said so.
    feedback.submit(Cloud(url=""), form(), now=NOW + 1)
    [item] = queue()
    assert feedback.status(Cloud())["reports"][0]["send_as"] == ""
    assert feedback.send_held(Cloud(), None, item["id"], URL, shown_as="")["sent"] == 1
    assert "authorization" not in inbox.requests[-1].headers


def test_the_dialog_sends_a_waiting_report_without_the_account_only_when_asked(inbox, settings):
    ada = Cloud(user_id="usr_ada")
    inbox.answers = [down()]
    feedback.submit(ada, form(), settings=settings, now=NOW)
    [item] = queue()
    signed_out = Cloud()
    [status] = _command("feedback_send_without_account", signed_out, settings, id=item["id"])
    assert status["event"] == "feedback_status" and status["data"]["flushed"]["sent"] == 1
    assert "authorization" not in inbox.requests[-1].headers
    [status] = _command("feedback_send_without_account", signed_out, settings, id=item["id"])
    assert "isn't waiting" in status["data"]["flushed"]["error"]


def test_another_process_holding_the_reports_is_waited_for_a_bounded_time(inbox, monkeypatch):
    """The reviewer's lock_stall.py: a second process waited for ever on macOS and Linux (about 10 s on Windows),
    and then went on unlocked. Now: the same bounded wait everywhere, then ``busy``, and nothing touched."""
    from lumi import file_lock

    script = ("import sys, time\n"
              "from lumi import feedback\n"
              "with feedback._state():\n"
              "    print('held', flush=True)\n"
              "    time.sleep(float(sys.argv[1]))\n")
    home = Path.home()
    env = {**os.environ, "USERPROFILE": str(home), "HOME": str(home), "LUMI_STATE_HOME": str(state_home()),
           "LUMI_KEYCHAIN": "off", "PYTHONPATH": str(Path(__file__).resolve().parent.parent),
           "PYTHONDONTWRITEBYTECODE": "1"}
    holder = subprocess.Popen([sys.executable, "-c", script, "20"], env=env, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, text=True)
    try:
        assert holder.stdout.readline().strip() == "held", holder.stderr.read()
        monkeypatch.setattr(file_lock, "LOCK_SECONDS", 0.5)
        monkeypatch.setattr(feedback, "LOCK_SECONDS", 0.5)
        started = time.monotonic()
        with pytest.raises(feedback.FeedbackError) as busy:
            feedback.waiting()
        assert busy.value.code == "busy" and 0.4 <= time.monotonic() - started < 5
        with pytest.raises(feedback.FeedbackError) as busy:  # nothing is written unlocked either
            feedback.submit(Cloud(url=""), form(), now=NOW)
        assert busy.value.code == "busy" and "Lumi feedback: Bug" in busy.value.copy
        status = feedback.status(Cloud())
        assert status["busy"] and status["reports"] == []
    finally:
        holder.kill()
        holder.communicate(timeout=30)
    assert feedback.waiting() == 0
