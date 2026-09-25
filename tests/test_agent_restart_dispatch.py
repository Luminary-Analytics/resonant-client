"""Re-dispatching an interrupted worker.

A worker thread cannot survive a process restart, so `AgentRegistry` marks
anything still in flight as `stuck` on load. The assignment was already durable;
this covers the half that makes it useful — actually re-running it.

`Session.restart_agent` deliberately re-enters through `_execute_task`, the same
path a parent's `task` tool call takes, so agent-type resolution, tool filtering,
backend routing, worktree isolation, and handoff construction stay in exactly one
place. These tests pin that it goes through that path, that lineage survives, and
that the retry is told what its predecessor already did.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterator

import pytest

from lumi.engine.agent_runtime import AgentRegistry, AgentStatus
from lumi.engine.session import Session


class _StubBackend:
    name = "ollama"
    model = "glm-5.2"
    tool_mode = "native"
    base_url = "http://test"
    api_key = None

    def stream(self, **_kwargs) -> Iterator[tuple[str, dict]]:
        raise RuntimeError("stub stream — the child session stops here")


def _session_with_registry(tmp_path: Path) -> tuple[Session, AgentRegistry]:
    registry = AgentRegistry(tmp_path, root=tmp_path / "agents")
    session = Session(backend=_StubBackend())
    session.project_path = str(tmp_path)
    session.agent_registry = registry
    return session, registry


def _interrupted_agent(registry: AgentRegistry, *, steps: int = 2, **overrides):
    """An agent killed mid-flight, as a fresh registry would present it."""
    record = registry.create(
        agent_type=overrides.pop("agent_type", "explore"),
        prompt=overrides.pop("prompt", "Map the auth flow"),
        **overrides,
    )
    registry.transition(record.id, AgentStatus.RUNNING)
    for index in range(steps):
        registry.append_event(record.id, {"event": "step.end", "step": index + 1})
    return record


# ── Guard rails ─────────────────────────────────────────────────────────


def test_restart_requires_a_registry():
    session = Session(backend=_StubBackend())
    session.agent_registry = None

    with pytest.raises(RuntimeError, match="no agent registry"):
        list(session.restart_agent("whatever"))


def test_restarting_an_unknown_agent_raises(tmp_path: Path):
    session, _ = _session_with_registry(tmp_path)

    with pytest.raises(KeyError):
        list(session.restart_agent("does-not-exist"))


def test_restarting_a_completed_agent_is_refused(tmp_path: Path):
    from lumi.engine.agent_runtime import AgentHandoff

    session, registry = _session_with_registry(tmp_path)
    record = registry.create(agent_type="explore", prompt="Map it")
    registry.complete(record.id, AgentHandoff(outcome="completed", summary="done"))

    with pytest.raises(ValueError, match="nothing to restart"):
        list(session.restart_agent(record.id))


# ── The prompt handed to the retry ──────────────────────────────────────


def test_the_retry_is_told_what_its_predecessor_finished(tmp_path: Path):
    """A retry told nothing will redo — or undo — completed work."""
    _, registry = _session_with_registry(tmp_path)
    record = _interrupted_agent(registry, steps=3)
    reloaded = AgentRegistry(tmp_path, root=tmp_path / "agents")

    prompt = Session._restart_prompt(reloaded.restart_assignment(record.id))

    assert prompt.startswith("Map the auth flow")
    assert "3 steps" in prompt
    assert "Inspect the current state before repeating" in prompt
    assert "Runtime restarted" in prompt


def test_a_worker_that_never_stepped_gets_its_prompt_unchanged(tmp_path: Path):
    _, registry = _session_with_registry(tmp_path)
    record = _interrupted_agent(registry, steps=0)
    reloaded = AgentRegistry(tmp_path, root=tmp_path / "agents")

    prompt = Session._restart_prompt(reloaded.restart_assignment(record.id))

    assert prompt == "Map the auth flow"
    assert "RESTART" not in prompt


def test_the_step_count_reads_naturally_for_one_step(tmp_path: Path):
    _, registry = _session_with_registry(tmp_path)
    record = _interrupted_agent(registry, steps=1)
    reloaded = AgentRegistry(tmp_path, root=tmp_path / "agents")

    prompt = Session._restart_prompt(reloaded.restart_assignment(record.id))

    assert "1 step " in prompt
    assert "1 steps" not in prompt


# ── Dispatch goes through the normal task path ──────────────────────────


def test_restart_dispatches_through_execute_task(tmp_path: Path):
    session, registry = _session_with_registry(tmp_path)
    record = _interrupted_agent(registry, steps=2, agent_type="explore")
    # Reload so the record is `stuck`, exactly as after a process restart.
    session.agent_registry = AgentRegistry(tmp_path, root=tmp_path / "agents")

    seen = {}

    def _fake_execute_task(fn_args, call_id, fn_args_str, **kwargs):
        seen.update(fn_args=fn_args, call_id=call_id)
        yield {"event": "subagent.start", "agent_id": "new"}

    session._execute_task = _fake_execute_task

    events = list(session.restart_agent(record.id))

    # The worker's events, inside a turn of their own.
    assert [event["event"] for event in events] == ["session.start", "subagent.start", "session.end"]
    assert events[1] == {"event": "subagent.start", "agent_id": "new"}
    assert events[0]["restart_of"] == record.id
    assert seen["fn_args"]["agent_type"] == "explore"
    assert seen["fn_args"]["restart_of"] == record.id
    assert seen["fn_args"]["prompt"].startswith("Map the auth flow")
    # A distinct call id keeps the retry from colliding with the original.
    assert seen["call_id"].startswith(f"restart:{record.id}:")


def test_worktree_isolation_is_carried_into_the_retry(tmp_path: Path):
    """A worker that needed an isolated workspace still needs one."""
    session, registry = _session_with_registry(tmp_path)
    record = _interrupted_agent(
        registry, agent_type="build", metadata={"isolation": "worktree"},
    )
    session.agent_registry = AgentRegistry(tmp_path, root=tmp_path / "agents")

    seen = {}
    session._execute_task = lambda fn_args, *a, **k: (
        seen.update(fn_args=fn_args) or iter(())
    )

    list(session.restart_agent(record.id))

    assert seen["fn_args"]["isolation"] == "worktree"


def test_a_shared_workspace_worker_does_not_gain_isolation(tmp_path: Path):
    session, registry = _session_with_registry(tmp_path)
    record = _interrupted_agent(registry, metadata={"isolation": "shared"})
    session.agent_registry = AgentRegistry(tmp_path, root=tmp_path / "agents")

    seen = {}
    session._execute_task = lambda fn_args, *a, **k: (
        seen.update(fn_args=fn_args) or iter(())
    )

    list(session.restart_agent(record.id))

    assert "isolation" not in seen["fn_args"]


def test_a_stale_director_task_is_refused_rather_than_misattached(tmp_path: Path):
    """Replaying a stale task id into a different run would attach this
    worker to unrelated work."""
    session, registry = _session_with_registry(tmp_path)
    record = _interrupted_agent(
        registry, metadata={"director_task_id": "task-from-an-old-run"},
    )
    session.agent_registry = AgentRegistry(tmp_path, root=tmp_path / "agents")

    class _DirectorRun:
        tasks = {"a-different-task": object()}

    session.director_run = _DirectorRun()

    with pytest.raises(ValueError, match="not part of the current Director run"):
        list(session.restart_agent(record.id))


def test_a_director_task_id_is_dropped_when_no_director_is_running(tmp_path: Path):
    """Without a Director, the id means nothing and must not be replayed."""
    session, registry = _session_with_registry(tmp_path)
    record = _interrupted_agent(
        registry, metadata={"director_task_id": "task-1", "worker_id": "w1"},
    )
    session.agent_registry = AgentRegistry(tmp_path, root=tmp_path / "agents")
    session.director_run = None

    seen = {}
    session._execute_task = lambda fn_args, *a, **k: (
        seen.update(fn_args=fn_args) or iter(())
    )

    list(session.restart_agent(record.id))

    assert "director_task_id" not in seen["fn_args"]
    assert "worker_id" not in seen["fn_args"]


# ── Lineage ─────────────────────────────────────────────────────────────


def test_the_retry_record_links_back_to_the_run_it_replaced(tmp_path: Path):
    """`restart_of` reaches the registry record _execute_task creates."""
    session, registry = _session_with_registry(tmp_path)
    original = _interrupted_agent(registry, steps=1)
    reloaded = AgentRegistry(tmp_path, root=tmp_path / "agents")
    session.agent_registry = reloaded

    captured = {}

    def _fake_execute_task(fn_args, call_id, fn_args_str, **kwargs):
        # Mirror what _execute_task does with `restart_of`.
        captured["record"] = reloaded.create(
            agent_type=fn_args["agent_type"],
            prompt=fn_args["prompt"],
            metadata={"resumed_from": str(fn_args.get("restart_of") or "") or None},
        )
        return iter(())

    session._execute_task = _fake_execute_task
    list(session.restart_agent(original.id))

    retry = captured["record"]
    assert retry.metadata["resumed_from"] == original.id
    assert [r.id for r in reloaded.restarts_of(original.id)] == [retry.id]
    # The interrupted run is preserved, not overwritten by its retry.
    assert reloaded.get(original.id).status == "stuck"
    assert retry.id != original.id


# ── A restart is a turn of its own ──────────────────────────────────────
#
# A client shows the restart running between session.start and session.end,
# and replays a turn without a session.end as interrupted. The end's outcome
# comes from the worker's handoff, since the worker did the turn's work.


def _restart_with(tmp_path: Path, *, handoff: dict | None = None, refusal: str = ""):
    session, registry = _session_with_registry(tmp_path)
    record = _interrupted_agent(registry, agent_type="build", prompt="Fix the notes")
    session.agent_registry = AgentRegistry(tmp_path, root=tmp_path / "agents")

    def _fake_execute_task(fn_args, call_id, fn_args_str, **kwargs):
        if refusal:
            yield {"event": "tool.result", "name": "task", "call_id": call_id,
                   "output": refusal, "is_error": True, "denied": True}
            return
        yield {"event": "subagent.end", "call_id": call_id, "steps": 3, "handoff": handoff}
        yield {"event": "tool.result", "name": "task", "call_id": call_id,
               "output": "handoff", "is_error": False}

    session._execute_task = _fake_execute_task
    return list(session.restart_agent(record.id))


def test_a_restart_that_changed_files_ends_with_those_changes(tmp_path: Path):
    events = _restart_with(tmp_path, handoff={
        "outcome": "completed", "summary": "Fixed the notes.",
        "changed_files": ["notes.txt"], "blockers": [],
    })

    end = events[-1]
    assert end["event"] == "session.end"
    assert end["outcome"] == "changed_unverified"
    assert end["evidence"]["changed_files"] == ["notes.txt"]
    assert end["evidence"]["visible_answer"] is True
    # A worker's check results are not named checks.
    assert end["evidence"]["checks"] == []
    assert end["total_steps"] == 3


def test_a_restart_whose_worker_failed_ends_failed(tmp_path: Path):
    events = _restart_with(tmp_path, handoff={
        "outcome": "failed", "summary": "(no output)",
        "changed_files": [], "blockers": ["Interrupted"],
    })

    end = events[-1]
    assert end["outcome"] == "failed"
    assert end["evidence"]["visible_answer"] is False


def test_a_refused_dispatch_ends_the_restart_failed(tmp_path: Path):
    events = _restart_with(tmp_path, refusal="Sub-agent blocked by lifecycle hook: no")

    assert [event["event"] for event in events] == ["session.start", "tool.result", "session.end"]
    assert events[-1]["outcome"] == "failed"


def test_a_refused_restart_starts_no_turn(tmp_path: Path):
    session, registry = _session_with_registry(tmp_path)
    record = _interrupted_agent(registry, metadata={"director_task_id": "task-from-an-old-run"})
    session.agent_registry = AgentRegistry(tmp_path, root=tmp_path / "agents")

    class _DirectorRun:
        tasks = {"a-different-task": object()}

    session.director_run = _DirectorRun()
    events = session.restart_agent(record.id)

    # The refusal comes before anything is yielded, so no turn is left open.
    with pytest.raises(ValueError):
        next(events)
