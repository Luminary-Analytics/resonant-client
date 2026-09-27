"""Send feedback (lumi/feedback.py): the report, what never leaves, the queue, and the dialog's commands.

Lumi Cloud is an ``httpx.MockTransport`` here (``feedback.set_transport_for_tests``),
never a real server. tests/feedback_view.test.cjs covers the dialog's own
logic, and tests/feedback.browser.cjs the dialog in a real browser.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from lumi import __version__, audit, feedback, offline, policy as lumi_policy
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


class Cloud:
    """The parts of CloudClient feedback uses: its address, where and as whom the person signed in, their token."""

    def __init__(self, url: str = URL, *, user_id: str = "", email: str = "ada@example.com",
                 sign_in_url: str | None = None) -> None:
        self.url = url
        self.user_id = user_id
        self.email = email
        self.sign_in_url = url if sign_in_url is None else sign_in_url
        self.token_calls = 0

    def status(self) -> dict:
        signed_in = bool(self.user_id)
        return {"signed_in": signed_in, "account": {"user_id": self.user_id, "email": self.email} if signed_in else {}}

    def account_token(self) -> str:
        self.token_calls += 1
        if not self.user_id:
            raise CloudError("Sign in to Lumi Cloud first.", code="signed_out")
        return f"access-for-{self.user_id}"


class Inbox:
    """Lumi Cloud's POST /api/v1/feedback: 201 with an id, unless an answer is lined up."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.answers: list = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.answers:
            answer = self.answers.pop(0)
            if isinstance(answer, BaseException):
                raise answer
            if callable(answer):
                return answer(request)
            status, body, headers = answer
            return httpx.Response(status, json=body, headers=headers or {})
        return httpx.Response(201, json={"id": f"fbk_{len(self.requests):04d}"})

    def bodies(self) -> list[dict]:
        return [json.loads(request.content) for request in self.requests]


@pytest.fixture
def inbox():
    answering = Inbox()
    feedback.set_transport_for_tests(httpx.MockTransport(answering))
    return answering


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


def down() -> httpx.ConnectError:
    return httpx.ConnectError("connection refused")


# ── The report ──────────────────────────────────────────────────────────────


def test_a_report_carries_exactly_what_the_contract_names(inbox, audit_log):
    outcome = feedback.submit(Cloud(), form(reply_to="Ada@Example.COM"), now=NOW)

    assert (outcome.status, outcome.id) == ("sent", "fbk_0001")
    assert "fbk_0001" in outcome.message
    [request] = inbox.requests
    assert (request.method, str(request.url)) == ("POST", ENDPOINT)
    assert request.headers["content-type"] == "application/json"
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


def test_install_ids_are_random_and_never_link_signed_out_reports_to_an_account(tmp_path, monkeypatch):
    secret = feedback._install_secret()
    assert (state_home() / "feedback" / "install-id").read_text(encoding="ascii") == secret
    anonymous = feedback.install_id(URL, "")
    # Stable for one Lumi Cloud signed out, so staff can tell reports came from one install...
    assert feedback.install_id(URL, "") == anonymous and feedback.INSTALL_ID.fullmatch(anonymous)
    # ...but another for each account, and for each Lumi Cloud: nothing ties them together without the secret.
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


def test_text_is_sent_as_seen():
    rlo, lri, pdi, lrm = chr(0x202E), chr(0x2066), chr(0x2069), chr(0x200E)
    text = f"  Line one\r\nline{chr(0)} two{chr(7)}\rtabs\tstay {rlo}evil{pdi} {lri}x {lrm}mark{chr(0x2028)}end \n"
    assert feedback.clean_text(text) == f"Line one\nline two\ntabs\tstay evil x {lrm}mark\nend"
    # A lone surrogate can't be encoded; it's dropped rather than breaking the report.
    assert feedback.clean_text("a" + chr(0xD800) + "b") == "ab"
    assert feedback.Form.read(form(include_diagnostics="true")).diagnostics is False  # only a real true


# ── What never leaves ──────────────────────────────────────────────────────


