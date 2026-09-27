"""Prepare the GitHub Pages update site for a new installer.

The source repository may be private, so installed apps cannot download
GitHub Release assets anonymously. The Pages site therefore carries both the
update feeds and the installers they point to:

    downloads/vX.Y.Z/lumi-setup-X.Y.Z.exe
    downloads/vX.Y.Z/lumi-X.Y.Z.msi          (stable releases, for administrators)
    downloads/vX.Y.Z/lumi-X.Y.Z.dmg          (macOS; Sparkle downloads this)
    downloads/vX.Y.Z/lumi-X.Y.Z.pkg          (macOS stable releases, for administrators)
    downloads/vX.Y.Z/macos.json              (whether that disk image is notarized)
    downloads/vX.Y.Z-beta.N/lumi-setup-X.Y.Z-beta.N.exe

This script copies the new installer into that layout and removes old ones so
the site stays small. It keeps:

- the newest KEEP stable releases;
- the newest release of each of the newest LINES release lines, so installs
  pinned to a line (lumi/update_channels.py) can still download its update;
- the newest KEEP betas that are newer than the newest stable release.

The release workflow runs it for Windows first, then with ``--platform macos``
for the disk image, which joins the same version's folder. A stable release
also rewrites index.html with links to the newest stable release's files; a
beta doesn't. Run update_appcast.py afterwards with
--download-base "<pages-url>/downloads" (and the same --platform) so the
feeds point at these files. The release workflow publishes the result as a
single fresh gh-pages commit (packaging/push_pages.py), so removed installers
do not accumulate in branch history.

The site also gets a ``.gitattributes`` that keeps Git from changing any
file's bytes. The macOS feeds are signed over their exact bytes, and Git for
Windows, where the release publishes, otherwise converts line ends as it
commits files and checks them out.
"""
from __future__ import annotations

import argparse
import html
import json
import re
import shutil
from collections.abc import Sequence
from pathlib import Path

KEEP = 3
LINES = 4
RELEASE = re.compile(r"(\d+)\.(\d+)\.(\d+)(?:-(alpha|beta|rc)\.(\d+))?")
_PRE_RANK = {"alpha": 0, "beta": 1, "rc": 2}
PLATFORMS = ("windows", "macos")
MACOS_STATUS = "macos.json"
# Every file on the site byte for byte, whatever core.autocrlf says.
GIT_ATTRIBUTES = b"# packaging/publish_pages.py: Git keeps every file here byte for byte.\n* -text\n"


def _version_key(version: str) -> tuple[int, ...] | None:
    match = RELEASE.fullmatch(version)
    if not match:
        return None
    major, minor, patch, pre, number = match.groups()
    tail = (0, _PRE_RANK[pre], int(number)) if pre else (1, 0, 0)
    return (int(major), int(minor), int(patch), *tail)


def _key(path: Path) -> tuple[int, ...]:
    key = _version_key(path.name[1:]) if path.name.startswith("v") else None
    return key or (-1,)


def publish(site: Path, installer: Path, version: str, keep: int = KEEP, lines: int = LINES,
            extras: Sequence[Path] = (), *, platform: str = "windows", notarized: bool = False) -> Path:
    """Copy the installer (and ``extras``, such as the MSI or PKG) into downloads/v<version>/.

    ``platform`` is ``macos`` for the disk image; ``notarized`` says whether
    Apple notarized it, which the download page tells macOS visitors.
    """
    key = _version_key(version)
    if key is None:
        raise ValueError("expected a release version: X.Y.Z, or X.Y.Z-beta.N for a beta")
    if platform not in PLATFORMS:
        raise ValueError(f"{platform!r} isn't one of {', '.join(PLATFORMS)}")
    prerelease = key[3] == 0
    downloads = site / "downloads"
    existing = [p for p in downloads.iterdir() if p.is_dir()] if downloads.is_dir() else []
    newest = max((_key(p) for p in existing if _key(p)[3:4] == (1,)), default=(-1,))
    if prerelease and key < newest:
        raise ValueError(f"{version} is older than the newest stable release; publish a newer beta")
    target_dir = downloads / f"v{version}"
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / installer.name
    shutil.copyfile(installer, target)
    for extra in extras:
        shutil.copyfile(extra, target_dir / extra.name)
    if platform == "macos":
        (target_dir / MACOS_STATUS).write_text(json.dumps({"notarized": bool(notarized)}) + "\n",
                                               encoding="utf-8", newline="\n")

    releases = sorted((p for p in (site / "downloads").iterdir() if p.is_dir() and _key(p) != (-1,)),
                      key=_key, reverse=True)
    stable = [p for p in releases if _key(p)[3] == 1]
    kept = set(stable[:keep])
    newest_in_line: dict[tuple[int, int], Path] = {}
    for path in stable:
        newest_in_line.setdefault(_key(path)[:2], path)
    kept.update(newest_in_line[line] for line in sorted(newest_in_line, reverse=True)[:lines])
    newest_stable = _key(stable[0]) if stable else (-1,)
    betas = [p for p in releases if _key(p)[3] == 0 and _key(p) > newest_stable]
    kept.update(betas[:keep])
    for stale in releases:
        if stale not in kept:
            shutil.rmtree(stale)

    if not prerelease:
        # The download page offers the newest stable release, which a fix
        # for an older, pinned line (say 0.19.3 after 0.20.1) is not.
        (site / "index.html").write_text(render_page(stable[0]), encoding="utf-8", newline="\n")
    (site / ".nojekyll").touch()
    (site / ".gitattributes").write_bytes(GIT_ATTRIBUTES)
    return target


