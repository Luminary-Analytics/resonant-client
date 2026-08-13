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
import threading
import time
import logging
from typing import Optional
from .tools import ToolResult

logger = logging.getLogger(__name__)

# Auto-screenshot delay after actions (seconds)
_AUTO_SCREENSHOT_DELAY = 0.4

# Screenshot limits. Anthropic's vision API downsamples anything larger than
# 1568px on the long edge OR ~1.15 megapixels total — if we don't downsample
# ourselves, the model reasons in coordinates of an image we never saw.
_MAX_EDGE = 1568
_MAX_PIXELS = int(1.15 * 1024 * 1024)

# ── Capture-scale tracking ───────────────────────────────────────────
#
# The model gives click coordinates in the space of the screenshot it last
# looked at. When that screenshot was downscaled (high-DPI screens), those
# coordinates must be scaled back up to real screen pixels — otherwise every
# click on a 4K display lands short of the target. We remember the geometry
# of the most recent capture (explicit or auto) and map through it.

_LAST_CAPTURE_LOCK = threading.Lock()
_LAST_CAPTURE: Optional[dict] = None


def _remember_capture(real_w: int, real_h: int, scaled_w: int, scaled_h: int,
                      offset_x: int, offset_y: int) -> None:
    global _LAST_CAPTURE
    with _LAST_CAPTURE_LOCK:
        _LAST_CAPTURE = {
            "real_w": real_w, "real_h": real_h,
            "scaled_w": scaled_w, "scaled_h": scaled_h,
            "offset_x": offset_x, "offset_y": offset_y,
        }


def get_last_capture() -> Optional[dict]:
    """Geometry of the most recent screenshot sent to the model, or None."""
    with _LAST_CAPTURE_LOCK:
        return dict(_LAST_CAPTURE) if _LAST_CAPTURE else None


def _api_scale_ratio(w: int, h: int) -> float:
    """How much to shrink a capture to fit vision-API limits (<= 1.0)."""
    edge_scale = _MAX_EDGE / max(w, h) if max(w, h) > _MAX_EDGE else 1.0
    pixel_scale = (_MAX_PIXELS / (w * h)) ** 0.5 if w * h > _MAX_PIXELS else 1.0
    return min(edge_scale, pixel_scale)


def _map_model_coords(x: int, y: int, origin: Optional[dict]) -> tuple[int, int, bool]:
    """
    Map model-provided (x, y) to real screen pixels.

    Coordinates are interpreted in the image space of the last screenshot when
    that's coherent with the requested origin; scaling is identity when the
    screen was never downscaled, so this is backward compatible.

    Returns (real_x, real_y, was_scaled).
    """
    cap = get_last_capture()
    if not cap or cap["scaled_w"] <= 0 or cap["scaled_h"] <= 0:
        if origin is not None:
            return origin["x"] + x, origin["y"] + y, False
        return x, y, False

    sx = cap["real_w"] / cap["scaled_w"]
    sy = cap["real_h"] / cap["scaled_h"]
    scaled = sx > 1.001 or sy > 1.001

    if origin is None:
        # Model is pointing at the last screenshot it saw; its offsets place
        # the click on the right monitor/window.
        return int(round(x * sx)) + cap["offset_x"], int(round(y * sy)) + cap["offset_y"], scaled

    if (origin["x"], origin["y"]) == (cap["offset_x"], cap["offset_y"]):
        # Relative click on the same window/monitor the last capture showed.
        return origin["x"] + int(round(x * sx)), origin["y"] + int(round(y * sy)), scaled

    # Origin differs from the last capture — no scale information applies.
    return origin["x"] + x, origin["y"] + y, False


def _virtual_screen_bounds() -> Optional[tuple[int, int, int, int]]:
    """(left, top, right, bottom) of the combined desktop, or None if unknown."""
    try:
        import mss
        with mss.mss() as sct:
            union = sct.monitors[0]
            return (union["left"], union["top"],
                    union["left"] + union["width"], union["top"] + union["height"])
    except Exception:
        pass
    try:
        import pyautogui
        w, h = pyautogui.size()
        return (0, 0, w, h)
    except Exception:
        return None


def _validate_bounds(x: int, y: int) -> Optional[str]:
    """Return an error message when (x, y) is off-screen, else None."""
    bounds = _virtual_screen_bounds()
    if bounds is None:
        return None
    left, top, right, bottom = bounds
    if not (left <= x < right and top <= y < bottom):
        return (
            f"Coordinates ({x}, {y}) are outside the screen bounds "
            f"({left},{top})..({right},{bottom}). Take a fresh screenshot "
            f"and use coordinates within the image you receive."
        )
    return None


