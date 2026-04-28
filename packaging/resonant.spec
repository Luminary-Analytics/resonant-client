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
- Backend SDKs (anthropic, openai) — bundled. Cheap (pure Python) and
  removes a "go install this" UX wart.

NOT bundled (deferred):
- pywebview          — v0.x ships browser-only mode. WebView2 runtime
                       dependency is too painful for first install.
- playwright         — adds 150+ MB and a Chromium download. Will be a
                       one-click "Install browser tools" button in
                       Settings → Tools that runs `playwright install`
                       post-install.
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
    (str(PKG_ROOT / "gui" / "static" / "styles.css"),
     "resonant_client/gui/static"),
    (str(PKG_ROOT / "gui" / "static" / "favicon.svg"),
     "resonant_client/gui/static"),
    (str(PKG_ROOT / "gui" / "static" / "resonant.ico"),
     "resonant_client/gui/static"),
    (str(PKG_ROOT / "gui" / "static" / "resonant.png"),
     "resonant_client/gui/static"),
]

# Include data files for libraries that ship their own (jinja2 has none, but
# starlette ships some HTML defaults for error pages).
datas += collect_data_files("starlette")

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

    # websockets internals occasionally missed
    "websockets.legacy",
    "websockets.legacy.server",
    "websockets.legacy.client",

    # Backend SDKs imported lazily by engine/backends.py
    "anthropic",
    "openai",

    # Desktop tools (bundled — see header comment)
    "pyautogui",
    "mss",
    "PIL",
    "PIL.Image",
    "PIL.ImageGrab",
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
    "playwright",       # explicitly deferred (see header)
    "pywebview",        # explicitly deferred (see header)
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
    console=True,                # See header — keep terminal visible for v0.x
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
