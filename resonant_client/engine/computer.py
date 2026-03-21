"""
Desktop computer control via pyautogui + mss.

Provides tools for the agent to control the desktop:
take screenshots, click, type, press keys, scroll.

This implements the execution side of the Computer Use pattern:
  Agent sends actions -> we execute them -> return screenshots -> repeat

The auto-screenshot feature is the key to the vision loop:
after every click/type/scroll, we automatically capture a screenshot
and include it in the tool result. The backend then passes this image
back to the model so it can see the result and decide what to do next.

Usage:
    pip install pyautogui mss Pillow
"""

import base64
import io
import time
import logging
from typing import Optional
from .tools import ToolResult

logger = logging.getLogger(__name__)

# Auto-screenshot delay after actions (seconds)
_AUTO_SCREENSHOT_DELAY = 0.4


# ── Screenshot capture ───────────────────────────────────────────────

def _take_screenshot(region: dict = None) -> tuple[bytes, int, int]:
    """
    Take a screenshot using mss (fast, cross-platform).
    Falls back to pyautogui if mss unavailable.

    Returns (png_bytes, width, height).
    """
    try:
        import mss
        from PIL import Image

        with mss.mss() as sct:
            if region:
                monitor = {
                    "left": region.get("x", 0),
                    "top": region.get("y", 0),
                    "width": region.get("width", 800),
                    "height": region.get("height", 600),
                }
            else:
                monitor = sct.monitors[1]  # Primary monitor

            sct_img = sct.grab(monitor)
            img = Image.frombytes("RGB", sct_img.size, sct_img.bgra, "raw", "BGRX")

            # Resize if needed (Anthropic max: 1568px longest edge)
            w, h = img.size
            max_edge = 1568
            if max(w, h) > max_edge:
                scale = max_edge / max(w, h)
                new_w, new_h = int(w * scale), int(h * scale)
                img = img.resize((new_w, new_h), Image.LANCZOS)
                w, h = new_w, new_h

            buf = io.BytesIO()
            img.save(buf, format="PNG", optimize=True)
            return buf.getvalue(), w, h

    except ImportError:
        pass

    # Fallback: pyautogui
    try:
        import pyautogui
        from PIL import Image

        screenshot = pyautogui.screenshot(
            region=(region["x"], region["y"], region["width"], region["height"]) if region else None
        )

        w, h = screenshot.size
        max_edge = 1568
        if max(w, h) > max_edge:
            scale = max_edge / max(w, h)
            new_w, new_h = int(w * scale), int(h * scale)
            screenshot = screenshot.resize((new_w, new_h), Image.LANCZOS)
            w, h = new_w, new_h

        buf = io.BytesIO()
        screenshot.save(buf, format="PNG", optimize=True)
        return buf.getvalue(), w, h

    except ImportError:
        raise ImportError("Neither 'mss' nor 'pyautogui' installed. Run: pip install mss pyautogui Pillow")


def _attach_auto_screenshot(result: ToolResult) -> ToolResult:
    """
    Take a follow-up screenshot and attach it to the tool result.

    This is what closes the vision loop: after every action (click, type, scroll),
    we capture what the screen looks like now. The session passes this image
    back to the model so it can see the result and decide what to do next.
    """
    try:
        time.sleep(_AUTO_SCREENSHOT_DELAY)
        png_bytes, w, h = _take_screenshot()
        b64 = base64.b64encode(png_bytes).decode("utf-8")
        result.metadata["screenshot_b64"] = b64
        result.metadata["media_type"] = "image/png"
        result.metadata["width"] = w
        result.metadata["height"] = h
        result.output += f"\n[Auto-screenshot: {w}x{h}]"
    except Exception as e:
        logger.debug(f"Auto-screenshot failed (non-fatal): {e}")
    return result


# ── Desktop tool executors ───────────────────────────────────────────

def exec_computer_screenshot(args: dict, start: float) -> ToolResult:
    """
    Take a screenshot of the desktop.
    Returns base64 PNG for sending to vision models.
    """
    region = args.get("region")  # Optional {"x", "y", "width", "height"}

    try:
        png_bytes, w, h = _take_screenshot(region)
        b64 = base64.b64encode(png_bytes).decode("utf-8")
        size_kb = len(png_bytes) / 1024
        elapsed = time.time() - start

        return ToolResult(
            f"Desktop screenshot taken ({w}x{h}, {size_kb:.0f}KB)",
            elapsed=elapsed,
            metadata={
                "screenshot_b64": b64,
                "media_type": "image/png",
                "width": w,
                "height": h,
                "size_bytes": len(png_bytes),
            },
        )
    except ImportError as e:
        return ToolResult(str(e), is_error=True, elapsed=time.time() - start)
    except Exception as e:
        return ToolResult(f"Screenshot error: {e}", is_error=True, elapsed=time.time() - start)


