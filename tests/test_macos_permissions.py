"""macOS permissions for computer use (lumi/engine/macos_permissions.py)."""

from __future__ import annotations

import pytest

from lumi.engine import macos_permissions
from lumi.engine.tools import COMPUTER_ACCESS_TOOL_NAMES, execute_tool


@pytest.fixture
def mac(monkeypatch):
    monkeypatch.setattr(macos_permissions.sys, "platform", "darwin")
    answers = {"accessibility": True, "screen": True}
    monkeypatch.setattr(macos_permissions, "accessibility_allowed", lambda: answers["accessibility"])
    monkeypatch.setattr(macos_permissions, "screen_recording_allowed", lambda: answers["screen"])
    return answers


def test_each_desktop_tool_needs_a_permission_it_can_lack():
    covered = macos_permissions.NEEDS_ACCESSIBILITY | macos_permissions.NEEDS_SCREEN_RECORDING
    assert covered <= COMPUTER_ACCESS_TOOL_NAMES


def test_missing_permissions_are_named_with_where_to_allow_them(mac):
    assert macos_permissions.missing_permission("computer_click") == ""
    mac["accessibility"] = False
    assert "Accessibility" in macos_permissions.missing_permission("computer_click")
    assert macos_permissions.missing_permission("computer_screenshot") == ""  # needs the other one
    mac["screen"] = False
    message = macos_permissions.missing_permission("computer_screenshot")
    assert "Screen Recording" in message and "restart Lumi" in message
    assert macos_permissions.missing_permission("clipboard_read") == ""  # needs neither
    # An unknown answer (an older macOS) never blocks.
    mac["accessibility"] = None
    assert macos_permissions.missing_permission("computer_click") == ""


def test_other_systems_are_never_asked(monkeypatch):
    monkeypatch.setattr(macos_permissions.sys, "platform", "win32")
    monkeypatch.setattr(macos_permissions, "accessibility_allowed", lambda: False)
    assert macos_permissions.missing_permission("computer_click") == ""


def test_a_tool_without_its_permission_is_refused_before_it_runs(mac, monkeypatch):
    mac["accessibility"] = False
    import lumi.engine.computer as computer

    monkeypatch.setattr(computer, "exec_computer_click", lambda *a, **k: pytest.fail("clicked without permission"))
    result = execute_tool("computer_click", {"x": 10, "y": 10})
    assert result.is_error and result.metadata == {"missing_permission": True}
    assert "System Settings > Privacy & Security > Accessibility" in result.output
