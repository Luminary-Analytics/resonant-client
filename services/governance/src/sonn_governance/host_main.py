"""Explicit standalone certificate-authenticated host API launch."""

import argparse
import json
from pathlib import Path
import sys

from .host_http import HostHTTPServer, HostTransportConfig
from .hosts import HostGovernance
from .identity import _unique_object
from .managed_resources import ManagedResources
from .monitoring import RunMonitoring
from .store import GovernanceStore


def load_configuration(path):
    with Path(path).open("rb") as stream:
        data = stream.read(65537)
    if len(data) > 65536:
        raise ValueError("Host service configuration exceeds limit")
    value = json.loads(data, object_pairs_hook=_unique_object)
    if (type(value) is not dict or set(value) not in ({"database_dsn", "transport"}, {"database_dsn", "transport", "sharing"})
            or type(value["database_dsn"]) is not str or not 1 <= len(value["database_dsn"]) <= 8192
            or type(value["transport"]) is not dict):
        raise ValueError("Explicit runtime connection and host transport configuration required")
    sharing = None
    if "sharing" in value:
        from .__main__ import load_content_keys
        if type(value["sharing"]) is not dict or set(value["sharing"]) != {"key_ring_file"}:
            raise ValueError("An explicit sharing key-ring file is required")
        sharing = load_content_keys(value["sharing"]["key_ring_file"])
    return value["database_dsn"], HostTransportConfig(**value["transport"]), sharing


def main(argv=None):
    parser = argparse.ArgumentParser(description="Run the separate SONN certificate-authenticated host API")
    parser.add_argument("--config-file", required=True, help="Operator-protected JSON host service configuration")
    args = parser.parse_args(argv)
    try:
        dsn, transport, sharing_keys = load_configuration(args.config_file)
        hosts = HostGovernance(GovernanceStore(dsn), minimum_runner_protocol=2)
        from .managed_collaboration import ManagedCollaboration
        sharing = ManagedCollaboration(hosts, sharing_keys) if sharing_keys is not None else None
        with HostHTTPServer(transport, hosts, monitoring=RunMonitoring(hosts), resources=ManagedResources(hosts), collaboration=sharing) as server:
            server.serve_forever(poll_interval=.2)
        return 0
    except KeyboardInterrupt:
        return 0
    except Exception:
        print("Host service could not start; verify protected configuration, certificate trust and runtime role.", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
