"""
WinSparkle auto-updater — ctypes wrapper around WinSparkle.dll.

Architecture
------------
- The bundled WinSparkle.dll lives next to the .exe in the PyInstaller
  one-folder layout (or under sys._MEIPASS in one-file mode).
- We call WinSparkle's C API via ctypes — no external Python dep.
- EdDSA public key is hard-coded in this module. The matching private
  key lives at `~/.resonant/keys/eddsa_priv.key` on the developer's
  machine and never enters the repo.
- Appcast URL points at the GitHub Pages site for the public repo.
- WinSparkle runs its own background thread for periodic checks; we
  fire-and-forget the init.

Usage
-----
    from resonant_client.updater import init_updater, check_for_updates_now

    init_updater()                # called once at startup (safe no-op if DLL missing)
    check_for_updates_now()       # menu / button trigger for explicit check

If WinSparkle.dll isn't present (running from source on dev machines, or
on non-Windows), every function becomes a no-op. The app still works,
just without auto-update.
"""

from __future__ import annotations

import ctypes
import logging
import sys
from pathlib import Path

from resonant_client import __version__

logger = logging.getLogger(__name__)

# ---- Constants ---------------------------------------------------------------

# Generated 2026-04-28 with WinSparkle 0.9.2 winsparkle-tool generate-key.
# The matching private key lives at ~/.resonant/keys/eddsa_priv.key on the
# developer's machine and is used by the release CI to sign every installer.
# Rotating this key requires a coordinated push: new pubkey here + new privkey
# in CI secret + signed first-update at the new key.
EDDSA_PUBLIC_KEY = "HgNb0s7xavpa1bFyX/8B24AnuUdgekpvgO6HQU+zv8k="

APPCAST_URL = "https://luminary-analytics.github.io/resonant-client/appcast.xml"
COMPANY_NAME = "Luminary Analytics"
APP_NAME = "Resonant"

# WinSparkle stores user prefs (last-checked time, "skip this version", etc.)
# under HKCU\Software\<COMPANY>\<APP>\WinSparkle. Explicit registry path keeps
# it predictable across upgrades.
REGISTRY_PATH = r"Software\Luminary Analytics\Resonant Client\WinSparkle"

# How often WinSparkle's background thread polls the appcast (in seconds).
# 24h is the default and the right answer — silent, low-noise, fresh enough.
UPDATE_CHECK_INTERVAL_SEC = 24 * 60 * 60

# ---- DLL loading -------------------------------------------------------------

_dll: ctypes.CDLL | None = None
_initialized = False


def _find_dll() -> Path | None:
    """Locate WinSparkle.dll in the bundle / source layout."""
    # PyInstaller one-file mode: extracted to sys._MEIPASS.
    # PyInstaller one-folder mode: alongside the exe in _internal/.
    if hasattr(sys, "_MEIPASS"):
        candidates = [
            Path(sys._MEIPASS) / "WinSparkle.dll",
            Path(sys._MEIPASS) / "_internal" / "WinSparkle.dll",
        ]
    else:
        # Running from source: look for a vendored copy.
        repo_root = Path(__file__).resolve().parent.parent
        candidates = [
            repo_root / "packaging" / "winsparkle" / "WinSparkle-0.9.2" / "x64" / "Release" / "WinSparkle.dll",
            repo_root / "WinSparkle.dll",
        ]

    for candidate in candidates:
        if candidate.exists():
            return candidate
    return None


