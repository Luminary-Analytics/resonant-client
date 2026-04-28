"""
Update appcast.xml after a new release is built.

Pipeline (called from .github/workflows/release.yml):
    1. CI builds resonant-setup-X.Y.Z.exe
    2. CI signs it with winsparkle-tool using the EDDSA_PRIVATE_KEY secret
    3. CI uploads the .exe to the GitHub Release for tag vX.Y.Z
    4. CI checks out the gh-pages branch into ./gh-pages-checkout
    5. CI runs THIS script with --version, --installer, --signature, --notes
    6. THIS script rewrites gh-pages-checkout/appcast.xml with a new <item> on top
    7. CI commits + pushes the gh-pages-checkout

Standalone usage (for local testing):
    python packaging/update_appcast.py \\
        --version 0.2.1 \\
        --installer dist/installer/resonant-setup-0.2.1.exe \\
        --signature "BASE64_EDDSA_SIG" \\
        --notes "Initial public release." \\
        --appcast gh-pages-checkout/appcast.xml \\
        --download-base "https://github.com/Luminary-Analytics/resonant-client/releases/download"

Behavior:
    - Reads the existing appcast.xml.
    - Inserts the new <item> as the FIRST child of <channel>, so WinSparkle
      always sees the latest release at the top (it sorts by version anyway,
      but ordering helps humans diff the file).
    - Preserves all existing <item> entries — the appcast is append-only.
      Old releases stay queryable in case a user is still on a much older
      version and needs to upgrade through intermediate steps.
    - Uses pubDate = now in RFC 2822 format (the format WinSparkle expects).
"""

from __future__ import annotations

import argparse
import sys
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import format_datetime
from pathlib import Path

# Sparkle XML namespace — needed so ET emits the right `sparkle:` prefix.
SPARKLE_NS = "http://www.andymatuschak.org/xml-namespaces/sparkle"
ET.register_namespace("sparkle", SPARKLE_NS)
ET.register_namespace("dc", "http://purl.org/dc/elements/1.1/")


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

    enclosure = ET.SubElement(item, "enclosure", attrib={
        "url": download_url,
        f"{{{SPARKLE_NS}}}version": version,
        f"{{{SPARKLE_NS}}}shortVersionString": version,
        "length": str(file_size),
        "type": "application/octet-stream",
        f"{{{SPARKLE_NS}}}edSignature": signature,
    })
    return item


def update_appcast(
    appcast_path: Path,
    version: str,
    installer_path: Path,
    signature: str,
    notes_html: str,
    download_base: str,
) -> None:
    """Insert a new <item> at the top of the appcast's <channel>."""
    if not appcast_path.exists():
        print(f"ERROR: appcast not found at {appcast_path}", file=sys.stderr)
        sys.exit(1)

    if not installer_path.exists():
        print(f"ERROR: installer not found at {installer_path}", file=sys.stderr)
        sys.exit(1)

    if not signature:
        print(
            "ERROR: empty signature — refuse to publish unsigned update",
            file=sys.stderr,
        )
        sys.exit(1)

    download_url = f"{download_base}/v{version}/{installer_path.name}"

    tree = ET.parse(appcast_path)
    root = tree.getroot()
    channel = root.find("channel")
    if channel is None:
        print("ERROR: <channel> element not found in appcast", file=sys.stderr)
        sys.exit(1)

    # Refuse to insert a duplicate version.
    for existing in channel.findall("item"):
        existing_version = existing.find(f"{{{SPARKLE_NS}}}version")
        if existing_version is not None and existing_version.text == version:
            print(
                f"ERROR: version {version} already in appcast — bump and retry",
                file=sys.stderr,
            )
            sys.exit(1)

    new_item = build_item(
        version=version,
        installer_path=installer_path,
        signature=signature,
        notes_html=notes_html,
        download_url=download_url,
    )

    # Insert as first <item> after the channel-level metadata (title/link/desc).
    # Find the index of the first existing <item> (or end of children) and
    # insert there.
    insert_at = len(channel)
    for idx, child in enumerate(channel):
        if child.tag == "item":
            insert_at = idx
            break
    channel.insert(insert_at, new_item)

    # Pretty-print: indent children for readable diffs.
    ET.indent(tree, space="    ")
    tree.write(appcast_path, encoding="utf-8", xml_declaration=True)

    print(
        f"Wrote {appcast_path}: added v{version} "
        f"({installer_path.stat().st_size:,} bytes, sig={signature[:12]}...)"
    )


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--version", required=True, help="Semver string e.g. 0.2.1")
    p.add_argument("--installer", required=True, type=Path,
                   help="Path to the signed .exe installer")
    p.add_argument("--signature", required=True,
                   help="Base64 EdDSA signature from winsparkle-tool sign")
    p.add_argument("--notes", required=True,
                   help="Release notes (HTML — will be embedded in <description>)")
    p.add_argument("--appcast", required=True, type=Path,
                   help="Path to the existing appcast.xml to update")
    p.add_argument("--download-base", required=True,
                   help="GitHub Releases download base URL")
    args = p.parse_args()

    update_appcast(
        appcast_path=args.appcast,
        version=args.version,
        installer_path=args.installer,
        signature=args.signature,
        notes_html=args.notes,
        download_base=args.download_base,
    )


if __name__ == "__main__":
    main()
