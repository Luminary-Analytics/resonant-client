"""Data loss prevention: the organization's rules on every outgoing model request (lumi/dlp.py)."""

from __future__ import annotations

import ast
import copy
import json
import threading
import time
from pathlib import Path

import httpx
import pytest

from lumi import audit, dlp, dlp_detectors, policy as lumi_policy
from lumi.audit import AuditLog
from lumi.dlp import Blocked, check_request, parse_section
from lumi.engine.compression import compress
from lumi.engine.request_purpose import auxiliary_stream
from lumi.engine.session import Session
from lumi.engine.session_titles import generate_session_title
from tests.streaming_stub import done, text_delta, tool_call

ROOT = Path(__file__).resolve().parents[1]
BASE = {"schema": "lumi.policy/v1", "organization": "Acme"}
CARD = "4111 1111 1111 1111"
SSN = "123-45-6789"
IBAN = "DE89 3704 0044 0532 0130 00"


def install(section, **extra):
    policy = lumi_policy.parse({**BASE, **extra, "dlp": section}, source="test policy")
    lumi_policy.set_for_tests(policy)
    return policy


def rules(*items, **detectors):
    return {"version": 1, "detectors": detectors, "rules": list(items)}


@dlp.guard_backend
class Backend:
    """Records exactly what each request would send to the provider.

    Guarded like the real backends (lumi/backends.py), so every test here also
    proves its path reaches the backend through dlp.send or dlp.permit.
    """

    def __init__(self, *, scripts=None, events=None, name="ollama", model="test-model"):
        self.name, self.model = name, model
        self.base_url, self.api_key, self.tool_mode, self.handles_tools = "http://test", None, "native", False
        self.requests: list[dict] = []
        self._scripts = list(scripts or [])
        self._events = events

    def stream(self, *, user_msg, conversation_history, instructions, tools, max_tokens=None, cancel_event=None):
        self.requests.append(copy.deepcopy({"user_msg": user_msg, "conversation_history": conversation_history,
                                            "instructions": instructions}))
        events = self._scripts.pop(0) if self._scripts else (self._events or [text_delta("Done."), done()])
        yield from events

    def classify(self, prompt, max_tokens=20):
        self.requests.append({"classify": prompt})
        return "SIMPLE"

    def sent(self, index=-1) -> str:
        return json.dumps(self.requests[index], ensure_ascii=False)


@pytest.fixture
def audit_log(tmp_path):
    log = AuditLog(tmp_path / "audit")
    audit.set_for_tests(log)
    return log


def read_log(log: AuditLog) -> list[dict]:
    return [json.loads(line) for path in sorted(log.root.glob("*.jsonl"))
            for line in path.read_text(encoding="utf-8").splitlines()]


def findings(log: AuditLog) -> list[dict]:
    return [record["data"] for record in read_log(log) if record["type"] == "dlp.finding"]


def request(user_msg="", history=None, instructions="You are helpful."):
    return {"user_msg": user_msg, "conversation_history": history if history is not None else [],
            "instructions": instructions, "tools": [], "max_tokens": 100}


# ── Built-in detectors ───────────────────────────────────────────────────────


def spans(detector: str, text: str) -> list[str]:
    """What a detector finds in ``text``, read as DLP reads it (normalized), as the original's text."""
    copy_ = dlp_detectors.normalized(text)
    return [text[slice(*copy_.span(start, end))] for start, end in dlp_detectors.DETECTORS[detector](copy_.text)]


@pytest.mark.parametrize("number", [
    "4111 1111 1111 1111", "4111-1111-1111-1111", "4111111111111111", "5555555555554444",
    "2223003122003222", "378282246310005", "3782 822463 10005", "6011111111111117",
    "3530111333300000", "30569309025904", "4000056655665556",
])
def test_card_numbers_are_found(number):
    assert spans("credit_card", f"Pay with {number}, thanks") == [number]


@pytest.mark.parametrize("text", [
    "4111 1111 1111 1112",       # fails the Luhn check
    "5555555555554445",          # fails the Luhn check
    "1234567812345670",          # passes Luhn, but no card network starts with 1
    "0000 0000 0000 0000",
    "12345678901234567890",      # too long
    "2026-09-27 12:00:00",
    "order 41111111 111",
])
def test_digits_that_arent_cards_are_left_alone(text):
    assert spans("credit_card", f"x {text} y") == []


def test_a_card_next_to_other_numbers_is_still_found():
    assert spans("credit_card", "ids 4111111111111111-12 and 9999 4111 1111 1111 1111") == [
        "4111111111111111", "4111 1111 1111 1111"]


@pytest.mark.parametrize("number", ["123-45-6789", "123 45 6789", "078-05-1120"])
def test_social_security_numbers_are_found(number):
    assert spans("us_ssn", f"SSN: {number}.") == [number]


@pytest.mark.parametrize("text", [
    "000-12-3456", "666-12-3456", "912-34-5678", "123-00-4567", "123-45-0000", "123456789",
    "123-45-67890", "1123-45-6789", "123-45 6789", "555-123-4567",
])
def test_numbers_that_arent_social_security_numbers_are_left_alone(text):
    assert spans("us_ssn", f"x {text} y") == []


@pytest.mark.parametrize("iban", [
    "DE89 3704 0044 0532 0130 00", "DE89370400440532013000", "GB82 WEST 1234 5698 7654 32",
    "FR14 2004 1010 0505 0001 3M02 606", "NL91ABNA0417164300", "BE68 5390 0754 7034",
    "CH93 0076 2011 6238 5295 7", "NO9386011117947", "de89370400440532013000",
])
def test_ibans_are_found(iban):
    assert spans("iban", f"Transfer to {iban}.") == [iban]


@pytest.mark.parametrize("text", [
    "DE89 3704 0044 0532 0130 01",   # fails mod-97
    "GB82 WEST 1234 5698 7654 33",   # fails mod-97
    "XX89 3704 0044 0532 0130 00",   # no such country
    "DE89 3704 0044 0532 0130",      # too short for Germany
    "DE89 3704 0044 0532 0130 00X",  # runs on
    "DE89  3704 0044 0532 0130 00",  # two spaces
])
def test_ibans_that_dont_check_out_are_left_alone(text):
    assert spans("iban", f"x {text} y") == []


def test_iban_checks():
    assert dlp_detectors.iban_ok("DE89370400440532013000")
    assert not dlp_detectors.iban_ok("DE89370400440532013001")
    assert not dlp_detectors.iban_ok("DE8937040044053201300")


@pytest.mark.parametrize("text,secret", [
    ("token ghp_" + "a1" * 18 + " end", "ghp_" + "a1" * 18),
    ("key AKIAIOSFODNN7EXAMPLE end", "AKIAIOSFODNN7EXAMPLE"),
    ("postgres://app:hunter2hunter2@db:5432/app", "hunter2hunter2"),
    ("export API_TOKEN=abcdefgh12345678", "abcdefgh12345678"),
    ("-----BEGIN RSA PRIVATE KEY-----\nMIIE\n-----END RSA PRIVATE KEY-----",
     "-----BEGIN RSA PRIVATE KEY-----\nMIIE\n-----END RSA PRIVATE KEY-----"),
    ("anthropic sk-ant-api03-" + "x" * 30, "sk-ant-api03-" + "x" * 30),
])
def test_secret_formats_come_from_secret_scan(text, secret):
    assert spans("secrets", text) == [secret]


@pytest.mark.parametrize("text", ["hello world", "ghp_short", "https://example.com/docs?page=2"])
def test_ordinary_text_has_no_secrets(text):
    assert spans("secrets", text) == []


def test_email_addresses():
    assert spans("email", "Mail jane.doe@example.com or a+b@mail.example.co.uk.") == [
        "jane.doe@example.com", "a+b@mail.example.co.uk"]
    assert spans("email", "@handle, user@localhost, a@b") == []


# Unicode formatting: characters written with chr() so the source stays plain ASCII.
NBSP, NARROW_NBSP, EN_DASH, ZWSP, SOFT_HYPHEN, FI = chr(0xA0), chr(0x202F), chr(0x2013), chr(0x200B), chr(0xAD), chr(0xFB01)


def full_width(text: str) -> str:
    return "".join(chr(ord(c) + 0xFEE0) if "!" <= c <= "~" else c for c in text)


@pytest.mark.parametrize("number", [
    f"4111{NBSP}1111{NBSP}1111{NBSP}1111", f"4111{NARROW_NBSP}1111{NARROW_NBSP}1111{NARROW_NBSP}1111",
    f"4111{EN_DASH}1111{EN_DASH}1111{EN_DASH}1111", "4111.1111.1111.1111", full_width("4111111111111111"),
    f"4111{ZWSP}1111{ZWSP}1111{ZWSP}1111", f"41{SOFT_HYPHEN}11111111111111",
])
def test_formatted_card_numbers_are_found(number):
    # The span is the original text's: all of the number, invisible characters included.
    assert spans("credit_card", f"Pay with {number}, thanks") == [number]


@pytest.mark.parametrize("text,number", [
    (f"SSN: 123{EN_DASH}45{EN_DASH}6789.", f"123{EN_DASH}45{EN_DASH}6789"),
    (f"SSN: 123{NBSP}45{NBSP}6789.", f"123{NBSP}45{NBSP}6789"),
    (f"SSN: {full_width('123-45-6789')}.", full_width("123-45-6789")),
    ("SSN-123-45-6789", "123-45-6789"),
])
def test_formatted_social_security_numbers_are_found(text, number):
    assert spans("us_ssn", text) == [number]