def test_secrets_are_removed_even_with_the_scan_off_in_settings(inbox, settings):
    assert settings.get("privacy", "secret_scan", False) is False
    message = f"It failed with {TOKEN}, and my key {SAVED_KEY} is in the log."
    outcome = feedback.submit(Cloud(), form(message=message), settings=settings, now=NOW)
    assert outcome.status == "sent"
    sent = inbox.requests[0].content.decode()
    assert TOKEN not in sent and SAVED_KEY not in sent
    assert "[REDACTED GitHub token]" in sent and "[REDACTED saved API key]" in sent
    preview = feedback.preview(Cloud(), form(message=message, include_diagnostics=True), settings=settings)
    assert preview["notices"][0].startswith("Removed 2 secrets")


def test_a_message_redaction_made_too_long_isnt_sent(inbox, monkeypatch):
    monkeypatch.setattr(feedback, "MAX_SENT_MESSAGE", 30)
    with pytest.raises(feedback.FeedbackError) as refused:
        feedback.submit(Cloud(), form(message="x" * 25 + " " + TOKEN), now=NOW)
    assert (refused.value.code, refused.value.field) == ("invalid", "message")
    assert "too long to send" in refused.value.message and inbox.requests == []


def _dlp(**detectors) -> None:
    lumi_policy.set_for_tests(lumi_policy.parse({
        "schema": "lumi.policy/v1", "organization": "Acme",
        "dlp": {"version": 1, "detectors": detectors,
                "rules": [{"name": "falcon", "keywords": ["Project Falcon"], "action": "block", "scope": ["prompt"]}]},
    }, source="test policy"))


def test_dlp_blocks_redacts_and_keeps_out_what_it_would_change(inbox, audit_log):
    _dlp(credit_card="redact", email="redact")
    with pytest.raises(feedback.FeedbackError) as refused:
        feedback.submit(Cloud(), form(message="Project Falcon broke"), now=NOW)
    assert refused.value.code == "dlp" and "falcon in your message" in refused.value.message
    assert "Project Falcon" not in refused.value.message
    assert inbox.requests == []

    outcome = feedback.submit(Cloud(), form(message="Card 4111 1111 1111 1111 was charged twice",
                                            reply_to="ada@example.com"), now=NOW)
    assert outcome.status == "sent"
    body = inbox.bodies()[0]
    assert body["message"] == "Card [REDACTED:credit_card] was charged twice"
    assert body["reply_to"] is None  # DLP would change the address, so it stays here

    audit_text = json.dumps(records(audit_log, ""))
    assert "4111" not in audit_text and "Project Falcon" not in audit_text and "ada@example.com" not in audit_text
    findings = [r["data"] for r in records(audit_log, "dlp.finding")]
    assert {(f["rule"], f["action"], f["purpose"]) for f in findings} >= {
        ("falcon", "block", "feedback"), ("credit_card", "redact", "feedback")}
    assert [r["data"]["reason"] for r in records(audit_log, "feedback.refused")] == ["dlp"]


def test_dlp_checks_diagnostics_as_an_attachment(inbox, settings):
    _dlp(credit_card="block")
    _write_log("Charged card 4111 1111 1111 1111 at startup\n")
    with pytest.raises(feedback.FeedbackError) as refused:
        feedback.preview(Cloud(), form(include_diagnostics=True), settings=settings)
    assert refused.value.code == "dlp" and "credit_card in an attachment" in refused.value.message
    assert inbox.requests == []


def test_a_policy_lumi_cant_use_refuses_feedback_too(inbox, monkeypatch):
    monkeypatch.setattr(lumi_policy, "blocked_reason", lambda: "Your organization's policy can't be used.")
    with pytest.raises(feedback.FeedbackError) as refused:
        feedback.submit(Cloud(), form(), now=NOW)
    assert refused.value.code == "dlp" and "can't be used" in refused.value.message
    assert inbox.requests == []


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


