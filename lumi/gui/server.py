"""
Lumi GUI — Server Launcher

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


def _client_host(host: str) -> str:
    """Address a local client uses to reach a server bound to ``host``.

    A wildcard bind is not a destination: browsers cannot open 0.0.0.0, and
    the server accepts only Host headers that name the address it was reached
    on (see gui/local_access.py).
    """
    return {"": "127.0.0.1", "0.0.0.0": "127.0.0.1", "::": "::1"}.get(host, host)


def _brand_macos_process(icon_path: str) -> None:
    """Show Lumi's name and icon in the macOS Dock and menu bar.

    A packaged Lumi.app gets both from its Info.plist and lumi.icns. A run from
    source is a Python process, which macOS would otherwise label "Python"
    with Python's icon. pywebview's own `icon` option is GTK/Qt only.
    """
    if sys.platform != "darwin":
        return
    try:
        from AppKit import NSApplication, NSImage
        from Foundation import NSBundle
    except ImportError:
        return
    try:
        info = NSBundle.mainBundle().infoDictionary()
        if info is not None and info.get("CFBundleName") in (None, "Python"):
            info["CFBundleName"] = "Lumi"
        image = NSImage.alloc().initWithContentsOfFile_(icon_path)
        if image is not None:
            NSApplication.sharedApplication().setApplicationIconImage_(image)
    except Exception:
        logger.debug("Could not set the macOS app name and icon", exc_info=True)


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
    Launch the Lumi GUI application.

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
    # After the check above: Starlette comes with the same optional extra.
    from .local_access import access as local_access

    if port == 0:
        port = _find_free_port()

    client_host = _client_host(host)
    url = f"http://{f'[{client_host}]' if ':' in client_host else client_host}:{port}"

    # Start uvicorn in a background thread
    config = uvicorn.Config(
        "lumi.gui.app:app",
        host=host,
        port=port,
        log_level="debug" if debug else "warning",
        access_log=debug,
    )
    server = uvicorn.Server(config)

    server_thread = threading.Thread(target=server.run, daemon=True)
    server_thread.start()

    # Wait for server to be ready
    if not _wait_for_server(client_host, port):
        print(f"Error: Server failed to start on {url}")
        sys.exit(1)

    # Flushed: a frozen app writing to a pipe or file buffers stdout, and the
    # launch link must reach whoever is waiting for it.
    print(f"  Lumi GUI running at {url}", flush=True)

    # Code editors find this launch through the bridge file (gui/editor_bridge.py).
    from .app import state as app_state
    from .editor_bridge import bridge as editor_bridge, enabled as editor_bridge_enabled

    editor_bridge.start(url, enabled=editor_bridge_enabled(app_state.settings))

    # An update installs only between agent turns, and then closes Lumi
    # itself so the installer can replace its files (lumi/updater.py).
    def _turn_running() -> bool:
        from .app import state as app_state

        runs = getattr(app_state, "_chat_run_loop", None)
        return bool(runs and runs.busy)

    def _close_for_update() -> None:
        import lumi.gui.app as gui_app

        server.should_exit = True
        window = getattr(gui_app, "_webview_window", None)
        if window is not None:
            window.destroy()

    from lumi.updater import set_host

    set_host(busy=_turn_running, shutdown=_close_for_update)

    def _run_in_browser():
        # The page is useless without this launch's access token, which it gets
        # by redeeming the code in the link's fragment. Browsers never send
        # fragments to the server, and the code works once, so a copy left in
        # the terminal or a log opens nothing after the page has loaded.
        print(f"  Open in browser (one-time link): {local_access.launch_url(url)}", flush=True)
        print("  Press Ctrl+C to stop.", flush=True)
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

                def open_in_browser(self):
                    """Open this app in the default browser with a fresh one-time link.

                    Only the desktop window has this bridge, so browser pages and
                    other local clients cannot mint launch codes.
                    """
                    import webbrowser
                    try:
                        return bool(webbrowser.open(local_access.launch_url(url)))
                    except Exception:
                        logger.debug("Could not open the default browser", exc_info=True)
                        return False

            win_ref = [None]
            api = _WindowAPI(win_ref)

            # Resolve the Windows icon used after the native HWND exists.
            icon_dir = os.path.join(os.path.dirname(__file__), "static")
            ico_path = os.path.join(icon_dir, "lumi.ico")

            # Paint the window in the saved theme's background until the page
            # loads, instead of a white flash. Same process as the server.
            from .app import state as app_state
            from .appearance import window_background

            wv_kwargs = dict(
                title="Lumi",
                url=local_access.launch_url(url),
                width=1200,
                height=800,
                min_size=(800, 600),
                resizable=True,
                text_select=True,
                background_color=window_background(app_state.settings),
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
                        "LuminaryAnalytics.Lumi"
                    )
                except Exception:
                    pass

            window = webview.create_window(**wv_kwargs)
            win_ref[0] = window
            import lumi.gui.app as _gui_app
            _gui_app._webview_window = window
            # The page's CSP refuses the eval that pywebview's bridge uses,
            # and WebKit (macOS, Linux) enforces it on the bridge too.
            try:
                from .webview_bridge import install as install_bridge_without_eval
                install_bridge_without_eval(window)
            except Exception:
                logger.warning("Could not set up the window bridge without eval", exc_info=True)

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
                    hwnd = user32.FindWindowW(None, "Lumi")
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

            _brand_macos_process(os.path.join(icon_dir, "lumi-macos.png"))
            webview.start(debug=debug)
        except (ImportError, Exception) as exc:
            logger.debug("pywebview not available: %s", exc)
            _run_in_browser()


def main():
    """CLI entry point for lumi-gui."""
    from ..paths import migrate_legacy_home
    migrate_legacy_home()
    parser = argparse.ArgumentParser(
        description="Lumi coding agent — desktop GUI",
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
    # Count the app's own crashes for fleet health (lumi/activity.py).
    from .. import activity

    activity.install_crash_counter()

    # Materialize new bundled skills on GUI startup, as the skill CLI does.
    # Existing user-edited skills are preserved by the installer's default.
    try:
        from lumi.orchestration.bundled_skills import install_bundled_skills
        install_bundled_skills()
    except Exception:
        logger.exception("Bundled skill installation failed (non-fatal)")

    try:
        from lumi.updater import init_updater
        init_updater()
    except Exception:
        logger.exception("Updater init failed (non-fatal)")

    # Transcript retention runs at startup and then daily, off the UI path.
    def _retention_loop():
        from .app import state as app_state
        while True:
            try:
                app_state.enforce_retention()
            except Exception:
                logger.exception("Transcript retention failed (non-fatal)")
            time.sleep(24 * 3600)

    threading.Thread(target=_retention_loop, daemon=True, name="lumi-retention").start()

    # Lumi Cloud check-ins (lumi/cloud.py): only for an enrolled computer, or
    # one whose machine policy asks to enroll; otherwise the loop does nothing.
    try:
        from lumi.cloud import start_background

        from .app import state as app_state
        start_background(app_state.cloud)
        # Requests from Slack and Teams, once someone turns them on.
        from lumi.remote_tasks import RemoteTasks
        from lumi.remote_tasks import start as start_remote_tasks

        app_state.cloud.remote_tasks = RemoteTasks(app_state.settings, app_state.cloud)
        start_remote_tasks(app_state.cloud.remote_tasks)
        # Agent pull requests join the organization's review queue (engine/review_gate.py).
        from lumi.engine import review_gate
        from lumi.review_queue import ReviewQueue

        review_gate.set_registrar(ReviewQueue(app_state.cloud))
        # Commands the organization's policy lists wait for a second person (engine/second_approval.py).
        from lumi.approvals import ApprovalRequester
        from lumi.engine import second_approval

        second_approval.set_requester(ApprovalRequester(app_state.cloud))
    except Exception:
        logger.exception("Lumi Cloud check-ins failed to start (non-fatal)")

    launch_gui(
        host=args.host,
        port=args.port,
        debug=args.debug,
        browser=args.browser,
    )


if __name__ == "__main__":
    main()
