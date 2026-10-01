"""Human administration through one explicit native OIDC login and API request."""

import argparse
import json
import sys

from .admin_client import AdminClient, AdminClientError, load_config, read_json
from .oidc import NativeOIDC


def main(argv=None):
    parser = argparse.ArgumentParser(description="Sign in to SONN governance and perform one explicit operation")
    parser.add_argument("--config-file", required=True, help="Absolute operator client JSON configuration")
    choice = parser.add_mutually_exclusive_group(required=True)
    choice.add_argument("--identity", action="store_true", help="Show only the verified opaque actor identity")
    choice.add_argument("--request-file", help="Absolute JSON file with method, fixed API path, optional query and body")
    args = parser.parse_args(argv)
    try:
        config = load_config(args.config_file)
        request = {"method": "GET", "path": "/v1/identity"} if args.identity else read_json(args.request_file, maximum=1402200)
        AdminClient.validate(request)
        credential = NativeOIDC(config.oidc).login()
        result = AdminClient(config, credential).request(request)
        print(json.dumps(result, sort_keys=True, ensure_ascii=True))
        return 0
    except AdminClientError as exc:
        # The caller can explicitly inspect/replay its saved command ID after
        # an ambiguous outcome. Do not hide ambiguity behind an automatic retry.
        label = "outcome_unknown" if exc.delivery_unknown else "request_denied"
        print(json.dumps({"error": label, "status": exc.status}), file=sys.stderr)
        return 2
    except Exception:
        print("Governance sign-in or configuration unavailable.", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
