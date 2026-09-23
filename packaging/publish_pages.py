"""Prepare the GitHub Pages update site for a new installer.

The source repository may be private, so installed apps cannot download
GitHub Release assets anonymously. The Pages site therefore carries both the
WinSparkle feed (appcast.xml) and the installers it points to:

    downloads/vX.Y.Z/resonant-setup-X.Y.Z.exe

This script copies the new installer into that layout, removes all but the
newest KEEP installers so the site stays small, and rewrites index.html with a
link to the newest installer. Run update_appcast.py afterwards with
--download-base "<pages-url>/downloads" so the feed points at these files.
The release workflow publishes the result as a single fresh gh-pages commit,
so removed installers do not accumulate in branch history.
"""
from __future__ import annotations

import argparse
import html
import re
import shutil
from pathlib import Path

KEEP = 3
VERSION_DIR = re.compile(r"^v(\d+)\.(\d+)\.(\d+)$")


def _key(path: Path) -> tuple[int, int, int]:
    match = VERSION_DIR.match(path.name)
    return tuple(int(part) for part in match.groups()) if match else (-1, -1, -1)


def publish(site: Path, installer: Path, version: str, keep: int = KEEP) -> Path:
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        raise ValueError("stable releases only: expected X.Y.Z")
    target_dir = site / "downloads" / f"v{version}"
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / installer.name
    shutil.copyfile(installer, target)

    releases = sorted((p for p in (site / "downloads").iterdir()
                       if p.is_dir() and VERSION_DIR.match(p.name)), key=_key, reverse=True)
    for stale in releases[keep:]:
        shutil.rmtree(stale)

    href = html.escape(f"downloads/v{version}/{installer.name}")
    (site / "index.html").write_text(PAGE.format(version=html.escape(version), href=href,
                                                 name=html.escape(installer.name)),
                                     encoding="utf-8", newline="\n")
    (site / ".nojekyll").touch()
    return target


PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>SONN Client download</title>
<style>
  :root {{ color-scheme: light dark; }}
  body {{ font-family: system-ui, sans-serif; max-width: 40rem; margin: 3rem auto; padding: 0 1rem; line-height: 1.5; }}
  a.button {{ display: inline-block; padding: .6rem 1rem; border-radius: .4rem; background: #3b5bdb; color: #fff; text-decoration: none; }}
  code {{ font-size: .95em; }}
</style>
</head>
<body>
<h1>SONN Client</h1>
<p>Desktop coding client for <a href="https://getsonn.com">SONN</a> by Luminary Analytics.</p>
<p><a class="button" href="{href}">Download SONN Client {version} for Windows</a></p>
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
    args = parser.parse_args()
    print(publish(args.site, args.installer, args.version, args.keep))


if __name__ == "__main__":
    main()
