"""
Resonant Code Agent - Agentic Coding TUI

A Claude Code-like terminal interface powered by the Resonant Cognitive Engine.
Supports:
  - File creation, editing, reading
  - Shell command execution
  - Multi-turn agentic loops (model calls tool -> execute -> feed back -> continue)
  - Live streaming token display with thinking spinner
  - Cognitive state display (energy, coherence, clusters)
  - Conversation history with full context
  - Tool approval/auto-approve modes
  - Rich file diff previews

Usage:
  resonant                                        # Auto-connect to localhost:8000
  resonant --api http://10.0.0.133:8000           # Connect to engine on LAN
  python -m resonant_client --api http://host:8000
"""

import json
import os
import re
import sys
import time
import subprocess
import argparse
import logging
import difflib
from pathlib import Path
from typing import Optional, Iterator, Tuple

from rich.console import Console
from rich.panel import Panel
from rich.markdown import Markdown
from rich.syntax import Syntax
from rich.table import Table
from rich.text import Text
from rich.rule import Rule
from rich.live import Live
from rich.spinner import Spinner
from rich.columns import Columns
from prompt_toolkit import prompt as pt_prompt
from prompt_toolkit.history import FileHistory
from prompt_toolkit.formatted_text import HTML
from prompt_toolkit.key_binding import KeyBindings

logger = logging.getLogger(__name__)

# Force UTF-8 output on Windows
import io
if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

console = Console(force_terminal=True)

# Color scheme
C_BRAND = "bright_cyan"
C_DIM = "dim"
C_OK = "green"
C_WARN = "yellow"
C_ERR = "red"
C_TOOL = "bright_magenta"

# ---------------------------------------------------------------------------
# Built-in tools the agent can use
# ---------------------------------------------------------------------------

AGENT_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "bash",
            "description": "Execute a shell command and return its output. Use for running scripts, installing packages, git operations, etc. Commands run non-interactively (no stdin) with a timeout. Do NOT run interactive programs (games, REPLs, servers) - they will timeout. For testing interactive programs, just verify the file was created correctly.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "The shell command to execute"
                    },
                    "timeout": {
                        "type": "integer",
                        "description": "Timeout in seconds (default: 30). Increase for long-running builds."
                    }
                },
                "required": ["command"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "file_write",
            "description": "Create or overwrite a file with the given content.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "File path (relative to working directory)"
                    },
                    "content": {
                        "type": "string",
                        "description": "The complete file content to write"
                    }
                },
                "required": ["path", "content"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "file_read",
            "description": "Read the contents of a file.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "File path to read"
                    }
                },
                "required": ["path"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "file_edit",
            "description": "Edit a file by replacing old_text with new_text. The old_text must match exactly.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "File path to edit"
                    },
                    "old_text": {
                        "type": "string",
                        "description": "The exact text to find and replace"
                    },
                    "new_text": {
                        "type": "string",
                        "description": "The replacement text"
                    }
                },
                "required": ["path", "old_text", "new_text"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "glob",
            "description": "Find files matching a glob pattern.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {
                        "type": "string",
                        "description": "Glob pattern like '**/*.py' or 'src/*.ts'"
                    },
                    "path": {
                        "type": "string",
                        "description": "Base directory to search from (default: current directory)"
                    }
                },
                "required": ["pattern"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "grep",
            "description": "Search for a regex pattern in files.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {
                        "type": "string",
                        "description": "Regex pattern to search for"
                    },
                    "path": {
                        "type": "string",
                        "description": "File or directory to search in (default: current directory)"
                    },
                    "glob": {
                        "type": "string",
                        "description": "File glob filter like '*.py'"
                    }
                },
                "required": ["pattern"]
            }
        }
    },
]


# ---------------------------------------------------------------------------
# Language detection for syntax highlighting
# ---------------------------------------------------------------------------

LANG_MAP = {
    "py": "python", "js": "javascript", "ts": "typescript",
    "jsx": "jsx", "tsx": "tsx", "rs": "rust", "go": "go",
    "java": "java", "cpp": "cpp", "c": "c", "h": "c",
    "html": "html", "css": "css", "json": "json", "yaml": "yaml",
    "yml": "yaml", "toml": "toml", "md": "markdown", "sh": "bash",
    "sql": "sql", "rb": "ruby", "php": "php",
}


def _detect_lang(path: str) -> str:
    ext = Path(path).suffix.lstrip(".")
    return LANG_MAP.get(ext, "text")