@pytest.mark.parametrize("text", ["1-123-45-6789", "123-45-6789-1", f"9{EN_DASH}123-45-6789"])
def test_social_security_numbers_inside_longer_numbers_are_left_alone(text):
    assert spans("us_ssn", f"x {text} y") == []


def test_ibans_grouped_with_no_break_spaces_are_found():
    iban = IBAN.replace(" ", NBSP)
    assert spans("iban", f"Transfer to {iban}.") == [iban]


@pytest.mark.parametrize("text", [
    "Project\nFalcon", "Project  Falcon", f"Project{NBSP}Falcon", "Project \r\n\t Falcon",
    f"Pro{ZWSP}ject Falcon", full_width("Project Falcon"),
])
def test_keywords_match_however_the_text_is_spaced_or_encoded(text):
    policy = parse_section(rules({"name": "falcon", "keywords": ["Project Falcon"], "action": "flag"}))
    sentence = f"about {text} today"
    assert [sentence[start:end] for *_rule, start, end in dlp.scan_text(sentence, policy=policy)] == [text]


def test_keywords_are_normalized_like_the_text():
    policy = parse_section(rules({"name": "codename", "keywords": [f"  Project{NBSP}{NBSP}Falcon ", "finance"],
                                  "action": "flag"}))
    assert len(dlp.scan_text("Project Falcon", policy=policy)) == 1
    found = dlp.scan_text(f"{FI}nance report", policy=policy)  # a ligature is "fi"
    assert [(start, end) for *_rest, start, end in found] == [(0, 6)]  # the whole original word
    with pytest.raises(dlp.DlpError, match="no visible characters"):
        parse_section(rules({"name": "x", "keywords": [ZWSP + SOFT_HYPHEN], "action": "flag"}))


def test_redactions_cover_the_original_characters():
    install(rules({"name": "falcon", "keywords": ["Project Falcon"], "action": "redact"}, credit_card="redact"))
    text = f"card 4111{ZWSP}1111{ZWSP}1111{ZWSP}1111 for Pro{ZWSP}ject{NBSP}Falcon, done"
    sent = check_request(request(text), purpose="primary").request["user_msg"]
    assert sent == "card [REDACTED:credit_card] for [REDACTED:falcon], done"


# ── Keyword and pattern rules ────────────────────────────────────────────────


def test_keywords_ignore_case_and_match_whole_words_by_default():
    policy = parse_section(rules({"name": "falcon", "keywords": ["Project Falcon", "FALCON-X"], "action": "flag"}))
    found = dlp.scan_text("PROJECT FALCON, falcon-x. Falconry isn't it; project falcons isn't either.",
                          policy=policy)
    assert [(name, action) for name, action, _s, _e in found] == [("falcon", "flag"), ("falcon", "flag")]


def test_keyword_options():
    exact = parse_section(rules({"name": "code", "keywords": ["Falcon"], "action": "flag", "case_sensitive": True}))
    assert len(dlp.scan_text("Falcon falcon", policy=exact)) == 1
    anywhere = parse_section(rules({"name": "code", "keywords": ["Falcon"], "action": "flag", "whole_word": False}))
    assert len(dlp.scan_text("Falconry", policy=anywhere)) == 1


def test_pattern_rules():
    policy = parse_section(rules(
        {"name": "customer-id", "pattern": r"CUST-\d{8}", "action": "redact"},
        {"name": "internal-host", "pattern": r"[a-z0-9-]{1,40}\.corp\.example\.com", "action": "flag"}))
    found = dlp.scan_text("CUST-12345678 on build-01.corp.example.com, cust-00000001", policy=policy)
    assert [name for name, *_ in found] == ["customer-id", "customer-id", "internal-host"]


@pytest.mark.parametrize("pattern,problem", [
    (r"a+", "repeats must have a limit"),
    (r"secret\d*", "repeats must have a limit"),
    (r"(?=.*secret)x", "repeats must have a limit"),
    (r"(a{1,10}){1,10}", "compete for the same characters"),
    (r"\d{1,20}\d{1,20}x", "compete for the same characters"),
    (r"(\w{1,5})\1", "backreferences"),
    (r"x?", "empty text"),
    (r"[a-z]{1,200}", "more than 128 characters"),
    (r"(unclosed", "isn't a valid regular expression"),
    # A lookaround runs its whole search each time the pattern reaches it:
    # inside a repeat, once per repetition (about 11 s per MB, and 17 minutes
    # with one more level, before lookarounds' work was counted).
    (r"(?:(?![a-z]{1,8}[a-z]{1,2}0)[a-z]){1,120}0", "could take too long"),
    (r"(?:(?!(?:(?![a-z]{1,8}[a-z]{1,2}0)[a-z]){1,9}0)[a-z]){1,120}0", "could take too long"),
    (r"(?>[a-z]{1,4}[a-z]{1,4}){1,15}0", "could take too long"),  # an atomic group's work, too
])
def test_patterns_that_could_scan_slowly_are_refused(pattern, problem):
    with pytest.raises(dlp.DlpError, match=problem) as refused:
        parse_section(rules({"name": "bad", "pattern": pattern, "action": "block"}))
    assert "dlp.rules[0] (bad).pattern" in str(refused.value)


def test_reasonable_patterns_pass_the_check():
    for pattern in (r"CUST-\d{8}", r"[A-Z]{2}\d{6,10}", r"[a-z0-9-]{1,63}\.corp\.example\.com",
                    r"(?:acct|invoice|order)[ #:]{0,3}\d{8}", r"(?i)acme-[a-z]{2,4}\d{3,5}",
                    r"\b[\w.+-]{1,64}@example\.com\b", r"(?<![0-9])\d{16}(?![0-9])",
                    r"(?:[a-z]{1,4}+){1,20}0", r"[a-z]{1,127}0"):
        dlp_detectors.check_pattern(pattern, case_sensitive=False)


# ── The dlp section ──────────────────────────────────────────────────────────


@pytest.mark.parametrize("section,problem", [
    ("yes", "dlp must be an object"),
    ({"detectors": {"credit_card": "block"}}, "dlp.version must be 1"),
    ({"version": 2}, "dlp.version must be 1"),
    ({"version": True}, "dlp.version must be 1"),
    ({"version": 1, "detector": {}}, "unknown keys: detector"),
    ({"version": 1, "detectors": {"passport": "block"}}, "unknown detector 'passport'"),
    ({"version": 1, "detectors": {"credit_card": "warn"}}, "action must be one of"),
    ({"version": 1, "detectors": {"credit_card": {"action": "block", "scope": ["email"]}}}, "scope must list"),
    ({"version": 1, "detectors": {"credit_card": {"action": "block", "where": "all"}}}, "unknown keys: where"),
    ({"version": 1, "rules": [{"name": "x", "action": "flag"}]}, "needs either keywords or a pattern"),
    ({"version": 1, "rules": [{"name": "x", "keywords": ["a"], "pattern": "a", "action": "flag"}]},
     "needs either keywords or a pattern"),
    ({"version": 1, "rules": [{"name": "x", "keywords": [], "action": "flag"}]}, "keywords must list"),
    ({"version": 1, "rules": [{"name": "x", "keywords": ["a"], "action": "flag", "colour": "red"}]},
     "unknown keys: colour"),
    ({"version": 1, "rules": [{"name": "!", "keywords": ["a"], "action": "flag"}]}, "name must be"),
    ({"version": 1, "rules": [{"name": "x", "pattern": "a", "action": "flag", "whole_word": True}]},
     "whole_word applies only to keywords"),
    ({"version": 1, "detectors": {"email": "flag"}, "rules": [{"name": "Email", "keywords": ["a"], "action": "flag"}]},
     "two rules are named"),
    ({"version": 1, "service": {"url": "http://dlp.example.com"}}, "https URL"),
    ({"version": 1, "service": {"url": "https://user:pw@dlp.example.com"}}, "https URL"),
    ({"version": 1, "service": {"url": "https://dlp.example.com", "timeout_seconds": 600}}, "timeout_seconds"),
    ({"version": 1, "service": {"url": "https://dlp.example.com", "on_error": "ignore"}}, "on_error"),
])
def test_the_section_is_parsed_strictly(section, problem):
    with pytest.raises(dlp.DlpError, match=problem):
        parse_section(section)


def test_an_invalid_section_fails_closed_but_the_rest_of_the_policy_applies(audit_log):
    policy = install({"version": 2, "detectors": {"credit_card": "block"}},
                     settings={"privacy.secret_scan": True}, models={"blocked": ["openrouter:*"]},
                     files={"exclude": ["*.pem"]})
    # The policy is there, with everything but its DLP rules...
    assert policy.organization == "Acme" and policy.locked("privacy", "secret_scan")
    assert policy.exclude == ("*.pem",) and not policy.model_allowed("openrouter", "x")
    assert policy.dlp is None and "dlp.version must be 1" in policy.dlp_error
    assert policy.summary()["dlp_error"] == policy.dlp_error
    # ...and nothing is sent to a model until the section is fixed.
    reason = lumi_policy.blocked_reason()
    assert "data loss prevention rules can't be applied" in reason and "dlp.version" in reason
    backend = Backend()
    events = list(Session(backend, auto_approve=True).run("hello"))
    assert backend.requests == [] and any("data loss prevention" in e.get("message", "") for e in events)
    with pytest.raises(Blocked) as blocked:
        list(auxiliary_stream(backend, "title", user_msg="hello", conversation_history=[], instructions="t",
                              tools=[], max_tokens=10))
    assert blocked.value.code == "policy_blocked" and backend.requests == []


