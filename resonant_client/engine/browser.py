"""
Browser automation via Playwright.

Provides tools for the agent to control Chrome/Chromium:
navigate, click, type, read page content, take screenshots, run JavaScript.

Can connect to an existing Chrome instance via CDP (--remote-debugging-port)
or launch a managed Chromium instance.

Usage:
    pip install playwright
    playwright install chromium
"""

import base64
import logging
import os
import shutil
import subprocess
import sys
import time
import urllib.request
from typing import Optional

from resonant_client.processes import background_process_kwargs

from .tools import ToolResult

logger = logging.getLogger(__name__)

# ── Real-Chrome / CDP config (v0.6.5) ─────────────────────────────────
#
# The agent attaches to the user's INSTALLED Chrome via the DevTools
# protocol (CDP) so it drives their real profiles + existing logins in a
# visible window they can work alongside — rather than a bundled, logged-
# out Chromium. Resonant launches Chrome with `--remote-debugging-port`
# (+ the chosen `--profile-directory`) when no debug endpoint is already
# up, then connects. All knobs are env-overridable.
_BROWSER_CDP_PORT = int(os.environ.get("RESONANT_BROWSER_CDP_PORT", "9222") or "9222")
_BROWSER_PROFILE = os.environ.get("RESONANT_BROWSER_PROFILE", "Default") or "Default"


def _find_chrome() -> Optional[str]:
    """Locate the installed Google Chrome executable (or honor an explicit
    RESONANT_BROWSER_CHROME_PATH). Returns None if not found."""
    override = os.environ.get("RESONANT_BROWSER_CHROME_PATH")
    if override and os.path.isfile(override):
        return override
    if sys.platform.startswith("win"):
        candidates = [
            r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
            os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
        ]
    elif sys.platform == "darwin":
        candidates = ["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"]
    else:
        candidates = ["google-chrome", "google-chrome-stable", "chromium", "chromium-browser"]
    for c in candidates:
        if os.path.isfile(c):
            return c
        found = shutil.which(c)
        if found:
            return found
    return None


def _default_user_data_dir() -> Optional[str]:
    """The user's real Chrome User Data dir (carries their existing
    profiles + logins). Override via RESONANT_BROWSER_USER_DATA_DIR."""
    override = os.environ.get("RESONANT_BROWSER_USER_DATA_DIR")
    if override:
        return override
    if sys.platform.startswith("win"):
        local = os.environ.get("LOCALAPPDATA")
        return os.path.join(local, "Google", "Chrome", "User Data") if local else None
    if sys.platform == "darwin":
        return os.path.expanduser("~/Library/Application Support/Google/Chrome")
    return os.path.expanduser("~/.config/google-chrome")

# ── Singleton browser manager ────────────────────────────────────────

_browser_mgr: Optional["BrowserManager"] = None


