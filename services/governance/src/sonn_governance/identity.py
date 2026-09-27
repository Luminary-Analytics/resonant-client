"""Configured resource-token verification; no discovery or tenant authority.

This is an API resource boundary, not an OIDC browser login implementation.
Only operator configuration selects an issuer, audience, and JWKS endpoint.
"""

from __future__ import annotations

from dataclasses import dataclass
import ipaddress
import json
import re
import threading
import time
from urllib.parse import urlsplit

import httpx
import jwt
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPublicKey

from .models import Principal


class AuthenticationFailed(Exception):
    """An intentionally generic failure which never contains token material."""

    def __init__(self):
        super().__init__("Resource authentication failed")


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON property")
        result[key] = value
    return result


def _test_host(host: str) -> bool:
    if host.casefold() == "localhost" or host.casefold().endswith((".localhost", ".test", ".invalid", ".example")):
        return True
    try:
        address = ipaddress.ip_address(host)
        return address.is_loopback or address.is_unspecified
    except ValueError:
        return False


@dataclass(frozen=True, slots=True)
class IdentityConfig:
    """Trusted operator settings; test loopback requires both explicit switches."""

    issuer: str
    audience: str
    jwks_url: str
    profile: str = "production"
    allow_test_loopback: bool = False
    cache_seconds: int = 300
    refresh_interval_seconds: int = 30
    request_timeout_seconds: float = 2.0
    maximum_token_lifetime_seconds: int = 3600

    def __post_init__(self):
        if self.profile not in {"production", "test"} or type(self.allow_test_loopback) is not bool:
            raise ValueError("Unknown identity configuration profile")
        if self.allow_test_loopback and self.profile != "test":
            raise ValueError("Test identity transport cannot be enabled in production")
        if type(self.audience) is not str or not self.audience or len(self.audience) > 512:
            raise ValueError("A bounded resource audience is required")
        for value in (self.issuer, self.jwks_url):
            if type(value) is not str or len(value) > 2048 or any(ord(char) < 33 for char in value):
                raise ValueError("Invalid configured identity endpoint")
            parsed = urlsplit(value)
            if not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
                raise ValueError("Identity endpoints cannot contain credentials, query, or fragment")
            local = _test_host(parsed.hostname)
            if self.profile == "production" and local:
                raise ValueError("Production identity cannot use a test issuer or endpoint")
            if local and self.profile == "test" and not self.allow_test_loopback:
                raise ValueError("Test identity endpoints require explicit opt-in")
            if parsed.scheme != "https":
                try:
                    loopback = ipaddress.ip_address(parsed.hostname).is_loopback
                except ValueError:
                    loopback = parsed.hostname.casefold() == "localhost"
                if not (parsed.scheme == "http" and loopback and self.profile == "test" and self.allow_test_loopback):
                    raise ValueError("Identity endpoints require HTTPS")
        for value, low, high in ((self.cache_seconds, 15, 900), (self.refresh_interval_seconds, 1, 300),
                                 (self.maximum_token_lifetime_seconds, 60, 3600)):
            if type(value) is not int or not low <= value <= high:
                raise ValueError("Identity cache or lifetime limit is invalid")
        if self.refresh_interval_seconds > self.cache_seconds:
            raise ValueError("JWKS refresh interval cannot exceed cache lifetime")
        if type(self.request_timeout_seconds) not in (int, float) or not .1 <= self.request_timeout_seconds <= 5:
            raise ValueError("Identity network timeout must be bounded")


