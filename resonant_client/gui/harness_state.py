"""
Compatibility shim for harness state.

The harness implementation now lives in `resonant_client.harness.state`.
Keep this module as a thin re-export while GUI callers migrate.
"""

from ..harness.state import *  # noqa: F401,F403