# ---------------------------------------------------------------------------
# Tool display — Claude Code style
# ---------------------------------------------------------------------------

def _show_tool_header(icon: str, label: str, summary: str, style: str = C_TOOL):
    """Compact one-line tool header."""
    console.print(f"  [{style}]{icon}[/{style}] [{style} bold]{label}[/{style} bold]  [dim]{summary}[/dim]")


def _show_file_write(path: str, content: str):
    """Show a file creation with syntax-highlighted preview."""
    lines = content.split("\n")
    line_count = len(lines)
    char_count = len(content)
    lang = _detect_lang(path)

    _show_tool_header("+", "CREATE", f"{path} ({line_count} lines)")

    # Preview first 15 lines
    preview = "\n".join(lines[:15])
    if line_count > 15:
        preview += f"\n# ... ({line_count - 15} more lines)"

    console.print(Panel(
        Syntax(preview, lang, theme="monokai", line_numbers=True),
        border_style="dim green",
        padding=(0, 1),
    ))


def _show_file_edit(path: str, old_text: str, new_text: str):
    """Show a file edit with diff preview."""
    old_lines = old_text.split("\n")
    new_lines = new_text.split("\n")
    diff = list(difflib.unified_diff(old_lines, new_lines, lineterm="", n=2))

    _show_tool_header("~", "EDIT", path)

    if diff:
        colored_lines = []
        for line in diff[2:20]:  # Skip headers, max 18 diff lines
            if line.startswith("+"):
                colored_lines.append(f"[green]{line}[/green]")
            elif line.startswith("-"):
                colored_lines.append(f"[red]{line}[/red]")
            elif line.startswith("@@"):
                colored_lines.append(f"[cyan]{line}[/cyan]")
            else:
                colored_lines.append(f"[dim]{line}[/dim]")
        if len(diff) > 20:
            colored_lines.append(f"[dim]... ({len(diff) - 20} more diff lines)[/dim]")

        console.print(Panel(
            "\n".join(colored_lines),
            border_style="dim yellow",
            padding=(0, 1),
        ))
    else:
        console.print("    [dim](no visible diff)[/dim]")


def _show_bash(command: str):
    """Show a bash command."""
    # Truncate very long commands
    display_cmd = command if len(command) < 120 else command[:117] + "..."
    _show_tool_header(">_", "BASH", f"$ {display_cmd}")


def _show_tool_result(result: str, is_error: bool = False):
    """Show tool result compactly."""
    lines = result.split("\n")
    if len(lines) > 12:
        display = "\n".join(lines[:10]) + f"\n[dim]... ({len(lines) - 10} more lines)[/dim]"
    else:
        display = result

    style = C_ERR if is_error else C_DIM
    for line in display.split("\n"):
        console.print(f"    [{style}]{line}[/{style}]")


def _show_result_status(message: str, ok: bool = True):
    """Show a compact result status line."""
    icon = "+" if ok else "x"
    style = C_OK if ok else C_ERR
    console.print(f"    [{style}]{icon} {message}[/{style}]")


# ---------------------------------------------------------------------------
# Tool execution
# ---------------------------------------------------------------------------

