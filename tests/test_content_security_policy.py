"""The app page's Content-Security-Policy, and the markup it constrains.

The page holds the launch's access token and renders model output and file
contents. Its policy (lumi/gui/local_access.py, content_security_policy)
lets only this server's own scripts and styles run in it and lets it connect
only to this server. Markup that depends on anything else breaks silently
under that policy: an inline handler never fires, and an element that a
style="" attribute hid shows up. The last test keeps the page and its scripts
inside the policy.
"""

import re
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from lumi.gui import app as gui
from lumi.gui.local_access import content_security_policy
from tests.gui_access import BASE_URL

REPO = Path(__file__).parents[1]
TEMPLATE = REPO / "lumi" / "gui" / "templates" / "index.html"
STATIC = REPO / "lumi" / "gui" / "static"


def _directives(policy: str) -> dict[str, list[str]]:
    parsed = {}
    for directive in policy.split(";"):
        name, *sources = directive.split()
        assert name not in parsed, f"{name} appears twice"
        parsed[name] = sources
    return parsed


@pytest.fixture
def gui_state(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.delenv("LUMI_STATE_HOME", raising=False)
    state = gui.AppState()
    monkeypatch.setattr(gui, "state", state)
    return state


@pytest.mark.parametrize("host", ["127.0.0.1:48123", "localhost:48123"])
def test_page_runs_only_this_servers_code_and_connects_only_to_it(gui_state, host):
    with TestClient(gui.app, base_url=BASE_URL) as client:
        page = client.get("/", headers={"Host": host})

    assert page.status_code == 200
    assert _directives(page.headers["content-security-policy"]) == {
        "default-src": ["'self'"],
        # No 'unsafe-inline' or 'unsafe-eval': injected markup runs nothing.
        "script-src": ["'self'"],
        "style-src": ["'self'"],
        # Screenshots and attachments; a remote image could carry data away.
        "img-src": ["'self'", "data:", "blob:"],
        # The app socket on this port, under either name the server accepts.
        "connect-src": ["'self'", "ws://127.0.0.1:48123", "ws://localhost:48123"],
        "object-src": ["'none'"],
        "base-uri": ["'none'"],
        "form-action": ["'none'"],
        "frame-ancestors": ["'none'"],
    }
    assert page.headers["x-frame-options"] == "DENY"


def test_socket_sources_follow_the_listening_address():
    def sockets(scope):
        return _directives(content_security_policy(scope))["connect-src"]

    assert sockets({"server": ("10.0.0.5", 5000), "scheme": "http"}) == [
        "'self'", "ws://10.0.0.5:5000", "ws://127.0.0.1:5000", "ws://localhost:5000"]
    assert sockets({"server": ("127.0.0.1", 5000), "scheme": "https"}) == [
        "'self'", "wss://127.0.0.1:5000", "wss://localhost:5000"]
    # A bracketed IPv6 literal is not a valid source; 'self' covers the page's own address.
    assert sockets({"server": ("::1", 5000), "scheme": "http"}) == [
        "'self'", "ws://127.0.0.1:5000", "ws://localhost:5000"]
    assert sockets({"server": None}) == ["'self'"]


# What the policy refuses, as it would appear in the template or in markup the
# scripts build. Comments mention style="" (empty), so a style attribute counts
# only with a value.
REFUSED = {
    "style attribute": re.compile(r"""\bstyle\s*=\s*(["'])(?!\1)"""),
    "setAttribute('style')": re.compile(r"""setAttribute\(\s*["'`]style["'`]"""),
    "style.cssText": re.compile(r"\.cssText\b"),
    "event-handler attribute": re.compile(r"<[a-zA-Z][^<>]*\son[a-z]+\s*=", re.S),
    "inline script": re.compile(r"<script\b(?![^>]*\bsrc\s*=)", re.I),
    "javascript: URL": re.compile(r"javascript:", re.I),
    "eval": re.compile(r"\beval\s*\(|\bFunction\s*\("),
    "string timer": re.compile(r"\bset(?:Timeout|Interval)\s*\(\s*[\"'`]"),
}


def test_page_and_scripts_use_nothing_the_policy_refuses():
    offenders = []
    for path in [TEMPLATE, *sorted(STATIC.glob("*.js"))]:
        text = path.read_text(encoding="utf-8")
        for rule, pattern in REFUSED.items():
            for match in pattern.finditer(text):
                line = text.count("\n", 0, match.start()) + 1
                offenders.append(f"{path.relative_to(REPO)}:{line}: {rule}")
    assert offenders == [], (
        "The page's Content-Security-Policy refuses these. Use a class, or set "
        "computed values through element.style, and addEventListener for "
        "handlers:\n  " + "\n  ".join(offenders)
    )
