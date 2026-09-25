"""A worker's handoff reports only what its tool results show happened.

Regression coverage for the handoff a delegated worker (the ``task`` tool)
returns. Its changed files came from the worker's write *calls*, so an edit
the user rejected, a hook or policy blocked, that failed, or that never ran
was reported as changed to the parent model, the agent registry and the
handoff panel. Its validation called every check without an error "passed",
including a check that was denied and never ran, and Director Mode recorded
that as passing evidence.

The worker's own changes are observed on disk alongside the reported ones.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from lumi.codex_events import CodexEvents
from lumi.engine.agent_runtime import AgentRegistry
from lumi.engine.director import DirectorConfig, DirectorRun
from lumi.engine.hooks import HookResult, HookType
from lumi.engine.policies import ExecutionPolicy, PolicyRule
from lumi.engine.sandbox import PathSandbox
from lumi.engine.session import Session
from tests.streaming_stub import (
    StreamingBackend,
    done,
    events_of_kind,
    first_of_kind,
    text_delta,
    tool_call,
)


def _edit(old_text: str = "old line") -> tuple[str, dict]:
    return tool_call(
        "file_edit", {"path": "notes.txt", "old_text": old_text, "new_text": "new line"}, call_id="w1",
    )


def _delegate(
    tmp_path: Path,
    worker_step: tuple[str, dict],
    *,
    on_permission=None,
    backend_class: type[StreamingBackend] = StreamingBackend,
    **session_attributes,
) -> tuple[Session, list[dict]]:
    """Run a turn whose worker takes ``worker_step`` and then finishes.

    With an ``on_permission`` answer the session asks before changes
    (``suggest``); without one it runs in Full auto.
    """
    (tmp_path / "notes.txt").write_text("old line\n", encoding="utf-8")
    backend = backend_class(scripts=[
        [tool_call("task", {"prompt": "edit the notes", "agent_type": "build"}, call_id="t1"), done()],
        [worker_step, done()],
        [text_delta("Worker finished."), done()],
        [text_delta("Parent finished."), done()],
    ])
    session = Session(backend=backend, max_steps=3, auto_approve=on_permission is None)
    session.project_path = str(tmp_path)
    session.sandbox = PathSandbox(str(tmp_path), enabled=True)
    session.agent_registry = AgentRegistry(tmp_path, root=tmp_path / "agents")
    for name, value in session_attributes.items():
        setattr(session, name, value)
    return session, list(session.run("delegate the edit", on_permission=on_permission))


def _result(events: list[dict], call_id: str) -> dict | None:
    return next((e for e in events_of_kind(events, "tool.result") if e.get("call_id") == call_id), None)


def _handoff(events: list[dict]) -> dict:
    """The handoff shown in the worker panel, checked against the parent's copy."""
    handoff = first_of_kind(events, "subagent.end")["handoff"]
    assert _result(events, "t1")["metadata"]["handoff"] == handoff
    return handoff


class _FrozenEdits:
    """A PRE_TOOL_USE hook that blocks every file_edit."""

    def run_hooks(self, hook_type, *, context, tool_name):
        blocked = hook_type == HookType.PRE_TOOL_USE and tool_name == "file_edit"
        return HookResult(allowed=not blocked, error="edits are frozen" if blocked else "")


class _StopsTheWorkerAfterItsCall(StreamingBackend):
    """Stops the worker once it has asked for its edit, before the edit runs."""

    def stream(self, *, cancel_event=None, **kwargs):
        for event in super().stream(cancel_event=cancel_event, **kwargs):
            yield event
            if event[0] == "tool_call" and event[1]["call_id"] == "w1":
                cancel_event.set()


# ── Changed files ──────────────────────────────────────────────────────


def test_an_edit_that_ran_is_a_changed_file_everywhere_the_handoff_goes(tmp_path):
    session, events = _delegate(tmp_path, _edit())

    assert (tmp_path / "notes.txt").read_text(encoding="utf-8") == "new line\n"
    handoff = _handoff(events)
    assert handoff["changed_files"] == ["notes.txt"]
    # What the parent model reads, and the durable agent record.
    assert "Changed files:\n- notes.txt" in _result(events, "t1")["output"]
    worker = session.agent_registry.get(first_of_kind(events, "subagent.end")["agent_id"])
    assert worker.handoff["changed_files"] == ["notes.txt"]


