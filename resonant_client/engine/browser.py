"""Native browser automation over the Chrome DevTools Protocol.

Resonant drives the user's installed Chrome directly. There is no Playwright
and no bundled Chromium: CDP is JSON-RPC over a WebSocket plus a small HTTP
discovery endpoint, so this needs only `httpx` (a core dependency) and
`websockets` (already bundled for the GUI's own socket). Installer cost is
zero.

That distinction is the reason this module exists again. Browser support was
removed in v0.9.13 because Playwright "adds 150+ MB and a Chromium download" —
but Playwright was only ever used here as a CDP *client*, via
`connect_over_cdp`, which never touches the downloaded browsers. The 150 MB
bought nothing but a protocol implementation.

Chrome runs under a dedicated Resonant profile rather than the user's everyday
one. Chrome refuses a second launch against a profile directory that is
already in use, so attaching to a personal profile means either racing the
user's own browser or killing it. A separate profile persists its own logins,
survives restarts, and can be closed without touching the user's session.

Tab grouping is the one capability CDP cannot provide — `chrome.tabGroups` is
an extension API, and none of Chrome's 57 CDP domains expose it. A small
unpacked extension ships alongside and is loaded at launch; see
`resonant_client/browser_extension/`.
"""

import base64
import json
import logging
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Optional

from .tools import ToolResult

logger = logging.getLogger(__name__)

# All knobs are env-overridable so a user with an unusual Chrome install or a
# port conflict can adjust without a rebuild.
_CDP_PORT = int(os.environ.get("RESONANT_BROWSER_CDP_PORT", "9222") or "9222")
_HEADLESS = (os.environ.get("RESONANT_BROWSER_HEADLESS", "") or "").strip().lower() in {"1", "true", "yes"}
_GROUP_TITLE = os.environ.get("RESONANT_BROWSER_GROUP_TITLE", "Resonant") or "Resonant"
_GROUP_COLOR = os.environ.get("RESONANT_BROWSER_GROUP_COLOR", "purple") or "purple"
_LAUNCH_TIMEOUT = float(os.environ.get("RESONANT_BROWSER_LAUNCH_TIMEOUT", "30") or "30")

_NAV_TIMEOUT = 30.0
_CDP_TIMEOUT = 30.0

# Source-checkout location of the unpacked extension.
_EXTENSION_SRC = Path(__file__).resolve().parent.parent / "browser_extension"

_browser_session_name = _GROUP_TITLE


def _group_title(session_name: str = "") -> str:
    """A compact Chrome-safe label for the active Resonant session."""
    cleaned = " ".join(str(session_name or "").split()).strip()
    if not cleaned or cleaned.lower() == "new session":
        return _GROUP_TITLE
    return cleaned if len(cleaned) <= 60 else cleaned[:57] + "..."


def set_browser_session_name(session_name: str = "") -> None:
    """Set the label used by the next native-browser action.

    This does not launch Chrome. It may be called for validation failures and
    from tests, so changing context must remain a side-effect-free operation
    until a browser tool actually executes.
    """
    global _browser_session_name
    title = _group_title(session_name)
    _browser_session_name = title
    with _manager_lock:
        if _manager is not None:
            _manager.set_session_name(title)


def _find_chrome() -> Optional[str]:
    """Locate the installed Chrome executable, or None."""
    override = os.environ.get("RESONANT_BROWSER_CHROME_PATH")
    if override and os.path.isfile(override):
        return override
    if sys.platform.startswith("win"):
        candidates = [
            r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
            os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
            # Edge is Chromium and speaks the same protocol; a usable fallback
            # on Windows boxes with no Chrome, where it is always present.
            r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
            r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        ]
    elif sys.platform == "darwin":
        candidates = [
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
        ]
    else:
        candidates = ["google-chrome", "google-chrome-stable", "chromium", "chromium-browser"]
    for candidate in candidates:
        if os.path.isfile(candidate):
            return candidate
        found = shutil.which(candidate)
        if found:
            return found
    return None


def _profile_dir() -> str:
    """Directory for Resonant's dedicated Chrome profile.

    Deliberately outside the user's Chrome User Data tree: Chrome locks a
    profile directory while it is running, so sharing one would mean the agent
    and the user cannot browse at the same time.
    """
    override = os.environ.get("RESONANT_BROWSER_USER_DATA_DIR")
    if override:
        return override
    return str(Path.home() / ".resonant" / "browser-profile")


def _extension_source_dir() -> Optional[Path]:
    """Locate the packaged extension. None if it is not present."""
    bundle_dir = getattr(sys, "_MEIPASS", "")
    if bundle_dir:
        # Both PyInstaller layouts, matching _ripgrep_executable and
        # updater._find_dll: one-file extracts to _MEIPASS directly, one-folder
        # puts contents under _internal/.
        candidates = [
            Path(bundle_dir) / "resonant_client" / "browser_extension",
            Path(bundle_dir) / "_internal" / "resonant_client" / "browser_extension",
        ]
    else:
        candidates = [_EXTENSION_SRC]
    for candidate in candidates:
        if (candidate / "manifest.json").is_file():
            return candidate
    return None