class BrowserManager:
    """
    Manages a Playwright browser instance.

    Supports two modes:
    - Launch: starts a new Chromium instance (headed by default)
    - Connect: connects to existing Chrome via CDP endpoint

    The manager is a singleton — one browser per session.
    """

    def __init__(self):
        self._playwright = None
        self._browser = None
        self._context = None
        self._page = None
        self._connected = False
        self._chrome_proc = None  # set when WE launched Chrome (CDP mode)

    @property
    def is_connected(self) -> bool:
        return self._connected and self._page is not None

    def launch(self, headless: bool = False) -> str:
        """Launch a new Chromium instance."""
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            return "Error: playwright not installed. Run: pip install playwright && playwright install chromium"

        if self._connected:
            return "Browser already connected."

        self._playwright = sync_playwright().start()
        self._browser = self._playwright.chromium.launch(
            headless=headless,
            args=["--no-first-run", "--no-default-browser-check", "--no-sandbox"],
        )
        self._context = self._browser.new_context(
            viewport={"width": 1280, "height": 720},
        )
        self._page = self._context.new_page()
        self._connected = True
        return "Browser launched (Chromium, 1280x720)."

    def connect_cdp(self, endpoint: str = "http://localhost:9222") -> str:
        """
        Connect to an existing Chrome via CDP.

        Start Chrome with: chrome --remote-debugging-port=9222
        """
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            return "Error: playwright not installed. Run: pip install playwright && playwright install chromium"

        if self._connected:
            return "Browser already connected."

        self._playwright = sync_playwright().start()
        try:
            self._browser = self._playwright.chromium.connect_over_cdp(endpoint)
            contexts = self._browser.contexts
            if contexts:
                self._context = contexts[0]
                pages = self._context.pages
                self._page = pages[0] if pages else self._context.new_page()
            else:
                self._context = self._browser.new_context()
                self._page = self._context.new_page()
            self._connected = True
            url = self._page.url
            return f"Connected to Chrome at {endpoint}. Current page: {url}"
        except Exception as e:
            self._playwright.stop()
            self._playwright = None
            return f"Error connecting to Chrome at {endpoint}: {e}"

    @staticmethod
    def _cdp_alive(endpoint: str) -> bool:
        """True if a Chrome DevTools endpoint is reachable at `endpoint`."""
        try:
            with urllib.request.urlopen(f"{endpoint}/json/version", timeout=1.0) as r:
                return getattr(r, "status", 200) == 200
        except Exception:
            return False

    def _wait_for_cdp(self, endpoint: str, timeout: float = 15.0) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self._cdp_alive(endpoint):
                return True
            time.sleep(0.25)
        return False

    def _open_agent_tab(self) -> None:
        """Give the agent its own fresh, foregrounded tab so it never
        drives a tab the user is actively working in."""
        if not self._connected or not self._context:
            return
        try:
            self._page = self._context.new_page()
            self._page.bring_to_front()
        except Exception:
            pass  # keep whatever connect_cdp selected

    def connect_or_launch_chrome(
        self,
        *,
        profile: Optional[str] = None,
        port: Optional[int] = None,
        user_data_dir: Optional[str] = None,
        headless: bool = False,
    ) -> str:
        """v0.6.5 — attach to the user's REAL Chrome via CDP (their profiles
        + existing logins, in a visible window they can work alongside):
          1. If a debug Chrome is already up on `port`, attach to it.
          2. Else launch installed Chrome with `--remote-debugging-port`
             (+ the chosen `--profile-directory`) and attach.
          3. If installed Chrome can't be found/launched, fall back to a
             bundled Chromium so the agent still has *a* browser."""
        if self._connected:
            return "Browser already connected."
        port = port or _BROWSER_CDP_PORT
        profile = profile or _BROWSER_PROFILE
        endpoint = f"http://127.0.0.1:{port}"

        # 1. Reuse an already-running debug Chrome.
        if self._cdp_alive(endpoint):
            result = self.connect_cdp(endpoint)
            self._open_agent_tab()
            return result

        # 2. Launch the real Chrome with the debug port + chosen profile.
        chrome = _find_chrome()
        if not chrome:
            logger.warning("Installed Chrome not found; falling back to bundled Chromium.")
            return self.launch(headless=headless)

        udd = user_data_dir if user_data_dir is not None else _default_user_data_dir()
        args = [chrome, f"--remote-debugging-port={port}"]
        if udd:
            args.append(f"--user-data-dir={udd}")
        if profile:
            args.append(f"--profile-directory={profile}")
        args += ["--no-first-run", "--no-default-browser-check"]
        if headless:
            args.append("--headless=new")

        try:
            self._chrome_proc = subprocess.Popen(
                args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        except Exception as e:
            logger.warning("Failed to launch Chrome (%s); falling back to Chromium.", e)
            return self.launch(headless=headless)

        if not self._wait_for_cdp(endpoint, timeout=20.0):
            return (
                f"Launched Chrome but its debug port {port} never came up. The "
                f"usual cause: your everyday Chrome is already running on this "
                f"profile, so the new launch handed off to it (Chrome can't open "
                f"the debug port on an already-running profile). Close Chrome and "
                f"retry, point RESONANT_BROWSER_USER_DATA_DIR at another dir, or "
                f"start Chrome yourself with --remote-debugging-port={port}."
            )
        result = self.connect_cdp(endpoint)
        self._open_agent_tab()
        return result

    @staticmethod
    def _chrome_running() -> bool:
        """True if any Chrome process is currently running."""
        try:
            if sys.platform.startswith("win"):
                out = subprocess.run(
                    ["tasklist", "/FI", "IMAGENAME eq chrome.exe", "/NH"],
                    capture_output=True, text=True, timeout=8,
                    **background_process_kwargs(),
                )
                return "chrome.exe" in (out.stdout or "").lower()
            target = "Google Chrome" if sys.platform == "darwin" else "chrome"
            out = subprocess.run(["pgrep", "-x", target], capture_output=True, timeout=8)
            return out.returncode == 0
        except Exception:
            return False

    @staticmethod
    def _close_all_chrome(force: bool = False) -> None:
        """Ask every Chrome window to close. Graceful by default so Chrome
        saves its session (tabs come back on relaunch); `force` hard-kills."""
        try:
            if sys.platform.startswith("win"):
                cmd = ["taskkill", "/IM", "chrome.exe"] + (["/F"] if force else [])
                subprocess.run(
                    cmd, capture_output=True, timeout=12,
                    **background_process_kwargs(),
                )
            elif sys.platform == "darwin":
                if force:
                    subprocess.run(["pkill", "-9", "-x", "Google Chrome"], capture_output=True, timeout=12)
                else:
                    subprocess.run(["osascript", "-e", 'tell application "Google Chrome" to quit'],
                                   capture_output=True, timeout=12)
            else:
                subprocess.run(["pkill", "-9" if force else "-TERM", "chrome"], capture_output=True, timeout=12)
        except Exception:
            logger.debug("close-all-chrome failed", exc_info=True)

    def _wait_chrome_exited(self, timeout: float = 12.0) -> bool:
        """Wait for Chrome processes to exit, escalating to a force-kill
        partway through if a graceful close didn't take."""
        deadline = time.time() + timeout
        forced = False
        while time.time() < deadline:
            if not self._chrome_running():
                return True
            if not forced and time.time() > deadline - timeout / 2:
                self._close_all_chrome(force=True)
                forced = True
            time.sleep(0.4)
        return not self._chrome_running()

    def relaunch_in_debug(
        self,
        *,
        profile: Optional[str] = None,
        port: Optional[int] = None,
        user_data_dir: Optional[str] = None,
        headless: bool = False,
    ) -> str:
        """v0.6.5 — the user has Chrome open WITHOUT the debug port (so we
        can't attach, and a plain launch just hands off to the running
        instance). Close it gracefully (Chrome saves the session for
        restore), then relaunch on the real profile WITH the debug port and
        attach. This is the one-click 'relaunch my Chrome in debug mode'."""
        if self._connected:
            return "Browser already connected."
        self._close_all_chrome(force=False)
        if not self._wait_chrome_exited(timeout=12.0):
            return (
                "Couldn't fully close Chrome to relaunch it in debug mode. "
                "Close all Chrome windows manually, then try again."
            )
        return self.connect_or_launch_chrome(
            profile=profile, port=port, user_data_dir=user_data_dir, headless=headless,
        )

    def close(self):
        """Close browser and cleanup."""
        if self._browser:
            try:
                self._browser.close()
            except Exception:
                pass
        if self._playwright:
            try:
                self._playwright.stop()
            except Exception:
                pass
        # v0.6.5 — if WE launched Chrome (CDP mode), terminate that process.
        # When we merely ATTACHED to an already-running Chrome, _chrome_proc
        # is None and the user's Chrome is left running untouched.
        if self._chrome_proc is not None:
            try:
                self._chrome_proc.terminate()
            except Exception:
                pass
            self._chrome_proc = None
        self._browser = None
        self._context = None
        self._page = None
        self._playwright = None
        self._connected = False

    @property
    def page(self):
        return self._page

    def ensure_page(self) -> Optional[str]:
        """Ensure we have a page. Returns error string or None."""
        if not self._connected or not self._page:
            return "Browser not connected. Use browser_navigate with a URL to auto-launch, or /browser connect."
        return None


def get_browser() -> BrowserManager:
    """Get or create the singleton browser manager."""
    global _browser_mgr
    if _browser_mgr is None:
        _browser_mgr = BrowserManager()
    return _browser_mgr


# ── Browser tool executors ───────────────────────────────────────────

def exec_browser_navigate(args: dict, start: float) -> ToolResult:
    """Navigate to a URL. Auto-launches browser if not connected."""
    url = args.get("url", "")
    if not url:
        return ToolResult("Error: 'url' is required", is_error=True, elapsed=time.time() - start)

    mgr = get_browser()

    # Auto-attach to the user's real Chrome via CDP if not connected (falls
    # back to a bundled Chromium internally if Chrome isn't available).
    if not mgr.is_connected:
        result = mgr.connect_or_launch_chrome()
        if "Error" in result or "never came up" in result:
            return ToolResult(result, is_error=True, elapsed=time.time() - start)

    try:
        mgr.page.goto(url, wait_until="domcontentloaded", timeout=30000)
        title = mgr.page.title()
        final_url = mgr.page.url
        elapsed = time.time() - start
        return ToolResult(
            f"Navigated to: {final_url}\nTitle: {title}",
            elapsed=elapsed,
            metadata={"url": final_url, "title": title},
        )
    except Exception as e:
        return ToolResult(f"Navigation error: {e}", is_error=True, elapsed=time.time() - start)


def exec_browser_click(args: dict, start: float) -> ToolResult:
    """Click an element by text content, selector, or coordinates."""
    mgr = get_browser()
    err = mgr.ensure_page()
    if err:
        return ToolResult(err, is_error=True, elapsed=time.time() - start)

    text = args.get("text", "")
    selector = args.get("selector", "")
    x = args.get("x")
    y = args.get("y")

    try:
        if text:
            clicked = False
            for locator_fn in [
                lambda: mgr.page.get_by_text(text, exact=False).first,
                lambda: mgr.page.get_by_role("link", name=text).first,
                lambda: mgr.page.get_by_role("button", name=text).first,
                lambda: mgr.page.locator(f"a:has-text('{text}')").first,
            ]:
                try:
                    loc = locator_fn()
                    loc.click(timeout=3000)
                    clicked = True
                    break
                except Exception:
                    continue
            if not clicked:
                return ToolResult(f"Could not find clickable element with text: '{text}'",
                                is_error=True, elapsed=time.time() - start)
            action = f"Clicked element with text: '{text}'"
        elif selector:
            mgr.page.click(selector, timeout=5000)
            action = f"Clicked: {selector}"
        elif x is not None and y is not None:
            mgr.page.mouse.click(x, y)
            action = f"Clicked at ({x}, {y})"
        else:
            return ToolResult("Error: provide 'text', 'selector', or 'x'+'y' coordinates",
                            is_error=True, elapsed=time.time() - start)

        mgr.page.wait_for_load_state("domcontentloaded", timeout=5000)
        elapsed = time.time() - start
        return ToolResult(action, elapsed=elapsed, metadata={"action": "click"})
    except Exception as e:
        return ToolResult(f"Click error: {e}", is_error=True, elapsed=time.time() - start)


def exec_browser_type(args: dict, start: float) -> ToolResult:
    """Type text into a focused element or a specific selector."""
    mgr = get_browser()
    err = mgr.ensure_page()
    if err:
        return ToolResult(err, is_error=True, elapsed=time.time() - start)

    text = args.get("text", "")
    selector = args.get("selector", "")
    clear = args.get("clear", False)
    submit = args.get("submit", False)

    try:
        if selector:
            if clear:
                mgr.page.fill(selector, text, timeout=5000)
            else:
                mgr.page.type(selector, text, timeout=5000)
            action = f"Typed into {selector}: '{text[:50]}{'...' if len(text) > 50 else ''}'"
        else:
            # Type into currently focused element
            mgr.page.keyboard.type(text)
            action = f"Typed: '{text[:50]}{'...' if len(text) > 50 else ''}'"

        if submit:
            mgr.page.keyboard.press("Enter")
            action += " (submitted)"
            mgr.page.wait_for_load_state("domcontentloaded", timeout=5000)

        elapsed = time.time() - start
        return ToolResult(action, elapsed=elapsed, metadata={"action": "type"})
    except Exception as e:
        return ToolResult(f"Type error: {e}", is_error=True, elapsed=time.time() - start)


def exec_browser_read(args: dict, start: float) -> ToolResult:
    """
    Read page content. Returns text content or accessibility snapshot.

    mode:
    - "text" (default): Get visible text content
    - "html": Get inner HTML
    - "accessibility": Get accessibility tree (like Playwright MCP)
    """
    mgr = get_browser()
    err = mgr.ensure_page()
    if err:
        return ToolResult(err, is_error=True, elapsed=time.time() - start)

    mode = args.get("mode", "text")
    selector = args.get("selector", "")

    try:
        if mode == "html":
            if selector:
                content = mgr.page.inner_html(selector, timeout=5000)
            else:
                content = mgr.page.content()
            # Truncate HTML
            if len(content) > 15000:
                content = content[:15000] + "\n... (truncated)"
        elif mode == "accessibility":
            try:
                snapshot = mgr.page.accessibility.snapshot()
                content = _format_accessibility_tree(snapshot) if snapshot else "(empty page)"
            except AttributeError:
                content = mgr.page.inner_text("body", timeout=5000)
                content = f"(accessibility API unavailable, falling back to text)\n{content}"
            if len(content) > 15000:
                content = content[:15000] + "\n... (truncated)"
        else:
            # Text mode
            if selector:
                content = mgr.page.inner_text(selector, timeout=5000)
            else:
                content = mgr.page.inner_text("body", timeout=5000)
            if len(content) > 15000:
                content = content[:15000] + "\n... (truncated)"

        url = mgr.page.url
        title = mgr.page.title()
        elapsed = time.time() - start

        header = f"URL: {url}\nTitle: {title}\n{'─' * 40}\n"
        return ToolResult(
            header + content,
            elapsed=elapsed,
            metadata={"url": url, "title": title, "mode": mode, "chars": len(content)},
        )
    except Exception as e:
        return ToolResult(f"Read error: {e}", is_error=True, elapsed=time.time() - start)


def exec_browser_screenshot(args: dict, start: float) -> ToolResult:
    """Take a screenshot of the current page. Returns base64 PNG."""
    mgr = get_browser()
    err = mgr.ensure_page()
    if err:
        return ToolResult(err, is_error=True, elapsed=time.time() - start)

    full_page = args.get("full_page", False)

    try:
        screenshot_bytes = mgr.page.screenshot(full_page=full_page, type="png")
        b64 = base64.b64encode(screenshot_bytes).decode("utf-8")
        url = mgr.page.url
        size_kb = len(screenshot_bytes) / 1024
        elapsed = time.time() - start

        return ToolResult(
            f"Screenshot taken ({size_kb:.0f}KB). URL: {url}",
            elapsed=elapsed,
            metadata={
                "screenshot_b64": b64,
                "media_type": "image/png",
                "url": url,
                "size_bytes": len(screenshot_bytes),
            },
        )
    except Exception as e:
        return ToolResult(f"Screenshot error: {e}", is_error=True, elapsed=time.time() - start)


def exec_browser_js(args: dict, start: float) -> ToolResult:
    """Execute JavaScript in the page context."""
    mgr = get_browser()
    err = mgr.ensure_page()
    if err:
        return ToolResult(err, is_error=True, elapsed=time.time() - start)

    code = args.get("code", "")
    if not code:
        return ToolResult("Error: 'code' is required", is_error=True, elapsed=time.time() - start)

    try:
        result = mgr.page.evaluate(code)
        elapsed = time.time() - start

        if result is None:
            output = "(no return value)"
        elif isinstance(result, (dict, list)):
            import json
            output = json.dumps(result, indent=2, default=str)
        else:
            output = str(result)

        if len(output) > 10000:
            output = output[:10000] + "\n... (truncated)"

        return ToolResult(output, elapsed=elapsed, metadata={"action": "js"})
    except Exception as e:
        return ToolResult(f"JS error: {e}", is_error=True, elapsed=time.time() - start)


def exec_browser_scroll(args: dict, start: float) -> ToolResult:
    """Scroll the page or scroll an element into view."""
    mgr = get_browser()
    err = mgr.ensure_page()
    if err:
        return ToolResult(err, is_error=True, elapsed=time.time() - start)

    selector = args.get("selector", "")
    direction = args.get("direction", "down")
    amount = args.get("amount", 500)

    try:
        if selector:
            mgr.page.locator(selector).first.scroll_into_view_if_needed(timeout=5000)
            action = f"Scrolled '{selector}' into view"
        else:
            delta = -amount if direction == "up" else amount
            mgr.page.mouse.wheel(0, delta)
            action = f"Scrolled {direction} {amount}px"

        elapsed = time.time() - start
        return ToolResult(action, elapsed=elapsed, metadata={"action": "scroll"})
    except Exception as e:
        return ToolResult(f"Scroll error: {e}", is_error=True, elapsed=time.time() - start)


def exec_browser_hover(args: dict, start: float) -> ToolResult:
    """Hover over an element to reveal tooltips or dropdowns."""
    mgr = get_browser()
    err = mgr.ensure_page()
    if err:
        return ToolResult(err, is_error=True, elapsed=time.time() - start)

    text = args.get("text", "")
    selector = args.get("selector", "")

    try:
        if text:
            mgr.page.get_by_text(text, exact=False).first.hover(timeout=5000)
            action = f"Hovered over element with text: '{text}'"
        elif selector:
            mgr.page.hover(selector, timeout=5000)
            action = f"Hovered over: {selector}"
        else:
            return ToolResult("Error: provide 'text' or 'selector'",
                            is_error=True, elapsed=time.time() - start)

        elapsed = time.time() - start
        return ToolResult(action, elapsed=elapsed, metadata={"action": "hover"})
    except Exception as e:
        return ToolResult(f"Hover error: {e}", is_error=True, elapsed=time.time() - start)


def exec_browser_select(args: dict, start: float) -> ToolResult:
    """Select an option from a dropdown."""
    mgr = get_browser()
    err = mgr.ensure_page()
    if err:
        return ToolResult(err, is_error=True, elapsed=time.time() - start)

    selector = args.get("selector", "")
    value = args.get("value", "")
    label = args.get("label", "")

    if not selector:
        return ToolResult("Error: 'selector' is required", is_error=True, elapsed=time.time() - start)

    try:
        if label:
            mgr.page.select_option(selector, label=label, timeout=5000)
            action = f"Selected option with label '{label}' in {selector}"
        elif value:
            mgr.page.select_option(selector, value=value, timeout=5000)
            action = f"Selected option with value '{value}' in {selector}"
        else:
            return ToolResult("Error: provide 'value' or 'label'",
                            is_error=True, elapsed=time.time() - start)

        elapsed = time.time() - start
        return ToolResult(action, elapsed=elapsed, metadata={"action": "select"})
    except Exception as e:
        return ToolResult(f"Select error: {e}", is_error=True, elapsed=time.time() - start)


def exec_browser_wait(args: dict, start: float) -> ToolResult:
    """Wait for a selector to appear or a fixed delay."""
    mgr = get_browser()
    err = mgr.ensure_page()
    if err:
        return ToolResult(err, is_error=True, elapsed=time.time() - start)

    selector = args.get("selector", "")
    timeout = args.get("timeout", 10000)
    delay = args.get("delay", 0)

    try:
        if selector:
            mgr.page.wait_for_selector(selector, state="visible", timeout=timeout)
            action = f"Element '{selector}' is now visible"
        elif delay:
            mgr.page.wait_for_timeout(min(delay, 30000))
            action = f"Waited {delay}ms"
        else:
            mgr.page.wait_for_load_state("networkidle", timeout=timeout)
            action = "Page reached network idle"

        elapsed = time.time() - start
        return ToolResult(action, elapsed=elapsed, metadata={"action": "wait"})
    except Exception as e:
        return ToolResult(f"Wait error: {e}", is_error=True, elapsed=time.time() - start)


def exec_browser_back(args: dict, start: float) -> ToolResult:
    """Navigate back in browser history."""
    mgr = get_browser()
    err = mgr.ensure_page()
    if err:
        return ToolResult(err, is_error=True, elapsed=time.time() - start)

    try:
        mgr.page.go_back(wait_until="domcontentloaded", timeout=10000)
        url = mgr.page.url
        title = mgr.page.title()
        elapsed = time.time() - start
        return ToolResult(
            f"Navigated back to: {url}\nTitle: {title}",
            elapsed=elapsed,
            metadata={"url": url, "title": title},
        )
    except Exception as e:
        return ToolResult(f"Back navigation error: {e}", is_error=True, elapsed=time.time() - start)


def exec_browser_tabs(args: dict, start: float) -> ToolResult:
    """List, switch, create, or close browser tabs."""
    mgr = get_browser()
    err = mgr.ensure_page()
    if err and args.get("action") != "list":
        return ToolResult(err, is_error=True, elapsed=time.time() - start)

    action = args.get("action", "list")

    try:
        if not mgr._context:
            return ToolResult("No browser context", is_error=True, elapsed=time.time() - start)

        pages = mgr._context.pages

        if action == "list":
            lines = []
            for i, p in enumerate(pages):
                current = " (active)" if p == mgr._page else ""
                lines.append(f"  [{i}] {p.url} — {p.title()}{current}")
            elapsed = time.time() - start
            return ToolResult(
                f"{len(pages)} tab(s):\n" + "\n".join(lines),
                elapsed=elapsed,
                metadata={"tab_count": len(pages)},
            )

        elif action == "switch":
            idx = args.get("index", 0)
            if 0 <= idx < len(pages):
                mgr._page = pages[idx]
                mgr._page.bring_to_front()
                url = mgr._page.url
                elapsed = time.time() - start
                return ToolResult(f"Switched to tab [{idx}]: {url}", elapsed=elapsed)
            return ToolResult(f"Invalid tab index {idx} (have {len(pages)} tabs)",
                            is_error=True, elapsed=time.time() - start)

        elif action == "new":
            url = args.get("url", "about:blank")
            new_page = mgr._context.new_page()
            if url != "about:blank":
                new_page.goto(url, wait_until="domcontentloaded", timeout=30000)
            mgr._page = new_page
            elapsed = time.time() - start
            return ToolResult(f"Opened new tab: {new_page.url}", elapsed=elapsed)

        elif action == "close":
            if len(pages) <= 1:
                return ToolResult("Cannot close the last tab", is_error=True,
                                elapsed=time.time() - start)
            mgr._page.close()
            mgr._page = mgr._context.pages[-1]
            elapsed = time.time() - start
            return ToolResult(f"Closed tab. Active: {mgr._page.url}", elapsed=elapsed)

        return ToolResult(f"Unknown action: {action}", is_error=True, elapsed=time.time() - start)
    except Exception as e:
        return ToolResult(f"Tabs error: {e}", is_error=True, elapsed=time.time() - start)


# ── Accessibility tree formatter ─────────────────────────────────────

def _format_accessibility_tree(node: dict, indent: int = 0) -> str:
    """Format Playwright accessibility snapshot into readable text."""
    lines = []
    role = node.get("role", "")
    name = node.get("name", "")
    value = node.get("value", "")

    prefix = "  " * indent
    parts = [role]
    if name:
        parts.append(f'"{name}"')
    if value:
        parts.append(f'value="{value}"')

    if role not in ("none", "generic", "") or name:
        lines.append(f"{prefix}{' '.join(parts)}")

    for child in node.get("children", []):
        lines.append(_format_accessibility_tree(child, indent + 1))

    return "\n".join(lines)