def test_offline_mode_refuses_before_dlp_looks(inbox, audit_log):
    _dlp()
    offline.set_for_tests(enabled=True)
    with pytest.raises(feedback.FeedbackError) as refused:
        feedback.submit(Cloud(), form(message="Project Falcon broke"), now=NOW)
    assert refused.value.code == "offline"  # the offline reason, not DLP's
    with pytest.raises(feedback.FeedbackError) as shown:
        feedback.preview(Cloud(), form(message="Project Falcon broke", include_diagnostics=True))
    assert shown.value.code == "offline"
    assert records(audit_log, "dlp.") == []  # a report that couldn't leave wasn't DLP-checked


def test_offline_mode_turned_on_while_reports_wait_keeps_them(inbox):
    feedback.submit(Cloud(url=""), form(), now=NOW)
    offline.set_for_tests(enabled=True)
    assert feedback.flush(Cloud(), force=True, now=NOW + 1) == {"sent": 0, "dropped": 0, "waiting": 1}
    assert inbox.requests == []
    offline.set_for_tests(enabled=False)
    assert feedback.flush(Cloud(), now=NOW + 2)["sent"] == 1


# ── Where a report goes, and as whom ───────────────────────────────────────


def test_the_account_goes_only_while_the_person_is_signed_in(inbox, audit_log, monkeypatch):
    monkeypatch.setattr(feedback, "MAX_RECENT", 100)
    anonymous = Cloud()
    feedback.submit(anonymous, form(), now=NOW)
    assert "authorization" not in inbox.requests[-1].headers and anonymous.token_calls == 0

    ada = Cloud(user_id="usr_ada")
    feedback.submit(ada, form(), now=NOW)
    assert inbox.requests[-1].headers["authorization"] == "Bearer access-for-usr_ada"
    assert records(audit_log)[-1]["data"]["attributed"] is True

    # Written while signed out: never attributed later, even once someone signs in.
    feedback.submit(Cloud(url=""), form(), now=NOW)
    feedback.flush(ada, now=NOW + 1)
    assert "authorization" not in inbox.requests[-1].headers
    # Written as Ada while Lumi Cloud was down: sent as Ada only while Ada is the one signed in.
    inbox.answers = [down()]
    feedback.submit(ada, form(), now=NOW)
    feedback.flush(Cloud(user_id="usr_bob"), force=True, now=NOW + 2)
    assert "authorization" not in inbox.requests[-1].headers
    inbox.answers = [down()]
    feedback.submit(ada, form(), now=NOW)
    feedback.flush(ada, force=True, now=NOW + 3)
    assert inbox.requests[-1].headers["authorization"] == "Bearer access-for-usr_ada"


def test_a_sign_in_that_ended_meanwhile_sends_without_the_account(inbox, audit_log):
    class Ended(Cloud):
        def account_token(self) -> str:
            raise CloudError("Your Lumi Cloud sign-in ended. Sign in again.", code="signed_out")

    assert feedback.submit(Ended(user_id="usr_ada"), form(), now=NOW).status == "sent"
    assert "authorization" not in inbox.requests[-1].headers
    assert records(audit_log)[-1]["data"]["attributed"] is False  # what went, not what was meant


def test_a_token_that_cant_be_had_now_keeps_the_report(inbox):
    class Flaky(Cloud):
        def account_token(self) -> str:
            raise CloudError("Lumi Cloud answered 503.", code="503")

    outcome = feedback.submit(Flaky(user_id="usr_ada"), form(), now=NOW)
    assert (outcome.status, outcome.reason) == ("queued", "unreachable")
    assert inbox.requests == []  # never sent without the account the person expected
    assert queue()[0]["account"] == "usr_ada"


def test_the_account_goes_only_to_the_lumi_cloud_it_signed_in_at(inbox):
    # A policy has since named another Lumi Cloud: reports go there, without the account or its token.
    moved = Cloud(url=OTHER, user_id="usr_ada", sign_in_url=URL)
    feedback.submit(moved, form(), now=NOW)
    assert str(inbox.requests[-1].url) == OTHER + "/api/v1/feedback"
    assert "authorization" not in inbox.requests[-1].headers and moved.token_calls == 0
    assert feedback.status(moved)["account"] == ""