def test_an_invalid_section_in_a_machine_policy_file_keeps_the_policy(tmp_path, monkeypatch):
    monkeypatch.setattr(lumi_policy, "_registry_policy", lambda: None)
    monkeypatch.setattr(lumi_policy, "_macos_managed_policy", lambda: None)
    monkeypatch.setattr(lumi_policy, "_machine_file_policy", lambda: None)
    monkeypatch.setattr(lumi_policy, "machine_keys", lambda: {})
    path = tmp_path / "policy.json"
    path.write_text(json.dumps({**BASE, "permissions": {"allowed_modes": ["ask"]},
                                "dlp": {"version": 1, "rules": [{"name": "x", "pattern": "a+", "action": "block"}]}}),
                    encoding="utf-8")
    monkeypatch.setenv("LUMI_POLICY_FILE", str(path))
    state = lumi_policy.load(force=True)
    assert state.error == "" and state.policy is not None
    assert not state.policy.mode_allowed("bypass")  # the rest applies
    assert "repeats must have a limit" in state.policy.dlp_error
    assert "dlp section" in lumi_policy.blocked_reason()


def test_settings_show_rule_names_and_actions_but_never_keywords_or_patterns(tmp_path):
    from lumi.gui.settings import SettingsManager

    install({"version": 1, "detectors": {"credit_card": "block"},
             "rules": [{"name": "falcon", "keywords": ["Project Falcon"], "action": "redact", "scope": ["prompt"]},
                       {"name": "customer-id", "pattern": r"CUST-\d{8}", "action": "flag"}],
             "service": {"url": "https://dlp.example.com/check", "on_error": "allow"}})
    meta = SettingsManager(tmp_path / "settings.json").get_masked()["_meta"]["policy"]
    shown = meta["summary"]["dlp"]
    assert shown["rules"] == [
        {"name": "credit_card", "action": "block", "type": "detector", "scope": []},
        {"name": "falcon", "action": "redact", "type": "keywords", "scope": ["prompt"]},
        {"name": "customer-id", "action": "flag", "type": "pattern", "scope": []},
    ]
    assert shown["service"] == "dlp.example.com" and shown["service_on_error"] == "allow"
    text = json.dumps(meta)
    assert "Project Falcon" not in text and "CUST-" not in text


# ── Actions on a request ─────────────────────────────────────────────────────


def test_redact_changes_only_the_copy_that_is_sent(audit_log):
    install(rules(credit_card="redact"))
    history = [{"role": "user", "content": f"Charge {CARD} now"}]
    original = copy.deepcopy(history)
    checked = check_request(request(f"Charge {CARD} now", history), purpose="primary", provider="p", model="m")
    sent = checked.request
    assert sent["user_msg"] == "Charge [REDACTED:credit_card] now"
    assert sent["conversation_history"][0]["content"] == "Charge [REDACTED:credit_card] now"
    assert history == original  # the conversation keeps the original
    # The message and its copy in the history are the same text: one match to report.
    assert "redacted 1 match " in checked.notice and CARD not in checked.notice


def test_flag_sends_unchanged_and_records(audit_log):
    install(rules(us_ssn="flag"))
    sent = request(f"SSN {SSN}", [{"role": "user", "content": f"SSN {SSN}"}])
    checked = check_request(sent, purpose="primary", provider="ollama", model="m",
                            audit_fields={"session": "s1", "project": "/p"})
    assert checked.request is sent and checked.notice == ""
    assert {(f["rule"], f["action"], f["kind"], f["count"]) for f in findings(audit_log)} == {("us_ssn", "flag", "prompt", 1)}


def test_block_refuses_and_names_the_rule_never_the_match(audit_log):
    install(rules({"name": "falcon", "keywords": ["Project Falcon"], "action": "block"},
                  {"name": "customer-id", "pattern": r"CUST-\d{8}", "action": "block"}, iban="block"))
    history = [{"role": "user", "content": "hi"},
               {"role": "tool_result", "call_id": "c1", "content": f"Project Falcon: CUST-12345678, {IBAN}"}]
    with pytest.raises(Blocked) as blocked:
        check_request(request("summarize", history), purpose="primary")
    message = blocked.value.message
    assert blocked.value.code == "dlp_blocked" and blocked.value.entries == (1,)
    for name in ("falcon", "customer-id", "iban"):
        assert f"{name} in a tool result" in message
    for secret in ("Project Falcon", "CUST-12345678", "12345678", IBAN, "DE89", "3704"):
        assert secret not in message
    assert "Nothing was sent" in message


def test_block_wins_over_redact_in_the_same_request(audit_log):
    install(rules(credit_card="redact", us_ssn="block"))
    with pytest.raises(Blocked, match="us_ssn in your message"):
        check_request(request(f"{CARD} and {SSN}"), purpose="primary")
    assert [f["action"] for f in findings(audit_log)] == ["block"]


def test_scope_limits_a_rule_to_kinds_of_content():
    install(rules({"name": "falcon", "keywords": ["Project Falcon"], "action": "redact", "scope": ["prompt"]}))
    history = [
        {"role": "user", "content": "About Project Falcon"},
        {"role": "tool_result", "call_id": "c", "content": "Project Falcon README"},
        # A compaction summary quotes several kinds: every rule checks it.
        {"role": "assistant", "content": "[Previous conversation summary]\nProject Falcon", "preserved_context": {}},
    ]
    sent = check_request(request("", history), purpose="primary").request["conversation_history"]
    assert sent[0]["content"] == "About [REDACTED:falcon]"
    assert sent[1]["content"] == "Project Falcon README"
    assert sent[2]["content"].endswith("[REDACTED:falcon]")


def test_instructions_are_checked_by_part():
    install(rules({"name": "falcon", "keywords": ["Project Falcon"], "action": "redact", "scope": ["attachment"]}))
    segments = [("instructions", "System. Project Falcon is our codename. "),
                ("attachment", "\n--- EXPLICIT CONTEXT ATTACHMENTS ---\nProject Falcon plan")]
    instructions = "".join(text for _, text in segments) + "\nHook context: Project Falcon"
    sent = check_request(request("", [], instructions), purpose="primary", segments=segments).request
    assert sent["instructions"] == ("System. Project Falcon is our codename. "
                                    "\n--- EXPLICIT CONTEXT ATTACHMENTS ---\n[REDACTED:falcon] plan"
                                    "\nHook context: Project Falcon")


def test_tool_call_arguments_stay_valid_json_after_redaction():
    install(rules(credit_card="redact", secrets="redact"))
    arguments = json.dumps({"path": "fixtures/cards.txt", "content": f'card = "{CARD}"\\n',
                            "count": 4111111111111111, "nested": [{"token": "ghp_" + "a1" * 18}]})
    history = [
        {"role": "user", "content": "write the fixture"},
        {"role": "tool_call", "name": "file_write", "call_id": "c1", "arguments": arguments,
         "content": "Called file_write", "response_id": "r1",
         "response_tool_calls": [{"id": "c1", "type": "function",
                                  "function": {"name": "file_write", "arguments": arguments}}]},
        {"role": "tool_result", "call_id": "c1", "content": "wrote 1 file"},
    ]
    sent = check_request(request("", history), purpose="primary").request["conversation_history"][1]
    for text in (sent["arguments"], sent["response_tool_calls"][0]["function"]["arguments"]):
        value = json.loads(text)
        assert set(value) == {"path", "content", "count", "nested"} and value["path"] == "fixtures/cards.txt"
        assert value["content"] == 'card = "[REDACTED:credit_card]"\\n'
        assert value["count"] == "[REDACTED:credit_card]"
        assert value["nested"] == [{"token": "[REDACTED:secrets]"}]
    assert (sent["name"], sent["call_id"], sent["response_id"]) == ("file_write", "c1", "r1")
    assert CARD not in json.dumps(sent)
    assert CARD in history[1]["arguments"]  # the conversation keeps the original


def tool_call_entry(arguments: str, **extra) -> dict:
    return {"role": "tool_call", "name": "file_write", "call_id": "c1", "arguments": arguments,
            "content": "Called file_write", **extra}


def test_tool_argument_keys_are_checked_too():
    install(rules({"name": "falcon", "keywords": ["Project Falcon"], "action": "redact"}, credit_card="redact"))
    arguments = '{"labels": {"Project Falcon": 1, "4111111111111111": 2, "5555555555554444": 3}}'
    sent = check_request(request("", [tool_call_entry(arguments)]), purpose="primary").request
    labels = json.loads(sent["conversation_history"][0]["arguments"])["labels"]
    # Keys redacted alike are numbered, so no value is lost.
    assert labels == {"[REDACTED:falcon]": 1, "[REDACTED:credit_card]": 2, "[REDACTED:credit_card] #2": 3}
    install(rules(credit_card="block"))
    with pytest.raises(Blocked, match="credit_card in an earlier reply"):
        check_request(request("", [tool_call_entry(arguments)]), purpose="primary")


