"""Stable UI presentation hints attached to tool lifecycle events.

Renderers should not need an ever-growing list of provider-specific tool
names. This module translates core, browser, desktop, and MCP-shaped calls
into a small intent vocabulary while preserving the original tool payload.
"""

from __future__ import annotations

from typing import Any


_FILE_PATH_KEYS = (
    "path",
    "file_path",
    "filepath",
    "paths",
    "files",
)
_PATH_KEYS = (
    *_FILE_PATH_KEYS,
    "directory",
    "cwd",
    "workdir",
    "root",
)


def _locations(
    arguments: dict[str, Any], keys: tuple[str, ...] = _PATH_KEYS
) -> list[str]:
    found: list[str] = []
    for key in keys:
        value = arguments.get(key)
        values = value if isinstance(value, list) else [value]
        for item in values:
            if not isinstance(item, (str, int)):
                continue
            location = str(item).strip()
            if location and location not in found:
                found.append(location)
    return found[:16]


def tool_presentation(name: str, arguments: dict[str, Any] | None) -> dict[str, Any]:
    """Return provider-agnostic render intent for one tool call."""
    tool = str(name or "").strip()
    lower = tool.lower()
    args = arguments if isinstance(arguments, dict) else {}
    locations = _locations(args)
    file_locations = _locations(args, _FILE_PATH_KEYS)
    presentation: dict[str, Any] = {
        "kind": "generic",
        "view": "inline",
        "label": tool.replace("_", " ").strip().title() or "Tool",
        "locations": locations,
    }

    if lower in {"file_write"} or any(token in lower for token in ("write_file", "create_file")):
        presentation.update(
            kind="write",
            view="diff",
            label="Write file",
            locations=file_locations or locations,
        )
    elif lower in {"file_edit", "apply_patch"} or any(
        token in lower for token in ("edit_file", "patch_file", "replace_in_file")
    ):
        presentation.update(
            kind="edit",
            view="diff",
            label="Edit file",
            locations=file_locations or locations,
        )
    elif lower in {"file_read"} or any(token in lower for token in ("read_file", "get_file")):
        presentation.update(
            kind="read",
            view="document",
            label="Read file",
            locations=file_locations or locations,
        )
    elif lower in {"glob", "grep"} or any(
        token in lower for token in ("search_files", "find_files", "search_code")
    ):
        presentation.update(kind="search", view="results", label="Search project")
    elif lower == "bash" or any(token in lower for token in ("shell", "terminal", "command")):
        presentation.update(kind="terminal", view="terminal", label="Run command")
    elif lower.startswith("browser_"):
        presentation.update(kind="web", view="browser", label="Use browser")
        target = str(args.get("url") or "").strip()
        if target:
            presentation["target"] = target
    elif lower.startswith("computer_"):
        presentation.update(kind="desktop", view="computer", label="Use computer")
    elif lower in {"task", "task_batch"} or "agent" in lower:
        presentation.update(kind="agent", view="workflow", label="Delegate work")
    elif lower.startswith("git_"):
        presentation.update(kind="version_control", view="changes", label="Use Git")
    elif locations:
        # Unknown MCP tools still become useful without coupling the GUI to
        # server-specific names. A location-bearing call renders as a resource.
        presentation.update(kind="resource", view="document", label="Use resource")

    presentation["interactive"] = presentation["view"] in {
        "browser",
        "computer",
        "terminal",
    }
    return presentation
