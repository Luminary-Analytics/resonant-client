"""Update feeds on the Pages site: stable, beta and one per release line (packaging/update_appcast.py)."""
from __future__ import annotations

import importlib.util
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from lumi import update_channels
from lumi.updater import APPCAST_URL

ROOT = Path(__file__).resolve().parents[1]
SPARKLE = "{http://www.andymatuschak.org/xml-namespaces/sparkle}"
BASE = "https://luminary-analytics.github.io/resonant-client/downloads"

LIVE_LIKE = """<?xml version='1.0' encoding='utf-8'?>
<rss xmlns:sparkle="http://www.andymatuschak.org/xml-namespaces/sparkle" version="2.0">
    <channel>
        <title>Resonant Client Updates</title>
        <link>https://luminary-analytics.github.io/resonant-client/</link>
        <description>Auto-update channel for Resonant Client</description>
        <language>en</language>
        <item>
            <title>Version 0.19.1</title>
            <sparkle:version>0.19.1</sparkle:version>
            <enclosure url="https://example.invalid/v0.19.1.exe" sparkle:version="0.19.1" length="1" type="application/octet-stream" sparkle:edSignature="c2ln" />
        </item>
        <item>
            <title>Version 0.18.2</title>
            <sparkle:version>0.18.2</sparkle:version>
            <enclosure url="https://example.invalid/v0.18.2.exe" sparkle:version="0.18.2" length="1" type="application/octet-stream" sparkle:edSignature="c2ln" />
        </item>
    </channel>
</rss>
"""


def _load():
    spec = importlib.util.spec_from_file_location("lumi_update_appcast", ROOT / "packaging" / "update_appcast.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def site(tmp_path):
    root = tmp_path / "site"
    root.mkdir()
    (root / "appcast.xml").write_text(LIVE_LIKE, encoding="utf-8")
    return root


def _release(feeds, site, tmp_path, version, **kwargs):
    installer = tmp_path / f"lumi-setup-{version}.exe"
    installer.write_bytes(b"x" * 10)
    return feeds.publish_feeds(site, version, installer, "c2lnbmF0dXJl", f"<p>Lumi {version}</p>", BASE, **kwargs)


def _versions(path: Path) -> list[str]:
    channel = ET.parse(path).getroot().find("channel")
    return [item.findtext(f"{SPARKLE}version") for item in channel.findall("item")]


def test_versions_order_like_winsparkle():
    feeds = _load()
    ordered = ["0.21.0-alpha.1", "0.21.0-beta.1", "0.21.0-beta.2", "0.21.0-rc.1", "0.21.0", "0.21.1", "1.0.0"]
    assert sorted(reversed(ordered), key=feeds.version_key) == ordered
    for bad in ("0.21", "0.21.0b1", "0.21.0-beta", "v0.21.0", "0.19.2.dev11"):
        with pytest.raises(ValueError):
            feeds.version_key(bad)


def test_a_stable_release_updates_every_feed(site, tmp_path):
    feeds = _load()
    written = _release(feeds, site, tmp_path, "0.21.0")
    assert {p.name for p in written} == {"appcast.xml", "appcast-beta.xml", "appcast-0.21.xml",
                                         "appcast-0.19.xml", "appcast-0.18.xml"}
    assert _versions(site / "appcast.xml") == ["0.21.0", "0.19.1", "0.18.2"]
    assert _versions(site / "appcast-beta.xml") == ["0.21.0", "0.19.1", "0.18.2"]
    assert _versions(site / "appcast-0.21.xml") == ["0.21.0"]
    stable = ET.parse(site / "appcast.xml").getroot().find("channel")
    # The stable feed keeps its title; the new entry points at the Pages copy.
    assert stable.findtext("title") == "Resonant Client Updates"
    enclosure = stable.find("item").find("enclosure")
    assert enclosure.get("url") == f"{BASE}/v0.21.0/lumi-setup-0.21.0.exe"
    assert enclosure.get(f"{SPARKLE}edSignature") == "c2lnbmF0dXJl" and enclosure.get("length") == "10"
    assert ET.parse(site / "appcast-beta.xml").getroot().find("channel").findtext("title") == "Lumi updates (beta)"
    # Running the same release again replaces its entry.
    _release(feeds, site, tmp_path, "0.21.0")
    assert _versions(site / "appcast.xml") == ["0.21.0", "0.19.1", "0.18.2"]


def test_a_beta_reaches_only_the_beta_feed_until_its_release(site, tmp_path):
    feeds = _load()
    _release(feeds, site, tmp_path, "0.21.0")
    stable_before = (site / "appcast.xml").read_bytes()
    written = _release(feeds, site, tmp_path, "0.22.0-beta.1")
    assert "appcast.xml" not in {p.name for p in written}
    assert (site / "appcast.xml").read_bytes() == stable_before
    assert _versions(site / "appcast-beta.xml") == ["0.22.0-beta.1", "0.21.0", "0.19.1", "0.18.2"]
    assert not (site / "appcast-0.22.xml").exists()
    _release(feeds, site, tmp_path, "0.22.0-beta.2")
    assert _versions(site / "appcast-beta.xml")[:2] == ["0.22.0-beta.2", "0.22.0-beta.1"]
    # The release retires its betas.
    _release(feeds, site, tmp_path, "0.22.0")
    assert _versions(site / "appcast-beta.xml") == ["0.22.0", "0.21.0", "0.19.1", "0.18.2"]
    assert _versions(site / "appcast-0.22.xml") == ["0.22.0"]


def test_only_the_newest_release_lines_get_a_feed(site, tmp_path):
    feeds = _load()
    for version in ("0.20.0", "0.21.0", "0.21.1"):
        _release(feeds, site, tmp_path, version, lines=2)
    assert _versions(site / "appcast-0.21.xml") == ["0.21.1", "0.21.0"]
    assert _versions(site / "appcast-0.20.xml") == ["0.20.0"]
    # A fix for a pinned, older line lands in that line's feed.
    _release(feeds, site, tmp_path, "0.20.1", lines=2)
    assert _versions(site / "appcast-0.20.xml") == ["0.20.1", "0.20.0"]
    assert _versions(site / "appcast.xml")[0] == "0.21.1"


def test_nothing_is_published_unsigned_or_for_a_bad_version(site, tmp_path):
    feeds = _load()
    installer = tmp_path / "lumi-setup-0.21.0.exe"
    installer.write_bytes(b"x")
    with pytest.raises(ValueError, match="unsigned"):
        feeds.publish_feeds(site, "0.21.0", installer, "", "", BASE)
    with pytest.raises(ValueError, match="release version"):
        feeds.publish_feeds(site, "0.21.0.dev1", installer, "c2ln", "", BASE)
    assert _versions(site / "appcast.xml") == ["0.19.1", "0.18.2"]


def test_the_client_reads_the_feeds_this_writes(site, tmp_path):
    feeds = _load()
    _release(feeds, site, tmp_path, "0.21.0")
    for channel, pin in (("stable", ""), ("beta", ""), ("stable", "0.21"), ("beta", "0.21")):
        name = update_channels.feed_name(channel, pin)
        assert (site / name).exists(), name
        prefs = update_channels.UpdatePreferences(channel=channel, pin=pin)
        assert prefs.feed_url == update_channels.FEED_BASE + name
    # The stable feed is the one every earlier install polls.
    assert update_channels.FEED_BASE + "appcast.xml" == APPCAST_URL
