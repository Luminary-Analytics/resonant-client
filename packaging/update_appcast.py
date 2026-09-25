"""
Add a release to the update feeds on the Pages site.

Pipeline (called from .github/workflows/release.yml):
    1. CI builds lumi-setup-X.Y.Z.exe
    2. CI signs it with winsparkle-tool using the EDDSA_PRIVATE_KEY secret
    3. CI uploads the .exe to the GitHub Release for tag vX.Y.Z
    4. CI checks out the gh-pages branch into ./gh-pages-checkout
    5. publish_pages.py copies the installer to downloads/vX.Y.Z/
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

The feeds (lumi/update_channels.py picks one per install):
    appcast.xml         stable releases. Installed copies before 0.21 poll only
                        this one, so it keeps its address and history.
    appcast-beta.xml    beta releases (tags like v0.21.0-beta.1) newer than the
                        newest stable release, and every stable release.
    appcast-X.Y.xml     stable releases of one release line, for installs pinned
                        to it; written for the newest LINES lines.

A stable release goes into appcast.xml and a pre-release into the beta feed;
the beta and line feeds are then rebuilt from those two, so they can't drift.
Items are ordered newest first (WinSparkle picks by version anyway). A
version that is already listed is replaced, so a re-run of a release job is
harmless. Nothing is published without a signature.
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

# Sparkle XML namespace — needed so ET emits the right `sparkle:` prefix.
SPARKLE_NS = "http://www.andymatuschak.org/xml-namespaces/sparkle"
ET.register_namespace("sparkle", SPARKLE_NS)
ET.register_namespace("dc", "http://purl.org/dc/elements/1.1/")

STABLE_FEED = "appcast.xml"
BETA_FEED = "appcast-beta.xml"
LINES = 4
VERSION = re.compile(r"(\d+)\.(\d+)\.(\d+)(?:-(alpha|beta|rc)\.(\d+))?")
_PRE_RANK = {"alpha": 0, "beta": 1, "rc": 2}


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


def build_item(
    version: str,
    installer_path: Path,
    signature: str,
    notes_html: str,
    download_url: str,
) -> ET.Element:
    """Build a new <item> element for this release."""
    pub_date = format_datetime(datetime.now(timezone.utc))
    file_size = installer_path.stat().st_size

    item = ET.Element("item")

    title = ET.SubElement(item, "title")
    title.text = f"Version {version}"

    pub = ET.SubElement(item, "pubDate")
    pub.text = pub_date

    sv = ET.SubElement(item, f"{{{SPARKLE_NS}}}version")
    sv.text = version

    ssv = ET.SubElement(item, f"{{{SPARKLE_NS}}}shortVersionString")
    ssv.text = version

    desc = ET.SubElement(item, "description")
    # CDATA isn't natively supported by ElementTree — pass HTML as text and
    # post-process. WinSparkle accepts either way.
    desc.text = notes_html

    ET.SubElement(item, "enclosure", attrib={
        "url": download_url,
        f"{{{SPARKLE_NS}}}version": version,
        f"{{{SPARKLE_NS}}}shortVersionString": version,
        "length": str(file_size),
        "type": "application/octet-stream",
        f"{{{SPARKLE_NS}}}edSignature": signature,
    })
    return item


def item_version(item: ET.Element) -> str:
    node = item.find(f"{{{SPARKLE_NS}}}version")
    return (node.text or "").strip() if node is not None else ""


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


def publish_feeds(
    site: Path,
    version: str,
    installer_path: Path,
    signature: str,
    notes_html: str,
    download_base: str,
    *,
    lines: int = LINES,
) -> list[Path]:
    """Add one release to the feeds in ``site`` and rebuild the derived feeds."""
    if not signature:
        raise ValueError("empty signature — refusing to publish an unsigned update")
    if not installer_path.exists():
        raise FileNotFoundError(f"installer not found at {installer_path}")
    version_key(version)  # a release version, or ValueError
    stable_path = site / STABLE_FEED
    if not stable_path.exists():
        raise FileNotFoundError(f"appcast not found at {stable_path}")
    template = ET.parse(stable_path).getroot().find("channel")
    if template is None:
        raise ValueError("<channel> element not found in appcast.xml")

    new_item = build_item(version, installer_path, signature, notes_html,
                          f"{download_base.rstrip('/')}/v{version}/{installer_path.name}")
    # appcast.xml keeps whatever it already holds; only its own release is added.
    stable_items = _items(stable_path)
    beta_items = [item for item in _known(_items(site / BETA_FEED)) if is_prerelease(item_version(item))]
    if is_prerelease(version):
        beta_items = _with(beta_items, new_item)
    else:
        stable_items = _with(stable_items, new_item)
    released = [item for item in _known(stable_items) if not is_prerelease(item_version(item))]
    newest = max((version_key(item_version(item)) for item in released), default=None)
    # A beta that a stable release has caught up with is no longer offered.
    beta_items = [item for item in beta_items if newest is None or version_key(item_version(item)) > newest]

    title = "Lumi updates"
    written = []
    if not is_prerelease(version):
        # A beta leaves the stable feed untouched. The stable feed keeps its
        # own title and any hand-made entries.
        known = _known(stable_items)
        unknown = [item for item in stable_items if all(item is not other for other in known)]
        written.append(_write(stable_path, template, template.findtext("title") or title,
                              template.findtext("description") or "Stable releases of Lumi",
                              _newest_first(known) + unknown))
    written.append(_write(site / BETA_FEED, template, f"{title} (beta)",
                          "Beta and stable releases of Lumi", _newest_first(beta_items + released)))
    by_line: dict[str, list[ET.Element]] = {}
    for item in released:
        by_line.setdefault(release_line(item_version(item)), []).append(item)
    newest_lines = sorted(by_line, key=lambda line: tuple(int(p) for p in line.split(".")), reverse=True)[:lines]
    for line in newest_lines:
        written.append(_write(site / f"appcast-{line}.xml", template, f"{title} ({line})",
                              f"Stable releases of Lumi {line}", _newest_first(by_line[line])))
    return written


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    p.add_argument("--site", required=True, type=Path, help="The gh-pages checkout holding appcast.xml")
    p.add_argument("--version", required=True, help="X.Y.Z, or X.Y.Z-beta.N for a beta")
    p.add_argument("--installer", required=True, type=Path,
                   help="Path to the signed .exe installer")
    p.add_argument("--signature", required=True,
                   help="Base64 EdDSA signature from winsparkle-tool sign")
    p.add_argument("--notes", required=True,
                   help="Release notes (HTML — will be embedded in <description>)")
    p.add_argument("--download-base", required=True,
                   help="Base URL of the downloads/ folder on the Pages site")
    p.add_argument("--lines", type=int, default=LINES, help="How many release lines get their own feed")
    args = p.parse_args()

    try:
        written = publish_feeds(args.site, args.version, args.installer, args.signature, args.notes,
                                args.download_base, lines=args.lines)
    except (OSError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
    for path in written:
        print(f"Wrote {path}")


if __name__ == "__main__":
    main()
