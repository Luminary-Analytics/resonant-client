"""
Resonant Code Agent — TUI Client

A thin terminal interface that renders EngineEvents.
Can run in two modes:
  - Embedded: Engine runs in-process (default, single binary)
  - Remote: Connects to an engine server via WebSocket

The TUI handles ONLY display and input. All logic lives in the engine.
"""

import json
import os
import re
import sys
import time
import argparse
import logging
import difflib
from pathlib import Path
from typing import Optional

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

from .events import EngineEvent, make_event
from .network_defaults import (
    get_default_backend,
    get_default_model,
    resolve_remote_engine_ws_url,
    resolve_resonant_api_url,
)
from .backends import (
    create_backend,
    OllamaBackend,
    ResonantBackend,
    ClaudeBackend,
    OpenAIBackend,
)
from .engine import Session
from .engine.tools import AGENT_TOOLS, get_tool_icon

logger = logging.getLogger(__name__)

# Force UTF-8 output on Windows
import io
if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

console = Console(force_terminal=True)


# ══════════════════════════════════════════════════════════════════════
#  Design System (inspired by opencode)
# ══════════════════════════════════════════════════════════════════════
# Three-depth visual hierarchy: bg → panel → element
# Split border pattern: thick left ┃ bar, no full boxes
# Semantic color tokens, not raw hex scattered around

C_BRAND   = "#5f87ff"    # Soft periwinkle — primary accent
C_BRAND2  = "#87afff"    # Lighter blue — secondary
C_DIM     = "#585858"    # Muted gray — deemphasized text
C_DIMMER  = "#3a3a3a"    # Even more muted — structural elements
C_OK      = "#87d787"    # Soft green — success
C_WARN    = "#ffaf5f"    # Warm amber — warnings, plan mode
C_ERR     = "#ff5f5f"    # Soft red — errors
C_TOOL    = "#af87ff"    # Lavender — tool actions
C_FILE    = "#5fd7af"    # Teal — file paths
C_BORDER  = "#3a3a3a"    # Panel borders (subtle)
C_TEXT    = "#d0d0d0"    # Main text (slightly off-white)
C_MUTED   = "#808080"    # Mid-gray — secondary info

# Three-depth backgrounds (opencode pattern)
C_BG_PANEL = "#1a1a2e"   # Slightly lighter than terminal bg
C_BG_ELEM  = "#16213e"   # Element background (code blocks, diffs)

# Diff colors (opencode style)
C_DIFF_ADD = "#2d5a2d"   # Background for added lines
C_DIFF_DEL = "#5a2d2d"   # Background for removed lines

# Unicode glyphs
G_PROMPT  = "❯"
G_ARROW   = "→"
G_CHECK   = "✓"
G_CROSS   = "✗"
G_DOT     = "●"
G_DASH    = "─"
G_STEP    = "◆"
G_PLAN    = "◇"
G_THINK   = "⋯"
G_SPLIT   = "┃"     # Split border (opencode pattern — thick left bar)
G_VLINE   = "│"     # Thin vertical line
G_SQUARE  = "▣"     # Step completion marker (opencode)
G_RUNNING = "◉"     # Active/running indicator

# Tool-specific icons (opencode style)
TOOL_DISPLAY = {
    "file_read":  ("→", "Read",  C_TOOL),
    "file_write": ("←", "Write", C_OK),
    "file_edit":  ("~", "Edit",  C_WARN),
    "bash":       ("$", "Shell", C_TOOL),
    "glob":       ("✱", "Glob",  C_TOOL),
    "grep":       ("/", "Grep",  C_TOOL),
    "task":       ("│", "Task",  C_BRAND2),
    "batch":      ("⚡", "Batch", C_BRAND),
    # Browser tools
    "browser_navigate":   ("⊕", "Navigate",   C_BRAND2),
    "browser_click":      ("◎", "Click",      C_BRAND2),
    "browser_type":       ("⌨", "Type",       C_BRAND2),
    "browser_read":       ("◫", "Read Page",  C_BRAND2),
    "browser_screenshot": ("◰", "Screenshot", C_BRAND2),
    "browser_js":         ("⟐", "JavaScript", C_BRAND2),
    # Desktop / Computer Use tools
    "computer_screenshot": ("▣", "Desktop Screenshot", C_WARN),
    "computer_click":      ("◎", "Desktop Click",      C_WARN),
    "computer_type":       ("⌨", "Desktop Type",       C_WARN),
    "computer_scroll":     ("↕", "Desktop Scroll",     C_WARN),
}

# Inline tool format: "icon description (metadata)" — single line for simple tools
# Block tool format: left bar + panel for tools with output (writes, edits, bash)

# Collapsible tools — these get grouped and collapsed (Claude Code pattern)
COLLAPSIBLE_TOOLS = {"file_read", "glob", "grep", "browser_read", "computer_screenshot", "browser_screenshot"}
# Block tools — always show full detail
BLOCK_TOOLS = {"bash", "file_write", "file_edit", "browser_js"}

# Max lines of tool output to show before truncation (Claude Code default: 3)
MAX_TOOL_OUTPUT_LINES = 5


# ══════════════════════════════════════════════════════════════════════
#  Language detection for syntax highlighting
# ══════════════════════════════════════════════════════════════════════

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


# ══════════════════════════════════════════════════════════════════════
#  Display renderers — consume EngineEvents
# ══════════════════════════════════════════════════════════════════════
# Two-tier display system (opencode pattern):
#   - Inline: single line for simple tools (read, glob, grep)
#   - Block: expandable panel for tools with output (write, edit, bash)

def _tool_info(name: str) -> tuple:
    """Get (icon, label, color) for a tool."""
    return TOOL_DISPLAY.get(name, ("⚙", name, C_TOOL))


def _inline_tool(icon: str, desc: str, meta: str = "", color: str = C_MUTED):
    """Render a single-line inline tool display (opencode InlineTool pattern)."""
    meta_part = f"  [{C_DIM}]({meta})[/{C_DIM}]" if meta else ""
    console.print(f"  [{C_DIMMER}]{G_SPLIT}[/{C_DIMMER}] [{color}]{icon}[/{color}] [{C_TEXT}]{desc}[/{C_TEXT}]{meta_part}")


def _block_tool_start(icon: str, title: str, color: str = C_TOOL):
    """Start a block tool display — title line with split border."""
    console.print(f"  [{color}]{G_SPLIT}[/{color}] [{color}]{icon} {title}[/{color}]")


def _block_tool_line(text: str, color: str = C_DIM):
    """Add a line inside a block tool display."""
    console.print(f"  [{C_DIMMER}]{G_VLINE}[/{C_DIMMER}]   [{color}]{text}[/{color}]")


def _block_tool_end():
    """End a block tool display with a subtle bottom line."""
    pass  # Clean exit, no explicit closer needed


