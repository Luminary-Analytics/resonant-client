"""On-screen glow shown while the agent is driving the computer.

When Resonant moves the mouse and types, the user needs to know at a glance
that the input is not theirs — otherwise the machine simply appears possessed.
This draws a soft purple glow around the edges of the monitor being acted on,
pulsing gently, with a banner reading "Resonant is using the computer".

Implemented directly on Win32 through ctypes. The alternatives were a second
pywebview window (heavy, and transparency support on Windows is patchy) or
tkinter (explicitly excluded from the bundle). ctypes adds nothing to the
installer, and `computer_use.py` already drives Win32 the same way.

The glow needs a real alpha ramp, so the window is composited with
`UpdateLayeredWindow` and a premultiplied 32-bit bitmap. The simpler
`SetLayeredWindowAttributes` colour-key route cannot express partial
transparency — every pixel is either fully drawn or fully absent — which gives
a hard border, not a glow.

Three properties matter and each is deliberate:

- **Click-through.** `WS_EX_TRANSPARENT` means the overlay never intercepts a
  click. An indicator that blocked the very input it is announcing would be
  worse than no indicator.
- **Invisible to screen capture.** The agent screenshots the desktop to decide
  where to click. If it saw the glow it would be reading Resonant's own chrome
  as part of the application under test, and the banner sits exactly where a
  title bar usually is. `hidden_for_capture()` takes it down for the grab.
- **Non-fatal.** Every entry point swallows its own failures. A decorative
  border must never be the reason a computer-use run dies, and the module
  no-ops entirely off Windows.
"""

import ctypes
import logging
import math
import queue
import threading
import time
from contextlib import contextmanager
from ctypes import wintypes
from typing import Optional

logger = logging.getLogger(__name__)

IS_WINDOWS = hasattr(ctypes, "windll")

BANNER_TEXT = "Resonant is using the computer"

# How far the glow reaches inward before it fades to nothing. Generous enough
# to read as a glow rather than a thick border.
GLOW_PX = 90
# Alpha at the very edge of the screen, tapering to 0 at GLOW_PX inward.
_EDGE_ALPHA = 190
# Resonant purple, as (R, G, B).
_GLOW_RGB = (124, 92, 255)

_BANNER_HEIGHT = 36
_BANNER_PAD_X = 26
_BANNER_BG = (32, 26, 44)
_BANNER_FG = (238, 234, 255)

# The pulse. Slow and shallow: a fast or deep blink in peripheral vision is
# genuinely unpleasant to sit next to for a long run.
_PULSE_PERIOD_S = 2.6
_PULSE_MIN = 0.62      # fraction of full intensity at the trough
_PULSE_MAX = 1.0
_PULSE_FPS = 25

# Win32 constants
_WS_EX_LAYERED = 0x00080000
_WS_EX_TRANSPARENT = 0x00000020
_WS_EX_TOPMOST = 0x00000008
_WS_EX_TOOLWINDOW = 0x00000080  # keeps it out of the taskbar and Alt+Tab
_WS_EX_NOACTIVATE = 0x08000000
_WS_POPUP = 0x80000000
_SW_HIDE = 0
_SW_SHOWNOACTIVATE = 4
_HWND_TOPMOST = -1
_SWP_NOACTIVATE = 0x0010
_SWP_NOMOVE = 0x0002
_SWP_NOSIZE = 0x0001
_ULW_ALPHA = 0x00000002
_AC_SRC_OVER = 0x00
_AC_SRC_ALPHA = 0x01
_BI_RGB = 0
_DIB_RGB_COLORS = 0
_DT_CENTER = 0x00000001
_DT_VCENTER = 0x00000004
_DT_SINGLELINE = 0x00000020
_TRANSPARENT_BK = 1
_DEFAULT_GUI_FONT = 17


_signatures_declared = False


