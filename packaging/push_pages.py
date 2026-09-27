"""Publish the Pages site as Macs and Windows will download it: stage, check byte for byte, commit, push.

release.yml runs this last in both jobs that write the gh-pages branch, and
build-macos.yml's rehearsal (scripts/rehearse_pages_publish.py) runs it the
same way against a copy of the branch on the runner:

    python packaging/push_pages.py SITE --message MESSAGE [--tool winsparkle-tool.exe]
    python packaging/push_pages.py SITE --check [--tool ...]              # stage and check, nothing more
    python packaging/push_pages.py REPO --check --rev gh-pages [--tool ...]  # a commit: what Pages serves

Sparkle reads a macOS feed only when its signature verifies over the exact
bytes it downloads, and installs a disk image only when it matches the
length and signature the feed gives. Pages serves the gh-pages commit, which
needn't match the working copy: Git for Windows, where the release
publishes, converts line ends as it stages text files unless told not to.
So after ``git add -A`` this reads the staged blobs (with ``--rev``, the
commit's), never the working copy, and commits nothing unless:

* ``.gitattributes`` keeps Git from converting anything (``* -text``, which
  packaging/publish_pages.py writes), so every later checkout gets these
  same bytes;
* every macOS feed (``appcast-macos*.xml``) ends with a signing block that
  verifies with the app's key (``EDDSA_PUBLIC_KEY`` in lumi/updater.py);
* every disk image a macOS feed lists, and the site holds, has the length
  and signature that feed gives it.

Then it commits the index as one fresh commit (no parent, so removed
installers don't pile up in the branch's history) and pushes it with a lease
on the commit it started from: a run never overwrites what another published.

Signatures are checked with winsparkle-tool (``--tool``), the tool that
makes them, or else with the cryptography package.
"""
from __future__ import annotations

import argparse
import base64
import importlib.util
import re
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Callable

ROOT = Path(__file__).resolve().parents[1]
MACOS_FEED = re.compile(r"appcast-macos[^/]*\.xml")
SPARKLE = "{http://www.andymatuschak.org/xml-namespaces/sparkle}"
BOT = ("github-actions[bot]", "41898282+github-actions[bot]@users.noreply.github.com")
BYTE_FOR_BYTE = "* -text"

# (data, base64 signature) -> whether the signature verifies with the app's key
Verifier = Callable[[bytes, str], bool]


class GitError(RuntimeError):
    """A git command failed; the message has its error output."""


