"""Prepare the GitHub Pages update site for a new installer.

The source repository may be private, so installed apps cannot download
GitHub Release assets anonymously. The Pages site therefore carries both the
WinSparkle feeds and the installers they point to:

    downloads/vX.Y.Z/lumi-setup-X.Y.Z.exe
    downloads/vX.Y.Z-beta.N/lumi-setup-X.Y.Z-beta.N.exe

This script copies the new installer into that layout and removes old ones so
the site stays small. It keeps:

- the newest KEEP stable installers;
- the newest installer of each of the newest LINES release lines, so installs
  pinned to a line (lumi/update_channels.py) can still download its update;
- the newest KEEP betas that are newer than the newest stable release.

A stable release also rewrites index.html with a link to it; a beta doesn't.
Run update_appcast.py afterwards with --download-base "<pages-url>/downloads"
so the feeds point at these files. The release workflow publishes the result
as a single fresh gh-pages commit, so removed installers do not accumulate in
branch history.
"""
from __future__ import annotations

import argparse
import html
import re
import shutil
from pathlib import Path

KEEP = 3
LINES = 4
RELEASE = re.compile(r"(\d+)\.(\d+)\.(\d+)(?:-(alpha|beta|rc)\.(\d+))?")
_PRE_RANK = {"alpha": 0, "beta": 1, "rc": 2}


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


def publish(site: Path, installer: Path, version: str, keep: int = KEEP, lines: int = LINES) -> Path:
    key = _version_key(version)
    if key is None:
        raise ValueError("expected a release version: X.Y.Z, or X.Y.Z-beta.N for a beta")
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
        newest_dir = stable[0]
        newest_installer = target if newest_dir == target_dir else next(iter(sorted(newest_dir.glob("*.exe"))), target)
        href = html.escape(f"downloads/{newest_dir.name}/{newest_installer.name}")
        (site / "index.html").write_text(PAGE.format(version=html.escape(newest_dir.name[1:]), href=href,
                                                     name=html.escape(newest_installer.name)),
                                         encoding="utf-8", newline="\n")
    (site / ".nojekyll").touch()
    return target


PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Lumi download</title>
<style>
  :root {{ color-scheme: light dark; }}
  body {{ font-family: system-ui, sans-serif; max-width: 40rem; margin: 3rem auto; padding: 0 1rem; line-height: 1.5; }}
  a.button {{ display: inline-block; padding: .6rem 1rem; border-radius: .4rem; background: #3b5bdb; color: #fff; text-decoration: none; }}
  code {{ font-size: .95em; }}
</style>
</head>
<body>
<h1>Lumi</h1>
<p>The coding agent by Luminary Analytics. Runs on the model endpoints you choose, including <a href="https://getsonn.com">SONN</a>.</p>
<p><a class="button" href="{href}">Download Lumi {version} for Windows</a></p>
<p>Installer: <code>{name}</code>. Installed copies update themselves from
<a href="appcast.xml">this update feed</a>, which is signed with EdDSA.</p>
</body>
</html>
"""


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--site", type=Path, required=True, help="gh-pages checkout")
    parser.add_argument("--installer", type=Path, required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--keep", type=int, default=KEEP)
    parser.add_argument("--lines", type=int, default=LINES)
    args = parser.parse_args()
    print(publish(args.site, args.installer, args.version, args.keep, args.lines))


if __name__ == "__main__":
    main()
