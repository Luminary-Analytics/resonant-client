"""Write the license texts of the Rust crates compiled into ripgrep's Windows release.

Lumi ships ripgrep's x86_64-pc-windows-msvc release (rg.exe, fetched by
packaging/fetch_ripgrep.ps1), which links the crates ripgrep depends on and
PCRE2 statically. ripgrep's release carries only its own license, so this
script collects the others from the crates themselves, for the exact
versions ripgrep's Cargo.lock pins:

    git clone --depth 1 --branch 15.2.0 https://github.com/BurntSushi/ripgrep.git rg-src
    curl -o LICENCE.md https://raw.githubusercontent.com/PCRE2Project/pcre2/pcre2-10.45/LICENCE.md
    python packaging/ripgrep_crate_licenses.py rg-src --pcre2 10.45 --pcre2-licence LICENCE.md \\
        --out packaging/licenses/ripgrep-15.2.0-crates-LICENSES.txt

It needs cargo, which downloads the crates. The crates are the normal
(not build or dev) dependencies of ripgrep with the pcre2 feature on that
target, as ``cargo tree`` lists them; ripgrep's own crates (globset, grep*,
ignore) are under ripgrep's license. A crate that offers the MIT License
among others is listed with its MIT text, the license Lumi takes it under;
any other text a crate publishes (encoding_rs's WHATWG notice) follows too.
PCRE2's licence comes from its repository, since pcre2-sys doesn't carry it.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

TARGET = "x86_64-pc-windows-msvc"
FEATURES = "pcre2"
# ripgrep's own crates: the same author and license as ripgrep (LICENSE-MIT, UNLICENSE).
RIPGREP_OWN = {"ripgrep", "globset", "grep", "grep-cli", "grep-matcher", "grep-pcre2", "grep-printer",
               "grep-regex", "grep-searcher", "ignore"}
MIT_FILE = re.compile(r"^licen[cs]e[-_.]?mit(\.(txt|md))?$", re.IGNORECASE)
PLAIN_LICENSE = re.compile(r"^licen[cs]e(\.(txt|md))?$", re.IGNORECASE)
OTHER_FILES = re.compile(r"^(licen[cs]e[-_.]?whatwg(\.(txt|md))?|copyright(\.(txt|md))?)$", re.IGNORECASE)
RULE = "=" * 78


def _cargo(source: Path, *args: str) -> str:
    return subprocess.run(["cargo", *args], cwd=source, check=True, capture_output=True, text=True,
                          encoding="utf-8").stdout


def crates(source: Path) -> list[tuple[str, str, str]]:
    """(name, version, license) of each third-party crate in the release binary, sorted by name."""
    listing = _cargo(source, "tree", "--locked", "--target", TARGET, "--features", FEATURES, "-e", "normal",
                     "--prefix", "none", "--format", "{p}|{l}")
    found = {}
    for line in listing.splitlines():
        package, _, license_ = line.replace(" (*)", "").partition("|")
        name, _, rest = package.partition(" v")
        version = rest.split(" ", 1)[0]
        if name and version and name not in RIPGREP_OWN:
            found[(name, version)] = license_.strip()
    return sorted((name, version, license_) for (name, version), license_ in found.items())


def manifest_dirs(source: Path) -> dict[tuple[str, str], Path]:
    data = json.loads(_cargo(source, "metadata", "--locked", "--format-version", "1", "--filter-platform", TARGET,
                             "--features", FEATURES))
    return {(item["name"], item["version"]): Path(item["manifest_path"]).parent for item in data["packages"]}


def texts_for(folder: Path) -> list[tuple[str, str]]:
    """The MIT text (or a single LICENSE), then any other notice the crate publishes."""
    files = sorted(path for path in folder.iterdir() if path.is_file())
    mit = [path for path in files if MIT_FILE.match(path.name)] or [path for path in files
                                                                    if PLAIN_LICENSE.match(path.name)]
    chosen = mit[:1] + [path for path in files if OTHER_FILES.match(path.name)]
    return [(path.name, path.read_text(encoding="utf-8").strip()) for path in chosen]


def render(version: str, rows: list[tuple[str, str, str, list[tuple[str, str]]]], pcre2: str,
           pcre2_licence: str) -> str:
    lines = [
        f"Third-party code in ripgrep {version}'s Windows release (rg.exe)",
        "",
        f"The Rust crates ripgrep {version} links on {TARGET} with its pcre2 feature, at",
        "the versions its Cargo.lock pins, and PCRE2, which pcre2-sys compiles in. Written by",
        "packaging/ripgrep_crate_licenses.py. ripgrep's own crates are under ripgrep's",
        "license. A crate offered under the MIT License or another license is used under",
        "the MIT License, whose text follows for each; other notices a crate publishes",
        "follow it.",
        "",
    ]
    lines += [f"  {name} {crate_version} ({license_})" for name, crate_version, license_, _ in rows]
    lines.append(f"  PCRE2 {pcre2} (BSD-3-Clause WITH PCRE2-exception)")
    for name, crate_version, license_, texts in rows:
        lines += ["", RULE, f"{name} {crate_version} ({license_})", RULE]
        for filename, body in texts:
            lines += ["", f"--- {filename} ---", "", body]
    lines += ["", RULE, f"PCRE2 {pcre2} (BSD-3-Clause WITH PCRE2-exception)", RULE, "", "--- LICENCE.md ---", "",
              pcre2_licence.strip()]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("source", type=Path, help="a checkout of ripgrep at the release's tag")
    parser.add_argument("--pcre2", required=True, help="the PCRE2 version rg --version reports")
    parser.add_argument("--pcre2-licence", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args(argv)
    manifest = (args.source / "Cargo.toml").read_text(encoding="utf-8")
    version = re.search(r'^version\s*=\s*"([^"]+)"', manifest, re.MULTILINE).group(1)
    dirs = manifest_dirs(args.source)
    rows = []
    for name, crate_version, license_ in crates(args.source):
        texts = texts_for(dirs[(name, crate_version)])
        if not texts:
            print(f"{name} {crate_version} has no license file", file=sys.stderr)
            return 1
        rows.append((name, crate_version, license_, texts))
    text = render(version, rows, args.pcre2, args.pcre2_licence.read_text(encoding="utf-8"))
    args.out.write_text(text, encoding="utf-8", newline="\n")
    print(f"Wrote {args.out}: {len(rows)} crates and PCRE2 {args.pcre2}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
