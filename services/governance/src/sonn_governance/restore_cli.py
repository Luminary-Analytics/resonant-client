"""Explicit offline recovery commands; no service-start or unseal command."""

import argparse
import json
from pathlib import Path
import sys

from psycopg.conninfo import conninfo_to_dict, make_conninfo

from .identity import _unique_object
from .restore import database_identity, inspect_seal, prepare_target, reconcile, seal_source


def _read(path, maximum):
    source = Path(path)
    if not source.is_absolute() or not source.is_file():
        raise ValueError("an absolute protected file is required")
    for part in (source, *source.parents):
        if part.is_symlink() or getattr(part.lstat(), "st_file_attributes", 0) & 0x400:
            raise ValueError("offline configuration cannot traverse symbolic links")
    with source.open("rb") as stream:
        data = stream.read(maximum + 1)
    if len(data) > maximum:
        raise ValueError("file exceeds offline limit")
    return json.loads(data, object_pairs_hook=_unique_object)


def _configuration(path, action):
    value = _read(path, 65536)
    fields = {
        "identity": {"operator_dsn"},
        "seal-source": {"source_dsn", "archive_dsn", "expected_database", "tenant_id", "archive_id", "source_id", "seal_id"},
        "inspect-seal": {"source_dsn", "expected_database"},
        "prepare-target": {"target_dsn", "expected_database", "restore_id"},
        "reconcile": {"target_dsn", "archive_dsn", "expected_database", "restore_id", "tenant_id", "archive_id", "source_id"},
    }[action]
    if type(value) is not dict or set(value) != fields | {"profile"} or value["profile"] not in {"test", "production"}:
        raise ValueError("configuration must match the exact offline command")
    profile = value.pop("profile")
    for field in fields:
        if not field.endswith("_dsn"):
            continue
        if type(value[field]) is not str or not 1 <= len(value[field]) <= 8192:
            raise ValueError("invalid protected database credential")
        parts = conninfo_to_dict(value[field])
        if profile == "production" and parts.get("sslmode") != "verify-full":
            raise ValueError("production requires verified database TLS")
        if profile == "test" and parts.get("host") not in {"127.0.0.1", "::1"}:
            raise ValueError("test profile requires explicit loopback")
        if profile == "test":
            # Explicit hostaddr overrides PGHOSTADDR and service-file routing;
            # host alone does not determine where libpq opens the socket.
            value[field] = make_conninfo(value[field], hostaddr=parts["host"])
    return value


def main(argv=None):
    parser = argparse.ArgumentParser(description="Permanently quarantine offline SONN retention recovery databases")
    parser.add_argument("action", choices=("identity", "seal-source", "inspect-seal", "prepare-target", "reconcile"))
    parser.add_argument("--config-file", required=True, help="Absolute protected operator configuration")
    parser.add_argument("--output-file", help="New absolute metadata file; existing files are never overwritten")
    parser.add_argument("--checkpoint-file")
    parser.add_argument("--checkpoint-sha256", help="Independent custodian pin, not merely the digest inside the file")
    args = parser.parse_args(argv)
    try:
        config = _configuration(args.config_file, args.action)
        if args.action in {"seal-source", "inspect-seal"} and not args.output_file:
            raise ValueError("source checkpoint requires a new output file")
        output = Path(args.output_file) if args.output_file else None
        if output and (not output.is_absolute() or output.exists() or not output.parent.is_dir()):
            raise ValueError("output must be a new absolute file")
        if args.action == "reconcile":
            if not args.checkpoint_file or not args.checkpoint_sha256:
                raise ValueError("reconciliation requires checkpoint and independent digest")
            document = _read(args.checkpoint_file, 16 * 1024 * 1024)
            if type(document) is not dict or set(document) != {"checkpoint", "sha256"} or document["sha256"] != args.checkpoint_sha256:
                raise ValueError("checkpoint wrapper differs from independent pin")
            result = reconcile(**config, checkpoint=document["checkpoint"], checkpoint_sha256=args.checkpoint_sha256)
        else:
            if args.checkpoint_file or args.checkpoint_sha256:
                raise ValueError("checkpoint flags apply only to reconciliation")
            function = {"identity": database_identity, "seal-source": seal_source,
                        "inspect-seal": inspect_seal, "prepare-target": prepare_target}[args.action]
            result = function(**config)
        if output:
            with output.open("x", encoding="utf-8") as stream:
                json.dump(result, stream, sort_keys=True, indent=2)
                stream.write("\n")
        else:
            print(json.dumps(result, sort_keys=True))
        return 0
    except Exception:
        # Lost acknowledgement/output failure may follow a committed seal. Never
        # retry a different seal: inspect-seal recovers its immutable checkpoint.
        print("Offline recovery refused or acknowledgement unavailable. Keep databases offline; inspect retained quarantine evidence before retrying. Credentials and provider work were not emitted.", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