def _render_tool_call(event: dict):
    """
    Render a TOOL_CALL event — opencode two-tier pattern.

    Simple tools (read, glob, grep) → inline single-line
    Complex tools (write, edit, bash) → block with expandable content
    """
    name = event.get("name", "")
    args = event.get("arguments", {})
    icon, label, color = _tool_info(name)

    if name == "bash":
        cmd = args.get("command", "")
        display_cmd = cmd if len(cmd) < 100 else cmd[:97] + "..."
        _block_tool_start(icon, label, color=C_TOOL)
        _block_tool_line(f"$ {display_cmd}", color=C_TEXT)

    elif name == "file_write":
        fpath = args.get("path", "")
        content = args.get("content", "")
        lines = content.split("\n")
        line_count = len(lines)
        lang = _detect_lang(fpath)

        _block_tool_start(icon, f"{label} [{C_FILE}]{fpath}[/{C_FILE}]", color=C_OK)

        # Syntax-highlighted preview with split border
        preview = "\n".join(lines[:15])
        if line_count > 15:
            preview += f"\n# ... ({line_count - 15} more lines)"

        console.print(Panel(
            Syntax(preview, lang, theme="one-dark", line_numbers=True, word_wrap=True),
            border_style=C_DIMMER,
            padding=(0, 1),
            expand=False,
            subtitle=f"[{C_DIM}]{line_count} lines[/{C_DIM}]",
            subtitle_align="right",
        ))

    elif name == "file_edit":
        fpath = args.get("path", "")
        old_text = args.get("old_text", "")
        new_text = args.get("new_text", "")

        _block_tool_start(icon, f"{label} [{C_FILE}]{fpath}[/{C_FILE}]", color=C_WARN)

        old_lines = old_text.split("\n")
        new_lines = new_text.split("\n")
        diff = list(difflib.unified_diff(old_lines, new_lines, lineterm="", n=2))
        if diff:
            colored_lines = []
            for line in diff[2:20]:
                if line.startswith("+"):
                    colored_lines.append(f"[{C_OK}]  + {line[1:]}[/{C_OK}]")
                elif line.startswith("-"):
                    colored_lines.append(f"[{C_ERR}]  - {line[1:]}[/{C_ERR}]")
                elif line.startswith("@@"):
                    colored_lines.append(f"[{C_BRAND}]  {line}[/{C_BRAND}]")
                else:
                    colored_lines.append(f"[{C_DIM}]    {line}[/{C_DIM}]")
            if len(diff) > 20:
                colored_lines.append(f"[{C_DIM}]    {G_THINK} {len(diff) - 20} more lines[/{C_DIM}]")
            added = sum(1 for l in diff[2:] if l.startswith("+"))
            removed = sum(1 for l in diff[2:] if l.startswith("-"))
            subtitle = f"[{C_OK}]+{added}[/{C_OK}] [{C_ERR}]-{removed}[/{C_ERR}]"
            console.print(Panel(
                "\n".join(colored_lines),
                border_style=C_DIMMER,
                padding=(0, 1),
                expand=False,
                subtitle=subtitle,
                subtitle_align="right",
            ))
        else:
            _block_tool_line("(no visible diff)", color=C_DIM)

    elif name == "file_read":
        # Inline — single line, no block needed
        fpath = args.get("path", "")
        _inline_tool(icon, f"[{C_FILE}]{fpath}[/{C_FILE}]", color=C_TOOL)

    elif name == "glob":
        pattern = args.get("pattern", "")
        base = args.get("path", ".")
        _inline_tool(icon, f"{pattern}", meta=base, color=C_TOOL)

    elif name == "grep":
        pattern = args.get("pattern", "")
        path = args.get("path", ".")
        _inline_tool(icon, f"'{pattern}'", meta=path, color=C_TOOL)

    elif name == "task":
        prompt = args.get("prompt", "")
        agent_type = args.get("agent_type", "explore")
        display_prompt = prompt if len(prompt) < 80 else prompt[:77] + "..."
        _block_tool_start(icon, f"{label} [{C_BRAND2}]{agent_type}[/{C_BRAND2}] agent", color=C_BRAND2)
        _block_tool_line(f'"{display_prompt}"', color=C_TEXT)

    elif name == "batch":
        calls = args.get("calls", [])
        _block_tool_start(icon, f"{label} [{C_BRAND}]{len(calls)} parallel calls[/{C_BRAND}]", color=C_BRAND)
        for call in calls[:10]:
            cname = call.get("name", "")
            cargs = call.get("arguments", {})
            cicon, clabel, ccolor = _tool_info(cname)
            # Show each batched call as a compact line
            if cname == "file_read":
                _block_tool_line(f"{cicon} {clabel} {cargs.get('path', '')}", color=ccolor)
            elif cname == "glob":
                _block_tool_line(f"{cicon} {clabel} {cargs.get('pattern', '')}", color=ccolor)
            elif cname == "grep":
                _block_tool_line(f"{cicon} {clabel} '{cargs.get('pattern', '')}'", color=ccolor)
            else:
                _block_tool_line(f"{cicon} {clabel}", color=ccolor)
        if len(calls) > 10:
            _block_tool_line(f"{G_THINK} +{len(calls) - 10} more", color=C_DIM)

    # ── Browser tools ──
    elif name == "browser_navigate":
        url = args.get("url", "")
        _inline_tool(icon, f"[{C_FILE}]{url}[/{C_FILE}]", color=C_BRAND2)
    elif name == "browser_click":
        text = args.get("text", "")
        selector = args.get("selector", "")
        target = text or selector or f"({args.get('x', '?')}, {args.get('y', '?')})"
        _inline_tool(icon, f"Click [{C_TEXT}]{target}[/{C_TEXT}]", color=C_BRAND2)
    elif name == "browser_type":
        text = args.get("text", "")
        display = text if len(text) < 40 else text[:37] + "..."
        _inline_tool(icon, f"Type [{C_TEXT}]'{display}'[/{C_TEXT}]", color=C_BRAND2)
    elif name == "browser_read":
        mode = args.get("mode", "text")
        selector = args.get("selector", "")
        meta = f"{mode}" + (f" · {selector}" if selector else "")
        _inline_tool(icon, f"Read page", meta=meta, color=C_BRAND2)
    elif name == "browser_screenshot":
        full = args.get("full_page", False)
        _inline_tool(icon, "Page screenshot", meta="full page" if full else "viewport", color=C_BRAND2)
    elif name == "browser_js":
        code = args.get("code", "")
        display = code if len(code) < 80 else code[:77] + "..."
        _block_tool_start(icon, label, color=C_BRAND2)
        _block_tool_line(display, color=C_TEXT)

    # ── Desktop / Computer Use tools ──
    elif name == "computer_screenshot":
        region = args.get("region")
        meta = f"{region['width']}×{region['height']}" if region else "full screen"
        _inline_tool(icon, "Desktop screenshot", meta=meta, color=C_WARN)
    elif name == "computer_click":
        x, y = args.get("x", 0), args.get("y", 0)
        button = args.get("button", "left")
        clicks = args.get("clicks", 1)
        click_type = "Double-click" if clicks == 2 else "Click"
        _inline_tool(icon, f"{click_type} ({x}, {y})", meta=button, color=C_WARN)
    elif name == "computer_type":
        text = args.get("text", "")
        key = args.get("key", "") or args.get("hotkey", "")
        if key:
            _inline_tool(icon, f"Press [{C_TEXT}]{key}[/{C_TEXT}]", color=C_WARN)
        else:
            display = text if len(text) < 40 else text[:37] + "..."
            _inline_tool(icon, f"Type [{C_TEXT}]'{display}'[/{C_TEXT}]", color=C_WARN)
    elif name == "computer_scroll":
        direction = args.get("direction", "down")
        amount = args.get("amount", 3)
        _inline_tool(icon, f"Scroll {direction} ×{amount}", color=C_WARN)

    else:
        _inline_tool("⚙", name, color=C_TOOL)


def _render_tool_result(event: dict):
    """
    Render a TOOL_RESULT event — completion status.

    Inline tools: append result on same visual group
    Block tools: show output in the block area
    """
    name = event.get("name", "")
    output = event.get("output", "")
    is_error = event.get("is_error", False)
    elapsed = event.get("elapsed", 0.0)
    denied = event.get("denied", False)
    metadata = event.get("metadata", {})

    if denied:
        console.print(f"  [{C_DIMMER}]{G_VLINE}[/{C_DIMMER}]   [{C_WARN}]✗ denied[/{C_WARN}]")
        return

    if name == "bash":
        # Block output — truncate to MAX_TOOL_OUTPUT_LINES (Claude Code shows ~3)
        lines = output.split("\n")
        if len(lines) > MAX_TOOL_OUTPUT_LINES:
            display_lines = lines[:MAX_TOOL_OUTPUT_LINES] + [f"{G_THINK} +{len(lines) - MAX_TOOL_OUTPUT_LINES} lines"]
        else:
            display_lines = lines
        style = C_ERR if is_error else C_DIM
        for line in display_lines:
            _block_tool_line(line, color=style)
        timed_out = metadata.get("timed_out", False)
        exit_code = metadata.get("exit_code", 0)
        status_parts = [f"{elapsed:.1f}s"]
        if timed_out:
            status_parts.append("timeout")
        if exit_code != 0:
            status_parts.append(f"exit {exit_code}")
        _block_tool_line(" · ".join(status_parts), color=C_DIM)

    elif name == "file_write":
        fpath = metadata.get("path", "")
        lines = metadata.get("lines", 0)
        chars = metadata.get("chars", 0)
        icon = G_CHECK if not is_error else G_CROSS
        style = C_OK if not is_error else C_ERR
        _block_tool_line(f"{icon} {lines} lines, {chars} chars", color=style)

    elif name == "file_edit":
        icon = G_CHECK if not is_error else G_CROSS
        style = C_OK if not is_error else C_ERR
        msg = "applied" if not is_error else output
        _block_tool_line(f"{icon} {msg}", color=style)

    elif name == "file_read":
        lines_count = metadata.get("lines", 0)
        if is_error:
            console.print(f"  [{C_DIMMER}]{G_VLINE}[/{C_DIMMER}]   [{C_ERR}]{G_CROSS} {output}[/{C_ERR}]")
        elif lines_count:
            console.print(f"  [{C_DIMMER}]{G_VLINE}[/{C_DIMMER}]   [{C_DIM}]{lines_count} lines[/{C_DIM}]")

    elif name == "glob":
        count = metadata.get("count", 0)
        console.print(f"  [{C_DIMMER}]{G_VLINE}[/{C_DIMMER}]   [{C_DIM}]{count} files[/{C_DIM}]")

    elif name == "grep":
        count = metadata.get("count", 0)
        console.print(f"  [{C_DIMMER}]{G_VLINE}[/{C_DIMMER}]   [{C_DIM}]{count} matches[/{C_DIM}]")

    elif name == "task":
        agent_type = metadata.get("agent_type", "")
        steps = metadata.get("steps", 0)
        icon = G_CHECK if not is_error else G_CROSS
        style = C_OK if not is_error else C_ERR
        _block_tool_line(f"{icon} {agent_type} · {steps} steps · {elapsed:.1f}s", color=style)

    elif name == "batch":
        successes = metadata.get("successes", 0)
        total = metadata.get("total", 0)
        failures = total - successes
        if failures == 0:
            _block_tool_line(f"{G_CHECK} {successes}/{total} succeeded · {elapsed:.1f}s", color=C_OK)
        else:
            _block_tool_line(f"{G_CROSS} {successes}/{total} succeeded, {failures} failed · {elapsed:.1f}s", color=C_WARN)

    # ── Browser tool results ──
    elif name == "browser_navigate":
        url = metadata.get("url", "")
        title = metadata.get("title", "")
        icon = G_CHECK if not is_error else G_CROSS
        style = C_OK if not is_error else C_ERR
        if is_error:
            console.print(f"  [{C_DIMMER}]{G_VLINE}[/{C_DIMMER}]   [{style}]{icon} {output}[/{style}]")
        else:
            console.print(f"  [{C_DIMMER}]{G_VLINE}[/{C_DIMMER}]   [{style}]{icon}[/{style}] [{C_DIM}]{title}[/{C_DIM}]")
    elif name in ("browser_click", "browser_type"):
        icon = G_CHECK if not is_error else G_CROSS
        style = C_OK if not is_error else C_ERR
        console.print(f"  [{C_DIMMER}]{G_VLINE}[/{C_DIMMER}]   [{style}]{icon} {output}[/{style}]")
    elif name == "browser_read":
        chars = metadata.get("chars", 0)
        if is_error:
            console.print(f"  [{C_DIMMER}]{G_VLINE}[/{C_DIMMER}]   [{C_ERR}]{G_CROSS} {output}[/{C_ERR}]")
        else:
            console.print(f"  [{C_DIMMER}]{G_VLINE}[/{C_DIMMER}]   [{C_DIM}]{chars:,} chars[/{C_DIM}]")
    elif name == "browser_screenshot":
        size_bytes = metadata.get("size_bytes", 0)
        size_kb = size_bytes / 1024 if size_bytes else 0
        icon = G_CHECK if not is_error else G_CROSS
        style = C_OK if not is_error else C_ERR
        if is_error:
            console.print(f"  [{C_DIMMER}]{G_VLINE}[/{C_DIMMER}]   [{style}]{icon} {output}[/{style}]")
        else:
            console.print(f"  [{C_DIMMER}]{G_VLINE}[/{C_DIMMER}]   [{style}]{icon}[/{style}] [{C_DIM}]{size_kb:.0f}KB[/{C_DIM}]")
    elif name == "browser_js":
        lines = output.split("\n")
        if len(lines) > MAX_TOOL_OUTPUT_LINES:
            display_lines = lines[:MAX_TOOL_OUTPUT_LINES] + [f"{G_THINK} +{len(lines) - MAX_TOOL_OUTPUT_LINES} lines"]
        else:
            display_lines = lines
        style = C_ERR if is_error else C_DIM
        for line in display_lines:
            _block_tool_line(line, color=style)

    # ── Desktop / Computer Use results ──
    elif name == "computer_screenshot":
        size_bytes = metadata.get("size_bytes", 0)
        w = metadata.get("width", 0)
        h = metadata.get("height", 0)
        size_kb = size_bytes / 1024 if size_bytes else 0
        icon = G_CHECK if not is_error else G_CROSS
        style = C_OK if not is_error else C_ERR
        if is_error:
            console.print(f"  [{C_DIMMER}]{G_VLINE}[/{C_DIMMER}]   [{style}]{icon} {output}[/{style}]")
        else:
            console.print(f"  [{C_DIMMER}]{G_VLINE}[/{C_DIMMER}]   [{style}]{icon}[/{style}] [{C_DIM}]{w}×{h} · {size_kb:.0f}KB[/{C_DIM}]")
    elif name in ("computer_click", "computer_type", "computer_scroll"):
        icon = G_CHECK if not is_error else G_CROSS
        style = C_OK if not is_error else C_ERR
        console.print(f"  [{C_DIMMER}]{G_VLINE}[/{C_DIMMER}]   [{style}]{icon} {output}[/{style}]")

    else:
        if is_error:
            console.print(f"  [{C_DIMMER}]{G_VLINE}[/{C_DIMMER}]   [{C_ERR}]{G_CROSS} {output}[/{C_ERR}]")
        else:
            console.print(f"  [{C_DIMMER}]{G_VLINE}[/{C_DIMMER}]   [{C_OK}]{G_CHECK}[/{C_OK}]")


