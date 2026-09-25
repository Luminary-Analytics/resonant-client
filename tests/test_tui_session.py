"""The terminal UI builds its session as ``lumi run`` does (headless.scope_session).

``lumi`` with no subcommand used to start a bare Session: no project, path
sandbox, file exclusions, execution policy or hooks, in Full-auto unless
--approve was given. The organization's shell rules and the review gate,
which live in the execution policy, never applied; excluded files and files
outside the project were read; the person's own hooks never ran; the
organization's permission modes were ignored; and --approve asked about
commands the guardrails refuse anyway.

These tests run the real ``main()`` in an isolated state folder, with Ollama,
the model and the keyboard scripted, and check what each call did and what
the model was told.
"""
from __future__ import annotations

import io
import json
import os
import re
import sys

import pytest
from rich.console import Console

from lumi import headless
from lumi import policy as lumi_policy
from lumi.backends import OllamaBackend
from lumi.engine import github_tools, os_sandbox
from lumi.engine.os_sandbox import Availability
from lumi.engine.session import Session
from lumi.gui.workspace_trust import WorkspaceTrust
from tests.streaming_stub import StreamingBackend, done, text_delta, tool_call
from tests.test_tui import tui  # imported the way Windows needs (tests/test_tui.py)

URL = "http://ollama.test"
WIDTH = 240  # wide enough that no message wraps
ORG_RULE = {"tool_pattern": "bash", "action": "deny", "arg_patterns": {"command": "org-denied"},
            "reason": "Acme: not from the agent's shell"}


class _Ollama(StreamingBackend):
    """The model, scripted; the terminal also asks it for its health and warms it up."""

    def health(self):
        return {"backend": "ollama", "model": self.model}

    def warm_up(self):
        pass


class _ListedOllama(OllamaBackend):
    """An Ollama backend, so /model applies, that lists its models without a server."""

    def __init__(self, model: str, models: list[str]):
        super().__init__(URL, model)
        self._models = models

    def health(self):
        return {"backend": "ollama", "model": self.model}

    def warm_up(self):
        pass

    def list_models(self):
        return list(self._models)


class Terminal:
    """``lumi.tui.main`` in an isolated state folder, with Ollama, the model and the keyboard scripted."""

    def __init__(self, tmp_path, monkeypatch):
        self.state = tmp_path / "state"
        self.project = tmp_path / "project"
        self.state.mkdir()
        self.project.mkdir()
        monkeypatch.setenv("LUMI_STATE_HOME", str(self.state))
        for name in ("LUMI_DEFAULT_BACKEND", "LUMI_DEFAULT_MODEL", "OLLAMA_HOST"):
            monkeypatch.delenv(name, raising=False)
        monkeypatch.chdir(tmp_path)  # main() changes folders; the test's comes back afterwards
        self._out = io.StringIO()
        monkeypatch.setattr(tui, "console", Console(
            file=self._out, width=WIDTH, color_system=None, force_terminal=False, legacy_windows=False,
        ))
        self.models = ["stub-model"]  # what Ollama has
        self.scripts: list[list] = []  # the model's replies: stream events, one list per request
        self.make_backend = lambda model: _Ollama(name="ollama", model=model, scripts=self.scripts)
        self.lines: list[str] = []  # typed at the prompt, then Ctrl+D
        self.answers: list[str] = []  # typed at "Allow …?"
        self.selections: list[str] = []  # typed at "Select …"
        self.asked: list[str] = []  # the tools "Allow …?" named
        self.sessions: list[Session] = []
        monkeypatch.setattr(tui, "_detect_backends",
                            lambda *args: {"ollama": {"url": URL, "models": list(self.models)}})
        monkeypatch.setattr(tui, "create_backend", lambda kind, url=None, **kwargs: self.make_backend(kwargs["model"]))
        monkeypatch.setattr(tui, "pt_prompt", self._prompt)
        build = tui.build_session

        def recorded(*args, **kwargs):
            self.sessions.append(build(*args, **kwargs))
            return self.sessions[-1]

        monkeypatch.setattr(tui, "build_session", recorded)

    def settings(self, **sections) -> None:
        (self.state / "settings.json").write_text(json.dumps(sections), encoding="utf-8")

    def _prompt(self, message, **kwargs):
        text = str(getattr(message, "value", message))
        asked = re.search(r"Allow (\w+)\?", text)
        if asked:
            self.asked.append(asked.group(1))
            return self.answers.pop(0) if self.answers else "n"
        if "Select" in text:
            return self.selections.pop(0) if self.selections else ""
        if self.lines:
            return self.lines.pop(0)
        raise EOFError  # Ctrl+D: the terminal says goodbye

    def run(self, *args: str, model: str = "stub-model"):
        """``lumi --backend ollama --model MODEL --dir PROJECT ARGS``: the session it built, or None."""
        built = len(self.sessions)
        tui.main(["--backend", "ollama", "--model", model, "--dir", str(self.project), *args])
        return self.sessions[-1] if len(self.sessions) > built else None

    @property
    def text(self) -> str:
        """Everything printed so far, each run of whitespace as one space."""
        return " ".join(self._out.getvalue().split())

    def told(self) -> dict[str, str]:
        """What the model was told of each call, by call id."""
        return {entry["call_id"]: entry["content"] for entry in self.sessions[-1].conversation_history
                if entry.get("role") == "tool_result"}


