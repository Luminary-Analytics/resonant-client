"""Guards for the vendored frontend assets (v0.11.14 startup pass).

index.html used to pull five render-blocking resources from two CDNs
(fonts.googleapis.com, cdn.jsdelivr.net) on every launch. They are now
fetched and SHA-256 verified at build time into `gui/static/vendor/` by
packaging/fetch_web_assets.ps1.

These tests lock the properties that made the change worth doing, because
each one is silent when it regresses: a re-added CDN link still *works* on
a developer machine with fast internet, and a stale cached library still
*renders*.
"""

import json
import re
from pathlib import Path

import pytest

REPO = Path(__file__).parents[1]
TEMPLATE = REPO / "resonant_client" / "gui" / "templates" / "index.html"
APP_JS = REPO / "resonant_client" / "gui" / "static" / "app.js"
POLICY = REPO / "packaging" / "bundle-policy.json"
STATIC = REPO / "resonant_client" / "gui" / "static"


def test_template_loads_nothing_from_the_network():
    """No external URL may reappear in the page.

    This is the whole point of the vendoring: startup must not depend on
    reaching a CDN. A re-added `<link>` or `<script>` pointing at a remote
    host is invisible in development and costs every user a render-blocking
    round trip (the Google Fonts stylesheet alone measured 273 ms).
    """
    html = TEMPLATE.read_text(encoding="utf-8")
    offenders = [
        line.strip()
        for line in html.splitlines()
        if "http://" in line or "https://" in line
    ]
    assert offenders == [], (
        "index.html must not reference external hosts; found:\n  "
        + "\n  ".join(offenders)
    )


def test_bundle_policy_requires_every_vendored_asset():
    """The shipped installer must contain the libraries the page loads.

    Without this the app degrades exactly like the psutil and ripgrep bugs
    did: fine for developers, broken for users, silent either way. Here the
    failure mode is a chat pane that renders raw markdown source.
    """
    required = set(json.loads(POLICY.read_text(encoding="utf-8"))["required_globs"])
    for name in (
        "marked.min.js",
        "highlight.min.js",
        "purify.min.js",
        "github-dark-dimmed.min.css",
    ):
        path = f"_internal/resonant_client/gui/static/vendor/{name}"
        assert path in required, f"bundle-policy.json must require {path}"


def test_render_markdown_refuses_to_inject_unsanitized_html():
    """`renderMarkdown` must not reach innerHTML when DOMPurify is missing.

    marked passes raw inline HTML straight through, and renderMarkdown is
    fed model output and tool output — including file contents the agent
    just read. The sanitizer used to come from a CDN and now comes from
    static/vendor/, which is absent when running from source without first
    running packaging/fetch_web_assets.ps1. That made "DOMPurify undefined"
    a reachable state rather than a theoretical one, so the fallback must
    degrade to text rather than injecting markup.
    """
    src = APP_JS.read_text(encoding="utf-8")
    start = src.index("renderMarkdown(el, text, streaming = false)")
    body = src[start:start + 2000]

    assert "sanitized = true" in body, (
        "renderMarkdown should track whether DOMPurify actually ran"
    )
    # The innerHTML assignment must be guarded by that flag, with a
    # textContent fallback for the unsanitized case. Matched with a
    # whitespace-tolerant regex so reindenting the method doesn't fail this.
    guarded = re.search(
        r"if\s*\(\s*sanitized\s*\)\s*\{\s*contentEl\.innerHTML\s*=\s*html\s*;",
        body,
    )
    assert guarded, (
        "renderMarkdown must only assign innerHTML when DOMPurify sanitized "
        "the string; found:\n" + body[body.index("let sanitized"):][:600]
    )
    assert re.search(r"contentEl\.textContent\s*=\s*text\s*;", body)


def test_asset_version_busts_cache_when_a_vendored_library_changes():
    """Re-pinning a library must invalidate clients' cached copies.

    Vendored filenames are stable across upgrades — marked.min.js stays
    marked.min.js — so bumping a pin in fetch_web_assets.ps1 changes the
    bytes but not the URL. If the cache-buster ignored `vendor/`, a version
    bump that touched no top-level asset would leave every existing client
    on the old library indefinitely.
    """
    from resonant_client.gui.app import _asset_version

    vendor = STATIC / "vendor"
    created_dir = not vendor.exists()
    vendor.mkdir(parents=True, exist_ok=True)
    probe = vendor / "__asset_version_probe__.js"
    try:
        before = _asset_version()
        probe.write_text("// probe\n", encoding="utf-8")
        # Push the mtime well past every other asset so the assertion tests
        # the glob rather than filesystem timestamp granularity.
        import os
        import time

        future = time.time() + 10_000
        os.utime(probe, (future, future))

        assert _asset_version() != before, (
            "_asset_version() must include static/vendor/* in its mtime scan"
        )
    finally:
        probe.unlink(missing_ok=True)
        if created_dir:
            try:
                vendor.rmdir()
            except OSError:
                pass


@pytest.mark.skipif(
    not (STATIC / "vendor").is_dir(),
    reason="vendor/ is gitignored and only present after fetch_web_assets.ps1",
)
def test_vendored_files_are_present_and_non_empty_when_fetched():
    """Sanity check for a developer machine that has run the fetch script."""
    vendor = STATIC / "vendor"
    for name in ("marked.min.js", "highlight.min.js", "purify.min.js"):
        path = vendor / name
        assert path.is_file(), f"{name} missing from {vendor}"
        assert path.stat().st_size > 1000, f"{name} looks truncated"
