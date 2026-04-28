"""Entry point for the Resonant Client."""
import os
import sys

# Bug #19 fix — frozen-no-console sys.std* shim.
#
# When PyInstaller's `console=False` bundles run, the OS doesn't attach a
# console to the process, so sys.stdout / sys.stderr / sys.stdin are None.
# Many libraries assume those are real file objects and crash early. The
# notable culprit is uvicorn's ColourizedFormatter calling sys.stderr.isatty()
# at logging-config time, which raises AttributeError before main() even runs.
#
# Replace any None std streams with /dev/null (NUL on Windows). This is a
# real file object that supports .isatty() (returns False), .write() (no-op
# from the app's perspective — writes to NUL), and .flush(). Unblocks
# uvicorn, click, prompt_toolkit, rich, and anything else that probes
# stdout/stderr at import time.
#
# Only fires when frozen + at least one stream is None — leaves dev runs
# (`python -m resonant_client`) untouched so output still hits the terminal.
if getattr(sys, "frozen", False) and (
    sys.stdout is None or sys.stderr is None or sys.stdin is None
):
    _devnull = open(os.devnull, "w", encoding="utf-8")
    if sys.stdout is None:
        sys.stdout = _devnull
    if sys.stderr is None:
        sys.stderr = _devnull
    if sys.stdin is None:
        sys.stdin = open(os.devnull, "r", encoding="utf-8")

# NOTE: absolute import (not `from . import`). When PyInstaller bundles
# __main__.py as the entry point, relative imports fail because the module
# runs as `__main__`, not `resonant_client.__main__`. Absolute import works
# in both `python -m resonant_client` and the frozen exe.
from resonant_client import __version__


def main():
    # Surface --version / -V before any other dispatch so it works without
    # loading the heavier TUI / GUI subsystems.
    if len(sys.argv) > 1 and sys.argv[1] in ("--version", "-V"):
        print(f"resonant-client {__version__}")
        return

    # Debug: dump the EdDSA public key baked into the binary. Useful when
    # diagnosing "improperly signed" update errors (verifies the key the
    # binary will check against matches the key used to sign updates).
    if len(sys.argv) > 1 and sys.argv[1] == "--print-pubkey":
        from resonant_client.updater import EDDSA_PUBLIC_KEY, APPCAST_URL
        print(f"EDDSA_PUBLIC_KEY={EDDSA_PUBLIC_KEY}")
        print(f"APPCAST_URL={APPCAST_URL}")
        return

    # Kick off the WinSparkle background updater. No-op on non-Windows or
    # when the DLL isn't bundled (dev runs from source). Fire-and-forget;
    # WinSparkle owns its own thread and surfaces a native dialog only when
    # a newer version is found in the appcast.
    try:
        from resonant_client.updater import init_updater
        init_updater()
    except Exception:
        # Updater failures must never block app startup.
        import logging
        logging.getLogger(__name__).exception("Updater init failed (non-fatal)")

    # Check for GUI subcommand before parsing full args
    if len(sys.argv) > 1 and sys.argv[1] == "gui":
        # Strip "gui" from argv so the GUI's argparse works cleanly
        sys.argv = [sys.argv[0]] + sys.argv[2:]
        # Absolute imports for PyInstaller compat — see comment at top of file.
        from resonant_client.gui.server import main as gui_main
        gui_main()
    else:
        from resonant_client.tui import main as tui_main
        tui_main()


if __name__ == "__main__":
    main()
