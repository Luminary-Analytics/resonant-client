"""Tool permission decisions: the user's answer is final and prompts fail closed.

Regression coverage for an Ask-mode defect. After the user pressed Deny, the
session emitted PERMISSION_REQUEST and treated ``HookResult``'s default
decision ("allow") as an approval, so the denied tool ran anyway. The GUI
always attaches a HookRunner, which is why these tests attach a real one
instead of the stub runners used by test_session_tool_dispatch.py.

Every case observes the side effect itself (a file the tool would create)
rather than only the reported result.
"""
from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

import pytest

from resonant_client.engine.hooks import HookRunner
from resonant_client.engine.policies import (
    ExecutionPolicy,
    PolicyAction,
    PolicyRule,
    policy_for_tier,
)
from resonant_client.engine.sandbox import PathSandbox
from resonant_client.engine.session import Session
from tests.streaming_stub import (
    StreamingBackend,
    done,
    events_of_kind,
    first_of_kind,
    text_delta,
    tool_call,
)


class _HookSettings:
    """The slice of SettingsManager that HookRunner reads."""

    def __init__(self, hooks: list[dict] | None = None):
        self._hooks = list(hooks or [])

    def get(self, section, key=None, default=None):
        if section == "hooks" and key is None:
            return self._hooks
        return default


def _hook_script(tmp_path: Path, name: str, body: str) -> str:
    script = tmp_path / f"{name}.py"
    script.write_text(body, encoding="utf-8")
    return f'"{sys.executable}" "{script}"'


def _permission_hook(tmp_path: Path, response: dict, *, marker: str = "") -> dict:
    """A structured PERMISSION_REQUEST hook that prints ``response`` as JSON."""
    lines = ["import json, sys", "sys.stdin.read()"]
    if marker:
        lines.append(f"open({str(tmp_path / marker)!r}, 'w').close()")
    lines.append(f"print(json.dumps({response!r}))")
    return {
        "hook_type": "permission_request",
        "command": _hook_script(tmp_path, "permission_hook", "\n".join(lines) + "\n"),
        "input_format": "json",
    }


def _session(
    tmp_path: Path,
    calls: list[tuple[str, dict]],
    *,
    tier: str,
    hooks: list[dict] | None = None,
) -> Session:
    script = [tool_call(name, args, call_id=f"c{index}") for index, (name, args) in enumerate(calls)]
    backend = StreamingBackend(scripts=[[*script, done()], [text_delta("Finished."), done()]])
    session = Session(backend=backend, max_steps=2, auto_approve=(tier == "full-auto"))
    session.autonomy_tier = tier
    session.project_path = str(tmp_path)
    session.sandbox = PathSandbox(str(tmp_path), enabled=True)
    session.hook_runner = HookRunner(_HookSettings(hooks))
    return session


def _write(name: str) -> tuple[str, dict]:
    return "file_write", {"path": name, "content": "created"}


def _results(events: list[dict]) -> dict[str, dict]:
    return {event["call_id"]: event for event in events_of_kind(events, "tool.result")}


# ── The user's decision is final ───────────────────────────────────────


def test_user_deny_is_final_with_a_real_hook_runner_attached(tmp_path):
    session = _session(tmp_path, [_write("denied.txt")], tier="suggest")
    asked = []

    events = list(session.run("write it", on_permission=lambda name, args: asked.append(name) or False))

    assert asked == ["file_write"]
    result = first_of_kind(events, "tool.result")
    assert result["denied"] is True
    assert "denied by user" in result["output"]
    assert not (tmp_path / "denied.txt").exists()


def test_permission_hook_cannot_override_the_users_deny(tmp_path):
    hook = _permission_hook(tmp_path, {"decision": "allow"}, marker="hook-ran")
    session = _session(tmp_path, [_write("denied.txt")], tier="suggest", hooks=[hook])

    events = list(session.run("write it", on_permission=lambda name, args: False))

    assert first_of_kind(events, "tool.result")["denied"] is True
    assert not (tmp_path / "denied.txt").exists()
    # The user answered, so the outcome was never undecided.
    assert not (tmp_path / "hook-ran").exists()


def test_users_approval_runs_the_tool(tmp_path):
    session = _session(tmp_path, [_write("approved.txt")], tier="suggest")

    events = list(session.run("write it", on_permission=lambda name, args: True))

    assert first_of_kind(events, "tool.result")["denied"] is False
    assert (tmp_path / "approved.txt").read_text(encoding="utf-8") == "created"


# ── No approval prompt available: only an explicit hook decision counts ──


def test_prompt_without_a_callback_fails_closed(tmp_path):
    # Auto-edit used to fall back to the legacy auto_approve flag, which the
    # GUI left True for every mode except Ask, so exec tools ran unprompted.
    session = _session(
        tmp_path,
        [("bash", {"command": "echo ran > ran.txt"})],
        tier="auto-edit",
    )

    events = list(session.run("run it"))

    result = first_of_kind(events, "tool.result")
    assert result["denied"] is True
    assert "requires approval" in result["output"]
    assert not (tmp_path / "ran.txt").exists()


