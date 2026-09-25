# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec for Lumi (Windows installer build; macOS app bundle).

Build:    pyinstaller packaging/lumi.spec --clean --noconfirm
Output:   dist/lumi/lumi.exe (one-folder bundle); dist/Lumi.app on macOS

Mode choices:
- One-folder (this spec): faster startup, easier debugging, the Inno Setup
  installer wraps the whole `dist/lumi/` directory anyway so the user
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

import json
import os
import re
import sys
from pathlib import Path
from PyInstaller.utils.hooks import collect_data_files, collect_submodules

block_cipher = None

# ---- Project layout ----------------------------------------------------------

# The .spec runs from the repo root when invoked as `pyinstaller packaging/lumi.spec`.
PROJECT_ROOT = Path.cwd()
PKG_ROOT = PROJECT_ROOT / "lumi"

# ---- Data files to bundle ----------------------------------------------------
# PyInstaller doesn't auto-detect Jinja templates or static assets; list them
# explicitly. Source path → destination path inside the bundle.

datas = [
    # Jinja templates served by the GUI
    (str(PKG_ROOT / "gui" / "templates" / "index.html"),
     "lumi/gui/templates"),

    # Static frontend assets (JS, CSS, icons)
    (str(PKG_ROOT / "gui" / "static" / "app.js"),
     "lumi/gui/static"),
    (str(PKG_ROOT / "gui" / "static" / "plan_graph_view.js"),
     "lumi/gui/static"),
    (str(PKG_ROOT / "gui" / "static" / "autonomous_view.js"),
     "lumi/gui/static"),
    (str(PKG_ROOT / "gui" / "static" / "settings_view.js"),
     "lumi/gui/static"),
    (str(PKG_ROOT / "gui" / "static" / "run_cards.js"),
     "lumi/gui/static"),
    (str(PKG_ROOT / "gui" / "static" / "employee_tasks.js"),
     "lumi/gui/static"),
    (str(PKG_ROOT / "gui" / "static" / "local_access.js"),
     "lumi/gui/static"),
    (str(PKG_ROOT / "gui" / "static" / "appearance.js"),
     "lumi/gui/static"),
    (str(PKG_ROOT / "gui" / "static" / "fonts.css"),
     "lumi/gui/static"),
    (str(PKG_ROOT / "gui" / "static" / "styles.css"),
     "lumi/gui/static"),
    (str(PKG_ROOT / "gui" / "static" / "favicon.svg"),
     "lumi/gui/static"),
    (str(PKG_ROOT / "gui" / "static" / "lumi.ico"),
     "lumi/gui/static"),
    (str(PKG_ROOT / "gui" / "static" / "lumi.png"),
     "lumi/gui/static"),
    (str(PKG_ROOT / "gui" / "static" / "lumi-macos.png"),
     "lumi/gui/static"),
    (str(PKG_ROOT / "gui" / "static" / "lumi-app-icon.svg"),
     "lumi/gui/static"),
    (str(PKG_ROOT / "gui" / "static" / "lumi-mark.svg"),
     "lumi/gui/static"),
    (str(PKG_ROOT / "gui" / "static" / "lumi-wordmark.svg"),
     "lumi/gui/static"),

    # Unpacked Chrome extension backing the browser tools' tab grouping.
    # Chrome 137+ ignores --load-extension, so it is installed at runtime via
    # the Extensions CDP domain; either way the files have to be in the bundle.
    (str(PKG_ROOT / "browser_extension" / "manifest.json"),
     "lumi/browser_extension"),
    (str(PKG_ROOT / "browser_extension" / "background.js"),
     "lumi/browser_extension"),
]

# The VS Code extension, packed into a .vsix at install time (lumi/code_editors).
for name in ("package.json", "extension.js", "bridge.js", "README.md", "LICENSE.txt"):
    datas.append((str(PKG_ROOT / "code_editors" / "vscode" / name), "lumi/code_editors/vscode"))

# Include data files for libraries that ship their own (jinja2 has none, but
# starlette ships some HTML defaults for error pages).
datas += collect_data_files("starlette")

# Skill instructions are package data, not Python modules. Include them
# explicitly so installed clients can discover the same skills as source runs.
for skill in sorted((PKG_ROOT / "orchestration" / "bundled_skills").glob("*.md")):
    datas.append((str(skill), "lumi/orchestration/bundled_skills"))

# Frontend libraries and fonts that index.html used to load from CDNs. Fetched
# and SHA-256 verified by packaging/fetch_web_assets.ps1, which build_clean.ps1
# runs before PyInstaller. Bundling them takes five render-blocking network
# round trips off every launch and makes startup work offline.
VENDOR_DIR = PKG_ROOT / "gui" / "static" / "vendor"
for vendored in sorted(VENDOR_DIR.glob("*")) if VENDOR_DIR.is_dir() else []:
    if vendored.is_file():
        datas.append((str(vendored), "lumi/gui/static/vendor"))