def _declare_signatures() -> None:
    """Give ctypes the real widths of every Win32 handle used here.

    Handles are pointer-sized. ctypes defaults undeclared parameters and return
    values to C int, so on 64-bit a handle whose value exceeds 2^31 raises
    "int too long to convert" — or worse, is silently truncated. Declaring the
    signatures once is the only reliable fix; doing it per-call is how the
    first version of this module ended up failing inside SelectObject after
    working in a simpler code path.
    """
    global _signatures_declared
    if _signatures_declared or not IS_WINDOWS:
        return
    user32 = ctypes.windll.user32
    gdi32 = ctypes.windll.gdi32

    LRESULT = ctypes.c_ssize_t
    user32.DefWindowProcW.argtypes = [
        wintypes.HWND, ctypes.c_uint, wintypes.WPARAM, wintypes.LPARAM,
    ]
    user32.DefWindowProcW.restype = LRESULT
    user32.CreateWindowExW.restype = wintypes.HWND
    user32.GetDC.argtypes = [wintypes.HWND]
    user32.GetDC.restype = wintypes.HDC
    user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
    user32.FillRect.argtypes = [wintypes.HDC, ctypes.POINTER(wintypes.RECT), wintypes.HBRUSH]
    user32.DrawTextW.argtypes = [
        wintypes.HDC, wintypes.LPCWSTR, ctypes.c_int,
        ctypes.POINTER(wintypes.RECT), ctypes.c_uint,
    ]
    user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.SetWindowPos.argtypes = [
        wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
        ctypes.c_int, ctypes.c_int, ctypes.c_uint,
    ]
    user32.UpdateLayeredWindow.argtypes = [
        wintypes.HWND, wintypes.HDC,
        ctypes.POINTER(wintypes.POINT), ctypes.POINTER(wintypes.SIZE),
        wintypes.HDC, ctypes.POINTER(wintypes.POINT),
        wintypes.COLORREF, ctypes.POINTER(_BLENDFUNCTION), wintypes.DWORD,
    ]
    user32.UpdateLayeredWindow.restype = wintypes.BOOL

    gdi32.CreateDIBSection.argtypes = [
        wintypes.HDC, ctypes.c_void_p, wintypes.UINT,
        ctypes.POINTER(ctypes.c_void_p), wintypes.HANDLE, wintypes.DWORD,
    ]
    gdi32.CreateDIBSection.restype = wintypes.HBITMAP
    gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
    gdi32.CreateCompatibleDC.restype = wintypes.HDC
    gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
    gdi32.SelectObject.restype = wintypes.HGDIOBJ
    gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
    gdi32.DeleteDC.argtypes = [wintypes.HDC]
    gdi32.CreateSolidBrush.argtypes = [wintypes.COLORREF]
    gdi32.CreateSolidBrush.restype = wintypes.HBRUSH
    gdi32.GetStockObject.argtypes = [ctypes.c_int]
    gdi32.GetStockObject.restype = wintypes.HGDIOBJ
    gdi32.SetBkMode.argtypes = [wintypes.HDC, ctypes.c_int]
    gdi32.SetTextColor.argtypes = [wintypes.HDC, wintypes.COLORREF]
    _signatures_declared = True


class _BLENDFUNCTION(ctypes.Structure):
    _fields_ = [
        ("BlendOp", ctypes.c_byte),
        ("BlendFlags", ctypes.c_byte),
        ("SourceConstantAlpha", ctypes.c_byte),
        ("AlphaFormat", ctypes.c_byte),
    ]


class _BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wintypes.DWORD),
        ("biWidth", ctypes.c_long),
        ("biHeight", ctypes.c_long),
        ("biPlanes", wintypes.WORD),
        ("biBitCount", wintypes.WORD),
        ("biCompression", wintypes.DWORD),
        ("biSizeImage", wintypes.DWORD),
        ("biXPelsPerMeter", ctypes.c_long),
        ("biYPelsPerMeter", ctypes.c_long),
        ("biClrUsed", wintypes.DWORD),
        ("biClrImportant", wintypes.DWORD),
    ]


