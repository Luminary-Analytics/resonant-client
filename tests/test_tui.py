"""The terminal UI shows why a refused tool call didn't run, and prints text
it didn't write exactly as written.

A TOOL_RESULT marked ``denied`` never ran, and its output is the reason the
model was given: a hook's message (a guard that timed out included), a policy
rule, a tool boundary, the session's allowlist, malformed arguments, or an
approval nobody could answer. The terminal printed only "✗ denied" and
dropped it, so a hook that never answered looked like the person's own Deny.
A refused read or search in a collapsed step read "0 matches".

Tool arguments and output, the model's words and model names go into Rich
markup. Unescaped, a grep pattern's "[a-z_]" class vanished as an unknown
style, "[/]" raised MarkupError out of consume_events, ":a:" became an
emoji, and a backslash before a "[" or at the end was lost. main()'s own
lines hold outside text too: folder names, the Ollama server's address and
model names, errors, and the commands the person typed. There "/cd [/]" and
"/[/]" raised MarkupError out of main(), which ended the TUI. The prompts
are prompt_toolkit HTML, which is XML: a folder named "R&D" raised
ExpatError at the first prompt.

These tests capture the TUI's Rich console as plain text. Four run real
sessions the way the TUI runs each message (`run_embedded`): one with a
hook that refuses, one answering the TUI's own approval prompt with "n",
one whose search pattern and command output hold brackets, and one asking
about a tool whose name holds "&" and "<". main() runs with prompt_toolkit
reading typed keys from a pipe, and no Ollama server.
"""
from __future__ import annotations

import importlib
import io
import itertools
import json
import os
import re
import sys
import types

import pytest
from prompt_toolkit.application import create_app_session
from prompt_toolkit.data_structures import Size
from prompt_toolkit.formatted_text import HTML, fragment_list_to_text, to_formatted_text
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output.vt100 import Vt100_Output
from rich.cells import cell_len
from rich.console import Console
from rich.text import Text

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


# ── Text the TUI didn't write prints as written ────────────────────────

CALL = "  ┃ "  # a tool call's line


def test_escaped_text_reads_back_exactly():
    # Every string of these characters up to six long, between the TUI's own
    # tags: tags and closing tags, backslashes before a "[" (one that opens a
    # tag and one that doesn't) and at the end, where they meet the closing tag.
    for length in range(7):
        for chars in itertools.product("[]\\/a ", repeat=length):
            text = "".join(chars)
            markup = f"[{tui.C_TEXT}]{tui._esc(text)}[/{tui.C_TEXT}]"
            assert Text.from_markup(markup, emoji=False).plain == text, markup


@pytest.mark.parametrize("text", [
    "'[a-z_]+\\(' 3 matches",  # the class read as a style: "'+\(' 3 matches"
    "Error: unexpected [/] in input",  # MarkupError: nothing to close
    "[bold]x[/bold] [/something]",  # "[/something]" is a MarkupError too
    "[link=https://example.com]y[/link] [@click=quit]z",
    "\\[0-9]+\\] and \\[bold]",  # a regex's escaped brackets lost their backslash
    "sed -e 's:a:b:' && echo :x:",  # emoji codes
    "C:\\hooks\\", "C:\\hooks\\\\", "a\\\\\\",  # backslashes before the closing tag
])
def test_outside_text_prints_as_written(screen, text):
    tui._print(f"  [{tui.C_TEXT}]{tui._esc(text)}[/{tui.C_TEXT}]")

    assert screen() == [f"  {text}"]


