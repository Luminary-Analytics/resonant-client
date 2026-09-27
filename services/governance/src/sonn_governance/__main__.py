"""Explicit standalone service launch from an operator-protected config file."""

from __future__ import annotations

import argparse
import base64
import ipaddress
import json
import os
from pathlib import Path
import sys

import uvicorn

from .app import create_app
from .content import ContentKeys, ContentStore
from .identity import Identity, IdentityConfig, _unique_object
from .hosts import HostGovernance
from .monitoring import RunMonitoring
from .store import GovernanceStore


def load_content_keys(path: str) -> ContentKeys:
    """Read a separate operator-protected key ring; never copy it to the DB.

    Windows operators must restrict the file ACL to the service identity and
    administrators. POSIX additionally rejects group/other-readable material.
    The file is not a credential discovery source and cannot be a symlink.
    """
    if type(path) is not str:
        raise ValueError("An absolute protected key-ring file is required")
    source = Path(path)
    if not source.is_absolute() or source.is_symlink() or not source.is_file():
        raise ValueError("An absolute protected key-ring file is required")
    if os.name != "nt" and source.stat().st_mode & 0o077:
        raise ValueError("Key-ring permissions must be restricted to its owner")
    with source.open("rb") as stream:
        encoded = stream.read(16385)
    if len(encoded) > 16384:
        raise ValueError("Key ring exceeds its limit")
    value = json.loads(encoded, object_pairs_hook=_unique_object)
    if (type(value) is not dict or set(value) != {"active_id", "keys"}
            or type(value["keys"]) is not dict or not 1 <= len(value["keys"]) <= 16):
        raise ValueError("Invalid key-ring configuration")
    keys = {}
    for key, text in value["keys"].items():
        if type(text) is not str or len(text) != 44:
            raise ValueError("Invalid key-ring material")
        decoded = base64.b64decode(text, validate=True)
        if len(decoded) != 32 or base64.b64encode(decoded).decode("ascii") != text:
            raise ValueError("Invalid key-ring material")
        keys[key] = decoded
    return ContentKeys(keys, value["active_id"])


def load_configuration(path: str | Path) -> dict:
    """Validate launch settings without starting a listener or revealing secrets."""
    source = Path(path)
    with source.open("rb") as stream:
        encoded = stream.read(65537)
    if len(encoded) > 65536:
        raise ValueError("Service configuration exceeds its limit")
    values = json.loads(encoded, object_pairs_hook=_unique_object)
    required = {"database_dsn", "identity", "listen", "tls"}
    if type(values) is not dict or not required <= set(values) or set(values) - required - {"content", "sharing"}:
        raise ValueError("Invalid service configuration fields")
    if type(values["database_dsn"]) is not str or not values["database_dsn"] or len(values["database_dsn"]) > 8192:
        raise ValueError("A bounded runtime database connection is required")
    if type(values["identity"]) is not dict:
        raise ValueError("Explicit identity configuration is required")
    config = IdentityConfig(**values["identity"])
    listener = values["listen"]
    if (type(listener) is not dict or set(listener) != {"host", "port"} or type(listener["host"]) is not str
            or type(listener["port"]) is not int or not 1 <= listener["port"] <= 65535):
        raise ValueError("An explicit bounded listener is required")
    # An IP literal avoids test-host DNS changes or a deceptive localhost alias.
    address = ipaddress.ip_address(listener["host"])
    if config.profile == "test" and not address.is_loopback:
        raise ValueError("Test identity requires a loopback-only listener")
    tls = values["tls"]
    if tls is None:
        if config.profile != "test":
            raise ValueError("Production governance requires TLS on the service transport")
    else:
        if type(tls) is not dict or set(tls) != {"certificate_file", "private_key_file"}:
            raise ValueError("Explicit TLS certificate and key files are required")
        for value in tls.values():
            if type(value) is not str or not Path(value).is_absolute() or not Path(value).is_file():
                raise ValueError("TLS material must be supplied by an existing absolute file")
    content = None
    if "content" in values:
        if type(values["content"]) is not dict or set(values["content"]) != {"key_ring_file"}:
            raise ValueError("An explicit content key-ring file is required")
        content = load_content_keys(values["content"]["key_ring_file"])
    sharing = None
    if "sharing" in values:
        if type(values["sharing"]) is not dict or set(values["sharing"]) != {"key_ring_file"}:
            raise ValueError("An explicit sharing key-ring file is required")
        sharing = load_content_keys(values["sharing"]["key_ring_file"])
    return {**values, "identity": config, "content": content, "sharing": sharing}


def main(argv=None) -> int:
    """Start only when explicitly invoked; never enroll or migrate automatically."""
    parser = argparse.ArgumentParser(description="Run the separate SONN governance resource API")
    parser.add_argument("--config-file", required=True, help="Operator-protected JSON service configuration")
    args = parser.parse_args(argv)
    try:
        settings = load_configuration(args.config_file)
        store = GovernanceStore(settings["database_dsn"])
        content = ContentStore(store, settings["content"]) if settings["content"] is not None else None
        hosts = HostGovernance(store)
        from .managed_collaboration import ManagedCollaboration
        from .sharing_retention import SharingRetention
        sharing = ManagedCollaboration(hosts, settings["sharing"]) if settings["sharing"] is not None else None
        app = create_app(Identity(settings["identity"]), store, hosts=hosts, content=content,
                         monitoring=RunMonitoring(hosts), collaboration=sharing, sharing_retention=SharingRetention(store))
        tls = settings["tls"] or {}
        uvicorn.run(app, host=settings["listen"]["host"], port=settings["listen"]["port"],
            ssl_certfile=tls.get("certificate_file"), ssl_keyfile=tls.get("private_key_file"),
            proxy_headers=False, access_log=False, log_config=None, server_header=False, ws="none",
            limit_concurrency=64, timeout_keep_alive=5, timeout_graceful_shutdown=10,
            h11_max_incomplete_event_size=16384)
        return 0
    except Exception:
        print("Governance service could not start; verify the protected configuration and runtime database role.", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
