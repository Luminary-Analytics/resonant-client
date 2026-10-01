"""On-screen glow shown while the agent is driving the computer.

When Lumi moves the mouse and types, the user needs to know at a glance
that the input is not theirs — otherwise the machine simply appears possessed.
This draws a soft purple glow around the edges of the monitor being acted on,
pulsing gently, with a banner reading "Lumi is using the computer", and traces
the real cursor in the same light.

Implemented directly on Win32 through ctypes. The alternatives were a second
pywebview window (heavy, and transparency support on Windows is patchy) or
tkinter (explicitly excluded from the bundle). ctypes adds nothing to the
installer, and `computer_use.py` already drives Win32 the same way.

The glow needs a real alpha ramp, so every window is composited with
`UpdateLayeredWindow` and a premultiplied 32-bit bitmap. The simpler
`SetLayeredWindowAttributes` colour-key route cannot express partial
transparency — every pixel is either fully drawn or fully absent — which gives
a hard border, not a glow. The pixels are computed here: shapes from signed
distances with analytic antialiasing, and the banner's text by FreeType through
Pillow, which the desktop tools already depend on. GDI draws the text when
Pillow is unavailable, so the banner never goes blank.

Three properties matter and each is deliberate:

- **Click-through.** `WS_EX_TRANSPARENT` means the overlay never intercepts a
  click. An indicator that blocked the very input it is announcing would be
  worse than no indicator.
- **Invisible to screen capture.** The agent screenshots the desktop to decide
  where to click. If it saw the glow it would be reading Lumi's own chrome
  as part of the application under test, and the banner sits exactly where a
  title bar usually is. `hidden_for_capture()` takes it down for the grab.
- **Non-fatal.** Every entry point swallows its own failures. A decorative
  border must never be the reason a computer-use run dies, and the module
  no-ops entirely off Windows.
"""

import ctypes
import logging
import math
import os
import queue
import threading
import time
from contextlib import contextmanager
from ctypes import wintypes
from typing import Optional

logger = logging.getLogger(__name__)

IS_WINDOWS = hasattr(ctypes, "windll")

BANNER_TEXT = "Lumi is using the computer"

# Sizes below are at 100% display scaling. Each monitor's surfaces are drawn at
# its own DPI, so the indicator keeps its proportions on a high-density screen.

# ── edge glow ────────────────────────────────────────────────────────

# How far the glow reaches inward before it fades to nothing. Generous enough
# to read as a glow rather than a thick border.
GLOW_PX = 90
# Alpha at the very edge of the screen, tapering to 0 at GLOW_PX inward.
_EDGE_ALPHA = 190
# Lumi purple, as (R, G, B).
_GLOW_RGB = (124, 92, 255)
# A thin, lighter rim along the very edge gives the glow a defined border, the
# way a lit bezel reads, rather than a haze with no edge to it.
_RIM_PX = 2.0
_RIM_ALPHA = 215
_RIM_RGB = (156, 132, 255)
# The glow turns each corner along a superellipse of this exponent. Combining
# the two edge distances with min() instead meets in a hard diagonal crease.
_CORNER_EXPONENT = 5.0

# ── banner ───────────────────────────────────────────────────────────

_BANNER_HEIGHT = 36
# From the top of the monitor to the top of the pill.
_BANNER_TOP = 30
_BANNER_FONT_PX = 14
# The product name is set a weight heavier than the rest of the sentence.
_BANNER_LEAD = "Lumi"
_BANNER_LEAD_WEIGHT = 650
_BANNER_TEXT_WEIGHT = 480
_BANNER_LEAD_RGB = (250, 248, 255)
_BANNER_TEXT_RGB = (214, 208, 238)
# A status light at the pill's left end, concentric with its rounded cap.
_BANNER_DOT_RADIUS = 4.0
_BANNER_DOT_RGB = (164, 142, 255)
_BANNER_HALO_PX = 2.6
_BANNER_HALO_ALPHA = 0.55
_BANNER_TEXT_GAP = 8.0
_BANNER_PAD_RIGHT = 17.0
# Dark, faintly purple glass, lit slightly from above.
_BANNER_FILL_TOP = (38, 31, 54)
_BANNER_FILL_BOTTOM = (25, 20, 36)
_BANNER_FILL_ALPHA = 0.95
# A hairline just inside the pill's edge keeps it distinct on dark screens.
_BANNER_EDGE_RGB = (184, 166, 255)
_BANNER_EDGE_ALPHA_TOP = 0.36
_BANNER_EDGE_ALPHA_BOTTOM = 0.16
# (vertical offset, blur sigma, opacity): an ambient shadow and a contact
# shadow, which lift the pill off a light screen.
_BANNER_SHADOWS = ((3.0, 7.0, 0.30), (1.0, 1.2, 0.18))
_BANNER_MARGIN_X = 18
_BANNER_MARGIN_TOP = 14
_BANNER_MARGIN_BOTTOM = 26
# Light text on a dark ground reads thinner than its coverage says; lifting
# the midtones restores the weight the font was designed with.
_TEXT_GAMMA = 0.85
# The banner drops in from this far above as it fades in.
_BANNER_SLIDE_PX = 6

# ── motion ───────────────────────────────────────────────────────────

# The pulse. Slow and shallow: a fast or deep blink in peripheral vision is
# genuinely unpleasant to sit next to for a long run.
_PULSE_PERIOD_S = 2.6
_PULSE_MIN = 0.62      # fraction of full intensity at the trough
_PULSE_MAX = 1.0
_PULSE_FPS = 25
# The ring has to keep up with a moving pointer, so the worker ticks at this
# rate and the glow's opacity is updated on a subset of those ticks.
_RING_FPS = 60
# Fades. Short enough that the indicator still arrives with the first action
# and leaves with the last, long enough not to pop. Restoring after a screen
# capture skips the fade: the glow must not visibly dip at every screenshot.
_FADE_IN_S = 0.16
_FADE_OUT_S = 0.28

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
_DT_SINGLELINE = 0x00000020
_DT_NOPREFIX = 0x00000800
_DT_CALCRECT = 0x00000400
_TRANSPARENT_BK = 1
_DEFAULT_CHARSET = 1
_ANTIALIASED_QUALITY = 4
_CURSOR_SHOWING = 0x00000001
_MONITOR_DEFAULTTONEAREST = 2
_MDT_EFFECTIVE_DPI = 0

_SQRT2 = math.sqrt(2.0)
_TRANSPARENT = bytes(4)


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
    user32.MonitorFromPoint.argtypes = [wintypes.POINT, wintypes.DWORD]
    user32.MonitorFromPoint.restype = wintypes.HMONITOR

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
    gdi32.CreateFontW.argtypes = [
        ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
        wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
        wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
        wintypes.LPCWSTR,
    ]
    gdi32.CreateFontW.restype = wintypes.HFONT
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
    try:
        # Per-monitor DPI (Windows 8.1+). Without it every monitor is drawn at
        # 100%, which is also what an older system reports anyway.
        shcore = ctypes.windll.shcore
        shcore.GetDpiForMonitor.argtypes = [
            wintypes.HMONITOR, ctypes.c_int,
            ctypes.POINTER(ctypes.c_uint), ctypes.POINTER(ctypes.c_uint),
        ]
        shcore.GetDpiForMonitor.restype = ctypes.c_long
    except (AttributeError, OSError):
        logger.debug("shcore unavailable; drawing the overlay at 100%")
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


