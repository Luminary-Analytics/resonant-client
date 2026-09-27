"""
Add a release to the update feeds on the Pages site.

Pipeline (called from .github/workflows/release.yml):
    1. CI builds lumi-setup-X.Y.Z.exe (and on macOS lumi-X.Y.Z.dmg)
    2. CI signs it with winsparkle-tool using the EDDSA_PRIVATE_KEY secret
    3. CI uploads it to the GitHub Release for tag vX.Y.Z
    4. CI checks out the gh-pages branch into ./gh-pages-checkout
    5. publish_pages.py copies it to downloads/vX.Y.Z/
    6. THIS script adds the release to the feeds (below)
    7. CI publishes gh-pages-checkout as one fresh commit

Standalone usage (for local testing):
    python packaging/update_appcast.py \\
        --site gh-pages-checkout \\
        --version 0.21.0 \\
        --installer dist/installer/lumi-setup-0.21.0.exe \\
        --signature "BASE64_EDDSA_SIG" \\
        --notes "<p>Lumi 0.21.0.</p>" \\
        --download-base "https://luminary-analytics.github.io/resonant-client/downloads"

    Add --platform macos with the .dmg as --installer for the macOS feeds.

The feeds (lumi/update_channels.py picks one per install):
    appcast.xml         stable releases. Installed copies before 0.21 poll only
                        this one, so it keeps its address and history.
    appcast-beta.xml    beta releases (tags like v0.21.0-beta.1) newer than the
                        newest stable release, and every stable release.
    appcast-X.Y.xml     stable releases of one release line, for installs pinned
                        to it; written for the newest LINES lines.

macOS has the same three kinds, listing disk images for Sparkle:
appcast-macos.xml, appcast-macos-beta.xml and appcast-macos-X.Y.xml. They are
separate files so the Windows feeds that installed copies poll stay exactly as
they are: WinSparkle never sees a disk image, and Sparkle never sees an .exe
(it would take an enclosure without ``sparkle:os`` for its own). A macOS item
also says ``sparkle:os="macos"``, the minimum macOS version, and its version
in the form Sparkle compares (``macos_bundle_version``, as in Lumi.app's
CFBundleVersion); ``sparkle:shortVersionString`` keeps the release's name.

A stable release goes into the stable feed and a pre-release into the beta
feed; the beta and line feeds are then rebuilt from those two, so they can't
drift. Items are ordered newest first (WinSparkle and Sparkle pick by version
anyway). A version that is already listed is replaced, so a re-run of a
release job is harmless. Nothing is published without a signature.
"""

from __future__ import annotations

import argparse
import copy
import re
import sys
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import format_datetime
from pathlib import Path
from typing import NamedTuple

# Sparkle XML namespace — needed so ET emits the right `sparkle:` prefix.
SPARKLE_NS = "http://www.andymatuschak.org/xml-namespaces/sparkle"
ET.register_namespace("sparkle", SPARKLE_NS)
ET.register_namespace("dc", "http://purl.org/dc/elements/1.1/")

STABLE_FEED = "appcast.xml"
BETA_FEED = "appcast-beta.xml"
LINES = 4
VERSION = re.compile(r"(\d+)\.(\d+)\.(\d+)(?:-(alpha|beta|rc)\.(\d+))?")
_PRE_RANK = {"alpha": 0, "beta": 1, "rc": 2}
# Lumi.app's LSMinimumSystemVersion (packaging/lumi.spec); Sparkle skips an
# item a Mac can't run.
MACOS_MINIMUM_SYSTEM_VERSION = "12.0"


class FeedSet(NamedTuple):
    """One platform's feed files and the words in them.

    A NamedTuple, not a dataclass: the tests and packaging/lumi.spec load this
    script by path, without registering it as a module, which dataclasses need.
    """

    stable: str
    beta: str
    line: str  # with {line}
    title: str
    stable_description: str
    beta_description: str
    line_description: str  # with {line}


FEEDS = {
    "windows": FeedSet(STABLE_FEED, BETA_FEED, "appcast-{line}.xml", "Lumi updates", "Stable releases of Lumi",
                       "Beta and stable releases of Lumi", "Stable releases of Lumi {line}"),
    "macos": FeedSet("appcast-macos.xml", "appcast-macos-beta.xml", "appcast-macos-{line}.xml",
                     "Lumi updates for macOS", "Stable releases of Lumi for macOS",
                     "Beta and stable releases of Lumi for macOS", "Stable releases of Lumi {line} for macOS"),
}


def version_key(version: str) -> tuple[int, ...]:
    """Sort key: numbers first; a pre-release sorts before its release."""
    match = VERSION.fullmatch(version)
    if not match:
        raise ValueError(f"{version!r} is not a release version (X.Y.Z or X.Y.Z-beta.N)")
    major, minor, patch, pre, number = match.groups()
    tail = (0, _PRE_RANK[pre], int(number)) if pre else (1, 0, 0)
    return (int(major), int(minor), int(patch), *tail)