def execute_tool(name: str, arguments: dict, auto_approve: bool = True) -> str:
    """Execute a tool and return the result as a string."""
    try:
        if name == "bash":
            cmd = arguments.get("command", "")
            _show_bash(cmd)

            if not auto_approve:
                try:
                    answer = pt_prompt(HTML('<style fg="yellow">    Allow? [Y/n] </style>'))
                    if answer.strip().lower() in ("n", "no"):
                        return "Tool execution denied by user."
                except (EOFError, KeyboardInterrupt):
                    return "Tool execution cancelled."

            timeout = arguments.get("timeout", 30)
            timed_out = False
            cmd_start = time.time()
            try:
                result = subprocess.run(
                    cmd, shell=True, capture_output=True, text=True,
                    timeout=timeout, cwd=os.getcwd(),
                    stdin=subprocess.DEVNULL,
                )
                elapsed = time.time() - cmd_start
                output = result.stdout
                if result.stderr:
                    output += ("\n" if output else "") + result.stderr
                if result.returncode != 0:
                    output += f"\n(exit code: {result.returncode})"
                output = output.strip() or "(no output)"
                is_err = result.returncode != 0
            except subprocess.TimeoutExpired:
                elapsed = timeout
                output = f"Command timed out after {timeout}s."
                is_err = True
                timed_out = True
            _show_tool_result(output, is_error=is_err)
            console.print(f"    [dim]{elapsed:.1f}s{' (timeout)' if timed_out else ''}[/dim]")
            return output

        elif name == "file_write":
            fpath = arguments.get("path", "")
            content = arguments.get("content", "")
            _show_file_write(fpath, content)

            if not auto_approve:
                try:
                    answer = pt_prompt(HTML('<style fg="yellow">    Allow? [Y/n] </style>'))
                    if answer.strip().lower() in ("n", "no"):
                        return "Tool execution denied by user."
                except (EOFError, KeyboardInterrupt):
                    return "Tool execution cancelled."

            path = Path(fpath)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
            lines = len(content.split("\n"))
            _show_result_status(f"Written {fpath} ({lines} lines, {len(content)} chars)")
            return f"File written: {fpath} ({lines} lines, {len(content)} characters)"

        elif name == "file_read":
            fpath = arguments.get("path", "")
            path = Path(fpath)
            if not path.exists():
                _show_tool_header("?", "READ", fpath)
                _show_result_status(f"File not found: {fpath}", ok=False)
                return f"Error: File not found: {fpath}"
            content = path.read_text(encoding="utf-8")
            lines = len(content.split("\n"))
            _show_tool_header("?", "READ", f"{fpath} ({lines} lines)")
            if len(content) > 10000:
                content = content[:10000] + f"\n\n... (truncated, {len(content)} total chars)"
            return content

        elif name == "file_edit":
            fpath = arguments.get("path", "")
            old_text = arguments.get("old_text", "")
            new_text = arguments.get("new_text", "")
            path = Path(fpath)
            if not path.exists():
                _show_tool_header("~", "EDIT", fpath)
                _show_result_status(f"File not found: {fpath}", ok=False)
                return f"Error: File not found: {fpath}"
            content = path.read_text(encoding="utf-8")
            if old_text not in content:
                _show_tool_header("~", "EDIT", fpath)
                _show_result_status(f"old_text not found in {fpath}", ok=False)
                return f"Error: old_text not found in {fpath}. The text to replace was not found."

            _show_file_edit(fpath, old_text, new_text)

            if not auto_approve:
                try:
                    answer = pt_prompt(HTML('<style fg="yellow">    Allow? [Y/n] </style>'))
                    if answer.strip().lower() in ("n", "no"):
                        return "Tool execution denied by user."
                except (EOFError, KeyboardInterrupt):
                    return "Tool execution cancelled."

            content = content.replace(old_text, new_text, 1)
            path.write_text(content, encoding="utf-8")
            _show_result_status(f"Edited {fpath}")
            return f"File edited: {fpath}"

        elif name == "glob":
            pattern = arguments.get("pattern", "")
            base = arguments.get("path", ".")
            _show_tool_header("*", "GLOB", f"{pattern} in {base}")
            matches = sorted(Path(base).glob(pattern))[:50]
            result = "\n".join(str(m) for m in matches)
            console.print(f"    [dim]{len(matches)} files found[/dim]")
            return result or "(no matches)"

        elif name == "grep":
            pattern = arguments.get("pattern", "")
            path = arguments.get("path", ".")
            file_glob = arguments.get("glob", "")
            _show_tool_header("/", "GREP", f"'{pattern}' in {path}")

            if sys.platform == "win32":
                cmd = f'findstr /s /n /r "{pattern}" "{path}\\*"'
                if file_glob:
                    cmd = f'findstr /s /n /r "{pattern}" "{path}\\{file_glob}"'
            else:
                cmd = f'grep -rn "{pattern}" "{path}"'
                if file_glob:
                    cmd = f'grep -rn --include="{file_glob}" "{pattern}" "{path}"'

            result = subprocess.run(
                cmd, shell=True, capture_output=True, text=True,
                timeout=30, cwd=os.getcwd()
            )
            output = result.stdout.strip()
            lines = output.split("\n") if output else []
            count = len(lines)
            if count > 30:
                output = "\n".join(lines[:30]) + f"\n... ({count} total matches)"
            console.print(f"    [dim]{count} matches[/dim]")
            return output or "(no matches)"

        else:
            return f"Error: Unknown tool '{name}'"

    except subprocess.TimeoutExpired:
        _show_result_status("Command timed out (120s)", ok=False)
        return "Error: Command timed out (120s limit)"
    except Exception as e:
        _show_result_status(str(e), ok=False)
        return f"Error: {e}"


# ---------------------------------------------------------------------------
# SSE streaming — parse Server-Sent Events from the API
# ---------------------------------------------------------------------------