def test_a_waiting_report_goes_only_to_the_lumi_cloud_it_was_written_for(inbox):
    inbox.answers = [down()]
    feedback.submit(Cloud(), form(), now=NOW)
    assert feedback.flush(Cloud(url=OTHER), force=True, now=NOW + 1) == {"sent": 0, "dropped": 0, "waiting": 1}
    assert len(inbox.requests) == 1
    assert feedback.flush(Cloud(), force=True, now=NOW + 2)["sent"] == 1
    assert str(inbox.requests[-1].url) == ENDPOINT


def test_the_real_cloud_client_sends_its_access_token_only_when_signed_in(inbox, request):
    from tests.test_cloud import _client, _sign_in

    cloud_fake = request.getfixturevalue("fake")  # test_cloud's isolation: no machine policy
    client = _client(cloud_fake)
    client.settings.update_section("cloud", {"url": URL})
    feedback.submit(client, form(), now=NOW)
    assert "authorization" not in inbox.requests[-1].headers
    _sign_in(client, cloud_fake)
    assert client.sign_in_url == URL
    feedback.submit(client, form(), now=NOW + 1)
    token = inbox.requests[-1].headers["authorization"][len("Bearer "):]
    assert token in cloud_fake.access_tokens
    assert client.settings.get("api_keys", "lumi_cloud_refresh") not in inbox.requests[-1].content.decode()


@pytest.mark.parametrize("address", ["http://cloud.example.test", "https://cloud.example.test:abc"])
def test_an_address_lumi_wouldnt_use_isnt_used(inbox, address):
    # Plain http to another computer would carry the report in the clear; a port that isn't one can't connect.
    outcome = feedback.submit(Cloud(url=address), form(), now=NOW)
    assert (outcome.status, outcome.reason) == ("queued", "no_cloud") and inbox.requests == []


# ── Waiting on this computer ───────────────────────────────────────────────


def test_without_a_lumi_cloud_the_report_waits_and_goes_later(inbox, audit_log):
    outcome = feedback.submit(Cloud(url=""), form(), now=NOW)
    assert (outcome.status, outcome.reason) == ("queued", "no_cloud")
    assert "once a Lumi Cloud address is set" in outcome.message
    assert inbox.requests == [] and len(queue()) == 1
    assert feedback.status(Cloud(url=""))["waiting"] == 1

    assert feedback.flush(Cloud(), now=NOW + 5) == {"sent": 1, "dropped": 0, "waiting": 0}
    assert inbox.bodies()[0]["message"] == FORM["message"]
    assert not (state_home() / "feedback" / "queue.json").exists()
    assert [(r["type"], r["data"].get("queued"), r["data"].get("reason")) for r in records(audit_log)] == [
        ("feedback.queued", None, "no_cloud"), ("feedback.sent", True, None)]


def test_an_unreachable_lumi_cloud_is_retried_with_backoff(inbox):
    inbox.answers = [down()]
    outcome = feedback.submit(Cloud(), form(), now=NOW)
    assert (outcome.status, outcome.reason) == ("queued", "unreachable")
    assert "couldn't be reached" in outcome.message
    [item] = queue()
    assert item["next_try"] == NOW + feedback.RETRY_SECONDS and item["attempts"] == 0 and item["url"] == URL

    assert feedback.flush(Cloud(), now=NOW + 10)["sent"] == 0 and len(inbox.requests) == 1  # not due yet
    inbox.answers = [down()]
    feedback.flush(Cloud(), now=NOW + 601)
    [item] = queue()
    assert item["attempts"] == 1 and item["next_try"] == NOW + 601 + 2 * feedback.RETRY_SECONDS
    inbox.answers = [down()]
    assert feedback.flush(Cloud(), now=NOW + 1300)["sent"] == 0 and len(inbox.requests) == 2  # not due yet
    feedback.flush(Cloud(), now=NOW + 1802)
    [item] = queue()
    assert item["attempts"] == 2 and item["next_try"] == NOW + 1802 + 4 * feedback.RETRY_SECONDS
    # The wait stops doubling at six hours.
    for attempt in range(3, 9):
        inbox.answers = [down()]
        feedback.flush(Cloud(), force=True, now=NOW + 10_000 * attempt)
    assert queue()[0]["next_try"] == NOW + 80_000 + feedback.MAX_BACKOFF_SECONDS
    assert feedback.flush(Cloud(), force=True, now=NOW + 80_001)["sent"] == 1  # Send now doesn't wait
    assert queue() == []