def _draw_cursor_crosshair(img, offset_x: int, offset_y: int, ratio: float) -> None:
    """
    Draw a red crosshair at the current cursor position (in image space).

    Screenshots don't include the pointer, so without this the model can't
    tell where its last click actually landed. With the crosshair it can
    compare aim vs. impact and correct its coordinates proportionally.
    """
    try:
        import pyautogui
        pos = pyautogui.position()
    except Exception:
        return
    try:
        from PIL import ImageDraw

        cx = int((pos[0] - offset_x) * ratio)
        cy = int((pos[1] - offset_y) * ratio)
        if not (0 <= cx < img.width and 0 <= cy < img.height):
            return
        draw = ImageDraw.Draw(img)
        size = 20
        draw.line([(cx - size, cy), (cx + size, cy)], fill=(255, 0, 0), width=3)
        draw.line([(cx, cy - size), (cx, cy + size)], fill=(255, 0, 0), width=3)
    except Exception:
        logger.debug("Cursor crosshair drawing failed (non-fatal)", exc_info=True)


# ── Screenshot capture ───────────────────────────────────────────────

def _take_screenshot(region: dict = None) -> tuple[bytes, int, int]:
    """
    Take a screenshot using mss (fast, cross-platform).
    Falls back to pyautogui if mss unavailable.

    Returns (png_bytes, width, height).

    The "Resonant is using the computer" halo is taken down for the duration of
    the grab. The agent decides where to click from these images, so leaving the
    overlay in them would feed Resonant's own border and banner back to the
    model as part of the application it is looking at — and the banner sits
    exactly where a title bar or toolbar usually is.
    """
    from .screen_overlay import hidden_for_capture

    with hidden_for_capture():
        return _grab(region)


def _finish_capture(img, offset_x: int, offset_y: int) -> tuple[bytes, int, int]:
    """Downscale to API limits, draw the cursor crosshair, remember geometry."""
    from PIL import Image

    real_w, real_h = img.size
    ratio = _api_scale_ratio(real_w, real_h)
    if ratio < 1.0:
        img = img.resize((int(real_w * ratio), int(real_h * ratio)), Image.LANCZOS)
    w, h = img.size

    _draw_cursor_crosshair(img, offset_x, offset_y, ratio)
    _remember_capture(real_w, real_h, w, h, offset_x, offset_y)

    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue(), w, h


def _grab(region: dict = None) -> tuple[bytes, int, int]:
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
            return _finish_capture(img, monitor["left"], monitor["top"])

    except ImportError:
        pass

    # Fallback: pyautogui
    try:
        import pyautogui

        screenshot = pyautogui.screenshot(
            region=(region["x"], region["y"], region["width"], region["height"]) if region else None
        )
        offset_x = region["x"] if region else 0
        offset_y = region["y"] if region else 0
        return _finish_capture(screenshot, offset_x, offset_y)

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

def _resolve_window_or_monitor(args: dict) -> tuple[Optional[dict], str]:
    """
    Resolve `target_window` or `monitor` args to a region/origin tuple.

    Precedence: target_window > monitor > (default: full primary monitor).
    Returns (region_dict_or_None, label).
    """
    target_window = (args.get("target_window") or "").strip()
    if target_window:
        from .computer_use import get_window_rect
        rect = get_window_rect(target_window)
        if rect:
            return ({"x": rect["x"], "y": rect["y"],
                     "width": rect["width"], "height": rect["height"]},
                    f"window '{rect.get('title','')}'")
        # Window not found — fall through to default; caller decides behavior.
        return (None, f"window not found: '{target_window}'")

    monitor = args.get("monitor")
    if monitor is not None:
        try:
            import mss
            with mss.mss() as sct:
                monitors = sct.monitors[1:]  # 0 is the virtual desktop union
                idx = int(monitor)
                if 0 <= idx < len(monitors):
                    m = monitors[idx]
                    return ({"x": m["left"], "y": m["top"],
                             "width": m["width"], "height": m["height"]},
                            f"monitor[{idx}]")
        except Exception:
            pass
    return (None, "primary monitor")


def exec_computer_screenshot(args: dict, start: float) -> ToolResult:
    """
    Take a screenshot of the desktop, a window, or a specific monitor.
    Returns base64 PNG for sending to vision models.

    Args:
        region:        Optional explicit bbox {x, y, width, height}.
        target_window: Optional window-title substring; screenshot just that window.
        monitor:       Optional monitor index (0 = primary).

    Precedence: region > target_window > monitor > full primary monitor.
    """
    import uuid as _uuid
    region = args.get("region")
    target_label = "explicit region" if region else None

    if not region:
        resolved_region, resolved_label = _resolve_window_or_monitor(args)
        if resolved_region is not None:
            region = resolved_region
            target_label = resolved_label
        else:
            target_label = resolved_label  # may be a "not found" message

    try:
        png_bytes, w, h = _take_screenshot(region)
        b64 = base64.b64encode(png_bytes).decode("utf-8")
        size_kb = len(png_bytes) / 1024
        elapsed = time.time() - start

        # Cache for screen_diff
        screenshot_id = _uuid.uuid4().hex[:10]
        try:
            from .screen_diff import remember_screenshot
            remember_screenshot(screenshot_id, png_bytes)
        except Exception:
            pass

        scope_msg = f" [{target_label}]" if target_label else ""
        return ToolResult(
            f"Desktop screenshot taken ({w}x{h}, {size_kb:.0f}KB){scope_msg}",
            elapsed=elapsed,
            metadata={
                "screenshot_b64": b64,
                "media_type": "image/png",
                "width": w,
                "height": h,
                "size_bytes": len(png_bytes),
                "target_label": target_label or "",
                "screenshot_id": screenshot_id,
            },
        )
    except ImportError as e:
        return ToolResult(str(e), is_error=True, elapsed=time.time() - start)
    except Exception as e:
        return ToolResult(f"Screenshot error: {e}", is_error=True, elapsed=time.time() - start)


