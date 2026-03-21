"""
Resonant Client GUI — Server Launcher

Starts the Starlette/uvicorn server and opens either a pywebview
native window or a browser tab for the GUI.
"""

import argparse
import logging
import os
import socket
import sys
import threading
import time

logger = logging.getLogger(__name__)


def _find_free_port() -> int:
    """Find a free TCP port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(('', 0))
        return s.getsockname()[1]


def _wait_for_server(host: str, port: int, timeout: float = 10.0):
    """Wait until the server is accepting connections."""
    start = time.time()
    while time.time() - start < timeout:
        try:
            with socket.create_connection((host, port), timeout=0.5):
                return True
        except (OSError, ConnectionRefusedError):
            time.sleep(0.1)
    return False


def launch_gui(
    host: str = "127.0.0.1",
    port: int = 0,
    debug: bool = False,
    browser: bool = False,
):
    """
    Launch the Resonant GUI application.

    Args:
        host: Host to bind the server to
        port: Port to bind to (0 = auto-pick free port)
        debug: Enable debug mode (DevTools in pywebview, verbose logging)
        browser: Open in browser instead of native window
    """
    try:
        import uvicorn
    except ImportError:
        print("Error: uvicorn not installed. Run: pip install uvicorn")
        sys.exit(1)

    if port == 0:
        port = _find_free_port()

    url = f"http://{host}:{port}"

    # Start uvicorn in a background thread
    config = uvicorn.Config(
        "resonant_client.gui.app:app",
        host=host,
        port=port,
        log_level="debug" if debug else "warning",
        access_log=debug,
    )
    server = uvicorn.Server(config)

    server_thread = threading.Thread(target=server.run, daemon=True)
    server_thread.start()

    # Wait for server to be ready
    if not _wait_for_server(host, port):
        print(f"Error: Server failed to start on {url}")
        sys.exit(1)

    print(f"  Resonant GUI running at {url}")

    if browser:
        # Open in default browser
        import webbrowser
        webbrowser.open(url)
        print("  Opened in browser. Press Ctrl+C to stop.")
        try:
            server_thread.join()
        except KeyboardInterrupt:
            print("\n  Stopped")
    else:
        # Try pywebview for native window
        try:
            import webview
            window = webview.create_window(
                "Resonant",
                url,
                width=1200,
                height=800,
                min_size=(800, 600),
                text_select=True,
            )
            # Share window reference with app.py for native folder dialogs
            from .app import _webview_window as _
            import resonant_client.gui.app as _gui_app
            _gui_app._webview_window = window
            webview.start(debug=debug)
        except ImportError:
            # Fallback to browser
            print("  pywebview not installed -- opening in browser")
            print("  For native window: pip install pywebview")
            import webbrowser
            webbrowser.open(url)
            try:
                server_thread.join()
            except KeyboardInterrupt:
                print("\n  Stopped")


def main():
    """CLI entry point for resonant-gui."""
    parser = argparse.ArgumentParser(
        description="Resonant Code Agent — Desktop GUI",
    )
    parser.add_argument("--host", type=str, default="127.0.0.1")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--debug", action="store_true")
    parser.add_argument("--browser", action="store_true",
                        help="Open in browser instead of native window")

    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.WARNING,
    )

    launch_gui(
        host=args.host,
        port=args.port,
        debug=args.debug,
        browser=args.browser,
    )


if __name__ == "__main__":
    main()