TOOL_CALLS = {
    "grep pattern": (
        "grep", {"pattern": "[a-z_]+\\(", "path": "src/[old]"},
        [f"{CALL}/ '[a-z_]+\\('  (src/[old])"],
    ),
    "glob pattern": ("glob", {"pattern": "**/[a-z]*.py", "path": "."}, [f"{CALL}✱ **/[a-z]*.py  (.)"]),
    "file path": ("file_read", {"path": "docs/[/]/[bold]notes.md"}, [f"{CALL}→ docs/[/]/[bold]notes.md"]),
    "command": (
        "bash", {"command": 'echo "[/] [bold]x[/bold]" :a: C:\\tmp\\'},
        [f"{CALL}$ Shell", f'{STATUS}$ echo "[/] [bold]x[/bold]" :a: C:\\tmp\\'],
    ),
    "task prompt": (
        "task", {"prompt": "find [/] and [bold]", "agent_type": "[explore]"},
        [f"{CALL}│ Task [explore] agent", f'{STATUS}"find [/] and [bold]"'],
    ),
    "batch lines": (
        "batch", {"calls": [
            {"name": "grep", "arguments": {"pattern": "[a-z_]+\\("}},
            {"name": "file_read", "arguments": {"path": "a/[b]\\"}},
            {"name": "[/]mystery", "arguments": {}},
        ]},
        [f"{CALL}⚡ Batch 3 parallel calls", f"{STATUS}/ Grep '[a-z_]+\\('",
         f"{STATUS}→ Read a/[b]\\", f"{STATUS}⚙ [/]mystery"],
    ),
    "url": (
        "browser_navigate", {"url": "https://example.test/?filter[name]=a"},
        [f"{CALL}⊕ https://example.test/?filter[name]=a"],
    ),
    "selector": ("browser_click", {"selector": 'a[href="/login"]'}, [f'{CALL}◎ Click a[href="/login"]']),
    "typed text": ("browser_type", {"text": "C:\\tmp\\"}, [f"{CALL}⌨ Type 'C:\\tmp\\'"]),
    "read selector": (
        "browser_read", {"mode": "text", "selector": "div[data-x]"},
        [f"{CALL}◫ Read page  (text · div[data-x])"],
    ),
    "script": (
        "browser_js", {"code": "document.querySelectorAll('a[href]')[0]"},
        [f"{CALL}⟐ JavaScript", f"{STATUS}document.querySelectorAll('a[href]')[0]"],
    ),
    "desktop typing": ("computer_type", {"text": "[/] done\\"}, [f"{CALL}⌨ Type '[/] done\\'"]),
    "desktop click": (
        "computer_click", {"x": "[b]", "y": 2, "button": "[/]"},
        [f"{CALL}◎ Click ([b], 2)  ([/])"],
    ),
    "unknown tool": ("mcp__docs__[bold]search", {}, [f"{CALL}⚙ mcp__docs__[bold]search"]),
}


@pytest.mark.parametrize("name, arguments, expected", list(TOOL_CALLS.values()), ids=list(TOOL_CALLS))
def test_a_tool_call_prints_its_arguments_as_written(screen, name, arguments, expected):
    tui._render_tool_call({"event": "tool.call", "name": name, "arguments": arguments})

    assert screen() == expected


def test_a_file_change_prints_its_path_and_text_as_written(screen):
    tui._render_tool_call({"event": "tool.call", "name": "file_edit", "arguments": {
        "path": "app/[id].py",
        "old_text": "def f(x: list[str]):",
        "new_text": "def f(x: list[str]) -> dict[str, int]:  # [/] :a:",
    }})
    tui._render_tool_call({"event": "tool.call", "name": "file_write", "arguments": {
        "path": "notes/[draft].md", "content": "[/]",
    }})

    lines = screen()
    assert lines[0] == f"{CALL}~ Edit app/[id].py"
    # Inside the diff's panel: the type hints were "list" and "dict" before.
    diff = "\n".join(lines)
    assert "- def f(x: list[str]):" in diff
    assert "+ def f(x: list[str]) -> dict[str, int]:  # [/] :a:" in diff
    assert f"{CALL}← Write notes/[draft].md" in lines


def _result(name: str, output: str, *, is_error: bool = False, metadata: dict | None = None,
            elapsed: float = 0.0) -> dict:
    return {"event": "tool.result", "name": name, "call_id": "c1", "output": output,
            "is_error": is_error, "denied": False, "elapsed": elapsed, "metadata": metadata or {}}


