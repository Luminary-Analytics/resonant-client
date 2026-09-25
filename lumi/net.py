"""HTTP client options shared by the model provider adapters.

One place decides TLS verification, proxies and timeouts for provider
traffic, so a corporate network (TLS inspection, a mandatory proxy) is
configured once rather than per adapter.
"""

from __future__ import annotations

from typing import Any


def client_options(*, timeout: Any, transport: Any = None) -> dict[str, Any]:
    """Keyword arguments for an ``httpx.Client`` that talks to a model provider.

    ``transport`` is for tests (an ``httpx.MockTransport``); production
    callers leave it unset.
    """
    options: dict[str, Any] = {"timeout": timeout}
    if transport is not None:
        options["transport"] = transport
    return options