@pytest.mark.parametrize(
    ("setup", "shape"),
    [
        # A denial by the user or a hook is not an error: only `denied` says so.
        pytest.param({"on_permission": lambda name, args: name == "task"}, (False, True),
                     id="rejected by the user"),
        pytest.param({"hook_runner": _FrozenEdits()}, (False, True), id="blocked by a hook"),
        pytest.param(
            {"execution_policy": ExecutionPolicy([
                PolicyRule(tool_pattern="file_edit", action="deny", reason="edits are frozen"),
            ])},
            (True, True),
            id="blocked by policy",
        ),
        pytest.param({"worker_step": _edit(old_text="missing line")}, (True, False), id="failed"),
    ],
)
def test_an_edit_that_did_not_happen_is_not_a_changed_file(tmp_path, setup, shape):
    setup = dict(setup)
    session, events = _delegate(tmp_path, setup.pop("worker_step", _edit()), **setup)

    assert (tmp_path / "notes.txt").read_text(encoding="utf-8") == "old line\n"
    result = _result(events, "w1")
    assert (result["is_error"], result["denied"]) == shape
    assert _handoff(events)["changed_files"] == []
    assert "Changed files" not in _result(events, "t1")["output"]
    worker = session.agent_registry.get(first_of_kind(events, "subagent.end")["agent_id"])
    assert worker.handoff["changed_files"] == []


def test_an_edit_that_never_ran_is_not_a_changed_file(tmp_path):
    _, events = _delegate(tmp_path, _edit(), backend_class=_StopsTheWorkerAfterItsCall)

    assert (tmp_path / "notes.txt").read_text(encoding="utf-8") == "old line\n"
    assert any(e.get("call_id") == "w1" for e in events_of_kind(events, "tool.call"))
    assert _result(events, "w1") is None
    handoff = _handoff(events)
    assert handoff["changed_files"] == []
    assert handoff["blockers"] == ["Interrupted"]


@pytest.mark.parametrize("succeeded", [True, False])
def test_a_cli_workers_file_change_counts_only_when_its_result_succeeds(tmp_path, succeeded):
    # Codex reports a file change in its result rather than naming it in a
    # write call. A parent on a CLI backend can't dispatch `task` itself (its
    # tool calls are display-only), so the worker is started directly.
    change = {"type": "item.completed", "item": {
        "id": "edit", "type": "file_change", "status": "completed" if succeeded else "failed",
        "changes": [{"path": "notes.txt", "kind": "update"}],
    }}
    backend = StreamingBackend(name="codex", handles_tools=True, scripts=[
        [*CodexEvents("gpt-6-astra").translate(change), text_delta("Edited the notes."), done()],
    ])
    session = Session(backend=backend, max_steps=3, auto_approve=True)
    session.project_path = str(tmp_path)
    arguments = {"prompt": "edit the notes", "agent_type": "build"}

    events = list(session._execute_task(arguments, "t1", json.dumps(arguments)))

    expected = [str((tmp_path / "notes.txt").resolve())] if succeeded else []
    assert _handoff(events)["changed_files"] == expected


# ── Validation ─────────────────────────────────────────────────────────


def _check() -> tuple[str, dict]:
    return tool_call("check_run", {"command": "python -m pytest -q", "requirement": "tests pass"},
                     call_id="w1")


def test_a_check_that_was_denied_is_reported_as_not_run(tmp_path):
    _, events = _delegate(tmp_path, _check(), on_permission=lambda name, args: name == "task")

    result = _result(events, "w1")
    assert (result["is_error"], result["denied"]) == (False, True)
    assert _handoff(events)["validation"] == ["check_run: not run"]
    assert "- check_run: not run" in _result(events, "t1")["output"]


def test_a_denied_check_does_not_pass_the_director_gate(tmp_path):
    run = DirectorRun(tmp_path, config=DirectorConfig.from_dict({
        "enabled": True,
        "director": {"backend": "ollama", "model": "frontier"},
        "workers": [{"id": "worker", "backend": "ollama", "model": "stub", "roles": ["implement"]}],
    }), root=tmp_path / "director")
    run.create_plan([{"id": "fix", "title": "Fix", "objective": "Fix the notes",
                      "role": "implement", "agent_type": "build"}])
    dispatch = {"prompt": "Fix the notes", "agent_type": "build", "director_task_id": "fix"}
    backend = StreamingBackend(scripts=[
        [tool_call("task", dispatch, call_id="t1"), done()],
        [_check(), done()],
        [text_delta("Checked."), done()],
        [text_delta("Reviewing."), done()],
    ])
    session = Session(backend=backend, max_steps=3, auto_approve=False)
    session.project_path = str(tmp_path)
    session.director_run = run

    list(session.run("fix the notes", on_permission=lambda name, args: name == "task"))

    task = run.tasks["fix"]
    assert [(item.name, item.passed, item.evidence) for item in task.validations] == [
        ("check_run", False, "check_run: not run"),
    ]
    allowed, reasons = run.acceptance_gate("fix")
    assert not allowed
    assert "One or more validations failed" in reasons