def _edge_alpha(distance: int) -> int:
    """Alpha for a pixel `distance` px from the nearest screen edge.

    Squared falloff rather than linear — a linear ramp reads as a wide flat
    band with a visible cutoff, while the square concentrates the brightness
    at the edge and lets the tail vanish smoothly.
    """
    if distance >= GLOW_PX:
        return 0
    t = 1.0 - (distance / GLOW_PX)
    return int(_EDGE_ALPHA * t * t)


def _build_glow_rows(width: int, height: int) -> bytearray:
    """Premultiplied BGRA for the whole monitor, transparent except the glow.

    Only the alpha ramp varies, and it depends solely on the distance to the
    nearest edge — so rows repeat. Distinct row patterns are built once and
    reused, which keeps a 2560x1080 surface well under a frame's worth of work
    instead of touching 2.7 million pixels individually in Python.
    """
    red, green, blue = _GLOW_RGB
    stride = width * 4
    buffer = bytearray(stride * height)

    # A row is fully determined by its vertical distance to the nearest edge.
    # Anything at or beyond GLOW_PX is entirely transparent.
    cache: dict[int, bytes] = {}

    def row_for(vertical: int) -> bytes:
        cached = cache.get(vertical)
        if cached is not None:
            return cached
        row = bytearray(stride)
        for x in range(width):
            horizontal = x if x < width - 1 - x else width - 1 - x
            alpha = _edge_alpha(min(vertical, horizontal))
            if not alpha:
                continue
            offset = x * 4
            # Premultiplied: the compositor expects colour already scaled by
            # alpha, otherwise the glow washes out to white at the edges.
            row[offset] = blue * alpha // 255
            row[offset + 1] = green * alpha // 255
            row[offset + 2] = red * alpha // 255
            row[offset + 3] = alpha
        packed = bytes(row)
        cache[vertical] = packed
        return packed

    for y in range(height):
        vertical = y if y < height - 1 - y else height - 1 - y
        # Rows past the glow depth still carry the left and right ramps, and
        # they are all identical — clamping collapses them onto one cached row.
        row = row_for(min(vertical, GLOW_PX))
        buffer[y * stride:(y + 1) * stride] = row
    return buffer