def is_prerelease(version: str) -> bool:
    return version_key(version)[3] == 0


def release_line(version: str) -> str:
    major, minor = version_key(version)[:2]
    return f"{major}.{minor}"


def macos_bundle_version(version: str) -> str:
    """The version Sparkle compares for a release: Lumi.app's CFBundleVersion and a macOS item's.

    Sparkle's comparator stops at the first dash, so ``0.21.0-beta.1`` would
    equal ``0.21.0`` and a beta would never update to its release. Without
    the dash (``0.21.0beta.1``) the suffix is a word, which Sparkle sorts
    before the release, and alpha < beta < rc. A development version such as
    ``0.19.2.dev11`` has no dash and stays as it is.
    """
    return version.replace("-", "")


def build_item(
    version: str,
    installer_path: Path,
    signature: str,
    notes_html: str,
    download_url: str,
    platform: str = "windows",
) -> ET.Element:
    """Build a new <item> element for this release."""
    pub_date = format_datetime(datetime.now(timezone.utc))
    file_size = installer_path.stat().st_size
    compared = macos_bundle_version(version) if platform == "macos" else version

    item = ET.Element("item")

    title = ET.SubElement(item, "title")
    title.text = f"Version {version}"

    pub = ET.SubElement(item, "pubDate")
    pub.text = pub_date

    sv = ET.SubElement(item, f"{{{SPARKLE_NS}}}version")
    sv.text = compared

    ssv = ET.SubElement(item, f"{{{SPARKLE_NS}}}shortVersionString")
    ssv.text = version

    if platform == "macos":
        minimum = ET.SubElement(item, f"{{{SPARKLE_NS}}}minimumSystemVersion")
        minimum.text = MACOS_MINIMUM_SYSTEM_VERSION

    desc = ET.SubElement(item, "description")
    # CDATA isn't natively supported by ElementTree — pass HTML as text and
    # post-process. WinSparkle accepts either way.
    desc.text = notes_html

    enclosure = {
        "url": download_url,
        f"{{{SPARKLE_NS}}}version": compared,
        f"{{{SPARKLE_NS}}}shortVersionString": version,
        "length": str(file_size),
        "type": "application/octet-stream",
        f"{{{SPARKLE_NS}}}edSignature": signature,
    }
    if platform == "macos":
        enclosure[f"{{{SPARKLE_NS}}}os"] = "macos"
    ET.SubElement(item, "enclosure", attrib=enclosure)
    return item


def item_version(item: ET.Element) -> str:
    """The release an item lists, as in its tag (``X.Y.Z`` or ``X.Y.Z-beta.N``).

    A Windows item's ``sparkle:version`` is that already. A macOS item's is the
    form Sparkle compares, so its ``sparkle:shortVersionString`` names it.
    """
    node = item.find(f"{{{SPARKLE_NS}}}version")
    version = (node.text or "").strip() if node is not None else ""
    enclosure = item.find("enclosure")
    if enclosure is not None and enclosure.get(f"{{{SPARKLE_NS}}}os") == "macos":
        short = item.find(f"{{{SPARKLE_NS}}}shortVersionString")
        version = (short.text or "").strip() if short is not None else ""
    return version


def _items(path: Path) -> list[ET.Element]:
    if not path.exists():
        return []
    channel = ET.parse(path).getroot().find("channel")
    return [] if channel is None else channel.findall("item")


def _known(items: list[ET.Element]) -> list[ET.Element]:
    """Items with a release version; anything else (hand edits) is left out of rebuilt feeds."""
    kept = []
    for item in items:
        try:
            version_key(item_version(item))
        except ValueError:
            continue
        kept.append(item)
    return kept


def _with(items: list[ET.Element], new_item: ET.Element) -> list[ET.Element]:
    version = item_version(new_item)
    return [item for item in items if item_version(item) != version] + [new_item]


def _newest_first(items: list[ET.Element]) -> list[ET.Element]:
    return sorted(items, key=lambda item: version_key(item_version(item)), reverse=True)


def _write(path: Path, template: ET.Element, title: str, description: str,
           items: list[ET.Element]) -> Path:
    """Write a feed with the stable feed's channel details, a title and these items."""
    rss = ET.Element("rss", attrib={"version": "2.0"})
    channel = ET.SubElement(rss, "channel")
    for child in template:
        if child.tag == "item":
            continue
        copied = copy.deepcopy(child)
        if child.tag == "title":
            copied.text = title
        elif child.tag == "description":
            copied.text = description
        channel.append(copied)
    for item in items:
        channel.append(copy.deepcopy(item))
    tree = ET.ElementTree(rss)
    ET.indent(tree, space="    ")
    tree.write(path, encoding="utf-8", xml_declaration=True)
    return path


