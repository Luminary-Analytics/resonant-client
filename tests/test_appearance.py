"""The saved appearance reaches the page and the desktop window.

The desktop window keeps no browser storage between launches (a new port and
private WebView storage each time), so the server must render the saved theme.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from lumi.gui import app as gui
from lumi.gui import appearance
from tests.gui_access import BASE_URL


class _Settings:
    def __init__(self, values: dict):
        self._values = values

    def get(self, section, key=None, default=None):
        return self._values.get(key, default)


@pytest.mark.parametrize(
    ("values", "expected"),
    [
        ({}, {"theme": "dark", "density": "comfortable", "font_size": None}),
        ({"theme": "light", "density": "compact", "font_size": "14"},
         {"theme": "light", "density": "compact", "font_size": "14"}),
        ({"theme": "system", "font_size": 13.5}, {"theme": "system", "density": "comfortable", "font_size": "13.5"}),
        ({"theme": "sepia", "density": "tiny", "font_size": "huge"},
         {"theme": "dark", "density": "comfortable", "font_size": None}),
        ({"font_size": "72"}, {"theme": "dark", "density": "comfortable", "font_size": None}),
    ],
)
def test_page_appearance_keeps_only_known_values(values, expected):
    assert appearance.page_appearance(_Settings(values)) == expected


def test_window_background_follows_the_saved_theme(monkeypatch):
    assert appearance.window_background(_Settings({"theme": "light"})) == "#F6F4EE"
    assert appearance.window_background(_Settings({})) == "#0F1126"
    monkeypatch.setattr(appearance, "system_prefers_light", lambda: True)
    assert appearance.window_background(_Settings({"theme": "system"})) == "#F6F4EE"
    monkeypatch.setattr(appearance, "system_prefers_light", lambda: False)
    assert appearance.window_background(_Settings({"theme": "system"})) == "#0F1126"


@pytest.fixture
def gui_state(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.delenv("LUMI_STATE_HOME", raising=False)
    state = gui.AppState()
    monkeypatch.setattr(gui, "state", state)
    return state


def _html_tag(client) -> tuple[str, dict]:
    page = client.get("/")
    assert page.status_code == 200
    return re.search(r"<html[^>]*>", page.text).group(0), page.headers


def test_page_renders_the_saved_appearance_before_any_script(gui_state):
    with TestClient(gui.app, base_url=BASE_URL) as client:
        tag, headers = _html_tag(client)
        assert tag == '<html lang="en" data-theme-setting="dark">'
        assert headers["cache-control"] == "no-store"

        gui_state.settings.set("appearance", "theme", "light")
        gui_state.settings.set("appearance", "density", "compact")
        gui_state.settings.set("appearance", "font_size", "14")
        tag, _ = _html_tag(client)
        # The font size is data: the page's CSP refuses style="" attributes,
        # so static/appearance.js applies it before the first paint.
        assert tag == ('<html lang="en" data-theme-setting="light" data-theme="light" '
                       'data-density="compact" data-font-size="14">')

        # The page resolves "system" against the OS (static/appearance.js).
        gui_state.settings.set("appearance", "theme", "system")
        tag, _ = _html_tag(client)
        assert 'data-theme-setting="system"' in tag
        assert "data-theme=" not in tag.replace("data-theme-setting=", "")
