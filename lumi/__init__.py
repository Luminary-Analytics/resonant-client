"""Lumi — durable agentic coding runtime and desktop client (formerly Resonant)."""
import os

__version__ = "0.19.2.dev11"


def _mirror_legacy_environment() -> None:
    """Honor RESONANT_* settings from before the rebrand as their LUMI_* names.

    Runs at first import, before any module reads its configuration. An
    explicit LUMI_* value always wins over the legacy one.
    """
    for name, value in list(os.environ.items()):
        if name.startswith("RESONANT_"):
            os.environ.setdefault("LUMI_" + name[len("RESONANT_"):], value)


_mirror_legacy_environment()