TOOL_RESULTS = {
    "read error": (
        _result("file_read", "Error: unexpected [/] in input", is_error=True),
        [f"{STATUS}✗ Error: unexpected [/] in input"],
    ),
    "edit error": (
        _result("file_edit", "old_text not found in app/[id].py: [bold]x[/bold]", is_error=True),
        [f"{STATUS}✗ old_text not found in app/[id].py: [bold]x[/bold]"],
    ),
    "command output": (
        _result("bash", "[/] [bold]x[/bold] \\[0-9] :a:\nC:\\tmp\\\n(exit code: 3)",
                is_error=True, metadata={"exit_code": 3}, elapsed=0.5),
        [f"{STATUS}[/] [bold]x[/bold] \\[0-9] :a:", f"{STATUS}C:\\tmp\\",
         f"{STATUS}(exit code: 3)", f"{STATUS}0.5s · exit 3"],
    ),
    "script output": (_result("browser_js", "[/]\n[1, 2][0]"), [f"{STATUS}[/]", f"{STATUS}[1, 2][0]"]),
    "page title": (
        _result("browser_navigate", "Navigated.", metadata={"title": "Docs [beta] — [/]"}),
        [f"{STATUS}✓ Docs [beta] — [/]"],
    ),
    "click output": (_result("browser_click", 'Clicked a[href="/login"]'), [f'{STATUS}✓ Clicked a[href="/login"]']),
    "screenshot error": (
        _result("browser_screenshot", "Error: [/] no page", is_error=True),
        [f"{STATUS}✗ Error: [/] no page"],
    ),
    "desktop output": (_result("computer_type", "Typed '[/]'"), [f"{STATUS}✓ Typed '[/]'"]),
    "other tool error": (
        _result("mcp__docs__search", "Error [E42]: [/] missing \\[x] :a: C:\\", is_error=True),
        [f"{STATUS}✗ Error [E42]: [/] missing \\[x] :a: C:\\"],
    ),
}


@pytest.mark.parametrize("event, expected", list(TOOL_RESULTS.values()), ids=list(TOOL_RESULTS))
def test_a_tool_result_prints_its_output_as_written(screen, event, expected):
    tui._render_tool_result(event)

    assert screen() == expected


def test_a_turn_prints_what_tools_and_the_model_said_as_written(screen):
    events = [
        {"event": "step.start", "step": 1, "step_type": "execute", "label": ""},
        # Only a search and a read: the step collapses to one line per call.
        {"event": "tool.call", "name": "grep", "arguments": {"pattern": "[a-z_]+\\(", "path": "."}},
        _result("grep", "app.py:1: def load(", metadata={"count": 3}),
        {"event": "tool.call", "name": "file_read", "arguments": {"path": "docs/[/]/[bold]notes.md"}},
        _result("file_read", "…", metadata={"lines": 12}),
        {"event": "status", "model": "local[/]:a:"},
        {"event": "step.end", "step": 1, "elapsed": 0.2},
        {"event": "step.start", "step": 2, "step_type": "execute", "label": "after [/]mystery"},
        {"event": "subagent.start", "agent_type": "[explore]", "prompt": "look for [/] in [bold]"},
        {"event": "subagent.end", "agent_type": "[explore]", "steps": 2, "elapsed": 1.5},
        {"event": "error", "message": "Backend error [500]: unexpected [/] in input C:\\"},
        {"event": "choices", "before": "Which one? [/] :a: C:\\tmp\\", "options": ["a", "b"]},
        {"event": "step.end", "step": 2, "elapsed": 0.4},
        {"event": "session.end", "total_elapsed": 1.0, "total_steps": 2},
    ]

    tui.consume_events(iter(events))

    lines = [line.rstrip() for line in screen()]
    for expected in [
        "  ◆ Step 1  Grep, Read",
        "    │ / '[a-z_]+\\('  3 matches",
        "    │ → docs/[/]/[bold]notes.md  12 lines",
        "  ▣ local[/]:a: · 0.2s",
        "  ◆ Step 2  after [/]mystery",
        f"{CALL}│ Task  [explore] agent",
        f'{STATUS}"look for [/] in [bold]"',
        f"{STATUS}✓ [explore] · 2 steps · 1.5s",
        "  ✗ Backend error [500]: unexpected [/] in input C:\\",
        "  Which one? [/] :a: C:\\tmp\\",
        "  ▣ local[/]:a: · 0.4s",
    ]:
        assert expected in lines, (expected, lines)


def test_a_choice_menu_prints_the_options_as_written(screen, monkeypatch):
    monkeypatch.setattr(tui, "pt_prompt", lambda *args, **kwargs: "2")

    chosen = tui._render_choices(["Keep [bold]x[/bold] (Recommended)", "Drop [/] C:\\tmp\\"])

    assert chosen == "Drop [/] C:\\tmp\\"
    lines = [line.rstrip() for line in screen()]
    assert "  ● 1. Keep [bold]x[/bold]  recommended" in lines
    assert "    2. Drop [/] C:\\tmp\\" in lines
    assert "  ✓ Drop [/] C:\\tmp\\" in lines


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