def _prepare_extension(profile_dir: str, group_title: str, group_color: str) -> Optional[str]:
    """Copy the extension into the profile and stamp it with the group label.

    Copied rather than loaded in place because the bundle directory is not a
    good place to write, and writing is what lets the group name describe the
    current run instead of being fixed at build time.
    """
    source = _extension_source_dir()
    if source is None:
        logger.warning("Browser extension not found; tabs will not be grouped")
        return None
    target = Path(profile_dir) / "resonant-extension"
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            shutil.rmtree(target, ignore_errors=True)
        shutil.copytree(source, target)
        (target / "config.js").write_text(
            f"const RESONANT_GROUP_TITLE = {json.dumps(group_title)};\n"
            f"const RESONANT_GROUP_COLOR = {json.dumps(group_color)};\n",
            encoding="utf-8",
        )
        return str(target)
    except Exception:
        logger.warning("Could not stage browser extension", exc_info=True)
        return None


def _port_is_open(port: int, host: str = "127.0.0.1") -> bool:
    sock = socket.socket()
    sock.settimeout(0.5)
    try:
        sock.connect((host, port))
        return True
    except Exception:
        return False
    finally:
        sock.close()


class CDPError(RuntimeError):
    """A CDP command returned an error, or the connection failed."""


class CDPConnection:
    """One WebSocket to one CDP target, with synchronous request/response.

    CDP multiplexes responses and events on a single socket, so a reply is
    matched by its `id` and any interleaved events are skipped rather than
    mistaken for the answer.
    """

    def __init__(self, ws_url: str, timeout: float = _CDP_TIMEOUT):
        from websockets.sync.client import connect

        # CDP frames carrying screenshots or full page text routinely exceed
        # the library's 1 MiB default, which would drop the connection.
        self._ws = connect(ws_url, max_size=None, open_timeout=timeout)
        self._id = 0
        self._lock = threading.Lock()
        self._timeout = timeout

    def call(self, method: str, params: Optional[dict] = None) -> dict:
        with self._lock:
            self._id += 1
            request_id = self._id
            self._ws.send(json.dumps({
                "id": request_id,
                "method": method,
                "params": params or {},
            }))
            deadline = time.time() + self._timeout
            while True:
                remaining = deadline - time.time()
                if remaining <= 0:
                    raise CDPError(f"{method} timed out after {self._timeout:.0f}s")
                message = json.loads(self._ws.recv(timeout=remaining))
                if message.get("id") != request_id:
                    continue  # an event, or a reply to an earlier call
                if "error" in message:
                    raise CDPError(f"{method}: {message['error'].get('message', message['error'])}")
                return message.get("result", {})

    def evaluate(self, expression: str, *, await_promise: bool = False) -> Any:
        """Run JS in the page and return its value.

        Page exceptions are raised rather than returned, so a failing selector
        reads as a tool error instead of a successful call returning None.
        """
        result = self.call("Runtime.evaluate", {
            "expression": expression,
            "returnByValue": True,
            "awaitPromise": await_promise,
        })
        if result.get("exceptionDetails"):
            details = result["exceptionDetails"]
            message = (
                details.get("exception", {}).get("description")
                or details.get("text")
                or "JavaScript error"
            )
            raise CDPError(message)
        return result.get("result", {}).get("value")

    def close(self) -> None:
        try:
            self._ws.close()
        except Exception:
            pass