def _new_channel(feeds: FeedSet, site_url: str) -> ET.Element:
    """Channel details for a platform's first feed (macOS; Windows has had appcast.xml all along)."""
    channel = ET.Element("channel")
    ET.SubElement(channel, "title").text = feeds.title
    ET.SubElement(channel, "link").text = site_url
    ET.SubElement(channel, "description").text = feeds.stable_description
    ET.SubElement(channel, "language").text = "en"
    return channel


def publish_feeds(
    site: Path,
    version: str,
    installer_path: Path,
    signature: str,
    notes_html: str,
    download_base: str,
    *,
    lines: int = LINES,
    platform: str = "windows",
) -> list[Path]:
    """Add one release to a platform's feeds in ``site`` and rebuild the derived feeds."""
    if not signature:
        raise ValueError("empty signature — refusing to publish an unsigned update")
    if not installer_path.exists():
        raise FileNotFoundError(f"installer not found at {installer_path}")
    if platform not in FEEDS:
        raise ValueError(f"{platform!r} isn't a platform with update feeds ({', '.join(FEEDS)})")
    version_key(version)  # a release version, or ValueError
    feeds = FEEDS[platform]
    stable_path = site / feeds.stable
    if stable_path.exists():
        template = ET.parse(stable_path).getroot().find("channel")
        if template is None:
            raise ValueError(f"<channel> element not found in {feeds.stable}")
    elif platform == "macos":
        # The first macOS release starts its feeds; the stable one is written
        # even for a beta, so the stable channel finds a feed with nothing new.
        template = _new_channel(feeds, download_base.rstrip("/").removesuffix("/downloads") + "/")
    else:
        raise FileNotFoundError(f"appcast not found at {stable_path}")

    new_item = build_item(version, installer_path, signature, notes_html,
                          f"{download_base.rstrip('/')}/v{version}/{installer_path.name}", platform)
    # The stable feed keeps whatever it already holds; only its own release is added.
    stable_items = _items(stable_path)
    beta_items = [item for item in _known(_items(site / feeds.beta)) if is_prerelease(item_version(item))]
    if is_prerelease(version):
        beta_items = _with(beta_items, new_item)
    else:
        stable_items = _with(stable_items, new_item)
    released = [item for item in _known(stable_items) if not is_prerelease(item_version(item))]
    newest = max((version_key(item_version(item)) for item in released), default=None)
    # A beta that a stable release has caught up with is no longer offered.
    beta_items = [item for item in beta_items if newest is None or version_key(item_version(item)) > newest]

    written = []
    if not is_prerelease(version) or not stable_path.exists():
        # A beta leaves an existing stable feed untouched. The stable feed
        # keeps its own title and any hand-made entries.
        known = _known(stable_items)
        unknown = [item for item in stable_items if all(item is not other for other in known)]
        written.append(_write(stable_path, template, template.findtext("title") or feeds.title,
                              template.findtext("description") or feeds.stable_description,
                              _newest_first(known) + unknown))
    written.append(_write(site / feeds.beta, template, f"{feeds.title} (beta)",
                          feeds.beta_description, _newest_first(beta_items + released)))
    by_line: dict[str, list[ET.Element]] = {}
    for item in released:
        by_line.setdefault(release_line(item_version(item)), []).append(item)
    newest_lines = sorted(by_line, key=lambda line: tuple(int(p) for p in line.split(".")), reverse=True)[:lines]
    for line in newest_lines:
        written.append(_write(site / feeds.line.format(line=line), template, f"{feeds.title} ({line})",
                              feeds.line_description.format(line=line), _newest_first(by_line[line])))
    return written


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    p.add_argument("--site", required=True, type=Path, help="The gh-pages checkout holding appcast.xml")
    p.add_argument("--version", required=True, help="X.Y.Z, or X.Y.Z-beta.N for a beta")
    p.add_argument("--installer", required=True, type=Path,
                   help="Path to the signed .exe installer, or the .dmg with --platform macos")
    p.add_argument("--signature", required=True,
                   help="Base64 EdDSA signature from winsparkle-tool sign")
    p.add_argument("--notes", required=True,
                   help="Release notes (HTML — will be embedded in <description>)")
    p.add_argument("--download-base", required=True,
                   help="Base URL of the downloads/ folder on the Pages site")
    p.add_argument("--lines", type=int, default=LINES, help="How many release lines get their own feed")
    p.add_argument("--platform", choices=sorted(FEEDS), default="windows",
                   help="Whose feeds: windows (appcast.xml and its kin) or macos (appcast-macos*.xml)")
    args = p.parse_args()

    try:
        written = publish_feeds(args.site, args.version, args.installer, args.signature, args.notes,
                                args.download_base, lines=args.lines, platform=args.platform)
    except (OSError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
    for path in written:
        print(f"Wrote {path}")


if __name__ == "__main__":
    main()