def test_a_turn_prints_a_pattern_and_command_output_as_written(tmp_path, screen):
    (tmp_path / "app.py").write_text("def load_config(path):\n    return open_file(path)\n", encoding="utf-8")
    # Output with markup-like text, on stdout and stderr, and a failing exit.
    script = tmp_path / "report.py"
    script.write_text(
        "import sys\n"
        "print('[/] [bold]x[/bold] \\\\[0-9] :a: C:\\\\tmp\\\\')\n"
        "sys.stdout.flush()\n"
        "sys.stderr.write('Error [E1]: unexpected [/] in input\\n')\n"
        "sys.exit(3)\n",
        encoding="utf-8",
    )
    model = "local[/]:a:"  # a model name reaches the step lines too
    backend = StreamingBackend(scripts=[
        [tool_call("grep", {"pattern": "[a-z_]+\\(", "path": "."}, call_id="c1"), done(model=model)],
        [tool_call("bash", {"command": f'"{sys.executable}" "{script}"'}, call_id="c2"), done(model=model)],
        [text_delta("The report failed."), done(model=model)],
    ])
    session = _session(tmp_path, backend)

    tui.run_embedded(session, "find the loaders, then run the report")

    told = [entry["content"] for entry in session.conversation_history if entry.get("role") == "tool_result"]
    assert "Error [E1]: unexpected [/] in input" in told[1]
    lines = [line.rstrip() for line in screen()]
    # The collapsed search, then its step's line with the model's name.
    search = next(i for i, line in enumerate(lines) if "'[a-z_]+\\('" in line)
    assert lines[search].startswith("    │ / '[a-z_]+\\('  ") and lines[search].endswith(" matches")
    assert lines[search + 1].startswith(f"  ▣ {model} · ")
    # The command's output, as it printed.
    output = lines.index(f"{STATUS}[/] [bold]x[/bold] \\[0-9] :a: C:\\tmp\\")
    assert lines[output + 2] == f"{STATUS}Error [E1]: unexpected [/] in input"
    assert lines[output + 4] == f"{STATUS}(exit code: 3)"
    assert lines[output + 5].startswith(STATUS) and lines[output + 5].endswith("s · exit 3")
    assert lines[output + 6].startswith(f"  ▣ {model} · ")


def test_the_approval_prompt_names_the_tool_as_written(tmp_path, screen):
    # An MCP server names its tools. The prompt is prompt_toolkit's HTML, which
    # is XML: unescaped, "&" or "<" raised ExpatError.
    name = "mcp__r&d__search<beta>"
    backend = StreamingBackend(scripts=[
        [tool_call(name, {"query": "release notes"}, call_id="c1"), done()],
        [text_delta("Not searched."), done()],
    ])
    session = _session(tmp_path, backend)
    session.auto_approve = False
    asked = io.StringIO()
    prompt_output = Vt100_Output(asked, lambda: Size(rows=24, columns=WIDTH), term="xterm")

    with create_pipe_input() as keys, create_app_session(input=keys, output=prompt_output):
        keys.send_text("n\r")
        tui.run_embedded(session, "search the docs")

    assert f"Allow {name}? [Y/n]" in asked.getvalue()
    assert f"{STATUS}✗ denied" in screen()


# ── main()'s own lines and prompts ─────────────────────────────────────

@pytest.mark.parametrize("text, shown", [
    ("R&D", "R&D"),  # ExpatError unescaped
    ("a<b>c", "a<b>c"),
    ("say \"hi\" & 'bye' {0}", "say \"hi\" & 'bye' {0}"),
    ("tab\there", "tab\there"),
    # What XML can't hold even escaped: a control character, a byte of a POSIX
    # file name that isn't UTF-8 (a lone surrogate to Python), U+FFFE.
    ("bell\x07", "bell\ufffd"),
    ("r\udce9sum\udce9", "r\ufffdsum\ufffd"),
    ("\ufffe", "\ufffd"),
])
def test_a_prompt_shows_outside_text_as_written(text, shown):
    prompt = HTML(f'<style fg="#5f87ff"><b>{tui._html_esc(text)}</b></style> ❯ ')

    assert fragment_list_to_text(to_formatted_text(prompt)) == f"{shown} ❯ "


