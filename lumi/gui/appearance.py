"""The saved appearance (theme, density, font size) for the page and the window.

The desktop window uses a fresh port and private browser storage on every
launch, so the page cannot remember its own theme. The server renders the
saved values into ``<html>``, which applies them before the first paint;
``static/appearance.js`` resolves ``system`` against the OS setting.
"""

from __future__ import annotations

import sys
from typing import Any

THEMES = ("dark", "light", "system")
DEFAULT_THEME = "dark"
FONT_SIZE_RANGE = (10.0, 20.0)

# Page background of each theme (the --bg token), shown by the native window
# until the page paints.
WINDOW_BACKGROUND = {"dark": "#0F1126", "light": "#F6F4EE"}


def page_appearance(settings: Any) -> dict[str, str | None]:
    """Validated appearance values for the page template.

    ``font_size`` is None unless the user chose one, so the density default
    still applies.
    """
    theme = settings.get("appearance", "theme", DEFAULT_THEME)
    if theme not in THEMES:
        theme = DEFAULT_THEME
    density = "compact" if settings.get("appearance", "density") == "compact" else "comfortable"
    font_size = None
    raw = settings.get("appearance", "font_size")
    if raw not in (None, ""):
        try:
            value = float(raw)
        except (TypeError, ValueError):
            value = None
        if value is not None and FONT_SIZE_RANGE[0] <= value <= FONT_SIZE_RANGE[1]:
            font_size = f"{value:g}"
    return {"theme": theme, "density": density, "font_size": font_size}


def system_prefers_light() -> bool:
    """Whether the OS asks apps for a light appearance. Windows only; else False."""
    if sys.platform != "win32":
        return False
    try:
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize",
        ) as key:
            value, _ = winreg.QueryValueEx(key, "AppsUseLightTheme")
        return bool(value)
    except OSError:
        return False


def resolved_theme(theme: str) -> str:
    """``dark`` or ``light`` for a saved theme, asking the OS for ``system``."""
    if theme == "system":
        return "light" if system_prefers_light() else "dark"
    return theme if theme in WINDOW_BACKGROUND else DEFAULT_THEME


def window_background(settings: Any) -> str:
    """The native window's background color for the saved theme."""
    return WINDOW_BACKGROUND[resolved_theme(page_appearance(settings)["theme"])]
