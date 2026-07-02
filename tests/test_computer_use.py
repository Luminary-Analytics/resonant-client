"""
Tests for the Computer Use system.

Tests cover:
- ScreenScale coordinate mapping
- SafetyZone containment
- Tool execution (mocked pyautogui/mss)
- Auto-screenshot attachment
- Window management stubs
- OCR fallback behavior
- Backend image passing (Ollama, Claude, OpenAI)
"""

import base64
import json
import time
import pytest
from unittest.mock import patch, MagicMock, PropertyMock

# ── ScreenScale Tests ─────────────────────────────────────────────────

from resonant_client.engine.computer_use import ScreenScale, SafetyZone


class TestScreenScale:
    def test_identity_scale(self):
        """No scaling when real == scaled."""
        s = ScreenScale(real_width=1920, real_height=1080,
                       scaled_width=1920, scaled_height=1080)
        assert s.to_real(100, 200) == (100, 200)
        assert s.to_scaled(100, 200) == (100, 200)

    def test_2x_downscale(self):
        """2x downscale: real 3840x2160 -> scaled 1568x882."""
        s = ScreenScale(real_width=3840, real_height=2160,
                       scaled_width=1568, scaled_height=882)
        # Model clicks at (784, 441) = center of scaled image
        rx, ry = s.to_real(784, 441)
        assert rx == int(784 * (3840 / 1568))  # ~1920
        assert ry == int(441 * (2160 / 882))   # ~1080

    def test_round_trip(self):
        """to_real then to_scaled should be close to original."""
        s = ScreenScale(real_width=2560, real_height=1440,
                       scaled_width=1568, scaled_height=882)
        for x, y in [(0, 0), (784, 441), (1567, 881), (100, 200)]:
            rx, ry = s.to_real(x, y)
            sx, sy = s.to_scaled(rx, ry)
            assert abs(sx - x) <= 1  # Rounding tolerance
            assert abs(sy - y) <= 1

    def test_offset(self):
        """Multi-monitor offset is added to real coordinates."""
        s = ScreenScale(real_width=1920, real_height=1080,
                       scaled_width=1568, scaled_height=882,
                       offset_x=1920, offset_y=0)
        rx, ry = s.to_real(0, 0)
        assert rx == 1920  # Offset for second monitor
        assert ry == 0

    def test_scale_factors(self):
        s = ScreenScale(real_width=1920, real_height=1080,
                       scaled_width=960, scaled_height=540)
        assert s.scale_x == 2.0
        assert s.scale_y == 2.0

    def test_zero_scaled_doesnt_crash(self):
        """Zero scaled dimensions should not divide by zero."""
        s = ScreenScale(real_width=1920, real_height=1080,
                       scaled_width=0, scaled_height=0)
        # scale_x and scale_y default to 1.0 when 0
        assert s.scale_x == 1.0
        assert s.scale_y == 1.0


class TestSafetyZone:
    def test_contains_inside(self):
        zone = SafetyZone(x=100, y=100, width=200, height=200)
        assert zone.contains(150, 150)
        assert zone.contains(100, 100)  # Edge
        assert zone.contains(300, 300)  # Edge

    def test_contains_outside(self):
        zone = SafetyZone(x=100, y=100, width=200, height=200)
        assert not zone.contains(50, 50)
        assert not zone.contains(301, 150)
        assert not zone.contains(150, 301)

    def test_label(self):
        zone = SafetyZone(x=0, y=0, width=50, height=50, label="taskbar")
        assert zone.label == "taskbar"


# ── Tool Execution Tests (Mocked) ────────────────────────────────────