class _Overlay:
    """A single click-through, per-pixel-alpha window covering one monitor."""

    def __init__(self) -> None:
        self._hwnd = None
        self._wndclass = None
        self._wndproc_ref = None  # must outlive the window
        self._bounds = (0, 0, 0, 0)
        self._hdc_mem = None
        self._hbitmap = None
        self._old_bitmap = None
        self._lock = threading.RLock()
        self._commands: "queue.Queue" = queue.Queue()
        self._thread: Optional[threading.Thread] = None
        self._ready = threading.Event()
        self._shutdown = threading.Event()
        self._shown = False

    # ── window plumbing ──────────────────────────────────────────────

    def _ensure_window(self) -> bool:
        if self._hwnd:
            return True
        _declare_signatures()
        user32 = ctypes.windll.user32

        LRESULT = ctypes.c_ssize_t
        WNDPROC = ctypes.WINFUNCTYPE(
            LRESULT, wintypes.HWND, ctypes.c_uint, wintypes.WPARAM, wintypes.LPARAM
        )
        self._wndproc_ref = WNDPROC(
            lambda hwnd, msg, wparam, lparam: user32.DefWindowProcW(hwnd, msg, wparam, lparam)
        )

        class WNDCLASS(ctypes.Structure):
            _fields_ = [
                ("style", ctypes.c_uint),
                ("lpfnWndProc", WNDPROC),
                ("cbClsExtra", ctypes.c_int),
                ("cbWndExtra", ctypes.c_int),
                ("hInstance", wintypes.HINSTANCE),
                ("hIcon", wintypes.HICON),
                ("hCursor", wintypes.HANDLE),
                ("hbrBackground", wintypes.HBRUSH),
                ("lpszMenuName", wintypes.LPCWSTR),
                ("lpszClassName", wintypes.LPCWSTR),
            ]

        wndclass = WNDCLASS()
        wndclass.lpfnWndProc = self._wndproc_ref
        wndclass.hInstance = ctypes.windll.kernel32.GetModuleHandleW(None)
        wndclass.lpszClassName = "ResonantComputerUseHalo"
        if not user32.RegisterClassW(ctypes.byref(wndclass)):
            # 1410 is ERROR_CLASS_ALREADY_EXISTS, which is fine on a re-show.
            if ctypes.get_last_error() not in (0, 1410):
                return False
        self._wndclass = wndclass

        self._hwnd = user32.CreateWindowExW(
            _WS_EX_LAYERED | _WS_EX_TRANSPARENT | _WS_EX_TOPMOST
            | _WS_EX_TOOLWINDOW | _WS_EX_NOACTIVATE,
            "ResonantComputerUseHalo", "Resonant",
            _WS_POPUP,
            0, 0, 10, 10,
            None, None, wndclass.hInstance, None,
        )
        return bool(self._hwnd)

    def _release_surface(self) -> None:
        gdi32 = ctypes.windll.gdi32
        if self._hdc_mem:
            if self._old_bitmap:
                gdi32.SelectObject(self._hdc_mem, self._old_bitmap)
            gdi32.DeleteDC(self._hdc_mem)
        if self._hbitmap:
            gdi32.DeleteObject(self._hbitmap)
        self._hdc_mem = self._hbitmap = self._old_bitmap = None

    def _build_surface(self, width: int, height: int) -> bool:
        """Render the glow and banner once into a reusable DIB."""
        gdi32 = ctypes.windll.gdi32
        user32 = ctypes.windll.user32

        self._release_surface()

        header = _BITMAPINFOHEADER()
        header.biSize = ctypes.sizeof(_BITMAPINFOHEADER)
        header.biWidth = width
        # Negative height makes it top-down, matching the row order below.
        header.biHeight = -height
        header.biPlanes = 1
        header.biBitCount = 32
        header.biCompression = _BI_RGB

        bits = ctypes.c_void_p()
        screen_dc = user32.GetDC(None)
        try:
            gdi32.CreateDIBSection.restype = wintypes.HBITMAP
            hbitmap = gdi32.CreateDIBSection(
                screen_dc, ctypes.byref(header), _DIB_RGB_COLORS,
                ctypes.byref(bits), None, 0,
            )
            if not hbitmap or not bits:
                return False
            hdc_mem = gdi32.CreateCompatibleDC(screen_dc)
        finally:
            user32.ReleaseDC(None, screen_dc)

        pixels = _build_glow_rows(width, height)
        ctypes.memmove(bits, bytes(pixels), len(pixels))

        old = gdi32.SelectObject(hdc_mem, hbitmap)

        # Banner, centred along the top edge. Drawn after the glow so it sits
        # on top of it.
        text_len = len(BANNER_TEXT)
        banner_width = min(width - 40, 22 * text_len + _BANNER_PAD_X * 2)
        banner_left = max(0, (width - banner_width) // 2)
        banner_top = max(0, GLOW_PX // 3)
        banner = wintypes.RECT(
            banner_left, banner_top,
            banner_left + banner_width, banner_top + _BANNER_HEIGHT,
        )
        bg = gdi32.CreateSolidBrush(
            _BANNER_BG[2] << 16 | _BANNER_BG[1] << 8 | _BANNER_BG[0]
        )
        user32.FillRect(hdc_mem, ctypes.byref(banner), bg)
        gdi32.DeleteObject(bg)

        gdi32.SetBkMode(hdc_mem, _TRANSPARENT_BK)
        gdi32.SetTextColor(
            hdc_mem, _BANNER_FG[2] << 16 | _BANNER_FG[1] << 8 | _BANNER_FG[0]
        )
        font = gdi32.GetStockObject(_DEFAULT_GUI_FONT)
        old_font = gdi32.SelectObject(hdc_mem, font)
        user32.DrawTextW(
            hdc_mem, BANNER_TEXT, -1, ctypes.byref(banner),
            _DT_CENTER | _DT_VCENTER | _DT_SINGLELINE,
        )
        gdi32.SelectObject(hdc_mem, old_font)

        # GDI text and FillRect write nothing to the alpha channel, so every
        # pixel they touched is left fully transparent and the banner would be
        # invisible. Force the banner rectangle opaque afterwards.
        stride = width * 4
        buffer = (ctypes.c_ubyte * (stride * height)).from_address(bits.value)
        for y in range(banner.top, min(banner.bottom, height)):
            base = y * stride
            for x in range(banner.left, min(banner.right, width)):
                buffer[base + x * 4 + 3] = 255

        self._hdc_mem = hdc_mem
        self._hbitmap = hbitmap
        self._old_bitmap = old
        return True

    def _composite(self, intensity: float) -> None:
        """Push the surface to the screen at the given pulse intensity."""
        if not (self._hwnd and self._hdc_mem):
            return
        user32 = ctypes.windll.user32
        x, y, width, height = self._bounds

        blend = _BLENDFUNCTION(
            _AC_SRC_OVER, 0,
            max(0, min(255, int(255 * intensity))),
            _AC_SRC_ALPHA,
        )
        size = wintypes.SIZE(width, height)
        source = wintypes.POINT(0, 0)
        dest = wintypes.POINT(x, y)
        user32.UpdateLayeredWindow(
            self._hwnd, None,
            ctypes.byref(dest), ctypes.byref(size),
            self._hdc_mem, ctypes.byref(source),
            0, ctypes.byref(blend), _ULW_ALPHA,
        )

    # ── owner thread ─────────────────────────────────────────────────
    #
    # Every Win32 call touching the window happens on the single thread that
    # created it. This is not tidiness — `ShowWindow` on a window owned by
    # another thread posts to the owner's message queue and BLOCKS until the
    # owner pumps it. Resonant's desktop tools run on worker threads and the
    # linger timer fires on yet another, so a cross-thread hide deadlocked
    # both: the caller waited on a thread that was itself asleep.
    #
    # The loop polls a command queue and repaints the pulse, so it needs no
    # message-based marshalling and the pulse tick comes free.

    def _worker(self) -> None:
        frame = 1.0 / _PULSE_FPS
        started = time.monotonic()
        try:
            if not self._ensure_window():
                self._ready.set()
                return
            self._ready.set()
            while not self._shutdown.is_set():
                self._drain_commands()
                self._pump_messages()
                if self._shown:
                    phase = (time.monotonic() - started) / _PULSE_PERIOD_S
                    # Sine eased into [_PULSE_MIN, _PULSE_MAX] — no hard edge.
                    wave = (math.sin(phase * 2 * math.pi) + 1.0) / 2.0
                    self._composite(_PULSE_MIN + (_PULSE_MAX - _PULSE_MIN) * wave)
                time.sleep(frame)
        except Exception:
            logger.debug("Halo overlay worker stopped", exc_info=True)
        finally:
            self._ready.set()

    def _pump_messages(self) -> None:
        user32 = ctypes.windll.user32
        msg = wintypes.MSG()
        while user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 1):  # PM_REMOVE
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))

    def _drain_commands(self) -> None:
        while True:
            try:
                action, payload, done, result = self._commands.get_nowait()
            except queue.Empty:
                return
            try:
                if action == "show":
                    self._apply_show(*payload)
                elif action == "hide":
                    self._apply_hide()
                elif action == "suppress":
                    # Report what the overlay was actually doing at the moment
                    # the hide took effect, not what a caller observed earlier.
                    result["was_shown"] = self._shown
                    result["bounds"] = self._bounds
                    self._apply_hide()
            except Exception:
                logger.debug("Halo overlay command failed", exc_info=True)
            finally:
                if done is not None:
                    done.set()

    def _apply_show(self, x: int, y: int, width: int, height: int) -> None:
        if self._bounds != (x, y, width, height) or not self._hbitmap:
            self._bounds = (x, y, width, height)
            if not self._build_surface(width, height):
                return
        user32 = ctypes.windll.user32
        self._composite(_PULSE_MAX)
        user32.ShowWindow(self._hwnd, _SW_SHOWNOACTIVATE)
        # Re-assert topmost: another window going full-screen can push it
        # down, and a glow behind the app it describes is useless.
        user32.SetWindowPos(
            self._hwnd, _HWND_TOPMOST, 0, 0, 0, 0,
            _SWP_NOMOVE | _SWP_NOSIZE | _SWP_NOACTIVATE,
        )
        self._shown = True

    def _apply_hide(self) -> None:
        if self._hwnd:
            ctypes.windll.user32.ShowWindow(self._hwnd, _SW_HIDE)
        self._shown = False

    def _ensure_worker(self) -> bool:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return True
            self._shutdown.clear()
            self._ready.clear()
            # Daemon: the indicator must never hold the process open at exit.
            self._thread = threading.Thread(
                target=self._worker, name="resonant-halo", daemon=True
            )
            self._thread.start()
        self._ready.wait(timeout=5)
        return bool(self._hwnd)

    def _submit(self, action: str, payload=(), wait: bool = False) -> dict:
        if not self._ensure_worker():
            return {}
        done = threading.Event() if wait else None
        result: dict = {}
        self._commands.put((action, payload, done, result))
        if done is not None:
            # A capture must not start until the overlay is actually gone, so
            # that path waits; everything else is fire-and-forget.
            done.wait(timeout=2)
        return result

    # ── public surface ───────────────────────────────────────────────

    def show(self, x: int, y: int, width: int, height: int) -> bool:
        return self._submit("show", (x, y, width, height)) is not None

    def hide(self, wait: bool = False) -> None:
        self._submit("hide", (), wait=wait)

    def suppress(self) -> tuple[bool, tuple]:
        """Hide synchronously and report whether it had been showing.

        Ordering matters more than the current value of `visible` here: a show
        queued microseconds earlier has not been applied yet, so a caller that
        checked `visible` would see False, skip the hide, and then have the
        glow appear in the middle of the very screenshot it was avoiding. This
        goes through the same queue, so it always observes the true state.
        """
        result = self._submit("suppress", (), wait=True)
        return bool(result.get("was_shown")), result.get("bounds") or (0, 0, 0, 0)

    @property
    def visible(self) -> bool:
        return bool(self._shown)