def exec_computer_click(args: dict, start: float) -> ToolResult:
    """Click at desktop coordinates. Auto-captures a follow-up screenshot."""
    try:
        import pyautogui
    except ImportError:
        return ToolResult("Error: pyautogui not installed. Run: pip install pyautogui",
                         is_error=True, elapsed=time.time() - start)

    x = args.get("x", 0)
    y = args.get("y", 0)
    button = args.get("button", "left")  # left, right, middle
    clicks = args.get("clicks", 1)       # 1 = single, 2 = double
    screenshot = args.get("screenshot", True)  # Auto-screenshot after

    try:
        pyautogui.click(x=x, y=y, button=button, clicks=clicks)
        elapsed = time.time() - start
        click_type = "Double-clicked" if clicks == 2 else "Clicked"
        result = ToolResult(
            f"{click_type} at ({x}, {y}) [{button}]",
            elapsed=elapsed,
            metadata={"action": "click", "x": x, "y": y, "button": button},
        )
        if screenshot:
            result = _attach_auto_screenshot(result)
        return result
    except Exception as e:
        return ToolResult(f"Click error: {e}", is_error=True, elapsed=time.time() - start)


def exec_computer_type(args: dict, start: float) -> ToolResult:
    """Type text or press key combinations on the desktop. Auto-captures a follow-up screenshot."""
    try:
        import pyautogui
    except ImportError:
        return ToolResult("Error: pyautogui not installed. Run: pip install pyautogui",
                         is_error=True, elapsed=time.time() - start)

    text = args.get("text", "")
    key = args.get("key", "")        # e.g. "enter", "ctrl+s", "alt+tab"
    hotkey = args.get("hotkey", "")   # e.g. "ctrl+c" (alias for key)
    screenshot = args.get("screenshot", True)  # Auto-screenshot after

    try:
        if key or hotkey:
            combo = key or hotkey
            # Parse key combos like "ctrl+s", "alt+tab"
            keys = [k.strip() for k in combo.split("+")]
            if len(keys) > 1:
                pyautogui.hotkey(*keys)
                action = f"Pressed: {combo}"
            else:
                pyautogui.press(keys[0])
                action = f"Pressed: {keys[0]}"
        elif text:
            pyautogui.typewrite(text, interval=0.02) if text.isascii() else pyautogui.write(text)
            display = text[:50] + "..." if len(text) > 50 else text
            action = f"Typed: '{display}'"
        else:
            return ToolResult("Error: provide 'text' or 'key'",
                            is_error=True, elapsed=time.time() - start)

        elapsed = time.time() - start
        result = ToolResult(action, elapsed=elapsed, metadata={"action": "type"})
        if screenshot:
            result = _attach_auto_screenshot(result)
        return result
    except Exception as e:
        return ToolResult(f"Type error: {e}", is_error=True, elapsed=time.time() - start)


def exec_computer_scroll(args: dict, start: float) -> ToolResult:
    """Scroll at a position on screen. Auto-captures a follow-up screenshot."""
    try:
        import pyautogui
    except ImportError:
        return ToolResult("Error: pyautogui not installed. Run: pip install pyautogui",
                         is_error=True, elapsed=time.time() - start)

    x = args.get("x")
    y = args.get("y")
    direction = args.get("direction", "down")  # up, down
    amount = args.get("amount", 3)             # scroll clicks
    screenshot = args.get("screenshot", True)  # Auto-screenshot after

    try:
        scroll_val = amount if direction == "up" else -amount
        if x is not None and y is not None:
            pyautogui.scroll(scroll_val, x=x, y=y)
        else:
            pyautogui.scroll(scroll_val)

        elapsed = time.time() - start
        result = ToolResult(
            f"Scrolled {direction} {amount} clicks",
            elapsed=elapsed,
            metadata={"action": "scroll", "direction": direction, "amount": amount},
        )
        if screenshot:
            result = _attach_auto_screenshot(result)
        return result
    except Exception as e:
        return ToolResult(f"Scroll error: {e}", is_error=True, elapsed=time.time() - start)
