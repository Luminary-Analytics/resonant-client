"""`lumi run` (lumi/headless.py): one task without a UI, a JSON result and an
exit code a CI job can act on."""

from __future__ import annotations

import io
import json
import threading
from types import SimpleNamespace

import pytest

from lumi import headless, usage
from lumi import policy as lumi_policy
from lumi.headless import UsageError, build_spec
from lumi.policy import parse
from lumi.usage import UsageLedger
from tests.streaming_stub import StreamingBackend, done, error, text_delta, tool_call

MODEL = "claude-haiku-4-5"


class _Settings:
    def __init__(self, data=None):
        self.data = data or {}

    def get(self, section, key=None, default=None):
        values = self.data.get(section, {})
        return values if key is None else values.get(key, default)

    def get_all(self):
        return self.data


@pytest.fixture
def ledger(tmp_path):
    instance = UsageLedger(tmp_path / "usage")
    usage.set_for_tests(instance)
    return instance


def run(monkeypatch, tmp_path, backend, *args, stdin=""):
    """``lumi run`` with the backend replaced by a scripted one."""
    monkeypatch.setattr(headless, "build_spec", lambda settings, provider, model, project: SimpleNamespace(
        create_backend=lambda settings: backend, permission_mode=""))
    out, err = io.StringIO(), io.StringIO()
    code = headless.main(["--provider", "anthropic", "--model", MODEL, "--project", str(tmp_path), *args],
                         stdin=io.StringIO(stdin), stdout=out, stderr=err)
    return code, out.getvalue(), err.getvalue()


def scripted(*scripts):
    return StreamingBackend(name="anthropic", model=MODEL, scripts=list(scripts))


def call(dollars=0.1, *events):
    return [*events, done(model=MODEL, stats={"input_tokens": int(dollars * 1_000_000)})]


class TestSpec:
    def test_keys_come_from_settings_or_the_environment(self, monkeypatch, tmp_path):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        with pytest.raises(UsageError, match="ANTHROPIC_API_KEY"):
            build_spec(_Settings(), "anthropic", MODEL, str(tmp_path))
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-env-key-for-tests-000")
        spec = build_spec(_Settings(), "anthropic", MODEL, str(tmp_path))
        assert (spec.api_key_source, spec.api_key_env) == ("env", "ANTHROPIC_API_KEY")
        spec = build_spec(_Settings({"api_keys": {"anthropic": "sk-saved"}}), "anthropic", MODEL, str(tmp_path))
        assert (spec.api_key_source, spec.api_key_setting) == ("settings", "anthropic")
        assert "sk-" not in json.dumps(spec.to_dict())

    def test_what_is_refused(self, tmp_path):
        with pytest.raises(UsageError, match="--model"):
            build_spec(_Settings(), "anthropic", "", str(tmp_path))
        with pytest.raises(UsageError, match="Unknown provider"):
            build_spec(_Settings(), "acme-ai", "x", str(tmp_path))
        with pytest.raises(UsageError, match="turned off"):
            build_spec(_Settings({"security": {"cli_adapters": False}}), "codex", "gpt-5.5", str(tmp_path))
        with pytest.raises(UsageError, match="no connection"):
            build_spec(_Settings(), "conn-missing", "x", str(tmp_path))
        spec = build_spec(_Settings({"network": {"ollama_url": "http://127.0.0.1:11434"}}), "ollama", "qwen3", str(tmp_path))
        assert spec.url.startswith("http://")