_overlay: Optional[_Overlay] = None
_overlay_lock = threading.Lock()
_suppressed = 0  # >0 while a screen capture is in flight


def _instance() -> Optional[_Overlay]:
    global _overlay
    if not IS_WINDOWS:
        return None
    with _overlay_lock:
        if _overlay is None:
            _overlay = _Overlay()
        return _overlay


def show_for_region(x: int, y: int, width: int, height: int) -> bool:
    """Draw the glow around the given screen rectangle."""
    if _suppressed:
        return False
    overlay = _instance()
    if overlay is None:
        return False
    try:
        return overlay.show(int(x), int(y), int(width), int(height))
    except Exception:
        logger.debug("Halo overlay failed to show", exc_info=True)
        return False


def show_for_monitor(index: Optional[int] = None) -> bool:
    """Draw the glow around a monitor, defaulting to the primary one."""
    try:
        from .computer_use import list_monitors

        monitors = list_monitors()
    except Exception:
        monitors = []
    if not monitors:
        return False

    chosen = None
    if index is not None and 0 <= int(index) < len(monitors):
        chosen = monitors[int(index)]
    if chosen is None:
        chosen = next((m for m in monitors if m.get("primary")), monitors[0])
    return show_for_region(chosen["x"], chosen["y"], chosen["width"], chosen["height"])