@pytest.fixture
def terminal(tmp_path, monkeypatch):
    yield Terminal(tmp_path, monkeypatch)
    # What main() configured process-wide (headless._configure) that conftest.py doesn't reset.
    github_tools.configure(None)
    os_sandbox.set_for_tests("off", None)
    lumi_policy.set_zero_retention_resolver(None)


def _policy(**sections) -> None:
    lumi_policy.set_for_tests(lumi_policy.parse(
        {"schema": "lumi.policy/v1", "organization": "Acme", **sections}, source="test"))


def _calls(*calls: tuple[str, str, dict]) -> list[list]:
    """A model request making these (call id, tool, arguments) calls, then one that answers."""
    return [[*(tool_call(name, args, call_id=call_id) for call_id, name, args in calls), done()],
            [text_delta("Done."), done()]]


# ── What a turn is held to ─────────────────────────────────────────────


def test_a_turn_meets_the_rules_lumi_run_and_the_app_enforce(terminal):
    project = terminal.project
    (project / ".env").write_text("API_TOKEN=dot-env-secret\n", encoding="utf-8")
    (project / "keys").mkdir()
    (project / "keys" / "deploy.pem").write_text("pem-secret\n", encoding="utf-8")
    outside = project.parent / "outside.txt"
    outside.write_text("outside-secret\n", encoding="utf-8")
    terminal.settings(
        privacy={"excluded_paths": [".env"]},
        review={"agent_changes": True, "reviewers": ["alice"]},
        # The person's own guard in Settings: no glob calls.
        hooks=[{"hook_type": "pre_tool_use", "matcher": "glob", "name": "no-glob",
                "command": f'"{sys.executable}" -c "import sys; sys.exit(1)"'}],
    )
    _policy(files={"exclude": ["**/*.pem"]}, shell={"rules": [ORG_RULE]})
    terminal.scripts = _calls(
        ("org", "bash", {"command": "echo org-denied> org.txt"}),
        ("push", "bash", {"command": "git push nowhere main"}),
        ("env", "file_read", {"path": ".env"}),
        ("pem", "file_read", {"path": "keys/deploy.pem"}),
        ("outside", "file_read", {"path": str(outside)}),
        ("hook", "glob", {"pattern": "*.txt"}),
    )
    terminal.lines = ["tidy up"]

    session = terminal.run()  # Bypass, the terminal's default: nothing asks

    told = terminal.told()
    assert told["org"] == "Blocked by policy: Acme: not from the agent's shell"
    assert told["push"].startswith("Blocked by policy: Pushing to the default branch is left to people")
    assert "is excluded by '.env' (Settings)" in told["env"]
    assert "is excluded by '**/*.pem' (organization policy)" in told["pem"]
    assert "Sandbox violation" in told["outside"]
    assert told["hook"].startswith("Blocked by hook: pre_tool_use hook `no-glob`")
    assert not (project / "org.txt").exists()
    assert terminal.asked == []
    # Nothing the rules hold back reached the conversation.
    history = json.dumps(session.conversation_history)
    assert [secret for secret in ("dot-env-secret", "pem-secret", "outside-secret") if secret in history] == []
    assert session.autonomy_tier == "full-auto"
    assert os.path.samefile(session.project_path, project)
    assert session.audit_session_id.startswith("tui:")