def _load_dll() -> ctypes.CDLL | None:
    """Load WinSparkle.dll and configure ctypes signatures. Returns None on failure."""
    if sys.platform != "win32":
        logger.debug("WinSparkle is Windows-only; updater disabled on %s", sys.platform)
        return None

    dll_path = _find_dll()
    if dll_path is None:
        logger.warning("WinSparkle.dll not found; auto-update disabled")
        return None

    try:
        dll = ctypes.CDLL(str(dll_path))
    except OSError as exc:
        logger.warning("Failed to load WinSparkle.dll: %s", exc)
        return None

    # Configure signatures so ctypes does the right type marshalling.
    # All char* are UTF-8; wchar_t* are UTF-16 (Windows native).
    dll.win_sparkle_init.argtypes = []
    dll.win_sparkle_init.restype = None

    dll.win_sparkle_cleanup.argtypes = []
    dll.win_sparkle_cleanup.restype = None

    dll.win_sparkle_set_appcast_url.argtypes = [ctypes.c_char_p]
    dll.win_sparkle_set_appcast_url.restype = None

    dll.win_sparkle_set_eddsa_public_key.argtypes = [ctypes.c_char_p]
    dll.win_sparkle_set_eddsa_public_key.restype = ctypes.c_int

    dll.win_sparkle_set_app_details.argtypes = [
        ctypes.c_wchar_p,  # company name
        ctypes.c_wchar_p,  # app name
        ctypes.c_wchar_p,  # version
    ]
    dll.win_sparkle_set_app_details.restype = None

    dll.win_sparkle_set_registry_path.argtypes = [ctypes.c_char_p]
    dll.win_sparkle_set_registry_path.restype = None

    dll.win_sparkle_set_automatic_check_for_updates.argtypes = [ctypes.c_int]
    dll.win_sparkle_set_automatic_check_for_updates.restype = None

    dll.win_sparkle_set_update_check_interval.argtypes = [ctypes.c_int]
    dll.win_sparkle_set_update_check_interval.restype = None

    dll.win_sparkle_check_update_with_ui.argtypes = []
    dll.win_sparkle_check_update_with_ui.restype = None

    dll.win_sparkle_check_update_without_ui.argtypes = []
    dll.win_sparkle_check_update_without_ui.restype = None

    return dll


# ---- Public API --------------------------------------------------------------


def init_updater() -> bool:
    """
    Initialize WinSparkle and start its background update-check thread.

    Safe to call multiple times — idempotent. Safe on non-Windows or when
    the DLL isn't bundled — becomes a no-op.

    Returns True if WinSparkle is now active, False if disabled/unavailable.
    """
    global _dll, _initialized

    if _initialized:
        return _dll is not None

    _initialized = True
    _dll = _load_dll()
    if _dll is None:
        return False

    try:
        # Order matters: appcast URL + pubkey + app details MUST be set before init().
        _dll.win_sparkle_set_appcast_url(APPCAST_URL.encode("utf-8"))

        result = _dll.win_sparkle_set_eddsa_public_key(EDDSA_PUBLIC_KEY.encode("utf-8"))
        if result != 1:  # WinSparkle returns 1 on success, 0 on failure
            logger.error("Failed to set EdDSA public key")
            return False

        _dll.win_sparkle_set_app_details(COMPANY_NAME, APP_NAME, __version__)
        _dll.win_sparkle_set_registry_path(REGISTRY_PATH.encode("utf-8"))
        _dll.win_sparkle_set_automatic_check_for_updates(1)
        _dll.win_sparkle_set_update_check_interval(UPDATE_CHECK_INTERVAL_SEC)

        # Init kicks off the background thread.
        _dll.win_sparkle_init()

        logger.info(
            "WinSparkle initialized: appcast=%s version=%s",
            APPCAST_URL, __version__
        )
        return True
    except (OSError, AttributeError) as exc:
        logger.error("WinSparkle init failed: %s", exc)
        return False


def check_for_updates_now(silent: bool = False) -> bool:
    """
    Trigger an immediate update check.

    silent=False (default): show the WinSparkle dialog regardless of result
        ("You're up to date" or "Update available"). Use for menu-driven
        "Check for updates" actions.
    silent=True: only show UI if an update is found. Use for automatic
        background re-checks.
    """
    if not _initialized:
        init_updater()
    if _dll is None:
        logger.debug("Update check requested but WinSparkle is unavailable")
        return False

    try:
        if silent:
            _dll.win_sparkle_check_update_without_ui()
        else:
            _dll.win_sparkle_check_update_with_ui()
        return True
    except OSError as exc:
        logger.error("Update check failed: %s", exc)
        return False


def cleanup_updater() -> None:
    """
    Stop WinSparkle's background thread cleanly. Call on app shutdown.

    Optional — if the process exits without this, WinSparkle will be torn
    down by the OS. But calling it is the polite move.
    """
    global _dll, _initialized

    if _dll is None:
        return

    try:
        _dll.win_sparkle_cleanup()
    except OSError:
        pass
    finally:
        _dll = None
        _initialized = False