def monitor_index_for_point(x: int, y: int) -> Optional[int]:
    """Which monitor contains this virtual-desktop point, if any.

    Lets a click at bare coordinates light up the screen it lands on rather
    than always the primary.
    """
    try:
        from .computer_use import list_monitors

        for monitor in list_monitors():
            if (monitor["x"] <= x < monitor["x"] + monitor["width"]
                    and monitor["y"] <= y < monitor["y"] + monitor["height"]):
                return monitor["index"]
    except Exception:
        logger.debug("monitor lookup failed", exc_info=True)
    return None


def hide() -> None:
    overlay = _instance()
    if overlay is None:
        return
    try:
        overlay.hide()
    except Exception:
        logger.debug("Halo overlay failed to hide", exc_info=True)


_activity_timer: Optional[threading.Timer] = None
_activity_lock = threading.Lock()

# How long the glow stays up after the last desktop action. A run is a burst of
# clicks and screenshots a few hundred milliseconds apart; showing and hiding
# around each one would strobe. This keeps it lit across the burst and takes it
# down shortly after the agent stops touching the machine.
LINGER_SECONDS = 3.0


def _rearm_linger(linger: float = LINGER_SECONDS) -> None:
    """(Re)start the countdown that takes the glow down."""
    global _activity_timer
    with _activity_lock:
        if _activity_timer is not None:
            _activity_timer.cancel()
        _activity_timer = threading.Timer(max(0.5, float(linger)), hide)
        # Daemon: a pending hide must never hold the process open at exit.
        _activity_timer.daemon = True
        _activity_timer.start()