class Identity:
    """Verify RS256 access tokens against a bounded, pinned JWKS cache.

    Cache removal takes effect by the configured TTL (at most 15 minutes).
    Unknown-key/signature rotation refreshes are globally rate limited. Current
    membership and resource revocation are independently checked by the store
    on every request, including an idempotent command replay.
    """

    def __init__(self, config: IdentityConfig):
        self.config = config
        self._keys: dict[str, RSAPublicKey] = {}
        self._expires_at = 0.0
        self._last_refresh = float("-inf")
        self._lock = threading.Lock()

    def _refresh(self, now: float) -> None:
        self._last_refresh = now
        deadline = time.monotonic() + 2 * self.config.request_timeout_seconds
        # Do not inherit proxy settings, redirects, credentials, or discovery.
        # At most two fixed-source requests occur within one refresh attempt.
        with httpx.Client(timeout=self.config.request_timeout_seconds, follow_redirects=False,
                          trust_env=False, limits=httpx.Limits(max_connections=1)) as client:
            for attempt in range(2):
                try:
                    data = bytearray()
                    with client.stream("GET", self.config.jwks_url, headers={"Accept": "application/json", "Accept-Encoding": "identity"}) as response:
                        if response.status_code != 200 or response.headers.get("Content-Encoding", "identity") != "identity":
                            raise AuthenticationFailed()
                        for chunk in response.iter_raw():
                            if time.monotonic() > deadline or len(data) + len(chunk) > 131072:
                                raise AuthenticationFailed()
                            data.extend(chunk)
                    document = json.loads(data, object_pairs_hook=_unique_object)
                    if (type(document) is not dict or set(document) != {"keys"} or type(document["keys"]) is not list
                            or not 1 <= len(document["keys"]) <= 32):
                        raise AuthenticationFailed()
                    keys = {}
                    for value in document["keys"]:
                        if type(value) is not dict:
                            raise AuthenticationFailed()
                        if value.get("kty") != "RSA" or value.get("alg", "RS256") != "RS256":
                            continue
                        kid = value.get("kid")
                        if (type(kid) is not str or not re.fullmatch(r"[A-Za-z0-9._-]{1,128}", kid) or kid in keys
                                or value.get("use", "sig") != "sig" or value.get("key_ops", ["verify"]) != ["verify"]
                                or set(value) & {"d", "p", "q", "dp", "dq", "qi", "oth", "jku", "x5u"}):
                            raise AuthenticationFailed()
                        key = jwt.PyJWK(value, algorithm="RS256").key
                        if not isinstance(key, RSAPublicKey) or not 2048 <= key.key_size <= 8192:
                            raise AuthenticationFailed()
                        keys[kid] = key
                    if not keys:
                        raise AuthenticationFailed()
                    self._keys, self._expires_at = keys, time.monotonic() + self.config.cache_seconds
                    return
                except (httpx.TransportError, OSError):
                    if attempt or time.monotonic() >= deadline:
                        raise AuthenticationFailed() from None
                except (ValueError, TypeError, jwt.PyJWTError):
                    raise AuthenticationFailed() from None
        raise AuthenticationFailed()

    def _key(self, kid: str, *, rotated: bool = False):
        with self._lock:
            now = time.monotonic()
            expired = now >= self._expires_at
            if expired or kid not in self._keys or rotated:
                if now - self._last_refresh >= self.config.refresh_interval_seconds:
                    self._refresh(now)
                elif expired:
                    raise AuthenticationFailed()
            if now >= self._expires_at or kid not in self._keys:
                raise AuthenticationFailed()
            return self._keys[kid]

    def authenticate(self, authorization: str | None) -> Principal:
        """Accept exactly one Bearer resource token; return no token claims as grants."""
        try:
            if type(authorization) is not str or len(authorization) > 16384:
                raise AuthenticationFailed()
            scheme, separator, token = authorization.partition(" ")
            if scheme.casefold() != "bearer" or not separator or not token or any(char.isspace() for char in token):
                raise AuthenticationFailed()
            pieces = token.split(".")
            if len(pieces) != 3:
                raise AuthenticationFailed()
            for piece in pieces[:2]:
                document = json.loads(jwt.utils.base64url_decode(piece), object_pairs_hook=_unique_object)
                if type(document) is not dict:
                    raise AuthenticationFailed()
            header = jwt.get_unverified_header(token)
            if (set(header) != {"alg", "kid", "typ"} or header["alg"] != "RS256" or header["typ"] != "at+jwt"
                    or type(header["kid"]) is not str or not re.fullmatch(r"[A-Za-z0-9._-]{1,128}", header["kid"])):
                raise AuthenticationFailed()
            for rotated in (False, True):
                try:
                    claims = jwt.decode(token, self._key(header["kid"], rotated=rotated), algorithms=["RS256"],
                        audience=self.config.audience, issuer=self.config.issuer,
                        options={"require": ["iss", "aud", "sub", "exp", "iat"], "strict_aud": True})
                    break
                except jwt.InvalidSignatureError:
                    if rotated:
                        raise AuthenticationFailed() from None
            subject = claims["sub"]
            if (type(subject) is not str or not 1 <= len(subject) <= 255 or any(ord(char) < 32 for char in subject)
                    or any(type(claims[key]) is not int for key in ("exp", "iat"))
                    or ("nbf" in claims and type(claims["nbf"]) is not int)
                    or not 0 < claims["exp"] - claims["iat"] <= self.config.maximum_token_lifetime_seconds):
                raise AuthenticationFailed()
            return Principal(issuer=self.config.issuer, subject=subject, expires_at=float(claims["exp"]), kind="human")
        except AuthenticationFailed:
            raise
        except (jwt.PyJWTError, ValueError, TypeError, KeyError, OverflowError):
            raise AuthenticationFailed() from None