def test_arguments_with_a_duplicated_key_go_out_as_the_tool_read_them(audit_log):
    install(rules(us_ssn="block"))
    # json.loads keeps the last value, and so did the tool; the first never reaches the model.
    arguments = f'{{"content": "SSN {SSN}", "content": "ok"}}'
    history = [tool_call_entry(arguments, response_tool_calls=[
        {"id": "c1", "type": "function", "function": {"name": "file_write", "arguments": arguments}}])]
    sent = check_request(request("", history), purpose="primary").request["conversation_history"][0]
    assert sent["arguments"] == '{"content": "ok"}'
    assert sent["response_tool_calls"][0]["function"]["arguments"] == '{"content": "ok"}'
    assert SSN not in json.dumps(sent) and history[0]["arguments"] == arguments


def test_a_withheld_entry_without_content_is_sent_as_a_notice():
    # Older saved conversations have entries with no "content" at all.
    install(rules(us_ssn="block"))
    entry = {"role": "tool_call", "name": "bash", "call_id": "c1", "arguments": json.dumps({"command": f"echo {SSN}"}),
             "dlp_withheld": True}
    sent = check_request(request("next", [entry, {"role": "user", "content": "next"}]),
                         purpose="primary").request["conversation_history"][0]
    assert sent["content"].startswith("[Withheld:") and sent["arguments"] == "{}"
    assert SSN not in json.dumps(sent) and "content" not in entry


def test_a_withheld_message_isnt_learning_input_for_sonn():
    from lumi.sonn import SonnBackend

    install(rules(us_ssn="block"))
    history = [{"role": "user", "content": f"My SSN is {SSN}", "dlp_withheld": True},
               {"role": "assistant", "content": "Understood."},
               {"role": "user", "content": "What's next?"}]
    sent = check_request(request("What's next?", history), purpose="primary").request
    body = SonnBackend("key", base_url="https://sonn.example/v1/workspace/projects/fixture/openai/v1")._payload(
        sent["user_msg"], sent["conversation_history"], sent["instructions"], [], 256)
    generated = body["metadata"]["sonn_generated_user_indices"]
    users = [(index, message["content"]) for index, message in enumerate(body["messages"]) if message["role"] == "user"]
    # The notice Lumi wrote in the message's place is marked generated; the person's next message isn't.
    assert [(index in generated, text.startswith("[Withheld:")) for index, text in users] == [(True, True), (False, False)]
    assert SSN not in json.dumps(body)


def test_messages_lumi_writes_are_checked_by_every_rule():
    # A hook's context or a nudge can quote tool output: rules scoped to tool
    # results check it, and the same text sent as the message is checked alike.
    install(rules({"name": "customer-id", "pattern": r"CUST-\d{8}", "action": "redact", "scope": ["tool_result"]}))
    nudge = "The last tool returned CUST-12345678; check it."
    history = [{"role": "user", "content": "go"}, {"role": "user", "content": nudge, "input_origin": "generated"}]
    sent = check_request(request(nudge, history), purpose="primary").request
    assert sent["user_msg"] == sent["conversation_history"][1]["content"] == (
        "The last tool returned [REDACTED:customer-id]; check it.")


def test_signed_reasoning_with_a_match_is_left_out_not_edited():
    install(rules(credit_card="redact"))
    history = [{"role": "user", "content": "go"},
               {"role": "tool_call", "name": "bash", "call_id": "c1", "arguments": "{}", "content": "Called bash",
                "reasoning_content": f"The card is {CARD}",
                "reasoning_details": [{"type": "thinking", "thinking": f"card {CARD}", "signature": "sig"}]},
               {"role": "tool_result", "call_id": "c1", "content": "ok"}]
    sent = check_request(request("", history), purpose="primary").request["conversation_history"][1]
    assert "reasoning_content" not in sent and "reasoning_details" not in sent
    assert history[1]["reasoning_details"][0]["signature"] == "sig"


def test_the_same_content_is_recorded_once_per_session_and_model(audit_log):
    install(rules(credit_card="flag"))
    history = [{"role": "user", "content": f"card {CARD}"}]
    for _ in range(3):
        check_request(request("", history), purpose="primary", provider="ollama", model="a",
                      audit_fields={"session": "s1"})
    check_request(request("", history), purpose="primary", provider="ollama", model="b",
                  audit_fields={"session": "s1"})
    assert [f["model"] for f in findings(audit_log)] == ["a", "b"]


def test_a_withheld_entry_goes_out_again_once_no_rule_blocks_it():
    history = [{"role": "user", "content": f"SSN {SSN}", "dlp_withheld": True},
               {"role": "user", "content": "next"}]
    install(rules(us_ssn="block"))
    sent = check_request(request("next", history), purpose="primary").request["conversation_history"]
    assert sent[0]["content"].startswith("[Withheld: this content was blocked") and "us_ssn" in sent[0]["content"]
    install(rules(us_ssn="redact"))  # the administrator relaxed the rule
    sent = check_request(request("next", history), purpose="primary").request["conversation_history"]
    assert sent[0]["content"] == "SSN [REDACTED:us_ssn]"


def test_a_check_that_fails_refuses_the_request(audit_log):
    policy = install(rules(credit_card="redact"))

    def broken(text):
        raise RuntimeError("detector bug")

    object.__setattr__(policy.dlp.rules[0], "find", broken)  # this test's own policy object
    with pytest.raises(Blocked, match="couldn't check this request"):
        check_request(request("hello"), purpose="primary")
    assert [r["data"]["reason"] for r in read_log(audit_log) if r["type"] == "dlp.error"] == ["internal"]


def test_project_instructions_are_instructions_for_scoped_rules():
    install(rules({"name": "falcon", "keywords": ["Project Falcon"], "action": "redact", "scope": ["instructions"]}))
    backend = Backend()
    session = Session(backend, auto_approve=True, max_steps=1,
                      project_instructions="Never mention Project Falcon outside the team.")
    list(session.run("What does Project Falcon do?"))
    sent = backend.requests[0]
    assert "Never mention [REDACTED:falcon] outside the team." in sent["instructions"]
    assert sent["user_msg"] == "What does Project Falcon do?"  # prompts aren't in this rule's scope


def test_no_policy_means_no_change():
    sent = request(f"card {CARD}")
    assert check_request(sent, purpose="primary").request is sent


def test_a_request_too_large_to_check_is_refused(monkeypatch):
    install(rules(credit_card="flag"))
    monkeypatch.setattr(dlp, "MAX_REQUEST_CHARS", 100)
    with pytest.raises(Blocked, match="larger than"):
        check_request(request("x" * 200), purpose="primary")


# ── Every outgoing request: turns, auxiliary requests, Team workers ─────────


def test_a_turn_sends_the_redacted_copy_and_shows_a_marker(audit_log):
    install(rules(credit_card="redact"))
    backend = Backend()
    session = Session(backend, auto_approve=True, max_steps=2)
    events = list(session.run(f"Refund card {CARD}"))
    assert len(backend.requests) == 1 and CARD not in backend.sent()
    assert backend.requests[0]["user_msg"] == "Refund card [REDACTED:credit_card]"
    assert session.conversation_history[0]["content"] == f"Refund card {CARD}"
    markers = [e for e in events if e.get("event") == "backend.status" and e.get("kind") == "dlp_redacted"]
    assert len(markers) == 1 and markers[0]["rules"] == {"credit_card": 1}
    record = findings(audit_log)[0]
    assert (record["rule"], record["action"], record["kind"], record["purpose"]) == (
        "credit_card", "redact", "prompt", "primary")


def test_a_tool_result_is_checked_before_the_next_request(tmp_path, audit_log):
    install(rules({"name": "customer-id", "pattern": r"CUST-\d{8}", "action": "redact", "scope": ["tool_result"]}))
    (tmp_path / "customers.csv").write_text("id\nCUST-12345678\n", encoding="utf-8")
    backend = Backend(scripts=[[tool_call("file_read", {"path": "customers.csv"}), done()],
                               [text_delta("One customer."), done()]])
    session = Session(backend, auto_approve=True, max_steps=3)
    session.project_path = str(tmp_path)
    list(session.run("How many customers?"))
    assert len(backend.requests) == 2
    assert "CUST-12345678" not in backend.sent(1) and "[REDACTED:customer-id]" in backend.sent(1)
    assert any("CUST-12345678" in str(entry.get("content")) for entry in session.conversation_history)


def test_a_blocked_turn_sends_nothing_and_later_turns_leave_the_content_out(audit_log):
    install(rules(us_ssn="block"))
    backend = Backend()
    session = Session(backend, auto_approve=True, max_steps=2)
    events = list(session.run(f"My SSN is {SSN}"))
    assert backend.requests == []
    error = next(e for e in events if e.get("event") == "error")
    assert error["code"] == "dlp_blocked" and "us_ssn in your message" in error["message"]
    assert SSN not in error["message"] and "6789" not in error["message"]
    end = next(e for e in events if e.get("event") == "session.end")
    assert end["evidence"]["model_requests"] == 0  # nothing left, so no request was made
    # The next turn goes out without it; this computer keeps the original.
    list(session.run("Never mind. What's 2 + 2?"))
    assert len(backend.requests) == 1 and SSN not in backend.sent()
    assert "[Withheld: this content was blocked" in backend.sent() and "us_ssn" in backend.sent()
    assert session.conversation_history[0]["content"] == f"My SSN is {SSN}"
    assert session.conversation_history[0]["dlp_withheld"] is True
    assert [f["action"] for f in findings(audit_log)] == ["block"]


