"""Gate hooks fail closed: a guard that times out or can't run blocks.

A PRE_TOOL_USE hook that hung past its ``timeout_seconds``, or couldn't be
started, used to record an error and let the tool run. On Windows the timeout
wasn't even enforced: ``subprocess.run`` killed the shell, then waited for the
Python program the shell had started, which held the output pipes.

These tests run real hook scripts through a real HookRunner, and check the
side effect itself (a file the tool would create, the process a hook started)
rather than only the reported result.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest

from lumi.engine.hooks import GATE_HOOK_TYPES, HookDefinition, HookRunner, HookType
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

# The sleeping hooks below would run for a minute. Returning well within this
# shows the timeout was enforced rather than waited out, with room for a slow
# machine to start Python.
_RETURNS_WITHIN = 20.0

# Sorted, so parallel test workers collect the same parameters.
GATE_TYPES = sorted(GATE_HOOK_TYPES, key=lambda hook_type: hook_type.value)
INFORMATIONAL_TYPES = [hook_type for hook_type in HookType if hook_type not in GATE_HOOK_TYPES]


class _HookSettings:
    """The slice of SettingsManager that HookRunner reads."""

    def __init__(self, hooks: list[dict]):
        self._hooks = list(hooks)

    def get(self, section, key=None, default=None):
        if section == "hooks" and key is None:
            return self._hooks
        return default


def _script(tmp_path: Path, name: str, body: str) -> str:
    script = tmp_path / f"{name}.py"
    script.write_text(body, encoding="utf-8")
    return f'"{sys.executable}" "{script}"'


def _sleeper(tmp_path: Path, pid_file: Path | None = None) -> str:
    """A hook that runs for a minute without reading its input."""
    lines = ["import os, time"]
    if pid_file is not None:
        lines.append(f"open({str(pid_file)!r}, 'w').write(str(os.getpid()))")
    lines.append("time.sleep(60)")
    return _script(tmp_path, "sleeper", "\n".join(lines) + "\n")


def _answering(tmp_path: Path, name: str, answer: dict) -> str:
    """A structured hook that prints ``answer``."""
    return _script(tmp_path, name, f"import json, sys\nsys.stdin.read()\nprint(json.dumps({answer!r}))\n")


def _runner(*hooks: HookDefinition) -> HookRunner:
    runner = HookRunner()
    runner.add_hooks(list(hooks))
    return runner


def _session(tmp_path: Path, backend: StreamingBackend, hooks: list[dict]) -> Session:
    """Full-auto, so the hooks are the only thing that can refuse a call."""
    session = Session(backend=backend, max_steps=3, auto_approve=True)
    session.autonomy_tier = "full-auto"
    session.project_path = str(tmp_path)
    session.sandbox = PathSandbox(str(tmp_path), enabled=True)
    session.hook_runner = HookRunner(_HookSettings(hooks))
    return session


def _one_call(name: str, args: dict) -> StreamingBackend:
    return StreamingBackend(scripts=[
        [tool_call(name, args, call_id="c1"), done()],
        [text_delta("Finished."), done()],
    ])


def _wait_for_pid(pid_file: Path, seconds: float = 10.0) -> int:
    """The process id a sleeper wrote, once it has written all of it."""
    deadline = time.monotonic() + seconds
    while True:
        try:
            return int(pid_file.read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError):
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.05)


def _still_running(psutil, pid: int) -> bool:
    try:
        process = psutil.Process(pid)
        # The hook's own program, not a process that reused its id.
        return process.status() != psutil.STATUS_ZOMBIE and "sleeper" in " ".join(process.cmdline())
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return False


# ── The runner: gate types fail closed, the others carry on ────────────


@pytest.mark.parametrize("input_format", ["env", "json"])
def test_a_gate_hook_that_times_out_blocks_and_names_itself(tmp_path, input_format):
    runner = _runner(HookDefinition(
        hook_type=HookType.PRE_TOOL_USE,
        command=_sleeper(tmp_path),
        name="slow-guard",
        input_format=input_format,
        timeout_seconds=1,
    ))
    # More than a pipe holds: writing an event the hook never reads mustn't
    # outlast the timeout either.
    args = {"path": "big.txt", "content": "x" * 100_000}

    started = time.monotonic()
    result = runner.run_hooks(
        HookType.PRE_TOOL_USE,
        {"project_path": str(tmp_path), "tool_args": args},
        tool_name="file_write",
    )

    assert time.monotonic() - started < _RETURNS_WITHIN
    assert result.allowed is False
    assert result.decision == "deny"
    assert result.failed is True
    assert result.reason.startswith('pre_tool_use hook `slow-guard` timed out after 1 s')
    assert "timeout_seconds" in result.reason


@pytest.mark.parametrize("job_object", [True, False], ids=["job-object", "without-job-object"])
def test_the_timeout_stops_the_program_the_hook_started(tmp_path, monkeypatch, job_object):
    psutil = pytest.importorskip("psutil")
    if not job_object:
        # Windows falls back to taskkill /T when it can't make a job object.
        def no_job(process, **kwargs):
            raise OSError("no job object")

        monkeypatch.setattr("lumi.engine.hooks.windows_kill_job", no_job)
    pid_file = tmp_path / "hook.pid"
    runner = _runner(HookDefinition(
        hook_type=HookType.PRE_TOOL_USE,
        command=_sleeper(tmp_path, pid_file),
        timeout_seconds=2,
    ))

    result = runner.run_hooks(HookType.PRE_TOOL_USE, {"project_path": str(tmp_path)}, tool_name="bash")

    assert result.decision == "deny"
    # The shell ran Python as its own child. Killing only the shell left that
    # program running, holding the output pipes open.
    pid = _wait_for_pid(pid_file, seconds=0)
    deadline = time.monotonic() + 5
    while _still_running(psutil, pid) and time.monotonic() < deadline:
        time.sleep(0.1)
    assert not _still_running(psutil, pid)


def test_a_program_a_hook_leaves_running_on_purpose_keeps_running(tmp_path):
    # Only a hook that runs out its time is stopped with what it started.
    psutil = pytest.importorskip("psutil")
    pid_file = tmp_path / "hook.pid"
    _sleeper(tmp_path, pid_file)  # writes sleeper.py, which the hook starts and leaves
    launcher = _script(tmp_path, "launcher", (
        "import subprocess, sys\n"
        f"subprocess.Popen([sys.executable, {str(tmp_path / 'sleeper.py')!r}],\n"
        "                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
    ))
    runner = _runner(HookDefinition(hook_type=HookType.POST_TOOL_USE, command=launcher))

    result = runner.run_hooks(HookType.POST_TOOL_USE, {"project_path": str(tmp_path)}, tool_name="bash")

    assert result.failed is False
    pid = _wait_for_pid(pid_file)
    try:
        time.sleep(0.5)
        assert _still_running(psutil, pid)
    finally:
        try:
            psutil.Process(pid).kill()
        except psutil.NoSuchProcess:
            pass


@pytest.mark.parametrize("hook_type", GATE_TYPES, ids=lambda hook_type: hook_type.value)
def test_every_gate_type_blocks_when_its_hook_cannot_run(tmp_path, hook_type):
    runner = _runner(HookDefinition(
        hook_type=hook_type, command=_script(tmp_path, "fine", "print('ok')\n"), name="guard",
    ))

    # A project folder that has gone away: the hook can't be started in it.
    result = runner.run_hooks(hook_type, {"project_path": str(tmp_path / "gone")}, tool_name="bash")

    assert result.allowed is False
    assert result.decision == "deny"
    assert result.failed is True
    assert result.reason.startswith(f'{hook_type.value} hook `guard` could not be run (')


@pytest.mark.parametrize("hook_type", INFORMATIONAL_TYPES, ids=lambda hook_type: hook_type.value)
def test_an_informational_hook_that_cannot_run_is_only_logged(tmp_path, hook_type):
    runner = _runner(HookDefinition(
        hook_type=hook_type, command=_script(tmp_path, "fine", "print('ok')\n"), name="observer",
    ))

    result = runner.run_hooks(hook_type, {"project_path": str(tmp_path / "gone")}, tool_name="bash")

    assert result.allowed is True
    assert result.decision == ""
    assert result.failed is True
    assert result.error.startswith(f'{hook_type.value} hook `observer` could not be run (')


def test_an_informational_hook_that_times_out_is_only_logged(tmp_path):
    runner = _runner(HookDefinition(
        hook_type=HookType.POST_TOOL_USE, command=_sleeper(tmp_path), name="observer", timeout_seconds=1,
    ))

    started = time.monotonic()
    result = runner.run_hooks(HookType.POST_TOOL_USE, {"project_path": str(tmp_path)}, tool_name="bash")

    assert time.monotonic() - started < _RETURNS_WITHIN
    assert result.allowed is True
    assert result.decision == ""
    assert result.failed is True
    assert result.error.startswith('post_tool_use hook `observer` timed out after 1 s')


def test_an_unexpected_answer_field_does_not_undo_a_deny(tmp_path):
    # Reading hookSpecificOutput as a dict raised after the deny was noted,
    # and the error handler then let the call through.
    answer = {"decision": "deny", "reason": "not today", "hookSpecificOutput": "text"}
    runner = _runner(HookDefinition(
        hook_type=HookType.PRE_TOOL_USE, command=_answering(tmp_path, "deny", answer), input_format="json",
    ))

    result = runner.run_hooks(HookType.PRE_TOOL_USE, {"project_path": str(tmp_path)}, tool_name="bash")

    assert result.allowed is False
    assert result.decision == "deny"
    assert result.reason == "not today"


@pytest.mark.parametrize("size, env_copy", [(1_000, True), (100_000, False)], ids=["small", "large"])
def test_a_json_hook_reads_large_arguments_from_standard_input_only(tmp_path, size, env_copy):
    # Linux can't start a process with a 128 KiB environment value, so a JSON
    # hook, which has the whole event on standard input, gets no copy of such
    # arguments in LUMI_TOOL_ARGS.
    seen = tmp_path / "seen.json"
    reader = _script(tmp_path, "reader", (
        "import json, os, sys\n"
        "event = json.load(sys.stdin)\n"
        f"open({str(seen)!r}, 'w').write(json.dumps({{\n"
        "    'stdin': len(event['tool_args']['content']), 'env': os.environ['LUMI_TOOL_ARGS']}))\n"
    ))
    runner = _runner(HookDefinition(hook_type=HookType.PRE_TOOL_USE, command=reader, input_format="json"))

    result = runner.run_hooks(
        HookType.PRE_TOOL_USE,
        {"project_path": str(tmp_path), "tool_args": {"content": "x" * size}},
        tool_name="file_write",
    )

    assert result.allowed is True
    assert result.failed is False
    seen_by_hook = json.loads(seen.read_text(encoding="utf-8"))
    assert seen_by_hook["stdin"] == size
    assert (seen_by_hook["env"] != "") is env_copy


def test_a_hook_without_a_project_runs_in_lumis_folder(tmp_path):
    # A session without a project passes "", which no process can start in.
    marker = tmp_path / "ran.txt"
    runner = _runner(HookDefinition(
        hook_type=HookType.PRE_TOOL_USE, command=_script(tmp_path, "mark", f"open({str(marker)!r}, 'w').close()\n"),
    ))

    result = runner.run_hooks(HookType.PRE_TOOL_USE, {"project_path": ""}, tool_name="bash")

    assert result.allowed is True
    assert result.failed is False
    assert marker.exists()


# ── Turns: the call doesn't run, and the model reads why ───────────────


def test_a_pre_tool_use_hook_that_times_out_blocks_the_call(tmp_path):
    hook = {"hook_type": "pre_tool_use", "name": "slow-guard", "command": _sleeper(tmp_path), "timeout_seconds": 1}
    session = _session(tmp_path, _one_call("file_write", {"path": "guarded.txt", "content": "x"}), [hook])

    started = time.monotonic()
    events = list(session.run("write it"))

    assert time.monotonic() - started < _RETURNS_WITHIN
    result = first_of_kind(events, "tool.result")
    assert result["denied"] is True
    assert result["output"].startswith('Blocked by hook: pre_tool_use hook `slow-guard` timed out after 1 s')
    assert "timeout_seconds" in result["output"]
    assert not (tmp_path / "guarded.txt").exists()
    tool_results = [entry["content"] for entry in session.conversation_history if entry.get("role") == "tool_result"]
    assert tool_results == [result["output"]]


def test_a_permission_hook_that_times_out_denies_with_its_reason(tmp_path):
    # Undecided prompts already failed closed; now the denial says why.
    hook = {"hook_type": "permission_request", "name": "slow-approver", "command": _sleeper(tmp_path),
            "timeout_seconds": 1}
    session = _session(tmp_path, _one_call("file_write", {"path": "asked.txt", "content": "x"}), [hook])
    # Ask, with nobody to answer: the read-only tier never asks a permission hook.
    session.autonomy_tier = "ask"

    events = list(session.run("write it"))

    result = first_of_kind(events, "tool.result")
    assert result["denied"] is True
    assert result["output"].startswith(
        'Tool execution denied by permission hook: permission_request hook `slow-approver` timed out after 1 s'
    )
    assert not (tmp_path / "asked.txt").exists()


def test_a_batch_gate_that_times_out_refuses_the_batch_and_tells_the_model(tmp_path):
    hook = {"hook_type": "pre_tool_batch", "name": "slow-batch-guard", "command": _sleeper(tmp_path),
            "timeout_seconds": 1}
    tasks = [{"prompt": "look around", "agent_type": "explore"}, {"prompt": "look again", "agent_type": "explore"}]
    backend = _one_call("task_batch", {"tasks": tasks})
    session = _session(tmp_path, backend, [hook])

    events = list(session.run("fan out"))

    result = first_of_kind(events, "tool.result")
    assert result["denied"] is True
    assert result["output"].startswith(
        'Task batch blocked by hook: pre_tool_batch hook `slow-batch-guard` timed out after 1 s'
    )
    # The refusal used to reach only the UI, so the model never learned why
    # its batch didn't run.
    entries = [entry for entry in session.conversation_history if entry.get("call_id") == "c1"]
    assert [entry["role"] for entry in entries] == ["tool_call", "tool_result"]
    assert entries[1]["content"] == result["output"]
    # No worker made a request: only the batch call and the parent's answer.
    assert backend.stream_count == 2


def test_a_completion_gate_that_times_out_ends_the_turn_instead_of_retrying(tmp_path):
    hook = {"hook_type": "task_completed", "name": "slow-check", "command": _sleeper(tmp_path), "timeout_seconds": 1}
    backend = StreamingBackend(scripts=[
        [text_delta("All done."), done()],
        [text_delta("All done, again."), done()],
    ])
    session = _session(tmp_path, backend, [hook])

    events = list(session.run("what does this project do?"))

    errors = [event["message"] for event in events_of_kind(events, "error")]
    assert len(errors) == 1
    assert errors[0].startswith('Completion was not accepted: task_completed hook `slow-check` timed out after 1 s')
    # A rejection asks the model to try again, but the model can't fix a hook
    # that doesn't answer; each retry would be another model request.
    assert backend.stream_count == 1
    assert first_of_kind(events, "session.end")["outcome"] == "failed"


def test_a_json_hook_receives_text_the_locale_cannot_encode(tmp_path):
    seen = tmp_path / "seen.json"
    reader = _script(tmp_path, "reader", (
        "import json, sys\n"
        "event = json.load(sys.stdin)\n"
        f"open({str(seen)!r}, 'w').write(json.dumps(event['tool_args']))\n"
        "print(json.dumps({'decision': 'allow'}))\n"
    ))
    content = "arrow →, check ✓, 日本語, \U0001f680"
    hook = {"hook_type": "pre_tool_use", "command": reader, "input_format": "json"}
    session = _session(tmp_path, _one_call("file_write", {"path": "unicode.txt", "content": content}), [hook])

    events = list(session.run("write it"))

    assert first_of_kind(events, "tool.result")["denied"] is False
    # Writing the event in a Windows code page failed before, so the hook
    # never saw this call.
    assert json.loads(seen.read_text(encoding="utf-8"))["content"] == content
    assert (tmp_path / "unicode.txt").read_text(encoding="utf-8") == content


def test_a_structured_deny_reports_its_reason(tmp_path):
    hook = {"hook_type": "pre_tool_use", "input_format": "json",
            "command": _answering(tmp_path, "deny", {"decision": "deny", "reason": "docs are frozen"})}
    session = _session(tmp_path, _one_call("file_write", {"path": "frozen.txt", "content": "x"}), [hook])

    events = list(session.run("write it"))

    result = first_of_kind(events, "tool.result")
    assert result["denied"] is True
    assert result["output"] == "Blocked by hook: docs are frozen"
    assert not (tmp_path / "frozen.txt").exists()


def test_a_block_reports_the_blocking_hooks_reason_not_an_earlier_allow(tmp_path):
    allow = {"hook_type": "pre_tool_use", "input_format": "json",
             "command": _answering(tmp_path, "allow", {"decision": "allow", "reason": "looks fine"})}
    block = {"hook_type": "pre_tool_use",
             "command": _script(tmp_path, "block", "import sys\nsys.stderr.write('secrets in content')\nsys.exit(2)\n")}
    session = _session(tmp_path, _one_call("file_write", {"path": "secret.txt", "content": "x"}), [allow, block])

    events = list(session.run("write it"))

    result = first_of_kind(events, "tool.result")
    assert result["denied"] is True
    assert result["output"] == "Blocked by hook: secrets in content"
    assert not (tmp_path / "secret.txt").exists()