class BrowserManager:
    """Owns the Chrome process and the connection to the active tab."""

    def __init__(self) -> None:
        self._proc: Optional[subprocess.Popen] = None
        self._conn: Optional[CDPConnection] = None
        self._target_id: str = ""
        self._launched_by_us = False
        self._extension_path: Optional[str] = None
        self._extension_id: str = ""
        self._session_name: str = _browser_session_name
        self._extension_context_signature: tuple[str, str] = ("", "")
        self._lock = threading.RLock()

    # ── lifecycle ────────────────────────────────────────────────────

    @property
    def is_connected(self) -> bool:
        return self._conn is not None

    def _http(self, path: str, method: str = "GET") -> Any:
        import httpx

        url = f"http://127.0.0.1:{_CDP_PORT}{path}"
        response = httpx.request(method, url, timeout=10)
        response.raise_for_status()
        if not response.content:
            return None
        try:
            return response.json()
        except Exception:
            return response.text

    def ensure_started(self) -> str:
        """Launch Chrome if needed and attach to a tab. Returns a status line."""
        with self._lock:
            if self.is_connected:
                self._sync_session_indicator()
                return "Browser already connected"

            if not _port_is_open(_CDP_PORT):
                message = self._launch_chrome()
                if message:
                    return message
                self._load_extension()

            try:
                message = self._attach()
                self._sync_session_indicator()
                return message
            except Exception as exc:
                return f"Error: could not attach to Chrome on port {_CDP_PORT}: {exc}"

    def set_session_name(self, session_name: str) -> None:
        title = _group_title(session_name)
        if title != self._session_name:
            self._session_name = title
            self._extension_context_signature = ("", "")

    def _extension_worker_target(self) -> Optional[dict]:
        def find() -> tuple[list[dict], Optional[dict]]:
            targets = self._http("/json/list") or []
            workers = [
                target for target in targets
                if target.get("type") in {"service_worker", "background_page"}
                and str(target.get("url") or "").startswith("chrome-extension://")
                and str(target.get("url") or "").endswith("/background.js")
            ]
            if self._extension_id:
                prefix = f"chrome-extension://{self._extension_id}/"
                match = next(
                    (
                        target for target in workers
                        if str(target.get("url") or "").startswith(prefix)
                    ),
                    None,
                )
                return workers, match
            return workers, None

        workers, match = find()
        if match is not None:
            return match
        if workers and not self._extension_id:
            return workers[0]
        if not self._extension_id:
            return None

        # Manifest V3 workers stop after an idle period. Wake our known worker
        # through the browser target, then wait briefly for its inspectable
        # target to appear. Without this, task-name updates work only during
        # the first few seconds after Chrome launches.
        browser_conn = None
        try:
            version = self._http("/json/version") or {}
            browser_conn = CDPConnection(version["webSocketDebuggerUrl"])
            browser_conn.call("ServiceWorker.enable")
            browser_conn.call(
                "ServiceWorker.startWorker",
                {"scopeURL": f"chrome-extension://{self._extension_id}/"},
            )
        finally:
            if browser_conn is not None:
                browser_conn.close()

        deadline = time.time() + 1.0
        while time.time() < deadline:
            _, match = find()
            if match is not None:
                return match
            time.sleep(0.05)
        return None

    def _sync_session_indicator(self) -> None:
        """Name and color the group containing the tab Resonant is driving."""
        signature = (self._session_name, self._target_id)
        if signature == self._extension_context_signature:
            return
        try:
            # Activating the target makes Chrome's purple group treatment land
            # on the exact tab being controlled, rather than an arbitrary tab
            # that happened to be selected before the tool call.
            if self._conn is not None and self._target_id:
                self._conn.call("Target.activateTarget", {"targetId": self._target_id})

            worker = self._extension_worker_target()
            if not worker or not worker.get("webSocketDebuggerUrl"):
                return
            connection = CDPConnection(worker["webSocketDebuggerUrl"])
            try:
                result = connection.evaluate(
                    "typeof configureResonantGroup === 'function' "
                    f"? configureResonantGroup({json.dumps({'title': self._session_name, 'color': _GROUP_COLOR})}) "
                    ": null",
                    await_promise=True,
                )
                if result is not None:
                    self._extension_context_signature = signature
            finally:
                connection.close()
        except Exception:
            # Browser operation remains usable when an enterprise Chrome policy
            # blocks unpacked extensions or service-worker inspection.
            logger.debug("Could not refresh Chrome session indicator", exc_info=True)

    def _load_extension(self) -> None:
        """Install the tab-group extension over CDP.

        Chrome 137 disabled the --load-extension command line flag, so on any
        current Chrome the flag passed at launch is silently ignored and the
        extension simply never loads — grouping then fails with no error
        anywhere. `Extensions.loadUnpacked` is the supported replacement. It
        must be issued on the *browser* target rather than a page target.

        Failure is not fatal: everything except tab grouping works without it.
        """
        if not self._extension_path:
            return
        try:
            import httpx

            version = httpx.get(f"http://127.0.0.1:{_CDP_PORT}/json/version", timeout=10).json()
            browser_conn = CDPConnection(version["webSocketDebuggerUrl"])
            try:
                result = browser_conn.call(
                    "Extensions.loadUnpacked", {"path": self._extension_path}
                )
                self._extension_id = result.get("id", "")
                logger.info("Loaded tab-group extension (%s)", self._extension_id or "no id")
            finally:
                browser_conn.close()
        except Exception:
            logger.warning(
                "Could not install the tab-group extension; browsing still works "
                "but agent tabs will not be grouped",
                exc_info=True,
            )

    def _launch_chrome(self) -> str:
        """Start Chrome with remote debugging. Returns "" on success."""
        chrome = _find_chrome()
        if not chrome:
            return (
                "Error: Chrome could not be found. Install Google Chrome, or set "
                "RESONANT_BROWSER_CHROME_PATH to its executable."
            )

        profile = _profile_dir()
        try:
            os.makedirs(profile, exist_ok=True)
        except Exception as exc:
            return f"Error: could not create the browser profile directory: {exc}"

        args = [
            chrome,
            f"--remote-debugging-port={_CDP_PORT}",
            f"--user-data-dir={profile}",
            "--no-first-run",
            "--no-default-browser-check",
            # Chrome only honours the debugging port on a fresh profile launch;
            # this profile is Resonant's alone, so there is nothing to restore.
            "--restore-last-session=false",
        ]
        if _HEADLESS:
            args.append("--headless=new")

        self._extension_path = _prepare_extension(profile, self._session_name, _GROUP_COLOR)
        if self._extension_path:
            # Chrome 137+ ignores --load-extension (it was disabled for
            # security), which is why the extension has to be installed over
            # CDP after launch — see _load_extension. The flag is kept for
            # older Chrome and Chromium builds, where it still works and the
            # Extensions CDP domain may not exist.
            args.append(f"--load-extension={self._extension_path}")

        try:
            from resonant_client.processes import background_process_kwargs
            kwargs = background_process_kwargs()
        except Exception:
            kwargs = {}
        kwargs.setdefault("stdout", subprocess.DEVNULL)
        kwargs.setdefault("stderr", subprocess.DEVNULL)

        try:
            self._proc = subprocess.Popen(args, **kwargs)
        except Exception as exc:
            return f"Error: could not start Chrome: {exc}"
        self._launched_by_us = True

        deadline = time.time() + _LAUNCH_TIMEOUT
        while time.time() < deadline:
            if _port_is_open(_CDP_PORT):
                return ""
            if self._proc.poll() is not None:
                return (
                    f"Error: Chrome exited immediately (code {self._proc.returncode}). "
                    f"Another Chrome may already be using the profile at {profile}."
                )
            time.sleep(0.2)
        return f"Error: Chrome did not open its debugging port within {_LAUNCH_TIMEOUT:.0f}s"

    def _attach(self, target_id: str = "") -> str:
        """Connect to a page target, creating one if none exists."""
        pages = [t for t in (self._http("/json/list") or []) if t.get("type") == "page"]
        target = None
        if target_id:
            target = next((t for t in pages if t.get("id") == target_id), None)
            if target is None:
                raise CDPError(f"No such tab: {target_id}")
        elif pages:
            target = pages[0]

        if target is None:
            target = self._http("/json/new?about:blank", method="PUT")

        if self._conn is not None:
            self._conn.close()
        self._conn = CDPConnection(target["webSocketDebuggerUrl"])
        self._target_id = target.get("id", "")
        self._conn.call("Page.enable")
        self._conn.call("Runtime.enable")
        self._extension_context_signature = ("", "")
        return f"Connected to Chrome (tab: {target.get('title') or target.get('url') or 'about:blank'})"

    @property
    def conn(self) -> CDPConnection:
        if self._conn is None:
            raise CDPError("Browser is not connected")
        return self._conn

    @property
    def target_id(self) -> str:
        return self._target_id

    # ── tab operations ───────────────────────────────────────────────
    #
    # Tab lifecycle goes through the HTTP endpoints rather than the Target
    # domain: /json/new and /json/close act on the browser as a whole, so they
    # work regardless of which page this connection is currently attached to.

    def list_pages(self) -> list[dict]:
        return [t for t in (self._http("/json/list") or []) if t.get("type") == "page"]

    def new_page(self, url: str = "about:blank") -> dict:
        return self._http(f"/json/new?{url}", method="PUT") or {}

    def close_page(self, target_id: str) -> None:
        if target_id:
            self._http(f"/json/close/{target_id}")

    def attach(self, target_id: str = "") -> str:
        message = self._attach(target_id)
        self._sync_session_indicator()
        return message

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None
            self._target_id = ""
            if self._proc is not None and self._launched_by_us:
                try:
                    self._proc.terminate()
                    self._proc.wait(timeout=10)
                except Exception:
                    try:
                        self._proc.kill()
                    except Exception:
                        pass
            self._proc = None
            self._launched_by_us = False

    # ── page helpers ─────────────────────────────────────────────────

    def wait_for_load(self, timeout: float = _NAV_TIMEOUT) -> None:
        """Block until the document finishes parsing.

        Polled rather than event-driven: Page.loadEventFired can arrive while
        an earlier command's response is still in flight, and this keeps the
        request/response discipline of CDPConnection.call intact.
        """
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                state = self.conn.evaluate("document.readyState")
            except CDPError:
                state = None
            if state in ("interactive", "complete"):
                return
            time.sleep(0.1)

    def current_url(self) -> str:
        try:
            return self.conn.evaluate("location.href") or ""
        except CDPError:
            return ""

    def current_title(self) -> str:
        try:
            return self.conn.evaluate("document.title") or ""
        except CDPError:
            return ""