# ---- Hidden imports ----------------------------------------------------------
# Modules dynamically imported (string-based) that PyInstaller's static
# analysis misses.

hiddenimports = [
    # Dispatched by __main__.py via string lookups
    "lumi.tui",
    "lumi.gui.server",
    "lumi.gui.app",

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

    # Corporate TLS and the OS credential store (lumi/net.py,
    # lumi/secrets_store.py) are imported lazily. PyInstaller's keyring hook
    # adds the backends and the entry-point metadata keyring uses to find
    # them; bundle-policy.json fails the build if that metadata is missing,
    # because without it keys silently stay in settings.json.
    "truststore",
    "keyring",
]

# pywebview's platform backend: Edge through pythonnet/WinForms on Windows,
# WebKit through PyObjC on macOS.
if sys.platform == "darwin":
    hiddenimports += ["webview.platforms.cocoa"]
else:
    hiddenimports += ["webview.platforms.edgechromium", "webview.platforms.winforms",
                      "clr", "clr_loader", "pythonnet"]

# Pull in all submodules of lumi itself so dynamic imports inside
# the engine (e.g. `importlib.import_module(f"lumi.engine.{tool}")`)
# resolve at runtime.
hiddenimports += collect_submodules("lumi")

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

# Installed dependencies the bundle must not contain, with the reason, from
# packaging/third-party-components.json (the notices leave them out too). Today
# these are PyAutoGUI's GPL-3.0 helpers, which it imports optionally and Lumi
# never calls.
excludes += [
    name for name in json.loads(
        (PROJECT_ROOT / "packaging" / "third-party-components.json").read_text(encoding="utf-8")
    )["not_shipped"]
    if not name.startswith("_")
]

# ---- Analysis ----------------------------------------------------------------

# ---- Native binaries ---------------------------------------------------------
# WinSparkle.dll for auto-update. Bundled next to lumi.exe so the ctypes
# loader in lumi/updater.py can find it via sys._MEIPASS.

WINSPARKLE_DLL = PROJECT_ROOT / "packaging" / "winsparkle" / "WinSparkle-0.9.2" / "x64" / "Release" / "WinSparkle.dll"

binaries = []
if sys.platform == "win32" and WINSPARKLE_DLL.exists():
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

# License texts of every third-party part of the bundle, generated from the
# build environment by packaging/third_party_notices.py; build_clean.ps1 passes
# the path. Like ripgrep, bundle-policy.json requires the file, so a build
# without it fails the policy gate.
NOTICES = os.environ.get("LUMI_THIRD_PARTY_NOTICES", "")
if NOTICES and Path(NOTICES).is_file():
    datas.append((NOTICES, "licenses"))

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
    name="lumi",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,                   # UPX compression often triggers AV; skip for now
    console=False,               # v0.2.2+: native desktop app, no cmd window
    # console=True,              # uncomment when debugging startup hangs (stderr to console)
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    # On macOS, packaging/build_macos.sh signs the finished Lumi.app when a
    # Developer ID is configured, so PyInstaller leaves signing alone.
    codesign_identity=None,
    entitlements_file=None,
    icon=str(PROJECT_ROOT / "packaging" / "macos" / "lumi.icns") if sys.platform == "darwin"
    else str(PKG_ROOT / "gui" / "static" / "lumi.ico"),
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
    name="lumi",
)

# ---- macOS app bundle ---------------------------------------------------------
#
# Built by packaging/build_macos.sh, which also makes the DMG and, when a
# Developer ID is configured, signs and notarizes it (docs/macos.md). The
# usage strings are what macOS shows when the agent asks for the microphone
# (dictation) or to automate other apps (computer use).
if sys.platform == "darwin":
    _version = re.search(r'__version__\s*=\s*"([^"]+)"',
                         (PKG_ROOT / "__init__.py").read_text(encoding="utf-8")).group(1)
    app = BUNDLE(
        coll,
        name="Lumi.app",
        icon=str(PROJECT_ROOT / "packaging" / "macos" / "lumi.icns"),
        bundle_identifier="com.luminaryanalytics.lumi",
        version=_version,
        info_plist={
            "CFBundleName": "Lumi",
            "CFBundleDisplayName": "Lumi",
            "CFBundleShortVersionString": _version,
            "CFBundleVersion": _version,
            "LSMinimumSystemVersion": "12.0",
            "NSHighResolutionCapable": True,
            "NSMicrophoneUsageDescription": "Lumi uses the microphone only while you dictate a message.",
            "NSAppleEventsUsageDescription": "Lumi controls other apps only when you let the agent use this computer.",
        },
    )
