"""
Compatibility shim for harness orchestrator.

The harness implementation now lives in `resonant_client.harness.orchestrator`.
Keep this module as a thin re-export while GUI callers migrate.
"""

from ..harness.orchestrator import *  # noqa: F401,F403
