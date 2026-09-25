"""Secrets are removed before a model request, and from the diagnostics bundle."""

from __future__ import annotations

import copy
import json
import zipfile
from unittest.mock import patch

import pytest

from lumi import secret_scan
from lumi.backends import EVENT_DONE, EVENT_TEXT_DELTA, EVENT_TOOL_CALL

SAVED_KEY = "sk-saved-provider-key-0123456789"
GITHUB = "ghp_" + "A1b2C3d4E5" * 4
PEM = "-----BEGIN RSA PRIVATE KEY-----\nMIIEow\nIBAAKC\n-----END RSA PRIVATE KEY-----"


class _Settings:
    """Enough of SettingsManager for secret_scan: get() and get_all()."""

    def __init__(self, *, scan=False, keys=None, extra=None):
        self.data = {"privacy": {"secret_scan": scan}, "api_keys": dict(keys or {}), **(extra or {})}

    def get(self, section, key=None, default=None):
        value = self.data.get(section, default)
        if key is None:
            return value
        return value.get(key, default) if isinstance(value, dict) else default

    def get_all(self):
        return copy.deepcopy(self.data)


@pytest.fixture
def scan_state(monkeypatch):
    """Isolated scan configuration; environment keys never leak into it."""
    monkeypatch.setattr(secret_scan, "_state", {"patterns": False, "known": (), "generation": "start"})
    monkeypatch.setattr(secret_scan, "_ENV_SECRETS", ())

    def configure(**kwargs):
        return secret_scan.configure(_Settings(**kwargs))

    return configure


class TestRedactText:
    @pytest.mark.parametrize("text, kind", [
        ("id AKIAIOSFODNN7EXAMPLE here", "AWS access key"),
        ("aws_secret_access_key = wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY", "AWS secret key"),
        (f"token {GITHUB}", "GitHub token"),
        (PEM, "private key"),
        ("key sk-ant-api03-" + "x" * 30, "Anthropic key"),
        ("key sk-proj-" + "y" * 30, "OpenAI key"),
        ("xoxb-1234567890-abcdefghij", "Slack token"),
        ("AIza" + "B" * 35, "Google API key"),
        ("postgres://app:hunter2hunter2@db:5432/app", "password in a URL"),
        ("DB_PASSWORD=correct-horse-battery", "secret in .env"),
        ("eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N", "JSON web token"),
    ])
    def test_known_formats(self, text, kind):
        redacted, found = secret_scan.redact_text(text, patterns=True, known=())
        assert found == {kind: 1}
        assert f"[REDACTED {kind}]" in redacted

    def test_names_stay_readable(self):
        redacted, _ = secret_scan.redact_text(
            "export API_TOKEN=abcdefgh12345678\npostgres://app:pw12345@db/x", patterns=True, known=())
        assert redacted == "export API_TOKEN=[REDACTED secret in .env]\npostgres://app:[REDACTED password in a URL]@db/x"

    @pytest.mark.parametrize("text", [
        "import sklearn  # sk-learn",
        "password: str = field(default='')",
        "API_KEY=${API_KEY}",
        "max_tokens = 4096",
        "https://example.com/docs?page=2",
    ])
    def test_ordinary_code_is_untouched(self, text):
        assert secret_scan.redact_text(text, patterns=True, known=()) == (text, {})

    def test_known_values_need_no_pattern(self):
        text = f"config says {SAVED_KEY}; dummy key ollama"
        redacted, found = secret_scan.redact_text(text, patterns=False, known=[SAVED_KEY, "ollama"])
        assert redacted == "config says [REDACTED saved API key]; dummy key ollama"
        assert found == {"saved API key": 1}


class TestScrubHistory:
    def test_saved_keys_leave_tool_output_even_with_the_scan_off(self, scan_state):
        scan_state(keys={"openai": SAVED_KEY})
        history = [
            {"role": "user", "content": f"my key is {SAVED_KEY}"},
            {"role": "tool_result", "content": f"OPENAI={SAVED_KEY} and {GITHUB}"},
        ]
        found = secret_scan.scrub_history(history)
        assert found == {"saved API key": 1}
        assert history[0]["content"] == f"my key is {SAVED_KEY}"  # the user's own message
        assert history[1]["content"] == f"OPENAI=[REDACTED saved API key] and {GITHUB}"

    def test_scan_covers_messages_and_formats(self, scan_state):
        scan_state(scan=True, keys={"openai": SAVED_KEY})
        history = [
            {"role": "user", "content": [{"type": "image", "data": "..."}, {"type": "text", "text": f"see {GITHUB}"}]},
            {"role": "tool_result", "content": PEM},
            {"role": "assistant", "content": f"echo {GITHUB}"},
        ]
        found = secret_scan.scrub_history(history)
        assert found == {"GitHub token": 1, "private key": 1}
        assert history[0]["content"][1]["text"] == "see [REDACTED GitHub token]"
        assert history[1]["content"] == "[REDACTED private key]"
        assert history[2]["content"] == f"echo {GITHUB}"  # model output is not rewritten

    def test_entries_are_checked_once_per_configuration(self, scan_state):
        scan_state(keys={"openai": SAVED_KEY})
        history = [{"role": "tool_result", "content": GITHUB}]
        assert secret_scan.scrub_history(history) == {}
        assert history[0]["content"] == GITHUB
        # Turning the scan on is a new configuration: the entry is checked again.
        scan_state(scan=True, keys={"openai": SAVED_KEY})
        assert secret_scan.scrub_history(history) == {"GitHub token": 1}
        assert secret_scan.scrub_history(history) == {}

    def test_sensitive_settings_count_as_known_values(self, scan_state):
        token = "mcp-server-token-abcdefghijk"
        scan_state(extra={"mcp_servers": {"docs": {"env": {"DOCS_TOKEN": token}, "command": "docs-server-command"}}})
        history = [{"role": "tool_result", "content": f"{token} via docs-server-command"}]
        secret_scan.scrub_history(history)
        assert history[0]["content"] == "[REDACTED saved API key] via docs-server-command"

    def test_nothing_configured_changes_nothing(self, scan_state):
        scan_state()
        history = [{"role": "tool_result", "content": GITHUB}]
        assert secret_scan.scrub_history(history) == {}
        assert history == [{"role": "tool_result", "content": GITHUB}]


