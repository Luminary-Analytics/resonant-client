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
# The ring has to keep up with a moving pointer, so the worker ticks at this
# rate and the glow is recomposited on a subset of those ticks.
_RING_FPS = 60

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
_CURSOR_SHOWING = 0x00000001


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
    # argtypes as well as restype. Declaring only the return value is what
    # broke the overlay in v0.12.4: GetModuleHandleW started returning a true
    # 64-bit handle, and CreateWindowExW — whose parameters still defaulted to
    # C int — then rejected it with "argument 11: int too long to convert".
    # The crash was fixed and the feature stopped working, silently, because
    # every failure here is swallowed by design.
    user32.CreateWindowExW.argtypes = [
        wintypes.DWORD,      # dwExStyle
        wintypes.LPCWSTR,    # lpClassName
        wintypes.LPCWSTR,    # lpWindowName
        wintypes.DWORD,      # dwStyle
        ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,  # x, y, w, h
        wintypes.HWND,       # hWndParent
        wintypes.HMENU,      # hMenu
        wintypes.HINSTANCE,  # hInstance
        wintypes.LPVOID,     # lpParam
    ]
    user32.CreateWindowExW.restype = wintypes.HWND
    user32.RegisterClassW.argtypes = [ctypes.c_void_p]
    user32.MoveWindow.argtypes = [
        wintypes.HWND, ctypes.c_int, ctypes.c_int,
        ctypes.c_int, ctypes.c_int, wintypes.BOOL,
    ]
    user32.GetDC.argtypes = [wintypes.HWND]
    user32.GetDC.restype = wintypes.HDC
    user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
    user32.FillRect.argtypes = [wintypes.HDC, ctypes.POINTER(wintypes.RECT), wintypes.HBRUSH]
    user32.DrawTextW.argtypes = [
        wintypes.HDC, wintypes.LPCWSTR, ctypes.c_int,
        ctypes.POINTER(wintypes.RECT), ctypes.c_uint,
    ]
    user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
    user32.GetCursorPos.restype = wintypes.BOOL
    user32.GetCursorInfo.argtypes = [ctypes.POINTER(_CURSORINFO)]
    user32.GetCursorInfo.restype = wintypes.BOOL
    user32.GetIconInfo.argtypes = [wintypes.HICON, ctypes.POINTER(_ICONINFO)]
    user32.GetIconInfo.restype = wintypes.BOOL
    user32.RegisterClassW.restype = wintypes.ATOM
    user32.PeekMessageW.argtypes = [
        ctypes.POINTER(wintypes.MSG), wintypes.HWND,
        wintypes.UINT, wintypes.UINT, wintypes.UINT,
    ]
    user32.PeekMessageW.restype = wintypes.BOOL
    user32.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
    user32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
    user32.DispatchMessageW.restype = ctypes.c_ssize_t

    # The one that actually crashed: an undeclared restype defaults to C int,
    # so a module handle above 4 GB is silently truncated and RegisterClassW
    # then faults on a garbage hInstance. It only reproduces when ASLR happens
    # to place the module high, which is why it survived hand-testing and
    # surfaced as an access violation under pytest.
    ctypes.windll.kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
    ctypes.windll.kernel32.GetModuleHandleW.restype = wintypes.HMODULE
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
    gdi32.GetObjectW.argtypes = [
        wintypes.HGDIOBJ, ctypes.c_int, wintypes.LPVOID,
    ]
    gdi32.GetObjectW.restype = ctypes.c_int
    gdi32.GetDIBits.argtypes = [
        wintypes.HDC, wintypes.HBITMAP, wintypes.UINT, wintypes.UINT,
        wintypes.LPVOID, wintypes.LPVOID, wintypes.UINT,
    ]
    gdi32.GetDIBits.restype = ctypes.c_int
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


class _CURSORINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("hCursor", wintypes.HANDLE),
        ("ptScreenPos", wintypes.POINT),
    ]


