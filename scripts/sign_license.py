"""Make Lumi offline licenses (for Luminary Analytics operations).

A license is the ``lumi.license/v1`` document lumi/license.py verifies: an
Ed25519 signature over its canonical JSON, like a signed organization policy.
Keep the private key offline; only its public half goes into Lumi.

Create a signing key once (writes the private key, prints the public entry)::

    python scripts/sign_license.py keygen --out luminary-license.pem --key-id luminary-2026

Put the printed ``{"<key id>": "<public key>"}`` entry in ``BUILTIN_KEYS`` in
lumi/license.py for a release, or give it to an organization's administrator
for ``LicenseKeys`` (Group Policy or Intune on Windows, the configuration
profile on macOS) or ``license-keys.json`` beside the machine policy file
(macOS and Linux).

Sign a license::

    python scripts/sign_license.py sign --key luminary-license.pem --key-id luminary-2026 \\
        --organization "Acme" --seats 50 --expires 2027-09-30 --offline --out acme.lumi-license.json

Print a key's public entry again, or check a license against it::

    python scripts/sign_license.py public-key --key luminary-license.pem --key-id luminary-2026
    python scripts/sign_license.py verify acme.lumi-license.json --public-key <base64> --key-id luminary-2026

The organization installs the file with ``lumi license install <file>``, or
puts it at ``license.json`` beside the machine policy file.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cryptography.hazmat.primitives import serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey  # noqa: E402

from lumi.license import SCHEMA, canonical, parse  # noqa: E402


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _expiry(text: str) -> str:
    """An expiry date or time as ISO 8601 UTC; a bare date means the end of that day."""
    value = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if len(text) == 10:
        value = value.replace(hour=23, minute=59, second=59)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _load_key(path: Path) -> Ed25519PrivateKey:
    key = serialization.load_pem_private_key(path.read_bytes(), password=None)
    if not isinstance(key, Ed25519PrivateKey):
        raise SystemExit(f"{path} isn't an Ed25519 private key.")
    return key


def _public(key: Ed25519PrivateKey) -> str:
    raw = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return base64.b64encode(raw).decode("ascii")


def keygen(out: Path, key_id: str) -> dict[str, str]:
    if out.exists():
        raise SystemExit(f"{out} exists; refusing to overwrite a signing key.")
    key = Ed25519PrivateKey.generate()
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                            serialization.NoEncryption())
    # Only the owner may read it (on Windows, keep it in a folder only you can open).
    descriptor = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(pem)
    return {key_id: _public(key)}


def sign(key: Ed25519PrivateKey, key_id: str, *, organization: str, seats: int, expires: str, offline: bool,
         license_id: str = "", issued_at: str = "") -> dict:
    """A signed license envelope."""
    document = {
        "schema": SCHEMA,
        "license_id": license_id or f"lumi-{uuid.uuid4().hex[:12]}",
        "organization": organization,
        "seats": seats,
        "offline": offline,
        "issued_at": issued_at or _now(),
        "expires_at": _expiry(expires),
    }
    signature = base64.b64encode(key.sign(canonical(document))).decode("ascii")
    envelope = {"license": document, "key_id": key_id, "signature": signature}
    parse(envelope, trusted_keys={key_id: _public(key)})  # the same check Lumi makes
    return envelope


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Make Lumi offline licenses.")
    commands = parser.add_subparsers(dest="command", required=True)
    make_key = commands.add_parser("keygen", help="Create a license-signing key")
    make_key.add_argument("--out", type=Path, required=True, help="Where to write the private key (PEM)")
    make_key.add_argument("--key-id", required=True, help="A name for the key, such as luminary-2026")
    show = commands.add_parser("public-key", help="Print a key's public entry")
    show.add_argument("--key", type=Path, required=True)
    show.add_argument("--key-id", required=True)
    make = commands.add_parser("sign", help="Sign a license")
    make.add_argument("--key", type=Path, required=True, help="The private key (PEM)")
    make.add_argument("--key-id", required=True)
    make.add_argument("--organization", required=True)
    make.add_argument("--seats", type=int, required=True)
    make.add_argument("--expires", required=True, help="YYYY-MM-DD (end of that day, UTC) or an ISO 8601 time")
    make.add_argument("--offline", action="store_true", help="The license covers offline use")
    make.add_argument("--license-id", default="")
    make.add_argument("--out", type=Path, help="Write here instead of printing")
    check = commands.add_parser("verify", help="Check a license file against a public key")
    check.add_argument("file", type=Path)
    check.add_argument("--public-key", required=True)
    check.add_argument("--key-id", required=True)
    args = parser.parse_args(argv)

    if args.command == "keygen":
        print(json.dumps(keygen(args.out, args.key_id), indent=2))
        return 0
    if args.command == "public-key":
        print(json.dumps({args.key_id: _public(_load_key(args.key))}, indent=2))
        return 0
    if args.command == "verify":
        granted = parse(json.loads(args.file.read_text(encoding="utf-8-sig")),
                        trusted_keys={args.key_id: args.public_key}, source=str(args.file))
        print(json.dumps(granted.summary(), indent=2))
        return 0
    if args.seats < 1:
        raise SystemExit("--seats must be at least 1.")
    envelope = sign(_load_key(args.key), args.key_id, organization=args.organization, seats=args.seats,
                    expires=args.expires, offline=args.offline, license_id=args.license_id)
    text = json.dumps(envelope, indent=2, ensure_ascii=False) + "\n"
    if args.out:
        args.out.write_text(text, encoding="utf-8")
        print(f"Wrote {args.out}")
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