def test_audit_records_never_contain_matched_text(audit_log):
    install(rules({"name": "falcon", "keywords": ["Project Falcon"], "action": "flag"},
                  credit_card="redact", us_ssn="block"))
    backend = Backend()
    session = Session(backend, auto_approve=True, max_steps=2)
    session.audit_session_id = "conversation-1"
    list(session.run(f"Project Falcon card {CARD}"))
    list(session.run(f"and SSN {SSN}"))
    records = [r for r in read_log(audit_log) if r["type"].startswith("dlp.")]
    assert {(r["data"]["rule"], r["data"]["action"]) for r in records} == {
        ("falcon", "flag"), ("credit_card", "redact"), ("us_ssn", "block")}
    assert all(r["session"] == "conversation-1" and r["data"]["count"] >= 1 for r in records)
    log_text = "\n".join(path.read_text(encoding="utf-8") for path in audit_log.root.glob("*.jsonl"))
    for secret in ("Project Falcon", CARD, "4111111111111111", SSN):
        assert secret not in log_text


def test_title_requests_are_checked():
    install(rules(credit_card="block"))
    backend = Backend(events=[text_delta("Refund a card"), done()])
    assert generate_session_title(backend, f"Refund card {CARD}", threading.Event()) == ""
    assert backend.requests == []
    install(rules(credit_card="redact"))
    assert generate_session_title(backend, f"Refund card {CARD}", threading.Event()) == "Refund a card"
    assert CARD not in backend.sent() and "[REDACTED:credit_card]" in backend.requests[0]["user_msg"]


def test_compaction_requests_are_checked():
    install(rules({"name": "falcon", "keywords": ["Project Falcon"], "action": "redact", "scope": ["prompt"]}))
    summary = {key: "Facts" for key in
               ("summary", "decisions", "changes", "verification", "unresolved_failures", "next_action")}
    summarizer = Backend(events=[text_delta(json.dumps(summary)), done()])
    session = Session(Backend(), auto_approve=True)
    session.conversation_history = [{"role": "user", "content": "Project Falcon " * 200} for _ in range(9)]
    compress(session, backend=summarizer, max_tokens=20)
    assert summarizer.requests and "Project Falcon" not in summarizer.sent()
    assert "[REDACTED:falcon]" in summarizer.requests[0]["user_msg"]


def test_compaction_leaves_out_withheld_entries_so_it_still_works():
    install(rules(us_ssn="block"))
    summary = {key: "Facts" for key in
               ("summary", "decisions", "changes", "verification", "unresolved_failures", "next_action")}
    summarizer = Backend(events=[text_delta(json.dumps(summary)), done()])
    session = Session(Backend(), auto_approve=True)
    session.conversation_history = (
        [{"role": "user", "content": f"My SSN is {SSN}", "dlp_withheld": True},
         # A withheld tool call's command and path stay out of the preserved evidence too.
         {"role": "tool_call", "name": "bash", "call_id": "c1", "dlp_withheld": True,
          "arguments": json.dumps({"command": f"grep {SSN} people.csv", "path": f"/{SSN}.txt"})},
         {"role": "tool_result", "name": "bash", "call_id": "c1", "content": f"{SSN}: Jane", "dlp_withheld": True},
         # And so does what an earlier summary kept, when that summary was withheld.
         {"role": "assistant", "content": "[Previous conversation summary]\n...", "dlp_withheld": True,
          "preserved_context": {"user_requirements": [f"SSN {SSN}"], "tool_evidence": []}}]
        + [{"role": "user", "content": "More context " * 200} for _ in range(9)])
    compressed, note = compress(session, backend=summarizer, max_tokens=20)
    assert summarizer.requests and SSN not in summarizer.sent() and note
    kept = [entry for entry in compressed if entry.get("preserved_context") is not None]
    assert kept and SSN not in json.dumps(kept) and "[Withheld:" in json.dumps(kept[0]["preserved_context"])
    # What the next request sends has none of it either.
    sent = check_request(request("next", compressed), purpose="primary").request
    assert SSN not in json.dumps(sent)


def test_planning_classification_is_checked():
    install(rules(credit_card="redact"))
    backend = Backend()
    Session(backend).should_plan(f"Refund {CARD}")
    assert CARD not in backend.requests[0]["classify"] and "[REDACTED:credit_card]" in backend.requests[0]["classify"]
    install(rules(credit_card="block"))
    backend.requests.clear()
    assert Session(backend).should_plan(f"Refund {CARD}") is False and backend.requests == []


def test_structured_output_repair_is_checked():
    from lumi.orchestration.runner import LocalSpecialistRunner

    install(rules(credit_card="block"))

    @dlp.guard_backend
    class Structured(Backend):
        def generate_structured(self, user_msg, schema, **kwargs):
            self.requests.append({"user_msg": user_msg})
            return {"ok": True}

    backend = Structured()
    assert LocalSpecialistRunner._repair_structured_output(backend, f"found {CARD}", {"type": "object"}) is None
    assert backend.requests == []
    install(rules(credit_card="redact"))
    assert LocalSpecialistRunner._repair_structured_output(backend, f"found {CARD}", {"type": "object"}) == {"ok": True}
    assert CARD not in backend.sent()


def test_vision_acceptance_questions_are_checked(audit_log):
    from lumi.orchestration.acceptance_check import VisionRunner

    asked = []
    runner = VisionRunner(model="llava", _call=lambda model, prompt, image: asked.append(prompt) or "YES, it is.")
    install(rules(credit_card="block"))
    verdict, raw = runner.ask(b"png", f"Does the page show {CARD}?")
    assert verdict is False and asked == [] and "credit_card" in raw and CARD not in raw
    install(rules(credit_card="redact"))
    assert runner.ask(b"png", f"Does the page show {CARD}?")[0] is True
    assert CARD not in asked[0] and "[REDACTED:credit_card]" in asked[0]


class FakeEngramTools:
    """What the Engram MCP server would receive."""

    def __init__(self):
        self.calls: list[tuple[str, dict]] = []

    def call_tool(self, name, arguments):
        self.calls.append((name, copy.deepcopy(arguments)))
        return {"content": [{"text": "a memory"}]}


def test_what_lumi_sends_engram_passes_the_rules(audit_log):
    from lumi.engine.memory import EngramIntegration

    install(rules(credit_card="redact", us_ssn="block"))
    engram = EngramIntegration()
    engram._enabled = True
    tools = FakeEngramTools()
    engram.set_mcp_manager(tools)
    assert engram.recall(f"refund {CARD}") == ["a memory"]
    assert engram.recall(f"my SSN {SSN}") == []  # a blocked query isn't sent
    engram.remember(f"customer SSN {SSN}")
    engram.remember(f"card {CARD}")
    engram.session_summary([
        {"role": "user", "content": f"My SSN is {SSN}", "dlp_withheld": True},
        {"role": "user", "content": "x" * 190 + f" {CARD}"},  # checked whole, before it's shortened
        {"role": "assistant", "content": "Refunded."},
    ])
    sent = json.dumps(tools.calls)
    assert SSN not in sent and CARD not in sent and "4111" not in sent
    assert [name for name, _ in tools.calls] == [
        "mcp_engram_engram_recall", "mcp_engram_engram_remember", "mcp_engram_engram_session_summary"]
    assert tools.calls[0][1]["query"] == "refund [REDACTED:credit_card]"
    shortened = ("x" * 190 + " [REDACTED:credit_card]")[:200] + "..."
    assert tools.calls[2][1]["items"] == ["user: " + shortened, "assistant: Refunded."]


def test_images_of_withheld_or_blocked_messages_arent_described():
    from lumi.engine import image_descriptions

    install(rules(us_ssn="block"))
    image = {"type": "image", "media_type": "image/png", "data": "iVBORw0KGgo="}
    history = [
        {"role": "user", "content": [dict(image), {"type": "text", "text": "a screenshot"}], "dlp_withheld": True},
        {"role": "user", "content": [dict(image), {"type": "text", "text": f"my SSN {SSN}"}]},  # will be blocked
        {"role": "tool_result", "call_id": "c", "content": "shot", "image": dict(image)},
    ]
    assert image_descriptions.pending(history) == [history[2]["image"]]


class RecordingGuard:
    """The ledger a Team worker's Session reports to (see tests/test_swarm_session_guard.py)."""

    def __init__(self):
        self.records = []
        self.requests = 0

    def begin_request(self, *, purpose, inputs):
        self.requests += 1
        self.records.append({"kind": "request.begin", "purpose": purpose, "inputs": copy.deepcopy(inputs)})
        return f"request-{self.requests}"

    def end_request(self, request_id, *, outcome, usage, error):
        self.records.append({"kind": "request.end", "outcome": outcome})

    def check_tool(self, request_id, name, arguments):
        pass

    def begin_tool(self, request_id, call_id, name, arguments, arguments_sha256):
        return "receipt"

    def end_tool(self, receipt_id, *, outcome, output, is_error, metadata):
        pass