def test_server_trouble_waits_and_retry_after_is_honoured_even_by_send_now(inbox):
    inbox.answers = [(503, {"error": "down"}, None), (429, {"error": "slow_down"}, {"Retry-After": "120"})]
    assert feedback.submit(Cloud(), form(), now=NOW).reason == "unreachable"
    outcome = feedback.submit(Cloud(), form(), now=NOW)
    assert (outcome.status, outcome.reason) == ("queued", "busy") and "busy" in outcome.message
    assert queue()[1]["next_try"] == NOW + 120
    # Send now doesn't wait out backoff, but it does wait out the server's Retry-After.
    assert feedback.flush(Cloud(), force=True, now=NOW + 1) == {"sent": 1, "dropped": 0, "waiting": 1}
    assert feedback.flush(Cloud(), force=True, now=NOW + 121)["sent"] == 1


# Digits other than 0-9 (a superscript two, Arabic-Indic 120) are digits to str.isdigit() and even int().
@pytest.mark.parametrize("value", ["abc", chr(0xB2).encode("latin-1"), "".join(map(chr, (0x661, 0x662, 0x660))).encode(),
                                   "1" * 5000, "-5", "Wed, 21 Oct 2026 07:28:00 GMT"])
def test_a_retry_after_that_isnt_seconds_means_the_usual_wait(inbox, value):
    inbox.answers = [(429, {"error": "slow_down"}, {"Retry-After": value})]
    outcome = feedback.submit(Cloud(), form(), now=NOW)
    assert outcome.reason == "busy" and queue()[0]["next_try"] == NOW + feedback.RETRY_SECONDS


def test_one_unreachable_report_stops_the_round(inbox):
    for _ in range(3):
        feedback.submit(Cloud(url=""), form(), now=NOW)
    inbox.answers = [down()]
    assert feedback.flush(Cloud(), now=NOW + 1) == {"sent": 0, "dropped": 0, "waiting": 3}
    assert len(inbox.requests) == 1


def test_an_unexpected_failure_mid_round_keeps_what_was_already_sent(inbox, monkeypatch):
    for n in range(3):
        feedback.submit(Cloud(url=""), form(message=f"report {n}"), now=NOW)
    inbox.answers = [(201, {"id": "fbk_a"}, None), RuntimeError("a bug")]
    assert feedback.flush(Cloud(), now=NOW + 1) == {"sent": 1, "dropped": 0, "waiting": 2}
    # The first isn't sent twice: the next round sends only the other two.
    assert feedback.flush(Cloud(), force=True, now=NOW + 2)["sent"] == 2
    assert [body["message"] for body in inbox.bodies()] == ["report 0", "report 1", "report 1", "report 2"]


@pytest.mark.parametrize("answer, complaint", [
    ((400, {"error": "invalid_request", "error_description": "Keep the message to 5,000 characters."}, None),
     "Lumi Cloud refused the report: Keep the message to 5,000 characters."),
    ((413, {"error": "too_large"}, None), "too large for Lumi Cloud"),
    ((404, {"detail": "Not Found"}, None), "doesn't take feedback"),
    ((422, {"error": "nope"}, None), "it answered 422"),
    ((302, {}, {"Location": "https://elsewhere.example/"}), "it answered 302"),
])
def test_what_lumi_cloud_refuses_isnt_kept(inbox, audit_log, answer, complaint):
    inbox.answers = [answer]
    with pytest.raises(feedback.FeedbackError) as refused:
        feedback.submit(Cloud(), form(), now=NOW)
    assert refused.value.code == "refused" and complaint in refused.value.message
    assert queue() == [] and len(inbox.requests) == 1  # no redirect followed
    [record] = records(audit_log)
    assert record["type"] == "feedback.refused" and record["data"]["status"] == answer[0]


