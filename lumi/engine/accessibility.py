"""
Accessibility-tree targeting for the agent's computer-use loop.

Why: clicking by pixel coords is fragile (DPI, theme, window position). The OS
exposes a semantic tree where every UI element has a role/name/automation_id —
clicking by `name="Save"` is far more reliable.

Backends:
- Windows: `uiautomation` (UIA) — installed on demand.
- macOS:   `AXUIElement` via pyobjc — install AppKit/Cocoa frameworks.
- Linux:   stub returning a "not supported" message.

Both tools degrade gracefully when the backend isn't installed: they return an
informative error rather than crashing.
"""

from __future__ import annotations

import sys
import time
from typing import Optional

from .tools import ToolResult


# ── Tree extraction ────────────────────────────────────────────────────


def get_tree(window_title: Optional[str] = None, *, max_depth: int = 6, max_nodes: int = 400) -> dict:
    """
    Returns a JSON-friendly accessibility subtree:
        {role, name, automation_id, value, bounds: {x, y, width, height}, children: [...]}

    If `window_title` is set, scoped to the matching top-level window;
    otherwise returns the desktop root (capped to max_depth).
    """
    if sys.platform == "win32":
        return _get_tree_windows(window_title, max_depth=max_depth, max_nodes=max_nodes)
    if sys.platform == "darwin":
        return _get_tree_macos(window_title, max_depth=max_depth, max_nodes=max_nodes)
    return {"error": "accessibility tree not supported on this platform"}


def find_element(query: dict) -> Optional[dict]:
    """
    Find the first element matching `query`. Query keys (all optional, AND-combined):
      - role:           exact role match (case-insensitive)
      - name:           substring match on name (case-insensitive)
      - automation_id:  exact match
      - window_title:   limit search to the matching window's subtree
    """
    window_title = query.get("window_title")
    tree = get_tree(window_title)
    if tree.get("error"):
        return None
    match = _walk_match(tree, query)
    return match


def click_element(element: dict) -> dict:
    """
    Click the center of `element["bounds"]` using pyautogui.
    Returns {"clicked": bool, "x": int, "y": int} or {"error": str}.
    """
    bounds = element.get("bounds") if isinstance(element, dict) else None
    if not bounds or "x" not in bounds or "y" not in bounds:
        return {"error": "element has no bounds"}
    cx = int(bounds["x"]) + int(bounds.get("width", 0)) // 2
    cy = int(bounds["y"]) + int(bounds.get("height", 0)) // 2
    try:
        import pyautogui
        pyautogui.click(x=cx, y=cy)
    except ImportError:
        return {"error": "pyautogui not installed"}
    except Exception as exc:
        return {"error": f"click failed: {exc}"}
    return {"clicked": True, "x": cx, "y": cy}


# ── Internal: walking + matching ───────────────────────────────────────


def _walk_match(node: dict, query: dict) -> Optional[dict]:
    if _matches(node, query):
        return node
    for child in node.get("children", []) or []:
        m = _walk_match(child, query)
        if m is not None:
            return m
    return None


def _matches(node: dict, query: dict) -> bool:
    role = (query.get("role") or "").strip().lower()
    if role and (node.get("role") or "").strip().lower() != role:
        return False
    name = (query.get("name") or "").strip().lower()
    if name and name not in (node.get("name") or "").strip().lower():
        return False
    aid = (query.get("automation_id") or "").strip()
    if aid and (node.get("automation_id") or "").strip() != aid:
        return False
    return True


# ── Windows (UIA) backend ──────────────────────────────────────────────


