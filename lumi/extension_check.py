"""``lumi extension``: work on extension packs (docs/extensions.md).

* ``check <folder>`` loads the manifest with Lumi's own rules, then starts
  each provider the way a connection would: once to list its models and once
  to answer a short prompt, checking what it writes against the protocol.
  The folder is the author's own pack, so it runs without an approval;
  nothing is installed or saved.
* ``keygen <key file>`` makes an Ed25519 key for signing packs, and ``sign
  <folder>`` writes the pack's ``lumi-pack.sig`` (engine/pack_signing.py).
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
from pathlib import Path

DEFAULT_PROMPT = "Reply with one short sentence."
EVENT_TYPES = {"text", "tool_call", "done", "error"}


def check(folder: str | Path, *, prompt: str = DEFAULT_PROMPT, api_key: str = "", run: bool = True,
          timeout: float = 60.0) -> list[tuple[str, str]]:
    """``(kind, message)`` findings, where kind is ``ok``, ``note`` or ``problem``."""
    from .engine import provider_extensions as extensions
    from .engine.capability_packs import MANIFEST_VERSION, CapabilityPackError, CapabilityPackManager

    folder = Path(folder).expanduser().resolve()
    findings: list[tuple[str, str]] = []
    with tempfile.TemporaryDirectory() as scratch:
        try:
            # Loaded as a personal pack, like Settings' installs (engine/pack_install.py).
            pack, _data = CapabilityPackManager(Path(scratch) / "no-project", roots=())._load(folder)
        except (CapabilityPackError, OSError) as exc:
            return [("problem", str(exc))]
    if pack.problem:
        findings.append(("problem", pack.problem))
    else:
        findings.append(("ok", f"{pack.name} {pack.version} loads (manifest version {pack.manifest_version})."))
    from .engine.pack_signing import signed_digest

    # What an organization's registry pins to accept exactly these files.
    findings.append(("ok", f"Content digest: {signed_digest(folder)}"))
    signature = pack.signature or {}
    key = " ".join(signature.get("key_id", "")[i:i + 4] for i in range(0, 16, 4))
    if signature.get("status") == "verified":
        findings.append(("ok", f"Signed by {signature['publisher']} (key {key}), a publisher your organization trusts."))
    elif signature.get("status") == "unknown_publisher":
        findings.append(("ok", f"Signed as \u201c{signature['claimed']}\u201d with key {key}; the files match."))
    elif signature.get("status") == "unsigned":
        findings.append(("note", "It isn't signed. `lumi extension sign` adds a publisher signature."))
    if pack.manifest_version < MANIFEST_VERSION:
        findings.append(("note", f'Add "manifest_version": {MANIFEST_VERSION}; without it, Lumi reads the '
                                  "manifest as one from before the Extension SDK."))
    if not pack.providers:
        findings.append(("note", "It declares no model providers."))
    for provider in pack.providers:
        label = provider["name"]
        try:
            command = extensions.command_for(pack, provider)
        except extensions.ProviderExtensionError as exc:
            findings.append(("problem", str(exc)))
            continue
        findings.append(("ok", f"{label} starts as: {' '.join(command)}"))
        if not run:
            continue
        try:
            models = extensions.list_models(pack, provider, api_key=api_key, timeout=timeout)
        except extensions.ProviderExtensionError as exc:
            findings.append(("problem", f"{label} didn't list its models: {exc}"))
            continue
        ids = [model["id"] for model in models]
        if not ids:
            findings.append(("problem", f"{label} offers no models."))
            continue
        findings.append(("ok", f"{label} offers {', '.join(ids)}."))
        declared = [model["id"] for model in provider.get("models") or []]
        if declared and set(declared) != set(ids):
            findings.append(("note", f"The manifest lists {', '.join(declared)}; Lumi offers those without "
                                     "starting the process, so keep the two in step."))
        findings.extend(_try_stream(pack, provider, command, ids[0], prompt, api_key, timeout))
    return findings


def _try_stream(pack, provider, command, model, prompt, api_key, timeout) -> list[tuple[str, str]]:
    from .engine import provider_extensions as extensions

    request = {"method": "stream", "params": {"model": model, "tools": [], "max_tokens": 200,
                                              "messages": extensions.messages([], "", prompt)}}
    label = f"{provider['name']} ({model})"
    try:
        events = list(extensions.run(pack, command, request, api_key=api_key, timeout=timeout))
    except extensions.ProviderExtensionError as exc:
        return [("problem", f"{label} failed to answer: {exc}")]
    findings = []
    unknown = sorted({str(event.get("type")) for event in events} - EVENT_TYPES)
    if unknown:
        findings.append(("note", f"{label} wrote types Lumi ignores: {', '.join(unknown)}."))
    last = next((event for event in reversed(events) if event.get("type") in EVENT_TYPES), {})
    if last.get("type") == "error":
        return [*findings, ("problem", f"{label} answered with an error: {last.get('message')}")]
    if last.get("type") != "done":
        return [*findings, ("problem", f"{label} stopped without done(): Lumi would report an unfinished answer.")]
    reply = "".join(str(event.get("text") or "") for event in events if event.get("type") == "text").strip()
    usage = last.get("usage") if isinstance(last.get("usage"), dict) else {}
    findings.append(("ok", f"{label} answered: {reply[:120] or '(no text)'} "
                           f"({usage.get('input_tokens', 0)} tokens in, {usage.get('output_tokens', 0)} out)"))
    if not usage:
        findings.append(("note", f"{label} reported no token usage, so budgets and usage records count it as 0."))
    return findings


def _keygen(path: Path) -> int:
    from .engine.pack_signing import fingerprint, generate_key

    private_pem, public_key = generate_key()
    try:
        # Never over an existing key, and readable only by its owner where the system allows.
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        print(f"{path} already exists. Choose a new file; a key is never overwritten.", file=sys.stderr)
        return 1
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(private_pem)
    print(f"Saved the private key to {path}. Keep it secret: anyone with it can sign as you.")
    print(f"Public key: {public_key}")
    print(f"Key id: {fingerprint(public_key)}")
    print("Publish the public key where people can check it, and sign packs with: lumi extension sign <folder> "
          f"--key {path} --publisher <name>")
    return 0


def _sign(folder: Path, key_file: Path, publisher: str) -> int:
    from .engine.capability_packs import _manifest_path
    from .engine.pack_signing import PackSigningError, sign

    if _manifest_path(folder) is None:
        print(f"{folder} has no lumi-pack.json.", file=sys.stderr)
        return 1
    try:
        record = sign(folder, key_file.read_bytes(), publisher)
    except (OSError, PackSigningError) as exc:
        print(f"Not signed: {exc}", file=sys.stderr)
        return 1
    print(f"Signed {folder} as {record['publisher']} with key {record['key_id']}. "
          "Any later change to its files breaks the signature, so sign again after editing.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="lumi extension", description="Work on Lumi extension packs.")
    commands = parser.add_subparsers(dest="command", required=True)
    checker = commands.add_parser("check", help="Load a pack with Lumi's rules and try its providers.")
    checker.add_argument("folder", help="The pack's folder (with lumi-pack.json)")
    checker.add_argument("--prompt", default=DEFAULT_PROMPT, help="What to ask each provider's first model")
    checker.add_argument("--key-env", default="LUMI_PROVIDER_API_KEY", metavar="NAME",
                         help="An environment variable holding the provider's key (never pass keys as arguments)")
    checker.add_argument("--no-run", action="store_true", help="Check the manifest without starting providers")
    keygen = commands.add_parser("keygen", help="Make a key for signing packs.")
    keygen.add_argument("key_file", help="Where to save the private key. Keep it secret; it isn't encrypted.")
    signer = commands.add_parser("sign", help="Sign a pack: writes lumi-pack.sig. Sign again after any change.")
    signer.add_argument("folder", help="The pack's folder (with lumi-pack.json)")
    signer.add_argument("--key", required=True, help="The private key file from lumi extension keygen")
    signer.add_argument("--publisher", required=True, help="Who publishes the pack, as people will see it")
    args = parser.parse_args(argv)
    if args.command == "keygen":
        return _keygen(Path(args.key_file))
    if args.command == "sign":
        return _sign(Path(args.folder), Path(args.key), args.publisher)
    findings = check(args.folder, prompt=args.prompt, api_key=os.environ.get(args.key_env, ""), run=not args.no_run)
    marks = {"ok": "ok", "note": "note", "problem": "PROBLEM"}
    for kind, message in findings:
        print(f"{marks[kind]:>7}  {message}")
    problems = sum(kind == "problem" for kind, _ in findings)
    print(f"\n{problems} problem{'s' if problems != 1 else ''}." if problems else "\nReady for Lumi.",
          file=sys.stderr if problems else sys.stdout)
    return 1 if problems else 0
