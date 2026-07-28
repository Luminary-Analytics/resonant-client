# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec for Resonant Client (Windows installer build).

Build:    pyinstaller packaging/resonant.spec --clean --noconfirm
Output:   dist/resonant/resonant.exe (one-folder bundle)

Mode choices:
- One-folder (this spec): faster startup, easier debugging, the Inno Setup
  installer wraps the whole `dist/resonant/` directory anyway so the user
  never sees the multi-file mess.
- Console=True for v0.x — keeps the terminal open so first-install
  Ollama-connection / port-bind issues are visible. Will flip to False once
  we have proper logging-to-file in v0.5+.

Bundled deps:
- Core (rich, prompt-toolkit, httpx, websockets) — auto-detected.
- GUI server (starlette, uvicorn, jinja2) — auto-detected, with a few
  hidden imports for uvicorn's worker discovery.
- Desktop tools (pyautogui, mss, Pillow) — bundled. They're optional in
  pyproject but the installed exe should include them so screenshot/click/
  type work out of the box.
- pywebview          — bundled (v0.2.2+). Provides the native desktop
                       frame so users see "the app", not a console + a
                       browser tab. Requires Microsoft Edge WebView2
                       runtime on Windows, which is pre-installed on
                       Windows 11 and Win10 1809+ (the vast majority).
- ripgrep (rg.exe)   — bundled (v0.11.9+). Backs the `grep` agent tool.
                       Without it a shipped install falls back to
                       `findstr`, whose regex dialect has no alternation,
                       no `+`, and no groups, so ordinary patterns match
                       nothing and the agent reads "(no matches)" as "not
                       in this codebase". Fetched and SHA-256 verified at
                       build time by packaging/fetch_ripgrep.ps1 rather
                       than committed, and required by bundle-policy.json
                       so a missing or non-running binary fails the build.

- Browser tools     — native, via the Chrome DevTools Protocol against the
                       user's installed Chrome (see engine/browser.py). CDP is
                       JSON-RPC over a WebSocket, so this needs only httpx and
                       websockets, both already bundled: zero installer cost.
                       A small unpacked extension ships alongside for tab
                       grouping, which CDP cannot do.

NOT bundled (runtime-optional):
- Playwright        — was used pre-v0.9.13 purely as a CDP client, which cost
                       150+ MB and a Chromium download for a protocol
                       implementation. Replaced, not deferred.
- opencv-python      — runtime-optional in engine/recording.py (wrapped
                       in try/except). Users who want screen recording
                       can `pip install opencv-python` themselves.
- uiautomation       — runtime-optional in engine/accessibility.py.
- pyperclip          — runtime-optional in engine/clipboard.py (has
                       OS-shell fallbacks).