def _render_step_start(event: dict):
    """
    Render a STEP_START event — opencode-style step header.

    Clean horizontal rule with step number and contextual label.
    """
    step = event.get("step", 0)
    step_type = event.get("step_type", "execute")
    label = event.get("label", "")

    console.print()

    if step_type == "plan":
        console.print(f"  [{C_DIMMER}]{G_DASH * 60}[/{C_DIMMER}]")
        console.print(f"  [{C_WARN}]{G_PLAN} Plan[/{C_WARN}]  [{C_DIM}]{label}[/{C_DIM}]")
    elif step == 1:
        console.print(f"  [{C_DIMMER}]{G_DASH * 60}[/{C_DIMMER}]")
        console.print(f"  [{C_BRAND}]{G_STEP} Step {step}[/{C_BRAND}]")
    else:
        console.print(f"  [{C_DIMMER}]{G_DASH * 60}[/{C_DIMMER}]")
        ctx = f"  [{C_DIM}]{label}[/{C_DIM}]" if label else ""
        console.print(f"  [{C_BRAND}]{G_STEP} Step {step}[/{C_BRAND}]{ctx}")


def _render_step_end(event: dict, model: str = None, stats: dict = None):
    """
    Render step completion marker (opencode pattern).
    Format: ▣ model · duration · tokens
    """
    step = event.get("step", 0)
    elapsed = event.get("elapsed", 0.0)
    if not model or elapsed <= 0:
        return

    parts = [model]
    if stats:
        input_tok = stats.get("input_tokens")
        output_tok = stats.get("output_tokens")
        eval_count = stats.get("eval_count")
        eval_dur = stats.get("eval_duration")
        if eval_count and eval_dur and eval_dur > 0:
            tps = eval_count / (eval_dur / 1e9)
            parts.append(f"{tps:.1f} tok/s")
        elif input_tok and output_tok:
            parts.append(f"{input_tok}→{output_tok} tok")
    parts.append(f"{elapsed:.1f}s")
    console.print(f"  [{C_DIM}]{G_SQUARE} {' · '.join(parts)}[/{C_DIM}]")


def _render_status(event: dict):
    """Render a STATUS event — model info, token counts, timing."""
    parts = []
    model = event.get("model")
    stats = event.get("stats") or {}
    elapsed = event.get("elapsed", 0)
    cog_state = event.get("cognitive_state")

    if model:
        parts.append(f"[{C_BRAND2}]{model}[/{C_BRAND2}]")

    if cog_state:
        energy = cog_state.get("energy", 0)
        coherence = cog_state.get("coherence", 0)
        mode = cog_state.get("processing_mode", "")
        clusters = cog_state.get("clusters", 0)
        parts.append(f"[{C_MUTED}]energy {energy:.0%}[/{C_MUTED}]")
        parts.append(f"[{C_MUTED}]coherence {coherence:.0%}[/{C_MUTED}]")
        if clusters:
            parts.append(f"[{C_MUTED}]{clusters} clusters[/{C_MUTED}]")
        if mode:
            parts.append(f"[{C_MUTED}]{mode}[/{C_MUTED}]")

    if stats:
        eval_count = stats.get("eval_count")
        eval_duration = stats.get("eval_duration")
        prompt_eval_count = stats.get("prompt_eval_count")
        prompt_eval_dur = stats.get("prompt_eval_duration")
        input_tokens = stats.get("input_tokens")
        output_tokens = stats.get("output_tokens")
        if eval_count and eval_duration and eval_duration > 0:
            tok_per_sec = eval_count / (eval_duration / 1e9)
            parts.append(f"[{C_MUTED}]{tok_per_sec:.1f} tok/s[/{C_MUTED}]")
        if prompt_eval_count and prompt_eval_dur and prompt_eval_dur > 0:
            pp_speed = prompt_eval_count / (prompt_eval_dur / 1e9)
            parts.append(f"[{C_MUTED}]pp {pp_speed:.0f}[/{C_MUTED}]")
        if input_tokens and output_tokens:
            parts.append(f"[{C_MUTED}]{input_tokens}{G_ARROW}{output_tokens} tok[/{C_MUTED}]")
        elif output_tokens:
            parts.append(f"[{C_MUTED}]{output_tokens} tok[/{C_MUTED}]")

    if elapsed > 0:
        parts.append(f"[{C_MUTED}]{elapsed:.1f}s[/{C_MUTED}]")

    if parts:
        sep = f" [{C_DIMMER}]·[/{C_DIMMER}] "
        console.print(f"  {sep.join(parts)}")


def _render_choices(options: list) -> str:
    """Render choices as a clean numbered menu and prompt the user to pick."""
    console.print()
    console.print(f"  [{C_BORDER}]{G_DASH * 40}[/{C_BORDER}]")
    for i, opt in enumerate(options):
        rec = f"  [{C_BRAND2}]recommended[/{C_BRAND2}]" if "(Recommended)" in opt or "(recommended)" in opt else ""
        label = opt.replace("(Recommended)", "").replace("(recommended)", "").strip()
        if i == 0:
            console.print(f"  [{C_BRAND}]{G_DOT}[/{C_BRAND}] [{C_DIM}]{i + 1}.[/{C_DIM}] [{C_TEXT}]{label}[/{C_TEXT}]{rec}")
        else:
            console.print(f"    [{C_DIM}]{i + 1}.[/{C_DIM}] [{C_TEXT}]{label}[/{C_TEXT}]{rec}")

    console.print(f"    [{C_DIM}]{len(options) + 1}. Other (type your own)[/{C_DIM}]")
    console.print()

    try:
        answer = pt_prompt(
            HTML(f'<style fg="#{C_BRAND[1:]}">  Choice [1-{len(options) + 1}]: </style>'),
        ).strip()
    except (EOFError, KeyboardInterrupt):
        return options[0]

    try:
        idx = int(answer) - 1
        if 0 <= idx < len(options):
            selected = options[idx].replace("(Recommended)", "").replace("(recommended)", "").strip()
            console.print(f"  [{C_OK}]{G_CHECK} {selected}[/{C_OK}]")
            return selected
        elif idx == len(options):
            custom = pt_prompt(HTML(f'<style fg="#{C_BRAND[1:]}">  Your choice: </style>')).strip()
            if custom:
                console.print(f"  [{C_OK}]{G_CHECK} {custom}[/{C_OK}]")
                return custom
            return options[0]
    except ValueError:
        if answer:
            console.print(f"  [{C_OK}]{G_CHECK} {answer}[/{C_OK}]")
            return answer

    return options[0]


# ══════════════════════════════════════════════════════════════════════
#  Collapsed step group renderer (Claude Code pattern)
# ══════════════════════════════════════════════════════════════════════