def test_approve_asks_before_changes_and_never_about_what_the_rules_refuse(terminal):
    project = terminal.project
    (project / "build").mkdir()
    (project / "README.md").write_text("# The app\n", encoding="utf-8")
    _policy(shell={"rules": [ORG_RULE]})
    terminal.scripts = _calls(
        ("never", "bash", {"command": "Stop-Computer -WhatIf"}),  # a guardrail: never runs, in any mode
        ("org", "bash", {"command": "echo org-denied> org.txt"}),
        ("recursive", "bash", {"command": "rm -rf build"}),  # refused in Ask, as in Auto-edit
        ("read", "file_read", {"path": "README.md"}),
        ("command", "bash", {"command": "echo approved> approved.txt"}),
        ("edit", "file_write", {"path": "notes.txt", "content": "notes"}),
    )
    terminal.lines = ["tidy up"]
    terminal.answers = ["y", "n"]  # the command yes, the edit no

    session = terminal.run("--approve")

    # Asked about the command and the edit only: reads don't ask, and what
    # the rules refuse is refused before anyone is asked.
    assert terminal.asked == ["bash", "file_write"]
    told = terminal.told()
    assert told["never"].startswith("Blocked by policy: Shutting down or restarting the computer is never allowed")
    assert told["org"] == "Blocked by policy: Acme: not from the agent's shell"
    assert told["recursive"] == "Blocked by policy: Recursive delete blocked — use a safer alternative"
    assert "# The app" in told["read"]
    assert (project / "approved.txt").is_file()
    assert told["edit"] == "Tool execution denied by user."
    assert not (project / "org.txt").exists() and not (project / "notes.txt").exists()
    assert (project / "build").is_dir()
    assert session.autonomy_tier == "ask"


def test_a_prompt_rule_asks_even_in_bypass(terminal):
    project = terminal.project
    # An untrusted project's rules still apply when they only make Lumi more careful.
    (project / "lumi-policy.json").write_text(json.dumps({"rules": [
        {"tool_pattern": "bash", "action": "prompt", "arg_patterns": {"command": "deploy"},
         "reason": "Deploys wait for a person"},
    ]}), encoding="utf-8")
    terminal.scripts = _calls(
        ("deploy", "bash", {"command": "echo deploy> deployed.txt"}),
        ("build", "bash", {"command": "echo build> built.txt"}),
    )
    terminal.lines = ["ship it"]
    terminal.answers = ["n"]

    terminal.run()

    assert terminal.asked == ["bash"]
    assert terminal.told()["deploy"] == "Tool execution denied by user."
    assert not (project / "deployed.txt").exists()
    assert (project / "built.txt").is_file()


def test_settings_an_organization_locks_reach_the_terminal(terminal):
    # The shell sandbox is required, on a computer where it can't run.
    os_sandbox.set_for_tests("off", Availability(False, reason="Not here."))
    _policy(settings={"security.shell_sandbox": "project", "security.computer_use": False})
    terminal.scripts = _calls(("command", "bash", {"command": "echo ran> ran.txt"}))
    terminal.lines = ["run it"]

    session = terminal.run()

    assert "can't run on this computer: Not here. No command was executed." in terminal.told()["command"]
    assert not (terminal.project / "ran.txt").exists()
    assert session.computer_use_enabled is False


# ── Modes and models the organization allows ───────────────────────────


def test_the_organization_policy_decides_the_mode(terminal):
    _policy(permissions={"allowed_modes": ["ask", "auto-edit"]})
    terminal.lines = ["/approve off", "/approve"]

    session = terminal.run()

    # Bypass, the default, isn't allowed, so the terminal asks instead.
    assert session.autonomy_tier == "ask"
    assert "mode ask asks before changes and commands · Acme's policy doesn't allow bypass" in terminal.text
    assert "Acme's policy doesn't allow bypass mode; here you can use ask, auto-edit." in terminal.text
    assert "Approval: ON · ask mode" in terminal.text


@pytest.mark.parametrize("allowed, args, refusal", [
    (["bypass"], ["--approve"], "Acme's policy doesn't allow ask mode; here you can use bypass."),
    (["plan"], [], "Acme's policy doesn't allow bypass mode, nor any other mode the terminal has."),
])
def test_a_mode_the_policy_refuses_stops_the_terminal(terminal, allowed, args, refusal):
    _policy(permissions={"allowed_modes": allowed})

    assert terminal.run(*args) is None
    assert refusal in terminal.text


def test_an_invalid_policy_stops_the_terminal(terminal):
    lumi_policy.set_for_tests(None, error="The organization policy at /etc/lumi/policy.json is invalid: bad rule.")

    assert terminal.run() is None
    assert "is invalid: bad rule. Ask your administrator to fix it." in terminal.text


def test_only_models_the_policy_allows_are_offered(terminal):
    terminal.models = ["blocked-coder", "open-coder"]
    _policy(models={"blocked": ["ollama:blocked-*"]})
    terminal.selections = ["1"]

    session = terminal.run(model="blocked-coder")

    assert "Model 'blocked-coder' isn't allowed by Acme's policy" in terminal.text
    assert re.findall(r"\d+\. (\S+)", terminal.text) == ["open-coder"]
    assert session.backend.model == "open-coder"


def test_a_policy_that_allows_none_of_the_models_stops_the_terminal(terminal):
    _policy(models={"allowed": ["anthropic:*"]})

    assert terminal.run() is None
    assert f"Acme's policy allows none of the models at {URL}" in terminal.text


