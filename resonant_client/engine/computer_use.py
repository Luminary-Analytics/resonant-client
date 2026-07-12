"""
Full Computer Use orchestration for the Resonant Engine.

Implements the autonomous screenshot → reason → act loop that enables
the agent to control the desktop and browser visually, like a human would.

The ComputerUseLoop works alongside the existing Session agentic loop:
- Session handles the LLM conversation and tool dispatch
- ComputerUseLoop provides enhanced computer primitives and coordination
- Screenshots from tool results are automatically fed back to the model as images

Architecture:
    User: "Open Chrome and search for Python docs"
    → Session: sends to LLM with computer_* tools available
    → LLM: calls computer_screenshot to see the screen
    → Session: executes screenshot, returns image to LLM
    → LLM: sees desktop, calls computer_click(x=..., y=...) on Chrome icon
    → Session: executes click, auto-takes follow-up screenshot
    → LLM: sees Chrome open, calls computer_type(text="python docs")
    → ... continues until task is complete

Key features:
- Coordinate scaling: maps model coordinates (scaled image) ↔ real screen pixels
- Auto-screenshot after actions: click/type/scroll automatically capture a follow-up
- Safety zones: configurable screen regions where clicks are blocked
- Action cooldown: prevents runaway click loops
- Window management: find, focus, resize, list windows
- OCR hints: optional text extraction from screenshots to help non-vision models
"""

import base64
import io
import logging
import time
from dataclasses import dataclass
from typing import Optional

from resonant_client.processes import background_process_kwargs

from .tools import ToolResult

logger = logging.getLogger(__name__)

# ── Constants ────────────────────────────────────────────────────────

# Anthropic's recommended max for computer use screenshots
MAX_SCREENSHOT_EDGE = 1568

# Minimum delay between actions (seconds) to prevent runaway loops
ACTION_COOLDOWN = 0.3

# Max consecutive actions without user input before safety stop
MAX_AUTONOMOUS_ACTIONS = 50


# ── Coordinate Scaler ────────────────────────────────────────────────

@dataclass
class ScreenScale:
    """
    Tracks the scaling between screenshot coordinates and real screen pixels.

    When we resize a 2560x1440 screenshot to 1568x882 for the model,
    the model will return coordinates in the 1568x882 space. We need
    to map those back to real pixel coordinates for pyautogui.
    """
    real_width: int = 1920
    real_height: int = 1080
    scaled_width: int = 1568
    scaled_height: int = 882
    offset_x: int = 0  # For multi-monitor or region captures
    offset_y: int = 0

    @property
    def scale_x(self) -> float:
        return self.real_width / self.scaled_width if self.scaled_width else 1.0

    @property
    def scale_y(self) -> float:
        return self.real_height / self.scaled_height if self.scaled_height else 1.0

    def to_real(self, x: int, y: int) -> tuple[int, int]:
        """Convert model/scaled coordinates to real screen coordinates."""
        real_x = int(x * self.scale_x) + self.offset_x
        real_y = int(y * self.scale_y) + self.offset_y
        return (real_x, real_y)

    def to_scaled(self, x: int, y: int) -> tuple[int, int]:
        """Convert real screen coordinates to model/scaled coordinates."""
        sx = int((x - self.offset_x) / self.scale_x)
        sy = int((y - self.offset_y) / self.scale_y)
        return (sx, sy)


# ── Safety Zone ──────────────────────────────────────────────────────

@dataclass
class SafetyZone:
    """A screen region where automated actions are blocked."""
    x: int
    y: int
    width: int
    height: int
    label: str = "protected"

    def contains(self, px: int, py: int) -> bool:
        return (self.x <= px <= self.x + self.width and
                self.y <= py <= self.y + self.height)


# ── Enhanced Screenshot ──────────────────────────────────────────────

