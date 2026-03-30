"""
Tool definitions and execution for the Resonant Engine.

Extracted from tui.py — this is the "hands" of the agent.
Tools run server-side (same machine as the engine).
"""

import json
import os
import re
import subprocess
import sys
import time
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Optional


# ── Tool Definitions (OpenAI function-calling format) ──────────────────

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
    {
        "type": "function",
        "function": {
            "name": "task",
            "description": (
                "Spawn a sub-agent to handle a subtask independently. The sub-agent gets its "
                "own context window and runs to completion, then returns its result. Use 'explore' "
                "for fast read-only codebase searches, 'plan' for analysis without modification, "
                "'build' for tasks that require writing/editing code."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "prompt": {
                        "type": "string",
                        "description": "Clear description of the task for the sub-agent"
                    },
                    "agent_type": {
                        "type": "string",
                        "enum": ["build", "explore", "plan"],
                        "description": "Type of agent: 'explore' (fast, read-only), 'plan' (analyze, no edits), 'build' (full coding)"
                    },
                },
                "required": ["prompt", "agent_type"]
            }
        }
    },
    # ── Browser tools (Playwright) ──────────────────────────────────
    {
        "type": "function",
        "function": {
            "name": "browser_navigate",
            "description": "Navigate the browser to a URL. Auto-launches Chromium if no browser is open. Use this to open web pages, follow links, or go to specific URLs.",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "The URL to navigate to (e.g. 'https://example.com')"
                    }
                },
                "required": ["url"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "browser_click",
            "description": "Click an element in the browser page. Can target by visible text, CSS selector, or x/y coordinates.",
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {
                        "type": "string",
                        "description": "Visible text of the element to click (most natural approach)"
                    },
                    "selector": {
                        "type": "string",
                        "description": "CSS selector of the element to click"
                    },
                    "x": {"type": "integer", "description": "X coordinate to click"},
                    "y": {"type": "integer", "description": "Y coordinate to click"}
                }
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "browser_type",
            "description": "Type text into a form field or the currently focused element in the browser.",
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {
                        "type": "string",
                        "description": "Text to type"
                    },
                    "selector": {
                        "type": "string",
                        "description": "CSS selector of the input field (optional — types into focused element if omitted)"
                    },
                    "clear": {
                        "type": "boolean",
                        "description": "Clear the field before typing (default: false)"
                    },
                    "submit": {
                        "type": "boolean",
                        "description": "Press Enter after typing (default: false)"
                    }
                },
                "required": ["text"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "browser_read",
            "description": "Read page content from the browser. Returns text, HTML, or accessibility tree.",
            "parameters": {
                "type": "object",
                "properties": {
                    "mode": {
                        "type": "string",
                        "enum": ["text", "html", "accessibility"],
                        "description": "What to read: 'text' (visible text), 'html' (raw HTML), 'accessibility' (a11y tree). Default: text"
                    },
                    "selector": {
                        "type": "string",
                        "description": "CSS selector to scope the read (optional — reads whole page if omitted)"
                    }
                }
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "browser_screenshot",
            "description": "Take a screenshot of the current browser page. Returns a PNG image.",
            "parameters": {
                "type": "object",
                "properties": {
                    "full_page": {
                        "type": "boolean",
                        "description": "Capture the full scrollable page (default: false, viewport only)"
                    }
                }
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "browser_js",
            "description": "Execute JavaScript code in the browser page context. Returns the result.",
            "parameters": {
                "type": "object",
                "properties": {
                    "code": {
                        "type": "string",
                        "description": "JavaScript code to execute in the page"
                    }
                },
                "required": ["code"]
            }
        }
    },
    # ── Desktop / Computer Use tools ─────────────────────────────────
    {
        "type": "function",
        "function": {
            "name": "computer_screenshot",
            "description": "Take a screenshot of the desktop. Returns a PNG image of the primary monitor or a specified region.",
            "parameters": {
                "type": "object",
                "properties": {
                    "region": {
                        "type": "object",
                        "description": "Optional region to capture: {x, y, width, height}",
                        "properties": {
                            "x": {"type": "integer"},
                            "y": {"type": "integer"},
                            "width": {"type": "integer"},
                            "height": {"type": "integer"}
                        }
                    }
                }
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "computer_click",
            "description": "Click at a specific position on the desktop screen. Automatically captures a follow-up screenshot so you can see the result.",
            "parameters": {
                "type": "object",
                "properties": {
                    "x": {"type": "integer", "description": "X coordinate on screen"},
                    "y": {"type": "integer", "description": "Y coordinate on screen"},
                    "button": {
                        "type": "string",
                        "enum": ["left", "right", "middle"],
                        "description": "Mouse button (default: left)"
                    },
                    "clicks": {
                        "type": "integer",
                        "description": "Number of clicks (1=single, 2=double). Default: 1"
                    },
                    "screenshot": {
                        "type": "boolean",
                        "description": "Take a follow-up screenshot after clicking (default: true)"
                    }
                },
                "required": ["x", "y"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "computer_type",
            "description": "Type text or press key combinations on the desktop. Use 'text' for typing strings, 'key' for hotkeys like 'ctrl+s' or 'enter'. Automatically captures a follow-up screenshot.",
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {
                        "type": "string",
                        "description": "Text to type character by character"
                    },
                    "key": {
                        "type": "string",
                        "description": "Key or key combo to press (e.g. 'enter', 'ctrl+s', 'alt+tab')"
                    },
                    "hotkey": {
                        "type": "string",
                        "description": "Alias for 'key' — key combo to press"
                    },
                    "screenshot": {
                        "type": "boolean",
                        "description": "Take a follow-up screenshot after typing (default: true)"
                    }
                }
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "computer_scroll",
            "description": "Scroll the mouse wheel at a position on the desktop. Automatically captures a follow-up screenshot.",
            "parameters": {
                "type": "object",
                "properties": {
                    "x": {"type": "integer", "description": "X coordinate (optional)"},
                    "y": {"type": "integer", "description": "Y coordinate (optional)"},
                    "direction": {
                        "type": "string",
                        "enum": ["up", "down"],
                        "description": "Scroll direction (default: down)"
                    },
                    "amount": {
                        "type": "integer",
                        "description": "Number of scroll clicks (default: 3)"
                    },
                    "screenshot": {
                        "type": "boolean",
                        "description": "Take a follow-up screenshot after scrolling (default: true)"
                    }
                }
            }
        }
    },
    # ── Enhanced Computer Use tools ──────────────────────────────────
    {
        "type": "function",
        "function": {
            "name": "computer_drag",
            "description": "Drag from one screen position to another. Use for moving windows, selecting text, drag-and-drop.",
            "parameters": {
                "type": "object",
                "properties": {
                    "start_x": {"type": "integer", "description": "Starting X coordinate"},
                    "start_y": {"type": "integer", "description": "Starting Y coordinate"},
                    "end_x": {"type": "integer", "description": "Ending X coordinate"},
                    "end_y": {"type": "integer", "description": "Ending Y coordinate"},
                    "button": {
                        "type": "string",
                        "enum": ["left", "right", "middle"],
                        "description": "Mouse button (default: left)"
                    },
                    "duration": {
                        "type": "number",
                        "description": "Drag duration in seconds (default: 0.5)"
                    }
                },
                "required": ["start_x", "start_y", "end_x", "end_y"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "computer_hover",
            "description": "Move the mouse cursor to a position without clicking. Use to trigger hover menus, tooltips, or preview effects.",
            "parameters": {
                "type": "object",
                "properties": {
                    "x": {"type": "integer", "description": "X coordinate"},
                    "y": {"type": "integer", "description": "Y coordinate"},
                    "duration": {
                        "type": "number",
                        "description": "Movement duration in seconds (default: 0.3)"
                    }
                },
                "required": ["x", "y"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "window_list",
            "description": "List all visible windows on the desktop with their titles, positions, and sizes.",
            "parameters": {
                "type": "object",
                "properties": {}
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "window_focus",
            "description": "Bring a window to the foreground by matching its title. Uses substring match.",
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {
                        "type": "string",
                        "description": "Partial window title to match (e.g. 'Chrome', 'Visual Studio')"
                    }
                },
                "required": ["title"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "computer_wait",
            "description": "Wait for the screen to change or pause for a duration. Use 'change' mode after clicking something that triggers a load.",
            "parameters": {
                "type": "object",
                "properties": {
                    "mode": {
                        "type": "string",
                        "enum": ["duration", "change"],
                        "description": "'duration' = wait N seconds; 'change' = wait until screen changes (default: duration)"
                    },
                    "seconds": {
                        "type": "number",
                        "description": "Seconds to wait (duration mode, default: 1.0)"
                    },
                    "timeout": {
                        "type": "number",
                        "description": "Max seconds to wait for change (change mode, default: 10)"
                    }
                }
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "screen_ocr",
            "description": "Extract text from the screen using OCR. Useful for reading text that's in images, non-selectable UI, or desktop applications.",
            "parameters": {
                "type": "object",
                "properties": {
                    "region": {
                        "type": "object",
                        "description": "Optional region to OCR: {x, y, width, height}. Omit for full screen.",
                        "properties": {
                            "x": {"type": "integer"},
                            "y": {"type": "integer"},
                            "width": {"type": "integer"},
                            "height": {"type": "integer"}
                        }
                    }
                }
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "open_application",
            "description": "Open a desktop application by name. Cross-platform: uses 'start' on Windows, 'open -a' on macOS, direct exec on Linux.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Application name or path (e.g. 'chrome', 'notepad', 'Firefox', 'code')"
                    }
                },
                "required": ["name"]
            }
        }
    },
    # ── Batch tool ────────────────────────────────────────────────────
    {
        "type": "function",
        "function": {
            "name": "batch",
            "description": (
                "Execute multiple tool calls in parallel. Use when you need to read several "
                "files at once, search for multiple patterns, or run independent operations "
                "concurrently. Maximum 25 calls. Cannot batch 'task' or 'batch' tools."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "calls": {
                        "type": "array",
                        "description": "Array of tool calls to execute in parallel",
                        "items": {
                            "type": "object",
                            "properties": {
                                "name": {
                                    "type": "string",
                                    "description": "Tool name (file_read, glob, grep, bash, file_write, file_edit)"
                                },
                                "arguments": {
                                    "type": "object",
                                    "description": "Tool arguments"
                                }
                            },
                            "required": ["name", "arguments"]
                        }
                    }
                },
                "required": ["calls"]
            }
        }
    },
]


# ── Tool-specific icons (inspired by opencode) ────────────────────────

TOOL_ICONS = {
    "file_read":  "→",   # Read arrow
    "file_write": "←",   # Write arrow
    "file_edit":  "~",   # Edit tilde
    "bash":       "$",   # Shell prompt
    "glob":       "✱",   # Glob star
    "grep":       "/",   # Search slash
    "task":       "│",   # Sub-agent
    "batch":      "⚡",   # Parallel execution
    # Browser tools
    "browser_navigate":   "⊕",
    "browser_click":      "◎",
    "browser_type":       "⌨",
    "browser_read":       "◫",
    "browser_screenshot": "◰",
    "browser_js":         "⟐",
    # Desktop / Computer Use tools
    "computer_screenshot": "▣",
    "computer_click":      "◎",
    "computer_type":       "⌨",
    "computer_scroll":     "↕",
    "computer_drag":       "↗",
    "computer_hover":      "⊙",
    "computer_wait":       "⏳",
    "window_list":         "☰",
    "window_focus":        "◉",
    "screen_ocr":          "🔍",
    "open_application":    "▶",
}


def get_tool_icon(name: str) -> str:
    """Get the display icon for a tool."""
    return TOOL_ICONS.get(name, "⚙")


# ── Tool Execution ─────────────────────────────────────────────────────

class ToolResult:
    """Result of executing a tool."""
    __slots__ = ("output", "is_error", "elapsed", "metadata")

    def __init__(self, output: str, is_error: bool = False, elapsed: float = 0.0, metadata: Optional[dict] = None):
        self.output = output
        self.is_error = is_error
        self.elapsed = elapsed
        self.metadata = metadata or {}

    def to_dict(self) -> dict:
        return {
            "output": self.output,
            "is_error": self.is_error,
            "elapsed": self.elapsed,
            "metadata": self.metadata,
        }


def execute_tool(name: str, arguments: dict, cancel_event: Optional[threading.Event] = None) -> ToolResult:
    """
    Execute a tool and return structured result.

    This is the pure execution layer — no display logic, no approval prompts.
    The engine handles permission; the TUI handles display.

    Note: 'task' tool is handled by Session (needs backend access), not here.
    """
    start = time.time()

    try:
        if name == "bash":
            return _exec_bash(arguments, start, cancel_event=cancel_event)
        elif name == "file_write":
            return _exec_file_write(arguments, start)
        elif name == "file_read":
            return _exec_file_read(arguments, start)
        elif name == "file_edit":
            return _exec_file_edit(arguments, start)
        elif name == "glob":
            return _exec_glob(arguments, start)
        elif name == "grep":
            return _exec_grep(arguments, start, cancel_event=cancel_event)
        elif name == "batch":
            return _exec_batch(arguments, start, cancel_event=cancel_event)
        elif name == "task":
            # Task tool requires session context — handled by Session, not here
            return ToolResult(
                "Error: 'task' tool must be executed through Session (needs backend access).",
                is_error=True, elapsed=time.time() - start,
            )
        # Browser tools (Playwright)
        elif name == "browser_navigate":
            from .browser import exec_browser_navigate
            return exec_browser_navigate(arguments, start)
        elif name == "browser_click":
            from .browser import exec_browser_click
            return exec_browser_click(arguments, start)
        elif name == "browser_type":
            from .browser import exec_browser_type
            return exec_browser_type(arguments, start)
        elif name == "browser_read":
            from .browser import exec_browser_read
            return exec_browser_read(arguments, start)
        elif name == "browser_screenshot":
            from .browser import exec_browser_screenshot
            return exec_browser_screenshot(arguments, start)
        elif name == "browser_js":
            from .browser import exec_browser_js
            return exec_browser_js(arguments, start)
        # Desktop / Computer Use tools
        elif name == "computer_screenshot":
            from .computer import exec_computer_screenshot
            return exec_computer_screenshot(arguments, start)
        elif name == "computer_click":
            from .computer import exec_computer_click
            return exec_computer_click(arguments, start)
        elif name == "computer_type":
            from .computer import exec_computer_type
            return exec_computer_type(arguments, start)
        elif name == "computer_scroll":
            from .computer import exec_computer_scroll
            return exec_computer_scroll(arguments, start)
        # Enhanced Computer Use tools
        elif name == "computer_drag":
            from .computer_use import exec_computer_drag
            return exec_computer_drag(arguments, start)
        elif name == "computer_hover":
            from .computer_use import exec_computer_hover
            return exec_computer_hover(arguments, start)
        elif name == "window_list":
            from .computer_use import exec_window_list
            return exec_window_list(arguments, start)
        elif name == "window_focus":
            from .computer_use import exec_window_focus
            return exec_window_focus(arguments, start)
        elif name == "computer_wait":
            from .computer_use import exec_computer_wait
            return exec_computer_wait(arguments, start)
        elif name == "screen_ocr":
            from .computer_use import exec_screen_ocr
            return exec_screen_ocr(arguments, start)
        elif name == "open_application":
            from .computer_use import exec_open_application
            return exec_open_application(arguments, start)
        else:
            return ToolResult(f"Error: Unknown tool '{name}'", is_error=True, elapsed=time.time() - start)

    except Exception as e:
        return ToolResult(f"Error: {e}", is_error=True, elapsed=time.time() - start)


def _run_subprocess_with_cancel(
    cmd,
    *,
    timeout: float,
    shell: bool,
    text: bool,
    cwd: str,
    stdin=None,
    cancel_event: Optional[threading.Event] = None,
):
    proc = subprocess.Popen(
        cmd,
        shell=shell,
        cwd=cwd,
        stdin=stdin,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=text,
    )

    watcher = None
    if cancel_event is not None:
        def _watch_cancel():
            cancel_event.wait()
            if proc.poll() is None:
                try:
                    proc.terminate()
                except Exception:
                    pass

        watcher = threading.Thread(target=_watch_cancel, daemon=True)
        watcher.start()

    try:
        stdout, stderr = proc.communicate(timeout=timeout)
        return proc.returncode, stdout, stderr, False
    except subprocess.TimeoutExpired:
        proc.kill()
        stdout, stderr = proc.communicate()
        return proc.returncode, stdout, stderr, True
    finally:
        if watcher:
            watcher.join(timeout=0)


def _exec_bash(args: dict, start: float, cancel_event: Optional[threading.Event] = None) -> ToolResult:
    cmd = args.get("command", "")
    timeout = args.get("timeout", 30)
    cwd = args.get("cwd", os.getcwd())

    try:
        returncode, stdout, stderr, timed_out = _run_subprocess_with_cancel(
            cmd,
            shell=True,
            text=True,
            timeout=timeout,
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            cancel_event=cancel_event,
        )
        elapsed = time.time() - start
        if cancel_event is not None and cancel_event.is_set():
            return ToolResult(
                "Command cancelled.",
                is_error=True,
                elapsed=elapsed,
                metadata={"command": cmd, "cancelled": True},
            )
        if timed_out:
            return ToolResult(
                f"Command timed out after {timeout}s.",
                is_error=True,
                elapsed=timeout,
                metadata={"command": cmd, "timed_out": True},
            )
        output = stdout
        if stderr:
            output += ("\n" if output else "") + stderr
        if returncode != 0:
            output += f"\n(exit code: {returncode})"
        output = output.strip() or "(no output)"
        return ToolResult(
            output,
            is_error=returncode != 0,
            elapsed=elapsed,
            metadata={"command": cmd, "exit_code": returncode},
        )
    except subprocess.TimeoutExpired:
        return ToolResult(
            f"Command timed out after {timeout}s.",
            is_error=True,
            elapsed=timeout,
            metadata={"command": cmd, "timed_out": True},
        )


def _exec_file_write(args: dict, start: float) -> ToolResult:
    fpath = args.get("path", "")
    content = args.get("content", "")
    path = Path(fpath)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    lines = len(content.split("\n"))
    elapsed = time.time() - start
    return ToolResult(
        f"File written: {fpath} ({lines} lines, {len(content)} characters)",
        elapsed=elapsed,
        metadata={"path": fpath, "lines": lines, "chars": len(content), "content": content},
    )


def _exec_file_read(args: dict, start: float) -> ToolResult:
    fpath = args.get("path", "")
    path = Path(fpath)
    if not path.exists():
        return ToolResult(f"Error: File not found: {fpath}", is_error=True, elapsed=time.time() - start)
    content = path.read_text(encoding="utf-8")
    lines = len(content.split("\n"))
    if len(content) > 10000:
        content = content[:10000] + f"\n\n... (truncated, {len(content)} total chars)"
    elapsed = time.time() - start
    return ToolResult(
        content,
        elapsed=elapsed,
        metadata={"path": fpath, "lines": lines},
    )


def _exec_file_edit(args: dict, start: float) -> ToolResult:
    fpath = args.get("path", "")
    old_text = args.get("old_text", "")
    new_text = args.get("new_text", "")
    path = Path(fpath)

    if not path.exists():
        return ToolResult(f"Error: File not found: {fpath}", is_error=True, elapsed=time.time() - start)

    content = path.read_text(encoding="utf-8")
    if old_text not in content:
        return ToolResult(
            f"Error: old_text not found in {fpath}. The text to replace was not found.",
            is_error=True,
            elapsed=time.time() - start,
        )

    content = content.replace(old_text, new_text, 1)
    path.write_text(content, encoding="utf-8")
    elapsed = time.time() - start
    return ToolResult(
        f"File edited: {fpath}",
        elapsed=elapsed,
        metadata={"path": fpath, "old_text": old_text, "new_text": new_text},
    )


def _exec_glob(args: dict, start: float) -> ToolResult:
    pattern = args.get("pattern", "")
    base = args.get("path", ".")
    matches = sorted(Path(base).glob(pattern))[:50]
    result = "\n".join(str(m) for m in matches)
    elapsed = time.time() - start
    return ToolResult(
        result or "(no matches)",
        elapsed=elapsed,
        metadata={"pattern": pattern, "count": len(matches)},
    )


def _exec_grep(args: dict, start: float, cancel_event: Optional[threading.Event] = None) -> ToolResult:
    pattern = args.get("pattern", "")
    path = args.get("path", ".")
    file_glob = args.get("glob", "")

    if sys.platform == "win32":
        cmd = f'findstr /s /n /r "{pattern}" "{path}\\*"'
        if file_glob:
            cmd = f'findstr /s /n /r "{pattern}" "{path}\\{file_glob}"'
    else:
        cmd = f'grep -rn "{pattern}" "{path}"'
        if file_glob:
            cmd = f'grep -rn --include="{file_glob}" "{pattern}" "{path}"'

    returncode, stdout, _stderr, timed_out = _run_subprocess_with_cancel(
        cmd,
        shell=True,
        text=False,
        timeout=30,
        cwd=os.getcwd(),
        cancel_event=cancel_event,
    )

    if cancel_event is not None and cancel_event.is_set():
        return ToolResult(
            "Search cancelled.",
            is_error=True,
            elapsed=time.time() - start,
            metadata={"pattern": pattern, "cancelled": True},
        )

    if timed_out:
        return ToolResult(
            "Search timed out after 30s.",
            is_error=True,
            elapsed=30,
            metadata={"pattern": pattern, "timed_out": True},
        )

    try:
        output = stdout.decode("utf-8").strip()
    except (UnicodeDecodeError, AttributeError):
        try:
            output = stdout.decode("latin-1").strip()
        except (UnicodeDecodeError, AttributeError):
            output = str(stdout).strip()

    lines = output.split("\n") if output else []
    count = len(lines)
    if count > 30:
        output = "\n".join(lines[:30]) + f"\n... ({count} total matches)"

    elapsed = time.time() - start
    return ToolResult(
        output or "(no matches)",
        elapsed=elapsed,
        metadata={"pattern": pattern, "count": count, "exit_code": returncode},
    )


# ── Batch tool (parallel execution) ──────────────────────────────────

# Tools that cannot be batched (prevent recursion)
BATCH_FORBIDDEN = {"batch", "task"}
BATCH_MAX_CALLS = 25
BATCH_MAX_WORKERS = 10


def _exec_batch(args: dict, start: float, cancel_event: Optional[threading.Event] = None) -> ToolResult:
    """
    Execute multiple tool calls in parallel using ThreadPoolExecutor.

    - Max 25 calls per batch
    - Cannot batch 'batch' or 'task' (recursion guard)
    - Each call runs independently; one failure doesn't stop others
    - Returns aggregated results
    """
    calls = args.get("calls", [])
    if not calls:
        return ToolResult("Error: No calls provided", is_error=True, elapsed=time.time() - start)

    if cancel_event is not None and cancel_event.is_set():
        return ToolResult(
            "Batch cancelled.",
            is_error=True,
            elapsed=time.time() - start,
            metadata={"cancelled": True, "results": []},
        )

    if len(calls) > BATCH_MAX_CALLS:
        calls = calls[:BATCH_MAX_CALLS]

    results = [None] * len(calls)
    forbidden_indices = set()

    with ThreadPoolExecutor(max_workers=min(len(calls), BATCH_MAX_WORKERS)) as pool:
        futures = {}
        for i, call in enumerate(calls):
            name = call.get("name", "")
            call_args = call.get("arguments", {})

            if name in BATCH_FORBIDDEN:
                results[i] = {
                    "index": i, "name": name, "status": "error",
                    "output": f"Cannot batch '{name}' tool",
                    "elapsed": 0,
                }
                forbidden_indices.add(i)
                continue

            future = pool.submit(execute_tool, name, call_args, cancel_event)
            futures[future] = (i, name)

        for future in as_completed(futures):
            i, name = futures[future]
            try:
                result = future.result()
                results[i] = {
                    "index": i, "name": name,
                    "status": "error" if result.is_error else "success",
                    "output": result.output,
                    "elapsed": result.elapsed,
                    "metadata": result.metadata,
                }
            except Exception as e:
                results[i] = {
                    "index": i, "name": name,
                    "status": "error", "output": str(e),
                    "elapsed": 0,
                }

    # Build summary
    successes = sum(1 for r in results if r and r["status"] == "success")
    failures = len(results) - successes

    summary_lines = [f"{successes}/{len(results)} succeeded\n"]
    for r in results:
        if r:
            status_icon = "✓" if r["status"] == "success" else "✗"
            # Truncate long output
            output = r["output"]
            if len(output) > 500:
                output = output[:497] + "..."
            summary_lines.append(f"[{status_icon} {r['name']}] {output}")

    elapsed = time.time() - start
    return ToolResult(
        output="\n".join(summary_lines),
        is_error=failures > 0 or (cancel_event is not None and cancel_event.is_set()),
        elapsed=elapsed,
        metadata={
            "results": results,
            "successes": successes,
            "failures": failures,
            "total": len(results),
            "cancelled": bool(cancel_event is not None and cancel_event.is_set()),
        },
    )
