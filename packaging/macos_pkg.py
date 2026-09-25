"""The parts of the macOS installer package (lumi-X.Y.Z.pkg) that aren't Apple's tools.

packaging/build_macos.sh makes the package for device management (Jamf,
Intune and other MDMs; docs/deploy-macos.md) after the DMG:

1. It copies Lumi.app into a staging root's ``Applications`` folder, and
   ``marker`` writes ``Contents/Resources/lumi-install.json``
   (``{"installer": "pkg"}``). The app then leaves updates to device
   management (lumi/update_channels.py), which a copy dragged from the DMG
   doesn't. The marker changes the bundle, so a signed build signs the staged
   copy again.
2. ``pkgbuild --analyze`` lists the bundle, and ``component`` makes it
   non-relocatable: an update must replace /Applications/Lumi.app, never a
   copy that happens to be somewhere else.
3. ``pkgbuild`` builds the component package, and ``distribution`` writes
   the file ``productbuild`` needs for the product archive MDMs deploy: a
   system-wide install, no choices to customize, Apple silicon (or the
   architecture it was built on) and the app's minimum macOS.

    python3 packaging/macos_pkg.py marker dist/pkgroot/Applications/Lumi.app
    python3 packaging/macos_pkg.py component component.plist
    python3 packaging/macos_pkg.py distribution --version 0.20.0 --arch arm64 --out distribution.xml

Standard library only: it runs with whatever python3 the Mac has.
"""

from __future__ import annotations

import argparse
import json
import plistlib
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

IDENTIFIER = "com.luminaryanalytics.lumi"  # the bundle id in packaging/lumi.spec, and the package id
COMPONENT = "lumi-component.pkg"
SPEC = Path(__file__).with_name("lumi.spec")


def minimum_macos() -> str:
    """LSMinimumSystemVersion from lumi.spec, so the package and the app agree."""
    match = re.search(r'"LSMinimumSystemVersion":\s*"([0-9.]+)"', SPEC.read_text(encoding="utf-8"))
    if not match:
        raise SystemExit("lumi.spec has no LSMinimumSystemVersion")
    return match.group(1)


def write_marker(app: Path) -> Path:
    """Mark a staged Lumi.app as installed by the package."""
    resources = app / "Contents" / "Resources"
    if not (app / "Contents" / "MacOS").is_dir():
        raise SystemExit(f"{app} isn't an app bundle (no Contents/MacOS)")
    resources.mkdir(parents=True, exist_ok=True)
    marker = resources / "lumi-install.json"
    marker.write_text(json.dumps({"installer": "pkg"}), encoding="utf-8")
    return marker


def pin_components(components: list[dict]) -> list[dict]:
    """``pkgbuild --analyze`` output with every bundle installed in place, upgrading what's there."""
    if not components:
        raise SystemExit("pkgbuild --analyze found no bundle to install")
    pinned = []
    for component in components:
        component = dict(component)
        component["BundleIsRelocatable"] = False
        component["BundleHasStrictIdentifier"] = True
        component["BundleIsVersionChecked"] = True
        component["BundleOverwriteAction"] = "upgrade"
        pinned.append(component)
    return pinned


def distribution_xml(version: str, *, arch: str, minimum: str | None = None,
                     identifier: str = IDENTIFIER, component: str = COMPONENT) -> str:
    """productbuild's distribution file for one component package."""
    if not re.fullmatch(r"[0-9][0-9A-Za-z.+-]*", version):
        raise SystemExit(f"Not a version: {version!r}")
    if arch not in ("arm64", "x86_64"):
        raise SystemExit(f"Unsupported architecture {arch!r}; build on arm64 or x86_64")
    root = ET.Element("installer-gui-script", minSpecVersion="2")
    ET.SubElement(root, "title").text = "Lumi"
    ET.SubElement(root, "organization").text = "com.luminaryanalytics"
    # For every user of the computer; never a per-user or other-volume install.
    ET.SubElement(root, "domains", enable_anywhere="false", enable_currentUserHome="false",
                  enable_localSystem="true")
    ET.SubElement(root, "options", customize="never", **{"require-scripts": "false"}, hostArchitectures=arch)
    check = ET.SubElement(ET.SubElement(root, "volume-check"), "allowed-os-versions")
    ET.SubElement(check, "os-version", min=minimum or minimum_macos())
    outline = ET.SubElement(root, "choices-outline")
    ET.SubElement(ET.SubElement(outline, "line", choice="default"), "line", choice=identifier)
    ET.SubElement(root, "choice", id="default")
    choice = ET.SubElement(root, "choice", id=identifier, visible="false")
    ET.SubElement(choice, "pkg-ref", id=identifier)
    ET.SubElement(root, "pkg-ref", id=identifier, version=version, onConclusion="none").text = component
    ET.indent(root)
    return '<?xml version="1.0" encoding="utf-8"?>\n' + ET.tostring(root, encoding="unicode") + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    commands = parser.add_subparsers(dest="command", required=True)
    marker = commands.add_parser("marker", help="mark a staged Lumi.app as installed by the package")
    marker.add_argument("app", type=Path)
    component = commands.add_parser("component", help="pin pkgbuild --analyze output in place")
    component.add_argument("plist", type=Path)
    distribution = commands.add_parser("distribution", help="write productbuild's distribution file")
    distribution.add_argument("--version", required=True)
    distribution.add_argument("--arch", required=True)
    distribution.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.command == "marker":
        print(f"Wrote {write_marker(args.app)}")
    elif args.command == "component":
        components = plistlib.loads(args.plist.read_bytes())
        args.plist.write_bytes(plistlib.dumps(pin_components(components)))
        print(f"Pinned {len(components)} bundle(s) in {args.plist}")
    else:
        args.out.write_text(distribution_xml(args.version, arch=args.arch), encoding="utf-8")
        print(f"Wrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
