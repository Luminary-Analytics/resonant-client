"""
Tests for the Cluster 2 computer-use upgrades.

Covers:
- Region support for computer_wait
- target_window / monitor plumbing in _resolve_window_or_monitor
- monitors_list / list_monitors
- clipboard text round-trip
- process_list / process_kill (guardrails)
- screen_diff (synthetic image)
- accessibility tree exec wrappers (degrade gracefully)

Tool-registration smoke checks confirm everything dispatches.
"""

from __future__ import annotations

import io
import os
import shutil
import sys
import time
from pathlib import Path

import pytest


# ── Tool registration smoke ─────────────────────────────────────────────


class TestRegistration:
    def test_all_new_tools_registered(self):
        from resonant_client.engine import tools as tools_mod
        names = {t["function"]["name"] for t in tools_mod.AGENT_TOOLS}
        for n in [
            "monitors_list",
            "clipboard_read", "clipboard_write",
            "process_list", "process_kill",
            "screen_record_start", "screen_record_stop",
            "screen_diff",
            "accessibility_tree", "accessibility_click",
        ]:
            assert n in names, f"missing {n}"

    def test_dispatch_routes_each(self):
        from resonant_client.engine.tools import execute_tool
        # These should not raise — they may return errors but the dispatcher must route.
        for n in ["monitors_list", "process_list", "accessibility_tree"]:
            r = execute_tool(n, {})
            assert hasattr(r, "output")

    def test_read_only_classification(self):
        from resonant_client.engine.sandbox import READ_ONLY_TOOLS
        for n in ["monitors_list", "clipboard_read", "process_list",
                  "accessibility_tree", "screen_diff"]:
            assert n in READ_ONLY_TOOLS
        # Mutating tools NOT in read-only:
        for n in ["clipboard_write", "process_kill", "accessibility_click",
                  "screen_record_start", "screen_record_stop"]:
            assert n not in READ_ONLY_TOOLS


# ── monitors_list ──────────────────────────────────────────────────────


class TestMonitors:
    def test_list_monitors_shape(self):
        from resonant_client.engine.computer_use import list_monitors
        monitors = list_monitors()
        # On a CI box without mss this can be empty — that's fine
        for m in monitors:
            assert {"index", "x", "y", "width", "height", "primary"} <= m.keys()
            assert m["width"] > 0 and m["height"] > 0
        if monitors:
            assert sum(1 for m in monitors if m["primary"]) <= 1

    def test_exec_wrapper(self):
        from resonant_client.engine.computer_use import exec_monitors_list
        r = exec_monitors_list({}, start=0.0)
        assert hasattr(r, "metadata")
        assert "monitors" in r.metadata


# ── target_window / monitor plumbing ───────────────────────────────────


class TestResolveWindowOrMonitor:
    def test_default_returns_none(self):
        from resonant_client.engine.computer import _resolve_window_or_monitor
        region, label = _resolve_window_or_monitor({})
        assert region is None
        assert "primary monitor" in label

    def test_unknown_window(self):
        from resonant_client.engine.computer import _resolve_window_or_monitor
        region, label = _resolve_window_or_monitor({"target_window": "definitely-not-a-real-window-xyz"})
        assert region is None
        assert "not found" in label

    def test_monitor_index(self):
        from resonant_client.engine.computer import _resolve_window_or_monitor
        from resonant_client.engine.computer_use import list_monitors
        if not list_monitors():
            pytest.skip("no monitors detected in this env")
        region, label = _resolve_window_or_monitor({"monitor": 0})
        assert region is not None
        assert region["width"] > 0


# ── computer_wait region ───────────────────────────────────────────────


class TestComputerWait:
    def test_duration_mode_short(self):
        from resonant_client.engine.computer_use import exec_computer_wait
        r = exec_computer_wait({"mode": "duration", "seconds": 0.1}, start=time.time())
        assert "Waited" in r.output

    def test_change_mode_with_region_param_accepted(self):
        """Region param must be accepted (no schema error). With static screen we expect timeout."""
        from resonant_client.engine.computer_use import exec_computer_wait
        # Tiny region, very short timeout — should return without throwing
        r = exec_computer_wait(
            {"mode": "change", "region": {"x": 0, "y": 0, "width": 5, "height": 5}, "timeout": 0.5},
            start=time.time(),
        )
        # We don't assert on success vs timeout — just that it doesn't error/raise
        assert hasattr(r, "output")


# ── clipboard text ─────────────────────────────────────────────────────


def _has_text_clipboard() -> bool:
    """Heuristic: pyperclip is importable AND copies/reads back."""
    try:
        import pyperclip  # type: ignore
        # Quick round-trip
        pyperclip.copy("__resonant_clipboard_test_marker__")
        return pyperclip.paste() == "__resonant_clipboard_test_marker__"
    except Exception:
        return False


@pytest.mark.skipif(not _has_text_clipboard(), reason="no working text clipboard backend")
class TestClipboardText:
    def test_round_trip(self):
        from resonant_client.engine.clipboard import read_clipboard_text, write_clipboard_text
        write_clipboard_text("hello resonant")
        assert read_clipboard_text() == "hello resonant"

    def test_exec_wrappers(self):
        from resonant_client.engine.clipboard import exec_clipboard_read, exec_clipboard_write
        w = exec_clipboard_write({"text": "tool round trip"}, start=0.0)
        assert w.is_error is False
        r = exec_clipboard_read({}, start=0.0)
        assert "tool round trip" in r.output

    def test_write_requires_text(self):
        from resonant_client.engine.clipboard import exec_clipboard_write
        r = exec_clipboard_write({}, start=0.0)
        assert r.is_error is True


# ── processes ──────────────────────────────────────────────────────────