MODELS = ["llama3.1:8b", "local[/]:a:", "qwen3:[bold]"]  # as an Ollama server might list them
OLLAMA = {"url": "http://ollama.test:11434", "models": MODELS}


@pytest.fixture
def wide_screen(monkeypatch):
    """`screen`, wide enough for a temporary folder's path; lines come right-stripped."""
    out = io.StringIO()
    monkeypatch.setattr(tui, "console", Console(
        file=out, width=400, color_system=None, force_terminal=False, legacy_windows=False,
    ))
    return lambda: [line.rstrip() for line in out.getvalue().splitlines()]


class LocalOllama(StreamingBackend):
    """What main() gets from create_backend("ollama", ...), with no server behind it."""

    def __init__(self, base_url: str, model: str, *, name: str = "ollama"):
        super().__init__(name=name, model=model)
        self.base_url = base_url

    def health(self) -> dict:
        return {"status": "ready", "backend": self.name, "model": self.model, "available_models": list(MODELS)}

    def warm_up(self):
        pass

    def list_models(self) -> list:
        return list(MODELS)


def _no_settings():
    # main() hands Settings to the audit log and the GitHub tools when it can
    # load them. Unloaded, that process-wide state can't reach other tests.
    raise RuntimeError("no Settings in this test")


def _run_main(monkeypatch, tmp_path, argv: list[str], keys: str = "", *,
              detected=({"ollama": OLLAMA},), name: str = "ollama") -> str:
    """
    Run the TUI's main() with `argv`, typing `keys` at its prompts through
    prompt_toolkit. Each scan for backends finds the next of `detected`, the
    last one repeating, instead of probing a server; backends are named
    `name`. Returns what the prompts showed.
    """
    monkeypatch.setattr(sys, "argv", ["lumi", *argv])
    for variable in ("OLLAMA_HOST", "LUMI_DEFAULT_BACKEND", "LUMI_DEFAULT_MODEL", "LUMI_STATE_HOME"):
        monkeypatch.delenv(variable, raising=False)
    scans = iter(detected)
    monkeypatch.setattr(tui, "_detect_backends", lambda *args: next(scans, detected[-1]))
    monkeypatch.setattr(tui, "create_backend", lambda kind, url, model=None: LocalOllama(url, model, name=name))
    monkeypatch.setattr(tui, "OllamaBackend", LocalOllama)  # so /model lists and switches models
    monkeypatch.setattr("lumi.gui.settings.SettingsManager", _no_settings)
    monkeypatch.setattr(tui, "_history_path", lambda: tmp_path / "tui_history")
    shown = io.StringIO()
    output = Vt100_Output(shown, lambda: Size(rows=24, columns=WIDTH), term="xterm")
    with create_pipe_input() as pipe, create_app_session(input=pipe, output=output):
        pipe.send_text(keys)
        pipe.close()  # a prompt past the typed lines ends, as at Ctrl+D
        tui.main()
    return shown.getvalue()


def _assert_in_order(lines: list[str], expected: list[str]):
    """Each of `expected` is one of `lines`, in this order."""
    at = 0
    for line in expected:
        assert line in lines[at:], (line, lines[at:])
        at = lines.index(line, at) + 1


