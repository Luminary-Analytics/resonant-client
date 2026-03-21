"""Entry point for the Resonant Client."""
import sys


def main():
    # Check for GUI subcommand before parsing full args
    if len(sys.argv) > 1 and sys.argv[1] == "gui":
        # Strip "gui" from argv so the GUI's argparse works cleanly
        sys.argv = [sys.argv[0]] + sys.argv[2:]
        from .gui.server import main as gui_main
        gui_main()
    else:
        from .tui import main as tui_main
        tui_main()


if __name__ == "__main__":
    main()
