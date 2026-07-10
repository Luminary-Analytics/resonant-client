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

    def _run_in_browser():
        print(f"  Open in browser: {url}")
        print("  Press Ctrl+C to stop.")
        try:
            server_thread.join()
        except KeyboardInterrupt:
            print("\n  Stopped")

    if browser:
        _run_in_browser()
    else:
        # Try pywebview for native frameless window
        try:
            import webview
            if not hasattr(webview, 'create_window'):
                raise ImportError("webview module is not pywebview")

            class _WindowAPI:
                def __init__(self, win_ref):
                    self._win = win_ref
                    self._maximized = False

                def minimize(self):
                    if self._win[0]:
                        self._win[0].minimize()

                def toggle_maximize(self):
                    if not self._win[0]:
                        return False
                    if self._maximized:
                        self._win[0].restore()
                        self._maximized = False
                    else:
                        self._win[0].maximize()
                        self._maximized = True
                    return self._maximized

                def is_maximized(self):
                    return self._maximized

                def close(self):
                    if self._win[0]:
                        self._win[0].destroy()

                def resize_window(self, width: int, height: int, x=None, y=None):
                    if not self._win[0]:
                        return False
                    try:
                        width = max(800, int(width))
                        height = max(600, int(height))
                        move_to = (
                            int(x) if x is not None else None,
                            int(y) if y is not None else None,
                        )
                    except (TypeError, ValueError):
                        return False
                    try:
                        if move_to[0] is not None and move_to[1] is not None:
                            self._win[0].move(move_to[0], move_to[1])
                        from webview.window import FixPoint
                        self._win[0].resize(width, height, FixPoint.NORTH | FixPoint.WEST)
                        return True
                    except Exception:
                        logger.debug("Could not resize pywebview window", exc_info=True)
                        return False

                def move_window(self, x: int, y: int):
                    if not self._win[0]:
                        return False
                    try:
                        self._win[0].move(int(x), int(y))
                        return True
                    except (TypeError, ValueError):
                        return False
                    except Exception:
                        logger.debug("Could not move pywebview window", exc_info=True)
                        return False

            win_ref = [None]
            api = _WindowAPI(win_ref)

            # Resolve the Windows icon used after the native HWND exists.
            icon_dir = os.path.join(os.path.dirname(__file__), "static")
            ico_path = os.path.join(icon_dir, "resonant.ico")

            wv_kwargs = dict(
                title="Resonant",
                url=url,
                width=1200,
                height=800,
                min_size=(800, 600),
                resizable=True,
                text_select=True,
                frameless=True,
                # Whole-window drag off; pywebview moves the window only from .pywebview-drag-region
                # (see menubar title in index.html). -webkit-app-region is for other hosts only.
                easy_drag=False,
                js_api=api,
            )

            # Set Windows taskbar icon BEFORE creating the window
            if sys.platform == "win32" and ico_path and os.path.exists(ico_path):
                try:
                    import ctypes
                    ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
                        "resonant.client.app"
                    )
                except Exception:
                    pass

            window = webview.create_window(**wv_kwargs)
            win_ref[0] = window
            import resonant_client.gui.app as _gui_app
            _gui_app._webview_window = window

            def _set_icon_on_shown():
                """Set the window icon after the HWND exists."""
                if sys.platform != "win32":
                    return
                try:
                    import ctypes
                    from ctypes import wintypes

                    WM_SETICON = 0x0080
                    ICON_SMALL = 0
                    ICON_BIG = 1
                    IMAGE_ICON = 1
                    LR_LOADFROMFILE = 0x0010
                    LR_DEFAULTSIZE = 0x0040

                    user32 = ctypes.windll.user32
                    LoadImageW = user32.LoadImageW
                    LoadImageW.restype = wintypes.HANDLE

                    # Find our window by title
                    hwnd = user32.FindWindowW(None, "Resonant")
                    if not hwnd:
                        hwnd = user32.GetForegroundWindow()

                    ico = str(ico_path).replace("/", "\\")

                    h_big = LoadImageW(0, ico, IMAGE_ICON, 48, 48,
                                       LR_LOADFROMFILE | LR_DEFAULTSIZE)
                    h_small = LoadImageW(0, ico, IMAGE_ICON, 16, 16,
                                         LR_LOADFROMFILE)

                    if h_big:
                        user32.SendMessageW(hwnd, WM_SETICON, ICON_BIG, h_big)
                    if h_small:
                        user32.SendMessageW(hwnd, WM_SETICON, ICON_SMALL, h_small)
                except Exception as e:
                    logger.debug("Could not set window icon: %s", e)

            try:
                window.events.shown += _set_icon_on_shown
            except Exception:
                pass

            webview.start(debug=debug)
        except (ImportError, Exception) as exc:
            logger.debug("pywebview not available: %s", exc)
            _run_in_browser()


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

    try:
        from resonant_client.updater import init_updater
        init_updater()
    except Exception:
        logger.exception("Updater init failed (non-fatal)")

    launch_gui(
        host=args.host,
        port=args.port,
        debug=args.debug,
        browser=args.browser,
    )


if __name__ == "__main__":
    main()