class _RecordingBackend:
    """Calls one tool, then answers; records exactly what each request carried."""

    name = "stub"
    model = "stub-model"
    tool_mode = "native"

    def __init__(self):
        self.requests: list[dict] = []

    def stream(self, **kwargs):
        self.requests.append({
            "user_msg": kwargs.get("user_msg"),
            "history": copy.deepcopy(kwargs.get("conversation_history")),
        })
        if len(self.requests) == 1:
            yield EVENT_TOOL_CALL, {"name": "file_read", "arguments": '{"path":".env"}', "call_id": "call-1"}
        else:
            yield EVENT_TEXT_DELTA, {"delta": "Done."}
        yield EVENT_DONE, {"cognitive_state": None, "stats": None, "model": self.model}


def _run_turn(message):
    from lumi.engine.session import Session
    from lumi.engine.tools import ToolResult

    backend = _RecordingBackend()
    session = Session(backend, max_steps=4, auto_approve=True)
    with patch("lumi.engine.session.execute_tool") as execute:
        execute.return_value = ToolResult(f"GITHUB_TOKEN={GITHUB}\nOPENAI_API_KEY={SAVED_KEY}\n", elapsed=0.0)
        events = list(session.run(message))
    return backend, events


def _sent_text(request):
    return json.dumps(request["history"]) + str(request["user_msg"])


class TestModelRequests:
    def test_scan_on_nothing_secret_reaches_the_model(self, scan_state):
        scan_state(scan=True, keys={"openai": SAVED_KEY})
        backend, events = _run_turn(f"Deploy with {GITHUB}")

        assert len(backend.requests) == 2
        for request in backend.requests:
            assert GITHUB not in _sent_text(request)
            assert SAVED_KEY not in _sent_text(request)
        assert backend.requests[0]["user_msg"] == "Deploy with [REDACTED GitHub token]"
        # The adapter sees the message it is sending already in history, so the
        # scrubbed copy is not appended a second time as the raw text.
        assert backend.requests[0]["history"][-1]["content"] == backend.requests[0]["user_msg"]

        notices = [e for e in events if e.get("kind") == "secrets_redacted"]
        assert [n["kinds"] for n in notices] == [{"GitHub token": 1}, {"GitHub token": 1, "saved API key": 1}]
        assert notices[0]["message"] == "Removed 1 secret (GitHub token) before sending to the model."

    def test_scan_off_only_saved_keys_are_removed(self, scan_state):
        scan_state(keys={"openai": SAVED_KEY})
        backend, events = _run_turn(f"Deploy with {GITHUB}")

        second = _sent_text(backend.requests[1])
        assert SAVED_KEY not in second
        assert GITHUB in second  # the user's scan is off: formats are left alone
        assert backend.requests[0]["user_msg"] == f"Deploy with {GITHUB}"
        assert [e["kinds"] for e in events if e.get("kind") == "secrets_redacted"] == [{"saved API key": 1}]


class TestDiagnostics:
    def test_known_values_are_removed_whatever_their_format(self, tmp_path):
        from lumi.gui.diagnostics import build_diagnostics_zip

        state = tmp_path / "state"
        (state / "logs" / "2026-09-24").mkdir(parents=True)
        (state / "logs" / "2026-09-24" / "s1.jsonl").write_text(
            '{"event": "error", "message": "401 for key plainvalue-without-prefix"}\n', encoding="utf-8")
        (state / "settings.json").write_text(json.dumps({
            "api_keys": {"openai": "__keychain__", "kimi": "moonshot-plain-key", "sonn": ""},
            "mcp_servers": {"docs": {"command": "docs", "env": {"DOCS_TOKEN": "tok-12345678"}}},
            "network": {"ollama_url": "http://10.0.0.5:11434"},
        }), encoding="utf-8")

        zip_path = build_diagnostics_zip(
            state, tmp_path / "out", version="test", known_secrets={"plainvalue-without-prefix", "short"})
        with zipfile.ZipFile(zip_path) as bundle:
            everything = "\n".join(bundle.read(name).decode("utf-8") for name in bundle.namelist())
            meta = bundle.read("meta.txt").decode("utf-8")

        assert "plainvalue-without-prefix" not in everything
        assert "moonshot-plain-key" not in everything and "tok-12345678" not in everything
        assert '"openai": "[in the OS credential store]"' in meta
        assert '"sonn": ""' in meta  # unset stays visibly unset
        assert "http://10.0.0.5:11434" in meta  # ordinary settings stay useful