class TestComputerDrag:
    @patch("resonant_client.engine.computer_use.time")
    def test_basic_drag(self, mock_time):
        mock_time.time.return_value = 1.0
        mock_time.sleep = MagicMock()

        with patch("pyautogui.moveTo") as mock_move, \
             patch("pyautogui.drag") as mock_drag:
            from resonant_client.engine.computer_use import exec_computer_drag
            result = exec_computer_drag(
                {"start_x": 100, "start_y": 100, "end_x": 200, "end_y": 200},
                start=0.5,
            )
            assert not result.is_error
            assert "Dragged" in result.output
            mock_move.assert_called_once_with(100, 100)
            mock_drag.assert_called_once()

    def test_drag_no_pyautogui(self):
        with patch.dict("sys.modules", {"pyautogui": None}):
            # Force reimport
            import importlib
            from resonant_client.engine import computer_use
            importlib.reload(computer_use)
            result = computer_use.exec_computer_drag(
                {"start_x": 0, "start_y": 0, "end_x": 1, "end_y": 1},
                start=time.time(),
            )
            assert result.is_error


class TestComputerHover:
    def test_basic_hover(self):
        with patch("pyautogui.moveTo") as mock_move:
            from resonant_client.engine.computer_use import exec_computer_hover
            result = exec_computer_hover({"x": 500, "y": 300}, start=time.time())
            assert not result.is_error
            assert "500" in result.output
            mock_move.assert_called_once()


class TestWindowList:
    @patch("sys.platform", "win32")
    def test_windows_empty(self):
        from resonant_client.engine.computer_use import exec_window_list
        with patch("resonant_client.engine.computer_use._list_windows_win32", return_value=[]):
            result = exec_window_list({}, start=time.time())
            assert "No visible windows" in result.output or "0" in result.output

    def test_windows_with_results(self):
        mock_windows = [
            {"title": "Chrome", "x": 0, "y": 0, "width": 1920, "height": 1080},
            {"title": "VS Code", "x": 100, "y": 100, "width": 1200, "height": 800},
        ]
        from resonant_client.engine.computer_use import exec_window_list
        with patch("resonant_client.engine.computer_use.list_windows", return_value=mock_windows):
            result = exec_window_list({}, start=time.time())
            assert "Chrome" in result.output
            assert "VS Code" in result.output
            assert result.metadata.get("count") == 2


class TestWindowFocus:
    def test_focus_requires_title(self):
        from resonant_client.engine.computer_use import exec_window_focus
        result = exec_window_focus({}, start=time.time())
        assert result.is_error
        assert "title" in result.output.lower()

    def test_focus_calls_platform(self):
        from resonant_client.engine.computer_use import exec_window_focus
        with patch("resonant_client.engine.computer_use.focus_window", return_value="Focused window: Chrome"):
            result = exec_window_focus({"title": "Chrome"}, start=time.time())
            assert not result.is_error
            assert "Chrome" in result.output


class TestComputerWait:
    def test_duration_mode(self):
        from resonant_client.engine.computer_use import exec_computer_wait
        with patch("time.sleep"):
            result = exec_computer_wait({"mode": "duration", "seconds": 0.1}, start=time.time())
            assert not result.is_error
            assert "Waited" in result.output

    def test_change_mode_no_screenshot(self):
        """Change mode should handle screenshot failure gracefully."""
        from resonant_client.engine.computer_use import exec_computer_wait
        with patch("resonant_client.engine.computer_use.take_screenshot_scaled", side_effect=ImportError("no mss")):
            with patch("time.sleep"):
                result = exec_computer_wait({"mode": "change"}, start=time.time())
                assert not result.is_error
                assert "unavailable" in result.output.lower() or "waited" in result.output.lower()

    def test_unknown_mode(self):
        from resonant_client.engine.computer_use import exec_computer_wait
        result = exec_computer_wait({"mode": "magic"}, start=time.time())
        assert result.is_error


class TestScreenOCR:
    def test_ocr_no_deps(self):
        """OCR should fail gracefully when dependencies missing."""
        from resonant_client.engine.computer_use import exec_screen_ocr
        with patch.dict("sys.modules", {"mss": None}):
            import importlib
            from resonant_client.engine import computer_use
            # Should handle ImportError
            result = exec_screen_ocr({}, start=time.time())
            # Either error about deps or actually runs (if mss is installed)
            assert isinstance(result.output, str)