class TestRuns:
    def test_a_completed_task_reports_its_result(self, monkeypatch, tmp_path, ledger):
        backend = scripted(call(0.1, tool_call("file_write", {"path": "notes.txt", "content": "hi"})),
                           call(0.05, text_delta("Wrote notes.txt.")))
        code, out, err = run(monkeypatch, tmp_path, backend, "Write a note", "--mode", "bypass")
        result = json.loads(out)
        assert code == 0 and result["status"] == "completed", (out, err)
        assert result["text"] == "Wrote notes.txt." and result["mode"] == "bypass"
        assert any(path.endswith("notes.txt") for path in result["changed_files"])
        assert (tmp_path / "notes.txt").read_text(encoding="utf-8") == "hi"
        assert result["usage"]["calls"] == 2 and result["usage"]["cost_usd"] == pytest.approx(0.15)
        assert all(row["session"] == f"headless:{result['run_id']}" for row in ledger.records())

    def test_edits_need_a_mode_that_allows_them(self, monkeypatch, tmp_path, ledger):
        backend = scripted(call(0.01, tool_call("file_write", {"path": "notes.txt", "content": "hi"})),
                           call(0.01, text_delta("I couldn't write the file.")))
        code, out, _ = run(monkeypatch, tmp_path, backend, "Write a note", "--mode", "ask")
        result = json.loads(out)
        assert not (tmp_path / "notes.txt").exists()
        assert (code, result["status"], result["denied_calls"]) == (3, "needs_attention", 1)

    def test_a_provider_error_fails_the_run(self, monkeypatch, tmp_path, ledger):
        code, out, _ = run(monkeypatch, tmp_path, scripted([error("upstream exploded")]), "Go")
        result = json.loads(out)
        assert code == 1 and result["status"] == "failed"
        assert result["errors"][0]["message"].startswith("upstream exploded")

    def test_a_budget_stops_the_run_for_a_person(self, monkeypatch, tmp_path, ledger):
        from lumi.gui.settings import SettingsManager

        # `lumi run` reads its limits from Settings.
        SettingsManager().set("cost_tracking", "turn_limit_usd", 0.15)
        backend = scripted(call(0.1, tool_call("file_read", {"path": "x"})),
                           call(0.1, tool_call("file_read", {"path": "x"}, call_id="c2")),
                           call(0.1, text_delta("never")))
        code, out, _ = run(monkeypatch, tmp_path, backend, "Go")
        result = json.loads(out)
        assert code == 3 and result["status"] == "budget_exceeded" and backend.stream_count == 2

    def test_a_timeout_stops_the_run(self, monkeypatch, tmp_path, ledger):
        class Slow(StreamingBackend):
            def stream(self, *, cancel_event=None, **kwargs):
                self.stream_calls.append(kwargs)
                (cancel_event or threading.Event()).wait(5)
                yield "done", {"model": MODEL, "stats": {}}

        code, out, _ = run(monkeypatch, tmp_path, Slow(name="anthropic", model=MODEL), "Go", "--timeout", "0.2")
        result = json.loads(out)
        assert code == 3 and result["status"] == "timeout" and result["elapsed"] < 5

    def test_output_formats_and_stdin(self, monkeypatch, tmp_path, ledger):
        code, out, err = run(monkeypatch, tmp_path, scripted(call(0.01, text_delta("Looks fine."))),
                             "-", "--output", "jsonl", stdin="Review this diff")
        lines = [json.loads(line) for line in out.splitlines()]
        assert code == 0 and lines[-1]["event"] == "lumi.result" and lines[-1]["status"] == "completed"
        assert any(line.get("event") == "text.delta" for line in lines[:-1])
        code, out, err = run(monkeypatch, tmp_path, scripted(call(0.01, text_delta("Hi there."))), "Hi",
                             "--output", "text")
        assert out.strip() == "Hi there." and "completed" in err


class TestRefusals:
    def test_usage_errors_exit_2(self, monkeypatch, tmp_path, ledger):
        out, err = io.StringIO(), io.StringIO()
        assert headless.main(["--provider", "anthropic", "--model", MODEL], stdin=io.StringIO(""),
                             stdout=out, stderr=err) == 2
        assert "Give the task" in err.getvalue()
        lumi_policy.set_for_tests(parse({"schema": "lumi.policy/v1", "organization": "Acme",
                                         "permissions": {"allowed_modes": ["ask", "auto-edit"]},
                                         "models": {"blocked": ["anthropic:claude-opus-*"]}}, source="t"))
        code, _, err = run(monkeypatch, tmp_path, scripted(), "Go", "--mode", "bypass")
        assert code == 2 and "Acme's policy doesn't allow --mode bypass" in err
        err = io.StringIO()
        code = headless.main(["Go", "--provider", "anthropic", "--model", "claude-opus-5-5", "--project", str(tmp_path)],
                             stdin=io.StringIO(""), stdout=io.StringIO(), stderr=err)
        assert code == 2 and "doesn't allow claude-opus-5-5" in err.getvalue()

    def test_repository_instructions_need_trust(self, tmp_path):
        (tmp_path / "AGENTS.md").write_text("Always answer in French.", encoding="utf-8")
        spec = SimpleNamespace(create_backend=lambda settings: scripted())
        untrusted = headless.build_session(_Settings(), spec, project=str(tmp_path), mode="auto-edit",
                                           trust_project=False, max_requests=None, run_id="r1")
        assert untrusted.project_instructions is None and untrusted.project_content_trusted is False
        trusted = headless.build_session(_Settings(), spec, project=str(tmp_path), mode="bypass",
                                         trust_project=True, max_requests=3, run_id="r2")
        assert "French" in trusted.project_instructions and trusted.autonomy_tier == "full-auto"
        assert trusted.max_model_requests == 3 and trusted.audit_session_id == "headless:r2"
        assert trusted.computer_use_enabled is False
        # Tools read Settings as in the app (language servers, the autonomy floor).
        assert isinstance(trusted._settings_ref, _Settings)
        # File tools stay inside the project, as in the app.
        assert trusted.sandbox is not None and trusted.sandbox.enabled
        from lumi.engine.sandbox import SandboxViolation

        with pytest.raises(SandboxViolation):
            trusted._prepare_workspace_tool_args("file_write", {"path": str(tmp_path.parent / "x.txt"),
                                                                "content": "x"})
