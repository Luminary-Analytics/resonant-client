"""Tests for the native CDP browser tools.

These run without Chrome. The parts worth guarding here are the ones that fail
silently in production: tool registration, argument validation, and the
packaging wiring that decides whether the tab-group extension exists in a
shipped install at all.

The protocol itself is exercised against real Chrome by hand — see the
v0.11.15 release notes for what was verified.
"""

import json
from pathlib import Path

import pytest

from resonant_client.engine import browser
from resonant_client.engine.tools import AGENT_TOOLS, execute_tool

REPO = Path(__file__).parents[1]
EXTENSION = REPO / "resonant_client" / "browser_extension"

BROWSER_TOOLS = [
    "browser_navigate", "browser_click", "browser_type", "browser_read",
    "browser_screenshot", "browser_js", "browser_scroll", "browser_hover",
    "browser_select", "browser_wait", "browser_back", "browser_tabs",
]


def test_every_browser_tool_is_registered_and_dispatchable():
    """A schema with no dispatch branch is advertised and then fails at use."""
    registered = {t["function"]["name"] for t in AGENT_TOOLS}
    for name in BROWSER_TOOLS:
        assert name in registered, f"{name} is missing from AGENT_TOOLS"
        assert hasattr(browser, f"exec_{name}"), f"exec_{name} is not implemented"


@pytest.mark.parametrize(
    ("tool", "args", "expected"),
    [
        ("browser_navigate", {}, "'url' is required"),
        ("browser_click", {}, "'text', 'selector', or 'x'/'y' is required"),
        ("browser_type", {"text": "x"}, "'selector' is required"),
        ("browser_hover", {}, "'selector' is required"),
        ("browser_select", {"value": "x"}, "'selector' is required"),
        ("browser_js", {}, "'code' is required"),
    ],
)
def test_missing_arguments_fail_before_launching_chrome(tool, args, expected, monkeypatch):
    """Validation must precede browser startup.

    Launching Chrome to discover an argument is missing costs seconds and pops
    a window; a malformed call from the model should cost neither.
    """
    def _boom():
        raise AssertionError(f"{tool} started the browser despite bad arguments")

    monkeypatch.setattr(browser, "get_browser", _boom)
    result = execute_tool(tool, args)
    assert result.is_error
    assert expected in result.output


def test_bare_domains_become_https():
    assert browser._normalize_url("example.com") == "https://example.com"
    assert browser._normalize_url("http://x.dev") == "http://x.dev"
    assert browser._normalize_url("https://x.dev") == "https://x.dev"
    assert browser._normalize_url("about:blank") == "about:blank"


def test_profile_is_not_the_users_real_chrome_directory(monkeypatch):
    """Chrome locks a profile while it runs.

    Pointing at the user's own Chrome directory would mean Resonant and the
    user cannot both browse, and the launch would fail whenever Chrome was
    already open.
    """
    monkeypatch.delenv("RESONANT_BROWSER_USER_DATA_DIR", raising=False)
    profile = browser._profile_dir().replace("\\", "/").lower()
    assert "/.resonant/" in profile
    assert "google/chrome/user data" not in profile


def test_extension_ships_with_the_package():
    """The extension must be inside the package, not beside it.

    Anything outside `resonant_client/` needs its own spec entry to reach a
    packaged install, and a missing one is invisible until a user's tabs
    silently stop grouping.
    """
    manifest = json.loads((EXTENSION / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["manifest_version"] == 3
    assert set(manifest["permissions"]) >= {"tabs", "tabGroups"}
    assert manifest["background"]["service_worker"] == "background.js"
    assert (EXTENSION / "background.js").is_file()


def test_bundle_policy_requires_the_extension():
    """Otherwise a build that drops it still passes the gate."""
    policy = json.loads((REPO / "packaging" / "bundle-policy.json").read_text(encoding="utf-8"))
    required = set(policy["required_globs"])
    for name in ("manifest.json", "background.js"):
        path = f"_internal/resonant_client/browser_extension/{name}"
        assert path in required, f"bundle-policy.json must require {path}"


def test_pip_install_includes_the_extension():
    """package-data, not just the PyInstaller spec.

    The spec only governs the frozen .exe. Without a package-data entry a
    `pip install resonant-client` omits the extension entirely — non-Python
    files are not picked up automatically — and grouping fails with no error.
    """
    pyproject = (REPO / "pyproject.toml").read_text(encoding="utf-8")
    assert '"resonant_client" = ["browser_extension/*"]' in pyproject


def test_spec_bundles_the_extension():
    spec = (REPO / "packaging" / "resonant.spec").read_text(encoding="utf-8")
    assert 'PKG_ROOT / "browser_extension" / "manifest.json"' in spec
    assert 'PKG_ROOT / "browser_extension" / "background.js"' in spec


def test_extension_is_installed_over_cdp_not_just_the_launch_flag():
    """Chrome 137+ silently ignores --load-extension.

    Relying on the flag alone means the extension never loads on any current
    Chrome and grouping fails with no error anywhere — which is exactly how
    this was first written. `Extensions.loadUnpacked` is the supported path.
    """
    source = (REPO / "resonant_client" / "engine" / "browser.py").read_text(encoding="utf-8")
    assert "Extensions.loadUnpacked" in source
    assert "_load_extension" in source


def test_group_config_is_written_next_to_the_copied_extension(tmp_path, monkeypatch):
    """The label is staged per session rather than baked in at build time."""
    monkeypatch.setattr(browser, "_extension_source_dir", lambda: EXTENSION)
    staged = browser._prepare_extension(str(tmp_path), "My Run", "cyan")

    assert staged is not None
    config = (Path(staged) / "config.js").read_text(encoding="utf-8")
    assert '"My Run"' in config
    assert '"cyan"' in config
    # The extension itself must come along, not just the config.
    assert (Path(staged) / "manifest.json").is_file()
    assert (Path(staged) / "background.js").is_file()


def test_prepare_extension_reports_missing_source(tmp_path, monkeypatch):
    """A missing extension degrades grouping; it must not raise."""
    monkeypatch.setattr(browser, "_extension_source_dir", lambda: None)
    assert browser._prepare_extension(str(tmp_path), "x", "purple") is None
