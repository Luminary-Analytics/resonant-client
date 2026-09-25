"""
WinSparkle auto-updater — ctypes wrapper around WinSparkle.dll.

Architecture
------------
- The bundled WinSparkle.dll lives next to the .exe in the PyInstaller
  one-folder layout (or under sys._MEIPASS in one-file mode).
- We call WinSparkle's C API via ctypes — no external Python dep.
- EdDSA public key is hard-coded in this module. The matching private
  key lives at `~/.lumi/keys/eddsa_priv.key` on the developer's
  machine and never enters the repo.
- Appcast URL points at the GitHub Pages site, which also hosts the
  installers. The source repository itself may be private.
- WinSparkle runs its own background thread for periodic checks; we
  fire-and-forget the init.

- Which feed, and whether WinSparkle checks at all, comes from the update
  mode, channel and pin (lumi/update_channels.py): Settings > Updates or the
  organization's policy, read once at startup.

Usage
-----
    from lumi.updater import init_updater, check_for_updates_now

    init_updater()                # called once at startup (safe no-op if DLL missing)
    check_for_updates_now()       # menu / button trigger for explicit check

If WinSparkle.dll isn't present, the copy runs from source (unless
LUMI_UPDATER_FROM_SOURCE=1), or it isn't Windows, every function becomes a
no-op. The app still works, just without auto-update.
"""

from __future__ import annotations

import ctypes
import logging
import os
import sys
from pathlib import Path

from lumi import __version__
from lumi.update_channels import UpdatePreferences, read as read_update_preferences

logger = logging.getLogger(__name__)

# ---- Constants ---------------------------------------------------------------

# Generated 2026-04-28 with WinSparkle 0.9.2 winsparkle-tool generate-key.
# The matching private key lives at ~/.lumi/keys/eddsa_priv.key on the
# developer's machine and is used by the release CI to sign every installer.
# Rotating this key requires a coordinated push: new pubkey here + new privkey
# in CI secret + signed first-update at the new key.
EDDSA_PUBLIC_KEY = "HgNb0s7xavpa1bFyX/8B24AnuUdgekpvgO6HQU+zv8k="

# Still the pre-rebrand Pages address on purpose: every installed SONN Client
# polls it, so the first Lumi releases must be published here. Moving the feed
# to a Lumi domain takes a bridge release whose binary points at the new URL,
# published here first. Rename the repository only after that (GitHub does not
# redirect Pages project sites after a rename). This is the stable feed; the
# beta channel and pinned release lines have their own (lumi/update_channels.py).
APPCAST_URL = "https://luminary-analytics.github.io/resonant-client/appcast.xml"
COMPANY_NAME = "Luminary Analytics"
APP_NAME = "Lumi"

# WinSparkle stores user prefs (last-checked time, "skip this version", etc.)
# under HKCU\Software\<COMPANY>\<APP>\WinSparkle. Explicit registry path keeps
# it predictable across upgrades.
REGISTRY_PATH = r"Software\Luminary Analytics\Lumi\WinSparkle"

# How often WinSparkle's background thread polls the appcast (in seconds).
# 24h is the default and the right answer — silent, low-noise, fresh enough.
UPDATE_CHECK_INTERVAL_SEC = 24 * 60 * 60

# ---- DLL loading -------------------------------------------------------------

_dll: ctypes.CDLL | None = None
_initialized = False
# The update settings applied at startup; a change in Settings waits for a restart.
_preferences: UpdatePreferences | None = None


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
    # A copy running from source can't update itself (the installer replaces
    # Program Files, not the checkout), and WinSparkle would still write the
    # real HKCU key and could show dialogs during tests and fixtures.
    if not getattr(sys, "frozen", False) and os.environ.get("LUMI_UPDATER_FROM_SOURCE") != "1":
        logger.debug("Running from source; the updater stays off (LUMI_UPDATER_FROM_SOURCE=1 tries it)")
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

    dll.win_sparkle_get_last_check_time.argtypes = []
    dll.win_sparkle_get_last_check_time.restype = ctypes.c_int64  # time_t, -1 before the first check

    return dll


# ---- Public API --------------------------------------------------------------


def init_updater(preferences: UpdatePreferences | None = None) -> bool:
    """
    Initialize WinSparkle and start its background update-check thread.

    Safe to call multiple times — idempotent. Safe on non-Windows or when
    the DLL isn't bundled — becomes a no-op. With the update mode ``off``
    WinSparkle isn't loaded at all, so nothing checks or prompts.

    Returns True if WinSparkle is now active, False if disabled/unavailable.
    """
    global _dll, _initialized, _preferences

    if _initialized:
        return _dll is not None

    _initialized = True
    try:
        _preferences = preferences or read_update_preferences()
    except Exception:
        logger.exception("Couldn't read the update settings; checking only when asked")
        _preferences = UpdatePreferences(mode="manual")
    if _preferences.mode == "off":
        logger.info("Updates are off%s", f" (managed by {_preferences.managed_by})" if _preferences.managed_by else "")
        return False
    _dll = _load_dll()
    if _dll is None:
        return False

    try:
        # Order matters: appcast URL + pubkey + app details MUST be set before init().
        _dll.win_sparkle_set_appcast_url(_preferences.feed_url.encode("utf-8"))

        result = _dll.win_sparkle_set_eddsa_public_key(EDDSA_PUBLIC_KEY.encode("utf-8"))
        if result != 1:  # WinSparkle returns 1 on success, 0 on failure
            logger.error("Failed to set EdDSA public key")
            return False

        _dll.win_sparkle_set_app_details(COMPANY_NAME, APP_NAME, __version__)
        _dll.win_sparkle_set_registry_path(REGISTRY_PATH.encode("utf-8"))
        # Set every start, so the mode in Settings (or the policy) wins over
        # the checkbox in WinSparkle's own dialog.
        _dll.win_sparkle_set_automatic_check_for_updates(1 if _preferences.mode == "automatic" else 0)
        _dll.win_sparkle_set_update_check_interval(UPDATE_CHECK_INTERVAL_SEC)

        # Init kicks off the background thread.
        _dll.win_sparkle_init()

        logger.info(
            "WinSparkle initialized: appcast=%s mode=%s version=%s",
            _preferences.feed_url, _preferences.mode, __version__
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
        logger.debug("Update check requested but WinSparkle is unavailable or updates are off")
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


def status() -> dict:
    """What Settings shows: the update settings in effect and the last check.

    ``pending`` holds settings saved since startup, which apply after a
    restart; ``available`` is False when this copy can't update itself
    (running from source, not on Windows, or updates are off).
    """
    if not _initialized:
        init_updater()
    active = _preferences or UpdatePreferences()
    try:
        saved = read_update_preferences()
    except Exception:
        saved = active
    last_check = None
    if _dll is not None:
        try:
            value = int(_dll.win_sparkle_get_last_check_time())
            last_check = value if value > 0 else None
        except (OSError, AttributeError, ValueError):
            last_check = None
    # Only what changes behavior counts: with updates off, or a pin that
    # makes the channel moot, a saved change isn't waiting for anything.
    def effect(prefs: UpdatePreferences) -> tuple:
        return ("off",) if prefs.mode == "off" else (prefs.mode, prefs.feed_url)

    changed = effect(saved) != effect(active)
    return {**active.as_dict(), "version": __version__, "available": _dll is not None,
            "last_check": last_check, "pending": saved.as_dict() if changed else None}


def reset_for_tests() -> None:
    """Forget the startup state (tests only). From source WinSparkle never loads."""
    global _dll, _initialized, _preferences
    _dll, _initialized, _preferences = None, False, None


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