def stream_engine_api(
    base_url: str,
    user_msg: str,
    conversation_history: list,
    instructions: str,
    tools: list,
    max_tokens: int = 4096,
) -> Iterator[Tuple[str, dict]]:
    """
    Stream events from the Resonant Engine /v1/responses endpoint.

    Yields (event_type, data_dict) tuples:
      - ("response.created", {...})
      - ("response.output_text.delta", {"delta": "..."})
      - ("response.output_item.done", {"item": {...}})
      - ("response.completed", {"response": {...}})
    """
    import httpx

    # Build input array
    inp = []
    for turn in conversation_history:
        role = turn["role"]
        content = turn["content"]
        if role == "tool_result":
            inp.append({
                "type": "function_call_output",
                "call_id": turn.get("call_id", ""),
                "output": content,
            })
        elif role == "tool_call":
            inp.append({
                "type": "function_call",
                "name": turn.get("name", ""),
                "arguments": turn.get("arguments", "{}"),
                "call_id": turn.get("call_id", ""),
            })
        else:
            inp.append({"role": role, "content": content})

    # Add current user message
    inp.append({"role": "user", "content": [{"type": "input_text", "text": user_msg}]})

    payload = {
        "model": "resonant-engine",
        "input": inp,
        "instructions": instructions,
        "tools": tools,
        "stream": True,
        "max_output_tokens": max_tokens,
    }

    with httpx.Client(timeout=httpx.Timeout(180.0, connect=10.0)) as client:
        with client.stream("POST", f"{base_url}/v1/responses", json=payload) as resp:
            resp.raise_for_status()
            event_type = None
            for line in resp.iter_lines():
                line = line.strip()
                if not line:
                    continue
                if line.startswith("event: "):
                    event_type = line[7:]
                elif line.startswith("data: ") and event_type:
                    try:
                        data = json.loads(line[6:])
                        yield (event_type, data)
                    except json.JSONDecodeError:
                        pass
                    event_type = None


def call_engine_api_blocking(
    base_url: str,
    user_msg: str,
    conversation_history: list,
    instructions: str,
    tools: list,
    max_tokens: int = 4096,
) -> dict:
    """Fallback: non-streaming API call."""
    import httpx

    inp = []
    for turn in conversation_history:
        role = turn["role"]
        content = turn["content"]
        if role == "tool_result":
            inp.append({"type": "function_call_output", "call_id": turn.get("call_id", ""), "output": content})
        elif role == "tool_call":
            inp.append({"type": "function_call", "name": turn.get("name", ""), "arguments": turn.get("arguments", "{}"), "call_id": turn.get("call_id", "")})
        else:
            inp.append({"role": role, "content": content})

    inp.append({"role": "user", "content": [{"type": "input_text", "text": user_msg}]})

    payload = {
        "model": "resonant-engine",
        "input": inp,
        "instructions": instructions,
        "tools": tools,
        "stream": False,
        "max_output_tokens": max_tokens,
    }

    with httpx.Client(timeout=httpx.Timeout(180.0, connect=10.0)) as client:
        resp = client.post(f"{base_url}/v1/responses", json=payload)
        resp.raise_for_status()
        return resp.json()



# ---------------------------------------------------------------------------
# Status bar
# ---------------------------------------------------------------------------

def _status_line(cog: dict, elapsed: float, step: int) -> str:
    """Build a compact status line."""
    parts = []
    if cog:
        energy = cog.get("energy", 0)
        coherence = cog.get("coherence", 0)
        mode = cog.get("processing_mode", "")
        clusters = cog.get("clusters", 0)
        parts.append(f"E:{energy:.0%}")
        parts.append(f"C:{coherence:.0%}")
        if clusters:
            parts.append(f"{clusters} clusters")
        if mode:
            parts.append(mode)
    if elapsed > 0:
        parts.append(f"{elapsed:.1f}s")
    return " [dim]|[/dim] ".join(f"[dim]{p}[/dim]" for p in parts)


def _strip_tool_call_tags(text: str) -> str:
    """Remove <tool_call>...</tool_call> blocks from display text."""
    return re.sub(r'<tool_call>.*?</tool_call>', '', text, flags=re.DOTALL).strip()


