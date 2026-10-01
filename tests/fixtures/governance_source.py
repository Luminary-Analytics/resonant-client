"""Locate the governance service for the managed-team browser fixtures.

The server-side governance service (the ``sonn_governance`` package) lives in
Lumi Cloud, not in this repository. The managed fixtures import its source and
its test helpers (``test_store``, ``test_host_http``, ``test_managed_host_client``)
from an explicit external checkout named by ``LUMI_GOVERNANCE_SOURCE``: either a
Lumi Cloud checkout or its ``services/governance`` directory. Nothing here guesses
a location; an unset or wrong path stops the fixture with a clear message.
"""

from __future__ import annotations

import os
from pathlib import Path

ENVIRONMENT = "LUMI_GOVERNANCE_SOURCE"


def governance_paths() -> list[str]:
    """Return the governance ``src`` and ``tests`` directories for ``sys.path``."""
    raw = os.environ.get(ENVIRONMENT, "").strip()
    if not raw:
        raise SystemExit(f"{ENVIRONMENT} is not set: point it at a Lumi Cloud checkout (or its "
                         "services/governance directory) to run the managed-team fixtures")
    base = Path(raw).expanduser().resolve()
    for candidate in (base, base / "services" / "governance"):
        if (candidate / "src" / "sonn_governance").is_dir() and (candidate / "tests").is_dir():
            return [str(candidate / "src"), str(candidate / "tests")]
    raise SystemExit(f"{ENVIRONMENT}={raw} contains no services/governance/src/sonn_governance "
                     "and tests directory")
