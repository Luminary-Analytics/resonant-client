"""Sign and check the macOS update feeds as Sparkle does (``SURequireSignedFeed``).

Sparkle 2.9 and later verify a feed whose last bytes are a signing block:

    <the feed's bytes><!-- sparkle-signatures:
    edSignature: <base64 Ed25519 signature of the feed's bytes>
    length: <how many bytes that is>
    -->

That is the format of Sparkle's own ``sign_update`` (``signAppcast`` in
common_cli/Signing.swift), which the app reads back with
``SPUExtractAppcastContent``: everything before the last block is the signed
content. The key is the one that signs the updates, so ``winsparkle-tool
sign`` on the unsigned feed makes the signature, as it does for the disk image
(packaging/publish_macos.ps1), and ``attach`` appends the block.

Only the macOS feeds are signed. WinSparkle doesn't read the block, and the
Windows feeds that installed copies poll stay exactly as they were.

The signature covers bytes, so a feed must reach the Pages site exactly as
it was signed: packaging/update_appcast.py writes "\\n" line ends on every
platform, the site's ``.gitattributes`` keeps Git from converting them, and
packaging/push_pages.py checks the staged blobs before it pushes.

    python packaging/feed_signature.py attach FEED SIGNATURE
    python packaging/feed_signature.py split FEED CONTENT_OUT   # prints the signature
    python packaging/feed_signature.py strip FEED               # removes the block, to sign again
    python packaging/feed_signature.py verify FEED PUBLIC_KEY   # needs the cryptography package
"""

from __future__ import annotations

import argparse
import base64
import binascii
import sys
from pathlib import Path

PREFIX = b"<!-- sparkle-signatures:\n"
SUFFIX = b"-->"


def split(data: bytes) -> tuple[bytes, str, int | None]:
    """(signed content, signature, stated length) as Sparkle reads them; no block gives (data, "", None)."""
    at = data.rfind(PREFIX)
    if at < 0:
        return data, "", None
    end = data.find(SUFFIX, at + len(PREFIX))
    if end < 0:
        return data, "", None
    signature, length = "", None
    for line in data[at + len(PREFIX):end].decode("utf-8", "replace").splitlines():
        if line.startswith("edSignature:"):
            signature = line[len("edSignature:"):].strip()
        elif line.startswith("length:"):
            try:
                length = int(line[len("length:"):].strip())
            except ValueError:
                length = None
    return data[:at], signature, length


def strip(data: bytes) -> bytes:
    """The feed without its last signing block, whatever its line ends: what a new signature covers.

    For re-signing a feed whose block no longer verifies (a publish that
    changed the bytes after signing; publish_macos.ps1 -ResignFeeds). A feed
    without a block is returned as it is.
    """
    at = data.rfind(PREFIX.rstrip(b"\n"))
    if at < 0 or data.find(SUFFIX, at) < 0:
        return data
    return data[:at]


def block(signature: str, length: int) -> bytes:
    """The signing block Sparkle's sign_update appends."""
    return f"<!-- sparkle-signatures:\nedSignature: {signature}\nlength: {length}\n-->\n".encode("utf-8")


def _checked_signature(signature: str) -> str:
    text = signature.strip()
    try:
        raw = base64.b64decode(text, validate=True)
    except (binascii.Error, ValueError):
        raw = b""
    if len(raw) != 64:
        raise ValueError("not a base64 Ed25519 signature")
    return text


def attach(path: Path, signature: str) -> None:
    """Append the block for ``signature``, made over the file as it is now; refuse a file already signed."""
    data = path.read_bytes()
    if PREFIX in data:
        raise ValueError(f"{path.name} already has a signing block; write it again unsigned first")
    path.write_bytes(data + block(_checked_signature(signature), len(data)))


def verify(path: Path, public_key: str) -> bool:
    """Whether the feed carries a block and its signature verifies with ``public_key`` (base64)."""
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    content, signature, length = split(path.read_bytes())
    if not signature or length != len(content):
        return False
    try:
        key = Ed25519PublicKey.from_public_bytes(base64.b64decode(public_key, validate=True))
        key.verify(base64.b64decode(signature, validate=True), content)
        return True
    except (InvalidSignature, ValueError):
        return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    attach_command = commands.add_parser("attach", help="Append the signing block for SIGNATURE")
    attach_command.add_argument("feed", type=Path)
    attach_command.add_argument("signature")
    split_command = commands.add_parser("split", help="Write the signed content to OUT and print the signature")
    split_command.add_argument("feed", type=Path)
    split_command.add_argument("out", type=Path)
    strip_command = commands.add_parser("strip", help="Remove the signing block, so the feed can be signed again")
    strip_command.add_argument("feed", type=Path)
    verify_command = commands.add_parser("verify", help="Check the block with PUBLIC_KEY")
    verify_command.add_argument("feed", type=Path)
    verify_command.add_argument("public_key")
    args = parser.parse_args(argv)
    try:
        if args.command == "attach":
            attach(args.feed, args.signature)
            print(f"Signed {args.feed}")
            return 0
        if args.command == "split":
            content, signature, length = split(args.feed.read_bytes())
            if not signature or length != len(content):
                print(f"{args.feed} has no signing block, or one for other content", file=sys.stderr)
                return 1
            args.out.write_bytes(content)
            print(signature)
            return 0
        if args.command == "strip":
            data = args.feed.read_bytes()
            args.feed.write_bytes(strip(data))
            print(f"{args.feed}: {'signing block removed' if strip(data) != data else 'no signing block'}")
            return 0
        if verify(args.feed, args.public_key):
            print(f"{args.feed}: signature verified")
            return 0
        print(f"{args.feed}: no valid signature for that key", file=sys.stderr)
        return 1
    except (OSError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