def _flush_collapsed_group(group: list):
    """
    Render a group of collapsed inline-only steps as a compact block.

    Claude Code pattern: "Read 5 files, searched 3 patterns"
    We show: step range, then each tool on one tight line.

    Instead of 7+ lines per step, each collapsed step takes 1 line.
    A group of 5 reads that would have been ~35 lines becomes ~8 lines.
    """
    if not group:
        return

    # Calculate step range
    first_step = group[0]["step_event"].get("step", 0)
    last_step = group[-1]["step_event"].get("step", 0)
    total_elapsed = sum(g["end_event"].get("elapsed", 0) for g in group)

    # Count tools by type
    tool_counts = {}
    for g in group:
        for tc in g["tool_calls"]:
            name = tc.get("name", "unknown")
            tool_counts[name] = tool_counts.get(name, 0) + 1

    # Build summary label: "Read 5 files, Glob 2 patterns"
    summary_parts = []
    for name, count in tool_counts.items():
        icon, label, _ = _tool_info(name)
        if count == 1:
            summary_parts.append(f"{label}")
        else:
            summary_parts.append(f"{label} ×{count}")
    summary = ", ".join(summary_parts)

    # Header
    console.print()
    console.print(f"  [{C_DIMMER}]{G_DASH * 60}[/{C_DIMMER}]")
    if first_step == last_step:
        console.print(f"  [{C_BRAND}]{G_STEP} Step {first_step}[/{C_BRAND}]  [{C_DIM}]{summary}[/{C_DIM}]")
    else:
        console.print(f"  [{C_BRAND}]{G_STEP} Steps {first_step}–{last_step}[/{C_BRAND}]  [{C_DIM}]{summary}[/{C_DIM}]")

    # Compact tool lines — one per tool call
    for g in group:
        for i, tc in enumerate(g["tool_calls"]):
            name = tc.get("name", "")
            args = tc.get("arguments", {})
            result = g["tool_results"][i] if i < len(g["tool_results"]) else {}
            metadata = result.get("metadata", {})
            is_error = result.get("is_error", False)
            icon, label, color = _tool_info(name)

            if name == "file_read":
                fpath = args.get("path", "")
                # Shorten path for display
                short = fpath.replace("\\", "/")
                if len(short) > 50:
                    short = "…/" + "/".join(short.split("/")[-2:])
                lines_count = metadata.get("lines", 0)
                meta = f"{lines_count} lines" if lines_count else ""
                status = f"[{C_ERR}]{G_CROSS}[/{C_ERR}]" if is_error else ""
                console.print(f"    [{C_DIMMER}]{G_VLINE}[/{C_DIMMER}] [{color}]{icon}[/{color}] [{C_FILE}]{short}[/{C_FILE}]  [{C_DIM}]{meta}[/{C_DIM}] {status}")

            elif name == "glob":
                pattern = args.get("pattern", "")
                count = metadata.get("count", 0)
                console.print(f"    [{C_DIMMER}]{G_VLINE}[/{C_DIMMER}] [{color}]{icon}[/{color}] [{C_TEXT}]{pattern}[/{C_TEXT}]  [{C_DIM}]{count} files[/{C_DIM}]")

            elif name == "grep":
                pattern = args.get("pattern", "")
                count = metadata.get("count", 0)
                console.print(f"    [{C_DIMMER}]{G_VLINE}[/{C_DIMMER}] [{color}]{icon}[/{color}] [{C_TEXT}]'{pattern}'[/{C_TEXT}]  [{C_DIM}]{count} matches[/{C_DIM}]")

            else:
                console.print(f"    [{C_DIMMER}]{G_VLINE}[/{C_DIMMER}] [{color}]{icon}[/{color}] [{C_TEXT}]{name}[/{C_TEXT}]")

    # Footer with timing
    model = group[-1].get("model")
    stats_info = group[-1].get("stats")
    parts = []
    if model:
        parts.append(model)
    if stats_info:
        input_tok = stats_info.get("input_tokens")
        output_tok = stats_info.get("output_tokens")
        if input_tok and output_tok:
            parts.append(f"{input_tok}{G_ARROW}{output_tok} tok")
    parts.append(f"{total_elapsed:.1f}s")
    console.print(f"  [{C_DIM}]{G_SQUARE} {' · '.join(parts)}[/{C_DIM}]")


# ══════════════════════════════════════════════════════════════════════
#  Event stream consumer — the core rendering loop
# ══════════════════════════════════════════════════════════════════════

def consume_events(event_stream, on_permission=None):
    """
    Consume an iterator of EngineEvents and render them to the terminal.

    This is the bridge between engine and display. Any client that produces
    EngineEvent dicts can use this function (embedded Session, WebSocket, etc.)

    Step collapsing (Claude Code / opencode pattern):
      - Steps with only inline tools (read, glob, grep) are buffered
      - Consecutive inline-only steps are collapsed into a compact group
      - Steps with text output or block tools (bash, write, edit) render fully
      - When a non-inline step arrives, the collapsed group is flushed first
    """
    spinner_live = None
    streaming_started = False
    last_model = None
    last_stats = None

    # Step collapsing state
    current_step_event = None       # Buffered STEP_START for deferred rendering
    step_tool_calls = []            # Buffered tool calls for current step
    step_tool_results = []          # Buffered tool results for current step
    step_is_inline_only = True      # True until text or block tool appears
    step_rendered = False           # Has the step header been printed?
    collapsed_group = []            # Accumulated inline-only steps for group display

    def _ensure_step_rendered():
        """Lazily render the step header + flush collapsed group when we need full output."""
        nonlocal step_rendered, collapsed_group
        if not step_rendered and current_step_event:
            # Flush any pending collapsed group first
            _flush_collapsed_group(collapsed_group)
            collapsed_group = []
            # Now render this step's header
            _render_step_start(current_step_event)
            # Flush any buffered inline tools that came before the block event
            for tc in step_tool_calls:
                console.print()
                _render_tool_call(tc)
            for tr in step_tool_results:
                _render_tool_result(tr)
            step_rendered = True

    for event in event_stream:
        etype = event.get("event", "")

        if etype == EngineEvent.STEP_START.value:
            # Buffer the step start — don't render until we know if it's inline-only
            current_step_event = event
            step_tool_calls = []
            step_tool_results = []
            step_is_inline_only = True
            step_rendered = False
            streaming_started = False
            # Start spinner
            spinner_live = Live(
                Spinner("dots", text=f"[{C_DIM}] thinking {G_THINK}[/{C_DIM}]", style=C_BRAND),
                console=console,
                refresh_per_second=4,
                transient=True,
            )
            spinner_live.start()

        elif etype == EngineEvent.TEXT_DELTA.value:
            delta = event.get("delta", "")
            step_is_inline_only = False
            if not streaming_started:
                if spinner_live:
                    spinner_live.stop()
                    spinner_live = None
                _ensure_step_rendered()
                streaming_started = True
                console.print()  # Newline before text
            sys.stdout.write(delta)
            sys.stdout.flush()

        elif etype == EngineEvent.TEXT_DONE.value:
            if streaming_started:
                sys.stdout.write("\n")
                sys.stdout.flush()
            elif spinner_live:
                spinner_live.stop()
                spinner_live = None

        elif etype == EngineEvent.TOOL_CALL.value:
            if spinner_live:
                spinner_live.stop()
                spinner_live = None
            name = event.get("name", "")
            if name in COLLAPSIBLE_TOOLS and step_is_inline_only:
                # Buffer inline tool — don't render yet
                step_tool_calls.append(event)
            else:
                # Block tool — need full rendering
                step_is_inline_only = False
                _ensure_step_rendered()
                streaming_started = True
                console.print()
                _render_tool_call(event)

        elif etype == EngineEvent.TOOL_RESULT.value:
            name = event.get("name", "")
            if step_is_inline_only and name in COLLAPSIBLE_TOOLS:
                # Buffer inline result
                step_tool_results.append(event)
            else:
                if not step_rendered:
                    _ensure_step_rendered()
                _render_tool_result(event)

        elif etype == EngineEvent.STATUS.value:
            last_model = event.get("model", last_model)
            last_stats = event.get("stats", last_stats)

        elif etype == EngineEvent.STEP_END.value:
            if spinner_live:
                spinner_live.stop()
                spinner_live = None

            if step_is_inline_only and step_tool_calls:
                # This was an inline-only step — add to collapsed group
                collapsed_group.append({
                    "step_event": current_step_event,
                    "tool_calls": step_tool_calls,
                    "tool_results": step_tool_results,
                    "end_event": event,
                    "model": last_model,
                    "stats": last_stats,
                })
            else:
                # Fully rendered step — show completion marker
                _render_step_end(event, model=last_model, stats=last_stats)

        elif etype == EngineEvent.CHOICES.value:
            if spinner_live:
                spinner_live.stop()
                spinner_live = None
            _ensure_step_rendered()
            options = event.get("options", [])
            if options:
                before = event.get("before", "")
                if before:
                    console.print(f"  {before}")

        elif etype == EngineEvent.PLAN_GENERATED.value:
            pass  # Plan was streamed via TEXT_DELTA

        elif etype == EngineEvent.SUBAGENT_START.value:
            if spinner_live:
                spinner_live.stop()
                spinner_live = None
            agent_type = event.get("agent_type", "")
            prompt = event.get("prompt", "")
            display_prompt = prompt if len(prompt) < 70 else prompt[:67] + "..."
            console.print(f"  [{C_BRAND2}]{G_SPLIT}[/{C_BRAND2}] [{C_BRAND2}]│ Task[/{C_BRAND2}]  [{C_MUTED}]{agent_type} agent[/{C_MUTED}]")
            console.print(f"  [{C_DIMMER}]{G_VLINE}[/{C_DIMMER}]   [{C_DIM}]\"{display_prompt}\"[/{C_DIM}]")

        elif etype == EngineEvent.SUBAGENT_END.value:
            agent_type = event.get("agent_type", "")
            steps = event.get("steps", 0)
            elapsed = event.get("elapsed", 0)
            console.print(f"  [{C_DIMMER}]{G_VLINE}[/{C_DIMMER}]   [{C_OK}]{G_CHECK} {agent_type} · {steps} steps · {elapsed:.1f}s[/{C_OK}]")

        elif etype == EngineEvent.ERROR.value:
            if spinner_live:
                spinner_live.stop()
                spinner_live = None
            _ensure_step_rendered()
            msg = event.get("message", "Unknown error")
            console.print(f"\n  [{C_ERR}]{G_CROSS} {msg}[/{C_ERR}]")

        elif etype == EngineEvent.SESSION_END.value:
            if spinner_live:
                spinner_live.stop()
                spinner_live = None
            # Flush any remaining collapsed group
            _flush_collapsed_group(collapsed_group)
            collapsed_group = []

            total_elapsed = event.get("total_elapsed", 0)
            total_steps = event.get("total_steps", 0)
            if total_steps > 1:
                console.print()
                console.print(f"  [{C_DIMMER}]{G_DASH * 60}[/{C_DIMMER}]")
                console.print(f"  [{C_OK}]{G_CHECK}[/{C_OK}] [{C_MUTED}]Done[/{C_MUTED}]  [{C_DIM}]{total_steps} steps · {total_elapsed:.1f}s[/{C_DIM}]")