"""

from pathlib import Path
from PyInstaller.utils.hooks import collect_data_files, collect_submodules

block_cipher = None

# ---- Project layout ----------------------------------------------------------

# The .spec runs from the repo root when invoked as `pyinstaller packaging/resonant.spec`.
PROJECT_ROOT = Path.cwd()
PKG_ROOT = PROJECT_ROOT / "resonant_client"

# ---- Data files to bundle ----------------------------------------------------
# PyInstaller doesn't auto-detect Jinja templates or static assets; list them
# explicitly. Source path → destination path inside the bundle.

datas = [
    # Jinja templates served by the GUI
    (str(PKG_ROOT / "gui" / "templates" / "index.html"),
     "resonant_client/gui/templates"),

    # Static frontend assets (JS, CSS, icons)
    (str(PKG_ROOT / "gui" / "static" / "app.js"),
     "resonant_client/gui/static"),
    (str(PKG_ROOT / "gui" / "static" / "plan_graph_view.js"),
     "resonant_client/gui/static"),
    (str(PKG_ROOT / "gui" / "static" / "autonomous_view.js"),
     "resonant_client/gui/static"),
    (str(PKG_ROOT / "gui" / "static" / "settings_view.js"),
     "resonant_client/gui/static"),
    (str(PKG_ROOT / "gui" / "static" / "run_cards.js"),
     "resonant_client/gui/static"),
    (str(PKG_ROOT / "gui" / "static" / "fonts.css"),
     "resonant_client/gui/static"),
    (str(PKG_ROOT / "gui" / "static" / "styles.css"),
     "resonant_client/gui/static"),
    (str(PKG_ROOT / "gui" / "static" / "favicon.svg"),
     "resonant_client/gui/static"),
    (str(PKG_ROOT / "gui" / "static" / "resonant.ico"),
     "resonant_client/gui/static"),
    (str(PKG_ROOT / "gui" / "static" / "resonant.png"),
     "resonant_client/gui/static"),

    # Unpacked Chrome extension backing the browser tools' tab grouping.
    # Chrome 137+ ignores --load-extension, so it is installed at runtime via
    # the Extensions CDP domain; either way the files have to be in the bundle.
    (str(PKG_ROOT / "browser_extension" / "manifest.json"),
     "resonant_client/browser_extension"),
    (str(PKG_ROOT / "browser_extension" / "background.js"),
     "resonant_client/browser_extension"),
]

# Include data files for libraries that ship their own (jinja2 has none, but
# starlette ships some HTML defaults for error pages).
datas += collect_data_files("starlette")

# Frontend libraries and fonts that index.html used to load from CDNs. Fetched
# and SHA-256 verified by packaging/fetch_web_assets.ps1, which build_clean.ps1
# runs before PyInstaller. Bundling them takes five render-blocking network
# round trips off every launch and makes startup work offline.
VENDOR_DIR = PKG_ROOT / "gui" / "static" / "vendor"
for vendored in sorted(VENDOR_DIR.glob("*")) if VENDOR_DIR.is_dir() else []:
    if vendored.is_file():
        datas.append((str(vendored), "resonant_client/gui/static/vendor"))

# ---- Hidden imports ----------------------------------------------------------
# Modules dynamically imported (string-based) that PyInstaller's static
# analysis misses.

hiddenimports = [
    # Dispatched by __main__.py via string lookups
    "resonant_client.tui",
    "resonant_client.gui.server",
    "resonant_client.gui.app",

    # uvicorn picks workers/protocols at runtime via importlib
    "uvicorn.logging",
    "uvicorn.loops",
    "uvicorn.loops.auto",
    "uvicorn.loops.asyncio",
    "uvicorn.protocols",
    "uvicorn.protocols.http",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.http.h11_impl",
    "uvicorn.protocols.websockets",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.protocols.websockets.websockets_impl",
    "uvicorn.lifespan",
    "uvicorn.lifespan.on",

    # websockets — bug #24 fix (v0.2.7+). Must be force-imported via
    # hidden imports because uvicorn's WebSocket protocol auto-discovery
    # uses runtime importlib lookups that PyInstaller's static analysis
    # misses. Without these, every WebSocket upgrade request fails with
    # "No supported WebSocket library detected" and the GUI hangs at
    # "Reconnecting...".
    "websockets",
    "websockets.legacy",
    "websockets.legacy.server",
    "websockets.legacy.client",
    "websockets.asyncio",
    "websockets.asyncio.server",
    "websockets.asyncio.client",
    "uvicorn.protocols.websockets.websockets_impl",
    "uvicorn.protocols.websockets.wsproto_impl",

    # Desktop tools (bundled — see header comment)
    "pyautogui",
    "mss",
    "PIL",
    "PIL.Image",
    "PIL.ImageGrab",

    # pywebview (bundled v0.2.2+) — native desktop frame.
    # webview is the import name; the package is "pywebview" on PyPI.
    "webview",
    "webview.platforms.edgechromium",
    "webview.platforms.winforms",
    # Windows-specific: pywebview uses pythonnet to host Edge/WinForms.
    "clr",
    "clr_loader",
    "pythonnet",
]

# Pull in all submodules of resonant_client itself so dynamic imports inside
# the engine (e.g. `importlib.import_module(f"resonant_client.engine.{tool}")`)
# resolve at runtime.
hiddenimports += collect_submodules("resonant_client")

# ---- Excludes ----------------------------------------------------------------
# Trim deadweight modules PyInstaller pulls in by default but we don't need.

excludes = [
    "tkinter",          # not used; saves ~10 MB
    "matplotlib",       # not a dep
    "numpy",            # only pulled by some optional cv2 paths we excluded
    "scipy",
    "pandas",
    "playwright",       # browser tools speak CDP directly; see engine/browser.py
    "cv2",              # runtime-optional, not bundled
    "uiautomation",     # runtime-optional, not bundled
]

# ---- Analysis ----------------------------------------------------------------

# ---- Native binaries ---------------------------------------------------------
# WinSparkle.dll for auto-update. Bundled next to resonant.exe so the ctypes
# loader in resonant_client/updater.py can find it via sys._MEIPASS.

WINSPARKLE_DLL = PROJECT_ROOT / "packaging" / "winsparkle" / "WinSparkle-0.9.2" / "x64" / "Release" / "WinSparkle.dll"

binaries = []
if WINSPARKLE_DLL.exists():
    # ('source', 'destination_dir_in_bundle')  — empty dest means top of bundle.
    binaries.append((str(WINSPARKLE_DLL), "."))

# ripgrep for the `grep` agent tool. Without it a shipped install falls back to
# `findstr`, whose regex dialect has no alternation, no `+`, and no groups — so
# ordinary patterns match nothing and the agent reads "(no matches)" as "not in
# this codebase". Fetched and SHA-256 verified by packaging/fetch_ripgrep.ps1,
# which build_clean.ps1 runs before PyInstaller.
#
# Deliberately not `if exists` — see packaging/bundle-policy.json. A missing
# search binary is the exact class of bug that shipped for months in the psutil
# case: present for developers, absent for users, silent either way. The policy
# gate fails the build instead.
RIPGREP_EXE = PROJECT_ROOT / "packaging" / "ripgrep" / "rg.exe"
if RIPGREP_EXE.exists():
    binaries.append((str(RIPGREP_EXE), "."))

    # ripgrep is dual-licensed MIT / Unlicense; redistributing the binary means
    # shipping its license text.
    for license_name in ("COPYING", "LICENSE-MIT", "UNLICENSE"):
        license_path = RIPGREP_EXE.parent / license_name
        if license_path.exists():
            datas.append((str(license_path), "licenses/ripgrep"))

a = Analysis(
    [str(PKG_ROOT / "__main__.py")],
    pathex=[str(PROJECT_ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

# ---- Executable --------------------------------------------------------------

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="resonant",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,                   # UPX compression often triggers AV; skip for now
    console=False,               # v0.2.2+: native desktop app, no cmd window
    # console=True,              # uncomment when debugging startup hangs (stderr to console)
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(PKG_ROOT / "gui" / "static" / "resonant.ico"),
)

# ---- Collect (one-folder bundle) --------------------------------------------

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="resonant",
)