@pytest.mark.parametrize("status", [401, 403])
def test_a_lumi_cloud_that_wants_a_sign_in_keeps_the_report(inbox, status):
    inbox.answers = [(status, {"error": "invalid_token"}, None)]
    outcome = feedback.submit(Cloud(), form(), now=NOW)
    assert (outcome.status, outcome.reason) == ("queued", "unauthorized")
    inbox.answers = [(status, {"error": "invalid_token"}, None)]
    assert feedback.flush(Cloud(), force=True, now=NOW + 1) == {"sent": 0, "dropped": 0, "waiting": 1}


def test_waiting_reports_lumi_cloud_refuses_dlp_blocks_or_that_expire_are_dropped(inbox, audit_log):
    for message in ("first", "second", "Project Falcon later"):
        feedback.submit(Cloud(url=""), form(message=message), now=NOW)
    feedback.submit(Cloud(url=""), form(message="old"), now=NOW - feedback.QUEUE_DAYS * 86400 - 60)
    _dlp()  # the organization's rules arrived after these were written
    inbox.answers = [(400, {"error": "invalid_request", "error_description": "No."}, None)]
    assert feedback.flush(Cloud(), now=NOW + 1) == {"sent": 1, "dropped": 3, "waiting": 0}
    assert inbox.bodies()[-1]["message"] == "second"
    dropped = sorted(r["data"]["reason"] for r in records(audit_log, "feedback.dropped"))
    assert dropped == ["dlp", "expired", "refused"]


def test_the_queue_is_bounded(inbox, monkeypatch, audit_log):
    monkeypatch.setattr(feedback, "MAX_RECENT", 100)
    for index in range(feedback.MAX_QUEUED):
        feedback.submit(Cloud(url=""), form(message=f"report {index}"), now=NOW)
    with pytest.raises(feedback.FeedbackError) as full:
        feedback.submit(Cloud(url=""), form(), now=NOW)
    assert full.value.code == "queue_full"
    assert full.value.message.startswith("No Lumi Cloud address is set, and 20 reports are already waiting")
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
    with pytest.raises(feedback.FeedbackError):
        feedback.submit(Cloud(url=""), form(), now=NOW + 11)  # and the queue can't be filled faster
    assert len(inbox.requests) == 4 and len(queue()) == 1
    assert feedback.submit(Cloud(), form(), now=NOW + feedback.RECENT_SECONDS + 6).status == "sent"
    assert [r["data"]["reason"] for r in records(audit_log, "feedback.refused")] == ["rate_limited"] * 2


def test_discarding_deletes_the_waiting_reports(inbox, audit_log):
    feedback.submit(Cloud(url=""), form(), now=NOW)
    feedback.submit(Cloud(url=""), form(kind="idea"), now=NOW)
    assert feedback.discard() == 2
    assert not (state_home() / "feedback" / "queue.json").exists()
    assert feedback.flush(Cloud(), now=NOW + 1)["sent"] == 0 and inbox.requests == []
    assert [(r["data"]["kind"], r["data"]["reason"]) for r in records(audit_log, "feedback.dropped")] == [
        ("bug", "discarded"), ("idea", "discarded")]