def _parse_choices(text: str) -> tuple:
    """
    Parse <choices> blocks from model output.

    Format the model emits:
        <choices>
        * Option one (Recommended)
        * Option two
        * Option three
        </choices>

    Returns (text_before, list_of_options, text_after) or (text, None, None) if no choices.
    """
    match = re.search(r'<choices>\s*(.*?)\s*</choices>', text, flags=re.DOTALL)
    if not match:
        return (text, None, None)

    before = text[:match.start()].strip()
    after = text[match.end():].strip()
    block = match.group(1)

    options = []
    for line in block.strip().split("\n"):
        line = line.strip()
        if line.startswith("* ") or line.startswith("- "):
            options.append(line[2:].strip())
        elif line:
            options.append(line)

    if not options:
        return (text, None, None)

    return (before, options, after)


def _render_choices(options: list) -> str:
    """
    Render choices as a numbered menu and prompt the user to pick one.
    Returns the selected option text.
    """
    console.print()
    for i, opt in enumerate(options):
        marker = f"[{C_BRAND} bold]>[/{C_BRAND} bold]" if i == 0 else " "
        num_style = C_BRAND if i == 0 else "white"
        rec = f" [{C_BRAND}](recommended)[/{C_BRAND}]" if "(Recommended)" in opt or "(recommended)" in opt else ""
        label = opt.replace("(Recommended)", "").replace("(recommended)", "").strip()
        console.print(f"  {marker} [{num_style}]{i + 1}.[/{num_style}] {label}{rec}")

    console.print(f"    [{C_DIM}]{len(options) + 1}. Other (type your own)[/{C_DIM}]")
    console.print()

    try:
        answer = pt_prompt(
            HTML(f'<style fg="ansibrightcyan">  Choice [1-{len(options) + 1}]: </style>'),
        ).strip()
    except (EOFError, KeyboardInterrupt):
        return options[0]  # Default to first

    # Parse answer
    try:
        idx = int(answer) - 1
        if 0 <= idx < len(options):
            selected = options[idx].replace("(Recommended)", "").replace("(recommended)", "").strip()
            console.print(f"  [{C_OK}]Selected: {selected}[/{C_OK}]")
            return selected
        elif idx == len(options):
            # "Other" option
            custom = pt_prompt(HTML('<style fg="ansiwhite">  Your choice: </style>')).strip()
            if custom:
                console.print(f"  [{C_OK}]Selected: {custom}[/{C_OK}]")
                return custom
            return options[0]
    except ValueError:
        # They typed text instead of a number — use it as their answer
        if answer:
            console.print(f"  [{C_OK}]Selected: {answer}[/{C_OK}]")
            return answer

    return options[0]


# ---------------------------------------------------------------------------
# Main TUI
# ---------------------------------------------------------------------------

def _get_system_instructions() -> str:
    """Build system instructions with platform-specific hints."""
    import platform as plat
    if sys.platform == "win32":
        platform_name = f"Windows ({plat.release()})"
        platform_hints = (
            "Use 'python' not 'python3'. Use 'pip' not 'pip3'. "
            "Do NOT use 'source' for venv - use 'venv\\\\Scripts\\\\activate'. "
            "Use PowerShell-compatible commands."
        )
    else:
        platform_name = f"Linux/macOS ({plat.system()})"
        platform_hints = "Use 'python3' and 'pip3'. Use 'source venv/bin/activate'."

    return f"""You are the Resonant Code Agent - an expert AI coding assistant powered by the Resonant Cognitive Engine with 60,000+ knowledge patterns.

You have these tools: bash, file_write, file_read, file_edit, glob, grep.

## Workflow — ALWAYS follow this sequence:

### Phase 1: Understand
Before writing ANY code, assess whether the request is clear enough to act on.
- If the request is ambiguous, ask 1-3 SHORT clarifying questions.
- For questions with discrete options, use the <choices> tag so the user gets a nice menu:

Example:
  What kind of app should this be?
  <choices>
  * Web app with Flask (Recommended)
  * CLI app
  * Desktop app with Tkinter
  </choices>

Put your recommended option FIRST with "(Recommended)" after it. The user can also type their own answer.
- If the request is clear and simple (e.g. "fix this bug", "add a docstring"), skip to Phase 3.
- Do NOT ask questions that have obvious answers or that you can decide yourself.

### Phase 2: Plan
For any task that involves creating or modifying more than one file, present a brief plan BEFORE coding:
- Use a numbered list of steps you'll take
- Mention the files you'll create or modify
- Keep it concise — 3-8 bullet points, not an essay
- End with: "Ready to proceed?" or similar
- Wait for user confirmation before executing tools
- For trivial single-file tasks, skip the plan and just do it.

### Phase 3: Execute
Now use tools to implement:
1. When asked to create something, USE TOOLS. Do not just show code in text.
2. When asked to run something, use bash.
3. When editing, prefer file_edit over file_write (preserves unchanged code).
4. If file_edit fails (old_text not found), read the file first with file_read, then retry.
5. After creating files, verify with file_read or bash.
6. Be concise in explanations. Let the code speak.
7. If a command fails, diagnose and fix it.

## Rules:
- PLATFORM: {platform_name}. {platform_hints}
- When creating applications, make them COMPLETE and functional, not stubs. Include real logic, error handling, and good UX.
- Use one tool call at a time. Do not combine multiple tool calls in one response.
- The bash tool runs NON-INTERACTIVELY (no stdin). Do NOT run interactive programs (games, REPLs, servers) — they will timeout. To test them, verify the file was created correctly.
- When you finish a task, give a brief summary of what was done and any next steps.
"""