def _feed_signature():
    spec = importlib.util.spec_from_file_location("lumi_feed_signature", Path(__file__).with_name("feed_signature.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def app_public_key() -> str:
    """The key Lumi.app and WinSparkle verify with (``EDDSA_PUBLIC_KEY`` in lumi/updater.py)."""
    source = (ROOT / "lumi" / "updater.py").read_text(encoding="utf-8")
    match = re.search(r'^EDDSA_PUBLIC_KEY = "([^"]+)"', source, re.M)
    if not match:
        raise ValueError("EDDSA_PUBLIC_KEY not found in lumi/updater.py")
    return match.group(1)


def git(repo: Path, *args: str, config: tuple[str, ...] = ()) -> bytes:
    command = ["git"]
    for setting in config:
        command += ["-c", setting]
    result = subprocess.run([*command, "-C", str(repo), *args], capture_output=True)
    if result.returncode != 0:
        raise GitError(f"git {' '.join(args)} failed: {result.stderr.decode('utf-8', 'replace').strip()}")
    return result.stdout


def files(repo: Path, rev: str | None = None) -> dict[str, str]:
    """Each file's blob id: as staged in ``repo``'s index, or in the commit ``rev``."""
    found: dict[str, str] = {}
    if rev is None:
        for record in filter(None, git(repo, "ls-files", "--stage", "-z").split(b"\0")):
            meta, _, path = record.partition(b"\t")
            _mode, blob, stage = meta.split(b" ")
            if stage != b"0":
                raise GitError(f"{path.decode('utf-8', 'replace')} has an unresolved conflict")
            found[path.decode("utf-8")] = blob.decode("ascii")
        return found
    for record in filter(None, git(repo, "ls-tree", "-r", "-z", rev).split(b"\0")):
        meta, _, path = record.partition(b"\t")
        _mode, kind, blob = meta.split(b" ")
        if kind == b"blob":
            found[path.decode("utf-8")] = blob.decode("ascii")
    return found


def blob(repo: Path, blob_id: str) -> bytes:
    return git(repo, "cat-file", "blob", blob_id)


def tool_verifier(tool: Path, public_key: str) -> Verifier:
    """Check signatures with winsparkle-tool, which makes them."""
    def verify(data: bytes, signature: str) -> bool:
        with tempfile.TemporaryDirectory() as folder:
            signed = Path(folder) / "signed"
            signed.write_bytes(data)
            result = subprocess.run([str(tool), "verify", "--public-key", public_key, "--signature", signature,
                                     str(signed)], capture_output=True)
            return result.returncode == 0

    return verify


def cryptography_verifier(public_key: str) -> Verifier:
    """Check signatures with the cryptography package (Ed25519, as winsparkle-tool and Sparkle do)."""
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    key = Ed25519PublicKey.from_public_bytes(base64.b64decode(public_key, validate=True))

    def verify(data: bytes, signature: str) -> bool:
        try:
            key.verify(base64.b64decode(signature, validate=True), data)
            return True
        except (InvalidSignature, ValueError):
            return False

    return verify


def _enclosures(feed: bytes) -> list[tuple[str, int, str]]:
    """(url, length, signature) of each enclosure in a feed Lumi wrote."""
    if b"<!DOCTYPE" in feed:
        raise ValueError("has a document type, which Lumi's feeds never do")
    found = []
    for enclosure in ET.fromstring(feed).iter("enclosure"):
        try:
            length = int(enclosure.get("length") or -1)
        except ValueError:
            length = -1
        found.append((enclosure.get("url") or "", length, enclosure.get(f"{SPARKLE}edSignature") or ""))
    return found


def _site_path(url: str) -> str | None:
    """Where on the site an enclosure's address points (``downloads/…``), or None for elsewhere."""
    match = re.search(r"/(downloads/[^?#]+)$", url)
    return match.group(1) if match else None


def check(repo: Path, verify: Verifier, rev: str | None = None) -> tuple[list[str], list[str]]:
    """(problems, what verified) for the files staged in ``repo``, or the commit ``rev``."""
    signing = _feed_signature()
    site = files(repo, rev)
    problems: list[str] = []
    verified: list[str] = []
    attributes = site.get(".gitattributes")
    rules = [line.strip() for line in blob(repo, attributes).decode("utf-8", "replace").splitlines()] \
        if attributes else []
    if BYTE_FOR_BYTE in rules:
        verified.append(f".gitattributes: {BYTE_FOR_BYTE}")
    else:
        problems.append(f".gitattributes doesn't say '{BYTE_FOR_BYTE}', so Git could change the files' bytes "
                        "(packaging/publish_pages.py writes it)")
    images: set[tuple[str, str]] = set()
    for name in sorted(path for path in site if MACOS_FEED.fullmatch(path)):
        content, signature, length = signing.split(blob(repo, site[name]))
        if not signature:
            problems.append(f"{name} has no signing block")
            continue
        if length != len(content):
            problems.append(f"{name}'s signing block is for {length} bytes, and it has {len(content)}")
            continue
        if not verify(content, signature):
            problems.append(f"{name}'s signature doesn't verify with the app's key over the bytes Git holds")
            continue
        verified.append(f"{name}: signature ({len(content)} bytes)")
        try:
            enclosures = _enclosures(content)
        except (ET.ParseError, ValueError) as exc:
            problems.append(f"{name} {exc}")
            continue
        for url, size, image_signature in enclosures:
            path = _site_path(url)
            if path is None or path not in site or (path, image_signature) in images:
                continue
            images.add((path, image_signature))
            image = blob(repo, site[path])
            if len(image) != size:
                problems.append(f"{path} is {len(image)} bytes, and {name} says {size}")
            elif not verify(image, image_signature):
                problems.append(f"{path} doesn't match the signature {name} gives it")
            else:
                verified.append(f"{path}: {size} bytes and signature, as {name} lists it")
    return problems, verified


def push(site: Path, message: str, *, remote: str = "origin", branch: str = "gh-pages") -> str:
    """Commit the index as one fresh commit and push it with a lease; the new commit, or "" if nothing changed."""
    base = git(site, "rev-parse", "HEAD").decode("ascii").strip()
    if subprocess.run(["git", "-C", str(site), "diff", "--cached", "--quiet"]).returncode == 0:
        return ""
    tree = git(site, "write-tree").decode("ascii").strip()
    name, email = BOT
    commit = git(site, "commit-tree", tree, "-m", message,
                 config=(f"user.name={name}", f"user.email={email}")).decode("ascii").strip()
    git(site, "push", f"--force-with-lease={branch}:{base}", remote, f"{commit}:refs/heads/{branch}")
    return commit


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("site", type=Path, help="The gh-pages checkout (with --rev, any clone of it, bare too)")
    parser.add_argument("--message", help="The commit's message; needed unless --check")
    parser.add_argument("--check", action="store_true", help="Stage and check only; nothing is committed")
    parser.add_argument("--rev", help="With --check: check this commit's files instead of staging any")
    parser.add_argument("--public-key", help="The key to verify with (default: EDDSA_PUBLIC_KEY in lumi/updater.py)")
    parser.add_argument("--tool", type=Path, help="winsparkle-tool to verify with (default: the cryptography package)")
    parser.add_argument("--remote", default="origin")
    parser.add_argument("--branch", default="gh-pages")
    args = parser.parse_args(argv)
    if args.rev and not args.check:
        parser.error("--rev only checks a commit; add --check")
    if not args.check and not args.message:
        parser.error("--message is needed to commit")
    public_key = args.public_key or app_public_key()
    verify = tool_verifier(args.tool.resolve(), public_key) if args.tool else cryptography_verifier(public_key)
    try:
        if not args.rev:
            # The bytes as they are, whatever this computer's Git would convert
            # (the site's .gitattributes says the same to every checkout).
            git(args.site, "add", "-A", config=("core.autocrlf=false", "core.safecrlf=false"))
        problems, verified = check(args.site, verify, args.rev)
        for line in verified:
            print(f"  ok: {line}")
        if problems:
            for problem in problems:
                print(f"ERROR: {problem}", file=sys.stderr)
            print("Nothing was committed or pushed." if not args.check else "The site doesn't verify.",
                  file=sys.stderr)
            return 1
        if args.check:
            print(f"{args.rev or 'The staged site'}: what Macs and Windows would download verifies")
            return 0
        commit = push(args.site, args.message, remote=args.remote, branch=args.branch)
        print(f"Pushed {commit} to {args.branch}" if commit else "Nothing on the site changed; nothing pushed")
        return 0
    except (GitError, OSError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