_manager: Optional[BrowserManager] = None
_manager_lock = threading.Lock()


def get_browser() -> BrowserManager:
    global _manager
    with _manager_lock:
        if _manager is None:
            _manager = BrowserManager()
        return _manager


def shutdown_browser() -> None:
    """Close the managed Chrome. Safe to call when nothing is running."""
    global _manager
    with _manager_lock:
        if _manager is not None:
            _manager.close()
            _manager = None


# ── helpers shared by the tool implementations ───────────────────────

def _js_string(value: str) -> str:
    """Embed a Python string in a JS expression safely."""
    return json.dumps(value)


def _ensure(start: float) -> Optional[ToolResult]:
    """Start the browser if needed; return a ToolResult only on failure."""
    manager = get_browser()
    # The connected path is intentionally not skipped: ensure_started() also
    # refreshes the extension context when a different Resonant session takes
    # over an already-running browser profile.
    message = manager.ensure_started()
    if message.startswith("Error"):
        return ToolResult(message, is_error=True, elapsed=time.time() - start)
    return None


def _element_center(manager: BrowserManager, selector: str) -> tuple[float, float]:
    """Viewport coordinates of an element's centre.

    Scrolls it into view first — CDP input events are dispatched at viewport
    coordinates, so an off-screen element would otherwise receive a click at
    whatever happens to be at those coordinates instead.
    """
    box = manager.conn.evaluate(
        f"(() => {{"
        f"  const el = document.querySelector({_js_string(selector)});"
        f"  if (!el) return null;"
        f"  el.scrollIntoView({{block: 'center', inline: 'center'}});"
        f"  const r = el.getBoundingClientRect();"
        f"  return {{x: r.left + r.width / 2, y: r.top + r.height / 2}};"
        f"}})()"
    )
    if not box:
        raise CDPError(f"No element matched: {selector}")
    return float(box["x"]), float(box["y"])