def test_main_prints_folders_model_names_and_typed_commands_as_written(tmp_path, wide_screen, monkeypatch):
    folder = tmp_path / "[old]" / "R&D"
    folder.mkdir(parents=True)
    monkeypatch.chdir(folder)
    with pytest.raises(OSError) as refused:
        os.chdir("[/]")  # what "/cd [/]" gets: the error repeats the path
    typed = [
        "2",  # the model picker: --model isn't on the server
        "/cd [/]", "/cd ..", "/cd R&D", "/cd", "/[/]", "/status",
        "/model", "qwen3:[bold]",  # a name typed instead of its number
        "/model", "3", "/help", "/backend", "/quit",
    ]

    shown = _run_main(monkeypatch, tmp_path, ["--model", "missing[/]"], "".join(f"{line}\r" for line in typed))

    lines = wide_screen()
    _assert_in_order(lines, [
        "  ✗ Model 'missing[/]' not found",
        "    1. llama3.1:8b",
        "    2. local[/]:a:",
        "    3. qwen3:[bold]",
        "  ✓ local[/]:a:",
        "  ⋯ Warming up local[/]:a:",
        "  ● backend  Ollama  ·  local[/]:a:",
        f"    cwd      {folder.as_posix()}",
        f"  ✗ {refused.value}",
        f"  → {folder.parent}",  # on Windows, a backslash before "[old]"
        f"  → {folder}",
        f"  {folder}",
        "  Unknown: /[/] · try /help",
        "  ● 2. local[/]:a:  (current)",
        "  ✓ qwen3:[bold]",
        "  ⋯ Warming up qwen3:[bold]",
        "  ✓ Switched to qwen3:[bold] · conversation cleared",
        "  ● 3. qwen3:[bold]  (current)",
        "  ✓ qwen3:[bold]",
        "  Keeping qwen3:[bold]",
        "  ┃ Lumi Code Agent  ollama · qwen3:[bold]",
        "  Already using ollama — no other backend available",
        "  Goodbye",
    ])
    # /status: the backend's own words, in Text cells rather than markup.
    rows = [[cell.strip() for cell in line.split("│")[1:-1]] for line in lines if line.startswith("│")]
    assert rows == [
        ["status", "ready"], ["backend", "ollama"], ["model", "local[/]:a:"],
        ["available_models", "llama3.1:8b, local[/]:a:, qwen3:[bold]"],
    ]
    # The prompt names the folder: "R&D", then "[old]" after "/cd ..".
    prompts = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", shown)
    assert "R&D ❯ /cd [/]" in prompts and "[old] ❯ /cd R&D" in prompts


def test_an_unreachable_ollama_is_named_as_written(tmp_path, wide_screen, monkeypatch):
    monkeypatch.chdir(tmp_path)

    # An IPv6 address: "[fd00::131]" read as a style, and vanished.
    _run_main(monkeypatch, tmp_path, ["--ollama-url", "http://[fd00::131]:11434"], detected=({},))

    _assert_in_order(wide_screen(), ["  ✗ Ollama not reachable", "    Checked: http://[fd00::131]:11434"])


def test_a_backends_name_prints_as_written(tmp_path, wide_screen, monkeypatch):
    # Backends name themselves ("ollama"); these lines print any name as written.
    monkeypatch.chdir(tmp_path)

    _run_main(monkeypatch, tmp_path, ["--model", "llama3.1:8b"], "/help\r/backend\r/quit\r",
              detected=({"ollama": OLLAMA}, {"[/]local": OLLAMA}), name="[/]local")

    _assert_in_order(wide_screen(), [
        "  ● backend  [/]local  ·  llama3.1:8b",
        "  ┃ Lumi Code Agent  [/]local · llama3.1:8b",
        "  Already using [/]local — no other backend available",
    ])


def test_a_fallback_backend_is_named_as_written(tmp_path, wide_screen, monkeypatch):
    # `--backend ollama` when the scan found only another backend (today's scan
    # finds Ollama or nothing).
    monkeypatch.chdir(tmp_path)

    _run_main(monkeypatch, tmp_path, ["--backend", "ollama"], "/quit\r", detected=({"[/]local": OLLAMA},))

    _assert_in_order(wide_screen(), ["  ✗ Backend 'ollama' not available", "  → Falling back to [/]local"])


class _EngineSocket:
    """A remote engine that reports its status and then only listens."""

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False

    async def recv(self) -> str:
        return json.dumps({"event": "status", "model": "local[/]:a:"})

    async def send(self, message: str):
        pass


def test_the_remote_prompt_names_the_folder_as_written(tmp_path, screen, monkeypatch):
    # run_remote has had no command since v0.4.4, and prompt_toolkit's blocking
    # prompt can't run inside its event loop, so this checks the prompt it builds.
    folder = tmp_path / "R&D"
    folder.mkdir()
    monkeypatch.chdir(folder)
    monkeypatch.setattr(tui, "_history_path", lambda: tmp_path / "tui_history")
    monkeypatch.setitem(sys.modules, "websockets", types.SimpleNamespace(connect=lambda url: _EngineSocket()))
    prompts = []

    def type_quit(message, **kwargs):
        prompts.append(fragment_list_to_text(to_formatted_text(message)))
        return "/quit"

    monkeypatch.setattr(tui, "pt_prompt", type_quit)

    tui.run_remote("ws://engine.test/[/]")

    assert prompts == ["R&D ❯ "]
    assert "  ✓ Connected to ws://engine.test/[/]" in screen()