# ══════════════════════════════════════════════════════════════════════
#  Banner & UI chrome
# ══════════════════════════════════════════════════════════════════════

def print_banner(backend=None, health_info: dict = None):
    """Print a clean, modern startup banner — opencode-inspired."""
    console.print()

    # Title with split border accent
    backend_name = health_info.get("backend", "") if health_info else ""
    taglines = {
        "ollama": "local models",
        "claude": "Claude API",
        "openai": "OpenAI API",
        "resonant": "oscillatory intelligence",
    }
    tagline = taglines.get(backend_name, "agentic coding")

    console.print(f"  [{C_BRAND}]{G_SPLIT}[/{C_BRAND}] [{C_BRAND} bold]Resonant[/{C_BRAND} bold]  [{C_DIM}]{tagline}[/{C_DIM}]")
    console.print(f"  [{C_DIMMER}]{G_DASH * 60}[/{C_DIMMER}]")

    # Status rows — clean key-value with colored dots
    if health_info:
        backend_name = health_info.get("backend", "unknown")
        model = health_info.get("model", "")
        label = _BACKEND_LABELS.get(backend_name, backend_name)

        # Backend + model on one line with connection dot
        console.print(f"  [{C_OK}]{G_DOT}[/{C_OK}] [{C_MUTED}]backend[/{C_MUTED}]  [{C_BRAND2}]{label}[/{C_BRAND2}]", end="")
        if model:
            console.print(f"  [{C_DIMMER}]·[/{C_DIMMER}]  [{C_TEXT}]{model}[/{C_TEXT}]", end="")

        # Resonant-specific status
        if backend_name == "resonant":
            patterns = health_info.get('memory_patterns', 0)
            energy = health_info.get('energy', 0)
            if patterns:
                console.print(f"  [{C_DIMMER}]·[/{C_DIMMER}]  [{C_MUTED}]{patterns:,} patterns[/{C_MUTED}]", end="")
            if energy:
                console.print(f"  [{C_DIMMER}]·[/{C_DIMMER}]  [{C_OK}]{energy:.0%}[/{C_OK}]", end="")
        console.print()

    # CWD and help
    cwd = os.getcwd()
    cwd_display = cwd.replace("\\", "/")  # Normalize for display
    console.print(f"    [{C_MUTED}]cwd[/{C_MUTED}]      [{C_FILE}]{cwd_display}[/{C_FILE}]")
    console.print(f"    [{C_MUTED}]help[/{C_MUTED}]     [{C_DIM}]/help · /plan · /model · /backend · /quit[/{C_DIM}]")
    console.print()


# ══════════════════════════════════════════════════════════════════════
#  Backend detection and selection
# ══════════════════════════════════════════════════════════════════════

_BACKEND_LABELS = {
    "resonant": "Resonant Engine",
    "ollama": "Ollama",
    "claude": "Claude",
    "openai": "OpenAI",
}


def _detect_backends(api_url: str, ollama_url: str, lmstudio_url: str = None) -> dict:
    """Probe all backends and return what's available."""
    import httpx
    available = {}

    try:
        resp = httpx.get(f"{api_url}/health", timeout=5)
        resp.raise_for_status()
        data = resp.json()
        if data.get("status") == "ready":
            available["resonant"] = {"url": api_url, "health": data}
    except Exception:
        pass

    try:
        resp = httpx.get(f"{ollama_url}/api/tags", timeout=5)
        resp.raise_for_status()
        data = resp.json()
        models = [m["name"] for m in data.get("models", [])
                  if not any(kw in m["name"].lower() for kw in ("embed", "bert", "bge", "nomic"))]
        if models:
            available["ollama"] = {"url": ollama_url, "models": models}
    except Exception:
        pass

    # LM Studio (OpenAI-compatible API)
    if lmstudio_url:
        try:
            # LM Studio serves /v1/models (or /models if base_url already has /v1)
            probe_url = lmstudio_url.rstrip("/")
            models_endpoint = f"{probe_url}/models" if probe_url.endswith("/v1") else f"{probe_url}/v1/models"
            base_url = f"{probe_url}" if probe_url.endswith("/v1") else f"{probe_url}/v1"
            resp = httpx.get(models_endpoint, timeout=5)
            resp.raise_for_status()
            data = resp.json()
            models = [m["id"] for m in data.get("data", [])]
            if models:
                available["lmstudio"] = {"base_url": base_url, "models": models}
        except Exception:
            pass

    anthropic_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if anthropic_key:
        try:
            import anthropic  # noqa: F401
            available["claude"] = {"api_key": anthropic_key, "models": ClaudeBackend.MODELS}
        except ImportError:
            pass

    openai_key = os.environ.get("OPENAI_API_KEY", "")
    if openai_key:
        try:
            import openai  # noqa: F401
            available["openai"] = {"api_key": openai_key, "models": OpenAIBackend.MODELS}
        except ImportError:
            pass

    return available


def _select_model_interactive(models: list, current: str = None) -> str:
    """Show a clean model picker and return the selected model name."""
    console.print()
    console.print(f"  [{C_BRAND} bold]Models[/{C_BRAND} bold]")
    console.print(f"  [{C_BORDER}]{G_DASH * 40}[/{C_BORDER}]")
    for i, model in enumerate(models):
        if model == current:
            console.print(f"  [{C_BRAND}]{G_DOT}[/{C_BRAND}] [{C_BRAND}]{i + 1}.[/{C_BRAND}] [{C_TEXT}]{model}[/{C_TEXT}]  [{C_DIM}](current)[/{C_DIM}]")
        else:
            console.print(f"    [{C_DIM}]{i + 1}.[/{C_DIM}] [{C_TEXT}]{model}[/{C_TEXT}]")

    console.print()
    try:
        answer = pt_prompt(
            HTML(f'<style fg="#{C_BRAND[1:]}">  Select [1-{len(models)}]: </style>'),
        ).strip()
    except (EOFError, KeyboardInterrupt):
        return current or models[0]

    try:
        idx = int(answer) - 1
        if 0 <= idx < len(models):
            selected = models[idx]
            console.print(f"  [{C_OK}]{G_CHECK} {selected}[/{C_OK}]")
            return selected
    except ValueError:
        if answer in models:
            console.print(f"  [{C_OK}]{G_CHECK} {answer}[/{C_OK}]")
            return answer

    console.print(f"  [{C_DIM}]Invalid — keeping current model[/{C_DIM}]")
    return current or models[0]


def _create_backend_from_available(target: str, available: dict):
    """Create a backend instance from the available dict."""
    if target == "resonant":
        return create_backend("resonant", available["resonant"]["url"])
    elif target == "ollama":
        model = _select_model_interactive(available["ollama"]["models"])
        return create_backend("ollama", available["ollama"]["url"], model=model)
    elif target == "claude":
        model = _select_model_interactive(available["claude"]["models"])
        return create_backend("claude", api_key=available["claude"]["api_key"], model=model)
    elif target == "openai":
        model = _select_model_interactive(available["openai"]["models"])
        return create_backend("openai", api_key=available["openai"]["api_key"], model=model)
    elif target == "lmstudio":
        model = _select_model_interactive(available["lmstudio"]["models"])
        return create_backend("lmstudio", api_key="lm-studio", model=model,
                              base_url=available["lmstudio"]["base_url"])
    else:
        raise ValueError(f"Unknown backend: {target}")


# ══════════════════════════════════════════════════════════════════════
#  Embedded mode runner (Session in-process)
# ══════════════════════════════════════════════════════════════════════

def run_embedded(session: Session, user_msg: str, images: list = None):
    """
    Run a session turn in embedded mode.
    The session yields events; we render them in real-time.
    """
    def on_permission(tool_name, tool_args):
        """Prompt user for tool approval."""
        try:
            answer = pt_prompt(HTML(f'<style fg="#{C_WARN[1:]}">    Allow {tool_name}? [Y/n] </style>'))
            return answer.strip().lower() not in ("n", "no")
        except (EOFError, KeyboardInterrupt):
            return False

    def on_choice(options):
        """Prompt user for choice selection."""
        return _render_choices(options)

    events = session.run(
        user_msg,
        on_permission=on_permission if not session.auto_approve else None,
        on_choice=on_choice,
        images=images,
    )

    consume_events(events, on_permission=on_permission)


