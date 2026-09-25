"""Publisher signatures for capability packs (docs/extensions.md#signing-a-pack).

A publisher signs a pack with an Ed25519 key; ``lumi extension sign`` writes
``lumi-pack.sig`` beside the manifest::

    {"format": "lumi-pack-signature/v1", "publisher": "Acme", "key_id": "<fingerprint>",
     "public_key": "<base64>", "digest": "<sha256 of the pack's files>", "signature": "<base64>"}

The digest covers every file in the pack but the signature, the same files an
approval pins. The signature is over the canonical JSON of the other fields.
Lumi checks it whenever it reads a pack:

* ``verified``: the files match, and the key is one the person or their
  organization trusts, under the name they gave it;
* ``unknown_publisher``: the files match the signature, but nobody trusts its
  key yet, so the publisher's name in it is only a claim;
* ``invalid``: the files don't match, or the signature is malformed. Such a
  pack can't be approved;
* ``unsigned``.

A signature never approves a pack. It says who made it, and an organization's
policy can require one from a publisher it lists (``extensions.require_signed``,
``extensions.trusted_publishers``).
"""

from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path
from typing import Any

SIGNATURE_FILE = "lumi-pack.sig"
FORMAT = "lumi-pack-signature/v1"
_DIGEST_PREFIX = b"lumi-pack-signed-content-v1\n"


class PackSigningError(RuntimeError):
    """A key or signature can't be used; the message says why."""


def fingerprint(public_key: str) -> str:
    """A key's id: the first 16 hex digits of the SHA-256 of its 32 raw bytes."""
    try:
        raw = base64.b64decode(public_key, validate=True)
    except ValueError as exc:
        raise PackSigningError("A public key must be base64.") from exc
    if len(raw) != 32:
        raise PackSigningError("An Ed25519 public key is 32 bytes.")
    return hashlib.sha256(raw).hexdigest()[:16]


def generate_key() -> tuple[bytes, str]:
    """A new signing key: (private key PEM, public key base64)."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    private = Ed25519PrivateKey.generate()
    pem = private.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                serialization.NoEncryption())
    public = private.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return pem, base64.b64encode(public).decode("ascii")


def signed_digest(directory: str | Path) -> str:
    """The SHA-256 a signature covers: every file in the pack except its signature."""
    from .capability_packs import _file_sha256, _pack_files

    digest = hashlib.sha256(_DIGEST_PREFIX)
    for relative, path in _pack_files(Path(directory)):
        if relative != SIGNATURE_FILE:
            digest.update(f"pack\0{relative}\0{_file_sha256(path)}\n".encode("utf-8"))
    return digest.hexdigest()


def _statement(record: dict) -> bytes:
    fields = {key: str(record.get(key) or "") for key in ("format", "publisher", "key_id", "public_key", "digest")}
    return json.dumps(fields, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def sign(directory: str | Path, private_key_pem: bytes, publisher: str) -> dict:
    """Sign the pack in ``directory`` and write its signature file."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    try:
        private = serialization.load_pem_private_key(private_key_pem, password=None)
    except (TypeError, ValueError) as exc:
        raise PackSigningError(f"That isn't an unencrypted Ed25519 private key: {exc}") from exc
    if not isinstance(private, Ed25519PrivateKey):
        raise PackSigningError("Packs are signed with Ed25519 keys (lumi extension keygen makes one).")
    name = " ".join(str(publisher or "").split())[:80]
    if not name:
        raise PackSigningError("Say who publishes the pack (--publisher).")
    public = base64.b64encode(private.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw)).decode("ascii")
    record = {"format": FORMAT, "publisher": name, "key_id": fingerprint(public), "public_key": public,
              "digest": signed_digest(directory)}
    record["signature"] = base64.b64encode(private.sign(_statement(record))).decode("ascii")
    (Path(directory) / SIGNATURE_FILE).write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    return record


def check(directory: str | Path, trusted: dict[str, dict] | None = None) -> dict[str, Any]:
    """What the pack's signature says: status, key_id, publisher (the trusted name, else the claim) and reason."""
    path = Path(directory) / SIGNATURE_FILE
    if not path.is_file():
        return {"status": "unsigned", "key_id": "", "publisher": "", "claimed": "", "reason": ""}
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    def invalid(reason: str, record: dict | None = None) -> dict:
        record = record or {}
        return {"status": "invalid", "key_id": str(record.get("key_id") or ""), "publisher": "",
                "claimed": str(record.get("publisher") or ""), "reason": reason}

    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        return invalid(f"{SIGNATURE_FILE} isn't readable JSON")
    if not isinstance(record, dict) or record.get("format") != FORMAT:
        return invalid(f"{SIGNATURE_FILE} isn't a {FORMAT} signature", record if isinstance(record, dict) else None)
    try:
        key_id = fingerprint(str(record.get("public_key") or ""))
    except PackSigningError as exc:
        return invalid(str(exc), record)
    if key_id != record.get("key_id"):
        return invalid("its key id doesn't belong to its key", record)
    try:
        Ed25519PublicKey.from_public_bytes(base64.b64decode(record["public_key"])).verify(
            base64.b64decode(str(record.get("signature") or ""), validate=True), _statement(record))
    except (InvalidSignature, ValueError):
        return invalid("the signature doesn't verify", record)
    try:
        current = signed_digest(directory)
    except Exception as exc:  # noqa: BLE001 - an unreadable pack can't match its signature
        return invalid(f"its files can't be read: {exc}", record)
    if current != record.get("digest"):
        return invalid("its files changed after it was signed", record)
    known = (trusted or {}).get(key_id)
    if isinstance(known, dict) and known.get("public_key") == record["public_key"]:
        return {"status": "verified", "key_id": key_id, "publisher": str(known.get("name") or record["publisher"]),
                "claimed": str(record["publisher"]), "reason": "", "public_key": record["public_key"]}
    return {"status": "unknown_publisher", "key_id": key_id, "publisher": "", "claimed": str(record["publisher"]),
            "reason": "", "public_key": record["public_key"]}


def publishers(value: Any) -> dict[str, dict]:
    """Trusted publishers as ``{key_id: {"name", "public_key"}}``, from a list or a mapping; bad entries are refused."""
    entries = list(value.values()) if isinstance(value, dict) else value if isinstance(value, list) else None
    if entries is None:
        raise PackSigningError("Trusted publishers must be a list of {name, public_key}.")
    result: dict[str, dict] = {}
    for entry in entries:
        if not isinstance(entry, dict) or not str(entry.get("name") or "").strip():
            raise PackSigningError("Each trusted publisher needs a name and a public key.")
        key = str(entry.get("public_key") or "").strip()
        result[fingerprint(key)] = {"name": " ".join(str(entry["name"]).split())[:80], "public_key": key}
    return result
