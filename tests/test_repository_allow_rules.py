"""A trusted repository's lumi-policy.json allow rules answer Auto-edit's prompt.

The trust banner and Settings described these rules as skipping approval, but
the engine read a policy ``allow`` only as "not denied", so Auto-edit still
asked. Now a call whose first matching rule in the repository's own policy is
``allow`` runs without asking in Auto-edit (and Plan, which uses its tier).
The guardrails, organization rules and built-in denies still decide first,
Ask never skips its prompt, and a command that chains, substitutes or
redirects keeps asking.

These run Session.run with a scripted model and an approval callback, and
check the side effect (a folder the command creates) as well as the result.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from lumi import audit
from lumi import policy as lumi_policy
from lumi.audit import AuditLog
from lumi.engine.hooks import HookRunner
from lumi.engine.policies import PolicyAction, project_execution_policy
from lumi.engine.sandbox import PathSandbox
from lumi.engine.session import Session
from tests.streaming_stub import StreamingBackend, done, events_of_kind, first_of_kind, text_delta, tool_call

ALLOW_MKDIR = {"tool_pattern": "bash", "action": "allow", "arg_globs": {"command": "mkdir made*"}}
ALLOW_EVERYTHING = {"tool_pattern": "*", "action": "allow", "reason": "repository says yes"}


class _NoHooks:
    """The slice of SettingsManager a HookRunner reads, with no hooks.

    The app always attaches a HookRunner, so these do too.
    """

    def get(self, section, key=None, default=None):
        return [] if section == "hooks" and key is None else default


def _project(tmp_path: Path, rules: list[dict]) -> Path:
    project = tmp_path / "project"
    project.mkdir()
    (project / "lumi-policy.json").write_text(json.dumps({"rules": rules}), encoding="utf-8")
    return project


def _session(project: Path, name: str, arguments: dict, *, tier: str = "auto-edit",
             trusted: bool = True) -> Session:
    backend = StreamingBackend(scripts=[
        [tool_call(name, arguments, call_id="c1"), done()],
        [text_delta("Finished."), done()],
    ])
    session = Session(backend=backend, max_steps=2, auto_approve=False)
    session.autonomy_tier = tier
    session.project_path = str(project)
    session.sandbox = PathSandbox(str(project), enabled=True)
    session.hook_runner = HookRunner(_NoHooks())
    # As the app builds it (gui/app.py): allow rules count only in a trusted project.
    session.execution_policy = project_execution_policy(tier, str(project), honor_allows=trusted)
    return session


def _run(session: Session, *, answer: bool | None = False) -> tuple[list[str], dict]:
    """One turn. The approval prompt notes each tool it's asked about and answers;
    ``answer=None`` means nobody can be asked, as in `lumi run`."""
    asked: list[str] = []

    def on_permission(name, args):
        asked.append(name)
        return answer

    events = list(session.run("do it", on_permission=None if answer is None else on_permission))
    return asked, first_of_kind(events, "tool.result")


# ── Auto-edit runs what a trusted repository allows ────────────────────


def test_a_trusted_repositorys_allow_rule_runs_a_matching_command_without_asking(tmp_path):
    project = _project(tmp_path, [ALLOW_MKDIR])

    asked, result = _run(_session(project, "bash", {"command": "mkdir made"}))

    assert asked == []
    assert result["denied"] is False
    assert (project / "made").is_dir()


@pytest.mark.parametrize(("command", "trusted"), [
    ("mkdir other", True),                  # no rule matches
    ("mkdir made && mkdir other", True),    # the glob matches, but a second command is chained on
    ("mkdir made", False),                  # the user hasn't trusted the project
])
def test_auto_edit_still_asks_and_the_answer_decides(tmp_path, command, trusted):
    project = _project(tmp_path, [ALLOW_MKDIR])

    asked, result = _run(_session(project, "bash", {"command": command}, trusted=trusted))

    assert asked == ["bash"]
    assert result["denied"] is True
    assert not (project / "made").exists()
    assert not (project / "other").exists()


def test_a_broken_rule_in_the_file_turns_its_allow_rules_off(tmp_path):
    # Without the broken rule, a later allow could let through what that rule
    # was meant to stop (engine/policies.repository_rules). Its deny stays.
    project = _project(tmp_path, [
        {"tool_pattern": "bash", "action": "deny", "arg_globs": {"command": "mkdir made-by-mistake"}},
        {"tool_pattern": "bash", "action": "prompt", "arg_patterns": "mkdir"},
        ALLOW_MKDIR,
    ])

    asked, result = _run(_session(project, "bash", {"command": "mkdir made"}))

    assert asked == ["bash"]
    assert result["denied"] is True
    assert not (project / "made").exists()
    denied_asked, denied = _run(_session(project, "bash", {"command": "mkdir made-by-mistake"}), answer=True)
    assert denied_asked == [] and denied["denied"] is True
    assert not (project / "made-by-mistake").exists()


@pytest.mark.parametrize("command", [
    "npm test; whoami",
    "npm test & whoami",
    "npm test && whoami",
    "npm test || whoami",
    "npm test | findstr ok",
    "npm test > C:/Users/someone/startup.cmd",
    "npm test < answers.txt",
    "npm test `whoami`",
    "npm test $(whoami)",
    "npm test\nwhoami",
])
def test_a_prefix_rule_does_not_pre_approve_what_is_chained_on(tmp_path, command):
    project = _project(tmp_path, [{"tool_pattern": "bash", "action": "allow", "arg_globs": {"command": "npm test*"}}])
    policy = project_execution_policy("auto-edit", str(project))

    assert policy.repository_allows("bash", {"command": "npm test -- --watch=false"})
    # Not denied, so Auto-edit asks about it as it would without the rule.
    assert not policy.repository_allows("bash", {"command": command})
    assert policy.evaluate("bash", {"command": command}) == PolicyAction.ALLOW


def test_a_program_rule_checks_each_word(tmp_path):
    project = _project(tmp_path, [{"tool_pattern": "job_start", "action": "allow"}])
    policy = project_execution_policy("auto-edit", str(project))

    assert policy.repository_allows("job_start", {"command": ["npm", "run", "dev"]})
    assert not policy.repository_allows("job_start", {"command": ["sh", "-c", "npm run dev; whoami"]})


@pytest.mark.parametrize(("command", "asked_for"), [
    ("mkdir made", []),
    ("mkdir made-by-hand", ["bash"]),
])
def test_the_repositorys_first_matching_rule_decides(tmp_path, command, asked_for):
    project = _project(tmp_path, [
        {"tool_pattern": "bash", "action": "prompt", "arg_globs": {"command": "mkdir made-by-hand"}},
        {"tool_pattern": "bash", "action": "allow", "arg_globs": {"command": "mkdir *"}},
    ])

    asked, result = _run(_session(project, "bash", {"command": command}))

    assert asked == asked_for
    assert result["denied"] is bool(asked_for)
    assert (project / command.split()[1]).exists() is (asked_for == [])


# ── Everything ranked above the repository still decides first ─────────


@pytest.mark.parametrize(("command", "reason"), [
    ("rm -rf build", "Recursive delete blocked"),   # Auto-edit's built-in deny
    ("mkfs.fake x", "Formatting a disk"),           # a guardrail; harmless if it ever ran
])
def test_guardrails_and_built_in_denies_outrank_a_repository_allow(tmp_path, command, reason):
    project = _project(tmp_path, [ALLOW_EVERYTHING])
    (project / "build").mkdir()
    (project / "build" / "keep.txt").write_text("kept", encoding="utf-8")

    asked, result = _run(_session(project, "bash", {"command": command}), answer=True)

    assert asked == []
    assert result["denied"] is True
    assert reason in result["output"]
    assert (project / "build" / "keep.txt").exists()


def _organization(rules: list[dict]) -> None:
    """Install an organization policy with these shell rules (conftest removes it)."""
    lumi_policy.set_for_tests(lumi_policy.parse(
        {"schema": lumi_policy.SCHEMA, "organization": "Example Corp", "shell": {"rules": rules}}, source="test",
    ))


@pytest.mark.parametrize(("action", "asked_for"), [("deny", []), ("prompt", ["bash"])])
def test_organization_rules_outrank_a_repository_allow(tmp_path, action, asked_for):
    _organization([{"tool_pattern": "bash", "action": action, "arg_globs": {"command": "mkdir *"},
                    "reason": "organization rule"}])
    project = _project(tmp_path, [ALLOW_MKDIR])

    asked, result = _run(_session(project, "bash", {"command": "mkdir made"}))

    assert asked == asked_for
    assert result["denied"] is True
    assert not (project / "made").exists()


@pytest.mark.parametrize(("repository_rules", "asked_for"), [
    ([ALLOW_MKDIR], []),    # the organization's allow doesn't hide the repository's
    ([], ["bash"]),         # and doesn't skip the prompt itself
])
def test_an_organization_allow_leaves_the_prompt_to_the_repository(tmp_path, repository_rules, asked_for):
    _organization([{"tool_pattern": "bash", "action": "allow", "arg_globs": {"command": "mkdir *"}}])
    project = _project(tmp_path, repository_rules)

    asked, _ = _run(_session(project, "bash", {"command": "mkdir made"}))

    assert asked == asked_for
    assert (project / "made").exists() is (asked_for == [])


# ── Only Auto-edit, attended or not ────────────────────────────────────


@pytest.mark.parametrize(("tier", "asked_for"), [
    ("auto-edit", []), ("ask", ["check_run"]), ("suggest", ["check_run"]),
])
def test_only_auto_edit_lets_the_repository_answer(tmp_path, tier, asked_for):
    # Ask and the read-only tier ask about check_run instead of refusing it,
    # so only the tier decides here whether the repository's allow answers.
    project = _project(tmp_path, [ALLOW_EVERYTHING])

    asked, _ = _run(_session(project, "check_run", {"command": "mkdir made", "requirement": "it runs"}, tier=tier))

    assert asked == asked_for
    assert (project / "made").exists() is (tier == "auto-edit")


def test_an_unattended_run_runs_only_what_the_repository_allows(tmp_path):
    # `lumi run --trust-project` and trusted scheduled tasks can't ask anyone.
    project = _project(tmp_path, [ALLOW_MKDIR])

    _, allowed = _run(_session(project, "bash", {"command": "mkdir made"}), answer=None)
    _, refused = _run(_session(project, "bash", {"command": "mkdir other"}), answer=None)

    assert allowed["denied"] is False
    assert (project / "made").is_dir()
    assert refused["denied"] is True
    assert "requires approval" in refused["output"]
    assert not (project / "other").exists()


def test_a_worker_uses_the_same_rules(tmp_path):
    # Delegated workers inherit the tier and the policy (Session.copy_execution_context_from).
    project = _project(tmp_path, [ALLOW_MKDIR])
    session = _session(project, "task", {})
    session.max_steps = 3
    session.backend = StreamingBackend(scripts=[
        [tool_call("task", {"prompt": "make the folder", "agent_type": "build"}, call_id="t1"), done()],
        [tool_call("bash", {"command": "mkdir made"}, call_id="w1"), done()],
        [text_delta("Worker finished."), done()],
        [text_delta("Parent finished."), done()],
    ])
    asked: list[str] = []

    events = list(session.run("delegate", on_permission=lambda name, args: asked.append(name) or False))

    assert asked == []
    worker = next(event for event in events_of_kind(events, "tool.result") if event.get("call_id") == "w1")
    assert worker["denied"] is False
    assert (project / "made").is_dir()


# ── Trust and the audit log ────────────────────────────────────────────


def test_allow_rules_apply_only_to_the_policy_content_trust_saw(tmp_path):
    project = _project(tmp_path, [ALLOW_MKDIR])
    trusted = hashlib.sha256((project / "lumi-policy.json").read_bytes()).hexdigest()

    policy = project_execution_policy("auto-edit", str(project), policy_digest=trusted)
    assert policy.repository_allows("bash", {"command": "mkdir made"})

    # The file changed after the trust check read it: its allow rules are off.
    (project / "lumi-policy.json").write_text(json.dumps({"rules": [ALLOW_EVERYTHING]}), encoding="utf-8")
    policy = project_execution_policy("auto-edit", str(project), policy_digest=trusted)
    assert not policy.repository_allows("bash", {"command": "mkdir made"})
    assert not policy.repository_allows("bash", {"command": "whoami"})


def test_the_audit_log_records_that_the_repository_answered(tmp_path):
    log = AuditLog(tmp_path / "audit")
    audit.set_for_tests(log)
    try:
        project = _project(tmp_path, [ALLOW_MKDIR])
        _run(_session(project, "bash", {"command": "mkdir made"}))
    finally:
        audit.set_for_tests(None)

    records = [json.loads(line) for path in log._files() for line in path.read_text(encoding="utf-8").splitlines()]
    approval = next(record for record in records if record["type"] == "approval")["data"]
    assert approval == {**approval, "tool": "bash", "call_id": "c1", "by": "project_policy", "decision": "approved"}
