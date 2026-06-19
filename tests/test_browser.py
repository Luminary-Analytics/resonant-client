"""Tests for engine/browser.py — the v0.6.5 real-Chrome CDP attach flow.

Browser automation previously launched a bundled, logged-out Chromium.
v0.6.5 attaches to the user's INSTALLED Chrome over CDP (their real
profiles + logins, visible/shared window), launching it with the debug
port + chosen profile when no debug endpoint is already up, and falling
back to bundled Chromium only when real Chrome can't be used.

These tests mock the browser/subprocess layer so they need neither a real
Chrome nor Playwright installed.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from resonant_client.engine import browser as br
from resonant_client.engine.browser import (
    BrowserManager,
    _default_user_data_dir,
    _find_chrome,
)


class TestFindChrome:
    @pytest.mark.unit
    def test_explicit_override_wins(self, tmp_path, monkeypatch):
        fake = tmp_path / "chrome.exe"
        fake.write_text("x")
        monkeypatch.setenv("RESONANT_BROWSER_CHROME_PATH", str(fake))
        assert _find_chrome() == str(fake)

    @pytest.mark.unit
    def test_returns_none_when_nothing_found(self, monkeypatch):
        monkeypatch.delenv("RESONANT_BROWSER_CHROME_PATH", raising=False)
        monkeypatch.setattr(br.os.path, "isfile", lambda p: False)
        monkeypatch.setattr(br.shutil, "which", lambda c: None)
        assert _find_chrome() is None


class TestDefaultUserDataDir:
    @pytest.mark.unit
    def test_override_wins(self, monkeypatch):
        monkeypatch.setenv("RESONANT_BROWSER_USER_DATA_DIR", "/custom/dir")
        assert _default_user_data_dir() == "/custom/dir"


class TestConnectOrLaunch:
    @pytest.mark.unit
    def test_attaches_to_already_running_debug_chrome(self, monkeypatch):
        mgr = BrowserManager()
        monkeypatch.setattr(mgr, "_cdp_alive", lambda ep: True)
        connect = MagicMock(return_value="Connected to Chrome at http://127.0.0.1:9222. Current page: x")
        monkeypatch.setattr(mgr, "connect_cdp", connect)
        monkeypatch.setattr(mgr, "_open_agent_tab", lambda: None)
        # Real Chrome must NOT be launched when one is already attachable.
        monkeypatch.setattr(br.subprocess, "Popen", MagicMock(side_effect=AssertionError("should not launch")))
        out = mgr.connect_or_launch_chrome()
        assert "Connected" in out
        connect.assert_called_once()

    @pytest.mark.unit
    def test_falls_back_to_chromium_when_chrome_missing(self, monkeypatch):
        mgr = BrowserManager()
        monkeypatch.setattr(mgr, "_cdp_alive", lambda ep: False)
        monkeypatch.setattr(br, "_find_chrome", lambda: None)
        launched = MagicMock(return_value="Browser launched (Chromium, 1280x720).")
        monkeypatch.setattr(mgr, "launch", launched)
        out = mgr.connect_or_launch_chrome()
        assert "Chromium" in out
        launched.assert_called_once()

    @pytest.mark.unit
    def test_launches_real_chrome_with_profile_then_attaches(self, monkeypatch):
        mgr = BrowserManager()
        monkeypatch.setattr(mgr, "_cdp_alive", lambda ep: False)
        monkeypatch.setattr(br, "_find_chrome", lambda: "/path/to/chrome")
        monkeypatch.setattr(br, "_default_user_data_dir", lambda: "/udd")
        popen = MagicMock(return_value=MagicMock())
        monkeypatch.setattr(br.subprocess, "Popen", popen)
        monkeypatch.setattr(mgr, "_wait_for_cdp", lambda ep, timeout=20.0: True)
        monkeypatch.setattr(mgr, "connect_cdp", MagicMock(return_value="Connected to Chrome at ..."))
        monkeypatch.setattr(mgr, "_open_agent_tab", lambda: None)

        out = mgr.connect_or_launch_chrome(profile="Profile 1", port=9333)
        assert "Connected" in out
        cmd = popen.call_args[0][0]          # the argv list passed to Popen
        assert cmd[0] == "/path/to/chrome"
        assert "--remote-debugging-port=9333" in cmd
        assert "--profile-directory=Profile 1" in cmd
        assert "--user-data-dir=/udd" in cmd

    @pytest.mark.unit
    def test_port_never_comes_up_returns_clear_error(self, monkeypatch):
        mgr = BrowserManager()
        monkeypatch.setattr(mgr, "_cdp_alive", lambda ep: False)
        monkeypatch.setattr(br, "_find_chrome", lambda: "/path/to/chrome")
        monkeypatch.setattr(br, "_default_user_data_dir", lambda: "/udd")
        monkeypatch.setattr(br.subprocess, "Popen", MagicMock(return_value=MagicMock()))
        monkeypatch.setattr(mgr, "_wait_for_cdp", lambda ep, timeout=20.0: False)
        out = mgr.connect_or_launch_chrome()
        assert "never came up" in out

    @pytest.mark.unit
    def test_already_connected_is_noop(self):
        mgr = BrowserManager()
        mgr._connected = True
        assert mgr.connect_or_launch_chrome() == "Browser already connected."


class TestClose:
    @pytest.mark.unit
    def test_close_terminates_chrome_we_launched(self):
        mgr = BrowserManager()
        proc = MagicMock()
        mgr._chrome_proc = proc
        mgr.close()
        proc.terminate.assert_called_once()
        assert mgr._chrome_proc is None

    @pytest.mark.unit
    def test_close_leaves_attached_chrome_alone(self):
        # When we only attached (no _chrome_proc), close() must not try to
        # kill anything — the user's Chrome keeps running.
        mgr = BrowserManager()
        mgr._chrome_proc = None
        mgr.close()  # must not raise
        assert mgr._chrome_proc is None


class TestRelaunchInDebug:
    @pytest.mark.unit
    def test_noop_when_already_connected(self):
        mgr = BrowserManager()
        mgr._connected = True
        assert mgr.relaunch_in_debug() == "Browser already connected."

    @pytest.mark.unit
    def test_closes_gracefully_then_connects(self, monkeypatch):
        mgr = BrowserManager()
        closed = []
        monkeypatch.setattr(mgr, "_close_all_chrome", lambda force=False: closed.append(force))
        monkeypatch.setattr(mgr, "_wait_chrome_exited", lambda timeout=12.0: True)
        col = MagicMock(return_value="Connected to Chrome at http://127.0.0.1:9222.")
        monkeypatch.setattr(mgr, "connect_or_launch_chrome", col)
        out = mgr.relaunch_in_debug(profile="Default")
        assert "Connected" in out
        assert closed == [False]          # graceful (not force) close first
        col.assert_called_once()

    @pytest.mark.unit
    def test_reports_when_chrome_wont_close(self, monkeypatch):
        mgr = BrowserManager()
        monkeypatch.setattr(mgr, "_close_all_chrome", lambda force=False: None)
        monkeypatch.setattr(mgr, "_wait_chrome_exited", lambda timeout=12.0: False)
        out = mgr.relaunch_in_debug()
        assert "Couldn't fully close Chrome" in out


class TestChromeRunning:
    @pytest.mark.unit
    def test_true_when_tasklist_lists_chrome(self, monkeypatch):
        monkeypatch.setattr(br.sys, "platform", "win32")
        monkeypatch.setattr(br.subprocess, "run",
                            lambda *a, **k: MagicMock(stdout="chrome.exe  1234 Console", returncode=0))
        assert BrowserManager._chrome_running() is True

    @pytest.mark.unit
    def test_false_when_no_chrome(self, monkeypatch):
        monkeypatch.setattr(br.sys, "platform", "win32")
        monkeypatch.setattr(br.subprocess, "run",
                            lambda *a, **k: MagicMock(stdout="INFO: No tasks are running.", returncode=0))
        assert BrowserManager._chrome_running() is False