def _first(folder: Path, pattern: str) -> Path | None:
    return next(iter(sorted(folder.glob(pattern))), None)


def render_page(release_dir: Path) -> str:
    """The download page for one release's folder: its Windows and macOS files, whichever it has."""
    version = html.escape(release_dir.name[1:])

    def href(path: Path) -> str:
        return html.escape(f"downloads/{release_dir.name}/{path.name}")

    buttons, lines = [], []
    exe = _first(release_dir, "*.exe")
    if exe is not None:
        buttons.append(BUTTON.format(href=href(exe), label=f"Download Lumi {version} for Windows"))
        lines.append(f"<p>Windows installer: <code>{html.escape(exe.name)}</code>.</p>")
    dmg = _first(release_dir, "*.dmg")
    if dmg is not None:
        buttons.append(BUTTON.format(href=href(dmg), label=f"Download Lumi {version} for macOS"))
        lines.append(f"<p>macOS disk image (Apple silicon, macOS 12 or later): <code>{html.escape(dmg.name)}</code>. "
                     "Open it and drag Lumi to Applications.</p>")
        try:
            notarized = bool(json.loads((release_dir / MACOS_STATUS).read_text(encoding="utf-8")).get("notarized"))
        except (OSError, ValueError, AttributeError):
            notarized = False  # unknown: say how to open it anyway
        if not notarized:
            lines.append(MACOS_UNNOTARIZED)
    msi = _first(release_dir, "*.msi")
    if msi is not None:
        lines.append(MSI_LINE.format(href=href(msi)))
    pkg = _first(release_dir, "*.pkg")
    if pkg is not None:
        lines.append(PKG_LINE.format(href=href(pkg)))
    feeds = []
    if exe is not None:
        feeds.append('<a href="appcast.xml">this update feed</a>' + (" on Windows" if dmg is not None else ""))
    if dmg is not None:
        feeds.append(f'<a href="appcast-macos.xml">{"this one" if exe is not None else "this update feed"}</a> on macOS')
    if feeds:
        lines.append(f"<p>Installed copies update themselves from {' and '.join(feeds)}. Each update is "
                     "signed with EdDSA, and Lumi checks the signature before installing it.</p>")
    return PAGE.format(buttons="\n".join(buttons), details="\n".join(lines))


PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Lumi download</title>
<style>
  :root {{ color-scheme: light dark; }}
  body {{ font-family: system-ui, sans-serif; max-width: 40rem; margin: 3rem auto; padding: 0 1rem; line-height: 1.5; }}
  a.button {{ display: inline-block; margin: 0 .5rem .5rem 0; padding: .6rem 1rem; border-radius: .4rem; background: #3b5bdb; color: #fff; text-decoration: none; }}
  code {{ font-size: .95em; }}
</style>
</head>
<body>
<h1>Lumi</h1>
<p>The coding agent by Luminary Analytics. Runs on the model endpoints you choose, including <a href="https://getsonn.com">SONN</a>.</p>
<p>{buttons}</p>
{details}
</body>
</html>
"""

BUTTON = '<a class="button" href="{href}">{label}</a>'

MACOS_UNNOTARIZED = """<p><strong>This macOS build isn't notarized by Apple yet.</strong> The first time you open Lumi,
macOS says it can't check it for malicious software and doesn't open it. Choose <strong>Done</strong>, then open
<strong>System Settings › Privacy &amp; Security</strong>, scroll to <strong>Security</strong> and choose
<strong>Open Anyway</strong> beside the message about Lumi, and confirm with your password. You do this once;
updates install without asking again.</p>
"""

MSI_LINE = """<p>For IT administrators: the <a href="{href}">MSI package</a> installs per machine and silently
(<code>msiexec /i lumi-X.Y.Z.msi /qn</code>) through Intune, Configuration Manager or Group Policy.
Copies installed from it leave updates to your device management.</p>
"""

PKG_LINE = """<p>For Mac administrators: the <a href="{href}">installer package</a> installs Lumi for every user
(<code>sudo installer -pkg lumi-X.Y.Z.pkg -target /</code>) through Jamf Pro, Intune or other device management.
Copies installed from it leave updates to your device management.</p>
"""


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--site", type=Path, required=True, help="gh-pages checkout")
    parser.add_argument("--installer", type=Path, required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--keep", type=int, default=KEEP)
    parser.add_argument("--lines", type=int, default=LINES)
    parser.add_argument("--extra", type=Path, action="append", default=[],
                        help="Another file for the same folder, such as the MSI or PKG (repeatable)")
    parser.add_argument("--platform", choices=PLATFORMS, default="windows",
                        help="macos for the disk image, which joins the version's folder")
    parser.add_argument("--notarized", action="store_true",
                        help="With --platform macos: Apple notarized the disk image")
    args = parser.parse_args()
    print(publish(args.site, args.installer, args.version, args.keep, args.lines, args.extra,
                  platform=args.platform, notarized=args.notarized))


if __name__ == "__main__":
    main()