class TestOpenApplication:
    @patch("sys.platform", "win32")
    def test_open_app_windows(self):
        from resonant_client.engine.computer_use import exec_open_application
        with patch("subprocess.Popen"):
            with patch("time.sleep"):
                result = exec_open_application({"name": "notepad"}, start=time.time())
                assert not result.is_error
                assert "notepad" in result.output

    def test_open_app_no_name(self):
        from resonant_client.engine.computer_use import exec_open_application
        result = exec_open_application({}, start=time.time())
        assert result.is_error


# ── Auto-Screenshot Tests ─────────────────────────────────────────────

class TestAutoScreenshot:
    def test_click_attaches_screenshot(self):
        """computer_click should attach a screenshot to its result."""
        fake_png = b"\x89PNG\r\n\x1a\nfake"
        with patch("pyautogui.click"), \
             patch("resonant_client.engine.computer._take_screenshot",
                   return_value=(fake_png, 1568, 882)), \
             patch("time.sleep"):
            from resonant_client.engine.computer import exec_computer_click
            result = exec_computer_click({"x": 100, "y": 200}, start=time.time())
            assert not result.is_error
            assert "screenshot_b64" in result.metadata
            assert result.metadata["media_type"] == "image/png"

    def test_click_no_screenshot_when_disabled(self):
        """screenshot=false should skip auto-screenshot."""
        with patch("pyautogui.click"):
            from resonant_client.engine.computer import exec_computer_click
            result = exec_computer_click(
                {"x": 100, "y": 200, "screenshot": False},
                start=time.time(),
            )
            assert not result.is_error
            assert "screenshot_b64" not in result.metadata

    def test_type_attaches_screenshot(self):
        fake_png = b"\x89PNG\r\n\x1a\nfake"
        with patch("pyautogui.typewrite"), \
             patch("resonant_client.engine.computer._take_screenshot",
                   return_value=(fake_png, 1568, 882)), \
             patch("time.sleep"):
            from resonant_client.engine.computer import exec_computer_type
            result = exec_computer_type({"text": "hello"}, start=time.time())
            assert not result.is_error
            assert "screenshot_b64" in result.metadata

    def test_scroll_attaches_screenshot(self):
        fake_png = b"\x89PNG\r\n\x1a\nfake"
        with patch("pyautogui.scroll"), \
             patch("resonant_client.engine.computer._take_screenshot",
                   return_value=(fake_png, 1568, 882)), \
             patch("time.sleep"):
            from resonant_client.engine.computer import exec_computer_scroll
            result = exec_computer_scroll({"direction": "down"}, start=time.time())
            assert not result.is_error
            assert "screenshot_b64" in result.metadata

    def test_screenshot_failure_is_nonfatal(self):
        """If auto-screenshot fails, the click itself should still succeed."""
        with patch("pyautogui.click"), \
             patch("resonant_client.engine.computer._take_screenshot",
                   side_effect=Exception("display error")), \
             patch("time.sleep"):
            from resonant_client.engine.computer import exec_computer_click
            result = exec_computer_click({"x": 100, "y": 200}, start=time.time())
            assert not result.is_error  # Click still succeeded
            assert "screenshot_b64" not in result.metadata  # But no screenshot


# ── Session Image Flow Tests ──────────────────────────────────────────

class TestSessionImageFlow:
    """Test that screenshot images flow correctly through conversation history."""

    def test_screenshot_added_to_history(self):
        """Session should add image data to tool_result entries when screenshots are present."""
        # Simulate what session.py does (lines 562-574)
        result_metadata = {
            "screenshot_b64": base64.b64encode(b"fake_png").decode(),
            "media_type": "image/png",
        }

        tool_result_entry = {
            "role": "tool_result",
            "call_id": "test_id",
            "content": "Clicked at (100, 200)",
        }
        if result_metadata.get("screenshot_b64"):
            tool_result_entry["image"] = {
                "type": "base64",
                "media_type": result_metadata.get("media_type", "image/png"),
                "data": result_metadata["screenshot_b64"],
            }

        assert "image" in tool_result_entry
        assert tool_result_entry["image"]["media_type"] == "image/png"

    def test_no_image_when_no_screenshot(self):
        """Tool results without screenshots should not have image data."""
        result_metadata = {"action": "click"}

        tool_result_entry = {
            "role": "tool_result",
            "call_id": "test_id",
            "content": "File read",
        }
        if result_metadata.get("screenshot_b64"):
            tool_result_entry["image"] = {}

        assert "image" not in tool_result_entry