# ══════════════════════════════════════════════════════════════════════
#  Remote mode runner (WebSocket client)
# ══════════════════════════════════════════════════════════════════════

def run_remote(ws_url: str):
    """
    Connect to a remote Resonant Engine server and run the TUI.
    """
    try:
        import websockets
        import asyncio
    except ImportError:
        console.print(f"  [{C_ERR}]{G_CROSS} websockets package required for remote mode[/{C_ERR}]")
        console.print(f"  [{C_DIM}]Install with: pip install websockets[/{C_DIM}]")
        return

    async def _connect():
        async with websockets.connect(ws_url) as ws:
            console.print(f"  [{C_OK}]{G_CHECK} Connected to {ws_url}[/{C_OK}]")

            # Receive initial status
            raw = await ws.recv()
            event = json.loads(raw)
            _render_status(event)
            console.print()

            history = FileHistory(str(Path.home() / ".resonant_agent_history"))

            while True:
                try:
                    cwd_short = Path(os.getcwd()).name
                    user_input = pt_prompt(
                        HTML(f'<style fg="#{C_BRAND[1:]}"><b>{cwd_short}</b></style> <style fg="#{C_BRAND2[1:]}">{G_PROMPT}</style> '),
                        history=history,
                    ).strip()
                except (EOFError, KeyboardInterrupt):
                    console.print(f"\n  [{C_DIM}]Goodbye[/{C_DIM}]")
                    break

                if not user_input:
                    continue

                if user_input.startswith("/"):
                    cmd = user_input.split()[0].lower()
                    if cmd in ("/quit", "/exit", "/q"):
                        break
                    elif cmd == "/clear":
                        await ws.send(json.dumps({"command": "clear"}))
                        console.clear()
                        continue

                # Send message
                await ws.send(json.dumps({
                    "command": "message",
                    "text": user_input,
                }))

                # Consume events
                while True:
                    raw = await ws.recv()
                    event = json.loads(raw)

                    etype = event.get("event", "")

                    if etype == EngineEvent.TEXT_DELTA.value:
                        sys.stdout.write(event.get("delta", ""))
                        sys.stdout.flush()
                    elif etype == EngineEvent.SESSION_END.value:
                        total = event.get("total_elapsed", 0)
                        steps = event.get("total_steps", 0)
                        if steps > 1:
                            console.print(f"\n  [{C_DIM}]{G_CHECK} Done · {total:.1f}s · {steps} steps[/{C_DIM}]")
                        break
                    elif etype == EngineEvent.ERROR.value:
                        console.print(f"\n  [{C_ERR}]{G_CROSS} {event.get('message', '')}[/{C_ERR}]")
                        if "step limit" in event.get("message", ""):
                            break
                    else:
                        # Pass through to renderer
                        if etype == EngineEvent.STEP_START.value:
                            _render_step_start(event)
                        elif etype == EngineEvent.TOOL_CALL.value:
                            console.print()
                            _render_tool_call(event)
                        elif etype == EngineEvent.TOOL_RESULT.value:
                            _render_tool_result(event)
                        elif etype == EngineEvent.STATUS.value:
                            _render_status(event)

    asyncio.run(_connect())


# ══════════════════════════════════════════════════════════════════════
#  Plan mode handler
# ══════════════════════════════════════════════════════════════════════

def _handle_plan_approval(session: Session) -> Optional[str]:
    """After plan generation, prompt user for approval. Returns next action."""
    console.print()
    console.print(f"  [{C_BORDER}]{G_DASH * 50}[/{C_BORDER}]")
    try:
        answer = pt_prompt(
            HTML(f'<style fg="#{C_WARN[1:]}" bg="">  Execute this plan? </style><style fg="#888888"> [Y/n/edit] </style>'),
        ).strip().lower()
    except (EOFError, KeyboardInterrupt):
        console.print(f"  [{C_WARN}]{G_CROSS} Plan cancelled[/{C_WARN}]")
        return None

    if answer in ("n", "no"):
        console.print(f"  [{C_WARN}]{G_CROSS} Plan rejected — ask a new question or refine your request[/{C_WARN}]")
        return None
    elif answer in ("e", "edit"):
        try:
            refinement = pt_prompt(
                HTML(f'<style fg="#{C_BRAND[1:]}">  Refinement: </style>'),
            ).strip()
        except (EOFError, KeyboardInterrupt):
            console.print(f"  [{C_WARN}]{G_CROSS} Cancelled[/{C_WARN}]")
            return None
        if refinement:
            return f"edit:{refinement}"
        console.print(f"  [{C_DIM}]No changes — executing original plan[/{C_DIM}]")

    console.print(f"  [{C_OK}]{G_CHECK} Plan approved — executing[/{C_OK}]")
    return "approved"


# ══════════════════════════════════════════════════════════════════════
#  Main entry point
# ══════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description="Resonant Code Agent — Agentic Coding TUI",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Modes:
  %(prog)s                                        # Embedded mode (default)
  %(prog)s serve [--port 8765]                    # Start engine server
  %(prog)s connect ws://host:port                 # Connect to remote engine

Examples:
  %(prog)s --backend ollama --model llama3.1:70b   # Use specific Ollama model
  %(prog)s --backend claude                        # Use Claude API
  %(prog)s --backend openai                        # Use OpenAI API
  %(prog)s --ollama-url http://10.0.0.133:11434   # Ollama on LAN
  %(prog)s --dir ~/projects/myapp                 # Set working directory
