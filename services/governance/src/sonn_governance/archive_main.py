"""Explicit operator-started relay; credentials never enter the resource API."""

import argparse
import json
from pathlib import Path
import sys
import threading

from psycopg.conninfo import conninfo_to_dict, make_conninfo

from .archive import AuditArchive, AuditRelay
from .identity import _unique_object
from .models import identifier


def load_configuration(path):
    """Read a bounded protected configuration, without opening database sockets."""
    source = Path(path)
    if not source.is_absolute() or not source.is_file():
        raise ValueError("An absolute protected configuration file is required")
    with source.open("rb") as stream:
        encoded = stream.read(65537)
    if len(encoded) > 65536:
        raise ValueError("Configuration exceeds limit")
    values = json.loads(encoded, object_pairs_hook=_unique_object)
    expected = {"source_dsn", "archive_dsn", "source_id", "archive_id", "tenant_ids", "profile", "interval_seconds"}
    if type(values) is not dict or set(values) != expected:
        raise ValueError("Invalid relay configuration")
    for field in ("source_id", "archive_id"):
        identifier(values[field])
    tenants = values["tenant_ids"]
    if type(tenants) is not list or not 1 <= len(tenants) <= 100 or len(set(tenants)) != len(tenants):
        raise ValueError("Invalid configured tenant set")
    for tenant in tenants:
        identifier(tenant)
    if values["profile"] not in {"production", "test"} or type(values["interval_seconds"]) is not int or not 1 <= values["interval_seconds"] <= 60:
        raise ValueError("Invalid explicit relay profile")
    for field in ("source_dsn", "archive_dsn"):
        value = values[field]
        if type(value) is not str or not 1 <= len(value) <= 8192:
            raise ValueError("Bounded protected credentials are required")
        parts = conninfo_to_dict(value)
        if values["profile"] == "production" and parts.get("sslmode") != "verify-full":
            raise ValueError("Production archive connections require verified TLS")
        if values["profile"] == "test":
            if parts.get("host") not in {"127.0.0.1", "::1"}:
                raise ValueError("Test archive connections require explicit loopback hosts")
            # libpq uses hostaddr for the network address even when host is
            # explicit. Pin it here so DSN/service/environment overrides cannot
            # send a loopback fixture's credentials to an external database.
            values[field] = make_conninfo(value, hostaddr=parts["host"])
    return values


def main(argv=None):
    parser = argparse.ArgumentParser(description="Relay SONN audit metadata into a separately protected archive")
    parser.add_argument("--config-file", required=True)
    parser.add_argument("--once", action="store_true", help="Deliver one bounded page per configured tenant and exit")
    args = parser.parse_args(argv)
    try:
        config = load_configuration(args.config_file)
        archive = AuditArchive(config["archive_dsn"], archive_id=config["archive_id"], source_id=config["source_id"])
        relay = AuditRelay(config["source_dsn"], archive)
        wait = threading.Event()
        while True:
            delivered = sum(relay.drain(tenant)["delivered"] for tenant in config["tenant_ids"])
            if args.once:
                print(json.dumps({"delivered": delivered, "tenants_checked": len(config["tenant_ids"])}))
                return 0
            wait.wait(config["interval_seconds"])
    except KeyboardInterrupt:
        return 0
    except Exception:
        print("Audit relay unavailable; verify protected configuration, role grants and archive connectivity.", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
