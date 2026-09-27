"""Update feeds on the Pages site: stable, beta and one per release line (packaging/update_appcast.py)."""
from __future__ import annotations

import base64
import importlib.util
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from lumi import update_channels
from lumi.updater import APPCAST_URL, MACOS_APPCAST_URL

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
    _mac_release(feeds, site, tmp_path, "0.21.0")
    for platform in ("windows", "macos"):
        for channel, pin in (("stable", ""), ("beta", ""), ("stable", "0.21"), ("beta", "0.21")):
            name = update_channels.feed_name(channel, pin, platform)
            assert (site / name).exists(), name
            prefs = update_channels.UpdatePreferences(channel=channel, pin=pin, platform=platform)
            assert prefs.feed_url == update_channels.FEED_BASE + name
    # The stable feed is the one every earlier install polls.
    assert update_channels.FEED_BASE + "appcast.xml" == APPCAST_URL
    assert update_channels.FEED_BASE + "appcast-macos.xml" == MACOS_APPCAST_URL


# ── macOS: disk images in feeds of their own ────────────────────────────────

def _mac_release(feeds, site, tmp_path, version, **kwargs):
    image = tmp_path / f"lumi-{version}.dmg"
    image.write_bytes(b"d" * 12)
    return feeds.publish_feeds(site, version, image, "bWFjc2ln", f"<p>Lumi {version}</p>", BASE,
                               platform="macos", **kwargs)


def test_macos_releases_leave_the_windows_feeds_as_they_are(site, tmp_path):
    feeds = _load()
    _release(feeds, site, tmp_path, "0.21.0")
    windows = {path.name: path.read_bytes() for path in site.glob("appcast*.xml")}
    written = _mac_release(feeds, site, tmp_path, "0.21.0")
    assert {p.name for p in written} == {"appcast-macos.xml", "appcast-macos-beta.xml", "appcast-macos-0.21.xml"}
    assert {path.name: path.read_bytes() for path in site.glob("appcast*.xml") if "macos" not in path.name} == windows
    # Nothing for Windows mentions a disk image or another OS.
    for name, data in windows.items():
        assert b".dmg" not in data and b"sparkle:os" not in data and b"minimumSystemVersion" not in data, name

    channel = ET.parse(site / "appcast-macos.xml").getroot().find("channel")
    assert channel.findtext("title") == "Lumi updates for macOS"
    assert channel.findtext("link") == "https://luminary-analytics.github.io/resonant-client/"
    [item] = channel.findall("item")
    assert item.findtext(f"{SPARKLE}version") == "0.21.0"
    assert item.findtext(f"{SPARKLE}shortVersionString") == "0.21.0"
    assert item.findtext(f"{SPARKLE}minimumSystemVersion") == "12.0"
    enclosure = item.find("enclosure")
    assert enclosure.get("url") == f"{BASE}/v0.21.0/lumi-0.21.0.dmg"
    assert enclosure.get(f"{SPARKLE}os") == "macos"
    assert enclosure.get(f"{SPARKLE}edSignature") == "bWFjc2ln" and enclosure.get("length") == "12"


def test_macos_betas_carry_the_version_sparkle_compares(site, tmp_path):
    feeds = _load()
    # The first macOS release can be a beta: the stable feed starts empty, so
    # the stable channel finds a feed with nothing newer rather than none.
    written = _mac_release(feeds, site, tmp_path, "0.22.0-beta.1")
    assert {p.name for p in written} == {"appcast-macos.xml", "appcast-macos-beta.xml"}
    assert ET.parse(site / "appcast-macos.xml").getroot().find("channel").findall("item") == []
    [item] = ET.parse(site / "appcast-macos-beta.xml").getroot().find("channel").findall("item")
    # Sparkle ignores what follows a dash, so the compared version has none;
    # the name people see keeps it.
    assert item.findtext(f"{SPARKLE}version") == "0.22.0beta.1"
    assert item.findtext(f"{SPARKLE}shortVersionString") == "0.22.0-beta.1"
    assert item.find("enclosure").get(f"{SPARKLE}version") == "0.22.0beta.1"
    stable_before = (site / "appcast-macos.xml").read_bytes()
    _mac_release(feeds, site, tmp_path, "0.22.0-beta.2")
    assert (site / "appcast-macos.xml").read_bytes() == stable_before
    assert _mac_versions(site / "appcast-macos-beta.xml") == ["0.22.0-beta.2", "0.22.0-beta.1"]
    # The release retires its betas, as on Windows.
    _mac_release(feeds, site, tmp_path, "0.22.0")
    assert _mac_versions(site / "appcast-macos-beta.xml") == ["0.22.0"]
    assert _mac_versions(site / "appcast-macos.xml") == ["0.22.0"]
    assert _mac_versions(site / "appcast-macos-0.22.xml") == ["0.22.0"]