def note_activity(monitor_index: Optional[int] = None, linger: float = LINGER_SECONDS) -> None:
    """Signal that the agent is driving the computer right now.

    Safe to call on every desktop tool invocation — repeated calls just push
    the hide deadline back rather than restarting the window.
    """
    if not IS_WINDOWS:
        return
    try:
        show_for_monitor(monitor_index)
        _rearm_linger(linger)
    except Exception:
        logger.debug("Halo activity signal failed", exc_info=True)


def stop_activity() -> None:
    """Take the glow down immediately, cancelling any pending linger."""
    global _activity_timer
    with _activity_lock:
        if _activity_timer is not None:
            _activity_timer.cancel()
            _activity_timer = None
    hide()


def monitor_index_for_args(args: dict) -> Optional[int]:
    """Best guess at which screen a desktop tool is about to touch.

    Explicit `monitor` wins. Otherwise a click's coordinates place it, which
    matters on a multi-monitor desk where the primary is often not the one
    being driven. Everything else falls through to the primary.
    """
    if not isinstance(args, dict):
        return None
    monitor = args.get("monitor")
    if isinstance(monitor, int):
        return monitor
    x, y = args.get("x"), args.get("y")
    if isinstance(x, (int, float)) and isinstance(y, (int, float)):
        return monitor_index_for_point(int(x), int(y))
    region = args.get("region")
    if isinstance(region, dict):
        rx, ry = region.get("x"), region.get("y")
        if isinstance(rx, (int, float)) and isinstance(ry, (int, float)):
            return monitor_index_for_point(int(rx), int(ry))
    return None


@contextmanager
def hidden_for_capture():
    """Take the glow down for the duration of a screen grab.

    The agent decides where to click from these screenshots. Leaving the glow
    in them would feed Resonant's own edge lighting and banner back to the
    model as if it were part of the application on screen — and the banner
    sits exactly where a window's title bar or toolbar usually is.

    Restores only if it was visible to begin with, so a capture never turns
    the glow on.
    """
    global _suppressed
    overlay = _instance()
    was_visible = False
    bounds = (0, 0, 0, 0)
    if overlay is not None:
        try:
            # Blocking and queue-ordered: the grab must not start until the
            # glow is actually off screen, or it lands in the very image the
            # agent reads.
            was_visible, bounds = overlay.suppress()
        except Exception:
            logger.debug("Halo overlay suppression failed", exc_info=True)
    _suppressed += 1
    try:
        yield
    finally:
        _suppressed -= 1
        if overlay is not None and was_visible:
            try:
                overlay.show(*bounds)
                # Re-arm the linger. The pending timer can fire during the
                # capture — while the glow is already hidden, so its hide is a
                # no-op — and the restore would then bring the glow back with
                # nothing left to take it down again, stranding it on screen
                # for the rest of the session.
                _rearm_linger()
            except Exception:
                logger.debug("Halo overlay failed to restore", exc_info=True)