def team_worker(tmp_path, backend, guard):
    """A guarded Session built like the Team runtime's workers (engine/swarming/workers.py, worker_child.py)."""
    from lumi.engine.sandbox import PathSandbox

    session = Session(backend, execution_guard=guard, max_model_requests=3, allowed_tools=[], prompt_role="subagent")
    session.project_path = str(tmp_path)
    session.sandbox = PathSandbox(str(tmp_path), enabled=True)
    return session


def test_team_worker_requests_are_checked(tmp_path, audit_log):
    install(rules(credit_card="redact", us_ssn="block"))
    guard = RecordingGuard()
    backend = Backend(events=[text_delta("Recorded."), done()])
    events = list(team_worker(tmp_path, backend, guard).run(f"Investigate charge {CARD}",
                                                            input_origin="generated"))
    admitted = [r for r in guard.records if r["kind"] == "request.begin"]
    assert len(admitted) == 1 and CARD not in json.dumps(admitted[0]["inputs"])
    assert "[REDACTED:credit_card]" in admitted[0]["inputs"]["user_msg"]
    assert CARD not in backend.sent()
    assert any(e.get("kind") == "dlp_redacted" for e in events)

    blocked_guard = RecordingGuard()
    blocked = Backend()
    events = list(team_worker(tmp_path, blocked, blocked_guard).run(f"Check SSN {SSN}", input_origin="generated"))
    assert blocked.requests == [] and blocked_guard.records == []
    assert any(e.get("code") == "dlp_blocked" for e in events)


def team_run(tmp_path):
    """A Team run with one reader assignment whose objective carries a card number."""
    import uuid

    from lumi.engine.swarming import AttemptContext, Command, Scope, SwarmStore
    from lumi.engine.swarming.policy import PolicyProfile
    from lumi.engine.swarming.supervisor import SwarmSupervisor
    from lumi.engine.swarming.tools import SWARM_TOOL_NAMES

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "refunds.txt").write_text(f"Refund card {CARD}\n", encoding="utf-8")
    supervisor = SwarmSupervisor(SwarmStore(tmp_path / "runtime" / "state.sqlite"))
    tools = frozenset({"file_read", "glob", "grep", "artifact_read"}) | SWARM_TOOL_NAMES
    authority = supervisor.create(Scope.personal("owner", "project", "session"), supervisor_id="supervisor",
                                  objective="Check refunds", request_limit=20,
                                  policy=PolicyProfile(1, tools, frozenset({"ollama"})), lease_seconds=300)

    def command(kind, payload):
        revision = supervisor.store.snapshot(authority.scope, authority.run_id)["run"]["revision"]
        return supervisor.handle(Command(uuid.uuid4().hex, authority.run_id, revision, authority.epoch,
                                         kind, payload), authority)

    command("plan", {"work_items": [{"id": "refunds", "objective": f"Check the refund for card {CARD}",
                                     "read_roots": ["."], "write_roots": [], "tools": sorted(tools),
                                     "criteria": ["fact"]}]})
    assigned = command("assign", {"work_item_id": "refunds", "worker_id": "refunds", "requests": 3,
                                  "model": {"provider": "ollama", "model": "chosen"}}).result
    context = AttemptContext(authority.scope, authority.run_id, assigned["attempt_id"], "refunds", authority.epoch)
    return supervisor, authority, workspace, context


def run_worker(runtime, context):
    from lumi.gui.runtime import BackendSpec

    try:
        runtime.start(context, BackendSpec("ollama", "chosen"))
        deadline = time.monotonic() + 60
        while runtime.inspect(context.attempt_id)["alive"] and time.monotonic() < deadline:
            time.sleep(0.02)
        assert not runtime.inspect(context.attempt_id)["alive"], runtime.poll()
    finally:
        runtime.close()


def test_the_team_runtimes_own_worker_sends_only_checked_requests(tmp_path):
    """engine/swarming/workers.py's in-process worker. The Team preview refuses new work
    while a policy applies (service.policy_refusal); the check holds when that relaxes."""
    from lumi.engine.swarming.workers import SwarmWorkerRunner

    install(rules(credit_card="redact"))
    supervisor, authority, workspace, context = team_run(tmp_path)
    backend = Backend(model="chosen", scripts=[[tool_call("file_read", {"path": "refunds.txt"}), done()],
                                               [text_delta("Checked."), done()]])
    run_worker(SwarmWorkerRunner(supervisor, authority, workspace, backend_factory=lambda spec: backend), context)
    assert len(backend.requests) == 2  # the assignment, then the file's contents
    assert CARD not in json.dumps(backend.requests)
    assert "[REDACTED:credit_card]" in backend.sent(0) and "[REDACTED:credit_card]" in backend.sent(1)


def test_a_team_worker_process_applies_the_policy_it_loads(tmp_path, monkeypatch):
    """engine/swarming/worker_child.py: the worker process reads the organization's
    policy itself (here from LUMI_POLICY_FILE, which it inherits) and checks its requests."""
    import sys

    from lumi.engine.swarming.process_worker import ManagedWorkerProcess
    from lumi.engine.swarming.processes import ProcessObservations
    from lumi.engine.swarming.workers import SwarmWorkerRunner

    policy_file = tmp_path / "policy.json"
    policy_file.write_text(json.dumps({**BASE, "dlp": rules(credit_card="redact")}), encoding="utf-8")
    monkeypatch.setenv("LUMI_POLICY_FILE", str(policy_file))
    monkeypatch.setenv("LUMI_STATE_HOME", str(tmp_path / "worker-state"))
    monkeypatch.setenv("LUMI_KEYCHAIN", "off")
    received = tmp_path / "received.jsonl"
    child = tmp_path / "dlp_worker_child.py"
    child.write_text(
        "import json, sys\n"
        f"sys.path.insert(0, {str(ROOT)!r})\n"
        "from lumi import policy\n"
        "# Only the fixture's policy file, never one installed on this machine.\n"
        "policy._registry_policy = policy._macos_managed_policy = policy._machine_file_policy = lambda: None\n"
        "from lumi.engine.swarming.worker_child import main\n"
        "from tests.streaming_stub import StreamingBackend, done, text_delta, tool_call\n"
        "class Recording(StreamingBackend):\n"
        "    def stream(self, **kwargs):\n"
        f"        with open({str(received)!r}, 'a', encoding='utf-8') as handle:\n"
        "            handle.write(json.dumps({key: kwargs.get(key) for key in ('user_msg', 'conversation_history')},"
        " default=str) + chr(10))\n"
        "        yield from super().stream(**kwargs)\n"
        "raise SystemExit(main(backend_factory=lambda spec: Recording(name=spec.backend_type, model=spec.model,\n"
        "    scripts=[[tool_call('file_read', {'path': 'refunds.txt'}), done()], [text_delta('Checked.'), done()]])))\n",
        encoding="utf-8")
    supervisor, authority, workspace, context = team_run(tmp_path)
    runtime = SwarmWorkerRunner(supervisor, authority, workspace, managed_readers=True,
                                backend_factory=lambda _: pytest.fail("the worker process builds its own backend"),
                                writer_process_factory=lambda: ManagedWorkerProcess(command=[sys.executable, str(child)]),
                                process_observations=ProcessObservations(supervisor.store, host_id="fixture-host"))
    run_worker(runtime, context)
    requests = received.read_text(encoding="utf-8").splitlines()
    assert len(requests) == 2  # the assignment, then the file's contents
    assert CARD not in "\n".join(requests) and all("[REDACTED:credit_card]" in line for line in requests)


# ── The external DLP service ─────────────────────────────────────────────────


class Service:
    """A fake DLP service behind httpx.MockTransport."""

    def __init__(self, answer):
        self.answer = answer
        self.calls: list[dict] = []

    def __call__(self, http_request: httpx.Request) -> httpx.Response:
        payload = json.loads(http_request.content)
        self.calls.append(payload)
        if isinstance(self.answer, Exception):
            raise self.answer
        if isinstance(self.answer, httpx.Response):
            return self.answer
        return httpx.Response(200, json=self.answer(payload) if callable(self.answer) else self.answer)


def with_service(answer, *, on_error="block", **detectors):
    install({"version": 1, "detectors": detectors,
             "service": {"url": "https://dlp.example.com/check", "timeout_seconds": 2, "on_error": on_error}})
    service = Service(answer)
    dlp.set_transport_for_tests(httpx.MockTransport(service))
    return service


def test_the_service_sees_text_after_the_built_in_redactions_and_can_allow(audit_log):
    service = with_service({"action": "allow"}, credit_card="redact")
    sent = check_request(request(f"Refund {CARD} for Jane", [{"role": "user", "content": f"Refund {CARD} for Jane"}]),
                         purpose="primary", provider="ollama", model="m").request
    assert sent["user_msg"] == "Refund [REDACTED:credit_card] for Jane"
    payload = service.calls[0]
    assert payload["version"] == 1 and payload["organization"] == "Acme" and payload["purpose"] == "primary"
    assert CARD not in json.dumps(payload)
    # The message and its copy in the history are one item.
    assert [item["text"] for item in payload["items"]].count("Refund [REDACTED:credit_card] for Jane") == 1
    assert {"kind": "prompt", "text": "Refund [REDACTED:credit_card] for Jane"}.items() <= payload["items"][-1].items()