def _text_center(manager: BrowserManager, text: str) -> tuple[float, float]:
    """Viewport coordinates of the smallest clickable element showing `text`.

    Targeting by visible text is how a model naturally describes a click, and
    it survives markup changes that break CSS selectors. Smallest-match wins
    because the text also matches every ancestor up to <body>, and clicking a
    wrapping container hits whatever happens to be at its centre.
    """
    box = manager.conn.evaluate(
        "(() => {"
        f"  const want = {_js_string(text)}.trim().toLowerCase();"
        "  const clickable = Array.from(document.querySelectorAll("
        "    'a, button, input, textarea, select, [role=button], [role=link], [onclick]'));"
        "  const all = clickable.concat(Array.from(document.querySelectorAll('*')));"
        "  let best = null, bestArea = Infinity;"
        "  for (const el of all) {"
        "    const label = (el.innerText || el.value || el.getAttribute('aria-label') || '')"
        "      .trim().toLowerCase();"
        "    if (!label || !label.includes(want)) continue;"
        "    const r = el.getBoundingClientRect();"
        "    if (r.width <= 0 || r.height <= 0) continue;"
        "    const area = r.width * r.height;"
        "    if (area < bestArea) { best = el; bestArea = area; }"
        "  }"
        "  if (!best) return null;"
        "  best.scrollIntoView({block: 'center', inline: 'center'});"
        "  const r = best.getBoundingClientRect();"
        "  return {x: r.left + r.width / 2, y: r.top + r.height / 2};"
        "})()"
    )
    if not box:
        raise CDPError(f"No visible element found with text: {text!r}")
    return float(box["x"]), float(box["y"])


# ── Tool implementations ─────────────────────────────────────────────
#
# Output shapes (header lines, metadata keys) match the pre-v0.9.13 Playwright
# implementation so prompts and downstream consumers written against them keep
# working.

_MAX_CONTENT = 15000


def _truncate(text: str) -> str:
    if len(text) > _MAX_CONTENT:
        return text[:_MAX_CONTENT] + "\n... (truncated)"
    return text


def _normalize_url(url: str) -> str:
    if not url.startswith(("http://", "https://", "about:", "file://")):
        return "https://" + url
    return url


def exec_browser_navigate(args: dict, start: float) -> ToolResult:
    """Navigate to a URL, starting Chrome if it is not already running."""
    url = args.get("url", "")
    if not url:
        return ToolResult("Error: 'url' is required", is_error=True, elapsed=time.time() - start)
    url = _normalize_url(url)

    failure = _ensure(start)
    if failure:
        return failure

    manager = get_browser()
    try:
        manager.conn.call("Page.navigate", {"url": url})
        manager.wait_for_load()
        final_url = manager.current_url()
        title = manager.current_title()
        return ToolResult(
            f"Navigated to: {final_url}\nTitle: {title}",
            elapsed=time.time() - start,
            metadata={"url": final_url, "title": title},
        )
    except Exception as exc:
        return ToolResult(f"Navigation error: {exc}", is_error=True, elapsed=time.time() - start)


def exec_browser_click(args: dict, start: float) -> ToolResult:
    """Click an element by CSS selector, or at explicit viewport coordinates."""
    selector = args.get("selector", "")
    text = args.get("text", "")
    x, y = args.get("x"), args.get("y")
    if not selector and not text and x is None:
        return ToolResult(
            "Error: one of 'text', 'selector', or 'x'/'y' is required",
            is_error=True, elapsed=time.time() - start,
        )

    failure = _ensure(start)
    if failure:
        return failure

    manager = get_browser()
    try:
        if text and not selector:
            x, y = _text_center(manager, text)
            target = f"text={text!r}"
        elif selector:
            x, y = _element_center(manager, selector)
            target = selector
        else:
            target = f"({x}, {y})"
        for event_type in ("mousePressed", "mouseReleased"):
            manager.conn.call("Input.dispatchMouseEvent", {
                "type": event_type,
                "x": x,
                "y": y,
                "button": "left",
                "clickCount": 1,
            })
        # A click commonly navigates; settle before reporting so the next tool
        # call does not read the previous page.
        time.sleep(0.3)
        manager.wait_for_load(timeout=10)
        return ToolResult(
            f"Clicked: {target}\nURL: {manager.current_url()}",
            elapsed=time.time() - start,
            metadata={"selector": selector, "x": x, "y": y},
        )
    except Exception as exc:
        return ToolResult(f"Click error: {exc}", is_error=True, elapsed=time.time() - start)


