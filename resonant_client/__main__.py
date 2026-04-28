"""Entry point for the Resonant Client."""
import sys

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
