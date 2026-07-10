"""
Tool definitions and execution for the Resonant Engine.

Extracted from tui.py — this is the "hands" of the agent.
Tools run server-side (same machine as the engine).
"""

import json
import os
import subprocess
import sys
import time
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Optional

from .truncation import (
    GREP_MAX_LINE_LENGTH,
    render_truncation_footer,
    truncate_head,
    truncate_line,
    truncate_tail,
)
from .editing import EditMatchError, apply_text_edit


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
                    },
                    "allow_leading_dash": {
                        "type": "boolean",
                        "description": "Set to true ONLY when you genuinely want a filename or directory whose name starts with '-'. Default: false. Without this flag, paths whose basename or any segment begins with '-' are rejected as a foot-gun guard (e.g. tokenization slips where 'mkdir -p src' becomes three args)."
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
                    },
                    "offset": {
                        "type": "integer",
                        "description": "Zero-based line offset (default: 0). Use next_offset from a prior result to continue."
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Maximum lines to return (default: 400, maximum: 2000)."
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
            "description": (
                "Edit a file by replacing old_text with new_text. Include enough surrounding "
                "context for a unique match; minor whitespace drift is repaired automatically."
            ),
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
                    },
                    "replace_all": {
                        "type": "boolean",
                        "description": "Replace every match only when intentionally editing repeated text. Default: false."
                    },
                    "allow_leading_dash": {
                        "type": "boolean",
                        "description": "Set to true ONLY when you genuinely want a filename or directory whose name starts with '-'. Default: false."
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
                    },
                    "offset": {
                        "type": "integer",
                        "description": "Zero-based result offset for pagination (default: 0)."
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Maximum paths to return (default: 50, maximum: 200)."
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
                    },
                    "offset": {
                        "type": "integer",
                        "description": "Zero-based match offset for pagination (default: 0)."
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Maximum matches to return (default: 50, maximum: 200)."
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
    {
        "type": "function",
        "function": {
            "name": "skill_view",
            "description": (
                "Read the full procedure and verification notes for a relevant "
                "Resonant skill surfaced in the prompt."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "skill_id": {
                        "type": "string",
                        "description": "Skill identifier from the relevant-skills prompt block."
                    }
                },
                "required": ["skill_id"]
            }
        }
    },
    # v0.3.5 — `await_user` lets the agent ask the human a focused question
    # when stuck rather than burning steps on speculative searches. Pairs
    # with the cycle guards from v0.3.3: when the model would be about to
    # cycle through `dir`/`glob` variations searching for context, it can
    # instead call await_user("Where is the API code located? frontend/api
    # or backend/api?") and get a concrete answer in one step.
    {
        "type": "function",
        "function": {
            "name": "await_user",
            "description": (
                "Pause and ask the user a focused question when you genuinely "
                "need information you can't find by reading code. Use this "
                "INSTEAD of cycling through speculative searches. Examples of "
                "good uses: clarifying ambiguous requirements ('should the "
                "export include or exclude tool calls?'), choosing between "
                "valid implementation paths ('use sqlite or just JSON?'), "
                "asking where to put new files when conventions are unclear. "
                "Do NOT use for things you can answer yourself by reading code "
                "(file paths, API shapes, existing function names). The user's "
                "answer is returned as the tool result."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "question": {
                        "type": "string",
                        "description": "A specific, focused question. Bad: 'what should I do next'. Good: 'should the /export command include tool-call activity, or only user/assistant messages?'"
                    },
                    "options": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional quick-reply choices. When provided, the user sees them as one-click chips. Keep to 2-5 options. Omit for free-text questions."
                    },
                },
                "required": ["question"]
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
    {
        "type": "function",
        "function": {
            "name": "browser_scroll",
            "description": "Scroll the browser page up or down, or scroll a specific element into view.",
            "parameters": {
                "type": "object",
                "properties": {
                    "direction": {
                        "type": "string",
                        "enum": ["up", "down"],
                        "description": "Scroll direction (default: down)"
                    },
                    "amount": {
                        "type": "integer",
                        "description": "Pixels to scroll (default: 500)"
                    },
                    "selector": {
                        "type": "string",
                        "description": "CSS selector of element to scroll into view (overrides direction/amount)"
                    }
                }
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "browser_hover",
            "description": "Hover over an element to reveal tooltips, dropdowns, or hidden content.",
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {
                        "type": "string",
                        "description": "Visible text of the element to hover"
                    },
                    "selector": {
                        "type": "string",
                        "description": "CSS selector of the element to hover"
                    }
                }
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "browser_select",
            "description": "Select an option from a dropdown/select element.",
            "parameters": {
                "type": "object",
                "properties": {
                    "selector": {
                        "type": "string",
                        "description": "CSS selector of the <select> element"
                    },
                    "value": {
                        "type": "string",
                        "description": "Option value to select"
                    },
                    "label": {
                        "type": "string",
                        "description": "Visible label of the option to select (alternative to value)"
                    }
                },
                "required": ["selector"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "browser_wait",
            "description": "Wait for a condition: a selector to appear, page navigation, or a fixed delay.",
            "parameters": {
                "type": "object",
                "properties": {
                    "selector": {
                        "type": "string",
                        "description": "CSS selector to wait for (waits until visible)"
                    },
                    "timeout": {
                        "type": "integer",
                        "description": "Max wait time in milliseconds (default: 10000)"
                    },
                    "delay": {
                        "type": "integer",
                        "description": "Fixed delay in milliseconds (use when no selector — just pause)"
                    }
                }
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "browser_back",
            "description": "Navigate back in browser history.",
            "parameters": {
                "type": "object",
                "properties": {}
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "browser_tabs",
            "description": "List open browser tabs or switch to a specific tab.",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["list", "switch", "new", "close"],
                        "description": "Action: list tabs, switch to tab by index, open new tab, or close current tab"
                    },
                    "index": {
                        "type": "integer",
                        "description": "Tab index to switch to (for 'switch' action)"
                    },
                    "url": {
                        "type": "string",
                        "description": "URL to open in new tab (for 'new' action)"
                    }
                }
            }
        }
    },
    # ── Desktop / Computer Use tools ─────────────────────────────────
    {
        "type": "function",
        "function": {
            "name": "computer_screenshot",
            "description": "Take a screenshot. Defaults to full primary monitor; pass `target_window` to capture just one window, `monitor` for a specific display, or `region` for an explicit bbox. Precedence: region > target_window > monitor.",
            "parameters": {
                "type": "object",
                "properties": {
                    "region": {
                        "type": "object",
                        "description": "Explicit region to capture: {x, y, width, height}",
                        "properties": {
                            "x": {"type": "integer"},
                            "y": {"type": "integer"},
                            "width": {"type": "integer"},
                            "height": {"type": "integer"}
                        }
                    },
                    "target_window": {
                        "type": "string",
                        "description": "Substring of a window title; capture just that window (full window rect, including title bar)."
                    },
                    "monitor": {
                        "type": "integer",
                        "description": "Monitor index (0 = primary). Use monitors_list to enumerate."
                    }
                }
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "computer_click",
            "description": "Click at a position. By default (x,y) are absolute screen coords. With `target_window`, (x,y) are relative to that window's top-left. With `monitor`, relative to that monitor's top-left. Auto-captures a follow-up screenshot.",
            "parameters": {
                "type": "object",
                "properties": {
                    "x": {"type": "integer", "description": "X coordinate (interpretation depends on target_window/monitor)"},
                    "y": {"type": "integer", "description": "Y coordinate (interpretation depends on target_window/monitor)"},
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
                    },
                    "target_window": {
                        "type": "string",
                        "description": "If set, (x,y) are interpreted relative to this window's top-left."
                    },
                    "monitor": {
                        "type": "integer",
                        "description": "If set, (x,y) are interpreted relative to this monitor's top-left."
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
            "description": "Wait for the screen to change or pause for a duration. Use 'change' mode after clicking something that triggers a load. Pass `region` to watch only a bbox (cheaper, fewer false positives than whole-screen watching).",
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
                    },
                    "region": {
                        "type": "object",
                        "description": "Optional bbox to watch instead of whole screen (change mode only).",
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
                "Execute multiple read-only workspace calls in parallel. Use when you need to "
                "read several files, search for multiple patterns, or inspect git state. "
                "Maximum 25 calls. Mutating, shell, UI, task, and nested batch tools are refused."
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
                                    "description": "Tool name (file_read, glob, grep, git_status, git_diff, git_log)"
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
    # ── Git tools (first-class, structured output) ──────────────
    {
        "type": "function",
        "function": {
            "name": "git_status",
            "description": "Show the working tree status with structured output: branch, ahead/behind counts, staged/unstaged/untracked files. Prefer over `bash(git status)` — output is structured for the UI.",
            "parameters": {
                "type": "object",
                "properties": {
                    "cwd": {"type": "string", "description": "Working directory (default: project root)"}
                },
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "git_diff",
            "description": "Show diff for working tree (or staged area). Returns structured hunks with addition/deletion counts per file. Prefer over `bash(git diff)`.",
            "parameters": {
                "type": "object",
                "properties": {
                    "cwd": {"type": "string", "description": "Working directory (default: project root)"},
                    "staged": {"type": "boolean", "description": "If true, show diff of staged changes (--cached)"},
                    "paths": {"type": "array", "items": {"type": "string"}, "description": "Optional file paths to limit the diff"}
                },
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "git_commit",
            "description": "Create a NEW git commit. Optionally stages `paths` first. Always creates a new commit (never amends, never skips hooks).",
            "parameters": {
                "type": "object",
                "properties": {
                    "cwd": {"type": "string", "description": "Working directory (default: project root)"},
                    "message": {"type": "string", "description": "Commit message (supports multi-line)"},
                    "paths": {"type": "array", "items": {"type": "string"}, "description": "Optional paths to stage before committing"}
                },
                "required": ["message"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "git_branch_create",
            "description": "Create AND check out a new branch from `from_ref` (default HEAD). Refuses if branch already exists.",
            "parameters": {
                "type": "object",
                "properties": {
                    "cwd": {"type": "string", "description": "Working directory (default: project root)"},
                    "branch": {"type": "string", "description": "New branch name"},
                    "from_ref": {"type": "string", "description": "Ref to branch from (default: HEAD)"}
                },
                "required": ["branch"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "git_log",
            "description": "Show recent commits as a structured table: short_sha, date, author, subject. Prefer over `bash(git log)`.",
            "parameters": {
                "type": "object",
                "properties": {
                    "cwd": {"type": "string", "description": "Working directory (default: project root)"},
                    "limit": {"type": "integer", "description": "Max commits to return (default: 20, max: 200)"},
                    "paths": {"type": "array", "items": {"type": "string"}, "description": "Optional paths to filter the log"}
                },
                "required": []
            }
        }
    },
    # ── Multi-monitor / clipboard / process tools ──
    {
        "type": "function",
        "function": {
            "name": "monitors_list",
            "description": "List physical monitors with their bounds and primary flag. Use the returned indices with computer_screenshot(monitor=N) or computer_click(monitor=N).",
            "parameters": {"type": "object", "properties": {}, "required": []}
        }
    },
    {
        "type": "function",
        "function": {
            "name": "clipboard_read",
            "description": "Read the current text contents of the system clipboard. Returns empty string if non-text or empty.",
            "parameters": {"type": "object", "properties": {}, "required": []}
        }
    },
    {
        "type": "function",
        "function": {
            "name": "clipboard_write",
            "description": "Replace the system clipboard with the given text. Useful for stashing snippets the user can paste elsewhere.",
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "Text to copy to clipboard"}
                },
                "required": ["text"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "process_list",
            "description": "List running processes (pid, name, memory, command line). Optionally filter by name substring.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name_filter": {"type": "string", "description": "Case-insensitive substring match on process name or cmdline"},
                    "limit": {"type": "integer", "description": "Max rows to return (default: 100)"}
                },
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "process_kill",
            "description": "Terminate a process by pid OR exact name (case-insensitive). Refuses system PIDs and critical names. Specify exactly one of pid/name.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pid": {"type": "integer", "description": "Process ID to kill"},
                    "name": {"type": "string", "description": "Exact process name to kill (matches all instances)"}
                },
                "required": []
            }
        }
    },
    # ── Screen recording + visual diff ──
    {
        "type": "function",
        "function": {
            "name": "screen_record_start",
            "description": "Start recording the screen to an MP4 file (~/.resonant/recordings/). Useful for debugging long-running automation. Use screen_record_stop when done.",
            "parameters": {
                "type": "object",
                "properties": {
                    "fps": {"type": "integer", "description": "Frames per second (default: 10, max: 30)"},
                    "monitor": {"type": "integer", "description": "Monitor index (default: 0 = primary)"},
                    "region": {
                        "type": "object",
                        "properties": {
                            "x": {"type": "integer"}, "y": {"type": "integer"},
                            "width": {"type": "integer"}, "height": {"type": "integer"}
                        }
                    }
                },
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "screen_record_stop",
            "description": "Stop the active screen recording. Returns the MP4 file path, duration, and size.",
            "parameters": {"type": "object", "properties": {}, "required": []}
        }
    },
    {
        "type": "function",
        "function": {
            "name": "screen_diff",
            "description": "Detect changed regions between two screenshots. Defaults to (prev = last computer_* screenshot, current = a fresh screenshot now). Returns a list of bounding boxes where pixels changed.",
            "parameters": {
                "type": "object",
                "properties": {
                    "prev_id": {"type": "string", "description": "Cached screenshot id; defaults to penultimate"},
                    "current_id": {"type": "string", "description": "Cached screenshot id; defaults to fresh capture"},
                    "threshold": {"type": "integer", "description": "Per-pixel max-channel-delta to count as 'changed' (0–255, default 30)"}
                },
                "required": []
            }
        }
    },
    # ── Accessibility-tree targeting (semantic, more reliable than pixel coords) ──
    {
        "type": "function",
        "function": {
            "name": "accessibility_tree",
            "description": "Return the OS accessibility tree for a window (or the desktop). Each node has role/name/automation_id/bounds. Use this BEFORE accessibility_click to discover element identifiers — far more reliable than pixel-coord clicking. Windows: requires `pip install uiautomation`.",
            "parameters": {
                "type": "object",
                "properties": {
                    "window_title": {"type": "string", "description": "Optional substring of a window title; scopes the tree to that window"},
                    "verbose": {"type": "boolean", "description": "Return more rows in the text summary (full tree always in metadata)"}
                },
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "accessibility_click",
            "description": "Click an element in the OS accessibility tree by its semantic identifiers (role / name / automation_id). Resilient to DPI changes, theme changes, and window moves. Use accessibility_tree first to discover identifiers.",
            "parameters": {
                "type": "object",
                "properties": {
                    "role": {"type": "string", "description": "Control type (e.g. 'Button', 'Edit', 'Tab')"},
                    "name": {"type": "string", "description": "Substring match on element name (e.g. 'Save', '7')"},
                    "automation_id": {"type": "string", "description": "Exact AutomationId (Windows UIA)"},
                    "window_title": {"type": "string", "description": "Optional window scope"}
                },
                "required": []
            }
        }
    },
    # ── Persistent REPLs (long-lived interpreter for incremental work) ──
    {
        "type": "function",
        "function": {
            "name": "repl_python_start",
            "description": "Start a long-lived Python REPL. Returns a repl_id you pass to repl_python_eval. Prefer over repeated `bash(python -c ...)` for incremental work — state persists across calls.",
            "parameters": {
                "type": "object",
                "properties": {
                    "cwd": {"type": "string", "description": "Working directory (default: current)"}
                },
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "repl_python_eval",
            "description": "Eval Python code in an existing REPL. State (variables, imports) persists across calls. Hard timeout per call.",
            "parameters": {
                "type": "object",
                "properties": {
                    "repl_id": {"type": "string", "description": "ID returned by repl_python_start"},
                    "code": {"type": "string", "description": "Python source to evaluate"},
                    "timeout": {"type": "number", "description": "Max seconds to wait for output (default: 30)"}
                },
                "required": ["repl_id", "code"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "repl_python_stop",
            "description": "Terminate a Python REPL. Always stop REPLs you started when you're done with them.",
            "parameters": {
                "type": "object",
                "properties": {
                    "repl_id": {"type": "string", "description": "ID returned by repl_python_start"}
                },
                "required": ["repl_id"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "repl_node_start",
            "description": "Start a long-lived Node.js REPL. Returns a repl_id for repl_node_eval. State persists across calls.",
            "parameters": {
                "type": "object",
                "properties": {
                    "cwd": {"type": "string", "description": "Working directory (default: current)"}
                },
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "repl_node_eval",
            "description": "Eval JavaScript code in an existing Node REPL. State persists across calls.",
            "parameters": {
                "type": "object",
                "properties": {
                    "repl_id": {"type": "string", "description": "ID returned by repl_node_start"},
                    "code": {"type": "string", "description": "JavaScript source to evaluate"},
                    "timeout": {"type": "number", "description": "Max seconds to wait for output (default: 30)"}
                },
                "required": ["repl_id", "code"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "repl_node_stop",
            "description": "Terminate a Node REPL. Always stop REPLs you started when done.",
            "parameters": {
                "type": "object",
                "properties": {
                    "repl_id": {"type": "string", "description": "ID returned by repl_node_start"}
                },
                "required": ["repl_id"]
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
    "browser_scroll":     "↕",
    "browser_hover":      "◌",
    "browser_select":     "☰",
    "browser_wait":       "⏳",
    "browser_back":       "◁",
    "browser_tabs":       "⊞",
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
    # Git tools
    "git_status":          "±",
    "git_diff":            "≠",
    "git_commit":          "✓",
    "git_branch_create":   "⎇",
    "git_log":             "☷",
    # REPL tools
    "repl_python_start":   "🐍",
    "repl_python_eval":    "▶",
    "repl_python_stop":    "■",
    "repl_node_start":     "⬢",
    "repl_node_eval":      "▶",
    "repl_node_stop":      "■",
    # Multi-monitor / clipboard / process
    "monitors_list":       "🖥",
    "clipboard_read":      "📋",
    "clipboard_write":     "📋",
    "process_list":        "⚙",
    "process_kill":        "✗",
    # Recording / diff
    "screen_record_start": "●",
    "screen_record_stop":  "■",
    "screen_diff":         "◇",
    # Accessibility
    "accessibility_tree":  "🌳",
    "accessibility_click": "◉",
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


def execute_tool(
    name: str,
    arguments: dict,
    cancel_event: Optional[threading.Event] = None,
    *,
    project_path: str = "",
    settings: object = None,
) -> ToolResult:
    """
    Execute a tool and return structured result.

    This is the pure execution layer — no display logic, no approval prompts.
    The engine handles permission; the TUI handles display.

    Full Autonomy floor: before dispatching, run the irreversibility-floor
    checks (`autonomy.check_floor`). On violation, return a ToolResult flagged
    with `metadata["floor_violation"]` so Session can pause for the user via
    the existing tool.permission flow. Routine actions never trigger the
    floor — only the ones that can't be undone.

    Per-tool-call auditing happens upstream in the orchestrator's specialist
    runner (it observes `tool.call` events from Session); this layer only
    handles dispatch + floor enforcement.

    Note: 'task' tool is handled by Session (needs backend access), not here.
    """
    start = time.time()

    # ── Irreversibility floor check ──
    if name not in ("task",):  # task isn't dispatched here at all
        try:
            from ..orchestration.autonomy import check_floor
            violation = check_floor(
                tool_name=name,
                args=arguments or {},
                project_path=project_path or "",
                settings=settings,
            )
        except Exception:
            violation = None
        if violation is not None:
            return ToolResult(
                output=(
                    f"FLOOR_VIOLATION: {violation.rule}\n"
                    f"{violation.reason}\n"
                    f"Suggested: {violation.suggested_action or '(none)'}"
                ),
                is_error=True,
                elapsed=time.time() - start,
                metadata={
                    "floor_violation": {
                        "rule": violation.rule,
                        "reason": violation.reason,
                        "severity": violation.severity,
                        "suggested_action": violation.suggested_action,
                        "tool_name": name,
                    },
                },
            )

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
        elif name == "skill_view":
            return _exec_skill_view(arguments, start, project_path=project_path)
        elif name == "batch":
            return _exec_batch(
                arguments,
                start,
                cancel_event=cancel_event,
                project_path=project_path,
                settings=settings,
            )
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
        elif name == "browser_scroll":
            from .browser import exec_browser_scroll
            return exec_browser_scroll(arguments, start)
        elif name == "browser_hover":
            from .browser import exec_browser_hover
            return exec_browser_hover(arguments, start)
        elif name == "browser_select":
            from .browser import exec_browser_select
            return exec_browser_select(arguments, start)
        elif name == "browser_wait":
            from .browser import exec_browser_wait
            return exec_browser_wait(arguments, start)
        elif name == "browser_back":
            from .browser import exec_browser_back
            return exec_browser_back(arguments, start)
        elif name == "browser_tabs":
            from .browser import exec_browser_tabs
            return exec_browser_tabs(arguments, start)
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
        # Git tools
        elif name == "git_status":
            from .git_tools import exec_git_status
            return exec_git_status(arguments, start)
        elif name == "git_diff":
            from .git_tools import exec_git_diff
            return exec_git_diff(arguments, start)
        elif name == "git_commit":
            from .git_tools import exec_git_commit
            return exec_git_commit(arguments, start)
        elif name == "git_branch_create":
            from .git_tools import exec_git_branch_create
            return exec_git_branch_create(arguments, start)
        elif name == "git_log":
            from .git_tools import exec_git_log
            return exec_git_log(arguments, start)
        # REPL tools
        elif name == "repl_python_start":
            from .repl import exec_repl_python_start
            return exec_repl_python_start(arguments, start)
        elif name == "repl_python_eval":
            from .repl import exec_repl_python_eval
            return exec_repl_python_eval(arguments, start)
        elif name == "repl_python_stop":
            from .repl import exec_repl_python_stop
            return exec_repl_python_stop(arguments, start)
        elif name == "repl_node_start":
            from .repl import exec_repl_node_start
            return exec_repl_node_start(arguments, start)
        elif name == "repl_node_eval":
            from .repl import exec_repl_node_eval
            return exec_repl_node_eval(arguments, start)
        elif name == "repl_node_stop":
            from .repl import exec_repl_node_stop
            return exec_repl_node_stop(arguments, start)
        # Multi-monitor / clipboard / process tools
        elif name == "monitors_list":
            from .computer_use import exec_monitors_list
            return exec_monitors_list(arguments, start)
        elif name == "clipboard_read":
            from .clipboard import exec_clipboard_read
            return exec_clipboard_read(arguments, start)
        elif name == "clipboard_write":
            from .clipboard import exec_clipboard_write
            return exec_clipboard_write(arguments, start)
        elif name == "process_list":
            from .processes import exec_process_list
            return exec_process_list(arguments, start)
        elif name == "process_kill":
            from .processes import exec_process_kill
            return exec_process_kill(arguments, start)
        elif name == "screen_record_start":
            from .recording import exec_screen_record_start
            return exec_screen_record_start(arguments, start)
        elif name == "screen_record_stop":
            from .recording import exec_screen_record_stop
            return exec_screen_record_stop(arguments, start)
        elif name == "screen_diff":
            from .screen_diff import exec_screen_diff
            return exec_screen_diff(arguments, start)
        elif name == "accessibility_tree":
            from .accessibility import exec_accessibility_tree
            return exec_accessibility_tree(arguments, start)
        elif name == "accessibility_click":
            from .accessibility import exec_accessibility_click
            return exec_accessibility_click(arguments, start)
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
        # Tail truncation — for shell output the model needs the *end* (errors,
        # final exit codes, last lines of a build log). Previously bash had no
        # truncation at all, so a `cat huge.log` could blow the context.
        result = truncate_tail(output)
        output = result.content + render_truncation_footer(result)
        return ToolResult(
            output,
            is_error=returncode != 0,
            elapsed=elapsed,
            metadata={
                "command": cmd,
                "exit_code": returncode,
                "lines": result.total_lines,
                "shown_lines": result.output_lines,
                "truncated": result.truncated,
            },
        )
    except subprocess.TimeoutExpired:
        return ToolResult(
            f"Command timed out after {timeout}s.",
            is_error=True,
            elapsed=timeout,
            metadata={"command": cmd, "timed_out": True},
        )


def _validate_write_path(fpath: str, allow_leading_dash: bool) -> str:
    """v0.5.7a3 — reject writes whose path basename starts with `-`
    unless the caller explicitly opts in via `allow_leading_dash=true`.

    Linux-bridge field-observation #8: a file literally named `-p`
    appeared at the project root mid-iteration, almost certainly from
    a tokenization slip where `mkdir -p src` got split into three
    separate args `mkdir`, `-p`, `src` and `-p` landed in the path
    field of the wrong tool. Files with leading-dash names are also
    a known foot-gun for shell command-line parsers (a later
    `rm <pattern>` may misinterpret the file as a flag).

    Returns "" on validation success; a non-empty error message
    when the path is rejected.
    """
    if allow_leading_dash:
        return ""
    if not fpath:
        return ""
    # Check the basename and every intermediate path segment. A
    # tokenization slip can land `-` anywhere in the path, not just
    # the last component (e.g. `-p/foo.txt` would create a directory
    # named `-p`).
    parts = Path(fpath).parts
    for seg in parts:
        # Skip Windows drive specs like 'C:\\' which surface as 'C:'
        # plus the separator. Drive specs end with `:`.
        if seg.endswith(":") or seg in (".", ".."):
            continue
        if seg.startswith("-"):
            return (
                f"Refusing to write to '{fpath}': path segment "
                f"'{seg}' starts with '-' which is almost always a "
                f"tokenization slip (e.g. shell flag mistakenly "
                f"routed into a path argument). If you genuinely "
                f"need this filename, retry with allow_leading_dash=true."
            )
    return ""


def _exec_file_write(args: dict, start: float) -> ToolResult:
    fpath = args.get("path", "")
    content = args.get("content", "")
    allow_dash = bool(args.get("allow_leading_dash", False))
    err = _validate_write_path(fpath, allow_dash)
    if err:
        return ToolResult(
            f"Error: {err}",
            is_error=True,
            elapsed=time.time() - start,
        )
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
    offset = max(0, int(args.get("offset", 0) or 0))
    limit = min(2_000, max(1, int(args.get("limit", 400) or 400)))
    path = Path(fpath)
    if not path.exists():
        return ToolResult(f"Error: File not found: {fpath}", is_error=True, elapsed=time.time() - start)
    content = path.read_text(encoding="utf-8")
    lines = content.split("\n")
    total_lines = len(lines)
    selected = "\n".join(lines[offset:offset + limit])
    result = truncate_head(selected, max_lines=limit)
    output = result.content
    shown_lines = result.output_lines
    next_offset = offset + shown_lines
    has_more = next_offset < total_lines
    if has_more:
        output += (
            f"\n\n[showing lines {offset + 1}-{next_offset} of {total_lines}. "
            f"Continue with file_read {{\"path\": {json.dumps(str(fpath))}, "
            f"\"offset\": {next_offset}, \"limit\": {limit}}}]"
        )
    elif result.truncated:
        output += render_truncation_footer(result)
    elapsed = time.time() - start
    return ToolResult(
        output,
        elapsed=elapsed,
        metadata={
            "path": fpath,
            "lines": total_lines,
            "shown_lines": shown_lines,
            "offset": offset,
            "limit": limit,
            "next_offset": next_offset if has_more else None,
            "truncated": has_more or result.truncated,
        },
    )


def _exec_file_edit(args: dict, start: float) -> ToolResult:
    fpath = args.get("path", "")
    old_text = args.get("old_text", "")
    new_text = args.get("new_text", "")
    replace_all = bool(args.get("replace_all", False))
    allow_dash = bool(args.get("allow_leading_dash", False))
    err = _validate_write_path(fpath, allow_dash)
    if err:
        return ToolResult(
            f"Error: {err}",
            is_error=True,
            elapsed=time.time() - start,
        )
    path = Path(fpath)

    if not path.exists():
        return ToolResult(f"Error: File not found: {fpath}", is_error=True, elapsed=time.time() - start)

    content = path.read_text(encoding="utf-8")
    try:
        application = apply_text_edit(
            content,
            old_text,
            new_text,
            replace_all=replace_all,
        )
    except EditMatchError as exc:
        return ToolResult(
            f"Error editing {fpath}: {exc}",
            is_error=True,
            elapsed=time.time() - start,
        )

    path.write_text(application.content, encoding="utf-8")
    elapsed = time.time() - start
    return ToolResult(
        (
            f"File edited: {fpath} "
            f"({application.strategy} match, line {application.line}, "
            f"{application.replacements} replacement(s))"
        ),
        elapsed=elapsed,
        metadata={
            "path": fpath,
            "old_text": old_text,
            "new_text": new_text,
            "match_strategy": application.strategy,
            "replacements": application.replacements,
            "line": application.line,
        },
    )


def _exec_glob(args: dict, start: float) -> ToolResult:
    pattern = args.get("pattern", "")
    base = args.get("path", ".")
    offset = max(0, int(args.get("offset", 0) or 0))
    limit = min(200, max(1, int(args.get("limit", 50) or 50)))

    # Models often pass absolute patterns (e.g. "D:/Repos/proj/**/*.py") —
    # Python's Path.glob refuses those with "Non-relative patterns are
    # unsupported." Split absolute patterns into (longest non-glob prefix,
    # relative pattern remainder) so callers don't need to know the convention.
    pat = Path(pattern)
    if pat.is_absolute():
        parts = pat.parts
        meta_chars = ("*", "?", "[")
        split_at = None
        for i, part in enumerate(parts):
            if any(c in part for c in meta_chars):
                split_at = i
                break
        if split_at is None:
            base = str(pat.parent) if pat.parent != pat else str(pat.anchor or ".")
            pattern = pat.name
        elif split_at == 0:
            base = pat.anchor or "."
            pattern = str(Path(*parts))
        else:
            base = str(Path(*parts[:split_at]))
            pattern = str(Path(*parts[split_at:]))

    try:
        all_matches = sorted(Path(base).glob(pattern))
    except (NotImplementedError, OSError) as exc:
        return ToolResult(
            f"Error: glob pattern not supported ({exc}). "
            f"Try a relative pattern like '**/*.py' with the project root as base.",
            is_error=True,
            elapsed=time.time() - start,
        )
    total = len(all_matches)
    matches = all_matches[offset:offset + limit]
    result = "\n".join(str(m) for m in matches)
    next_offset = offset + len(matches)
    if next_offset < total:
        result += (
            f"\n\n[showing paths {offset + 1}-{next_offset} of {total}. "
            f"Continue with glob {{\"pattern\": {json.dumps(str(pattern))}, "
            f"\"path\": {json.dumps(str(base))}, \"offset\": {next_offset}, "
            f"\"limit\": {limit}}}]"
        )
    elapsed = time.time() - start
    return ToolResult(
        result or "(no matches)",
        elapsed=elapsed,
        metadata={
            "pattern": pattern,
            "count": total,
            "shown": len(matches),
            "offset": offset,
            "limit": limit,
            "next_offset": next_offset if next_offset < total else None,
            "base": base,
        },
    )


def _exec_grep(args: dict, start: float, cancel_event: Optional[threading.Event] = None) -> ToolResult:
    pattern = args.get("pattern", "")
    path = args.get("path", ".")
    file_glob = args.get("glob", "")
    offset = max(0, int(args.get("offset", 0) or 0))
    limit = min(200, max(1, int(args.get("limit", 50) or 50)))

    # Never interpolate model-controlled search values into a shell command.
    # A quoted string is not a security boundary: a pattern containing a quote
    # can escape it and turn this read-only tool into arbitrary shell execution.
    if sys.platform == "win32":
        target = os.path.join(path, file_glob or "*") if os.path.isdir(path) else path
        cmd = ["findstr", "/s", "/n", "/r", f"/c:{pattern}", target]
    else:
        cmd = ["grep", "-rn"]
        if file_glob:
            cmd.extend([f"--include={file_glob}"])
        cmd.extend(["--", pattern, path])

    returncode, stdout, _stderr, timed_out = _run_subprocess_with_cancel(
        cmd,
        shell=False,
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
    # Cap each match line at 500 chars so a single minified-JS hit can't
    # dominate the result list. Then head-truncate the overall match set.
    if lines:
        capped: list[str] = []
        any_line_truncated = False
        for ln in lines:
            t, was = truncate_line(ln, GREP_MAX_LINE_LENGTH)
            capped.append(t)
            any_line_truncated = any_line_truncated or was
        selected = capped[offset:offset + limit]
        joined = "\n".join(selected)
        result = truncate_head(joined, max_lines=limit)
        output = result.content
        shown = result.output_lines if selected else 0
        next_offset = offset + shown
        if next_offset < count:
            output += (
                f"\n\n[showing matches {offset + 1}-{next_offset} of {count}. "
                f"Continue with grep {{\"pattern\": {json.dumps(str(pattern))}, "
                f"\"path\": {json.dumps(str(path))}, \"offset\": {next_offset}, "
                f"\"limit\": {limit}}}]"
            )
        if any_line_truncated:
            output += "\n[note: some match lines were individually truncated]"
    else:
        shown = 0
        next_offset = offset

    elapsed = time.time() - start
    return ToolResult(
        output or "(no matches)",
        elapsed=elapsed,
        metadata={
            "pattern": pattern,
            "count": count,
            "shown": shown,
            "offset": offset,
            "limit": limit,
            "next_offset": next_offset if next_offset < count else None,
            "exit_code": returncode,
        },
    )


def _exec_skill_view(args: dict, start: float, *, project_path: str = "") -> ToolResult:
    """Read a project/global skill body without exposing skill storage paths."""
    skill_id = str(args.get("skill_id", "") or "").strip()
    from ..orchestration.skills import load_skill, skill_dir

    skill = None
    resolved_scope = ""
    for scope in ("project", "global"):
        if scope == "project" and not project_path:
            continue
        skill = load_skill(
            skill_id,
            scope=scope,
            project_path=project_path if scope == "project" else None,
        )
        if skill:
            resolved_scope = scope
            break
    if not skill:
        return ToolResult(
            f"Error: skill not found: {skill_id}",
            is_error=True,
            elapsed=time.time() - start,
        )

    directory = skill_dir(
        skill.id,
        scope=resolved_scope,
        project_path=project_path if resolved_scope == "project" else None,
    )
    sections = [f"# {skill.id}\n\n{skill.description}".strip()]
    for filename, heading in (
        ("procedure.md", "Procedure"),
        ("verification.md", "Verification"),
    ):
        path = directory / filename
        if path.is_file():
            body = path.read_text(encoding="utf-8").strip()
            if body:
                sections.append(f"## {heading}\n\n{body}")
    output = "\n\n".join(sections)
    result = truncate_head(output, max_lines=800, max_bytes=24 * 1024)
    return ToolResult(
        result.content + render_truncation_footer(result),
        elapsed=time.time() - start,
        metadata={
            "skill_id": skill.id,
            "scope": resolved_scope,
            "truncated": result.truncated,
        },
    )


# ── Batch tool (parallel execution) ──────────────────────────────────

# Batch is deliberately limited to workspace reads.  The outer ``batch`` call
# receives one policy/permission decision; allowing arbitrary child tools would
# let a model smuggle writes, shell commands, or desktop actions past specialist
# allowlists and the session sandbox.
BATCH_ALLOWED_TOOL_NAMES = frozenset({
    "file_read", "glob", "grep", "git_status", "git_diff", "git_log", "skill_view",
})
BATCH_MAX_CALLS = 25
BATCH_MAX_WORKERS = 10


def _exec_batch(
    args: dict,
    start: float,
    cancel_event: Optional[threading.Event] = None,
    *,
    project_path: str = "",
    settings: object = None,
) -> ToolResult:
    """
    Execute approved read-only tool calls in parallel using ThreadPoolExecutor.

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

            if name not in BATCH_ALLOWED_TOOL_NAMES:
                results[i] = {
                    "index": i, "name": name, "status": "error",
                    "output": (
                        f"Cannot batch '{name}' tool. Batch only supports: "
                        f"{sorted(BATCH_ALLOWED_TOOL_NAMES)}"
                    ),
                    "elapsed": 0,
                }
                forbidden_indices.add(i)
                continue

            future = pool.submit(
                execute_tool,
                name,
                call_args,
                cancel_event,
                project_path=project_path,
                settings=settings,
            )
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