def exec_browser_type(args: dict, start: float) -> ToolResult:
    """Type text into an element, optionally clearing it or submitting after."""
    selector = args.get("selector", "")
    text = args.get("text", "")
    if not selector:
        return ToolResult("Error: 'selector' is required", is_error=True, elapsed=time.time() - start)

    failure = _ensure(start)
    if failure:
        return failure

    manager = get_browser()
    try:
        focused = manager.conn.evaluate(
            "(() => {"
            f"  const el = document.querySelector({_js_string(selector)});"
            "  if (!el) return false;"
            "  el.focus();"
            "  return true;"
            "})()"
        )
        if not focused:
            raise CDPError(f"No element matched: {selector}")

        if args.get("clear", False):
            # Clearing through the value setter alone leaves frameworks that
            # track their own state out of sync; fire the events they listen for.
            manager.conn.evaluate(
                "(() => {"
                f"  const el = document.querySelector({_js_string(selector)});"
                "  el.value = '';"
                "  el.dispatchEvent(new Event('input', {bubbles: true}));"
                "  el.dispatchEvent(new Event('change', {bubbles: true}));"
                "})()"
            )

        if text:
            manager.conn.call("Input.insertText", {"text": text})

        submitted = ""
        if args.get("submit", False):
            for event_type in ("keyDown", "keyUp"):
                manager.conn.call("Input.dispatchKeyEvent", {
                    "type": event_type,
                    "key": "Enter",
                    "code": "Enter",
                    "windowsVirtualKeyCode": 13,
                    "nativeVirtualKeyCode": 13,
                })
            time.sleep(0.3)
            manager.wait_for_load(timeout=15)
            submitted = " and submitted"

        return ToolResult(
            f"Typed into {selector}{submitted}\nURL: {manager.current_url()}",
            elapsed=time.time() - start,
            metadata={"selector": selector, "chars": len(text), "submitted": bool(submitted)},
        )
    except Exception as exc:
        return ToolResult(f"Type error: {exc}", is_error=True, elapsed=time.time() - start)


def _accessibility_outline(manager: "BrowserManager") -> str:
    """A flat outline of the interactive and landmark nodes on the page.

    Accessibility.getFullAXTree is the faithful source but returns thousands of
    nodes for a real page, most of them ignored generics. Keeping the roles an
    agent actually acts on is what makes this mode useful for deciding what to
    click, and keeps it inside a sane token budget.
    """
    try:
        manager.conn.call("Accessibility.enable")
        nodes = manager.conn.call("Accessibility.getFullAXTree").get("nodes", [])
    except CDPError:
        return "(accessibility tree unavailable)"

    interesting = {
        "button", "link", "textbox", "checkbox", "radio", "combobox", "listbox",
        "menuitem", "tab", "heading", "searchbox", "switch", "slider",
        "navigation", "main", "form", "banner", "contentinfo", "dialog",
    }
    lines = []
    for node in nodes:
        if node.get("ignored"):
            continue
        role = (node.get("role") or {}).get("value", "")
        if role not in interesting:
            continue
        name = (node.get("name") or {}).get("value", "").strip()
        if not name:
            continue
        lines.append(f"{role}: {name}")
        if len(lines) >= 400:
            lines.append("... (more nodes omitted)")
            break
    return "\n".join(lines) if lines else "(no labelled interactive elements)"


def exec_browser_read(args: dict, start: float) -> ToolResult:
    """Read the page as text, HTML, or an accessibility outline."""
    failure = _ensure(start)
    if failure:
        return failure

    manager = get_browser()
    mode = args.get("mode", "text")
    selector = args.get("selector", "")

    try:
        if mode == "html":
            expression = (
                f"document.querySelector({_js_string(selector)})?.innerHTML ?? ''"
                if selector else "document.documentElement.outerHTML"
            )
            content = manager.conn.evaluate(expression) or ""
        elif mode == "accessibility":
            content = _accessibility_outline(manager)
        else:
            expression = (
                f"document.querySelector({_js_string(selector)})?.innerText ?? ''"
                if selector else "document.body ? document.body.innerText : ''"
            )
            content = manager.conn.evaluate(expression) or ""

        if selector and not content and mode != "accessibility":
            raise CDPError(f"No element matched: {selector}")

        content = _truncate(content)
        url, title = manager.current_url(), manager.current_title()
        header = f"URL: {url}\nTitle: {title}\n{'-' * 40}\n"
        return ToolResult(
            header + content,
            elapsed=time.time() - start,
            metadata={"url": url, "title": title, "mode": mode, "chars": len(content)},
        )
    except Exception as exc:
        return ToolResult(f"Read error: {exc}", is_error=True, elapsed=time.time() - start)