def take_screenshot_scaled(
    region: Optional[dict] = None,
    max_edge: int = MAX_SCREENSHOT_EDGE,
) -> tuple[bytes, ScreenScale]:
    """
    Take a screenshot and return (png_bytes, scale_info).

    The scale_info lets callers map coordinates between the
    scaled image (sent to model) and real screen pixels.
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
                offset_x = region.get("x", 0)
                offset_y = region.get("y", 0)
            else:
                monitor = sct.monitors[1]  # Primary monitor
                offset_x = monitor["left"]
                offset_y = monitor["top"]

            sct_img = sct.grab(monitor)
            img = Image.frombytes("RGB", sct_img.size, sct_img.bgra, "raw", "BGRX")

            real_w, real_h = img.size

            # Scale down if needed
            if max(real_w, real_h) > max_edge:
                ratio = max_edge / max(real_w, real_h)
                new_w, new_h = int(real_w * ratio), int(real_h * ratio)
                img = img.resize((new_w, new_h), Image.LANCZOS)
            else:
                new_w, new_h = real_w, real_h

            buf = io.BytesIO()
            img.save(buf, format="PNG", optimize=True)

            scale = ScreenScale(
                real_width=real_w,
                real_height=real_h,
                scaled_width=new_w,
                scaled_height=new_h,
                offset_x=offset_x,
                offset_y=offset_y,
            )

            return buf.getvalue(), scale

    except ImportError:
        raise ImportError("Screenshot requires 'mss' and 'Pillow'. Run: pip install mss Pillow")


# ── Window Management ────────────────────────────────────────────────

def list_windows() -> list[dict]:
    """
    List visible windows on the desktop.

    Returns list of {title, pid, x, y, width, height} dicts.
    Cross-platform: Windows (win32gui), macOS (AppKit), Linux (wmctrl).
    """
    import sys

    if sys.platform == "win32":
        return _list_windows_win32()
    elif sys.platform == "darwin":
        return _list_windows_macos()
    else:
        return _list_windows_linux()


def _list_windows_win32() -> list[dict]:
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        windows = []

        def callback(hwnd, _):
            if user32.IsWindowVisible(hwnd):
                length = user32.GetWindowTextLengthW(hwnd)
                if length > 0:
                    buf = ctypes.create_unicode_buffer(length + 1)
                    user32.GetWindowTextW(hwnd, buf, length + 1)
                    title = buf.value

                    rect = wintypes.RECT()
                    user32.GetWindowRect(hwnd, ctypes.byref(rect))

                    # Get PID
                    pid = wintypes.DWORD()
                    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))

                    windows.append({
                        "title": title,
                        "pid": pid.value,
                        "hwnd": hwnd,
                        "x": rect.left,
                        "y": rect.top,
                        "width": rect.right - rect.left,
                        "height": rect.bottom - rect.top,
                    })
            return True

        WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
        user32.EnumWindows(WNDENUMPROC(callback), 0)
        return windows
    except Exception as e:
        logger.warning(f"Failed to list windows: {e}")
        return []


def _list_windows_macos() -> list[dict]:
    try:
        import subprocess
        result = subprocess.run(
            ["osascript", "-e", '''
                tell application "System Events"
                    set windowList to {}
                    repeat with p in (every process whose visible is true)
                        try
                            repeat with w in (every window of p)
                                set end of windowList to (name of p & "|" & name of w & "|" & position of w & "|" & size of w)
                            end repeat
                        end try
                    end repeat
                    return windowList
                end tell
            '''],
            capture_output=True, text=True, timeout=10,
        )
        windows = []
        for line in result.stdout.strip().split(", "):
            parts = line.split("|")
            if len(parts) >= 4:
                windows.append({
                    "title": f"{parts[0]} - {parts[1]}",
                    "x": int(parts[2].split(",")[0]) if "," in parts[2] else 0,
                    "y": int(parts[2].split(",")[1]) if "," in parts[2] else 0,
                    "width": int(parts[3].split(",")[0]) if "," in parts[3] else 0,
                    "height": int(parts[3].split(",")[1]) if "," in parts[3] else 0,
                })
        return windows
    except Exception as e:
        logger.warning(f"Failed to list windows (macOS): {e}")
        return []


def _list_windows_linux() -> list[dict]:
    try:
        import subprocess
        result = subprocess.run(
            ["wmctrl", "-lG"],
            capture_output=True, text=True, timeout=5,
        )
        windows = []
        for line in result.stdout.strip().split("\n"):
            parts = line.split(None, 8)
            if len(parts) >= 8:
                windows.append({
                    "title": parts[7] if len(parts) > 7 else "",
                    "x": int(parts[2]),
                    "y": int(parts[3]),
                    "width": int(parts[4]),
                    "height": int(parts[5]),
                })
        return windows
    except Exception as e:
        logger.warning(f"Failed to list windows (Linux): {e}")
        return []


def list_monitors() -> list[dict]:
    """
    Enumerate physical monitors. Returns list of:
      [{"index": int, "x": int, "y": int, "width": int, "height": int, "primary": bool}, ...]

    The mss "virtual desktop" union (monitors[0]) is excluded — only physical
    monitors at indices 0..N-1 in the returned list.
    """
    try:
        import mss
        with mss.mss() as sct:
            physical = sct.monitors[1:]  # skip virtual desktop
            # Heuristic for primary: the one whose origin is (0, 0) (Windows + macOS).
            # If none match, mark the first as primary.
            primary_idx = next(
                (i for i, m in enumerate(physical) if m.get("left", 0) == 0 and m.get("top", 0) == 0),
                0 if physical else -1,
            )
            return [
                {
                    "index": i,
                    "x": m["left"],
                    "y": m["top"],
                    "width": m["width"],
                    "height": m["height"],
                    "primary": i == primary_idx,
                }
                for i, m in enumerate(physical)
            ]
    except ImportError:
        return []
    except Exception as exc:
        logger.warning(f"list_monitors failed: {exc}")
        return []


def exec_monitors_list(args: dict, start: float) -> ToolResult:
    """Tool wrapper for list_monitors."""
    monitors = list_monitors()
    if not monitors:
        return ToolResult(
            "No monitors detected (mss may not be installed).",
            elapsed=time.time() - start,
            metadata={"monitors": []},
        )
    lines = [f"Found {len(monitors)} monitor(s):"]
    for m in monitors:
        flag = "  [primary]" if m["primary"] else ""
        lines.append(f"  [{m['index']}] {m['width']}x{m['height']} @ ({m['x']},{m['y']}){flag}")
    return ToolResult(
        "\n".join(lines),
        elapsed=time.time() - start,
        metadata={"monitors": monitors},
    )


def get_window_rect(title_substring: str) -> Optional[dict]:
    """
    Find a visible window whose title contains `title_substring` (case-insensitive)
    and return its bbox.

    Returns {x, y, width, height, title} or None if no match.

    NOTE: returns the full window rect (including title bar / chrome). This is
    "good enough" for screenshot framing; the agent can adjust click offsets.
    """
    if not title_substring:
        return None
    needle = title_substring.lower()
    try:
        for w in list_windows():
            title = (w.get("title") or "").lower()
            if needle in title:
                if w.get("width", 0) <= 0 or w.get("height", 0) <= 0:
                    continue
                return {
                    "x": w["x"],
                    "y": w["y"],
                    "width": w["width"],
                    "height": w["height"],
                    "title": w.get("title", ""),
                }
    except Exception as exc:
        logger.warning(f"get_window_rect failed: {exc}")
    return None


def focus_window(title_substring: str) -> str:
    """Focus a window by partial title match."""
    import sys

    if sys.platform == "win32":
        return _focus_window_win32(title_substring)
    elif sys.platform == "darwin":
        return _focus_window_macos(title_substring)
    else:
        return _focus_window_linux(title_substring)


def _focus_window_win32(title: str) -> str:
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        target_hwnd = None

        def callback(hwnd, _):
            nonlocal target_hwnd
            if user32.IsWindowVisible(hwnd):
                length = user32.GetWindowTextLengthW(hwnd)
                if length > 0:
                    buf = ctypes.create_unicode_buffer(length + 1)
                    user32.GetWindowTextW(hwnd, buf, length + 1)
                    if title.lower() in buf.value.lower():
                        target_hwnd = hwnd
                        return False  # Stop enumeration
            return True

        WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
        user32.EnumWindows(WNDENUMPROC(callback), 0)

        if target_hwnd:
            user32.SetForegroundWindow(target_hwnd)
            return f"Focused window: {title}"
        return f"Window not found: {title}"
    except Exception as e:
        return f"Error focusing window: {e}"


def _focus_window_macos(title: str) -> str:
    try:
        import subprocess
        subprocess.run(
            ["osascript", "-e", f'''
                tell application "System Events"
                    set frontmost of first process whose name contains "{title}" to true
                end tell
            '''],
            capture_output=True, timeout=5,
        )
        return f"Focused window: {title}"
    except Exception as e:
        return f"Error focusing window: {e}"


def _focus_window_linux(title: str) -> str:
    try:
        import subprocess
        result = subprocess.run(
            ["wmctrl", "-a", title],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            return f"Focused window: {title}"
        return f"Window not found: {title}"
    except Exception as e:
        return f"Error focusing window: {e}"


# ── Enhanced Desktop Actions ─────────────────────────────────────────

def exec_computer_drag(args: dict, start: float) -> ToolResult:
    """Drag from one point to another on screen."""
    try:
        import pyautogui
    except ImportError:
        return ToolResult("Error: pyautogui not installed", is_error=True, elapsed=time.time() - start)

    x1 = args.get("start_x", args.get("x1", 0))
    y1 = args.get("start_y", args.get("y1", 0))
    x2 = args.get("end_x", args.get("x2", 0))
    y2 = args.get("end_y", args.get("y2", 0))
    button = args.get("button", "left")
    duration = args.get("duration", 0.5)

    try:
        pyautogui.moveTo(x1, y1)
        pyautogui.drag(x2 - x1, y2 - y1, duration=duration, button=button)
        elapsed = time.time() - start
        return ToolResult(
            f"Dragged from ({x1},{y1}) to ({x2},{y2}) [{button}]",
            elapsed=elapsed,
            metadata={"action": "drag", "start": (x1, y1), "end": (x2, y2)},
        )
    except Exception as e:
        return ToolResult(f"Drag error: {e}", is_error=True, elapsed=time.time() - start)


def exec_computer_hover(args: dict, start: float) -> ToolResult:
    """Move mouse to a position without clicking."""
    try:
        import pyautogui
    except ImportError:
        return ToolResult("Error: pyautogui not installed", is_error=True, elapsed=time.time() - start)

    x = args.get("x", 0)
    y = args.get("y", 0)
    duration = args.get("duration", 0.3)

    try:
        pyautogui.moveTo(x, y, duration=duration)
        elapsed = time.time() - start
        return ToolResult(
            f"Moved mouse to ({x}, {y})",
            elapsed=elapsed,
            metadata={"action": "hover", "x": x, "y": y},
        )
    except Exception as e:
        return ToolResult(f"Hover error: {e}", is_error=True, elapsed=time.time() - start)


def exec_window_list(args: dict, start: float) -> ToolResult:
    """List all visible windows on the desktop."""
    try:
        windows = list_windows()
        if not windows:
            return ToolResult("No visible windows found (or window listing unavailable on this platform).",
                            elapsed=time.time() - start)

        lines = []
        for w in windows[:30]:  # Cap at 30
            title = w.get("title", "Untitled")[:60]
            pos = f"({w.get('x', '?')},{w.get('y', '?')})"
            size = f"{w.get('width', '?')}x{w.get('height', '?')}"
            lines.append(f"  {title}  {pos}  {size}")

        elapsed = time.time() - start
        return ToolResult(
            f"Visible windows ({len(windows)}):\n" + "\n".join(lines),
            elapsed=elapsed,
            metadata={"count": len(windows), "windows": windows[:30]},
        )
    except Exception as e:
        return ToolResult(f"Window list error: {e}", is_error=True, elapsed=time.time() - start)


def exec_window_focus(args: dict, start: float) -> ToolResult:
    """Focus a window by title substring match."""
    title = args.get("title", "")
    if not title:
        return ToolResult("Error: 'title' is required", is_error=True, elapsed=time.time() - start)

    try:
        result = focus_window(title)
        elapsed = time.time() - start
        return ToolResult(result, elapsed=elapsed, metadata={"action": "focus", "title": title})
    except Exception as e:
        return ToolResult(f"Focus error: {e}", is_error=True, elapsed=time.time() - start)


def exec_computer_wait(args: dict, start: float) -> ToolResult:
    """
    Wait for the screen (or a region) to change, or wait a fixed duration.

    Modes:
    - duration: Wait N seconds (capped at 30).
    - change:   Poll screenshots until pixels change. Set `region` to watch
                only a bbox (much cheaper + much less false-positive than
                comparing the whole screen).

    Args:
        mode:    "duration" | "change"
        seconds: For mode=duration. Default 1.0.
        timeout: For mode=change. Default 10.0.
        region:  For mode=change. {x, y, width, height}. Optional.
    """
    mode = args.get("mode", "duration")
    seconds = args.get("seconds", 1.0)
    timeout = args.get("timeout", 10.0)
    region = args.get("region")  # Optional {x, y, width, height}

    try:
        if mode == "duration":
            time.sleep(min(seconds, 30))  # Cap at 30 seconds
            elapsed = time.time() - start
            return ToolResult(f"Waited {seconds:.1f}s", elapsed=elapsed)

        elif mode == "change":
            import hashlib
            initial_hash = None
            scope_label = "screen"
            if region:
                scope_label = (
                    f"region ({region.get('x',0)},{region.get('y',0)},"
                    f"{region.get('width','?')}x{region.get('height','?')})"
                )

            try:
                png_bytes, _ = take_screenshot_scaled(region=region)
                initial_hash = hashlib.md5(png_bytes).hexdigest()
            except Exception:
                time.sleep(1)
                elapsed = time.time() - start
                return ToolResult("Waited 1s (screenshot comparison unavailable)", elapsed=elapsed)

            wait_start = time.time()
            while time.time() - wait_start < timeout:
                time.sleep(0.5)
                try:
                    png_bytes, _ = take_screenshot_scaled(region=region)
                    current_hash = hashlib.md5(png_bytes).hexdigest()
                    if current_hash != initial_hash:
                        elapsed = time.time() - start
                        return ToolResult(
                            f"{scope_label.capitalize()} changed after {time.time() - wait_start:.1f}s",
                            elapsed=elapsed,
                        )
                except Exception:
                    continue

            elapsed = time.time() - start
            return ToolResult(
                f"No change detected in {scope_label} within {timeout:.0f}s",
                elapsed=elapsed,
            )

        else:
            return ToolResult(f"Unknown wait mode: {mode}", is_error=True, elapsed=time.time() - start)

    except Exception as e:
        return ToolResult(f"Wait error: {e}", is_error=True, elapsed=time.time() - start)


def exec_screen_ocr(args: dict, start: float) -> ToolResult:
    """
    Extract text from the screen or a region using OCR.

    Requires: pip install pytesseract (and Tesseract installed on system)
    Falls back to a simpler method if unavailable.
    """
    region = args.get("region")  # Optional {x, y, width, height}

    try:
        from PIL import Image
        import mss

        with mss.mss() as sct:
            if region:
                monitor = {
                    "left": region.get("x", 0),
                    "top": region.get("y", 0),
                    "width": region.get("width", 800),
                    "height": region.get("height", 600),
                }
            else:
                monitor = sct.monitors[1]

            sct_img = sct.grab(monitor)
            img = Image.frombytes("RGB", sct_img.size, sct_img.bgra, "raw", "BGRX")

        # Try Tesseract OCR
        try:
            import pytesseract
            text = pytesseract.image_to_string(img)
            elapsed = time.time() - start
            return ToolResult(
                f"OCR text ({len(text)} chars):\n{text[:5000]}",
                elapsed=elapsed,
                metadata={"method": "tesseract", "chars": len(text)},
            )
        except ImportError:
            pass

        # Fallback: try Windows OCR via PowerShell
        import sys
        if sys.platform == "win32":
            try:
                import subprocess
                import tempfile
                import os

                tmp = os.path.join(tempfile.gettempdir(), "resonant_ocr.png")
                img.save(tmp, format="PNG")

                ps_script = f"""
