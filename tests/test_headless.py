"""`lumi run` (lumi/headless.py): one task without a UI, a JSON result and an
exit code a CI job can act on."""

from __future__ import annotations

import hashlib
import io
import json
import sys
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


def hook_script(tmp_path, name, body):
    """A hook command that runs ``body`` with this Python, kept outside the project."""
    folder = tmp_path.parent / f"{tmp_path.name}-hooks"
    folder.mkdir(exist_ok=True)
    script = folder / f"{name}.py"
    script.write_text(body, encoding="utf-8")
    return f'"{sys.executable}" "{script}"'


def approving_hook(tmp_path, marker):
    """A permission_request hook that allows every call it's asked about, and notes each one."""
    return {"hook_type": "permission_request", "input_format": "json", "command": hook_script(tmp_path, "approve", (
        "import json, os, sys\n"
        "sys.stdin.read()\n"
        f"open({str(marker)!r}, 'a').write(os.environ['LUMI_TOOL_NAME'] + '\\n')\n"
        "print(json.dumps({'decision': 'allow'}))\n"))}


def results(out):
    lines = [json.loads(line) for line in out.splitlines()]
    return [line for line in lines if line.get("event") == "tool.result"], lines[-1]


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

    def test_a_trusted_repositorys_allow_rules_run_their_commands(self, monkeypatch, tmp_path, ledger):
        (tmp_path / "lumi-policy.json").write_text(json.dumps({"rules": [
            {"tool_pattern": "bash", "action": "allow", "arg_globs": {"command": "mkdir made"}},
        ]}), encoding="utf-8")

        def attempt(*args):
            backend = scripted(call(0.01, tool_call("bash", {"command": "mkdir made"})),
                               call(0.01, text_delta("Done.")))
            code, out, _ = run(monkeypatch, tmp_path, backend, "Make the folder", "--mode", "auto-edit", *args)
            return code, json.loads(out)

        # Nobody can answer a prompt, so an untrusted repository's command is
        # refused, and so is a trusted run's when the policy isn't the version named.
        for args in ((), ("--trust-project", "--policy-digest", "0" * 64)):
            code, result = attempt(*args)
            assert (code, result["denied_calls"]) == (3, 1) and not (tmp_path / "made").exists()
        digest = hashlib.sha256((tmp_path / "lumi-policy.json").read_bytes()).hexdigest()
        for args in (("--trust-project", "--policy-digest", digest), ("--trust-project",)):
            code, result = attempt(*args)
            assert (code, result["status"], result["denied_calls"]) == (0, "completed", 0)
            assert (tmp_path / "made").is_dir()
            (tmp_path / "made").rmdir()

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

    @pytest.mark.parametrize("content", [b"[]", b'{"rules": 5}', b"\xff\xfe{\x00"],
                             ids=["a list", "rules is a number", "not UTF-8"])
    def test_a_malformed_repository_policy_keeps_the_organizations_rules(self, monkeypatch, tmp_path, ledger,
                                                                          content):
        # These used to crash the run: building the policy, or the trust check before it.
        lumi_policy.set_for_tests(parse({
            "schema": "lumi.policy/v1", "organization": "Acme",
            "shell": {"rules": [{"tool_pattern": "bash", "action": "deny",
                                 "arg_patterns": {"command": "forbidden-by-acme"},
                                 "reason": "Acme: not from the agent's shell"}]},
        }, source="t"))
        (tmp_path / "lumi-policy.json").write_bytes(content)
        backend = scripted(call(0.01, tool_call("bash", {"command": "echo forbidden-by-acme > ran.txt"})),
                           call(0.01, text_delta("That command was refused.")))

        code, out, err = run(monkeypatch, tmp_path, backend, "Run it", "--mode", "bypass", "--trust-project",
                             "--output", "jsonl")

        lines = [json.loads(line) for line in out.splitlines()]
        refused = [line for line in lines if line.get("event") == "tool.result"]
        assert refused and refused[0]["denied"] is True, (out, err)
        assert "Acme: not from the agent's shell" in refused[0]["output"]
        assert (code, lines[-1]["status"], lines[-1]["denied_calls"]) == (3, "needs_attention", 1)
        assert not (tmp_path / "ran.txt").exists()

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


