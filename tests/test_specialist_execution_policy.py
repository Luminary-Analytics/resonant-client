"""Orchestration specialists run under the organization's and the project's rules.

LocalSpecialistRunner gave each specialist session the Full-auto tier's
built-in policy: the guardrails, then "allow everything". Missions and
autonomous sessions dispatch through it, so a specialist could run a command
the organization's ``shell.rules`` or the project's lumi-policy.json refuse.
Specialists now get the policy the app gives a chat session
(project_execution_policy), read from the project root.

These run the runner with a scripted model and the real Session, and check
the side effect (the file the command writes) as well as the result.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from lumi import policy as lumi_policy
from lumi.engine.tools import AGENT_TOOLS
from lumi.gui.workspace_trust import WorkspaceTrust
from lumi.orchestration import LocalSpecialistRunner, NodeSpecialization, PlanGraph, PlanNode, new_node_id
from tests.streaming_stub import StreamingBackend, done, events_of_kind, text_delta, tool_call

ACME = {
    "schema": "lumi.policy/v1", "organization": "Acme",
    "shell": {"rules": [{"tool_pattern": "bash", "action": "deny",
                         "arg_patterns": {"command": "forbidden-by-acme"}, "reason": "Acme: no"}]},
}


@pytest.fixture(autouse=True)
def _isolated_state(tmp_path, monkeypatch):
    # Trust decisions live in the state folder: keep them in the test's own.
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("LUMI_STATE_HOME", str(home / ".lumi"))


def _project(tmp_path: Path, rules: list[dict] | None = None) -> Path:
    project = tmp_path / "project"
    project.mkdir()
    if rules is not None:
        (project / "lumi-policy.json").write_text(json.dumps({"rules": rules}), encoding="utf-8")
    return project


def _run_specialist(project: Path, command: str, *, working_subdir: str | None = None) -> dict:
    """One implement specialist whose model runs ``command`` with bash; returns the tool result."""
    backend = StreamingBackend(scripts=[
        [tool_call("bash", {"command": command}, call_id="c1"), done()],
        [text_delta("Finished."), done()],
    ])
    events: list[dict] = []
    runner = LocalSpecialistRunner(
        backend=backend, project_path=str(project), all_tools=list(AGENT_TOOLS), on_session_event=events.append,
    )
    graph = PlanGraph.new("write ran.txt")
    node = PlanNode(id=new_node_id(), intent_id=graph.intent_id, goal="write ran.txt",
                    specialization=NodeSpecialization.IMPLEMENT, working_subdir=working_subdir)
    graph.add_node(node)
    runner(node, graph)
    [result] = events_of_kind(events, "tool.result")
    return result


def test_an_organization_deny_refuses_a_specialists_command(tmp_path):
    lumi_policy.set_for_tests(lumi_policy.parse(ACME, source="test"))
    project = _project(tmp_path)

    result = _run_specialist(project, "echo forbidden-by-acme > ran.txt")

    assert result["denied"] is True
    assert result["output"] == "Blocked by policy: Acme: no"
    assert not (project / "ran.txt").exists()


def test_a_command_no_rule_refuses_still_runs(tmp_path):
    # The refusal above is the policy's: under the same rules, Full-auto runs this one.
    lumi_policy.set_for_tests(lumi_policy.parse(ACME, source="test"))
    project = _project(tmp_path)

    result = _run_specialist(project, "echo allowed-by-acme > ran.txt")

    assert not result.get("denied"), result["output"]
    assert (project / "ran.txt").read_text().strip() == "allowed-by-acme"


def test_the_projects_rules_apply_in_a_working_subdir(tmp_path):
    # The specialist works in web/, but lumi-policy.json is the project's, at its root.
    project = _project(tmp_path, [{"tool_pattern": "bash", "action": "deny",
                                   "arg_globs": {"command": "echo forbidden-by-project*"}, "reason": "Project: no"}])
    (project / "web").mkdir()

    result = _run_specialist(project, "echo forbidden-by-project > ran.txt", working_subdir="web")

    assert result["output"] == "Blocked by policy: Project: no"
    assert not (project / "web" / "ran.txt").exists()


@pytest.mark.parametrize("trusted", [True, False], ids=["trusted", "untrusted"])
def test_a_repository_allow_counts_only_in_a_trusted_project(tmp_path, trusted):
    # Everything else asks, and nobody can answer a specialist, so an ask refuses the call.
    project = _project(tmp_path, [
        {"tool_pattern": "bash", "action": "allow", "arg_globs": {"command": "echo made*"}},
        {"tool_pattern": "bash", "action": "prompt", "reason": "Ask first"},
    ])
    if trusted:
        WorkspaceTrust().trust(str(project))

    result = _run_specialist(project, "echo made > ran.txt")

    assert (project / "ran.txt").exists() is trusted
    if not trusted:
        assert result["denied"] is True
        assert "no approval prompt is available" in result["output"]