""",
    )

    # Subcommands
    subparsers = parser.add_subparsers(dest="mode")

    # Serve mode
    serve_parser = subparsers.add_parser("serve", help="Start engine server")
    serve_parser.add_argument("--host", type=str, default="0.0.0.0")
    serve_parser.add_argument("--port", type=int, default=8765)

    # Connect mode
    connect_parser = subparsers.add_parser("connect", help="Connect to remote engine")
    connect_parser.add_argument(
        "url",
        nargs="?",
        type=str,
        help="WebSocket URL (ws://host:port). Defaults to saved network.remote_engine_ws_url or RESONANT_ENGINE_WS_URL.",
    )

    # Common args (for embedded and serve modes)
    parser.add_argument("--backend", type=str, choices=["ollama", "resonant", "claude", "openai", "lmstudio", "auto"], default="auto")
    parser.add_argument("--model", type=str, default=None)
    parser.add_argument("--api", type=str, default=None)
    parser.add_argument("--ollama-url", type=str, default=None)
    parser.add_argument("--lmstudio-url", type=str, default=None,
                        help="LM Studio API URL (e.g. http://192.168.1.50:1234/v1)")
    parser.add_argument("--dir", type=str, default=None)
    parser.add_argument("--max-tokens", type=int, default=4096)
    parser.add_argument("--max-steps", type=int, default=25)
    parser.add_argument("--approve", action="store_true")
    parser.add_argument("--auto-plan", action="store_true",
                        help="Automatically enable plan mode for complex requests")

    args = parser.parse_args()

    logging.basicConfig(level=logging.WARNING)

    if args.dir:
        os.chdir(args.dir)

    # ── Connect mode ──
    if args.mode == "connect":
        run_remote(resolve_remote_engine_ws_url(args.url))
        return

    # ── Resolve URLs and detect backends ──
    api_url = resolve_resonant_api_url(args.api)
    ollama_url = (args.ollama_url or os.environ.get("OLLAMA_URL", os.environ.get("OLLAMA_HOST", "http://10.0.0.133:11434"))).rstrip("/")
    lmstudio_url = (args.lmstudio_url or os.environ.get("LMSTUDIO_URL"))

    console.print(f"\n  [{C_DIM}]{G_THINK} Scanning backends[/{C_DIM}]")
    available = _detect_backends(api_url, ollama_url, lmstudio_url)

    if not available:
        console.print()
        console.print(f"  [{C_ERR}]{G_CROSS} No backends found[/{C_ERR}]")
        console.print(f"  [{C_BORDER}]{G_DASH * 50}[/{C_BORDER}]")
        console.print(f"  [{C_DIM}]Checked:[/{C_DIM}]")
        console.print(f"    [{C_DIM}]Resonant Engine  {api_url}[/{C_DIM}]")
        console.print(f"    [{C_DIM}]Ollama           {ollama_url}[/{C_DIM}]")
        if lmstudio_url:
            console.print(f"    [{C_DIM}]LM Studio        {lmstudio_url}[/{C_DIM}]")
        console.print()
        console.print(f"  [{C_DIM}]Start a backend or specify URLs:[/{C_DIM}]")
        console.print(f"    [{C_TEXT}]resonant --api http://<host>:8000[/{C_TEXT}]")
        console.print(f"    [{C_TEXT}]resonant --ollama-url http://<host>:11434[/{C_TEXT}]")
        console.print(f"    [{C_TEXT}]resonant --lmstudio-url http://<host>:1234[/{C_TEXT}]")
        console.print()
        return

    # ── Select backend ──
    backend = None
    health_info = None

    if args.backend == "auto":
        preferred_backend = get_default_backend()
        preferred_model = get_default_model()
        if preferred_backend and preferred_backend in available:
            chosen = preferred_backend
            args.model = args.model or preferred_model or args.model
        elif len(available) > 1:
            console.print()
            console.print(f"  [{C_BRAND} bold]Backends[/{C_BRAND} bold]")
            console.print(f"  [{C_BORDER}]{G_DASH * 40}[/{C_BORDER}]")
            options = []
            for key in ["resonant", "ollama", "lmstudio", "claude", "openai"]:
                if key not in available:
                    continue
                i = len(options) + 1
                label = _BACKEND_LABELS.get(key, key)
                detail = ""
                if key == "resonant":
                    h = available["resonant"]["health"]
                    patterns = h.get("memory_patterns", 0)
                    detail = f"{patterns:,} patterns"
                elif key == "ollama":
                    detail = f"{len(available['ollama']['models'])} models"
                elif key == "claude":
                    detail = f"{len(available['claude']['models'])} models"
                elif key == "openai":
                    detail = f"{len(available['openai']['models'])} models"
                console.print(f"    [{C_DIM}]{i}.[/{C_DIM}] [{C_TEXT}]{label}[/{C_TEXT}]  [{C_DIM}]{detail}[/{C_DIM}]")
                options.append(key)

            console.print()
            try:
                answer = pt_prompt(
                    HTML(f'<style fg="#{C_BRAND[1:]}">  Select [1-{len(options)}]: </style>'),
                ).strip()
                idx = int(answer) - 1
                if 0 <= idx < len(options):
                    chosen = options[idx]
                else:
                    chosen = options[0]
            except (EOFError, KeyboardInterrupt, ValueError):
                chosen = options[0]
        elif len(available) == 1:
            chosen = list(available.keys())[0]
        else:
            chosen = "ollama"
    else:
        chosen = args.backend
        if chosen not in available:
            hint = ""
            if chosen == "claude" and not os.environ.get("ANTHROPIC_API_KEY"):
                hint = f"\n  [{C_DIM}]Set ANTHROPIC_API_KEY and install: pip install resonant-client[claude][/{C_DIM}]"
            elif chosen == "openai" and not os.environ.get("OPENAI_API_KEY"):
                hint = f"\n  [{C_DIM}]Set OPENAI_API_KEY and install: pip install resonant-client[openai][/{C_DIM}]"
            elif chosen == "lmstudio":
                hint = f"\n  [{C_DIM}]Use --lmstudio-url or set LMSTUDIO_URL (e.g. http://192.168.1.50:1234)[/{C_DIM}]"
            console.print(f"\n  [{C_ERR}]{G_CROSS} Backend '{chosen}' not available[/{C_ERR}]{hint}")
            if available:
                fallback = list(available.keys())[0]
                console.print(f"  [{C_DIM}]{G_ARROW} Falling back to {fallback}[/{C_DIM}]")
                chosen = fallback
            else:
                return

    # ── Create backend ──
    if chosen == "resonant":
        backend = create_backend("resonant", available["resonant"]["url"])
        health_info = backend.health()
    elif chosen == "ollama":
        ollama_info = available["ollama"]
        model = args.model
        if not model:
            model = _select_model_interactive(ollama_info["models"])
        elif model not in ollama_info["models"]:
            console.print(f"  [{C_WARN}]{G_CROSS} Model '{model}' not found[/{C_WARN}]")
            model = _select_model_interactive(ollama_info["models"])
        backend = create_backend("ollama", ollama_info["url"], model=model)
        health_info = backend.health()
        console.print(f"  [{C_DIM}]{G_THINK} Warming up {model}[/{C_DIM}]")
        backend.warm_up()
    elif chosen == "claude":
        claude_info = available["claude"]
        model = args.model
        if not model:
            model = _select_model_interactive(claude_info["models"])
        elif model not in claude_info["models"]:
            console.print(f"  [{C_WARN}]{G_CROSS} Model '{model}' not found[/{C_WARN}]")
            model = _select_model_interactive(claude_info["models"])
        backend = create_backend("claude", api_key=claude_info["api_key"], model=model)
        health_info = backend.health()
    elif chosen == "openai":
        openai_info = available["openai"]
        model = args.model
        if not model:
            model = _select_model_interactive(openai_info["models"])
        elif model not in openai_info["models"]:
            console.print(f"  [{C_WARN}]{G_CROSS} Model '{model}' not found[/{C_WARN}]")
            model = _select_model_interactive(openai_info["models"])
        backend = create_backend("openai", api_key=openai_info["api_key"], model=model)
        health_info = backend.health()
    elif chosen == "lmstudio":
        lms_info = available["lmstudio"]
        model = args.model
        if not model:
            model = _select_model_interactive(lms_info["models"])
        elif model not in lms_info["models"]:
            console.print(f"  [{C_WARN}]{G_CROSS} Model '{model}' not found[/{C_WARN}]")
            model = _select_model_interactive(lms_info["models"])
        backend = create_backend("lmstudio", api_key="lm-studio", model=model,
                                 base_url=lms_info["base_url"])
        health_info = backend.health()

    # ── Serve mode ──
    if args.mode == "serve":
        import asyncio
        from .engine.server import EngineServer
        server = EngineServer(
            backend=backend,
            host=args.host if hasattr(args, 'host') else "0.0.0.0",
            port=args.port if hasattr(args, 'port') else 8765,
            max_steps=args.max_steps,
            max_tokens=args.max_tokens,
        )
        try:
            asyncio.run(server.start())
        except KeyboardInterrupt:
            console.print(f"\n  [{C_DIM}]Server stopped[/{C_DIM}]")
        return

    # ── Embedded mode (default) ──
    print_banner(backend=backend, health_info=health_info)

    session = Session(
        backend=backend,
        max_steps=args.max_steps,
        max_tokens=args.max_tokens,
        auto_approve=not args.approve,
        auto_plan=args.auto_plan,
    )

    history = FileHistory(str(Path.home() / ".resonant_agent_history"))
    plan_mode = False
    pending_images = []  # List of (image_bytes, media_type) for multimodal

    # ── Ctrl+V keybinding for image paste ──
    from prompt_toolkit.keys import Keys
    kb = KeyBindings()

    @kb.add(Keys.ControlV)
    def _paste_image(event):
        """Try clipboard image first, fall back to text paste."""
        nonlocal pending_images
        try:
            from resonant_client.engine.clipboard import read_clipboard_image, image_size_label
            img_bytes, media_type = read_clipboard_image()
            if img_bytes:
                pending_images.append((img_bytes, media_type))
                size = image_size_label(img_bytes)
                console.print(f"  [{C_OK}]📎 Image pasted ({size})[/{C_OK}]")
                return
        except Exception:
            pass
        # Fall back to text paste
        event.current_buffer.paste_clipboard_data(
            event.app.clipboard.get_data()
        )

    while True:
        try:
            cwd_short = Path(os.getcwd()).name
            mode_indicator = '<style fg="ansiyellow"> plan </style>' if plan_mode else ""
            img_indicator = f'<style fg="ansigreen"> 📎{len(pending_images)} </style>' if pending_images else ""
            user_input = pt_prompt(
                HTML(f'{img_indicator}{mode_indicator}<style fg="#{C_BRAND[1:]}"><b>{cwd_short}</b></style> <style fg="#{C_BRAND2[1:]}">{G_PROMPT}</style> '),
                history=history,
                multiline=False,
                key_bindings=kb,
            ).strip()
        except (EOFError, KeyboardInterrupt):
            console.print(f"\n  [{C_DIM}]Goodbye[/{C_DIM}]")
            break

        if not user_input:
            continue

        # ── Slash commands ──
        if user_input.startswith("/"):
            parts = user_input.split(maxsplit=1)
            cmd = parts[0].lower()
            rest = parts[1] if len(parts) > 1 else ""

            if cmd in ("/quit", "/exit", "/q"):
                console.print(f"  [{C_DIM}]Goodbye[/{C_DIM}]")
                break

            elif cmd == "/cd":
                if rest:
                    try:
                        os.chdir(rest)
                        console.print(f"  [{C_FILE}]{G_ARROW} {os.getcwd()}[/{C_FILE}]")
                    except Exception as e:
                        console.print(f"  [{C_ERR}]{G_CROSS} {e}[/{C_ERR}]")
                else:
                    console.print(f"  [{C_FILE}]{os.getcwd()}[/{C_FILE}]")

            elif cmd == "/clear":
                session.clear()
                console.clear()
                print_banner(backend=backend, health_info=health_info)

            elif cmd == "/status":
                try:
                    health = session.backend.health()
                    table = Table(show_header=False, border_style="dim", padding=(0, 1))
                    table.add_column(style=C_BRAND)
                    table.add_column()
                    for k, v in health.items():
                        if k == "available_models":
                            table.add_row(k, ", ".join(v) if isinstance(v, list) else str(v))
                        else:
                            table.add_row(k, str(v))
                    console.print(table)
                except Exception as e:
                    console.print(f"  [{C_ERR}]{e}[/{C_ERR}]")

            elif cmd == "/model":
                be = session.backend
                if isinstance(be, OllamaBackend):
                    models = be.list_models()
                    if models:
                        new_model = _select_model_interactive(models, current=be.model)
                        if new_model != be.model:
                            new_be = create_backend("ollama", be.base_url, model=new_model)
                            session.set_backend(new_be)
                            health_info = new_be.health()
                            console.print(f"  [{C_DIM}]{G_THINK} Warming up {new_model}[/{C_DIM}]")
                            new_be.warm_up()
                            console.print(f"  [{C_OK}]{G_CHECK} Switched to {new_model} · conversation cleared[/{C_OK}]")
                        else:
                            console.print(f"  [{C_DIM}]Keeping {be.model}[/{C_DIM}]")
                    else:
                        console.print(f"  [{C_ERR}]{G_CROSS} Could not list models[/{C_ERR}]")
                elif isinstance(be, ClaudeBackend):
                    models = be.list_models()
                    new_model = _select_model_interactive(models, current=be.model)
                    if new_model != be.model:
                        new_be = create_backend("claude", api_key=be.api_key, model=new_model)
                        session.set_backend(new_be)
                        health_info = new_be.health()
                        console.print(f"  [{C_OK}]{G_CHECK} Switched to {new_model} · conversation cleared[/{C_OK}]")
                    else:
                        console.print(f"  [{C_DIM}]Keeping {be.model}[/{C_DIM}]")
                elif isinstance(be, OpenAIBackend):
                    models = be.list_models()
                    new_model = _select_model_interactive(models, current=be.model)
                    if new_model != be.model:
                        new_be = create_backend("openai", api_key=be.api_key, model=new_model)
                        session.set_backend(new_be)
                        health_info = new_be.health()
                        console.print(f"  [{C_OK}]{G_CHECK} Switched to {new_model} · conversation cleared[/{C_OK}]")
                    else:
                        console.print(f"  [{C_DIM}]Keeping {be.model}[/{C_DIM}]")
                elif isinstance(be, ResonantBackend):
                    console.print(f"  [{C_DIM}]Resonant Engine uses its own model — use /backend to switch[/{C_DIM}]")

            elif cmd == "/backend":
                new_available = _detect_backends(api_url, ollama_url, lmstudio_url)
                if not new_available:
                    console.print(f"  [{C_ERR}]{G_CROSS} No backends available[/{C_ERR}]")
                else:
                    others = [k for k in new_available if k != session.backend.name]
                    if not others:
                        console.print(f"  [{C_DIM}]Already using {session.backend.name} — no other backend available[/{C_DIM}]")
                    elif len(others) == 1:
                        target = others[0]
                        new_be = _create_backend_from_available(target, new_available)
                        session.set_backend(new_be)
                        health_info = new_be.health()
                        console.print(f"  [{C_OK}]{G_CHECK} Switched to {target} · conversation cleared[/{C_OK}]")
                    else:
                        console.print()
                        console.print(f"  [{C_BRAND} bold]Switch Backend[/{C_BRAND} bold]")
                        console.print(f"  [{C_BORDER}]{G_DASH * 40}[/{C_BORDER}]")
                        for i, key in enumerate(others):
                            label = _BACKEND_LABELS.get(key, key)
                            console.print(f"    [{C_DIM}]{i+1}.[/{C_DIM}] [{C_TEXT}]{label}[/{C_TEXT}]")
                        console.print()
                        try:
                            answer = pt_prompt(
                                HTML(f'<style fg="#{C_BRAND[1:]}">  Select [1-{len(others)}]: </style>'),
                            ).strip()
                            idx = int(answer) - 1
                            target = others[idx] if 0 <= idx < len(others) else others[0]
                        except (EOFError, KeyboardInterrupt, ValueError):
                            target = others[0]
                        new_be = _create_backend_from_available(target, new_available)
                        session.set_backend(new_be)
                        health_info = new_be.health()
                        console.print(f"  [{C_OK}]{G_CHECK} Switched to {target} · conversation cleared[/{C_OK}]")

            elif cmd == "/plan":
                plan_mode = not plan_mode
                session.plan_mode = plan_mode
                if plan_mode:
                    console.print(f"  [{C_WARN}]{G_PLAN} Plan mode ON[/{C_WARN}]  [{C_DIM}]think first, approve, then execute[/{C_DIM}]")
                else:
                    console.print(f"  [{C_OK}]{G_STEP} Plan mode OFF[/{C_OK}]  [{C_DIM}]agent acts immediately[/{C_DIM}]")

            elif cmd == "/autoplan":
                session.auto_plan = not session.auto_plan
                if session.auto_plan:
                    console.print(f"  [{C_WARN}]{G_PLAN} Auto-plan ON[/{C_WARN}]  [{C_DIM}]complex requests auto-enable plan mode[/{C_DIM}]")
                else:
                    console.print(f"  [{C_OK}]{G_STEP} Auto-plan OFF[/{C_OK}]  [{C_DIM}]plan mode is manual only[/{C_DIM}]")

            elif cmd == "/approve":
                if rest.lower() in ("on", "true", "yes"):
                    session.auto_approve = False
                    console.print(f"  [{C_WARN}]{G_DOT} Approval ON[/{C_WARN}]  [{C_DIM}]will ask before each tool[/{C_DIM}]")
                elif rest.lower() in ("off", "false", "no"):
                    session.auto_approve = True
                    console.print(f"  [{C_OK}]{G_DOT} Approval OFF[/{C_OK}]  [{C_DIM}]auto-execute[/{C_DIM}]")
                else:
                    state = "ON" if not session.auto_approve else "OFF"
                    console.print(f"  [{C_DIM}]Approval: {state} · use /approve on|off[/{C_DIM}]")

            elif cmd == "/help":
                backend_desc = f"{session.backend.name}"
                if hasattr(session.backend, "model"):
                    backend_desc += f" · {session.backend.model}"

                console.print()
                console.print(f"  [{C_BRAND}]{G_SPLIT}[/{C_BRAND}] [{C_BRAND} bold]Resonant Code Agent[/{C_BRAND} bold]  [{C_DIM}]{backend_desc}[/{C_DIM}]")
                console.print(f"  [{C_DIMMER}]{G_DASH * 55}[/{C_DIMMER}]")
                console.print()
                console.print(f"  [{C_BRAND2}]Commands[/{C_BRAND2}]")
                console.print(f"    [{C_TEXT}]/help[/{C_TEXT}]             [{C_MUTED}]this help[/{C_MUTED}]")
                console.print(f"    [{C_TEXT}]/plan[/{C_TEXT}]             [{C_MUTED}]toggle plan mode (think → approve → act)[/{C_MUTED}]")
                console.print(f"    [{C_TEXT}]/autoplan[/{C_TEXT}]         [{C_MUTED}]toggle auto-plan (classify complexity first)[/{C_MUTED}]")
                console.print(f"    [{C_TEXT}]/model[/{C_TEXT}]            [{C_MUTED}]switch model[/{C_MUTED}]")
                console.print(f"    [{C_TEXT}]/backend[/{C_TEXT}]          [{C_MUTED}]switch backend (Ollama, Claude, OpenAI, Resonant)[/{C_MUTED}]")
                console.print(f"    [{C_TEXT}]/cd[/{C_TEXT}] <dir>         [{C_MUTED}]change directory[/{C_MUTED}]")
                console.print(f"    [{C_TEXT}]/clear[/{C_TEXT}]            [{C_MUTED}]reset conversation[/{C_MUTED}]")
                console.print(f"    [{C_TEXT}]/status[/{C_TEXT}]           [{C_MUTED}]backend status[/{C_MUTED}]")
                console.print(f"    [{C_TEXT}]/approve[/{C_TEXT}] on|off   [{C_MUTED}]tool approval[/{C_MUTED}]")
                console.print(f"    [{C_TEXT}]/quit[/{C_TEXT}]             [{C_MUTED}]exit[/{C_MUTED}]")
                console.print()
                console.print(f"  [{C_BRAND2}]Architecture[/{C_BRAND2}]")
                console.print(f"    [{C_TEXT}]resonant[/{C_TEXT}]                  [{C_MUTED}]embedded engine + TUI (default)[/{C_MUTED}]")
                console.print(f"    [{C_TEXT}]resonant serve[/{C_TEXT}]             [{C_MUTED}]run engine as WebSocket server[/{C_MUTED}]")
                console.print(f"    [{C_TEXT}]resonant connect ws://...[/{C_TEXT}]  [{C_MUTED}]TUI client → remote engine[/{C_MUTED}]")
                console.print()
                console.print(f"  [{C_BRAND2}]Tips[/{C_BRAND2}]")
                console.print(f'    [{C_MUTED}]Ask naturally — "Build a REST API with auth"[/{C_MUTED}]')
                console.print(f'    [{C_MUTED}]Be specific — "Edit main.py to add error handling"[/{C_MUTED}]')
                console.print(f"    [{C_MUTED}]Ctrl+C to interrupt · /clear to reset · /plan to think first[/{C_MUTED}]")
                console.print()

            else:
                console.print(f"  [{C_DIM}]Unknown: {cmd} · try /help[/{C_DIM}]")
            continue

        # ── Run agent ──
        session.plan_mode = plan_mode

        # Grab any pending images for this message
        images_for_msg = pending_images.copy() if pending_images else None
        pending_images.clear()

        # Auto-plan: classify request complexity and temporarily enable plan mode
        auto_planned = False
        if session.auto_plan and not plan_mode:
            console.print(f"  [{C_DIM}]{G_THINK} Classifying request...[/{C_DIM}]", end="")
            if session.should_plan(user_input):
                auto_planned = True
                plan_mode = True
                session.plan_mode = True
                console.print(f"\r  [{C_WARN}]{G_PLAN} Complex request detected — auto-planning[/{C_WARN}]")
            else:
                console.print(f"\r  [{C_DIM}]{G_STEP} Simple request — acting directly         [/{C_DIM}]")

        if plan_mode:
            # Plan mode: generate plan, get approval, then execute
            run_embedded(session, user_input, images=images_for_msg)

            result = _handle_plan_approval(session)
            if result is None:
                if auto_planned:
                    plan_mode = False
                    session.plan_mode = False
                continue
            elif result.startswith("edit:"):
                refinement = result[5:]
                session.conversation_history.append({"role": "user", "content": f"Revise the plan: {refinement}"})
                run_embedded(session, f"Revise the plan: {refinement}")
                result = _handle_plan_approval(session)
                if result is None:
                    if auto_planned:
                        plan_mode = False
                        session.plan_mode = False
                    continue

            # Execute the approved plan
            session.plan_mode = False
            session.conversation_history.append({"role": "user", "content": "Approved. Execute the plan now. Start with step 1."})
            run_embedded(session, "Approved. Execute the plan now. Start with step 1.")
            # Restore: if auto-planned, turn plan_mode back off; otherwise keep manual state
            if auto_planned:
                plan_mode = False
            session.plan_mode = plan_mode
        else:
            run_embedded(session, user_input, images=images_for_msg)


if __name__ == "__main__":
    main()