def exec_browser_screenshot(args: dict, start: float) -> ToolResult:
    """Capture the viewport, the full page, or a single element."""
    failure = _ensure(start)
    if failure:
        return failure

    manager = get_browser()
    selector = args.get("selector", "")
    full_page = bool(args.get("full_page", False))

    try:
        params: dict = {"format": "png"}
        if selector:
            box = manager.conn.evaluate(
                "(() => {"
                f"  const el = document.querySelector({_js_string(selector)});"
                "  if (!el) return null;"
                "  el.scrollIntoView({block: 'center', inline: 'center'});"
                "  const r = el.getBoundingClientRect();"
                "  return {x: r.left, y: r.top, width: r.width, height: r.height};"
                "})()"
            )
            if not box:
                raise CDPError(f"No element matched: {selector}")
            params["clip"] = {
                "x": float(box["x"]), "y": float(box["y"]),
                "width": float(box["width"]), "height": float(box["height"]),
                "scale": 1,
            }
        elif full_page:
            params["captureBeyondViewport"] = True

        data = manager.conn.call("Page.captureScreenshot", params).get("data", "")
        png = base64.b64decode(data)
        scope = f" [{selector}]" if selector else (" [full page]" if full_page else "")
        url = manager.current_url()
        return ToolResult(
            f"Browser screenshot taken ({len(png) / 1024:.0f}KB){scope}\nURL: {url}",
            elapsed=time.time() - start,
            metadata={
                "screenshot_b64": data,
                "media_type": "image/png",
                "size_bytes": len(png),
                "url": url,
            },
        )
    except Exception as exc:
        return ToolResult(f"Screenshot error: {exc}", is_error=True, elapsed=time.time() - start)


def exec_browser_js(args: dict, start: float) -> ToolResult:
    """Evaluate JavaScript in the page and return its value."""
    code = args.get("code", "")
    if not code:
        return ToolResult("Error: 'code' is required", is_error=True, elapsed=time.time() - start)

    failure = _ensure(start)
    if failure:
        return failure

    try:
        value = get_browser().conn.evaluate(code, await_promise=True)
        rendered = value if isinstance(value, str) else json.dumps(value, default=str)
        return ToolResult(
            _truncate(rendered if rendered is not None else "undefined"),
            elapsed=time.time() - start,
            metadata={"type": type(value).__name__},
        )
    except Exception as exc:
        return ToolResult(f"JavaScript error: {exc}", is_error=True, elapsed=time.time() - start)


def exec_browser_scroll(args: dict, start: float) -> ToolResult:
    """Scroll the page by an amount, to an element, or to top/bottom."""
    failure = _ensure(start)
    if failure:
        return failure

    manager = get_browser()
    selector = args.get("selector", "")
    direction = (args.get("direction") or "down").lower()
    amount = int(args.get("amount", 500) or 500)

    try:
        if selector:
            found = manager.conn.evaluate(
                "(() => {"
                f"  const el = document.querySelector({_js_string(selector)});"
                "  if (!el) return false;"
                "  el.scrollIntoView({block: 'center', behavior: 'instant'});"
                "  return true;"
                "})()"
            )
            if not found:
                raise CDPError(f"No element matched: {selector}")
            described = f"to {selector}"
        elif direction in ("top", "bottom"):
            target = "0" if direction == "top" else "document.body.scrollHeight"
            manager.conn.evaluate(f"window.scrollTo(0, {target})")
            described = f"to {direction}"
        else:
            delta = -amount if direction == "up" else amount
            manager.conn.evaluate(f"window.scrollBy(0, {delta})")
            described = f"{direction} {amount}px"

        position = manager.conn.evaluate("Math.round(window.scrollY)")
        return ToolResult(
            f"Scrolled {described} (y={position})",
            elapsed=time.time() - start,
            metadata={"scroll_y": position},
        )
    except Exception as exc:
        return ToolResult(f"Scroll error: {exc}", is_error=True, elapsed=time.time() - start)


def exec_browser_hover(args: dict, start: float) -> ToolResult:
    """Hover the pointer over an element, revealing menus and tooltips."""
    selector = args.get("selector", "")
    if not selector:
        return ToolResult("Error: 'selector' is required", is_error=True, elapsed=time.time() - start)

    failure = _ensure(start)
    if failure:
        return failure

    manager = get_browser()
    try:
        x, y = _element_center(manager, selector)
        manager.conn.call("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": x, "y": y})
        time.sleep(0.2)
        return ToolResult(
            f"Hovering over: {selector}",
            elapsed=time.time() - start,
            metadata={"selector": selector, "x": x, "y": y},
        )
    except Exception as exc:
        return ToolResult(f"Hover error: {exc}", is_error=True, elapsed=time.time() - start)


def exec_browser_select(args: dict, start: float) -> ToolResult:
    """Choose an option in a <select> element by value or visible label."""
    selector = args.get("selector", "")
    value = args.get("value", "")
    if not selector:
        return ToolResult("Error: 'selector' is required", is_error=True, elapsed=time.time() - start)

    failure = _ensure(start)
    if failure:
        return failure

    try:
        outcome = get_browser().conn.evaluate(
            "(() => {"
            f"  const el = document.querySelector({_js_string(selector)});"
            "  if (!el) return 'no-element';"
            f"  const want = {_js_string(str(value))};"
            "  const options = Array.from(el.options || []);"
            "  const match = options.find(o => o.value === want)"
            "    || options.find(o => o.text.trim() === want.trim());"
            "  if (!match) return 'no-option';"
            "  el.value = match.value;"
            "  el.dispatchEvent(new Event('input', {bubbles: true}));"
            "  el.dispatchEvent(new Event('change', {bubbles: true}));"
            "  return 'ok:' + match.text.trim();"
            "})()"
        )
        if outcome == "no-element":
            raise CDPError(f"No element matched: {selector}")
        if outcome == "no-option":
            raise CDPError(f"No option matching {value!r} in {selector}")
        return ToolResult(
            f"Selected {outcome[3:]!r} in {selector}",
            elapsed=time.time() - start,
            metadata={"selector": selector, "value": value},
        )
    except Exception as exc:
        return ToolResult(f"Select error: {exc}", is_error=True, elapsed=time.time() - start)