def test_the_service_can_redact(audit_log):
    with_service({"action": "redact", "rule": "person-name", "redactions": ["Jane Doe"]})
    checked = check_request(request("Email Jane Doe today"), purpose="primary")
    assert checked.request["user_msg"] == "Email [REDACTED:person-name] today"
    assert "person-name" in checked.notice
    assert [(f["rule"], f["action"], f["source"]) for f in findings(audit_log)] == [("person-name", "redact", "service")]


def test_the_service_can_block_and_its_verdicts_are_reused(audit_log):
    # The service names the item it objects to, so later requests can leave it out.
    service = with_service(lambda payload: {"action": "block", "rule": "export-control", "items": [
        item["id"] for item in payload["items"] if "ITAR" in item["text"]]})
    history = [{"role": "user", "content": "the ITAR schematic"}]
    with pytest.raises(Blocked) as blocked:
        check_request(request("", history), purpose="primary")
    assert "export-control" in blocked.value.message and "ITAR" not in blocked.value.message
    assert blocked.value.entries == (0,)
    service.answer = {"action": "allow"}
    check_request(request("", [{"role": "user", "content": "fine"}]), purpose="primary")
    check_request(request("", [{"role": "user", "content": "fine"}]), purpose="primary")
    assert len(service.calls) == 2  # the second "fine" was already judged


@pytest.mark.parametrize("failure", [
    httpx.ReadTimeout("timed out"),
    httpx.ConnectError("refused"),
    httpx.Response(500, text="oops"),
    httpx.Response(200, text="not json"),
    {"action": "maybe"},
    {"action": "redact", "redactions": []},
])
def test_a_service_that_cant_answer_blocks_by_default(failure, audit_log):
    with_service(failure)
    with pytest.raises(Blocked, match="service couldn't check this request"):
        check_request(request("hello"), purpose="primary")
    assert [r["data"]["on_error"] for r in read_log(audit_log) if r["type"] == "dlp.error"] == ["block"]


def test_a_service_that_cant_answer_is_skipped_when_allowed(audit_log):
    with_service(httpx.ReadTimeout("timed out"), on_error="allow", credit_card="redact")
    sent = check_request(request(f"hello {CARD}"), purpose="primary").request
    assert sent["user_msg"] == "hello [REDACTED:credit_card]"  # the built-in rules still apply
    errors = [r["data"] for r in read_log(audit_log) if r["type"] == "dlp.error"]
    assert errors and errors[0]["on_error"] == "allow" and errors[0]["error"] == "timed out"


def test_the_service_uses_the_apps_network_settings(monkeypatch):
    from lumi import net

    seen = []
    original = net.client_options
    monkeypatch.setattr(net, "client_options", lambda **kw: seen.append(kw) or original(**kw))
    with_service({"action": "allow"})
    check_request(request("hello"), purpose="primary")
    assert seen and seen[0]["timeout"] == 2.0 and seen[0]["transport"] is not None


def export_control(payload):
    """A service that blocks, naming them, the items that mention ITAR."""
    named = [item["id"] for item in payload["items"] if "ITAR" in item["text"]]
    return {"action": "block", "rule": "export-control", "items": named} if named else {"action": "allow"}


def texts_sent(service, since=0) -> list[str]:
    return [item["text"] for call in service.calls[since:] for item in call["items"]]


def test_the_conversation_goes_on_after_a_service_block(audit_log):
    service = with_service(export_control)
    backend = Backend()
    session = Session(backend, auto_approve=True, max_steps=2)
    events = list(session.run("Summarize the ITAR schematic"))
    assert backend.requests == [] and any(e.get("code") == "dlp_blocked" for e in events)
    assert session.conversation_history[0]["dlp_withheld"] is True
    # Later requests leave it out, and the service isn't asked about it again.
    before = len(service.calls)
    list(session.run("Never mind. What's 2 + 2?"))
    assert len(backend.requests) == 1 and "ITAR" not in backend.sent() and "[Withheld:" in backend.sent()
    assert not any("ITAR" in text for text in texts_sent(service, before))
    # Sending the same text again is refused from memory, without asking.
    before = len(service.calls)
    events = list(session.run("Summarize the ITAR schematic"))
    assert len(service.calls) == before and len(backend.requests) == 1
    assert any(e.get("code") == "dlp_blocked" and "export-control" in e["message"] for e in events)


def test_a_withheld_entry_stays_out_when_the_service_cant_answer(audit_log):
    # on_error "allow" sends what the built-in rules allow; never what was blocked before.
    with_service(httpx.ConnectError("refused"), on_error="allow")
    history = [{"role": "user", "content": "the ITAR schematic", "dlp_withheld": True},
               {"role": "user", "content": "next"}]
    sent = check_request(request("next", history), purpose="primary").request
    assert sent["conversation_history"][0]["content"].startswith("[Withheld:")
    assert "ITAR" not in json.dumps(sent)


def test_a_service_block_naming_no_items_leaves_out_what_it_hadnt_allowed(audit_log):
    service = with_service(lambda payload: {"action": "block"} if any("ITAR" in text for text in [
        item["text"] for item in payload["items"]]) else {"action": "allow"})
    backend = Backend()
    session = Session(backend, auto_approve=True, max_steps=2)
    list(session.run("hello"))
    events = list(session.run("the ITAR schematic"))
    assert any(e.get("code") == "dlp_blocked" and "dlp-service" in e["message"] for e in events)
    withheld = [entry.get("content") for entry in session.conversation_history if entry.get("dlp_withheld")]
    assert "the ITAR schematic" in withheld and "hello" not in withheld  # "hello" was allowed before
    before = len(service.calls)
    list(session.run("What's 2 + 2?"))
    assert len(backend.requests) == 2 and "ITAR" not in backend.sent()
    assert not any("ITAR" in text for text in texts_sent(service, before))


def test_service_redactions_leave_signed_reasoning_out(audit_log):
    with_service({"action": "redact", "rule": "person", "redactions": ["Jane Doe"]})
    history = [{"role": "user", "content": "go"},
               {"role": "tool_call", "name": "bash", "call_id": "c1", "arguments": "{}",
                "content": "Called bash for Jane Doe", "reasoning_content": "Jane Doe asked for this",
                "reasoning_details": [{"type": "thinking", "thinking": "Jane Doe asked", "signature": "sig"}]},
               {"role": "tool_result", "call_id": "c1", "content": "ok"}]
    sent = check_request(request("", history), purpose="primary").request["conversation_history"][1]
    # A signed thinking block can't be edited: it's left out, never sent changed under its signature.
    assert "reasoning_details" not in sent and "reasoning_content" not in sent
    assert sent["content"] == "Called bash for [REDACTED:person]"
    assert history[1]["reasoning_details"][0]["thinking"] == "Jane Doe asked"


# ── Speed ────────────────────────────────────────────────────────────────────


MB = 1_000_000
ADVERSARIAL = {
    "digits": "1" * MB,
    "card groups": "4111 " * 200_000,
    "ssn-like": "123-45-678 " * 90_910,
    "iban-like": "DE89 3704 0044 0532 0130 " * 40_000,
    "letters": "a" * MB,
    "dotted": "a." * 500_000,
    "jwt-like": "-eyJ" * 250_000,
    "jwt first parts": "eyJ" + "a-" * 499_999,
    "url schemes": "1." * 499_999 + "a://",
    "env names": "TOKEN" * 200_000,
    "key headers": "-----BEGIN PRIVATE KEY-----\n" * 35_715,
    "emails": "a@b." * 250_000,
    "secret anchors": ("-----BEGIN PRIVATE KEY----- AKIA ghp_ glpat- xox hooks.slack.com _live_ sk-ant- "
                       "AIza hf_ npm_ eyJ :// = aws_secret_access_key AccountKey= ") * 7_600,
    # The pattern rules' fixed text is there, so their regexes run over all of it.
    "customer ids": ("CUST-" + "1" * 7 + " ") * 76_923,
    "host labels": "a" * (MB - 17) + ".corp.example.com",
    "spaced keywords": "Project" + " " * (MB - 7),
    # Text that has to be normalized first.
    "no-break card groups": f"4111{NBSP}" * 200_000,
    "zero-width digits": f"4{ZWSP}" * 500_000,
    "full-width digits": full_width("4111 ") * 200_000,
    "ligatures": FI * MB,
    "dashed ssn-like": f"123{EN_DASH}45{EN_DASH}678 " * 90_910,
    "ideographs": chr(0x4E2D) * MB,
}


@pytest.mark.parametrize("label", sorted(ADVERSARIAL))
def test_a_megabyte_of_adversarial_text_scans_in_well_under_a_second(label):
    policy = parse_section({
        "version": 1,
        "detectors": {name: "flag" for name in dlp.DETECTORS},
        "rules": [{"name": "codenames", "keywords": ["Project Falcon", "FALCON-X"] + [f"code-{i}" for i in range(200)],
                   "action": "flag"},
                  {"name": "customer-id", "pattern": r"CUST-\d{8}", "action": "flag"},
                  {"name": "host", "pattern": r"[a-z0-9-]{1,40}\.corp\.example\.com", "action": "flag"}],
    })
    text = ADVERSARIAL[label]
    assert len(text) >= 990_000
    started = time.perf_counter()
    dlp.scan_text(text, policy=policy)
    assert time.perf_counter() - started < 1.0