def exec_computer_click(args: dict, start: float) -> ToolResult:
    """
    Click at desktop coordinates. Auto-captures a follow-up screenshot.

    Coordinate origin:
    - Default: absolute screen coords.
    - With `target_window`: (x, y) are window-relative — translated to screen coords.
    - With `monitor`:       (x, y) are monitor-relative — translated to screen coords.
    """
    try:
        import pyautogui
    except ImportError:
        return ToolResult("Error: pyautogui not installed. Run: pip install pyautogui",
                         is_error=True, elapsed=time.time() - start)

    x = args.get("x", 0)
    y = args.get("y", 0)
    button = args.get("button", "left")
    clicks = args.get("clicks", 1)
    screenshot = args.get("screenshot", True)

    origin, label = _resolve_window_or_monitor(args)
    abs_x, abs_y, was_scaled = _map_model_coords(x, y, origin)

    bounds_error = _validate_bounds(abs_x, abs_y)
    if bounds_error:
        return ToolResult(bounds_error, is_error=True, elapsed=time.time() - start)

    try:
        pyautogui.click(x=abs_x, y=abs_y, button=button, clicks=clicks)
        # After the click, not before: pyautogui has moved the real cursor by
        # now, so the ring pulses where the pointer actually landed rather than
        # where the agent aimed. A mis-click should be visible as one.
        try:
            from .screen_overlay import note_click

            for _ in range(max(1, int(clicks))):
                note_click()
        except Exception:
            pass
        elapsed = time.time() - start
        click_type = "Double-clicked" if clicks == 2 else "Clicked"
        scope_msg = f" relative to {label}" if origin is not None else ""
        if was_scaled:
            scope_msg += " (image coords scaled to screen)"
        result = ToolResult(
            f"{click_type} at ({x}, {y}){scope_msg} → screen ({abs_x}, {abs_y}) [{button}]",
            elapsed=elapsed,
            metadata={
                "action": "click",
                "x": x, "y": y,
                "abs_x": abs_x, "abs_y": abs_y,
                "button": button,
                "target_label": label,
                "coords_scaled": was_scaled,
            },
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
    direction = args.get("direction", "down")  # up, down, left, right
    amount = args.get("amount", 3)             # scroll clicks
    screenshot = args.get("screenshot", True)  # Auto-screenshot after

    try:
        abs_x = abs_y = None
        if x is not None and y is not None:
            abs_x, abs_y, _ = _map_model_coords(x, y, None)

        if direction in ("left", "right"):
            scroll_val = amount if direction == "right" else -amount
            if abs_x is not None:
                pyautogui.hscroll(scroll_val, x=abs_x, y=abs_y)
            else:
                pyautogui.hscroll(scroll_val)
        else:
            scroll_val = amount if direction == "up" else -amount
            if abs_x is not None:
                pyautogui.scroll(scroll_val, x=abs_x, y=abs_y)
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


def exec_computer_cursor_position(args: dict, start: float) -> ToolResult:
    """
    Report the current cursor position, in both real screen pixels and the
    coordinate space of the last screenshot (what the model should use).
    """
    try:
        import pyautogui
    except ImportError:
        return ToolResult("Error: pyautogui not installed. Run: pip install pyautogui",
                         is_error=True, elapsed=time.time() - start)

    try:
        pos = pyautogui.position()
        real_x, real_y = int(pos[0]), int(pos[1])
        cap = get_last_capture()
        if cap and cap["scaled_w"] > 0 and cap["scaled_h"] > 0:
            sx = cap["real_w"] / cap["scaled_w"]
            sy = cap["real_h"] / cap["scaled_h"]
            img_x = int(round((real_x - cap["offset_x"]) / sx))
            img_y = int(round((real_y - cap["offset_y"]) / sy))
            msg = (f"Cursor at image coords ({img_x}, {img_y}) "
                   f"[screen pixels: ({real_x}, {real_y})]")
            metadata = {"x": img_x, "y": img_y, "screen_x": real_x, "screen_y": real_y}
        else:
            msg = f"Cursor at screen ({real_x}, {real_y}) — no screenshot taken yet"
            metadata = {"x": real_x, "y": real_y, "screen_x": real_x, "screen_y": real_y}
        return ToolResult(msg, elapsed=time.time() - start, metadata=metadata)
    except Exception as e:
        return ToolResult(f"Cursor position error: {e}", is_error=True, elapsed=time.time() - start)