def exec_browser_wait(args: dict, start: float) -> ToolResult:
    """Wait for an element to appear, or for a fixed number of seconds."""
    failure = _ensure(start)
    if failure:
        return failure

    manager = get_browser()
    selector = args.get("selector", "")
    timeout = float(args.get("timeout", 10) or 10)

    try:
        if not selector:
            time.sleep(min(timeout, 60))
            return ToolResult(f"Waited {timeout:.1f}s", elapsed=time.time() - start)

        deadline = time.time() + timeout
        while time.time() < deadline:
            visible = manager.conn.evaluate(
                "(() => {"
                f"  const el = document.querySelector({_js_string(selector)});"
                "  if (!el) return false;"
                "  const r = el.getBoundingClientRect();"
                "  return r.width > 0 && r.height > 0;"
                "})()"
            )
            if visible:
                return ToolResult(
                    f"Element appeared: {selector}",
                    elapsed=time.time() - start,
                    metadata={"selector": selector},
                )
            time.sleep(0.25)
        return ToolResult(
            f"Timed out after {timeout:.0f}s waiting for: {selector}",
            is_error=True,
            elapsed=time.time() - start,
        )
    except Exception as exc:
        return ToolResult(f"Wait error: {exc}", is_error=True, elapsed=time.time() - start)


def exec_browser_back(args: dict, start: float) -> ToolResult:
    """Go back, or forward, in the tab's history."""
    failure = _ensure(start)
    if failure:
        return failure

    manager = get_browser()
    forward = bool(args.get("forward", False))
    try:
        manager.conn.evaluate("history.forward()" if forward else "history.back()")
        time.sleep(0.4)
        manager.wait_for_load(timeout=15)
        direction = "forward" if forward else "back"
        url = manager.current_url()
        return ToolResult(
            f"Went {direction}\nURL: {url}",
            elapsed=time.time() - start,
            metadata={"url": url, "direction": direction},
        )
    except Exception as exc:
        return ToolResult(f"History error: {exc}", is_error=True, elapsed=time.time() - start)


def exec_browser_tabs(args: dict, start: float) -> ToolResult:
    """List, switch to, open, or close tabs."""
    action = (args.get("action") or "list").lower()

    failure = _ensure(start)
    if failure:
        return failure

    manager = get_browser()
    try:
        pages = manager.list_pages()

        if action == "list":
            lines = [
                f"  [{i}] {t.get('url', '')} - {t.get('title', '')}"
                f"{' (active)' if t.get('id') == manager.target_id else ''}"
                for i, t in enumerate(pages)
            ]
            return ToolResult(
                f"{len(pages)} tab(s):\n" + "\n".join(lines),
                elapsed=time.time() - start,
                metadata={"tab_count": len(pages)},
            )

        if action == "switch":
            index = int(args.get("index", 0) or 0)
            if not 0 <= index < len(pages):
                return ToolResult(
                    f"Invalid tab index {index} (have {len(pages)} tabs)",
                    is_error=True, elapsed=time.time() - start,
                )
            target = pages[index]
            manager.attach(target.get("id", ""))
            # Raise it in the real window too, so what the agent reports and
            # what the user sees on screen are the same tab.
            try:
                manager.conn.call("Page.bringToFront")
            except CDPError:
                pass
            return ToolResult(
                f"Switched to tab [{index}]: {target.get('url', '')}",
                elapsed=time.time() - start,
                metadata={"index": index},
            )

        if action == "new":
            url = _normalize_url(args.get("url", "about:blank") or "about:blank")
            created = manager.new_page(url)
            manager.attach(created.get("id", ""))
            manager.wait_for_load(timeout=15)
            return ToolResult(
                f"Opened new tab: {manager.current_url()}",
                elapsed=time.time() - start,
                metadata={"url": manager.current_url()},
            )

        if action == "close":
            index = args.get("index")
            target_id = manager.target_id
            if index is not None:
                index = int(index)
                if not 0 <= index < len(pages):
                    return ToolResult(
                        f"Invalid tab index {index} (have {len(pages)} tabs)",
                        is_error=True, elapsed=time.time() - start,
                    )
                target_id = pages[index].get("id", "")
            manager.close_page(target_id)
            # The closed tab may have been the attached one; re-attach so the
            # next call does not talk to a dead target.
            remaining = manager.list_pages()
            if remaining:
                manager.attach(remaining[0].get("id", ""))
            return ToolResult(
                f"Closed tab; {len(remaining)} remaining",
                elapsed=time.time() - start,
                metadata={"tab_count": len(remaining)},
            )

        return ToolResult(
            f"Unknown action: {action} (use list, switch, new, or close)",
            is_error=True, elapsed=time.time() - start,
        )
    except Exception as exc:
        return ToolResult(f"Tabs error: {exc}", is_error=True, elapsed=time.time() - start)
