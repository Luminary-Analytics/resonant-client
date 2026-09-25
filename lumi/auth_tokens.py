"""Tokens and client certificates for enterprise model sign-in.

* **OAuth 2.0 client credentials**: a connection with ``auth: "oauth"``
  exchanges its client id and secret at ``token_url`` for a bearer token.
* **Microsoft Entra ID** (Azure OpenAI, ``auth: "entra"``): a client id and
  secret in the tenant, or, without them, the machine's own sign-in through
  ``azure-identity`` (managed identity, environment, Visual Studio Code …)
  when it is installed, then the Azure CLI (``az login``).
* **Client certificates (mTLS)**: ``ssl_context`` loads a certificate and key
  for gateways that require them, on top of the trust store Lumi already uses.

Tokens are cached in memory until a minute before they expire. Nothing here
logs or returns a secret or a token except to the caller that sends it.
"""

from __future__ import annotations

import json
import re
import shutil
import ssl
import subprocess
import threading
import time
from typing import Any

AZURE_OPENAI_SCOPE = "https://cognitiveservices.azure.com/.default"
_REFRESH_EARLY = 60.0
_TENANT = re.compile(r"[A-Za-z0-9][A-Za-z0-9.-]{0,99}")
_RESOURCE = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://[A-Za-z0-9._:/-]+")

_lock = threading.Lock()
_cache: dict[tuple, tuple[str, float]] = {}


class SignInError(RuntimeError):
    """A token couldn't be obtained; the message says what to check."""


def _cached(key: tuple) -> str:
    with _lock:
        token, expires = _cache.get(key, ("", 0.0))
    return token if token and time.time() < expires - _REFRESH_EARLY else ""


def _store(key: tuple, token: str, lifetime: float) -> str:
    with _lock:
        _cache[key] = (token, time.time() + max(60.0, float(lifetime or 3600)))
    return token


def clear_cache() -> None:
    with _lock:
        _cache.clear()


def client_credentials_token(token_url: str, client_id: str, client_secret: str, *, scope: str = "",
                             audience: str = "", transport: Any = None) -> str:
    """An OAuth 2.0 client-credentials access token (RFC 6749 §4.4)."""
    key = ("oauth", token_url, client_id, scope, audience)
    cached = _cached(key)
    if cached:
        return cached
    if not (token_url and client_id and client_secret):
        raise SignInError("OAuth sign-in needs a token URL, a client id and a client secret.")
    import httpx

    from .net import client_options

    form = {"grant_type": "client_credentials", "client_id": client_id, "client_secret": client_secret}
    if scope:
        form["scope"] = scope
    if audience:
        form["audience"] = audience
    try:
        with httpx.Client(**client_options(timeout=30.0, transport=transport)) as client:
            response = client.post(token_url, data=form, headers={"Accept": "application/json"})
    except httpx.HTTPError as exc:
        raise SignInError(f"The token endpoint didn't answer ({type(exc).__name__}).") from exc
    try:
        body = response.json()
    except ValueError:
        body = {}
    if response.status_code >= 400 or not body.get("access_token"):
        # OAuth errors name the problem without echoing the secret.
        detail = body.get("error_description") or body.get("error") or f"HTTP {response.status_code}"
        raise SignInError(f"Sign-in was refused: {detail}")
    return _store(key, str(body["access_token"]), float(body.get("expires_in") or 3600))


def entra_token(tenant: str, *, client_id: str = "", client_secret: str = "", scope: str = "",
                transport: Any = None) -> str:
    """A Microsoft Entra ID token for Azure OpenAI."""
    scope = scope or AZURE_OPENAI_SCOPE
    if tenant and not _TENANT.fullmatch(tenant):
        raise SignInError("The tenant id should be a GUID or a domain name.")
    if client_id:
        # An app registration signs in as itself; never fall back to whoever
        # is signed in on this computer.
        if not client_secret:
            raise SignInError("Entra ID sign-in with a client id needs its client secret. "
                              "Clear the client id to use this computer's Azure sign-in.")
        if not tenant:
            raise SignInError("Entra ID sign-in with a client secret needs the tenant id.")
        url = f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"
        return client_credentials_token(url, client_id, client_secret, scope=scope, transport=transport)
    key = ("entra", tenant, scope)
    cached = _cached(key)
    if cached:
        return cached
    last = ""
    try:
        from azure.identity import DefaultAzureCredential  # type: ignore[import-not-found]
    except ImportError:
        DefaultAzureCredential = None
    if DefaultAzureCredential is not None:
        try:
            token = DefaultAzureCredential(**({"additionally_allowed_tenants": [tenant]} if tenant else {})).get_token(
                scope, **({"tenant_id": tenant} if tenant else {}))
            return _store(key, token.token, token.expires_on - time.time())
        except Exception as exc:  # azure-identity raises its own credential errors
            last = (str(exc).splitlines() or [""])[0][:200]
    resource = scope.removesuffix("/.default")
    # On Windows `az` is a batch file, which cmd.exe parses: allow only plain
    # resource URIs so nothing in the arguments can be interpreted.
    if not _RESOURCE.fullmatch(resource):
        raise SignInError("The scope for Azure CLI sign-in should be a single resource URI.")
    az = shutil.which("az")
    completed = None
    if az:
        command = [az, "account", "get-access-token", "--resource", resource, "--output", "json"]
        if tenant:
            command += ["--tenant", tenant]
        from .processes import background_process_kwargs

        try:
            completed = subprocess.run(command, capture_output=True, text=True, timeout=60,
                                       **background_process_kwargs())
        except (OSError, subprocess.TimeoutExpired):
            completed = None
    if completed is None or completed.returncode != 0:
        detail = (completed.stderr.strip().splitlines() or [""])[-1][:200] if completed is not None else ""
        hint = last or detail or "Sign in with `az login`, or give the connection a client id and secret."
        raise SignInError(f"No Entra ID sign-in is available. {hint}")
    try:
        data = json.loads(completed.stdout)
        token = str(data["accessToken"])
        expires = float(data.get("expires_on") or 0) or time.time() + 3000
    except (ValueError, KeyError, TypeError) as exc:
        raise SignInError("The Azure CLI returned an unexpected token response.") from exc
    return _store(key, token, expires - time.time())


def ssl_context(cert_file: str, key_file: str = "") -> ssl.SSLContext:
    """A TLS context presenting a client certificate, trusting what Lumi trusts.

    ``ssl.create_default_context`` uses the operating system's store when
    Settings > Connections > Network > Use the system certificate store is on
    (lumi/net.py injects truststore).
    """
    context = ssl.create_default_context()
    asked = []

    def no_passphrase() -> bytes:
        # Without a callback OpenSSL would prompt on a console nobody sees.
        asked.append(True)
        return b""

    try:
        context.load_cert_chain(cert_file, key_file or None, password=no_passphrase)
    except (OSError, ssl.SSLError) as exc:
        if asked:
            raise SignInError("The client key is protected by a passphrase, which Lumi doesn't support. "
                              "Use a key file without one, readable only by you.") from exc
        raise SignInError(f"The client certificate couldn't be loaded: {exc.__class__.__name__}.") from exc
    return context