def _get_tree_windows(window_title: Optional[str], *, max_depth: int, max_nodes: int) -> dict:
    try:
        import uiautomation as auto  # type: ignore
    except ImportError:
        return {"error": "uiautomation not installed (pip install uiautomation)"}

    counter = {"n": 0}

    def _serialize(ctrl, depth: int) -> Optional[dict]:
        if counter["n"] >= max_nodes:
            return None
        try:
            rect = ctrl.BoundingRectangle
            bounds = {
                "x": int(rect.left),
                "y": int(rect.top),
                "width": int(rect.right - rect.left),
                "height": int(rect.bottom - rect.top),
            }
            # Skip off-screen / invisible
            if bounds["width"] <= 0 or bounds["height"] <= 0:
                return None
        except Exception:
            return None

        node = {
            "role": getattr(ctrl, "ControlTypeName", "") or "",
            "name": getattr(ctrl, "Name", "") or "",
            "automation_id": getattr(ctrl, "AutomationId", "") or "",
            "value": "",
            "bounds": bounds,
            "children": [],
        }
        try:
            value_pattern = ctrl.GetValuePattern()
            if value_pattern:
                node["value"] = value_pattern.Value or ""
        except Exception:
            pass
        counter["n"] += 1

        if depth < max_depth:
            try:
                for child in ctrl.GetChildren():
                    serialized = _serialize(child, depth + 1)
                    if serialized:
                        node["children"].append(serialized)
                    if counter["n"] >= max_nodes:
                        break
            except Exception:
                pass
        return node

    try:
        if window_title:
            needle = window_title.lower()
            root = auto.GetRootControl()
            target = None
            for w in root.GetChildren():
                if needle in (w.Name or "").lower():
                    target = w
                    break
            if target is None:
                return {"error": f"no window matching '{window_title}'"}
            tree = _serialize(target, 0)
        else:
            tree = _serialize(auto.GetRootControl(), 0)
        return tree or {"error": "tree extraction failed"}
    except Exception as exc:
        return {"error": f"UIA error: {exc}"}


# ── macOS (AX) backend ─────────────────────────────────────────────────


def _get_tree_macos(window_title: Optional[str], *, max_depth: int, max_nodes: int) -> dict:
    # NB: full AX support is non-trivial (HIServices.AXUIElement APIs via pyobjc).
    # This stub returns a clear error so the caller can fall back gracefully.
    try:
        import HIServices  # noqa: F401  type: ignore
    except ImportError:
        return {"error": "macOS accessibility requires pyobjc (pip install pyobjc-framework-Cocoa pyobjc-framework-ApplicationServices)"}
    return {"error": "macOS accessibility tree not yet implemented — file an issue if you need it"}


# ── Tool wrappers ──────────────────────────────────────────────────────


def _summarize_tree(node: dict, depth: int = 0, lines: Optional[list] = None, max_lines: int = 60) -> list[str]:
    if lines is None:
        lines = []
    if len(lines) >= max_lines:
        return lines
    indent = "  " * depth
    role = node.get("role") or "?"
    name = (node.get("name") or "").strip()
    aid = (node.get("automation_id") or "").strip()
    bits = [role]
    if name:
        bits.append(f"name={name!r}")
    if aid:
        bits.append(f"id={aid!r}")
    bounds = node.get("bounds")
    if bounds:
        bits.append(f"@({bounds['x']},{bounds['y']},{bounds['width']}x{bounds['height']})")
    lines.append(f"{indent}{' '.join(bits)}")
    for child in node.get("children", []) or []:
        if len(lines) >= max_lines:
            break
        _summarize_tree(child, depth + 1, lines, max_lines)
    return lines


def exec_accessibility_tree(args: dict, start: float) -> ToolResult:
    window_title = args.get("window_title")
    verbose = bool(args.get("verbose", False))
    tree = get_tree(window_title)
    if tree.get("error"):
        return ToolResult(tree["error"], is_error=True, elapsed=time.time() - start, metadata=tree)

    if verbose:
        # Caller wants the raw structured tree (kept in metadata).
        text = "\n".join(_summarize_tree(tree, max_lines=200))
    else:
        text = "\n".join(_summarize_tree(tree, max_lines=60))
    return ToolResult(text, elapsed=time.time() - start, metadata={"tree": tree})


def exec_accessibility_click(args: dict, start: float) -> ToolResult:
    query = {
        "role": args.get("role"),
        "name": args.get("name"),
        "automation_id": args.get("automation_id"),
        "window_title": args.get("window_title"),
    }
    if not any((query["role"], query["name"], query["automation_id"])):
        return ToolResult(
            "Provide at least one of: role, name, automation_id.",
            is_error=True, elapsed=time.time() - start,
        )

    el = find_element(query)
    if el is None:
        return ToolResult(
            f"No element matching {query}",
            is_error=True, elapsed=time.time() - start,
        )

    res = click_element(el)
    if res.get("error"):
        return ToolResult(res["error"], is_error=True, elapsed=time.time() - start, metadata={"matched": el, **res})

    label = el.get("name") or el.get("automation_id") or el.get("role") or "?"
    return ToolResult(
        f"Clicked {el.get('role','?')} {label!r} at ({res['x']}, {res['y']})",
        elapsed=time.time() - start,
        metadata={"matched": el, **res},
    )