class _ICONINFO(ctypes.Structure):
    _fields_ = [
        ("fIcon", wintypes.BOOL),
        ("xHotspot", wintypes.DWORD),
        ("yHotspot", wintypes.DWORD),
        ("hbmMask", wintypes.HBITMAP),
        ("hbmColor", wintypes.HBITMAP),
    ]


class _BITMAP(ctypes.Structure):
    _fields_ = [
        ("bmType", ctypes.c_long),
        ("bmWidth", ctypes.c_long),
        ("bmHeight", ctypes.c_long),
        ("bmWidthBytes", ctypes.c_long),
        ("bmPlanes", wintypes.WORD),
        ("bmBitsPixel", wintypes.WORD),
        ("bmBits", wintypes.LPVOID),
    ]


class _RGBQUAD(ctypes.Structure):
    _fields_ = [
        ("rgbBlue", ctypes.c_ubyte),
        ("rgbGreen", ctypes.c_ubyte),
        ("rgbRed", ctypes.c_ubyte),
        ("rgbReserved", ctypes.c_ubyte),
    ]


class _BITMAPINFO(ctypes.Structure):
    _fields_ = [
        ("bmiHeader", _BITMAPINFOHEADER),
        ("bmiColors", _RGBQUAD * 2),
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


# ── cursor glow ──────────────────────────────────────────────────────
#
# The edge glow says "Resonant is driving". The cursor glow says *where*.
# Because the real system cursor is already visible, the overlay only traces
# its familiar arrow silhouette instead of painting another pointer over it.

RING_BOX = 160          # square window, anchored at the cursor hot spot
_RING_RADIUS = 18
_RING_ALPHA = 150
_CURSOR_ANCHOR = RING_BOX // 2
# The core hugs the native cursor by only two pixels. The bloom is wider but
# deliberately faint, so it reads as emitted light rather than a larger arrow.
_CURSOR_OUTLINE_PX = 1.5
_CURSOR_GLOW_PX = 8.0
_CURSOR_ALPHA = 235
# Click feedback: the ring expands outward and fades. Pre-rendered because
# re-rasterising on the click path would put Python drawing work between the
# agent's click and the screenshot that follows it.
_PULSE_FRAMES = 9
_PULSE_MAX_RADIUS = 46


def _build_ring_frame(radius: float, thickness: float, alpha_scale: float) -> bytearray:
    """One premultiplied BGRA frame containing a click ripple."""
    red, green, blue = _GLOW_RGB
    size = RING_BOX
    stride = size * 4
    buffer = bytearray(stride * size)
    centre = (size - 1) / 2.0
    # A wide stroke gets a proportional feather so it fades like emitted light
    # instead of presenting a crisp progress-ring edge.  The small-thickness
    # path remains useful for the brief click ripple below.
    feather = min(
        max(1.2, thickness * 0.75),
        max(1.2, radius - thickness / 2 - 2.0),
    )

    for y in range(size):
        dy = y - centre
        base = y * stride
        for x in range(size):
            dx = x - centre
            distance = math.hypot(dx, dy)
            edge = abs(distance - radius)
            half_stroke = thickness / 2
            if edge > half_stroke + feather:
                continue
            if edge <= half_stroke:
                # Even the brightest part is gently rounded.  This avoids a
                # visible circular stroke while keeping the cursor-sized hole
                # in the middle completely transparent.
                coverage = 0.72 + 0.28 * (1.0 - edge / max(half_stroke, 0.01))
            else:
                fade = 1.0 - (edge - half_stroke) / feather
                coverage = fade * fade
            alpha = int(_RING_ALPHA * alpha_scale * coverage)
            if alpha <= 0:
                continue
            offset = base + x * 4
            buffer[offset] = blue * alpha // 255
            buffer[offset + 1] = green * alpha // 255
            buffer[offset + 2] = red * alpha // 255
            buffer[offset + 3] = alpha
    return buffer


def _read_bitmap_pixels(
    bitmap: int,
    width: int,
    height: int,
    bit_count: int,
) -> tuple[bytes, int] | None:
    """Read a GDI bitmap into a top-down, DWORD-aligned DIB buffer."""
    user32 = ctypes.windll.user32
    gdi32 = ctypes.windll.gdi32
    info = _BITMAPINFO()
    header = info.bmiHeader
    header.biSize = ctypes.sizeof(_BITMAPINFOHEADER)
    header.biWidth = width
    header.biHeight = -height
    header.biPlanes = 1
    header.biBitCount = bit_count
    header.biCompression = _BI_RGB
    stride = ((width * bit_count + 31) // 32) * 4
    pixels = (ctypes.c_ubyte * (stride * height))()
    screen_dc = user32.GetDC(None)
    try:
        lines = gdi32.GetDIBits(
            screen_dc, bitmap, 0, height, pixels,
            ctypes.byref(info), _DIB_RGB_COLORS,
        )
        if lines != height:
            return None
        return bytes(pixels), stride
    finally:
        user32.ReleaseDC(None, screen_dc)


def _mask_bit(pixels: bytes, stride: int, x: int, y: int) -> int:
    """Read one MSB-first pixel from a 1-bit DIB."""
    value = pixels[y * stride + x // 8]
    return (value >> (7 - x % 8)) & 1


def _default_arrow_mask() -> tuple[bytearray, int, int, int, int]:
    """Pixel mask matching the standard 32px Windows arrow.

    This is used only when an application deliberately installs a transparent
    cursor. Keeping the fallback at native cursor dimensions avoids returning
    to the oversized decorative arrow that this indicator replaced.
    """
    width, height = 25, 32
    polygon = (
        (0.0, 0.0),
        (0.0, 24.0),
        (6.5, 17.5),
        (12.5, 31.0),
        (19.0, 28.0),
        (13.0, 16.0),
        (24.0, 16.0),
    )
    mask = bytearray(width * height)
    for y in range(height):
        py = y + 0.5
        for x in range(width):
            px = x + 0.5
            inside = False
            previous = polygon[-1]
            for current in polygon:
                x1, y1 = previous
                x2, y2 = current
                crosses = (y1 > py) != (y2 > py)
                if crosses:
                    intersection = (x2 - x1) * (py - y1) / (y2 - y1) + x1
                    if px < intersection:
                        inside = not inside
                previous = current
            if inside:
                mask[y * width + x] = 255
    return mask, width, height, 0, 0


def _current_cursor_state():
    """Return the visible cursor handle and screen position."""
    if not IS_WINDOWS:
        return None
    _declare_signatures()
    cursor_info = _CURSORINFO()
    cursor_info.cbSize = ctypes.sizeof(_CURSORINFO)
    if not ctypes.windll.user32.GetCursorInfo(ctypes.byref(cursor_info)):
        return None
    if not (cursor_info.flags & _CURSOR_SHOWING) or not cursor_info.hCursor:
        return None
    return (
        int(cursor_info.hCursor),
        int(cursor_info.ptScreenPos.x),
        int(cursor_info.ptScreenPos.y),
    )


def _capture_cursor_mask(cursor_handle: int | None = None):
    """Capture the active Windows cursor's exact alpha mask and hot spot.

    Modern cursors expose a 32-bit alpha bitmap. Legacy cursors use a 1-bit
    transparency mask (or stacked AND/XOR masks); both paths are preserved so
    the glow follows Windows' actual cursor rather than an approximation.
    """
    if not IS_WINDOWS:
        return None
    _declare_signatures()
    user32 = ctypes.windll.user32
    gdi32 = ctypes.windll.gdi32

    if cursor_handle is None:
        cursor_state = _current_cursor_state()
        if cursor_state is None:
            return None
        cursor_handle = cursor_state[0]
    requested_handle = int(cursor_handle)

    icon_info = _ICONINFO()
    if not user32.GetIconInfo(cursor_handle, ctypes.byref(icon_info)):
        return None
    try:
        source_bitmap = icon_info.hbmColor or icon_info.hbmMask
        bitmap_info = _BITMAP()
        if not source_bitmap or not gdi32.GetObjectW(
            source_bitmap, ctypes.sizeof(_BITMAP), ctypes.byref(bitmap_info)
        ):
            return None
        width = abs(int(bitmap_info.bmWidth))
        height = abs(int(bitmap_info.bmHeight))
        if not icon_info.hbmColor:
            height //= 2  # monochrome masks stack AND and XOR planes
        if width <= 0 or height <= 0:
            return None

        mask = bytearray(width * height)
        if icon_info.hbmColor:
            color = _read_bitmap_pixels(icon_info.hbmColor, width, height, 32)
            if color is None:
                return None
            color_pixels, color_stride = color
            for y in range(height):
                for x in range(width):
                    mask[y * width + x] = color_pixels[
                        y * color_stride + x * 4 + 3
                    ]

        # Some legacy colour cursors have no alpha channel, while monochrome
        # cursors store stacked AND/XOR planes. The 1-bit mask recovers both
        # cases without inventing a geometric approximation.
        if not any(mask):
            raw_mask_height = height if icon_info.hbmColor else height * 2
            raw_mask = _read_bitmap_pixels(
                icon_info.hbmMask, width, raw_mask_height, 1
            )
            if raw_mask is None:
                return None
            mask_pixels, mask_stride = raw_mask
            for y in range(height):
                for x in range(width):
                    and_bit = _mask_bit(mask_pixels, mask_stride, x, y)
                    if icon_info.hbmColor:
                        visible = not and_bit
                    else:
                        xor_bit = _mask_bit(
                            mask_pixels, mask_stride, x, y + height
                        )
                        visible = not (and_bit and not xor_bit)
                    mask[y * width + x] = 255 if visible else 0
        if not any(mask):
            fallback_mask, width, height, hotspot_x, hotspot_y = (
                _default_arrow_mask()
            )
            return (
                requested_handle,
                fallback_mask,
                width,
                height,
                hotspot_x,
                hotspot_y,
            )
        return (
            requested_handle,
            mask,
            width,
            height,
            int(icon_info.xHotspot),
            int(icon_info.yHotspot),
        )
    finally:
        if icon_info.hbmMask:
            gdi32.DeleteObject(icon_info.hbmMask)
        if icon_info.hbmColor:
            gdi32.DeleteObject(icon_info.hbmColor)


def _build_cursor_glow_frame(
    mask: bytearray,
    width: int,
    height: int,
    hotspot_x: int,
    hotspot_y: int,
    alpha_scale: float = 1.0,
) -> bytearray:
    """Build a tight outline and soft bloom from a native cursor mask."""
    red, green, blue = _GLOW_RGB
    size = RING_BOX
    stride = size * 4
    buffer = bytearray(stride * size)
    silhouette = bytearray(size * size)
    origin_x = _CURSOR_ANCHOR - hotspot_x
    origin_y = _CURSOR_ANCHOR - hotspot_y
    for source_y in range(height):
        target_y = origin_y + source_y
        if not 0 <= target_y < size:
            continue
        for source_x in range(width):
            target_x = origin_x + source_x
            if not 0 <= target_x < size:
                continue
            coverage = mask[source_y * width + source_x]
            if coverage > 24:
                silhouette[target_y * size + target_x] = coverage

    edge_points = []
    for y in range(size):
        for x in range(size):
            if not silhouette[y * size + x]:
                continue
            if any(
                not (0 <= x + dx < size and 0 <= y + dy < size)
                or not silhouette[(y + dy) * size + x + dx]
                for dx, dy in ((-1, 0), (1, 0), (0, -1), (0, 1))
            ):
                edge_points.append((x + 0.5, y + 0.5))
    if not edge_points:
        return buffer

    for y in range(size):
        base = y * stride
        py = y + 0.5
        for x in range(size):
            px = x + 0.5
            distance = min(
                math.hypot(px - edge_x, py - edge_y)
                for edge_x, edge_y in edge_points
            )
            if distance > _CURSOR_GLOW_PX:
                continue

            # Draw only the edge pixels inside the silhouette; the real cursor
            # covers those. Outside it, a two-pixel purple core hugs the exact
            # contour and a much fainter bloom falls away independently.
            inside = bool(silhouette[y * size + x])
            if inside and distance > 0.75:
                continue
            bloom = max(0.0, 1.0 - distance / _CURSOR_GLOW_PX) ** 2.4
            core = max(0.0, 1.0 - distance / _CURSOR_OUTLINE_PX)
            coverage = min(1.0, 0.46 * bloom + 0.92 * core)
            alpha = int(_CURSOR_ALPHA * alpha_scale * coverage)
            if alpha <= 0:
                continue
            offset = base + x * 4
            buffer[offset] = blue * alpha // 255
            buffer[offset + 1] = green * alpha // 255
            buffer[offset + 2] = red * alpha // 255
            buffer[offset + 3] = alpha
    return buffer


def _composite_frames(base: bytearray, overlay: bytearray) -> bytearray:
    """Alpha-composite two equally sized premultiplied BGRA frames."""
    result = bytearray(base)
    for offset in range(0, len(result), 4):
        source_alpha = overlay[offset + 3]
        if source_alpha == 0:
            continue
        inverse = 255 - source_alpha
        for channel in range(3):
            result[offset + channel] = min(
                255,
                overlay[offset + channel]
                + result[offset + channel] * inverse // 255,
            )
        result[offset + 3] = min(
            255,
            source_alpha + result[offset + 3] * inverse // 255,
        )
    return result


class _Overlay:
    """A single click-through, per-pixel-alpha window covering one monitor."""

    def __init__(self, *, cursor_indicator: bool = True) -> None:
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
        self._cursor_indicator = cursor_indicator
        # Cursor glow — a second window, owned by the same thread for the same
        # reason the first one is: cross-thread window calls deadlock.
        self._ring_hwnd = None
        self._ring_hdc = None
        self._ring_bitmaps: list = []
        self._ring_old_bitmap = None
        self._ring_shown = False
        self._ring_frame = 0          # 0 = idle ring, 1..N = click pulse
        self._ring_last_pos = None
        self._ring_cursor_handle = None

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

    def _release_ring(self) -> None:
        """Free the ring's DC and its pre-rendered frames.

        Ten DIB sections at 116x116 is not much, but GDI objects are a
        per-process quota and leaking them across a long autonomous run is how
        a process ends up unable to create any window at all.
        """
        gdi32 = ctypes.windll.gdi32
        if self._ring_hdc:
            if self._ring_old_bitmap:
                gdi32.SelectObject(self._ring_hdc, self._ring_old_bitmap)
            gdi32.DeleteDC(self._ring_hdc)
        for bitmap in self._ring_bitmaps:
            gdi32.DeleteObject(bitmap)
        self._ring_bitmaps = []
        self._ring_hdc = self._ring_old_bitmap = None
        self._ring_cursor_handle = None

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
        # The loop runs at cursor-tracking speed; the edge glow is recomposited
        # only every few ticks. Compositing a full-monitor layered surface at
        # 60 Hz is real GPU and CPU work for a pulse nobody can perceive that
        # fast, while the ring genuinely needs the rate to not visibly lag the
        # pointer it is drawing around.
        frame = 1.0 / _RING_FPS
        glow_every = max(1, round(_RING_FPS / _PULSE_FPS))
        tick = 0
        started = time.monotonic()
        try:
            if not self._ensure_window():
                self._ready.set()
                return
            self._ready.set()
            while not self._shutdown.is_set():
                self._drain_commands()
                self._pump_messages()
                self._tick_ring()
                if self._shown and tick % glow_every == 0:
                    phase = (time.monotonic() - started) / _PULSE_PERIOD_S
                    # Sine eased into [_PULSE_MIN, _PULSE_MAX] — no hard edge.
                    wave = (math.sin(phase * 2 * math.pi) + 1.0) / 2.0
                    self._composite(_PULSE_MIN + (_PULSE_MAX - _PULSE_MIN) * wave)
                tick += 1
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
                    if self._cursor_indicator:
                        self._apply_ring_show()
                elif action == "hide":
                    self._apply_hide()
                elif action == "click":
                    # Restart the pulse from frame 1 even if one is already
                    # running: a double-click should read as two beats, not one
                    # long fade.
                    self._ring_frame = 1
                    self._ring_last_pos = None  # force a redraw this tick
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
        self._apply_ring_hide()

    # ── cursor glow ──────────────────────────────────────────────────

    def _ensure_ring(self) -> bool:
        if self._ring_hwnd:
            return bool(self._ring_bitmaps) or self._rebuild_ring_frames()
        user32 = ctypes.windll.user32
        gdi32 = ctypes.windll.gdi32

        self._ring_hwnd = user32.CreateWindowExW(
            _WS_EX_LAYERED | _WS_EX_TRANSPARENT | _WS_EX_TOPMOST
            | _WS_EX_TOOLWINDOW | _WS_EX_NOACTIVATE,
            "ResonantComputerUseHalo", "Resonant cursor",
            _WS_POPUP,
            0, 0, RING_BOX, RING_BOX,
            None, None, ctypes.windll.kernel32.GetModuleHandleW(None), None,
        )
        if not self._ring_hwnd:
            return False

        screen_dc = user32.GetDC(None)
        try:
            self._ring_hdc = gdi32.CreateCompatibleDC(screen_dc)
        finally:
            user32.ReleaseDC(None, screen_dc)
        if not self._ring_hdc:
            return False
        return self._rebuild_ring_frames()

    def _rebuild_ring_frames(self, cursor_handle: int | None = None) -> bool:
        """Rebuild the glow whenever Windows changes the native cursor."""
        snapshot = _capture_cursor_mask(cursor_handle)
        if snapshot is None:
            return False
        handle, mask, width, height, hotspot_x, hotspot_y = snapshot
        if handle == self._ring_cursor_handle and self._ring_bitmaps:
            return True

        cursor_glow = _build_cursor_glow_frame(
            mask, width, height, hotspot_x, hotspot_y
        )
        frames = [cursor_glow]
        for step in range(1, _PULSE_FRAMES + 1):
            progress = step / _PULSE_FRAMES
            ripple = _build_ring_frame(
                _RING_RADIUS + (_PULSE_MAX_RADIUS - _RING_RADIUS) * progress,
                max(1.4, 4.0 * (1.0 - 0.55 * progress)),
                0.9 * (1.0 - progress),
            )
            frames.append(_composite_frames(cursor_glow, ripple))

        header = _BITMAPINFOHEADER()
        header.biSize = ctypes.sizeof(_BITMAPINFOHEADER)
        header.biWidth = RING_BOX
        header.biHeight = -RING_BOX  # top-down, matching the row order
        header.biPlanes = 1
        header.biBitCount = 32
        header.biCompression = _BI_RGB

        new_bitmaps = []
        user32 = ctypes.windll.user32
        gdi32 = ctypes.windll.gdi32
        screen_dc = user32.GetDC(None)
        try:
            for pixels in frames:
                bits = ctypes.c_void_p()
                bitmap = gdi32.CreateDIBSection(
                    screen_dc, ctypes.byref(header), _DIB_RGB_COLORS,
                    ctypes.byref(bits), None, 0,
                )
                if not bitmap or not bits:
                    for pending in new_bitmaps:
                        gdi32.DeleteObject(pending)
                    return False
                ctypes.memmove(bits, bytes(pixels), len(pixels))
                new_bitmaps.append(bitmap)
        finally:
            user32.ReleaseDC(None, screen_dc)

        # Put the stock bitmap back before deleting any currently selected
        # frame. This is mandatory when a link/resize cursor appears mid-run.
        if self._ring_old_bitmap:
            gdi32.SelectObject(self._ring_hdc, self._ring_old_bitmap)
        for bitmap in self._ring_bitmaps:
            gdi32.DeleteObject(bitmap)
        self._ring_bitmaps = new_bitmaps
        self._ring_old_bitmap = None
        self._ring_cursor_handle = handle
        self._ring_last_pos = None
        return bool(self._ring_bitmaps)

    def _composite_ring(self, x: int, y: int) -> None:
        """Place the glow's hot spot on (x, y) and draw the current frame."""
        if not (self._ring_hwnd and self._ring_hdc and self._ring_bitmaps):
            return
        gdi32 = ctypes.windll.gdi32
        user32 = ctypes.windll.user32

        index = min(self._ring_frame, len(self._ring_bitmaps) - 1)
        previous = gdi32.SelectObject(self._ring_hdc, self._ring_bitmaps[index])
        if self._ring_old_bitmap is None:
            self._ring_old_bitmap = previous

        blend = _BLENDFUNCTION(_AC_SRC_OVER, 0, 255, _AC_SRC_ALPHA)
        size = wintypes.SIZE(RING_BOX, RING_BOX)
        source = wintypes.POINT(0, 0)
        dest = wintypes.POINT(x - RING_BOX // 2, y - RING_BOX // 2)
        user32.UpdateLayeredWindow(
            self._ring_hwnd, None,
            ctypes.byref(dest), ctypes.byref(size),
            self._ring_hdc, ctypes.byref(source),
            0, ctypes.byref(blend), _ULW_ALPHA,
        )

    def _apply_ring_show(self) -> None:
        if not self._ensure_ring():
            return
        cursor_state = _current_cursor_state()
        if cursor_state is None:
            return
        cursor_handle, cursor_x, cursor_y = cursor_state
        if cursor_handle != self._ring_cursor_handle:
            if not self._rebuild_ring_frames(cursor_handle):
                return
        user32 = ctypes.windll.user32
        self._composite_ring(cursor_x, cursor_y)
        user32.ShowWindow(self._ring_hwnd, _SW_SHOWNOACTIVATE)
        user32.SetWindowPos(
            self._ring_hwnd, _HWND_TOPMOST, 0, 0, 0, 0,
            _SWP_NOMOVE | _SWP_NOSIZE | _SWP_NOACTIVATE,
        )
        self._ring_shown = True

    def _apply_ring_hide(self) -> None:
        if self._ring_hwnd:
            ctypes.windll.user32.ShowWindow(self._ring_hwnd, _SW_HIDE)
        self._ring_shown = False
        self._ring_frame = 0

    def _tick_ring(self) -> None:
        """Follow the cursor and advance any click pulse. Runs every frame."""
        if not self._ring_shown:
            return
        cursor_state = _current_cursor_state()
        if cursor_state is None:
            return
        cursor_handle, cursor_x, cursor_y = cursor_state
        if cursor_handle != self._ring_cursor_handle:
            if not self._rebuild_ring_frames(cursor_handle):
                return
        position = (cursor_x, cursor_y)
        animating = self._ring_frame > 0
        # Redraw only when something changed. A stationary cursor with no pulse
        # in flight costs nothing, which matters because this runs at 60 Hz for
        # as long as the agent is working.
        if position == self._ring_last_pos and not animating:
            return
        self._ring_last_pos = position
        self._composite_ring(cursor_x, cursor_y)
        if animating:
            self._ring_frame += 1
            if self._ring_frame > _PULSE_FRAMES:
                self._ring_frame = 0

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
        # Wait until the border is genuinely visible before returning control
        # to the desktop action.  Besides being the honest indicator timing,
        # this lets a second monitor claim a second window instead of racing a
        # still-queued first show and moving that same window away.
        return self._submit("show", (x, y, width, height), wait=True) is not None

    def hide(self, wait: bool = False) -> None:
        self._submit("hide", (), wait=wait)

    def click_pulse(self) -> None:
        self._submit("click", ())

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
_secondary_overlays: list[_Overlay] = []
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


def _all_instances() -> list[_Overlay]:
    """Snapshot every border window, with the cursor owner first."""
    primary = _instance()
    if primary is None:
        return []
    with _overlay_lock:
        return [primary, *_secondary_overlays]


def _instance_for_region(bounds: tuple[int, int, int, int]) -> Optional[_Overlay]:
    """Return a border window for ``bounds`` without replacing a live one.

    A single layered window can only draw one rectangular monitor border.  On
    a multi-display computer the previous implementation simply moved that
    window whenever activity crossed screens, making one active display lose
    its indicator.  Keep a small pool instead: visible windows retain their
    monitor until the shared linger timer expires; hidden ones are reusable.
    Only the first owns the cursor halo, so multiple borders never duplicate
    the pointer indicator.
    """
    global _secondary_overlays
    primary = _instance()
    if primary is None:
        return None
    with _overlay_lock:
        overlays = [primary, *_secondary_overlays]
        for overlay in overlays:
            if overlay.visible and overlay._bounds == bounds:
                return overlay
        for overlay in overlays:
            if not overlay.visible:
                return overlay
        overlay = _Overlay(cursor_indicator=False)
        _secondary_overlays.append(overlay)
        return overlay


def show_for_region(x: int, y: int, width: int, height: int) -> bool:
    """Draw the glow around the given screen rectangle."""
    if _suppressed:
        return False
    bounds = (int(x), int(y), int(width), int(height))
    overlay = _instance_for_region(bounds)
    if overlay is None:
        return False
    # Repeated desktop actions on the same monitor only need to extend the
    # linger timer. Avoid a synchronous window redraw when the correct border
    # is already visible.
    if overlay.visible and overlay._bounds == bounds:
        return True
    try:
        return overlay.show(*bounds)
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


def monitor_index_for_foreground_window() -> Optional[int]:
    """Return the monitor containing the foreground window's center."""
    if not IS_WINDOWS:
        return None
    try:
        user32 = ctypes.windll.user32
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return None
        rect = wintypes.RECT()
        if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            return None
        return monitor_index_for_point(
            int((rect.left + rect.right) / 2),
            int((rect.top + rect.bottom) / 2),
        )
    except Exception:
        logger.debug("foreground monitor lookup failed", exc_info=True)
        return None


def hide() -> None:
    for overlay in _all_instances():
        try:
            overlay.hide()
        except Exception:
            logger.debug("Halo overlay failed to hide", exc_info=True)


def note_click() -> None:
    """Pulse the cursor ring — the agent just clicked.

    Takes no coordinates on purpose: the ring already tracks the real cursor,
    and pyautogui has moved it to the click point by the time this is called.
    Passing the intended coordinates instead would draw the pulse where the
    agent *meant* to click, hiding exactly the mis-clicks worth seeing.
    """
    if not IS_WINDOWS:
        return
    overlay = _instance()
    if overlay is None or not overlay.visible:
        return
    try:
        overlay.click_pulse()
    except Exception:
        logger.debug("Cursor pulse failed", exc_info=True)


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
    overlays = _all_instances()
    restore: list[tuple[_Overlay, tuple]] = []
    for overlay in overlays:
        try:
            # Blocking and queue-ordered: the grab must not start until the
            # glow is actually off screen, or it lands in the very image the
            # agent reads.
            was_visible, bounds = overlay.suppress()
            if was_visible:
                restore.append((overlay, bounds))
        except Exception:
            logger.debug("Halo overlay suppression failed", exc_info=True)
    _suppressed += 1
    try:
        yield
    finally:
        _suppressed -= 1
        if restore:
            try:
                for overlay, bounds in restore:
                    overlay.show(*bounds)
                # Re-arm the linger. The pending timer can fire during the
                # capture — while the glow is already hidden, so its hide is a
                # no-op — and the restore would then bring the glow back with
                # nothing left to take it down again, stranding it on screen
                # for the rest of the session.
                _rearm_linger()
            except Exception:
                logger.debug("Halo overlay failed to restore", exc_info=True)