def test_explicit_hook_allow_decides_an_unanswerable_prompt(tmp_path):
    hook = _permission_hook(tmp_path, {"decision": "allow"})
    session = _session(tmp_path, [_write("hook-approved.txt")], tier="suggest", hooks=[hook])

    events = list(session.run("write it"))

    assert first_of_kind(events, "tool.result")["denied"] is False
    assert (tmp_path / "hook-approved.txt").exists()


def test_explicit_hook_deny_decides_an_unanswerable_prompt(tmp_path):
    hook = _permission_hook(tmp_path, {"decision": "deny", "reason": "not here"})
    session = _session(tmp_path, [_write("hook-denied.txt")], tier="suggest", hooks=[hook])

    events = list(session.run("write it"))

    result = first_of_kind(events, "tool.result")
    assert result["denied"] is True
    assert "not here" in result["output"]
    assert not (tmp_path / "hook-denied.txt").exists()


def test_structured_hook_without_a_decision_is_not_an_approval(tmp_path):
    hook = _permission_hook(tmp_path, {"additional_context": "logged"})
    session = _session(tmp_path, [_write("undecided.txt")], tier="suggest", hooks=[hook])

    events = list(session.run("write it"))

    assert first_of_kind(events, "tool.result")["denied"] is True
    assert not (tmp_path / "undecided.txt").exists()


def test_legacy_hook_exit_zero_is_not_an_approval(tmp_path):
    hook = {
        "hook_type": "permission_request",
        "command": _hook_script(tmp_path, "legacy_hook", "print('observed')\n"),
    }
    session = _session(tmp_path, [_write("legacy.txt")], tier="suggest", hooks=[hook])

    events = list(session.run("write it"))

    assert first_of_kind(events, "tool.result")["denied"] is True
    assert not (tmp_path / "legacy.txt").exists()


def test_hook_rewritten_arguments_are_checked_against_the_policy_again(tmp_path):
    hook = _permission_hook(
        tmp_path,
        {"decision": "allow", "modified_args": {"command": "echo forbidden > rewritten.txt"}},
    )
    session = _session(
        tmp_path,
        [("bash", {"command": "echo harmless"})],
        tier="auto-edit",
        hooks=[hook],
    )
    session.execution_policy = ExecutionPolicy([
        PolicyRule(tool_pattern="bash", action="deny", arg_patterns={"command": "forbidden"},
                   reason="forbidden command"),
    ])

    events = list(session.run("run it"))

    result = first_of_kind(events, "tool.result")
    assert result["denied"] is True
    assert "forbidden command" in result["output"]
    assert not (tmp_path / "rewritten.txt").exists()


# ── Tier and policy semantics ──────────────────────────────────────────


def test_auto_edit_asks_for_exec_tools_but_not_file_edits(tmp_path):
    session = _session(
        tmp_path,
        [_write("edited.txt"), ("bash", {"command": "echo ran > ran.txt"})],
        tier="auto-edit",
    )
    asked = []

    events = list(session.run("edit then run", on_permission=lambda name, args: asked.append(name) or False))

    results = _results(events)
    assert asked == ["bash"]
    assert results["c0"]["denied"] is False
    assert results["c1"]["denied"] is True
    assert (tmp_path / "edited.txt").exists()
    assert not (tmp_path / "ran.txt").exists()


def test_policy_prompt_requires_approval_even_in_full_auto(tmp_path):
    session = _session(tmp_path, [_write("prompted.txt")], tier="full-auto")
    session.execution_policy = ExecutionPolicy([
        PolicyRule(tool_pattern="file_write", action="prompt", reason="review writes"),
    ])

    unanswered = list(session.run("write it"))

    assert first_of_kind(unanswered, "tool.result")["denied"] is True
    assert not (tmp_path / "prompted.txt").exists()

    asked = []
    session.backend = StreamingBackend(scripts=[
        [tool_call(*_write("prompted.txt"), call_id="c9"), done()],
        [text_delta("Finished."), done()],
    ])
    answered = list(session.run("write it", on_permission=lambda name, args: asked.append(name) or True))

    assert asked == ["file_write"]
    assert first_of_kind(answered, "tool.result")["denied"] is False
    assert (tmp_path / "prompted.txt").exists()


def test_repository_policy_cannot_weaken_built_in_denies():
    allow_everything = ExecutionPolicy.from_rules([
        {"tool_pattern": "*", "action": "allow", "reason": "repository says yes"},
    ])

    auto_edit = policy_for_tier("auto-edit").merge(allow_everything)
    suggest = policy_for_tier("suggest").merge(allow_everything)

    assert auto_edit.evaluate("bash", {"command": "rm -rf build"}) == PolicyAction.DENY
    assert "Recursive delete" in auto_edit.get_reason("bash", {"command": "rm -rf build"})
    assert suggest.evaluate("file_write", {"path": "x"}) == PolicyAction.DENY
    # Rules the repository adds still apply where no built-in deny matches.
    assert auto_edit.evaluate("bash", {"command": "ls"}) == PolicyAction.ALLOW


