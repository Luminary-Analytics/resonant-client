"""Network settings shared by every HTTP call Lumi makes.

Corporate networks break Python tools in two ways: TLS inspection signs
traffic with a company root certificate that only the operating system
trusts, and outbound traffic must go through a proxy. ``configure`` applies
both process-wide from Settings > Network:

* the operating system's certificate store verifies TLS (``truststore``),
  so a company root CA works without exporting PEM bundles;
* a configured proxy is exported as ``HTTPS_PROXY``/``HTTP_PROXY``, which
  httpx and the standard library honour, while local addresses always
  bypass it so Ollama, EXO and the app's own server keep working.

A proxy already set in the user's environment is left alone unless Settings
names one.
"""

from __future__ import annotations

import logging
import os
import threading
import urllib.parse
from typing import Any

logger = logging.getLogger(__name__)

LOCAL_BYPASS = ("localhost", "127.0.0.1", "::1")
_PROXY_VARS = ("HTTPS_PROXY", "HTTP_PROXY")
_lock = threading.Lock()
_state: dict[str, Any] = {"trust_injected": False, "saved_env": None}


def client_options(*, timeout: Any, transport: Any = None, verify: Any = None) -> dict[str, Any]:
    """Keyword arguments for an ``httpx.Client`` that talks to a model provider.

    ``transport`` is for tests (an ``httpx.MockTransport``); production
    callers leave it unset. Proxy and certificate settings apply through the
    environment and the injected trust store, so they need no argument here.
    ``verify`` is an ``ssl.SSLContext`` presenting a client certificate
    (lumi/auth_tokens.py), for connections that require mTLS.
    """
    options: dict[str, Any] = {"timeout": timeout}
    if transport is not None:
        options["transport"] = transport
    if verify is not None:
        options["verify"] = verify
    return options


def validate_proxy_url(value: str) -> str:
    """The proxy URL to store, or raise ValueError with the fix."""
    url = str(value or "").strip()
    if not url:
        return ""
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in {"http", "https", "socks5", "socks5h"} or not parts.hostname:
        raise ValueError("Enter the proxy as http://host:port (or https://, socks5://).")
    if parts.username or parts.password:
        raise ValueError(
            "Proxy sign-in isn't supported yet. Use a proxy that authenticates the machine, "
            "or a local helper such as px or cntlm."
        )
    return url.rstrip("/")


def use_system_certificates(enabled: bool) -> bool:
    """Verify TLS with the operating system's certificate store. Returns whether it is active."""
    with _lock:
        try:
            import truststore
        except ImportError:
            if enabled:
                logger.warning("truststore is not installed; using the bundled certificate list")
            return False
        if enabled and not _state["trust_injected"]:
            truststore.inject_into_ssl()
            _state["trust_injected"] = True
        elif not enabled and _state["trust_injected"]:
            truststore.extract_from_ssl()
            _state["trust_injected"] = False
        return bool(_state["trust_injected"])


def apply_proxy(proxy_url: str, no_proxy: str = "") -> None:
    """Export the configured proxy; restore the user's own variables when cleared."""
    with _lock:
        if _state["saved_env"] is None:
            _state["saved_env"] = {name: os.environ.get(name) for name in (*_PROXY_VARS, "NO_PROXY")}
        saved = _state["saved_env"]
        if proxy_url:
            for name in _PROXY_VARS:
                os.environ[name] = proxy_url
        else:
            for name in _PROXY_VARS:
                if saved.get(name) is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = saved[name]
        bypass: list[str] = []
        for item in [*(saved.get("NO_PROXY") or "").split(","), *str(no_proxy or "").split(","), *LOCAL_BYPASS]:
            item = item.strip()
            if item and item not in bypass:
                bypass.append(item)
        os.environ["NO_PROXY"] = ",".join(bypass)


def configure(settings: Any) -> dict[str, Any]:
    """Apply Settings > Network proxy and certificate choices to this process."""
    get = settings.get if settings is not None else (lambda *args, **kwargs: None)
    system_certs = get("network", "system_certificates", True)
    trust_active = use_system_certificates(system_certs is not False)
    proxy = ""
    try:
        proxy = validate_proxy_url(get("network", "proxy_url", "") or "")
    except ValueError as exc:
        logger.warning("Ignoring the saved proxy: %s", exc)
    apply_proxy(proxy, str(get("network", "no_proxy", "") or ""))
    return {"system_certificates": trust_active, "proxy": bool(proxy)}
