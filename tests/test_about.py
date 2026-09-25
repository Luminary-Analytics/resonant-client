"""Settings > About Lumi: version, license, plan and who manages the copy."""
from __future__ import annotations

from lumi import __version__, policy


def test_about_says_what_this_copy_is(tmp_path):
    from lumi.gui.settings import SettingsManager
    from tests.test_connections import _command

    settings = SettingsManager(tmp_path / "settings.json")
    [reply] = _command(settings, "about_info")
    assert reply["event"] == "about_info"
    data = reply["data"]
    assert (data["version"], data["license"], data["organization"], data["installed_by"]) == (__version__, "MIT", "", "")
    assert data["notices"] == ""  # running from source: no bundled notices
    policy.set_for_tests(policy.parse({"schema": policy.SCHEMA, "organization": "Example Corp"}, source="test"))
    [reply] = _command(settings, "about_info")
    assert reply["data"]["organization"] == "Example Corp"
