"""Explicit provisioning listener; credentials are operator-enrolled in the DB."""

import argparse
import ipaddress
import json
from pathlib import Path
import sys

import uvicorn

from .identity import _unique_object
from .scim import ScimStore
from .scim_app import ScimTransportConfig, create_scim_app
from .store import GovernanceStore


def load_configuration(path):
    with Path(path).open("rb") as stream:
        encoded = stream.read(65537)
    if len(encoded) > 65536:
        raise ValueError("Provisioning configuration exceeds limit")
    value = json.loads(encoded, object_pairs_hook=_unique_object)
    if (type(value) is not dict or set(value) != {"database_dsn", "listen", "transport", "tls"}
            or type(value["database_dsn"]) is not str or not 1 <= len(value["database_dsn"]) <= 8192
            or type(value["transport"]) is not dict):
        raise ValueError("Explicit provisioning transport configuration required")
    config = ScimTransportConfig(**value["transport"])
    listener = value["listen"]
    if (type(listener) is not dict or set(listener) != {"host", "port"}
            or type(listener["host"]) is not str or type(listener["port"]) is not int
            or not 1 <= listener["port"] <= 65535):
        raise ValueError("Explicit provisioning listener required")
    address = ipaddress.ip_address(listener["host"])
    if config.profile == "test" and not address.is_loopback:
        raise ValueError("Test provisioning must bind loopback only")
    tls = value["tls"]
    if tls is None:
        if config.profile != "test":
            raise ValueError("Production provisioning requires TLS")
    else:
        if type(tls) is not dict or set(tls) != {"certificate_file", "private_key_file"}:
            raise ValueError("Explicit TLS files required")
        for item in tls.values():
            if type(item) is not str or not Path(item).is_absolute() or not Path(item).is_file():
                raise ValueError("Existing absolute TLS files required")
    return {**value, "transport": config}


def main(argv=None):
    parser = argparse.ArgumentParser(description="Run the separate SONN SCIM provisioning API")
    parser.add_argument("--config-file", required=True, help="Operator-protected JSON provisioning configuration")
    args = parser.parse_args(argv)
    try:
        config = load_configuration(args.config_file)
        app = create_scim_app(ScimStore(GovernanceStore(config["database_dsn"])), config=config["transport"])
        tls = config["tls"] or {}
        uvicorn.run(app, host=config["listen"]["host"], port=config["listen"]["port"],
                    ssl_certfile=tls.get("certificate_file"), ssl_keyfile=tls.get("private_key_file"),
                    proxy_headers=False, access_log=False, log_config=None, server_header=False, ws="none",
                    limit_concurrency=64, timeout_keep_alive=5, timeout_graceful_shutdown=10,
                    h11_max_incomplete_event_size=16384)
        return 0
    except Exception:
        print("Provisioning service could not start; verify protected configuration and runtime database role.", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