def print_banner(engine_info: dict = None):
    """Print the startup banner."""
    patterns = engine_info.get('memory_patterns', 0) if engine_info else 0
    energy = engine_info.get('energy', 0) if engine_info else 0

    status_parts = []
    if patterns:
        status_parts.append(f"[{C_BRAND}]{patterns:,}[/{C_BRAND}] patterns")
    if energy:
        status_parts.append(f"[{C_OK}]{energy:.0%}[/{C_OK}] energy")
    status_parts.append(f"[dim]{os.getcwd()}[/dim]")

    status_line = "  |  ".join(status_parts)

    console.print()
    console.print(Panel(
        Text.from_markup(
            f"[bold {C_BRAND}]  Resonant Code Agent[/bold {C_BRAND}]\n"
            f"[dim]  Agentic coding powered by oscillatory intelligence[/dim]\n\n"
            f"  {status_line}\n\n"
            f"  [dim]Commands: /help /cd /clear /status /approve /quit[/dim]"
        ),
        border_style=C_BRAND,
        padding=(0, 1),
    ))
    console.print()


def run_agent_loop_streaming(
    base_url: str,
    user_msg: str,
    conversation_history: list,
    max_iterations: int = 10,
    max_tokens: int = 4096,
    auto_approve: bool = True,
):
    """
    Streaming agentic loop for API mode.
    Uses SSE to stream tokens live to the console.
    """
    iteration = 0
    current_msg = user_msg
    total_start = time.time()

    while iteration < max_iterations:
        iteration += 1

        # Step header
        if iteration == 1:
            console.print()
            console.print(Rule(f"[bold {C_BRAND}]Agent[/bold {C_BRAND}]", style=C_BRAND))
        else:
            console.print(f"\n  [dim {C_BRAND}]--- step {iteration} ---[/dim {C_BRAND}]")

        step_start = time.time()

        # Show thinking spinner, then stream tokens
        collected_text = []
        tool_calls = []
        cog_state = None
        first_token = True

        try:
            with Live(
                Spinner("dots", text=f"[{C_BRAND}] Thinking...[/{C_BRAND}]", style=C_BRAND),
                console=console,
                refresh_per_second=12,
                transient=True,
            ) as live:
                for event_type, data in stream_engine_api(
                    base_url=base_url,
                    user_msg=current_msg,
                    conversation_history=conversation_history,
                    instructions=_get_system_instructions(),
                    tools=AGENT_TOOLS,
                    max_tokens=max_tokens,
                ):
                    if event_type == "response.output_text.delta":
                        delta = data.get("delta", "")
                        collected_text.append(delta)
                        # Update live display with accumulated text
                        display_text = "".join(collected_text)
                        live.update(Text(display_text, style="white"))
                        first_token = False

                    elif event_type == "response.output_item.done":
                        item = data.get("item", {})
                        if item.get("type") == "function_call":
                            tool_calls.append(item)

                    elif event_type == "response.completed":
                        resp = data.get("response", {})
                        # Extract cognitive state if available
                        cog_state = resp.get("cognitive_state")

        except KeyboardInterrupt:
            console.print(f"\n  [{C_WARN}]Interrupted.[/{C_WARN}]")
            break
        except Exception as e:
            console.print(f"\n  [{C_ERR}]Stream error: {e}[/{C_ERR}]")
            # Fallback to blocking call
            try:
                response = call_engine_api_blocking(
                    base_url=base_url,
                    user_msg=current_msg,
                    conversation_history=conversation_history,
                    instructions=_get_system_instructions(),
                    tools=AGENT_TOOLS,
                    max_tokens=max_tokens,
                )
                # Process like non-streaming
                for item in response.get("output", []):
                    if item.get("type") == "message":
                        for part in item.get("content", []):
                            if part.get("type") == "output_text":
                                collected_text.append(part.get("text", ""))
                    elif item.get("type") == "function_call":
                        tool_calls.append(item)
                cog_state = response.get("cognitive_state")
            except Exception as e2:
                console.print(f"  [{C_ERR}]Fallback also failed: {e2}[/{C_ERR}]")
                break

        step_elapsed = time.time() - step_start

        # Display the collected message text (if any)
        full_text = "".join(collected_text).strip()
        full_text = _strip_tool_call_tags(full_text)

        if full_text:
            # Check for <choices> blocks
            before, choices, after = _parse_choices(full_text)

            if choices:
                # Display text before choices
                if before:
                    console.print()
                    try:
                        console.print(Panel(Markdown(before), border_style="dim", padding=(0, 1)))
                    except Exception:
                        console.print(f"  {before}")

                # Render interactive choice menu
                selected = _render_choices(choices)

                # Display text after choices
                if after:
                    console.print(f"  [dim]{after}[/dim]")

                # Record in conversation and auto-reply with selection
                conversation_history.append({"role": "assistant", "content": full_text})
                conversation_history.append({"role": "user", "content": selected})
                # Continue the loop with the user's selection
                current_msg = selected
                tool_calls = []
                collected_text = []

                status = _status_line(cog_state, step_elapsed, iteration)
                if status:
                    console.print(f"  {status}")
                continue
            else:
                # Normal text display
                console.print()
                try:
                    console.print(Panel(Markdown(full_text), border_style="dim", padding=(0, 1)))
                except Exception:
                    console.print(f"  {full_text}")

            conversation_history.append({"role": "assistant", "content": full_text})

        # Execute any tool calls
        has_tool_calls = len(tool_calls) > 0
        for item in tool_calls:
            fn_name = item.get("name", "")
            fn_args_str = item.get("arguments", "{}")
            call_id = item.get("call_id", "")

            try:
                fn_args = json.loads(fn_args_str) if isinstance(fn_args_str, str) else fn_args_str
            except json.JSONDecodeError:
                fn_args = {}

            console.print()
            result = execute_tool(fn_name, fn_args, auto_approve=auto_approve)

            conversation_history.append({
                "role": "tool_call",
                "name": fn_name,
                "arguments": fn_args_str,
                "call_id": call_id,
                "content": f"Called {fn_name}",
            })
            conversation_history.append({
                "role": "tool_result",
                "call_id": call_id,
                "content": result,
            })

        # Status line
        status = _status_line(cog_state, step_elapsed, iteration)
        if status:
            console.print(f"  {status}")

        # Continue loop if there were tool calls
        if has_tool_calls:
            tool_calls = []
            collected_text = []
            current_msg = "Continue based on the tool results above."
            continue
        else:
            break

    total_elapsed = time.time() - total_start
    if iteration > 1:
        console.print(f"  [dim]Completed in {total_elapsed:.1f}s ({iteration} steps)[/dim]")

    if iteration >= max_iterations:
        console.print(f"\n  [{C_WARN}]Reached {max_iterations} steps. Use /clear to reset.[/{C_WARN}]")