Add-Type -AssemblyName System.Runtime.WindowsRuntime
$null = [Windows.Media.Ocr.OcrEngine, Windows.Foundation, ContentType = WindowsRuntime]
$engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromUserProfileLanguages()
$file = [Windows.Storage.StorageFile]::GetFileFromPathAsync('{tmp}').GetAwaiter().GetResult()
$stream = $file.OpenAsync([Windows.Storage.FileAccessMode]::Read).GetAwaiter().GetResult()
$decoder = [Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream).GetAwaiter().GetResult()
$bitmap = $decoder.GetSoftwareBitmapAsync().GetAwaiter().GetResult()
$result = $engine.RecognizeAsync($bitmap).GetAwaiter().GetResult()
Write-Output $result.Text
"""
                result = subprocess.run(
                    ["powershell", "-NoProfile", "-Command", ps_script],
                    capture_output=True, text=True, timeout=15,
                    **background_process_kwargs(),
                )
                os.unlink(tmp)
                text = result.stdout.strip()
                if text:
                    elapsed = time.time() - start
                    return ToolResult(
                        f"OCR text ({len(text)} chars):\n{text[:5000]}",
                        elapsed=elapsed,
                        metadata={"method": "windows_ocr", "chars": len(text)},
                    )
            except Exception:
                pass

        elapsed = time.time() - start
        return ToolResult(
            "OCR unavailable. Install pytesseract: pip install pytesseract\n"
            "And install Tesseract: https://github.com/tesseract-ocr/tesseract",
            is_error=True,
            elapsed=elapsed,
        )

    except ImportError as e:
        return ToolResult(f"OCR requires mss and Pillow: {e}", is_error=True, elapsed=time.time() - start)
    except Exception as e:
        return ToolResult(f"OCR error: {e}", is_error=True, elapsed=time.time() - start)


def exec_open_application(args: dict, start: float) -> ToolResult:
    """Open an application by name."""
    import subprocess
    import sys

    app_name = args.get("name", "")
    if not app_name:
        return ToolResult("Error: 'name' is required", is_error=True, elapsed=time.time() - start)

    try:
        if sys.platform == "win32":
            # Try Start-Process first, then os.startfile
            try:
                subprocess.Popen(
                    ["cmd", "/c", "start", "", app_name],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    **background_process_kwargs(),
                )
            except Exception:
                import os
                os.startfile(app_name)
        elif sys.platform == "darwin":
            subprocess.Popen(
                ["open", "-a", app_name],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        else:
            subprocess.Popen(
                [app_name],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )

        time.sleep(1)  # Brief pause for app to start
        elapsed = time.time() - start
        return ToolResult(
            f"Opened application: {app_name}",
            elapsed=elapsed,
            metadata={"action": "open_app", "name": app_name},
        )
    except Exception as e:
        return ToolResult(f"Error opening {app_name}: {e}", is_error=True, elapsed=time.time() - start)


# ── Auto-screenshot wrapper ──────────────────────────────────────────

def with_auto_screenshot(
    action_func,
    args: dict,
    start: float,
    take_screenshot: bool = True,
) -> ToolResult:
    """
    Execute an action and optionally take a follow-up screenshot.

    This is the key pattern for computer use: every action returns
    a screenshot so the model can see the result and plan next steps.
    """
    result = action_func(args, start)

    if take_screenshot and not result.is_error:
        try:
            time.sleep(ACTION_COOLDOWN)  # Brief pause for UI to update
            png_bytes, scale = take_screenshot_scaled()
            b64 = base64.b64encode(png_bytes).decode("utf-8")

            # Append screenshot metadata to the result
            result.metadata["screenshot_b64"] = b64
            result.metadata["media_type"] = "image/png"
            result.metadata["screen_width"] = scale.real_width
            result.metadata["screen_height"] = scale.real_height
            result.metadata["scaled_width"] = scale.scaled_width
            result.metadata["scaled_height"] = scale.scaled_height
            result.output += f"\n[Screenshot taken: {scale.scaled_width}x{scale.scaled_height}]"
        except Exception as e:
            logger.debug(f"Auto-screenshot failed (non-fatal): {e}")

    return result