def test_discarding_during_a_round_stops_what_hasnt_gone(inbox, audit_log):
    for n in range(3):
        feedback.submit(Cloud(url=""), form(message=f"report {n}"), now=NOW)
    arrived, release = threading.Event(), threading.Event()

    def slow(request):  # the first report is on its way when the person chooses Discard
        arrived.set()
        release.wait(10)
        return httpx.Response(201, json={"id": "fbk_slow"})

    inbox.answers = [slow]
    round_ = threading.Thread(target=lambda: feedback.flush(Cloud(), now=NOW + 1))
    round_.start()
    assert arrived.wait(10)
    assert feedback.discard() == 2  # the one being sent can't be taken back
    assert [item["body"]["message"] for item in queue()] == ["report 0"]
    release.set()
    round_.join(10)
    assert [body["message"] for body in inbox.bodies()] == ["report 0"] and queue() == []
    types = [(r["type"], r["data"].get("reason")) for r in records(audit_log) if r["type"] != "feedback.queued"]
    assert types == [("feedback.dropped", "discarded"), ("feedback.dropped", "discarded"), ("feedback.sent", None)]


def test_the_background_thread_sends_when_woken(inbox, monkeypatch):
    monkeypatch.setattr(feedback, "LOOP_SECONDS", 0.05)
    cloud = Cloud(url="")
    feedback.submit(cloud, form(), now=time.time())
    stop = threading.Event()
    thread = feedback.start_background(cloud, first_delay=0.05, stop=stop)
    try:
        time.sleep(0.3)
        assert inbox.requests == [] and feedback.waiting() == 1  # still nowhere to send it
        cloud.url = cloud.sign_in_url = URL
        feedback.wake()
        deadline = time.monotonic() + 10
        while feedback.waiting() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert feedback.waiting() == 0 and len(inbox.requests) == 1
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
                             provider="conn-8f3a1c", model="acme-large", now=NOW)
    details = shown["body"]["diagnostics"]
    assert set(details) == {"python", "platform", "packaged", "provider", "model", "offline_mode", "log_tail"}
    assert (details["provider"], details["model"], details["offline_mode"]) == ("connection", "acme-large", False)
    tail = details["log_tail"]
    assert "line 199" in tail and "line 100" not in tail and len(tail.splitlines()) <= feedback.LOG_TAIL_LINES
    for secret in (TOKEN, SAVED_KEY, "abcdefghijklmnop", home):
        assert secret not in json.dumps(shown["body"])
    assert "~\\projects\\app.py" in tail
    assert (shown["destination"], shown["account"]) == ("cloud.example.test", "ada@example.com")
    assert shown["body"]["install_id"] == feedback.install_id(URL, "usr_ada")

    # The log grows after the preview: what was shown is what goes.
    log.write_text(log.read_text(encoding="utf-8") + "a later line\n", encoding="utf-8")
    outcome = feedback.submit(Cloud(user_id="usr_ada"), form(include_diagnostics=True), settings=settings,
                              preview_id=shown["preview_id"], now=NOW + 60)
    assert outcome.status == "sent"
    assert inbox.bodies()[0] == shown["body"]
    assert records(audit_log)[-1]["data"]["diagnostics"] is True


def test_diagnostics_go_only_after_the_person_saw_that_report(inbox, settings):
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
    with pytest.raises(feedback.FeedbackError):
        feedback.submit(Cloud(), form(include_diagnostics=True), settings=settings, preview_id=shown["preview_id"],
                        now=time.time() + feedback.PREVIEW_SECONDS + 1)
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