def test_repository_policy_can_still_tighten_the_built_in_policy():
    deny_writes = ExecutionPolicy.from_rules([
        {"tool_pattern": "file_write", "action": "deny", "reason": "frozen"},
    ])

    merged = policy_for_tier("full-auto").merge(deny_writes)

    assert merged.evaluate("file_write", {"path": "x"}) == PolicyAction.DENY
    assert merged.get_reason("file_write", {"path": "x"}) == "frozen"
    assert merged.evaluate("file_read", {"path": "x"}) == PolicyAction.ALLOW


# ── Delegated workers ask through the parent's prompt ──────────────────


def test_subagent_tools_use_the_parents_approval_prompt(tmp_path):
    backend = StreamingBackend(scripts=[
        [tool_call("task", {"prompt": "write the file", "agent_type": "build"}, call_id="t1"), done()],
        [tool_call(*_write("worker.txt"), call_id="w1"), done()],
        [text_delta("Worker finished."), done()],
        [text_delta("Parent finished."), done()],
    ])
    session = Session(backend=backend, max_steps=3, auto_approve=False)
    session.project_path = str(tmp_path)
    session.sandbox = PathSandbox(str(tmp_path), enabled=True)
    session.hook_runner = HookRunner(_HookSettings())
    asked = []

    def on_permission(name, args):
        asked.append(name)
        return name == "task"

    events = list(session.run("delegate", on_permission=on_permission))

    assert asked == ["task", "file_write"]
    worker_result = next(
        event for event in events_of_kind(events, "tool.result") if event.get("call_id") == "w1"
    )
    assert worker_result["denied"] is True
    assert not (tmp_path / "worker.txt").exists()


class _WorkerBackend(StreamingBackend):
    """Routes parallel workers by prompt instead of by global call order."""

    def stream(self, *, user_msg, conversation_history, instructions, tools, max_tokens, cancel_event=None):
        answered = any(entry.get("role") == "tool_result" for entry in conversation_history)
        if answered:
            yield text_delta("Finished.")
            yield done()
            return
        if str(user_msg).startswith("worker"):
            name = str(user_msg).split()[0]
            yield tool_call("file_write", {"path": f"{name}.txt", "content": "x"}, call_id=f"{name}-write")
        else:
            yield tool_call("task_batch", {"tasks": [
                {"prompt": "worker-a writes", "agent_type": "build"},
                {"prompt": "worker-b writes", "agent_type": "build"},
            ]}, call_id="batch")
        yield done()


def test_parallel_workers_ask_one_question_at_a_time(tmp_path):
    session = Session(backend=_WorkerBackend(), max_steps=3, auto_approve=False)
    session.project_path = str(tmp_path)
    session.sandbox = PathSandbox(str(tmp_path), enabled=True)
    session.hook_runner = HookRunner(_HookSettings())
    lock = threading.Lock()
    active = 0
    peak = 0
    asked = []

    def on_permission(name, args):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
            asked.append(name)
        time.sleep(0.2)
        with lock:
            active -= 1
        return name == "task_batch"

    list(session.run("fan out", on_permission=on_permission))

    assert sorted(asked) == ["file_write", "file_write", "task_batch"]
    assert peak == 1
    assert not (tmp_path / "worker-a.txt").exists()
    assert not (tmp_path / "worker-b.txt").exists()


def test_restarted_worker_asks_through_the_prompt_of_its_own_run(tmp_path):
    from resonant_client.engine.agent_runtime import AgentRegistry, AgentStatus

    registry = AgentRegistry(tmp_path, root=tmp_path / "agents")
    record = registry.create(agent_type="build", prompt="write the file")
    registry.transition(record.id, AgentStatus.RUNNING)
    backend = StreamingBackend(scripts=[
        [tool_call(*_write("restarted.txt"), call_id="w1"), done()],
        [text_delta("Worker finished."), done()],
    ])
    session = Session(backend=backend, max_steps=3, auto_approve=False)
    session.project_path = str(tmp_path)
    session.sandbox = PathSandbox(str(tmp_path), enabled=True)
    session.hook_runner = HookRunner(_HookSettings())
    session.agent_registry = AgentRegistry(tmp_path, root=tmp_path / "agents")
    # A prompt from an earlier turn whose UI channel is gone must not be reused.
    session._permission_prompt = lambda name, args: pytest.fail("stale prompt reused")
    asked = []

    list(session.restart_agent(record.id, on_permission=lambda name, args: asked.append(name) or False))

    assert asked == ["file_write"]
    assert not (tmp_path / "restarted.txt").exists()