def _display_scale(x: int, y: int, width: int, height: int) -> float:
    """The DPI scale of the monitor holding this rectangle; 1.0 is 100%."""
    if not IS_WINDOWS:
        return 1.0
    try:
        _declare_signatures()
        monitor = ctypes.windll.user32.MonitorFromPoint(
            wintypes.POINT(x + width // 2, y + height // 2),
            _MONITOR_DEFAULTTONEAREST,
        )
        dpi_x, dpi_y = ctypes.c_uint(), ctypes.c_uint()
        if monitor and ctypes.windll.shcore.GetDpiForMonitor(
            monitor, _MDT_EFFECTIVE_DPI, ctypes.byref(dpi_x), ctypes.byref(dpi_y)
        ) == 0 and dpi_x.value:
            return min(4.0, max(1.0, dpi_x.value / 96.0))
    except Exception:
        logger.debug("Monitor DPI lookup failed", exc_info=True)
    return 1.0


def _clamp01(value: float) -> float:
    return 0.0 if value <= 0.0 else 1.0 if value >= 1.0 else value


def _coverage(signed_distance: float) -> float:
    """How much of a pixel a shape covers, from its centre's signed distance.

    A one-pixel box filter: covered a half pixel inside the edge, empty a half
    pixel outside it. This is what antialiases every shape drawn here.
    """
    return _clamp01(0.5 - signed_distance)


def _alpha_byte(level: float) -> int:
    return max(0, min(255, round(255 * level)))


# ── edge glow ────────────────────────────────────────────────────────


def _corner_distance(horizontal: float, vertical: float) -> float:
    """Distance in from the lit edges, turning each corner smoothly.

    The distance to the nearer edge alone meets itself along a hard diagonal
    crease in every corner. A p-norm of the two rounds the contours into a
    superellipse, brightening the corner only slightly. Once one distance is
    three times the other the two agree to within a tenth of a percent, which
    is what lets whole rows of the glow be shared.
    """
    if horizontal >= 3.0 * vertical:
        return vertical
    if vertical >= 3.0 * horizontal:
        return horizontal
    power = _CORNER_EXPONENT
    return (horizontal ** -power + vertical ** -power) ** (-1.0 / power)


def _glow_pixel(distance: float, reach: float, rim: float) -> bytes:
    """Premultiplied BGRA for a pixel `distance` px in from the lit edge.

    Squared falloff rather than linear — a linear ramp reads as a wide flat
    band with a visible cutoff, while the square concentrates the brightness
    at the edge and lets the tail vanish smoothly. The rim sits over it.
    """
    glow = 0.0
    if distance < reach:
        t = 1.0 - distance / reach
        glow = _EDGE_ALPHA / 255.0 * t * t
    rim_alpha = _RIM_ALPHA / 255.0 * _clamp01(rim + 0.5 - distance)
    alpha = rim_alpha + glow * (1.0 - rim_alpha)
    if alpha < 0.5 / 255.0:
        return _TRANSPARENT
    # Premultiplied: the compositor expects colour already scaled by alpha,
    # otherwise the glow washes out to white at the edges.
    under = glow * (1.0 - rim_alpha)
    return bytes((
        round(_RIM_RGB[2] * rim_alpha + _GLOW_RGB[2] * under),
        round(_RIM_RGB[1] * rim_alpha + _GLOW_RGB[1] * under),
        round(_RIM_RGB[0] * rim_alpha + _GLOW_RGB[0] * under),
        round(alpha * 255.0),
    ))


def _build_glow_rows(width: int, height: int, scale: float = 1.0) -> bytes:
    """Premultiplied BGRA for the whole monitor, transparent except the glow.

    A pixel depends only on its distances to the nearest vertical and
    horizontal edges, so rows repeat. Each distinct row is built once, and
    within it only the bands near the side edges are computed per pixel; the
    middle is a single repeated value. That keeps even a 4K surface to tens of
    thousands of pixel evaluations in Python rather than millions.
    """
    reach = GLOW_PX * scale
    rim = max(1.0, _RIM_PX * scale)
    half = width // 2
    # Rows further than this from the top and bottom are all alike.
    settle = int(math.ceil(3.0 * reach))
    cache: dict[int, bytes] = {}

    def row_for(key: int) -> bytes:
        cached = cache.get(key)
        if cached is not None:
            return cached
        vertical = key + 0.5
        if vertical < reach:
            # Past three times the vertical distance the side edges no
            # longer matter: the row carries the top/bottom value.
            band = min(half, int(3.0 * vertical) + 1)
        else:
            # Only the side ramps are lit; beyond them nothing is.
            band = min(half, int(1.4 * reach) + 1)
        pixels = [
            _glow_pixel(_corner_distance(x + 0.5, vertical), reach, rim)
            for x in range(band)
        ]
        middle = width - 2 * band
        if middle <= 0:
            centre = b""
        elif band == half:
            # An odd width's single centre pixel, equidistant from both sides.
            centre = _glow_pixel(_corner_distance(half + 0.5, vertical), reach, rim)
        else:
            centre = _glow_pixel(vertical, reach, rim) * middle
        row = b"".join(pixels) + centre + b"".join(reversed(pixels))
        cache[key] = row
        return row

    return b"".join(
        row_for(min(y, height - 1 - y, settle)) for y in range(height)
    )


# ── banner ───────────────────────────────────────────────────────────
#
# A pill at the top of the monitor: shadow, body, hairline and status light
# drawn from signed distances, the text rasterised separately and laid over.

_TEXT_GAMMA_LUT = [round(255 * (value / 255) ** _TEXT_GAMMA) for value in range(256)]
_banner_cache: dict[float, tuple[bytes, int, int, int]] = {}


def _banner_runs() -> list[tuple[str, int]]:
    """The banner text as (text, weight) runs: the name, then the sentence."""
    if BANNER_TEXT.startswith(_BANNER_LEAD + " "):
        return [
            (_BANNER_LEAD, _BANNER_LEAD_WEIGHT),
            (BANNER_TEXT[len(_BANNER_LEAD):], _BANNER_TEXT_WEIGHT),
        ]
    return [(BANNER_TEXT, _BANNER_LEAD_WEIGHT)]


def _banner_font(size: int, weight: int, points: float):
    """Segoe UI Variable at this weight, or the nearest face that exists.

    Windows 11 ships Segoe UI Variable with weight and optical-size axes;
    Windows 10 has the static Segoe UI faces. Pillow's own default face keeps
    the banner legible where neither exists.
    """
    from PIL import ImageFont

    fonts = os.path.join(
        os.environ.get("WINDIR") or os.environ.get("SystemRoot") or r"C:\Windows",
        "Fonts",
    )
    static = ("seguisb.ttf", "arialbd.ttf") if weight >= 550 else ("segoeui.ttf", "arial.ttf")
    for name in ("SegUIVar.ttf", *static):
        path = os.path.join(fonts, name)
        if not os.path.exists(path):
            continue
        try:
            font = ImageFont.truetype(path, size)
            if name == "SegUIVar.ttf":
                values = []
                for axis in font.get_variation_axes():
                    label = axis.get("name", b"")
                    if isinstance(label, bytes):
                        label = label.decode("ascii", "ignore")
                    label = str(label).lower()
                    if label.startswith("weight"):
                        value = weight
                    elif "optical" in label:
                        value = points
                    else:
                        value = axis.get("default", axis["minimum"])
                    values.append(max(axis["minimum"], min(axis["maximum"], value)))
                font.set_variation_by_axes(values)
            return font
        except Exception:
            logger.debug("Banner font %s unusable", path, exc_info=True)
    font = ImageFont.load_default(size)
    if not hasattr(font, "getmetrics"):
        raise RuntimeError("no scalable font for the banner")
    return font


def _freetype_text_mask(scale: float) -> tuple[bytes, int, int, float, int]:
    """Coverage of the banner text rendered by FreeType through Pillow."""
    from PIL import Image, ImageDraw

    size = max(8, round(_BANNER_FONT_PX * scale))
    # The optical size follows the logical size, not the pixel size: 14 px at
    # 150% is still 10.5 pt text and should keep its text-sized design.
    points = _BANNER_FONT_PX * 0.75
    runs = _banner_runs()
    fonts = [_banner_font(size, weight, points) for _, weight in runs]
    ascent = max(font.getmetrics()[0] for font in fonts)
    descent = max(font.getmetrics()[1] for font in fonts)
    advances = [font.getlength(text) for (text, _), font in zip(runs, fonts)]
    width = int(math.ceil(sum(advances))) + 2
    height = ascent + descent + 2
    image = Image.new("L", (width, height), 0)
    draw = ImageDraw.Draw(image)
    baseline = ascent + 1
    pen = 1.0
    split = width
    for index, ((text, _), font, advance) in enumerate(zip(runs, fonts, advances)):
        if index == 1:
            split = int(round(pen))
        draw.text((pen, baseline), text, fill=255, font=font, anchor="ls")
        pen += advance
    image = image.point(_TEXT_GAMMA_LUT)
    # Centre on the capitals, not the line box: descenders would otherwise pull
    # the words visibly high in the pill.
    cap_top = fonts[0].getbbox("H", anchor="ls")[1]
    return image.tobytes(), width, height, baseline + cap_top / 2.0, split


def _gdi_text_mask(scale: float) -> tuple[bytes, int, int, float, int]:
    """Coverage of the banner text from GDI's greyscale antialiasing.

    The fallback when Pillow is unavailable: white text on a black DIB, whose
    green channel is then the coverage. One weight, one colour.
    """
    _declare_signatures()
    user32 = ctypes.windll.user32
    gdi32 = ctypes.windll.gdi32
    size = max(8, round(_BANNER_FONT_PX * scale))
    screen_dc = user32.GetDC(None)
    try:
        hdc = gdi32.CreateCompatibleDC(screen_dc)
    finally:
        user32.ReleaseDC(None, screen_dc)
    if not hdc:
        raise OSError("no DC for the banner text")
    font = gdi32.CreateFontW(
        -size, 0, 0, 0, 600, 0, 0, 0, _DEFAULT_CHARSET, 0, 0,
        _ANTIALIASED_QUALITY, 0, "Segoe UI",
    )
    old_font = gdi32.SelectObject(hdc, font)
    bitmap = old_bitmap = None
    try:
        flags = _DT_SINGLELINE | _DT_NOPREFIX
        measured = wintypes.RECT(0, 0, 0, 0)
        user32.DrawTextW(hdc, BANNER_TEXT, -1, ctypes.byref(measured), flags | _DT_CALCRECT)
        width, height = measured.right + 2, measured.bottom + 2
        header = _BITMAPINFOHEADER()
        header.biSize = ctypes.sizeof(_BITMAPINFOHEADER)
        header.biWidth = width
        header.biHeight = -height
        header.biPlanes = 1
        header.biBitCount = 32
        header.biCompression = _BI_RGB
        bits = ctypes.c_void_p()
        bitmap = gdi32.CreateDIBSection(
            hdc, ctypes.byref(header), _DIB_RGB_COLORS, ctypes.byref(bits), None, 0,
        )
        if not bitmap or not bits:
            raise OSError("no DIB for the banner text")
        old_bitmap = gdi32.SelectObject(hdc, bitmap)
        gdi32.SetBkMode(hdc, _TRANSPARENT_BK)
        gdi32.SetTextColor(hdc, 0x00FFFFFF)
        target = wintypes.RECT(1, 1, width, height)
        user32.DrawTextW(hdc, BANNER_TEXT, -1, ctypes.byref(target), flags)
        pixels = ctypes.string_at(bits, width * height * 4)
        return pixels[1::4], width, height, height / 2.0, width
    finally:
        if old_bitmap:
            gdi32.SelectObject(hdc, old_bitmap)
        if bitmap:
            gdi32.DeleteObject(bitmap)
        gdi32.SelectObject(hdc, old_font)
        gdi32.DeleteObject(font)
        gdi32.DeleteDC(hdc)


def _banner_text_mask(scale: float) -> tuple[bytes, int, int, float, int]:
    """(coverage, width, height, centre y, x where the second colour starts)."""
    try:
        return _freetype_text_mask(scale)
    except Exception:
        logger.debug("FreeType banner text unavailable; using GDI", exc_info=True)
        return _gdi_text_mask(scale)


def _build_banner_frame(scale: float = 1.0) -> tuple[bytes, int, int, int]:
    """Premultiplied BGRA for the banner pill and its shadow.

    Returns the pixels, the frame's width and height, and the pill's offset
    from the top of the frame; everything around the pill is its shadow. The
    pill is the same on every monitor of a given scale, so it is built once.
    """
    key = round(scale, 3)
    cached = _banner_cache.get(key)
    if cached is not None:
        return cached

    coverage, text_width, text_height, text_centre, split = _banner_text_mask(scale)
    pill_height = max(16, round(_BANNER_HEIGHT * scale))
    radius = pill_height / 2.0
    dot_radius = _BANNER_DOT_RADIUS * scale
    text_offset = radius + dot_radius + _BANNER_TEXT_GAP * scale
    pill_width = int(math.ceil(text_offset + text_width + _BANNER_PAD_RIGHT * scale))
    margin_x = int(math.ceil(_BANNER_MARGIN_X * scale))
    margin_top = int(math.ceil(_BANNER_MARGIN_TOP * scale))
    width = pill_width + 2 * margin_x
    height = pill_height + margin_top + int(math.ceil(_BANNER_MARGIN_BOTTOM * scale))

    centre_x = margin_x + pill_width / 2.0
    centre_y = margin_top + pill_height / 2.0
    straight = pill_width / 2.0 - radius   # half-length of the flat run
    dot_x = centre_x - straight            # centre of the left cap
    hairline = max(1.0, scale)
    halo = _BANNER_HALO_PX * scale
    halo_reach = dot_radius + 4.0 * halo
    shadows = [
        (offset * scale, sigma * scale * _SQRT2, opacity)
        for offset, sigma, opacity in _BANNER_SHADOWS
    ]
    fill_top = [channel / 255.0 for channel in _BANNER_FILL_TOP]
    fill_bottom = [channel / 255.0 for channel in _BANNER_FILL_BOTTOM]
    edge = [channel / 255.0 for channel in _BANNER_EDGE_RGB]
    dot = [channel / 255.0 for channel in _BANNER_DOT_RGB]

    def stadium(px: float, py: float) -> float:
        """Signed distance to the pill: negative inside."""
        qx = abs(px - centre_x) - straight
        qy = abs(py - centre_y)
        if qx > 0.0:
            return math.hypot(qx, qy) - radius
        return qy - radius

    def over(dst, colour, alpha):
        keep = 1.0 - alpha
        return [
            colour[0] * alpha + dst[0] * keep,
            colour[1] * alpha + dst[1] * keep,
            colour[2] * alpha + dst[2] * keep,
            alpha + dst[3] * keep,
        ]

    def pixel(px: float, py: float, fill, edge_alpha: float, lit: bool) -> bytes:
        out = [0.0, 0.0, 0.0, 0.0]   # premultiplied RGBA
        for offset, spread, opacity in shadows:
            shade = opacity * 0.5 * math.erfc(stadium(px, py - offset) / spread)
            out = over(out, (0.0, 0.0, 0.0), shade)
        distance = stadium(px, py)
        body = _coverage(distance)
        if body > 0.0:
            out = over(out, fill, _BANNER_FILL_ALPHA * body)
            line = body - _coverage(distance + hairline)
            if line > 0.0:
                out = over(out, edge, edge_alpha * line)
        if lit:
            light = math.hypot(px - dot_x, py - centre_y) - dot_radius
            if light < halo_reach:
                glow = max(0.0, light) / halo
                out = over(out, dot, _BANNER_HALO_ALPHA * math.exp(-glow * glow))
                out = over(out, dot, _coverage(light))
        return bytes((
            round(out[2] * 255), round(out[1] * 255),
            round(out[0] * 255), round(out[3] * 255),
        ))

    # Between the status light and the right cap every column is the same, so
    # it is computed once per row and repeated.
    flat_start = max(0, int(math.ceil(dot_x + halo_reach - 0.5)))
    flat_end = int(math.floor(centre_x + straight - 0.5))
    rows = []
    for y in range(height):
        py = y + 0.5
        t = _clamp01((py - margin_top) / pill_height)
        fill = [top + (bottom - top) * t for top, bottom in zip(fill_top, fill_bottom)]
        edge_alpha = (
            _BANNER_EDGE_ALPHA_TOP
            + (_BANNER_EDGE_ALPHA_BOTTOM - _BANNER_EDGE_ALPHA_TOP) * t
        )
        if flat_start <= flat_end:
            left = [pixel(x + 0.5, py, fill, edge_alpha, True) for x in range(flat_start)]
            flat = pixel(flat_start + 0.5, py, fill, edge_alpha, False)
            right = [
                pixel(x + 0.5, py, fill, edge_alpha, False)
                for x in range(flat_end + 1, width)
            ]
            rows.append(b"".join(left) + flat * (flat_end + 1 - flat_start) + b"".join(right))
        else:
            rows.append(b"".join(
                pixel(x + 0.5, py, fill, edge_alpha, True) for x in range(width)
            ))
    frame = bytearray(b"".join(rows))

    # The text, over the pill. Its mask carries a one-pixel pad on the left.
    origin_x = int(round(margin_x + text_offset)) - 1
    origin_y = int(round(centre_y - text_centre))
    lead = _BANNER_LEAD_RGB
    rest = _BANNER_TEXT_RGB
    for ty in range(text_height):
        y = origin_y + ty
        if not 0 <= y < height:
            continue
        for tx in range(text_width):
            alpha = coverage[ty * text_width + tx]
            x = origin_x + tx
            if not alpha or not 0 <= x < width:
                continue
            red, green, blue = lead if tx < split else rest
            offset = (y * width + x) * 4
            keep = 255 - alpha
            frame[offset] = (blue * alpha + frame[offset] * keep) // 255
            frame[offset + 1] = (green * alpha + frame[offset + 1] * keep) // 255
            frame[offset + 2] = (red * alpha + frame[offset + 2] * keep) // 255
            frame[offset + 3] = (255 * alpha + frame[offset + 3] * keep) // 255

    result = (bytes(frame), width, height, margin_top)
    _banner_cache[key] = result
    return result


def _banner_origin(
    monitor_width: int, frame_width: int, pill_top: int, scale: float = 1.0
) -> tuple[int, int]:
    """Where the banner frame goes on its monitor: centred along the top."""
    return (
        (monitor_width - frame_width) // 2,
        round(_BANNER_TOP * scale) - pill_top,
    )


# ── cursor glow ──────────────────────────────────────────────────────
#
# The edge glow says "Lumi is driving". The cursor glow says *where*.
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
# The outline's profile starts this far out from the cursor's true contour.
# The glow used to measure from the centres of every pixel above a faint alpha
# threshold, which set antialiased edges half a pixel further out than hard
# ones. This value keeps that overall weight on the standard antialiased
# cursors (within 1% of their total glow) while the outline stays even.
_CURSOR_CONTOUR_SHIFT = 0.15
# Click feedback: the ring expands outward, easing out, and fades.
# Pre-rendered because re-rasterising on the click path would put Python
# drawing work between the agent's click and the screenshot that follows it.
_PULSE_FRAMES = 12
_PULSE_MAX_RADIUS = 46


def _ring_box_for(
    scale: float, width: int, height: int, hotspot_x: int, hotspot_y: int
) -> int:
    """Window size that holds the cursor's glow and the widest ripple."""
    ripple = (_PULSE_MAX_RADIUS + 3.0) * scale + 4.0
    extent = max(width - hotspot_x, height - hotspot_y, hotspot_x, hotspot_y)
    extent += _CURSOR_GLOW_PX * scale + 2.0
    box = max(RING_BOX, 2 * int(math.ceil(max(ripple, extent))) + 2)
    return box + box % 2


def _build_ring_frame(
    radius: float,
    thickness: float,
    alpha_scale: float,
    box: int = RING_BOX,
    base: Optional[bytearray] = None,
) -> bytearray:
    """One premultiplied BGRA frame containing a click ripple.

    With `base`, the ripple is composited over a copy of it rather than drawn
    on a clear frame. Only the ring's own rows and spans are visited.
    """
    red, green, blue = _GLOW_RGB
    stride = box * 4
    buffer = bytearray(base) if base is not None else bytearray(stride * box)
    # Centred on the hot-spot pixel, the anchor of every cursor frame.
    centre = box // 2
    half_stroke = thickness / 2
    # A wide stroke gets a proportional feather so it fades like emitted light
    # instead of presenting a crisp progress-ring edge.  The small-thickness
    # path remains useful for the brief click ripple below.
    feather = min(
        max(1.2, thickness * 0.75),
        max(1.2, radius - half_stroke - 2.0),
    )
    outer = radius + half_stroke + feather
    inner = max(0.0, radius - half_stroke - feather)
    for y in range(max(0, int(centre - outer) - 1), min(box, int(centre + outer) + 2)):
        dy = y - centre
        if abs(dy) > outer:
            continue
        span = math.sqrt(outer * outer - dy * dy)
        hole = math.sqrt(inner * inner - dy * dy) if abs(dy) < inner else 0.0
        low, high = int(centre - span) - 1, int(centre + span) + 2
        # Two spans either side of the hole; a row through the hole's edge
        # (or past it) is a single span.
        spans = (
            [(low, int(centre - hole) + 2), (int(centre + hole) - 1, high)]
            if hole > 2.0 else [(low, high)]
        )
        base_offset = y * stride
        for start, stop in spans:
            for x in range(max(0, start), min(box, stop)):
                distance = math.hypot(x - centre, dy)
                edge = abs(distance - radius)
                if edge > half_stroke + feather:
                    continue
                if edge <= half_stroke:
                    # Even the brightest part is gently rounded.  This avoids a
                    # visible circular stroke while keeping the cursor-sized
                    # hole in the middle completely transparent.
                    coverage = 0.72 + 0.28 * (1.0 - edge / max(half_stroke, 0.01))
                else:
                    fade = 1.0 - (edge - half_stroke) / feather
                    coverage = fade * fade
                alpha = int(_RING_ALPHA * alpha_scale * coverage)
                if alpha <= 0:
                    continue
                offset = base_offset + x * 4
                keep = 255 - alpha
                buffer[offset] = blue * alpha // 255 + buffer[offset] * keep // 255
                buffer[offset + 1] = green * alpha // 255 + buffer[offset + 1] * keep // 255
                buffer[offset + 2] = red * alpha // 255 + buffer[offset + 2] * keep // 255
                buffer[offset + 3] = alpha + buffer[offset + 3] * keep // 255
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


_NEIGHBOURS_FORWARD = ((-1, -1), (0, -1), (1, -1), (-1, 0))
_NEIGHBOURS_BACKWARD = ((1, 1), (0, 1), (-1, 1), (1, 0))


def _build_cursor_glow_frame(
    mask: bytearray,
    width: int,
    height: int,
    hotspot_x: int,
    hotspot_y: int,
    alpha_scale: float = 1.0,
    box: int = RING_BOX,
    scale: float = 1.0,
) -> bytearray:
    """Build a tight outline and soft bloom from a native cursor mask.

    Distances are measured to the cursor's antialiased contour rather than to
    the centres of its edge pixels, so the outline follows the arrow's slopes
    smoothly instead of stepping with its pixel grid. Each edge pixel's
    coverage places the contour inside it; a two-pass sweep then carries the
    nearest edge pixel across the glow's reach.
    """
    red, green, blue = _GLOW_RGB
    stride = box * 4
    buffer = bytearray(stride * box)
    anchor = box // 2
    origin_x = anchor - hotspot_x
    origin_y = anchor - hotspot_y
    outline_px = _CURSOR_OUTLINE_PX * scale
    glow_px = _CURSOR_GLOW_PX * scale

    # Work only in the cursor's box plus the glow's reach.
    apron = int(math.ceil(glow_px)) + 2
    left = max(0, origin_x - apron)
    top = max(0, origin_y - apron)
    right = min(box, origin_x + width + apron)
    bottom = min(box, origin_y + height + apron)
    grid_w, grid_h = right - left, bottom - top
    if grid_w <= 0 or grid_h <= 0:
        return buffer
    cells = grid_w * grid_h
    coverage = [0.0] * cells
    for source_y in range(height):
        grid_y = origin_y + source_y - top
        if not 0 <= grid_y < grid_h:
            continue
        for source_x in range(width):
            grid_x = origin_x + source_x - left
            if 0 <= grid_x < grid_w:
                value = mask[source_y * width + source_x]
                if value:
                    coverage[grid_y * grid_w + grid_x] = value / 255.0

    def on_contour(grid_x: int, grid_y: int, inside: bool) -> bool:
        """Whether a neighbour lies on the other side of the contour."""
        for ny in (grid_y - 1, grid_y, grid_y + 1):
            for nx in (grid_x - 1, grid_x, grid_x + 1):
                neighbour = (
                    coverage[ny * grid_w + nx]
                    if 0 <= nx < grid_w and 0 <= ny < grid_h else 0.0
                )
                if (neighbour >= 0.5) != inside:
                    return True
        return False

    # Edge sites: pixels on either side of the half-coverage contour. A faint
    # pixel away from the body (a drop shadow's fringe) is not one.
    site_x: list[int] = []
    site_y: list[int] = []
    site_offset: list[float] = []
    nearest = [-1] * cells
    distance = [math.inf] * cells
    for grid_y in range(grid_h):
        for grid_x in range(grid_w):
            index = grid_y * grid_w + grid_x
            value = coverage[index]
            if value <= 0.0 or not on_contour(grid_x, grid_y, value >= 0.5):
                continue
            # The contour lies (coverage - 0.5) px beyond this pixel's centre.
            nearest[index] = len(site_x)
            distance[index] = 0.5 - value
            site_x.append(grid_x)
            site_y.append(grid_y)
            site_offset.append(0.5 - value)
    if not site_x:
        return buffer

    def sweep(rows, columns, neighbours) -> None:
        for grid_y in rows:
            for grid_x in columns:
                index = grid_y * grid_w + grid_x
                best = distance[index]
                for dx, dy in neighbours:
                    nx, ny = grid_x + dx, grid_y + dy
                    if not (0 <= nx < grid_w and 0 <= ny < grid_h):
                        continue
                    site = nearest[ny * grid_w + nx]
                    if site < 0:
                        continue
                    candidate = math.hypot(
                        grid_x - site_x[site], grid_y - site_y[site]
                    ) + site_offset[site]
                    if candidate < best:
                        best = candidate
                        distance[index] = candidate
                        nearest[index] = site

    sweep(range(grid_h), range(grid_w), _NEIGHBOURS_FORWARD)
    sweep(range(grid_h - 1, -1, -1), range(grid_w - 1, -1, -1), _NEIGHBOURS_BACKWARD)

    for grid_y in range(grid_h):
        base = (top + grid_y) * stride
        for grid_x in range(grid_w):
            index = grid_y * grid_w + grid_x
            if coverage[index] >= 0.5:
                # Inside the silhouette the real cursor covers the glow; only
                # its edge pixels keep the core, filling under the cursor's
                # own antialiasing so the outline meets it without a gap.
                if distance[index] > 0.0 or nearest[index] < 0:
                    continue
                strength = 1.0
            else:
                reach = max(0.0, distance[index]) + _CURSOR_CONTOUR_SHIFT
                if reach >= glow_px:
                    continue
                bloom = (1.0 - reach / glow_px) ** 2.4
                core = max(0.0, 1.0 - reach / outline_px)
                strength = min(1.0, 0.46 * bloom + 0.92 * core)
            alpha = int(_CURSOR_ALPHA * alpha_scale * strength)
            if alpha <= 0:
                continue
            offset = base + (left + grid_x) * 4
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


def _build_cursor_frames(
    mask: bytearray,
    width: int,
    height: int,
    hotspot_x: int,
    hotspot_y: int,
    scale: float = 1.0,
) -> tuple[list[bytearray], int, int]:
    """The idle glow and the click ripple's frames, with their box and anchor.

    Sized by the display's scale, not the cursor's bitmap: applications and
    pointer-size settings hand Windows cursors of many sizes (64 px bitmaps
    are common at 100%), and the glow around them should keep one weight.
    The bitmap only decides how large the window must be to hold it.
    """
    box = _ring_box_for(scale, width, height, hotspot_x, hotspot_y)
    glow = _build_cursor_glow_frame(
        mask, width, height, hotspot_x, hotspot_y, box=box, scale=scale,
    )
    frames = [glow]
    for step in range(1, _PULSE_FRAMES + 1):
        progress = step / _PULSE_FRAMES
        # Ease out: the ring leaves the click point fast and settles as it
        # fades, the way a ripple on water does.
        eased = 1.0 - (1.0 - progress) ** 3
        frames.append(_build_ring_frame(
            (_RING_RADIUS + (_PULSE_MAX_RADIUS - _RING_RADIUS) * eased) * scale,
            max(1.4, 4.0 * (1.0 - 0.55 * eased)) * scale,
            0.9 * (1.0 - progress) ** 1.35,
            box=box,
            base=glow,
        ))
    return frames, box, box // 2


def _create_dib(pixels: bytes, width: int, height: int):
    """A top-down 32-bit DIB section holding these premultiplied pixels."""
    user32 = ctypes.windll.user32
    gdi32 = ctypes.windll.gdi32
    header = _BITMAPINFOHEADER()
    header.biSize = ctypes.sizeof(_BITMAPINFOHEADER)
    header.biWidth = width
    # Negative height makes it top-down, matching the row order of the pixels.
    header.biHeight = -height
    header.biPlanes = 1
    header.biBitCount = 32
    header.biCompression = _BI_RGB
    bits = ctypes.c_void_p()
    screen_dc = user32.GetDC(None)
    try:
        bitmap = gdi32.CreateDIBSection(
            screen_dc, ctypes.byref(header), _DIB_RGB_COLORS,
            ctypes.byref(bits), None, 0,
        )
    finally:
        user32.ReleaseDC(None, screen_dc)
    if not bitmap or not bits:
        if bitmap:
            gdi32.DeleteObject(bitmap)
        return None
    ctypes.memmove(bits, bytes(pixels), len(pixels))
    return bitmap


class _Overlay:
    """Click-through, per-pixel-alpha windows for one monitor.

    Up to three: the edge glow covering the monitor; the banner, in a window
    of its own so it stays solid while the glow breathes; and, for the overlay
    that owns it, the cursor glow.
    """

    def __init__(self, *, cursor_indicator: bool = True) -> None:
        self._hwnd = None
        self._wndclass = None
        self._wndproc_ref = None  # must outlive the window
        self._bounds = (0, 0, 0, 0)
        self._scale = 1.0
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
        self._pulse_started = time.monotonic()
        # How visible the whole indicator is, 0..1, and the fade moving it.
        # While `_hiding`, a fade-out is still on screen though the overlay
        # already counts as hidden.
        self._fade = 0.0
        self._fade_from = 0.0
        self._fade_to = 0.0
        self._fade_started = 0.0
        self._fade_duration = 0.0
        self._hiding = False
        # The banner pill, a small window of its own.
        self._banner_hwnd = None
        self._banner_hdc = None
        self._banner_bitmap = None
        self._banner_old_bitmap = None
        self._banner_frame = (0, 0, 0)   # width, height, pill offset from top
        self._banner_scale = None
        # Cursor glow — another window, owned by the same thread for the same
        # reason the first one is: cross-thread window calls deadlock.
        self._ring_hwnd = None
        self._ring_hdc = None
        self._ring_bitmaps: list = []
        self._ring_old_bitmap = None
        self._ring_shown = False
        self._ring_frame = 0          # 0 = idle glow, 1..N = click pulse
        self._ring_last_pos = None
        self._ring_cursor_handle = None
        self._ring_box = RING_BOX
        self._ring_scale = None

    # ── window plumbing ──────────────────────────────────────────────

    def _create_layered_window(self, title: str, width: int, height: int):
        return ctypes.windll.user32.CreateWindowExW(
            _WS_EX_LAYERED | _WS_EX_TRANSPARENT | _WS_EX_TOPMOST
            | _WS_EX_TOOLWINDOW | _WS_EX_NOACTIVATE,
            "LumiComputerUseHalo", title,
            _WS_POPUP,
            0, 0, width, height,
            None, None, ctypes.windll.kernel32.GetModuleHandleW(None), None,
        )

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
        wndclass.lpszClassName = "LumiComputerUseHalo"
        if not user32.RegisterClassW(ctypes.byref(wndclass)):
            # 1410 is ERROR_CLASS_ALREADY_EXISTS, which is fine on a re-show.
            if ctypes.get_last_error() not in (0, 1410):
                return False
        self._wndclass = wndclass

        self._hwnd = self._create_layered_window("Lumi", 10, 10)
        if self._hwnd:
            # Without a window of its own the banner is simply skipped; the
            # glow still announces the run.
            self._banner_hwnd = self._create_layered_window("Lumi banner", 10, 10)
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

    def _release_banner(self) -> None:
        gdi32 = ctypes.windll.gdi32
        if self._banner_hdc:
            if self._banner_old_bitmap:
                gdi32.SelectObject(self._banner_hdc, self._banner_old_bitmap)
            gdi32.DeleteDC(self._banner_hdc)
        if self._banner_bitmap:
            gdi32.DeleteObject(self._banner_bitmap)
        self._banner_hdc = self._banner_bitmap = self._banner_old_bitmap = None

    def _release_ring(self) -> None:
        """Free the ring's DC and its pre-rendered frames.

        Thirteen small DIB sections is not much, but GDI objects are a
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

    def _selected_dc(self, bitmap):
        """A memory DC with `bitmap` selected, and the bitmap it displaced."""
        user32 = ctypes.windll.user32
        gdi32 = ctypes.windll.gdi32
        screen_dc = user32.GetDC(None)
        try:
            hdc = gdi32.CreateCompatibleDC(screen_dc)
        finally:
            user32.ReleaseDC(None, screen_dc)
        if not hdc:
            return None, None
        return hdc, gdi32.SelectObject(hdc, bitmap)

    def _build_surface(self, width: int, height: int) -> bool:
        """Render the edge glow once into a reusable DIB."""
        self._release_surface()
        hbitmap = _create_dib(_build_glow_rows(width, height, self._scale), width, height)
        if not hbitmap:
            return False
        hdc_mem, old = self._selected_dc(hbitmap)
        if not hdc_mem:
            ctypes.windll.gdi32.DeleteObject(hbitmap)
            return False
        self._hdc_mem = hdc_mem
        self._hbitmap = hbitmap
        self._old_bitmap = old
        return True

    def _build_banner_surface(self) -> bool:
        """Render the banner for the current scale into its own DIB."""
        self._release_banner()
        # Recorded even on failure, so a broken font is not retried per show.
        self._banner_scale = self._scale
        pixels, width, height, pill_top = _build_banner_frame(self._scale)
        bitmap = _create_dib(pixels, width, height)
        if not bitmap:
            return False
        hdc, old = self._selected_dc(bitmap)
        if not hdc:
            ctypes.windll.gdi32.DeleteObject(bitmap)
            return False
        self._banner_hdc = hdc
        self._banner_bitmap = bitmap
        self._banner_old_bitmap = old
        self._banner_frame = (width, height, pill_top)
        return True

    def _composite(self, intensity: float) -> None:
        """Push the glow surface to the screen at the given opacity."""
        if not (self._hwnd and self._hdc_mem):
            return
        user32 = ctypes.windll.user32
        x, y, width, height = self._bounds

        blend = _BLENDFUNCTION(_AC_SRC_OVER, 0, _alpha_byte(intensity), _AC_SRC_ALPHA)
        size = wintypes.SIZE(width, height)
        source = wintypes.POINT(0, 0)
        dest = wintypes.POINT(x, y)
        user32.UpdateLayeredWindow(
            self._hwnd, None,
            ctypes.byref(dest), ctypes.byref(size),
            self._hdc_mem, ctypes.byref(source),
            0, ctypes.byref(blend), _ULW_ALPHA,
        )

    def _composite_level(self, intensity: float) -> None:
        """Change only the glow's overall opacity, keeping its pixels.

        With no source DC the compositor reuses the bitmap it already holds,
        so the breathing no longer copies a whole monitor's worth of pixels on
        every pulse frame.
        """
        if not (self._hwnd and self._hdc_mem):
            return
        blend = _BLENDFUNCTION(_AC_SRC_OVER, 0, _alpha_byte(intensity), _AC_SRC_ALPHA)
        if not ctypes.windll.user32.UpdateLayeredWindow(
            self._hwnd, None, None, None, None, None,
            0, ctypes.byref(blend), _ULW_ALPHA,
        ):
            self._composite(intensity)

    def _composite_banner(self) -> None:
        """Place the banner on its monitor at the current fade."""
        if not (self._banner_hwnd and self._banner_hdc):
            return
        width, height, pill_top = self._banner_frame
        x, y, monitor_width, _ = self._bounds
        left, top = _banner_origin(monitor_width, width, pill_top, self._scale)
        # It drops into place as it fades in, and lifts away as it fades out.
        slide = round((1.0 - self._fade) * _BANNER_SLIDE_PX * self._scale)
        blend = _BLENDFUNCTION(_AC_SRC_OVER, 0, _alpha_byte(self._fade), _AC_SRC_ALPHA)
        size = wintypes.SIZE(width, height)
        source = wintypes.POINT(0, 0)
        dest = wintypes.POINT(x + left, y + top - slide)
        ctypes.windll.user32.UpdateLayeredWindow(
            self._banner_hwnd, None,
            ctypes.byref(dest), ctypes.byref(size),
            self._banner_hdc, ctypes.byref(source),
            0, ctypes.byref(blend), _ULW_ALPHA,
        )

    def _raise(self, hwnd) -> None:
        # Re-assert topmost: another window going full-screen can push it
        # down, and a glow behind the app it describes is useless. Each call
        # also puts this window above the previous one, which is how the
        # banner stays over the glow and the cursor over both.
        ctypes.windll.user32.SetWindowPos(
            hwnd, _HWND_TOPMOST, 0, 0, 0, 0,
            _SWP_NOMOVE | _SWP_NOSIZE | _SWP_NOACTIVATE,
        )

    # ── fading and breathing ─────────────────────────────────────────

    def _pulse(self, now: float) -> float:
        """The glow's breathing: a sine eased into [_PULSE_MIN, _PULSE_MAX]."""
        phase = (now - self._pulse_started) / _PULSE_PERIOD_S
        wave = (math.sin(phase * 2 * math.pi) + 1.0) / 2.0
        return _PULSE_MIN + (_PULSE_MAX - _PULSE_MIN) * wave

    def _start_fade(self, target: float, duration: float, now: float) -> None:
        self._fade_from = self._fade
        self._fade_to = target
        self._fade_started = now
        self._fade_duration = duration

    def _set_fade(self, value: float) -> None:
        self._fade = self._fade_from = self._fade_to = value
        self._fade_duration = 0.0

    def _advance_fade(self, now: float) -> bool:
        """Step a fade in progress; True on every tick that changed the level."""
        if self._fade_duration <= 0.0:
            return False
        progress = (now - self._fade_started) / self._fade_duration
        if progress >= 1.0:
            self._set_fade(self._fade_to)
            return True
        eased = 1.0 - (1.0 - progress) ** 3
        self._fade = self._fade_from + (self._fade_to - self._fade_from) * eased
        return True

    # ── owner thread ─────────────────────────────────────────────────
    #
    # Every Win32 call touching the window happens on the single thread that
    # created it. This is not tidiness — `ShowWindow` on a window owned by
    # another thread posts to the owner's message queue and BLOCKS until the
    # owner pumps it. Lumi's desktop tools run on worker threads and the
    # linger timer fires on yet another, so a cross-thread hide deadlocked
    # both: the caller waited on a thread that was itself asleep.
    #
    # The loop polls a command queue and repaints the pulse, so it needs no
    # message-based marshalling and the pulse tick comes free.

    def _worker(self) -> None:
        # The loop runs at cursor-tracking speed; the glow's opacity changes
        # only every few ticks, since nobody perceives a slow pulse any faster,
        # while the ring genuinely needs the rate to not visibly lag the
        # pointer it is drawing around. Fades run at the full rate.
        frame = 1.0 / _RING_FPS
        glow_every = max(1, round(_RING_FPS / _PULSE_FPS))
        tick = 0
        try:
            if not self._ensure_window():
                self._ready.set()
                return
            self._ready.set()
            while not self._shutdown.is_set():
                self._drain_commands()
                self._pump_messages()
                now = time.monotonic()
                fading = self._advance_fade(now)
                self._tick_ring(force=fading)
                if self._shown or self._hiding:
                    if fading or tick % glow_every == 0:
                        self._composite_level(self._pulse(now) * self._fade)
                    if fading:
                        self._composite_banner()
                if self._hiding and self._fade_duration <= 0.0:
                    self._hide_windows()
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
                    self._apply_hide(immediate=True)
            except Exception:
                logger.debug("Halo overlay command failed", exc_info=True)
            finally:
                if done is not None:
                    done.set()

    def _apply_show(
        self, x: int, y: int, width: int, height: int, fade_in: bool = True
    ) -> None:
        if self._bounds != (x, y, width, height) or not self._hbitmap:
            self._bounds = (x, y, width, height)
            self._scale = _display_scale(x, y, width, height)
            if not self._build_surface(width, height):
                return
        if self._banner_hwnd and self._banner_scale != self._scale:
            try:
                self._build_banner_surface()
            except Exception:
                logger.debug("Banner rendering failed", exc_info=True)
        now = time.monotonic()
        if fade_in and not self._shown:
            # From wherever it is: nothing, or a fade-out being reversed.
            self._start_fade(1.0, _FADE_IN_S, now)
        else:
            self._set_fade(1.0)
        self._hiding = False
        user32 = ctypes.windll.user32
        # At the pulse's current level, so a restore after a screenshot does
        # not flash the glow to full before the next pulse tick.
        self._composite(self._pulse(now) * self._fade)
        user32.ShowWindow(self._hwnd, _SW_SHOWNOACTIVATE)
        self._raise(self._hwnd)
        if self._banner_hdc:
            self._composite_banner()
            user32.ShowWindow(self._banner_hwnd, _SW_SHOWNOACTIVATE)
            self._raise(self._banner_hwnd)
        self._shown = True

    def _apply_hide(self, immediate: bool = False) -> None:
        """Fade the indicator out, or with `immediate` take it down now."""
        if not immediate and (self._shown or self._hiding):
            if self._shown:
                self._shown = False
                self._hiding = True
                self._start_fade(0.0, _FADE_OUT_S, time.monotonic())
            return
        self._hide_windows()

    def _hide_windows(self) -> None:
        user32 = ctypes.windll.user32
        for hwnd in (self._hwnd, self._banner_hwnd):
            if hwnd:
                user32.ShowWindow(hwnd, _SW_HIDE)
        self._shown = False
        self._hiding = False
        self._set_fade(0.0)
        self._apply_ring_hide()

    # ── cursor glow ──────────────────────────────────────────────────

    def _ensure_ring(self) -> bool:
        if self._ring_hwnd:
            return bool(self._ring_bitmaps) or self._rebuild_ring_frames()
        user32 = ctypes.windll.user32
        gdi32 = ctypes.windll.gdi32

        self._ring_hwnd = self._create_layered_window("Lumi cursor", RING_BOX, RING_BOX)
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
        if (handle == self._ring_cursor_handle and self._ring_bitmaps
                and self._ring_scale == self._scale):
            return True

        frames, box, _anchor = _build_cursor_frames(
            mask, width, height, hotspot_x, hotspot_y, self._scale
        )
        gdi32 = ctypes.windll.gdi32
        new_bitmaps = []
        for pixels in frames:
            bitmap = _create_dib(pixels, box, box)
            if not bitmap:
                for pending in new_bitmaps:
                    gdi32.DeleteObject(pending)
                return False
            new_bitmaps.append(bitmap)

        # Put the stock bitmap back before deleting any currently selected
        # frame. This is mandatory when a link/resize cursor appears mid-run.
        if self._ring_old_bitmap:
            gdi32.SelectObject(self._ring_hdc, self._ring_old_bitmap)
        for bitmap in self._ring_bitmaps:
            gdi32.DeleteObject(bitmap)
        self._ring_bitmaps = new_bitmaps
        self._ring_box = box
        self._ring_scale = self._scale
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

        box = self._ring_box
        blend = _BLENDFUNCTION(_AC_SRC_OVER, 0, _alpha_byte(self._fade), _AC_SRC_ALPHA)
        size = wintypes.SIZE(box, box)
        source = wintypes.POINT(0, 0)
        dest = wintypes.POINT(x - box // 2, y - box // 2)
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
        # The monitor being driven may have changed, and its scale with it.
        if (cursor_handle != self._ring_cursor_handle
                or self._ring_scale != self._scale):
            if not self._rebuild_ring_frames(cursor_handle):
                return
        user32 = ctypes.windll.user32
        self._composite_ring(cursor_x, cursor_y)
        user32.ShowWindow(self._ring_hwnd, _SW_SHOWNOACTIVATE)
        self._raise(self._ring_hwnd)
        self._ring_shown = True

    def _apply_ring_hide(self) -> None:
        if self._ring_hwnd:
            ctypes.windll.user32.ShowWindow(self._ring_hwnd, _SW_HIDE)
        self._ring_shown = False
        self._ring_frame = 0

    def _tick_ring(self, force: bool = False) -> None:
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
        # or fade in flight costs nothing, which matters because this runs at
        # 60 Hz for as long as the agent is working.
        if position == self._ring_last_pos and not animating and not force:
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
                target=self._worker, name="lumi-halo", daemon=True
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

    def show(
        self, x: int, y: int, width: int, height: int, fade_in: bool = True
    ) -> bool:
        # Wait until the border is genuinely on screen before returning control
        # to the desktop action.  Besides being the honest indicator timing,
        # this lets a second monitor claim a second window instead of racing a
        # still-queued first show and moving that same window away.
        return self._submit(
            "show", (x, y, width, height, fade_in), wait=True
        ) is not None

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
    """Take the glow down promptly, cancelling any pending linger."""
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
    in them would feed Lumi's own edge lighting and banner back to the
    model as if it were part of the application on screen — and the banner
    sits exactly where a window's title bar or toolbar usually is.

    Restores only if it was visible to begin with, so a capture never turns
    the glow on. Both the hide and the restore are immediate: a fade either
    side of every screenshot would make the glow blink through a run.
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
                    overlay.show(*bounds, fade_in=False)
                # Re-arm the linger. The pending timer can fire during the
                # capture — while the glow is already hidden, so its hide is a
                # no-op — and the restore would then bring the glow back with
                # nothing left to take it down again, stranding it on screen
                # for the rest of the session.
                _rearm_linger()
            except Exception:
                logger.debug("Halo overlay failed to restore", exc_info=True)
