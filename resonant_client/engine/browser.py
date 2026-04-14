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
import time
import logging
from typing import Optional
from .tools import ToolResult

logger = logging.getLogger(__name__)

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

    # Auto-launch if not connected
    if not mgr.is_connected:
        result = mgr.launch(headless=False)
        if "Error" in result:
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
            # Click by visible text (most natural for LLMs)
            mgr.page.get_by_text(text, exact=False).first.click(timeout=5000)
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
            snapshot = mgr.page.accessibility.snapshot()
            content = _format_accessibility_tree(snapshot) if snapshot else "(empty page)"
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