@pytest.mark.parametrize("text", ["a" * MB, "a0" * (MB // 2), "aaaaaaaaaaa0" * (MB // 12)], ids=["a", "a0", "11a0"])
def test_a_pattern_at_the_checks_limit_scans_a_megabyte_in_well_under_a_second(text):
    # About the costliest pattern the check allows: 10 ways of 12 steps.
    policy = parse_section(rules({"name": "costliest", "pattern": r"[a-z]{1,10}[a-z]0", "action": "flag"}))
    started = time.perf_counter()
    dlp.scan_text(text, policy=policy)
    assert time.perf_counter() - started < 1.0


# ── Every model request goes through DLP ─────────────────────────────────────

ARGUMENTS = {"user_msg": "hello", "conversation_history": [], "instructions": "", "tools": []}


def test_a_backend_call_that_skips_the_check_is_refused(audit_log):
    backend = Backend()
    list(backend.stream(**ARGUMENTS))  # no policy, nothing to enforce
    install(rules(credit_card="flag"))
    with pytest.raises(Blocked) as refused:
        backend.stream(**ARGUMENTS)
    assert refused.value.code == "dlp_unchecked" and len(backend.requests) == 1
    errors = [r["data"] for r in read_log(audit_log) if r["type"] == "dlp.error"]
    assert errors == [{"reason": "unchecked", "error": "Backend.stream"}]
    with pytest.raises(Blocked):
        backend.classify("hello")
    assert list(dlp.send(backend.stream, **ARGUMENTS))  # a checked request goes through
    with dlp.permit():  # fixed text, such as a warm-up
        assert backend.classify("fixed text") == "SIMPLE"
    # A policy that refuses every request (here, one with an unusable dlp section) refuses these too.
    install({"version": 2})
    with pytest.raises(Blocked):
        backend.stream(**ARGUMENTS)


def test_a_stream_holds_the_permit_only_while_it_makes_events():
    @dlp.guard_backend
    class Adapter(Backend):
        def stream(self, **kwargs):
            yield from super().stream(**kwargs)  # its parent's guarded stream, reached as this one runs

    install(rules(credit_card="flag"))
    backend, other = Adapter(), Backend()
    events = []
    for event in dlp.send(backend.stream, **ARGUMENTS):
        events.append(event)
        with pytest.raises(Blocked):  # the caller, between events, has no permit
            other.classify("hello")
    assert events and len(backend.requests) == 1 and other.requests == []


def model_backend_classes() -> dict[tuple[str, str], set[str]]:
    """(module, class) -> the request methods it defines, for every class in lumi/."""
    found = {}
    for path in sorted((ROOT / "lumi").rglob("*.py")):
        module = ".".join(path.relative_to(ROOT).with_suffix("").parts)
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.ClassDef):
                methods = {item.name for item in node.body if isinstance(item, ast.FunctionDef)} & set(dlp.REQUEST_METHODS)
                if methods:
                    found[(module, node.name)] = methods
    return found


def test_every_model_backend_guards_its_request_methods():
    import importlib

    found = model_backend_classes()
    # Not a backend: it takes one and calls the backend's own guarded methods,
    # with the permit its callers hold (Session._model_stream and should_plan).
    assert found.pop(("lumi.engine.execution_guard", "ExecutionBoundary"))
    assert ("lumi.backends", "OllamaBackend") in found and len(found) >= 10
    for (module, name), methods in sorted(found.items()):
        cls = getattr(importlib.import_module(module), name)
        for method in methods:
            assert getattr(cls.__dict__[method], "dlp_guarded", False), f"{module}.{name}.{method} isn't guarded"


# The second line: every use of a backend's request methods in lumi/ (called,
# passed on, or looked up with getattr), counted per function, and why it's
# covered. A new use, even in a function listed here, changes a count and
# must be reviewed.
COVERED_USES = {
    ("lumi/engine/session.py", "should_plan", "classify"): (3, "prompt passed dlp.check_text; sent under dlp.permit"),
    ("lumi/engine/session.py", "invoke", "stream"): (1, "dlp.send of the request _model_stream checked"),
    ("lumi/engine/session.py", "_model_stream", "stream"): (1, "the execution boundary around that checked request"),
    ("lumi/engine/request_purpose.py", "send_checked", "stream"): (1, "dlp.send, after dlp.check_request"),
    ("lumi/engine/request_purpose.py", "send_checked", "stream_auxiliary"): (1, "dlp.send, after dlp.check_request"),
    ("lumi/engine/execution_guard.py", "classify", "classify"): (1, "reached from should_plan's permit"),
    ("lumi/orchestration/runner.py", "_repair_structured_output", "generate_structured"): (
        1, "prompt passed dlp.check_text; sent with dlp.send"),
    ("lumi/backends.py", "warm_up", "stream"): (1, "EXO warm-up: fixed text, under dlp.permit"),
    ("lumi/backends.py", "classify", "stream"): (2, "CLI adapters' classify (guarded) runs their own stream"),
    ("lumi/backends.py", "stream", "stream"): (1, "EXO's stream calling its parent's, as it runs under the permit"),
    ("lumi/engine/provider_extensions.py", "classify", "stream"): (1, "classify (guarded) runs its own stream"),
    ("lumi/sonn.py", "stream_auxiliary", "stream"): (1, "stream_auxiliary (guarded) runs a copy's stream"),
    ("lumi/sonn.py", "stream", "stream"): (1, "SONN's stream calling its parent's, as it runs"),
    ("lumi/smoke/flaky.py", "stream", "stream"): (1, "test wrapper (guarded) around a Session's backend"),
    ("lumi/sonn_tasks.py", "_request", "stream"): (1, "httpx transport; the advice question passes dlp.check_text"),
}
# Functions whose code names a model API endpoint: a direct HTTP call to a
# model has no backend method to guard, so each is listed with its reason.
COVERED_ENDPOINTS = {
    ("lumi/backends.py", "_open_chat_stream_with_retry"): "OllamaBackend.stream's request (guarded)",
    ("lumi/backends.py", "stream"): "the backends' own guarded stream methods",
    ("lumi/backends.py", "classify"): "OllamaBackend.classify (guarded)",
    ("lumi/backends.py", "generate_structured"): "OllamaBackend.generate_structured (guarded)",
    ("lumi/backends.py", "warm_up"): "Ollama warm-up: the fixed text \"hi\"",
    ("lumi/backends.py", "_detect_tool_support"): "Ollama tool-support probe: fixed text",
    ("lumi/anthropic_api.py", "_endpoint"): "AnthropicBackend.stream's address (guarded)",
    ("lumi/openai_api.py", "stream"): "OpenAIResponsesBackend.stream (guarded)",
    ("lumi/orchestration/acceptance_check.py", "_call_ollama"): "VisionRunner.ask checks the question first",
    ("lumi/sonn_tasks.py", "ask_advice"): "the question passes dlp.check_text first",
}
_ENDPOINT_MARKERS = ("/api/chat", "/api/generate", "/chat/completions", "/v1/messages")


class _ModelUses(ast.NodeVisitor):
    def __init__(self):
        self.functions: list[str] = []
        self.uses: dict[tuple[str, str], int] = {}
        self.endpoints: set[str] = set()

    def _where(self) -> str:
        return self.functions[-1] if self.functions else "<module>"

    def visit_FunctionDef(self, node):
        self.functions.append(node.name)
        self.generic_visit(node)
        self.functions.pop()

    visit_AsyncFunctionDef = visit_FunctionDef

    def _use(self, method: str) -> None:
        key = (self._where(), method)
        self.uses[key] = self.uses.get(key, 0) + 1

    def visit_Call(self, node):
        func = node.func
        if (isinstance(func, ast.Attribute) and func.attr == "stream" and node.args
                and isinstance(node.args[0], ast.Constant) and node.args[0].value in {"GET", "POST"}):
            # httpx's client.stream("POST", url): transport, not a backend method.
            for child in [func.value, *node.args, *node.keywords]:
                self.visit(child)
            return
        if (isinstance(func, ast.Name) and func.id == "getattr" and len(node.args) >= 2
                and isinstance(node.args[1], ast.Constant) and node.args[1].value in dlp.REQUEST_METHODS):
            self._use(node.args[1].value)
        self.generic_visit(node)

    def visit_Attribute(self, node):
        if node.attr in dlp.REQUEST_METHODS and isinstance(node.ctx, ast.Load):
            self._use(node.attr)
        self.generic_visit(node)

    def visit_Expr(self, node):
        if not (isinstance(node.value, ast.Constant) and isinstance(node.value.value, str)):  # not a docstring
            self.generic_visit(node)

    def visit_Constant(self, node):
        if isinstance(node.value, str) and (node.value == "responses" or any(
                marker in node.value for marker in _ENDPOINT_MARKERS)):
            self.endpoints.add(self._where())


def test_every_use_of_a_model_request_is_listed_with_its_check():
    uses: dict = {}
    endpoints: set = set()
    for path in sorted((ROOT / "lumi").rglob("*.py")):
        visitor = _ModelUses()
        visitor.visit(ast.parse(path.read_text(encoding="utf-8")))
        name = path.relative_to(ROOT).as_posix()
        uses.update({(name, function, method): count for (function, method), count in visitor.uses.items()})
        endpoints |= {(name, function) for function in visitor.endpoints}
    assert uses == {key: count for key, (count, _why) in COVERED_USES.items()}
    assert endpoints == set(COVERED_ENDPOINTS)