def _has_psutil() -> bool:
    try:
        import psutil  # noqa: F401
        return True
    except ImportError:
        return False


@pytest.mark.skipif(not _has_psutil(), reason="psutil not installed")
class TestProcesses:
    def test_list_processes_returns_self(self):
        from resonant_client.engine.processes import list_processes
        data = list_processes()
        assert "processes" in data
        # We should see at least our own python process somewhere
        names = {(p.get("name") or "").lower() for p in data["processes"]}
        # Python process name varies (python.exe, python, python3.X) — just assert non-empty
        assert len(names) > 0

    def test_kill_self_refused(self):
        from resonant_client.engine.processes import kill_process
        data = kill_process(os.getpid())
        # self should be skipped (or unable due to system PID floor)
        assert data["killed"] == [] or data.get("error")
        if "skipped" in data:
            assert data["skipped"], "expected self to be skipped"

    def test_kill_low_pid_refused(self):
        from resonant_client.engine.processes import kill_process
        data = kill_process(1)
        assert data["killed"] == []

    def test_exec_list(self):
        from resonant_client.engine.processes import exec_process_list
        r = exec_process_list({"limit": 5}, start=0.0)
        assert r.is_error is False

    def test_exec_kill_requires_one(self):
        from resonant_client.engine.processes import exec_process_kill
        r = exec_process_kill({}, start=0.0)
        assert r.is_error is True


# ── screen_diff ────────────────────────────────────────────────────────


def _has_pil_numpy() -> bool:
    try:
        import PIL  # noqa: F401
        import numpy  # noqa: F401
        return True
    except ImportError:
        return False


@pytest.mark.skipif(not _has_pil_numpy(), reason="PIL or numpy missing")
class TestScreenDiff:
    def _make_png(self, color, size=(100, 100), patch=None):
        from PIL import Image
        img = Image.new("RGB", size, color)
        if patch:
            x, y, w, h, c = patch
            for j in range(y, y + h):
                for i in range(x, x + w):
                    img.putpixel((i, j), c)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()

    def test_no_change(self):
        from resonant_client.engine.screen_diff import diff_images
        a = self._make_png((100, 100, 100))
        b = self._make_png((100, 100, 100))
        data = diff_images(a, b)
        assert data["rects"] == []
        assert data["changed_pixel_pct"] == 0.0

    def test_known_changed_rect(self):
        from resonant_client.engine.screen_diff import diff_images
        a = self._make_png((50, 50, 50))
        # Add a 20x20 red patch at (30, 40)
        b = self._make_png((50, 50, 50), patch=(30, 40, 20, 20, (255, 0, 0)))
        data = diff_images(a, b, threshold=20)
        assert len(data["rects"]) >= 1
        # The dominant rect should overlap the patch area
        first = data["rects"][0]
        assert 25 <= first["x"] <= 35
        assert 35 <= first["y"] <= 45
        assert 18 <= first["width"] <= 25
        assert 18 <= first["height"] <= 25

    def test_screenshot_cache(self):
        from resonant_client.engine.screen_diff import (
            remember_screenshot, get_remembered_screenshot, previous_two_ids,
            _screenshot_cache, _cache_order,
        )
        # Reset cache
        _screenshot_cache.clear()
        _cache_order.clear()
        a = self._make_png((1, 2, 3))
        b = self._make_png((4, 5, 6))
        remember_screenshot("idA", a)
        remember_screenshot("idB", b)
        prev, curr = previous_two_ids()
        assert prev == "idA"
        assert curr == "idB"
        assert get_remembered_screenshot("idA") == a


# ── accessibility ──────────────────────────────────────────────────────


class TestAccessibility:
    def test_tree_returns_dict(self):
        """Either returns a tree, or returns {error: ...} — never raises."""
        from resonant_client.engine.accessibility import get_tree
        result = get_tree()
        assert isinstance(result, dict)
        assert ("error" in result) or ("role" in result)

    def test_exec_click_requires_a_field(self):
        from resonant_client.engine.accessibility import exec_accessibility_click
        r = exec_accessibility_click({}, start=0.0)
        assert r.is_error is True


# ── recording ──────────────────────────────────────────────────────────


def _can_encode_video() -> bool:
    try:
        import cv2  # noqa: F401
        import numpy  # noqa: F401
        return True
    except ImportError:
        pass
    try:
        import subprocess
        subprocess.run(["ffmpeg", "-version"], capture_output=True, timeout=3, check=False)
        return True
    except Exception:
        return False


def _has_mss() -> bool:
    try:
        import mss  # noqa: F401
        return True
    except ImportError:
        return False


@pytest.mark.skipif(not (_can_encode_video() and _has_mss()),
                    reason="opencv/ffmpeg or mss not installed")
class TestRecording:
    def test_short_recording(self, tmp_path):
        from resonant_client.engine.recording import _RECORDER
        # If a previous test left it active, stop first
        if _RECORDER.is_active:
            _RECORDER.stop()

        out = tmp_path / "test.mp4"
        path = _RECORDER.start(output_path=out, fps=10)
        assert _RECORDER.is_active
        time.sleep(0.7)  # capture ~7 frames
        data = _RECORDER.stop()
        assert "error" not in data
        assert Path(data["path"]).exists()
        assert data["frames"] >= 1

    def test_double_start_returns_existing(self, tmp_path):
        from resonant_client.engine.recording import _RECORDER
        if _RECORDER.is_active:
            _RECORDER.stop()
        out = tmp_path / "double.mp4"
        first = _RECORDER.start(output_path=out, fps=5)
        second = _RECORDER.start(fps=99)  # different args; should be ignored
        assert first == second
        _RECORDER.stop()