def _mac_versions(path: Path) -> list[str]:
    channel = ET.parse(path).getroot().find("channel")
    return [item.findtext(f"{SPARKLE}shortVersionString") for item in channel.findall("item")]


def test_feeds_are_the_same_bytes_on_every_platform(site, tmp_path):
    # "\n" line ends, also on Windows, where the release publishes: the macOS
    # feeds are signed over exactly these bytes.
    feeds = _load()
    written = _release(feeds, site, tmp_path, "0.21.0") + _mac_release(feeds, site, tmp_path, "0.21.0")
    assert {"appcast.xml", "appcast-beta.xml", "appcast-macos.xml", "appcast-macos-beta.xml"} <= {
        path.name for path in written}
    for path in written:
        data = path.read_bytes()
        assert data.startswith(b"<?xml version='1.0' encoding='utf-8'?>\n<rss"), path.name
        assert b"\r" not in data, path.name


def test_the_macos_bundle_version_drops_only_the_dash():
    feeds = _load()
    assert feeds.macos_bundle_version("0.21.0") == "0.21.0"
    assert feeds.macos_bundle_version("0.21.0-beta.1") == "0.21.0beta.1"
    assert feeds.macos_bundle_version("0.21.0-rc.2") == "0.21.0rc.2"
    assert feeds.macos_bundle_version("0.19.2.dev11") == "0.19.2.dev11"


def test_the_app_and_its_feed_items_agree(tmp_path):
    # Lumi.app's Info.plist (packaging/lumi.spec) and the feed items are compared by Sparkle.
    feeds = _load()
    spec = (ROOT / "packaging" / "lumi.spec").read_text(encoding="utf-8")
    assert f'"LSMinimumSystemVersion": "{feeds.MACOS_MINIMUM_SYSTEM_VERSION}"' in spec
    assert '"CFBundleVersion": _appcast.macos_bundle_version(_version)' in spec
    assert '"SUPublicEDKey": _eddsa_public_key' in spec and '"SUFeedURL": _macos_feed' in spec
    with pytest.raises(ValueError, match="platform"):
        feeds.publish_feeds(tmp_path, "0.21.0", Path(__file__), "c2ln", "", BASE, platform="linux")


# ── Signed macOS feeds (packaging/feed_signature.py; Sparkle's SURequireSignedFeed) ──


def _feed_signature():
    spec = importlib.util.spec_from_file_location("lumi_feed_signature", ROOT / "packaging" / "feed_signature.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _key():
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

    key = Ed25519PrivateKey.generate()
    return key, base64.b64encode(key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)).decode()


def _signature(key, data: bytes) -> str:
    return base64.b64encode(key.sign(data)).decode()