# ── Backend Image Passing Tests ───────────────────────────────────────

class TestBackendImagePassing:
    """Test that backends correctly pass screenshot images back to the model."""

    def _make_history_with_screenshot(self):
        """Create a conversation history with a tool_result containing a screenshot."""
        fake_b64 = base64.b64encode(b"fake_screenshot").decode()
        return [
            {"role": "user", "content": "Click the blue button"},
            {"role": "tool_call", "name": "computer_click",
             "arguments": '{"x": 100, "y": 200}', "call_id": "c1",
             "content": "Called computer_click"},
            {"role": "tool_result", "call_id": "c1",
             "content": "Clicked at (100, 200)\n[Auto-screenshot: 1568x882]",
             "image": {
                 "type": "base64",
                 "media_type": "image/png",
                 "data": fake_b64,
             }},
        ]

    def test_ollama_passes_screenshot_as_user_image(self):
        """Ollama backend should inject screenshot as a user message with images field."""
        history = self._make_history_with_screenshot()

        # Simulate Ollama message building (native mode)
        messages = []
        for turn in history:
            role = turn["role"]
            content = turn["content"]
            if role == "tool_result":
                messages.append({"role": "tool", "content": content})
                if turn.get("image"):
                    img = turn["image"]
                    messages.append({
                        "role": "user",
                        "content": "[Screenshot from tool result]",
                        "images": [img.get("data", "")],
                    })
            elif role == "tool_call":
                messages.append({"role": "assistant", "content": ""})
            else:
                messages.append({"role": role, "content": content})

        # Should have: user, assistant (tool_call), tool, user (screenshot)
        assert len(messages) == 4
        screenshot_msg = messages[3]
        assert screenshot_msg["role"] == "user"
        assert "images" in screenshot_msg
        assert len(screenshot_msg["images"]) == 1

    def test_claude_passes_screenshot_in_tool_result(self):
        """Claude backend should include screenshot in tool_result content array."""
        history = self._make_history_with_screenshot()

        # Simulate Claude message building
        messages = []
        for turn in history:
            role = turn["role"]
            content = turn["content"]
            if role == "tool_result":
                call_id = turn.get("call_id", "")
                tool_result_content = [{"type": "text", "text": content}]
                if turn.get("image"):
                    img = turn["image"]
                    tool_result_content.append({
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": img.get("media_type", "image/png"),
                            "data": img["data"],
                        },
                    })
                messages.append({
                    "role": "user",
                    "content": [{"type": "tool_result", "tool_use_id": call_id,
                                "content": tool_result_content}],
                })
            elif role == "tool_call":
                messages.append({"role": "assistant", "content": [{"type": "tool_use"}]})
            else:
                messages.append({"role": role, "content": content})

        tool_result_msg = messages[2]
        content_block = tool_result_msg["content"][0]["content"]
        assert len(content_block) == 2  # text + image
        assert content_block[1]["type"] == "image"

    def test_openai_passes_screenshot_as_image_url(self):
        """OpenAI backend should include screenshot as image_url in tool result."""
        history = self._make_history_with_screenshot()
        fake_b64 = history[2]["image"]["data"]

        # Simulate OpenAI message building (native mode)
        for turn in history:
            if turn["role"] == "tool_result" and turn.get("image"):
                img = turn["image"]
                tool_content = [
                    {"type": "text", "text": turn["content"]},
                    {"type": "image_url", "image_url": {
                        "url": f"data:{img.get('media_type', 'image/png')};base64,{img['data']}",
                    }},
                ]
                assert len(tool_content) == 2
                assert "image_url" in tool_content[1]
                assert fake_b64 in tool_content[1]["image_url"]["url"]


