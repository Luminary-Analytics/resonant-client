"""Explicit operator client: native sign-in, fixed resource origin, no token file."""

from dataclasses import dataclass
import ipaddress
import json
from pathlib import Path
import re
import ssl
import time
from urllib.parse import urlsplit

import httpx

from .identity import _unique_object
from .oidc import LoginResult, NativeOIDCConfig

_UUID = r"[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}"
_TENANT = rf"/v1/tenants/{_UUID}"
_PROJECT = rf"{_TENANT}/projects/{_UUID}"
_SHARING_CONTENT = rf"{_TENANT}/sharing/content/(?:terms|message)/{_UUID}"
_GET = re.compile(rf"(?:/v1/identity|{_TENANT}/audit|{_TENANT}/members/[a-f0-9]{{64}}|{_TENANT}/grants/{_UUID}|"
                  rf"{_PROJECT}/(?:metadata|policy|audit|hosts|runs|sharing/policy)|{_PROJECT}/content/{_UUID}(?:/bytes)?|{_SHARING_CONTENT})")
_POST = re.compile(rf"(?:{_TENANT}/commands|{_PROJECT}/(?:commands|hosts/commands|sharing/policy)|{_PROJECT}/runs/{_UUID}/controls|"
                   rf"{_PROJECT}/content/{_UUID}(?:/retention)?|{_SHARING_CONTENT}/retention)")


class AdminClientError(Exception):
    """Bounded local classification; no server body or credential is reflected."""

    def __init__(self, *, status=None, delivery_unknown=True):
        self.status, self.delivery_unknown = status, delivery_unknown
        super().__init__("Governance request unavailable")


@dataclass(frozen=True)
class AdminConfig:
    server_url: str
    oidc: NativeOIDCConfig
    ca_file: str | None = None

    def __post_init__(self):
        if type(self.server_url) is not str or len(self.server_url) > 2048 or not isinstance(self.oidc, NativeOIDCConfig):
            raise ValueError("Explicit resource origin and identity configuration required")
        parsed = urlsplit(self.server_url)
        if (not parsed.hostname or parsed.username or parsed.password or parsed.path not in {"", "/"}
                or parsed.query or parsed.fragment or any(ord(char) < 33 for char in self.server_url)):
            raise ValueError("Invalid resource origin")
        if self.oidc.profile == "test":
            try:
                local = ipaddress.ip_address(parsed.hostname).is_loopback
            except ValueError:
                local = False
            if not local or parsed.scheme not in {"http", "https"}:
                raise ValueError("Test resources require an explicit loopback origin")
        elif parsed.scheme != "https":
            raise ValueError("Production resources require HTTPS")
        if self.ca_file is not None and (type(self.ca_file) is not str or not Path(self.ca_file).is_absolute()
                                         or Path(self.ca_file).is_symlink() or not Path(self.ca_file).is_file()):
            raise ValueError("CA file must be an existing absolute regular file")


def read_json(path, *, maximum=65536):
    """Read only an explicitly supplied bounded file; never discover credentials."""
    source = Path(path)
    if not source.is_absolute() or source.is_symlink() or not source.is_file():
        raise ValueError("An absolute regular JSON file is required")
    with source.open("rb") as stream:
        data = stream.read(maximum + 1)
    if len(data) > maximum:
        raise ValueError("JSON file exceeds limit")
    return json.loads(data, object_pairs_hook=_unique_object,
                      parse_constant=lambda _: (_ for _ in ()).throw(ValueError("Nonfinite JSON")))


def load_config(path):
    value = read_json(path)
    if type(value) is not dict or set(value) not in ({"server_url", "oidc"}, {"server_url", "oidc", "ca_file"}) or type(value["oidc"]) is not dict:
        raise ValueError("Invalid operator client configuration")
    identity = dict(value["oidc"])
    if "scopes" in identity:
        if type(identity["scopes"]) is not list:
            raise ValueError("Invalid OIDC scopes")
        identity["scopes"] = tuple(identity["scopes"])
    return AdminConfig(value["server_url"], NativeOIDCConfig(**identity), value.get("ca_file"))


class AdminClient:
    """One explicit HTTP call; mutations are never retried automatically."""

    def __init__(self, config: AdminConfig, credential: LoginResult):
        if (not isinstance(credential, LoginResult) or credential.principal.issuer != config.oidc.issuer
                or credential.principal.expires_at <= time.time()):
            raise AdminClientError(delivery_unknown=False)
        self.config, self._credential = config, credential
        self._tls = ssl.create_default_context(cafile=config.ca_file)
        self._tls.minimum_version = ssl.TLSVersion.TLSv1_2
        self._tls.keylog_filename = None

    @staticmethod
    def validate(document):
        if type(document) is not dict or set(document) - {"method", "path", "query", "body"}:
            raise AdminClientError(delivery_unknown=False)
        method, path = document.get("method"), document.get("path")
        if (type(path) is not str or type(method) is not str or method not in {"GET", "POST"}
                or not (_GET if method == "GET" else _POST).fullmatch(path)):
            raise AdminClientError(delivery_unknown=False)
        query = document.get("query", {})
        if (type(query) is not dict or set(query) - {"after", "limit", "offset"}
                or any(type(value) not in {str, int} or len(str(value)) > 100 for value in query.values())
                or method == "GET" and "body" in document
                or method == "POST" and (query or type(document.get("body")) is not dict)):
            raise AdminClientError(delivery_unknown=False)
        return method, path, query

    def request(self, document):
        method, path, query = self.validate(document)
        if self._credential.principal.expires_at <= time.time():
            raise AdminClientError(delivery_unknown=False)
        body = None
        try:
            if method == "POST":
                body = json.dumps(document["body"], allow_nan=False, separators=(",", ":")).encode()
                if len(body) > 1402200:
                    raise AdminClientError(delivery_unknown=False)
            data = bytearray()
            deadline = time.monotonic() + 10
            with httpx.Client(verify=self._tls, trust_env=False, follow_redirects=False, timeout=5) as client:
                with client.stream(method, self.config.server_url.rstrip("/") + path, params=query, content=body,
                                   headers={"Authorization": "Bearer " + self._credential.access_token,
                                            "Content-Type": "application/json", "Accept": "application/json", "Accept-Encoding": "identity"}) as response:
                    if response.status_code != 200:
                        raise AdminClientError(status=response.status_code, delivery_unknown=response.status_code not in {400, 401, 403, 409})
                    if (response.headers.get("Content-Encoding", "identity") != "identity"
                            or response.headers.get("Content-Type", "").split(";", 1)[0] != "application/json"):
                        raise AdminClientError()
                    for chunk in response.iter_raw():
                        if time.monotonic() >= deadline or len(data) + len(chunk) > 1048576:
                            raise AdminClientError()
                        data.extend(chunk)
            if time.monotonic() >= deadline:
                raise AdminClientError()
            result = json.loads(data, object_pairs_hook=_unique_object,
                                parse_constant=lambda _: (_ for _ in ()).throw(ValueError("Nonfinite JSON")))
            if type(result) is not dict:
                raise AdminClientError()
            return result
        except AdminClientError:
            raise
        except Exception:
            raise AdminClientError() from None
