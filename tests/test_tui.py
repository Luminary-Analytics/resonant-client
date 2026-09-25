"""The terminal UI shows why a refused tool call didn't run.

A TOOL_RESULT marked ``denied`` never ran, and its output is the reason the
model was given: a hook's message (a guard that timed out included), a policy
rule, a tool boundary, the session's allowlist, malformed arguments, or an
approval nobody could answer. The terminal printed only "✗ denied" and
dropped it, so a hook that never answered looked like the person's own Deny.
A refused read or search in a collapsed step read "0 matches".

These tests capture the TUI's Rich console as plain text. Two run real
sessions the way the TUI runs each message (`run_embedded`): one with a
hook that refuses, one answering the TUI's own approval prompt with "n".
"""
from __future__ import annotations

import importlib
import io
import sys

import pytest
from prompt_toolkit.application import create_app_session
from prompt_toolkit.data_structures import Size
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output.vt100 import Vt100_Output
from rich.cells import cell_len
from rich.console import Console

from lumi.engine.hooks import HookDefinition, HookRunner, HookType
from lumi.engine.sandbox import PathSandbox
from lumi.engine.session import Session
from tests.streaming_stub import StreamingBackend, done, text_delta, tool_call


def _import_tui():
    # On Windows, importing lumi.tui wraps sys.stdout and sys.stderr in new
    # UTF-8 text wrappers. Once pytest puts its own streams back, those
    # wrappers are collected and close pytest's capture files under them, so
    # the import wraps throwaway streams instead.
    streams = sys.stdout, sys.stderr
    sys.stdout = io.TextIOWrapper(io.BytesIO(), encoding="utf-8")
    sys.stderr = io.TextIOWrapper(io.BytesIO(), encoding="utf-8")
    try:
        return importlib.import_module("lumi.tui")
    finally:
        sys.stdout, sys.stderr = streams


tui = _import_tui()

WIDTH = 72
STATUS = "  │   "  # a tool result's line
REASON = "  │     "  # a refusal's reason, under the words of its status

# What the engine tells the model, verbatim (lumi/engine/session.py, hooks.py, sandbox.py).
REFUSALS = {
    "hook timeout": (
        "bash",
        "Blocked by hook: pre_tool_use hook `slow-guard` timed out after 2 s; gate hooks block "
        "when they give no answer. Raise its timeout_seconds if it needs longer.",
    ),
    "policy": ("bash", "Blocked by policy: Recursive delete blocked — use a safer alternative"),
    "tool boundary": (
        "file_write",
        "Blocked by tool boundary: Sandbox violation: 'C:\\Users\\me\\.ssh\\config' is outside "
        "project directory 'D:\\work\\app'",
    ),
    "allowlist": (
        "file_write",
        "Tool 'file_write' is not in this session's allowlist. Allowed tools: ['file_read', 'glob', 'grep']",
    ),
    "malformed arguments": (
        "file_edit",
        "Tool arguments were malformed: Expecting ',' delimiter: line 1 column 20 (char 19). "
        "Correct the arguments and call the tool again.",
    ),
    "no approval prompt": (
        "bash",
        "Tool execution requires approval, but no approval prompt is available for this run, so bash "
        "was not executed. Continue without it, or ask the user to switch to a permission mode that "
        "allows it.",
    ),
    "task batch hook": ("task_batch", "Task batch blocked by hook: [batch-guard] two workers at most"),
}


@pytest.fixture
def screen(monkeypatch):
    """The TUI's console, WIDTH cells wide; call it for the lines printed so far."""
    out = io.StringIO()
    monkeypatch.setattr(tui, "console", Console(
        file=out, width=WIDTH, color_system=None, force_terminal=False, legacy_windows=False,
    ))
    return lambda: out.getvalue().splitlines()


def _refused(name: str, output: str) -> dict:
    return {"event": "tool.result", "name": name, "call_id": "c1", "output": output,
            "is_error": False, "denied": True, "elapsed": 0.0}


def _reason_lines(lines: list[str], gutter: str) -> list[str]:
    """The reason's own text, one entry per printed line, gutter checked."""
    assert lines and all(line.startswith(gutter) for line in lines), lines
    return [line[len(gutter):].rstrip() for line in lines]


@pytest.mark.parametrize("name, reason", list(REFUSALS.values()), ids=list(REFUSALS))
def test_a_refused_call_shows_the_reason_the_model_got(screen, name, reason):
    tui._render_tool_result(_refused(name, reason))

    lines = screen()
    assert lines[0] == f"{STATUS}✗ not run"
    # Word for word, in order, and wrapped so every line keeps the gutter.
    assert " ".join(_reason_lines(lines[1:], REASON)).split() == reason.split()
    assert all(cell_len(line) <= WIDTH for line in lines)


