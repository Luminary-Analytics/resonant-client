"""Whether macOS lets Lumi see the screen and drive the mouse and keyboard.

Without Screen Recording permission, macOS still returns screenshots, but
they show only the desktop background and menu bar. Without Accessibility,
clicks and keystrokes are silently dropped. Either way the tool appears to
succeed and the model acts on nothing, so ``missing_permission`` is checked
before each desktop tool on macOS (tools.execute_tool).

The checks are the system's own preflight calls, made through ctypes: they
never show a prompt, and an unknown answer (an older macOS, a call that
isn't there) doesn't block anything.
"""

from __future__ import annotations

import ctypes
import sys

# Tools that read the screen, and tools that drive other apps.
NEEDS_SCREEN_RECORDING = frozenset({
    "computer_screenshot", "screen_ocr", "screen_record_start", "screen_diff", "window_list",
})
NEEDS_ACCESSIBILITY = frozenset({
    "computer_click", "computer_type", "computer_scroll", "computer_drag", "computer_hover",
    "window_focus", "accessibility_click", "accessibility_tree",
})


def _preflight(framework: str, symbol: str) -> bool | None:
    try:
        library = ctypes.cdll.LoadLibrary(f"/System/Library/Frameworks/{framework}.framework/{framework}")
        function = getattr(library, symbol)
    except (OSError, AttributeError):
        return None
    function.restype = ctypes.c_bool
    function.argtypes = []
    return bool(function())


def screen_recording_allowed() -> bool | None:
    return _preflight("CoreGraphics", "CGPreflightScreenCaptureAccess")


def accessibility_allowed() -> bool | None:
    return _preflight("ApplicationServices", "AXIsProcessTrusted")


def missing_permission(tool_name: str) -> str:
    """Why macOS would make ``tool_name`` do nothing, or "" (always "" off macOS)."""
    if sys.platform != "darwin":
        return ""
    if tool_name in NEEDS_ACCESSIBILITY and accessibility_allowed() is False:
        return ("macOS hasn't given Lumi Accessibility access, so clicks and typing would do nothing. "
                "Allow Lumi in System Settings > Privacy & Security > Accessibility, then try again.")
    if tool_name in NEEDS_SCREEN_RECORDING and screen_recording_allowed() is False:
        return ("macOS hasn't given Lumi Screen Recording access, so screenshots would show only the desktop "
                "background. Allow Lumi in System Settings > Privacy & Security > Screen Recording, then "
                "restart Lumi.")
    return ""