class TestHooks:
    """The person's own hooks (``hooks`` in settings.json) run in `lumi run`, as in
    the app. `lumi run` used to build its session without them, so a Settings
    guard that refused a call in the app let it run here."""

    def test_a_settings_hook_guards_the_run(self, monkeypatch, tmp_path, ledger):
        from lumi.gui.settings import SettingsManager

        seen = tmp_path.parent / f"{tmp_path.name}-seen.txt"
        record = ("import os\n"
                  f"open({str(seen)!r}, 'a').write(os.environ['LUMI_HOOK_TYPE'] + ' ' + os.environ['LUMI_TOOL_NAME'] + '\\n')\n")
        guard = hook_script(tmp_path, "guard", record + "import sys\nsys.stderr.write('no writes from unattended runs')\n"
                                                        "sys.exit(1)\n")
        recorder = hook_script(tmp_path, "record", record)
        SettingsManager().set("hooks", None, [
            {"hook_type": "pre_tool_use", "matcher": "file_write", "name": "no writes", "command": guard},
            {"hook_type": "session_start", "command": recorder},
            {"hook_type": "session_end", "command": recorder},
        ])
        backend = scripted(call(0.01, tool_call("file_write", {"path": "notes.txt", "content": "hi"})),
                           call(0.01, text_delta("A hook refused the write.")))

        code, out, err = run(monkeypatch, tmp_path, backend, "Write a note", "--mode", "bypass", "--output", "jsonl")

        calls, result = results(out)
        assert calls[0]["denied"] is True, (out, err)
        assert calls[0]["output"] == "Blocked by hook: no writes from unattended runs"
        assert not (tmp_path / "notes.txt").exists()
        assert (code, result["status"], result["denied_calls"]) == (3, "needs_attention", 1)
        assert seen.read_text(encoding="utf-8").splitlines() == [
            "session_start ", "pre_tool_use file_write", "session_end "]

    def test_a_permission_hook_answers_what_the_mode_would_ask_about(self, monkeypatch, tmp_path, ledger):
        from lumi.gui.settings import SettingsManager

        # Auto-edit asks before commands, and nobody can answer in a run; the
        # person's own permission_request hook can, as in the app's background work.
        marker = tmp_path.parent / f"{tmp_path.name}-asked.txt"
        SettingsManager().set("hooks", None, [approving_hook(tmp_path, marker)])
        backend = scripted(call(0.01, tool_call("bash", {"command": "mkdir made"})), call(0.01, text_delta("Done.")))

        code, out, err = run(monkeypatch, tmp_path, backend, "Make the folder", "--mode", "auto-edit",
                             "--output", "jsonl")

        calls, result = results(out)
        assert calls[0]["denied"] is False, (out, err)
        assert (code, result["status"], result["denied_calls"]) == (0, "completed", 0)
        assert (tmp_path / "made").is_dir() and marker.read_text(encoding="utf-8") == "bash\n"

    def test_ask_stays_read_only_whatever_a_permission_hook_says(self, monkeypatch, tmp_path, ledger):
        from lumi.gui.settings import SettingsManager

        # The read-only tier's own policy refuses writes and commands; check_run
        # is left to the approval nobody can give. The hook isn't asked.
        marker = tmp_path.parent / f"{tmp_path.name}-asked.txt"
        SettingsManager().set("hooks", None, [approving_hook(tmp_path, marker)])
        backend = scripted(call(0.01, tool_call("check_run", {"command": "mkdir made", "requirement": "it runs"})),
                           call(0.01, text_delta("I only read.")))

        code, out, err = run(monkeypatch, tmp_path, backend, "Check it", "--mode", "ask", "--output", "jsonl")

        calls, result = results(out)
        assert calls[0]["denied"] is True, (out, err)
        assert "no approval prompt is available" in calls[0]["output"]
        assert (code, result["status"], result["denied_calls"]) == (3, "needs_attention", 1)
        assert not (tmp_path / "made").exists() and not marker.exists()

    def test_a_permission_hook_does_not_answer_for_the_organization(self, monkeypatch, tmp_path, ledger):
        from lumi.gui.settings import SettingsManager

        lumi_policy.set_for_tests(parse({
            "schema": "lumi.policy/v1", "organization": "Acme",
            "shell": {"rules": [{"tool_pattern": "bash", "action": "prompt", "arg_globs": {"command": "mkdir made*"},
                                 "reason": "Acme: a person approves new folders"}]},
        }, source="t"))
        marker = tmp_path.parent / f"{tmp_path.name}-asked.txt"
        SettingsManager().set("hooks", None, [approving_hook(tmp_path, marker)])
        backend = scripted(call(0.01, tool_call("bash", {"command": "mkdir made"})),
                           call(0.01, tool_call("bash", {"command": "mkdir other"}, call_id="c2")),
                           call(0.01, text_delta("Made one of them.")))

        code, out, err = run(monkeypatch, tmp_path, backend, "Make the folders", "--mode", "auto-edit",
                             "--output", "jsonl")

        calls, result = results(out)
        # Acme's prompt needs a person, whatever the person's own hook would say.
        assert calls[0]["denied"] is True, (out, err)
        assert calls[0]["output"] == (
            "The organization's policy requires a person to approve this call (Acme: a person approves new "
            "folders), but no approval prompt is available for this run, so bash was not executed. "
            "Continue without it.")
        assert not (tmp_path / "made").exists()
        # Auto-edit's own question about the other command is the person's to answer, and their hook does.
        assert calls[1]["denied"] is False and (tmp_path / "other").is_dir()
        assert marker.read_text(encoding="utf-8") == "bash\n"
        assert (code, result["denied_calls"]) == (3, 1)