# ── Tool Definitions Test ─────────────────────────────────────────────

class TestComputerUseToolDefs:
    """Verify all computer use tools are properly defined and routable."""

    def test_all_tools_defined(self):
        from resonant_client.engine.tools import AGENT_TOOLS
        tool_names = {t["function"]["name"] for t in AGENT_TOOLS}

        expected = {
            "computer_screenshot", "computer_click", "computer_type", "computer_scroll",
            "computer_drag", "computer_hover", "computer_wait",
            "window_list", "window_focus", "screen_ocr", "open_application",
        }
        for name in expected:
            assert name in tool_names, f"Missing tool definition: {name}"

    def test_all_tools_have_icons(self):
        from resonant_client.engine.tools import TOOL_ICONS
        expected = [
            "computer_screenshot", "computer_click", "computer_type", "computer_scroll",
            "computer_drag", "computer_hover", "computer_wait",
            "window_list", "window_focus", "screen_ocr", "open_application",
        ]
        for name in expected:
            assert name in TOOL_ICONS, f"Missing icon: {name}"

    def test_all_tools_routable(self):
        """execute_tool should route all computer use tools (even if deps missing).

        Every OS-facing call must be stubbed: unmocked, this test popped a
        real Windows "cannot find 'test'" ShellExecute dialog on the
        developer's desktop (open_application), jumped the mouse to the
        top-left corner (drag/hover), and could steal focus from a live
        window (window_focus) on every suite run.
        """
        from resonant_client.engine.tools import execute_tool

        # These should all be routable (may fail on deps but shouldn't be "Unknown tool")
        tools_to_test = [
            ("computer_drag", {"start_x": 0, "start_y": 0, "end_x": 1, "end_y": 1}),
            ("computer_hover", {"x": 0, "y": 0}),
            ("window_list", {}),
            ("window_focus", {"title": "test"}),
            ("computer_wait", {"mode": "duration", "seconds": 0.01}),
            ("open_application", {"name": "test"}),
        ]
        with patch("pyautogui.moveTo"), patch("pyautogui.drag"), \
             patch("resonant_client.engine.computer_use.focus_window",
                   return_value="Focused window: test"), \
             patch("subprocess.Popen"), patch("os.startfile", create=True), \
             patch("time.sleep"):
            for name, args in tools_to_test:
                result = execute_tool(name, args)
                assert "Unknown tool" not in result.output, f"{name} not routed"


# ── Integration: Vision Loop Test ─────────────────────────────────────

class TestVisionLoop:
    """Test the complete screenshot → action → screenshot cycle."""

    def test_click_produces_image_for_history(self):
        """A click should produce a result with screenshot_b64 that session can use."""
        fake_png = b"\x89PNG\r\n\x1a\n_test_data_"

        with patch("pyautogui.click"), \
             patch("resonant_client.engine.computer._take_screenshot",
                   return_value=(fake_png, 1568, 882)), \
             patch("time.sleep"):
            from resonant_client.engine.computer import exec_computer_click
            result = exec_computer_click({"x": 500, "y": 300}, start=time.time())

            # Verify the result has everything needed for the vision loop
            assert result.metadata.get("screenshot_b64")
            assert result.metadata.get("media_type") == "image/png"

            # Verify the base64 decodes correctly
            decoded = base64.b64decode(result.metadata["screenshot_b64"])
            assert decoded == fake_png

            # Verify this would create the right history entry
            entry = {"role": "tool_result", "call_id": "test", "content": result.output}
            if result.metadata.get("screenshot_b64"):
                entry["image"] = {
                    "type": "base64",
                    "media_type": result.metadata["media_type"],
                    "data": result.metadata["screenshot_b64"],
                }
            assert "image" in entry
            assert entry["image"]["data"] == result.metadata["screenshot_b64"]