@pytest.mark.parametrize("output", ["Tool execution denied by user.", "", "  "])
def test_the_persons_own_deny_reads_just_denied(screen, output):
    tui._render_tool_result(_refused("bash", output))

    assert screen() == [f"{STATUS}✗ denied"]


def test_a_reason_is_printed_as_text_never_as_markup(screen):
    # Unescaped, "[/]" raises MarkupError and the rest would become styles;
    # ":x:" would become an emoji, and the last backslash would escape the
    # closing tag after it.
    reason = "Blocked by hook: [/] [bold red]forged[/bold red] [link=https://example.com]x[/link] :x: C:\\hooks\\"

    tui._render_tool_result(_refused("bash", reason))

    lines = screen()
    assert lines[0] == f"{STATUS}✗ not run"
    assert " ".join(_reason_lines(lines[1:], REASON)) == reason


def test_a_long_unbroken_reason_folds_inside_the_gutter(screen):
    path = "/".join(["node_modules"] * 12) + "/index.js"  # 164 characters, no spaces
    reason = f"Blocked by tool boundary: Sandbox violation: '{path}' is outside project directory '/app'"

    tui._render_tool_result(_refused("file_read", reason))

    lines = screen()
    assert len(lines) > 4
    assert all(cell_len(line) <= WIDTH for line in lines)
    # Folded pieces join back up to the reason, nothing lost.
    folded = "".join(_reason_lines(lines[1:], REASON))
    assert folded.replace(" ", "") == reason.replace(" ", "")


# ── Real turns, run the way the TUI runs each message ──────────────────


def _session(tmp_path, backend: StreamingBackend) -> Session:
    session = Session(backend=backend, max_steps=5)
    session.project_path = str(tmp_path)
    session.sandbox = PathSandbox(str(tmp_path), enabled=True)
    return session


def test_a_turn_shows_why_a_hook_refused_its_calls(tmp_path, screen):
    # A guard hook that refuses every call, with brackets in its message.
    script = tmp_path / "guard.py"
    script.write_text(
        "import sys\nsys.stderr.write('[guard] refused: [/] secrets stay local\\n')\nsys.exit(1)\n",
        encoding="utf-8",
    )
    hooks = HookRunner()
    hooks.add_hooks([HookDefinition(
        hook_type=HookType.PRE_TOOL_USE, command=f'"{sys.executable}" "{script}"', name="guard",
    )])
    backend = StreamingBackend(scripts=[
        # Only a search: the step collapses into a compact group.
        [tool_call("grep", {"pattern": "API_KEY", "path": "."}, call_id="c1"), done()],
        # A command: the step renders in full.
        [tool_call("bash", {"command": "echo pushed > pushed.txt"}, call_id="c2"), done()],
        [text_delta("Neither ran."), done()],
    ])
    session = _session(tmp_path, backend)  # full-auto, the TUI's default: no prompt
    session.hook_runner = hooks

    tui.run_embedded(session, "find the key, then push")

    assert not (tmp_path / "pushed.txt").exists()
    reason = "Blocked by hook: [guard] refused: [/] secrets stay local"
    # The model was told exactly this, once per call.
    told = [entry["content"] for entry in session.conversation_history if entry.get("role") == "tool_result"]
    assert told == [reason, reason]
    lines = screen()
    # The search didn't find "0 matches": it didn't run.
    search = next(i for i, line in enumerate(lines) if "'API_KEY'" in line)
    assert lines[search].endswith("  ✗ not run")
    assert _reason_lines(lines[search + 1:search + 2], "    │   ") == [reason]
    command = lines.index(f"{STATUS}$ echo pushed > pushed.txt")
    status = lines.index(f"{STATUS}✗ not run", command)
    assert _reason_lines(lines[status + 1:status + 2], REASON) == [reason]
    assert not any("matches" in line or "denied" in line for line in lines)


def test_a_turn_shows_the_persons_own_deny_as_denied(tmp_path, screen):
    backend = StreamingBackend(scripts=[
        [tool_call("bash", {"command": "echo published > published.txt"}, call_id="c1"), done()],
        [text_delta("Not published."), done()],
    ])
    session = _session(tmp_path, backend)
    session.auto_approve = False  # `lumi --approve`: the TUI asks before commands
    asked = io.StringIO()
    prompt_output = Vt100_Output(asked, lambda: Size(rows=24, columns=WIDTH), term="xterm")

    # The TUI's own approval prompt, answered "n" as a person would type it.
    with create_pipe_input() as keys, create_app_session(input=keys, output=prompt_output):
        keys.send_text("n\r")
        tui.run_embedded(session, "publish it")

    assert "Allow bash? [Y/n]" in asked.getvalue()
    assert not (tmp_path / "published.txt").exists()
    lines = screen()
    command = lines.index(f"{STATUS}$ echo published > published.txt")
    assert lines[command + 1] == f"{STATUS}✗ denied"
    assert not any("not run" in line or "Tool execution denied" in line for line in lines)