def test_model_switches_only_to_models_the_policy_allows(terminal):
    terminal.models = ["open-coder", "blocked-coder", "open-writer"]
    terminal.make_backend = lambda model: _ListedOllama(model, terminal.models)
    _policy(models={"blocked": ["ollama:blocked-*"]})
    terminal.lines = ["/model"]
    terminal.selections = ["2"]

    session = terminal.run(model="open-coder")

    assert re.findall(r"\d+\. (\S+)", terminal.text) == ["open-coder", "open-writer"]
    assert session.backend.model == "open-writer"


# ── The project: trust, /cd and /approve ────────────────────────────────


def test_repository_instructions_apply_only_once_trusted_in_the_app(terminal):
    project = str(terminal.project)
    (terminal.project / "AGENTS.md").write_text("Always answer in French.", encoding="utf-8")

    untrusted = terminal.run()

    assert untrusted.project_instructions is None and untrusted.project_content_trusted is False
    assert "project not trusted its instructions, notes and policy allow rules are off" in terminal.text
    # The terminal records no trust of its own, nor the file the app's first
    # run looks for: that run still trusts the projects in Recent projects.
    assert WorkspaceTrust().status(project).decision == ""
    assert not (terminal.state / "trusted_projects.json").exists()

    WorkspaceTrust(recent_projects=[project])  # the app's first run, with this project in Recent projects
    trusted = terminal.run()

    assert "French" in trusted.project_instructions and trusted.project_content_trusted is True
    assert terminal.text.count("not trusted") == 1


def test_cd_moves_the_session_and_its_rules_to_the_new_folder(terminal):
    first = terminal.project
    (first / "first.txt").write_text("first-project-file", encoding="utf-8")
    other = first.parent / "other"
    other.mkdir()
    (other / ".lumiignore").write_text("secret.txt\n", encoding="utf-8")
    (other / "secret.txt").write_text("other-secret", encoding="utf-8")
    terminal.scripts = _calls(
        ("secret", "file_read", {"path": "secret.txt"}),
        ("first", "file_read", {"path": str(first / "first.txt")}),
    )
    terminal.lines = [f"/cd {other}", "look around", "/cd no-such-folder"]

    session = terminal.run()

    told = terminal.told()
    assert "is excluded by 'secret.txt' (.lumiignore)" in told["secret"]
    assert "Sandbox violation" in told["first"]
    # A folder that isn't there changes nothing.
    assert "no-such-folder" in terminal.text
    assert os.path.samefile(session.project_path, other) and os.path.samefile(os.getcwd(), other)


def test_approve_on_and_off_change_the_tier_and_its_rules(terminal):
    project = terminal.project
    (project / "build").mkdir()
    terminal.scripts = [
        [tool_call("bash", {"command": "rm -rf build"}, call_id="recursive"),
         tool_call("bash", {"command": "echo ok> ok.txt"}, call_id="ok"), done()],
        [text_delta("Done."), done()],
        [tool_call("bash", {"command": "echo later> later.txt"}, call_id="later"), done()],
        [text_delta("Done."), done()],
    ]
    terminal.lines = ["/approve on", "clean up", "/approve off", "note it"]
    terminal.answers = ["y"]

    session = terminal.run()

    # In Ask only the command it didn't refuse was asked about; in Bypass nothing was.
    assert terminal.asked == ["bash"]
    assert terminal.told()["recursive"] == "Blocked by policy: Recursive delete blocked — use a safer alternative"
    assert (project / "build").is_dir()
    assert (project / "ok.txt").is_file() and (project / "later.txt").is_file()
    assert session.autonomy_tier == "full-auto"


class _NoSettings:
    def get(self, section, key=None, default=None):
        return default


def test_a_rescope_that_fails_leaves_the_session_as_it_was(tmp_path, monkeypatch):
    first, second = tmp_path / "first", tmp_path / "second"
    first.mkdir()
    second.mkdir()
    session = Session(backend=StreamingBackend())
    headless.scope_session(session, _NoSettings(), str(first), tier="ask", trust_project=False)
    before = (session.project_path, session.sandbox, session.execution_policy, session.exclusions,
              session.autonomy_tier, session.project_instructions, session.project_content_trusted)

    def unavailable(*args, **kwargs):
        raise OSError("the drive went away")

    monkeypatch.setattr("lumi.engine.sandbox.PathSandbox", unavailable)
    with pytest.raises(OSError):
        headless.scope_session(session, _NoSettings(), str(second), tier="full-auto", trust_project=False)

    assert (session.project_path, session.sandbox, session.execution_policy, session.exclusions,
            session.autonomy_tier, session.project_instructions, session.project_content_trusted) == before