def test_the_copy_is_what_would_have_been_sent(settings):
    text = feedback.copy_text(form(message=f"Broke with {TOKEN}", reply_to="ada@example.com"), settings=settings)
    assert text.startswith("Lumi feedback: Bug\n\nBroke with [REDACTED GitHub token]\n")
    assert "Reply to: ada@example.com" in text and f"Lumi {__version__}" in text and TOKEN not in text
    assert "Diagnostics" not in text
    assert "Diagnostics:" in feedback.copy_text(form(include_diagnostics=True), settings=settings)
    # With the organization's rules: redacted as it would have been sent, and nothing when they block it.
    _dlp(credit_card="redact")
    assert "[REDACTED:credit_card]" in feedback.copy_text(form(message="card 4111 1111 1111 1111"))
    assert feedback.copy_text(form(message="Project Falcon broke")) == ""
    assert feedback.copy_text(form(message="")) == ""


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
    assert {k: status["data"][k] for k in ("destination", "configured", "account", "offline", "waiting")} == {
        "destination": "cloud.example.test", "configured": True, "account": "ada@example.com", "offline": "",
        "waiting": 0}
    assert status["data"]["app"]["version"] == __version__

    [preview] = _command("feedback_preview", Cloud(), settings, request=7, form=form(include_diagnostics=True))
    assert preview["request"] == 7 and preview["data"]["body"]["diagnostics"]["provider"] == "ollama"
    assert preview["data"]["body"]["diagnostics"]["model"] == "qwen3:8b"

    result, after = _command("feedback_send", Cloud(), settings, form=form(include_diagnostics=True),
                             preview_id=preview["data"]["preview_id"])
    assert (result["event"], result["ok"], result["status"]) == ("feedback_result", True, "sent")
    assert after["event"] == "feedback_status"

    [invalid, _status] = _command("feedback_send", Cloud(), settings, form=form(message=""))
    assert (invalid["ok"], invalid["code"], invalid["field"], invalid["copy_text"]) == (False, "invalid", "message", "")

    offline.set_for_tests(enabled=True)
    [refused, _status] = _command("feedback_send", Cloud(), settings, form=form(message=f"Oops {TOKEN}"))
    assert (refused["ok"], refused["code"]) == (False, "offline")
    assert refused["copy_text"].startswith("Lumi feedback: Bug") and TOKEN not in refused["copy_text"]
    [shown] = _command("feedback_preview", Cloud(), settings, request=8, form=form(include_diagnostics=True))
    assert shown["error"]["code"] == "offline" and shown["copy_text"].startswith("Lumi feedback: Bug")
    offline.set_for_tests(enabled=False)

    _dlp()
    [blocked, _status] = _command("feedback_send", Cloud(), settings, form=form(message="Project Falcon broke"))
    assert (blocked["code"], blocked["copy_text"]) == ("dlp", "")  # no copy of what the rules keep here
    lumi_policy.set_for_tests(None)

    feedback.submit(Cloud(url=""), form(), now=time.time())
    [flushed] = _command("feedback_flush", Cloud(), settings)
    assert flushed["data"]["flushed"] == {"sent": 1, "dropped": 0, "waiting": 0}
    feedback.submit(Cloud(url=""), form(), now=time.time())
    [discarded] = _command("feedback_discard", Cloud(), settings)
    assert (discarded["data"]["discarded"], discarded["data"]["waiting"]) == (1, 0)


def test_an_unexpected_failure_still_answers_the_dialog(inbox, settings, monkeypatch):
    def broken(*args, **kwargs):
        raise OSError("disk full")

    feedback._install_secret()
    monkeypatch.setattr(feedback, "_write", broken)  # the disk filled up: the report can't be kept to send later
    [result, status] = _command("feedback_send", Cloud(url=""), settings, form=form())
    assert (result["event"], result["ok"], result["code"]) == ("feedback_result", False, "error")
    assert "couldn't send or save" in result["message"] and result["copy_text"].startswith("Lumi feedback")
    assert status["event"] == "feedback_status"
    monkeypatch.setattr(feedback, "flush", broken)
    [flushed] = _command("feedback_flush", Cloud(), settings)
    assert flushed["data"]["flushed"]["failed"] is True


def test_the_dialogs_long_commands_leave_the_socket_live(inbox, settings):
    from lumi.gui import ws_commands
    from tests.test_connections import _StubWS

    release = threading.Event()
    inbox.answers = [lambda request: (release.wait(10), httpx.Response(201, json={"id": "fbk_x"}))[1]]
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
    sent = [*_command("feedback_status", cloud, settings),
            *_command("feedback_preview", cloud, settings, request=1, form=form(include_diagnostics=True)),
            *_command("feedback_send", cloud, settings, form=form())]
    text = json.dumps(sent)
    assert "access-for-usr_ada" not in text and SAVED_KEY not in text
    assert inbox.requests[-1].headers["authorization"] == "Bearer access-for-usr_ada"