def main():
    parser = argparse.ArgumentParser(
        description="Resonant Code Agent - Agentic Coding TUI",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s                                        # Auto-connect to localhost:8000
  %(prog)s --api http://10.0.0.133:8000           # Connect to engine on LAN
  %(prog)s --dir ~/projects/myapp                 # Set working directory
""",
    )
    parser.add_argument("--api", type=str, default=None,
                       help="Engine API URL (default: http://localhost:8000)")
    parser.add_argument("--dir", type=str, default=None,
                       help="Working directory")
    parser.add_argument("--max-tokens", type=int, default=4096,
                       help="Max output tokens per turn (default: 4096)")
    parser.add_argument("--max-steps", type=int, default=10,
                       help="Max agentic loop steps (default: 10)")
    parser.add_argument("--approve", action="store_true",
                       help="Require approval before executing tools")
    args = parser.parse_args()

    logging.basicConfig(level=logging.WARNING)

    if args.dir:
        os.chdir(args.dir)

    # Connect to engine API
    base_url = (args.api or os.environ.get("RESONANT_API", "http://localhost:8000")).rstrip("/")
    engine_info = {}
    auto_approve = not args.approve

    try:
        import httpx
        health = httpx.get(f"{base_url}/health", timeout=5).json()
        engine_info = health
    except Exception:
        console.print(Panel(
            f"[{C_WARN}]Could not connect to engine at {base_url}[/{C_WARN}]\n\n"
            "[dim]Make sure the engine is running:[/dim]\n"
            "  python -m resonant_engine --load data/resonant_engine\n\n"
            "[dim]Or specify a different host:[/dim]\n"
            f"  resonant --api http://<engine-host>:8000\n\n"
            "[dim]Or set the RESONANT_API environment variable:[/dim]\n"
            "  export RESONANT_API=http://10.0.0.133:8000",
            title="Connection Error",
            border_style=C_ERR,
        ))
        return

    # Banner
    print_banner(engine_info)

    # Input history
    history_file = Path.home() / ".resonant_agent_history"
    history = FileHistory(str(history_file))

    conversation_history = []

    while True:
        try:
            cwd_short = Path(os.getcwd()).name
            user_input = pt_prompt(
                HTML(f'<style fg="ansibrightcyan"><b>{cwd_short}</b></style> <style fg="ansiwhite"><b>&gt;</b></style> '),
                history=history,
                multiline=False,
            ).strip()
        except (EOFError, KeyboardInterrupt):
            console.print("\n[dim]Goodbye![/dim]")
            break

        if not user_input:
            continue

        # Slash commands
        if user_input.startswith("/"):
            parts = user_input.split(maxsplit=1)
            cmd = parts[0].lower()
            rest = parts[1] if len(parts) > 1 else ""

            if cmd in ("/quit", "/exit", "/q"):
                console.print("[dim]Goodbye![/dim]")
                break

            elif cmd == "/cd":
                if rest:
                    try:
                        os.chdir(rest)
                        console.print(f"  [dim]-> {os.getcwd()}[/dim]")
                    except Exception as e:
                        console.print(f"  [{C_ERR}]{e}[/{C_ERR}]")
                else:
                    console.print(f"  [dim]{os.getcwd()}[/dim]")

            elif cmd == "/clear":
                conversation_history.clear()
                console.clear()
                print_banner(engine_info)

            elif cmd == "/status":
                try:
                    import httpx
                    health = httpx.get(f"{base_url}/health", timeout=5).json()
                    table = Table(show_header=False, border_style="dim", padding=(0, 1))
                    table.add_column(style=C_BRAND)
                    table.add_column()
                    for k, v in health.items():
                        table.add_row(k, str(v))
                    console.print(table)
                except Exception as e:
                    console.print(f"  [{C_ERR}]{e}[/{C_ERR}]")

            elif cmd == "/approve":
                if rest.lower() in ("on", "true", "yes"):
                    auto_approve = False
                    console.print(f"  [{C_WARN}]Tool approval: ON (will ask before executing)[/{C_WARN}]")
                elif rest.lower() in ("off", "false", "no"):
                    auto_approve = True
                    console.print(f"  [{C_OK}]Tool approval: OFF (auto-execute)[/{C_OK}]")
                else:
                    state = "ON (ask before each tool)" if not auto_approve else "OFF (auto-execute)"
                    console.print(f"  Approval mode: {state}")
                    console.print("  [dim]Use /approve on or /approve off[/dim]")

            elif cmd == "/help":
                console.print(Panel(
                    f"[bold]Connected to[/bold] {base_url}\n\n"
                    "[bold]Commands[/bold]\n"
                    "  /help             Show this help\n"
                    "  /quit             Exit\n"
                    "  /cd <dir>         Change directory\n"
                    "  /clear            Clear conversation\n"
                    "  /status           Engine status\n"
                    "  /approve on|off   Toggle tool approval\n\n"
                    "[bold]Tips[/bold]\n"
                    '  - Ask naturally: "Build a REST API with auth"\n'
                    '  - Be specific: "Edit main.py to add error handling"\n'
                    '  - Chain tasks: "Run tests and fix failures"\n'
                    '  - Ctrl+C to interrupt a running agent step',
                    title=f"[{C_BRAND}]Resonant Code Agent[/{C_BRAND}]",
                    border_style=C_BRAND,
                    padding=(0, 1),
                ))

            else:
                console.print(f"  [dim]Unknown: {cmd}. Try /help[/dim]")
            continue

        # Add to history and run
        conversation_history.append({"role": "user", "content": user_input})

        run_agent_loop_streaming(
            base_url=base_url,
            user_msg=user_input,
            conversation_history=conversation_history,
            max_iterations=args.max_steps,
            max_tokens=args.max_tokens,
            auto_approve=auto_approve,
        )


if __name__ == "__main__":
    main()
