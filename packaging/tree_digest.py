"""The SHA-256 of the files one release job hands to the next, to check they arrive unchanged.

release.yml's `build` and `macos` jobs run this over exactly what they upload
and pass the digest on as a job output, which no other job can change;
`release` and `publish-macos` download the artifact by the ID those jobs
recorded (never by name: any job in the run could delete an artifact and
upload another under the same name) and run this again with ``--expect``,
which fails on any difference. download-artifact checks the zip it downloads
against the digest upload-artifact recorded, but only warns when they differ,
and keeps no zip to check again; this checks the files themselves, as they
will be signed and published.

The digest covers each file's path (relative to ROOT, with forward slashes),
size and SHA-256, sorted. Folders, times and permissions don't count: an
artifact keeps none of them. A link is refused, since an artifact can't
carry one.

    python packaging/tree_digest.py ROOT [ENTRY ...]                  # prints the digest
    python packaging/tree_digest.py ROOT [ENTRY ...] --expect DIGEST  # fails unless it matches

Without ENTRY names, every entry in ROOT counts, so a file that arrived
beside the expected ones changes the digest too.
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import re
import sys
from pathlib import Path


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def listing(root: Path, entries: list[str] | None = None) -> list[str]:
    """One line per file: its path under ``root``, size and SHA-256, sorted."""
    root = Path(root)
    if not root.is_dir():
        raise FileNotFoundError(f"{root} isn't a folder")
    names = entries if entries else sorted(item.name for item in root.iterdir())
    lines = []
    for name in names:
        top = root / name
        if top.is_symlink():
            raise ValueError(f"{top} is a link")
        if top.is_file():
            files = [top]
        elif top.is_dir():
            files = []
            for item in sorted(top.rglob("*")):
                if item.is_symlink():
                    raise ValueError(f"{item} is a link")
                if item.is_file():
                    files.append(item)
        else:
            raise FileNotFoundError(f"{top} doesn't exist")
        for file in files:
            lines.append(f"{file.relative_to(root).as_posix()}\t{file.stat().st_size}\t{_file_sha256(file)}")
    return sorted(lines)


def digest_of(lines: list[str]) -> str:
    return hashlib.sha256("".join(line + "\n" for line in lines).encode("utf-8")).hexdigest()


def tree_digest(root: Path, entries: list[str] | None = None) -> str:
    return digest_of(listing(root, entries))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("root", type=Path)
    parser.add_argument("entries", nargs="*", help="Names in ROOT to cover (default: all of them)")
    parser.add_argument("--expect", help="Fail unless the digest is this one")
    args = parser.parse_args(argv)
    try:
        lines = listing(args.root, args.entries)
    except (OSError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    digest = digest_of(lines)
    if args.expect is None:
        print(digest)
        return 0
    expected = args.expect.strip().lower()
    if not re.fullmatch(r"[0-9a-f]{64}", expected):
        print(f"ERROR: no digest to compare with (got {args.expect!r}); the job that made these files "
              "didn't pass one on", file=sys.stderr)
        return 1
    if not hmac.compare_digest(digest, expected):
        print(f"ERROR: the files in {args.root} aren't what the job that made them handed over "
              f"(digest {digest}, expected {expected}). What arrived:", file=sys.stderr)
        for line in lines:
            print(f"  {line}", file=sys.stderr)
        return 1
    print(f"{len(lines)} files in {args.root} are byte for byte what was handed over (digest {digest})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
