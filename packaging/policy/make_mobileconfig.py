"""Turn a Lumi policy file into a macOS configuration profile, for Jamf, Intune or another MDM.

    python3 packaging/policy/make_mobileconfig.py acme-policy.json --out lumi-policy.mobileconfig
    python3 packaging/policy/make_mobileconfig.py acme-policy.json --plist --out com.luminaryanalytics.lumi.plist
    python3 packaging/policy/make_mobileconfig.py signed.json --keys policy-keys.json --out lumi-policy.mobileconfig

The profile sets keys of the ``com.luminaryanalytics.lumi`` preferences at
device (System) scope, which Lumi reads from ``/Library/Managed Preferences``
(lumi/policy.py): ``Policy``, the policy, and with ``--keys`` also
``PolicyKeys``, the signing keys the Mac trusts, as the registry's
``PolicyKeys`` does on Windows. ``--plist`` writes only those preferences:
Jamf Pro's Application & Custom Settings and Intune's preference file profile
take that form and wrap it themselves.

The policy is checked with the app's own parser first, so a mistake is found
here rather than on every Mac, where it would stop Lumi's model requests
until fixed. A signed policy's signature is verified too when ``--keys`` is
given; otherwise it's checked for structure only, and each Mac verifies it.

The profile's identifiers come from ``--identifier`` and its UUIDs from the
identifier and the contents. Regenerating the same policy gives the same
profile, and a changed policy replaces the old one when deployed with the
same identifier.
"""

from __future__ import annotations

import argparse
import json
import plistlib
import sys
import uuid
from pathlib import Path

DOMAIN = "com.luminaryanalytics.lumi"
REPO = Path(__file__).resolve().parents[2]


def check(document: dict, keys: dict[str, str] | None = None) -> str:
    """The organization's name, after the app's parser accepts the policy; raise ValueError otherwise."""
    if str(REPO) not in sys.path:
        sys.path.insert(0, str(REPO))
    try:
        from lumi.policy import PolicyError, parse
    except ImportError:
        # Copied out of the repository: the profile is still made, unchecked.
        print("Lumi's policy parser isn't available here, so only the JSON was checked.", file=sys.stderr)
        inner = document.get("policy", document)
        return str(inner.get("organization") or "") if isinstance(inner, dict) else ""
    signed = "signature" in document
    if signed and not keys:
        document = document.get("policy")
        if not isinstance(document, dict):
            raise ValueError("A signed policy needs its 'policy' object.")
    try:
        return parse(document, source="profile", trusted_keys=keys or {}).organization
    except PolicyError as exc:
        raise ValueError(str(exc)) from exc


def preferences(document: dict, keys: dict[str, str] | None = None) -> dict:
    """The managed preferences: the policy as compact JSON text under ``Policy``, and any keys.

    Text rather than a dictionary, because property lists have no null and
    policies may use it.
    """
    data = {"Policy": json.dumps(document, separators=(",", ":"), ensure_ascii=False)}
    if keys:
        data["PolicyKeys"] = dict(keys)
    return data


def profile(document: dict, *, identifier: str, organization: str, keys: dict[str, str] | None = None) -> dict:
    """A configuration profile at System scope carrying ``preferences(document, keys)``."""
    text = json.dumps([document, keys or {}], sort_keys=True)
    payload = {
        "PayloadType": DOMAIN,
        "PayloadIdentifier": f"{identifier}.settings",
        "PayloadUUID": str(uuid.uuid5(uuid.NAMESPACE_URL, f"{identifier}.settings\n{text}")).upper(),
        "PayloadVersion": 1,
        "PayloadDisplayName": "Lumi organization policy",
        **preferences(document, keys),
    }
    return {
        "PayloadContent": [payload],
        "PayloadDisplayName": f"Lumi policy for {organization}" if organization else "Lumi policy",
        "PayloadDescription": "Sets the organization policy the Lumi app follows on this Mac.",
        "PayloadIdentifier": identifier,
        "PayloadOrganization": organization,
        "PayloadScope": "System",
        "PayloadType": "Configuration",
        "PayloadUUID": str(uuid.uuid5(uuid.NAMESPACE_URL, f"{identifier}\n{text}")).upper(),
        "PayloadVersion": 1,
        "PayloadRemovalDisallowed": True,
    }


def _read_json(path: Path, what: str):
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"Couldn't read the {what} {path}: {exc}") from exc


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("policy", type=Path, help="the policy file (lumi.policy/v1 JSON, signed or not)")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--keys", type=Path,
                        help="a JSON object of trusted signing keys (key id: base64 Ed25519 public key)")
    parser.add_argument("--identifier", default=f"{DOMAIN}.policy",
                        help="the profile's identifier; keep it the same for every version you deploy")
    parser.add_argument("--plist", action="store_true",
                        help="write only the preferences, for Jamf Custom Settings or an Intune preference file")
    args = parser.parse_args(argv)
    try:
        document = _read_json(args.policy, "policy")
        if not isinstance(document, dict):
            raise ValueError("A policy must be a JSON object.")
        keys = _read_json(args.keys, "keys") if args.keys else None
        if keys is not None and not (isinstance(keys, dict) and keys
                                     and all(isinstance(k, str) and isinstance(v, str) for k, v in keys.items())):
            raise ValueError("The keys file must be a JSON object of key ids and base64 public keys.")
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 1
    try:
        organization = check(document, keys)
    except ValueError as exc:
        print(f"Lumi would refuse this policy: {exc}", file=sys.stderr)
        return 1
    data = preferences(document, keys) if args.plist else profile(document, identifier=args.identifier,
                                                                  organization=organization, keys=keys)
    args.out.write_bytes(plistlib.dumps(data))
    print(f"Wrote {args.out} for {organization or 'the organization'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