def test_a_signed_macos_feed_carries_sparkles_block_and_still_reads(site, tmp_path):
    feeds, signing = _load(), _feed_signature()
    key, public = _key()
    _mac_release(feeds, site, tmp_path, "0.21.0")
    feed = site / "appcast-macos.xml"
    unsigned = feed.read_bytes()
    signature = _signature(key, unsigned)
    signing.attach(feed, signature)
    signed = feed.read_bytes()
    # What Sparkle's sign_update appends, after the feed's own bytes, which are what was signed.
    assert signed == unsigned + (f"<!-- sparkle-signatures:\nedSignature: {signature}\n"
                                 f"length: {len(unsigned)}\n-->\n").encode()
    assert signing.split(signed) == (unsigned, signature, len(unsigned))
    assert signing.verify(feed, public)
    # A comment after the feed's root: the client and the next release read it as before.
    assert _mac_versions(feed) == ["0.21.0"]
    with pytest.raises(ValueError, match="already"):
        signing.attach(feed, signature)
    # The next release writes its feeds afresh, unsigned, for publish_macos.ps1 to sign again.
    _mac_release(feeds, site, tmp_path, "0.21.1")
    assert signing.PREFIX not in feed.read_bytes()
    assert _mac_versions(feed) == ["0.21.1", "0.21.0"]


def test_a_feed_that_doesnt_match_its_signature_fails(site, tmp_path):
    feeds, signing = _load(), _feed_signature()
    key, public = _key()
    other, other_public = _key()
    _mac_release(feeds, site, tmp_path, "0.21.0")
    feed = site / "appcast-macos.xml"
    unsigned = feed.read_bytes()
    assert not signing.verify(feed, public)  # no block at all
    signing.attach(feed, _signature(key, unsigned))
    signed = feed.read_bytes()
    assert signing.verify(feed, public) and not signing.verify(feed, other_public)
    # Another download address, of the same length: the signature no longer covers it.
    feed.write_bytes(signed.replace(b"lumi-0.21.0.dmg", b"lumi-0.21.9.dmg", 1))
    assert not signing.verify(feed, public)
    # Content added before the block: the stated length is wrong.
    feed.write_bytes(signed.replace(b"</rss>", b"</rss>\n", 1))
    assert not signing.verify(feed, public)
    with pytest.raises(ValueError, match="Ed25519"):
        signing.attach(site / "appcast-macos-beta.xml", "bm90IGEgc2lnbmF0dXJl")


def test_a_feed_can_be_signed_again(site, tmp_path, capsys):
    # A key rotation, or a publish that changed a feed's bytes after signing
    # (publish_macos.ps1 -ResignFeeds): the old block goes, whatever its line
    # ends, and the feed is signed over what's left.
    feeds, signing = _load(), _feed_signature()
    old, _ = _key()
    new, new_public = _key()
    _mac_release(feeds, site, tmp_path, "0.21.0")
    feed = site / "appcast-macos.xml"
    unsigned = feed.read_bytes()
    signing.attach(feed, _signature(old, unsigned))
    assert signing.strip(feed.read_bytes()) == unsigned
    assert signing.strip(feed.read_bytes().replace(b"\n", b"\r\n")) == unsigned.replace(b"\n", b"\r\n")
    assert signing.strip(unsigned) == unsigned
    assert signing.main(["strip", str(feed)]) == 0 and feed.read_bytes() == unsigned
    signing.attach(feed, _signature(new, unsigned))
    assert signing.verify(feed, new_public)


def test_the_feed_signature_command_line(site, tmp_path, capsys):
    feeds, signing = _load(), _feed_signature()
    key, public = _key()
    _mac_release(feeds, site, tmp_path, "0.21.0")
    feed = site / "appcast-macos.xml"
    unsigned = feed.read_bytes()
    content = tmp_path / "content.xml"
    assert signing.main(["split", str(feed), str(content)]) == 1  # nothing signed yet
    assert signing.main(["verify", str(feed), public]) == 1
    signature = _signature(key, unsigned)
    assert signing.main(["attach", str(feed), signature]) == 0
    capsys.readouterr()
    # What publish_macos.ps1 checks with winsparkle-tool: the signed content and its signature.
    assert signing.main(["split", str(feed), str(content)]) == 0
    assert capsys.readouterr().out.strip() == signature and content.read_bytes() == unsigned
    assert signing.main(["verify", str(feed), public]) == 0
    assert signing.main(["attach", str(feed), signature]) == 1  # already signed
