"""Lumi Cloud from the desktop app: signing in, enrolling this computer, check-ins.

**Signing in** follows OAuth 2.0 for native apps (RFC 8252). Lumi opens the
browser at the organization's Lumi Cloud with a PKCE challenge and a one-time
listener on ``127.0.0.1`` for the answer; the person signs in there and
approves. The refresh token goes in the credential store
(``api_keys.lumi_cloud_refresh``); access tokens stay in memory. The account
(name, email, organizations) is shown in Settings > Lumi account and is
separate from the local display name and from any SONN or ChatGPT identity.

**The account's tokens belong to the Lumi Cloud that issued them.** When a
sign-in completes, its address is recorded (``cloud.account_url``, read with
``SettingsManager.stored``, so no policy lock stands in for it). Refreshing,
account calls and revoking go only there. When the address this computer
uses changes (a machine policy's ``cloud.url``, an enrollment, another
address typed in Settings), the person counts as signed out for the new one:
nothing of the account is sent to it, the tokens stay for the old one, and
Settings says so (``status()["signed_in_elsewhere"]``).

**Enrolling** registers an Ed25519 key this computer generates (the private
half goes in the credential store as ``api_keys.lumi_cloud_device_key``) with
one organization: either the person picks one they have a seat in, or a
machine policy with a ``cloud`` section enrolls it with the administrator's
enrollment token. The device then signs short assertions to get device
tokens, so it keeps working after the person signs out.

**Check-ins** (hourly, from a background thread) report the app version, the
policy version in force and usage totals per model since the last check-in:
request and token counts and cost, never prompts, code or file names. When
the organization publishes a new policy, or the cached one is within a week
of expiring, Lumi downloads it, checks its signature against the trusted
keys (lumi/policy.py) and applies it. A revoked device forgets its
enrollment and the downloaded policy.

**Organization oversight** (lumi/oversight.py) is separate: only when the
organization's policy asks, turn records and security flags go to
``/api/v1/oversight/events`` through ``device_call``. A person's confirmation
of the notice is a record signed with this computer's device key
(``sign_as_device``) and sent to ``/api/v1/oversight/acknowledgments``.
Leaving the organization deletes records still queued, and leaving or
signing out forgets the confirmed notice.
"""

from __future__ import annotations

import base64
import hashlib
import http.server
import logging
import platform
import secrets
import socket
import threading
import time
import uuid
import webbrowser
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit

logger = logging.getLogger(__name__)

CLIENT_ID = "lumi-desktop"
REFRESH_SECRET = "lumi_cloud_refresh"
DEVICE_SECRET = "lumi_cloud_device_key"
SIGN_IN_SECONDS = 300
DEFAULT_CHECKIN_SECONDS = 3600
RETRY_SECONDS = 600
REFRESH_POLICY_BEFORE = timedelta(days=7)
LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")


class CloudError(Exception):
    """Something Lumi Cloud refused or couldn't do; the message is for people.

    ``code`` is Lumi Cloud's error code (or the HTTP status when it gave
    none); ``status`` the HTTP status, 0 when Lumi Cloud wasn't reached.
    """

    def __init__(self, message: str, *, code: str = "", status: int = 0) -> None:
        super().__init__(message)
        self.code = code
        self.status = status


def normalize_url(url: str) -> str:
    """A Lumi Cloud address: https, or http only on this computer (development).

    Never with a user name or password (``https://user:pass@host``): they
    would be sent with every request and shown wherever the address is.
    """
    text = str(url or "").strip().rstrip("/")
    parts = urlsplit(text)
    if "@" in parts.netloc:
        raise CloudError("Enter the Lumi Cloud address without a user name or password.")
    if not parts.hostname:
        raise CloudError("Enter the Lumi Cloud address, such as https://cloud.example.com.")
    if parts.scheme != "https" and not (parts.scheme == "http" and parts.hostname in LOOPBACK_HOSTS):
        raise CloudError("Lumi Cloud must use https (http only works for a server on this computer).")
    if parts.query or parts.fragment:
        raise CloudError("Enter just the Lumi Cloud address, without ? or #.")
    try:
        parts.port  # noqa: B018 - a port that isn't a number raises ValueError here, not mid-request
    except ValueError as exc:
        raise CloudError("The Lumi Cloud address has a port that isn't a number.") from exc
    return f"{parts.scheme}://{parts.netloc}{parts.path}"


def same_address(one: str, other: str) -> bool:
    """Whether two Lumi Cloud addresses are the same one (False when either isn't one Lumi uses)."""
    try:
        return bool(one) and bool(other) and normalize_url(one) == normalize_url(other)
    except CloudError:
        return False


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(moment: datetime) -> str:
    # Milliseconds, like the usage ledger's timestamps, so string comparison
    # of check-in windows and records agrees (lumi/usage.py).
    return moment.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _challenge(verifier: str) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode("ascii")


# ── The loopback listener ───────────────────────────────────────────────────


_DONE_PAGE = """<!doctype html><meta charset="utf-8"><title>Lumi</title>
<body style="font-family:system-ui,sans-serif;background:#10122b;color:#f6f4ee;display:grid;place-items:center;
min-height:90vh"><div style="max-width:420px;text-align:center"><h1 style="font-weight:600">{title}</h1>
<p>{body}</p></div></body>"""


class _Loopback:
    """A one-shot listener on 127.0.0.1 for the browser's redirect back to Lumi."""

    def __init__(self, state: str) -> None:
        self.state = state
        self.result: dict[str, str] = {}
        self.done = threading.Event()
        owner = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802 (http.server's naming)
                parts = urlsplit(self.path)
                if parts.path != "/callback":
                    self.send_error(404)
                    return
                query = {k: v[0] for k, v in parse_qs(parts.query).items()}
                if not secrets.compare_digest(query.get("state", ""), owner.state):
                    self._page(400, "That sign-in isn't this one", "Start again from Lumi's settings.")
                    return
                owner.result = query
                if query.get("error"):
                    self._page(200, "Sign-in cancelled", "You can close this tab and return to Lumi.")
                else:
                    self._page(200, "You're signed in", "You can close this tab and return to Lumi.")
                owner.done.set()

            def _page(self, status: int, title: str, body: str) -> None:
                data = _DONE_PAGE.format(title=title, body=body).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *_args: Any) -> None:  # the address holds the code; never log it
                pass

        self.server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True, name="lumi-cloud-signin")
        self.thread.start()

    @property
    def redirect_uri(self) -> str:
        return f"http://127.0.0.1:{self.port}/callback"

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


@dataclass
class _PendingSignIn:
    url: str
    verifier: str
    loopback: _Loopback
    started: float = field(default_factory=time.monotonic)
    cancelled: threading.Event = field(default_factory=threading.Event)


# ── The client ──────────────────────────────────────────────────────────────


class CloudClient:
    """Lumi Cloud for one computer, backed by Settings (lumi/gui/settings.py)."""

    def __init__(self, settings: Any, *, transport: Any = None, open_browser: Callable[[str], Any] | None = None,
                 on_change: Callable[[dict], None] | None = None) -> None:
        self.settings = settings
        self._transport = transport
        self._open_browser = open_browser or webbrowser.open
        self.on_change = on_change
        self._lock = threading.RLock()
        self._pending: _PendingSignIn | None = None
        self._access: tuple[str, float] | None = None
        self._device_access: tuple[str, float] | None = None
        self.last_error = ""
        self.next_checkin = DEFAULT_CHECKIN_SECONDS
        # Tasks from Slack and Teams (lumi/remote_tasks.py), when the app runs them.
        self.remote_tasks: Any = None
        # The organization's shared credit, as the last check-in reported it.
        from . import budgets

        saved = self._section().get("shared_credit")
        budgets.set_shared_credit(saved.get("budget") if isinstance(saved, dict) else None,
                                  since=str(saved.get("since") or "") if isinstance(saved, dict) else "")

    # ── Settings ───────────────────────────────────────────────────────────
    def _section(self) -> dict:
        section = self.settings.get("cloud")
        return dict(section) if isinstance(section, dict) else {}

    def _save(self, **updates: Any) -> None:
        self.settings.update_section("cloud", updates)

    def managed(self) -> dict:
        """The machine policy's ``cloud`` section: an administrator's Lumi Cloud and token."""
        from . import policy

        return policy.machine_cloud_settings()

    @property
    def url(self) -> str:
        """The Lumi Cloud this computer uses: the machine policy's, else the one saved in Settings.

        An address with a user name or password is never used or shown: "".
        """
        managed = self.managed()
        value = str(managed.get("url") or self._section().get("url") or "")
        return "" if "@" in urlsplit(value).netloc else value

    @property
    def account_url(self) -> str:
        """The Lumi Cloud that issued the sign-in's tokens, recorded when the sign-in completed; "" for none.

        Read as saved, whatever a policy locks; only ``_finish_sign_in`` writes it.
        """
        stored = getattr(self.settings, "stored", None)
        value = stored("cloud", "account_url", "") if callable(stored) else self._section().get("account_url")
        return str(value or "")

    def _account_here(self) -> bool:
        """Whether the person's sign-in was issued by the Lumi Cloud this computer uses now."""
        return same_address(self.account_url, self.url)

    def _signed_out_here(self) -> CloudError:
        issued = self.account_url
        if issued and self.settings.get("api_keys", REFRESH_SECRET):
            return CloudError(f"Your Lumi Cloud sign-in is for {urlsplit(issued).netloc}, and this computer now "
                              f"uses {urlsplit(self.url).netloc or 'no Lumi Cloud'}. Sign in again here.",
                              code="signed_out")
        return CloudError("Sign in to Lumi Cloud first.", code="signed_out")

    def device(self) -> dict:
        device = self._section().get("device")
        return dict(device) if isinstance(device, dict) and device.get("id") else {}

    def status(self) -> dict:
        section = self._section()
        managed = self.managed()
        device = self.device()
        from . import policy

        state = policy.load()
        has_tokens = bool(self.settings.get("api_keys", REFRESH_SECRET))
        here = has_tokens and self._account_here()
        return {
            "url": self.url,
            "url_locked": bool(managed.get("url")),
            "managed_organization": str(managed.get("organization_id") or ""),
            # Signed in to the Lumi Cloud this computer uses now; a sign-in another one issued doesn't count.
            "signed_in": here,
            "signed_in_elsewhere": self.account_url if has_tokens and not here else "",
            "account": (section.get("account") or {}) if has_tokens else {},
            "signing_in": self._pending is not None,
            "device": {k: v for k, v in device.items() if k != "trusted_keys"},
            "last_checkin": section.get("last_checkin") or "",
            "policy_version": section.get("policy_version"),
            "policy_source": state.source if state.cloud else "",
            "cloud_error": state.cloud_error,
            "error": self.last_error,
            "remote_tasks": self.remote_tasks.status() if self.remote_tasks is not None else None,
        }

    def _changed(self) -> None:
        if self.on_change:
            try:
                self.on_change(self.status())
            except Exception:
                logger.debug("cloud status listener failed", exc_info=True)

    # ── HTTP ───────────────────────────────────────────────────────────────
    def _http(self):
        import httpx

        from .net import client_options

        # Check-ins, sign-in, sharing, the team library, hand-offs, reviews and
        # tasks from chat all come through here, so offline mode refuses them
        # all with one message (lumi/offline.py).
        return httpx.Client(**client_options(timeout=20.0, transport=self._transport, feature="Lumi Cloud"),
                            headers={"User-Agent": f"Lumi/{_app_version()} ({platform.system()})"})

    def _call(self, method: str, url: str, **kwargs: Any) -> dict:
        import httpx

        from . import offline

        try:
            with self._http() as http:
                response = http.request(method, url, **kwargs)
        except httpx.HTTPError as exc:
            reason = offline.message_for(exc)
            if reason:
                raise CloudError(reason, code="offline") from exc
            raise CloudError(f"Lumi Cloud couldn't be reached ({type(exc).__name__}).", code="unreachable") from exc
        try:
            data = response.json() if response.content else {}
        except ValueError:
            data = {}
        if response.status_code >= 400:
            code = str(data.get("error") or response.status_code) if isinstance(data, dict) else str(
                response.status_code)
            message = data.get("error_description") if isinstance(data, dict) else ""
            raise CloudError(str(message or f"Lumi Cloud answered {response.status_code}."), code=code,
                             status=response.status_code)
        return data if isinstance(data, dict) else {}

    # ── Signing in ─────────────────────────────────────────────────────────
    def begin_sign_in(self, url: str = "") -> str:
        """Open the browser to sign in; finishes in the background. Returns the address opened."""
        from . import offline

        with self._lock:
            target = normalize_url(url or self.url)
            if self.managed().get("url") and target != normalize_url(self.managed()["url"]):
                raise CloudError("Your organization's policy sets which Lumi Cloud this computer uses.")
            # Signing in happens in the browser, outside Lumi's own clients:
            # say so now rather than open a page that can't load.
            reason = offline.refusal(target, "Lumi Cloud")
            if reason:
                raise CloudError(reason, code="offline")
            self.cancel_sign_in()
            verifier, state = secrets.token_urlsafe(48), secrets.token_urlsafe(24)
            loopback = _Loopback(state)
            pending = _PendingSignIn(url=target, verifier=verifier, loopback=loopback)
            self._pending = pending
            self.last_error = ""
            # The address is saved when the sign-in completes (_finish_sign_in): a sign-in
            # that's cancelled or fails leaves the Lumi Cloud this computer uses as it was.
        authorize = f"{target}/oauth/authorize?" + urlencode({
            "response_type": "code", "client_id": CLIENT_ID, "redirect_uri": loopback.redirect_uri,
            "state": state, "code_challenge": _challenge(verifier), "code_challenge_method": "S256",
            "scope": "profile email offline_access devices",
        })
        threading.Thread(target=self._wait_for_sign_in, args=(pending,), daemon=True,
                         name="lumi-cloud-signin-wait").start()
        self._open_browser(authorize)
        self._changed()
        return authorize

    def cancel_sign_in(self) -> None:
        with self._lock:
            pending, self._pending = self._pending, None
        if pending is not None:
            pending.cancelled.set()
            pending.loopback.done.set()

    def _wait_for_sign_in(self, pending: _PendingSignIn) -> None:
        try:
            finished = pending.loopback.done.wait(SIGN_IN_SECONDS)
            if pending.cancelled.is_set():
                return
            if not finished:
                raise CloudError("Signing in took more than five minutes. Try again.")
            result = pending.loopback.result
            if result.get("error"):
                raise CloudError("Sign-in was cancelled in the browser.")
            self._finish_sign_in(pending, result.get("code", ""))
        except CloudError as exc:
            self.last_error = str(exc)
        except Exception as exc:  # never leave the page waiting forever
            logger.exception("Lumi Cloud sign-in failed")
            self.last_error = f"Sign-in failed: {exc}"
        finally:
            pending.loopback.close()
            with self._lock:
                if self._pending is pending:
                    self._pending = None
            self._changed()

    def _finish_sign_in(self, pending: _PendingSignIn, code: str) -> None:
        tokens = self._call("POST", f"{pending.url}/oauth/token", data={
            "grant_type": "authorization_code", "code": code, "redirect_uri": pending.loopback.redirect_uri,
            "client_id": CLIENT_ID, "code_verifier": pending.verifier,
        })
        previous, issued = self.settings.get("api_keys", REFRESH_SECRET) or "", self.account_url
        if previous and issued and not same_address(issued, pending.url):
            # An earlier sign-in, at another Lumi Cloud: revoked there, where it was issued, never here.
            self._revoke(issued, previous)
        self._store_tokens(tokens)
        # These tokens are this Lumi Cloud's: recorded now, where the sign-in completed, and used nowhere else.
        self._save(url=pending.url, account_url=pending.url)
        me = self._call("GET", f"{pending.url}/api/v1/me",
                        headers={"Authorization": f"Bearer {self._access[0]}"})
        account = self._keep_account(me)
        from . import audit

        audit.record("cloud.signed_in", url=pending.url, organizations=len(account.get("organizations") or []))

    def _store_tokens(self, tokens: dict) -> None:
        access, refresh = str(tokens.get("access_token") or ""), str(tokens.get("refresh_token") or "")
        if not access or not refresh:
            raise CloudError("Lumi Cloud didn't return a sign-in.")
        self.settings.set("api_keys", REFRESH_SECRET, refresh)
        self._access = (access, time.monotonic() + max(60, int(tokens.get("expires_in") or 3600)) - 60)

    def _access_token(self, *, refresh: bool = False) -> str:
        """The account's access token, refreshed at the Lumi Cloud that issued it and never another.

        CloudError ``signed_out`` when nobody is signed in, or when the sign-in
        is another Lumi Cloud's than the one this computer uses now. ``refresh``
        gets a new one even when the one held hasn't expired (a 401 for it).
        """
        if not self._account_here():
            raise self._signed_out_here()
        if not refresh and self._access and self._access[1] > time.monotonic():
            return self._access[0]
        refresh_token = self.settings.get("api_keys", REFRESH_SECRET) or ""
        if not refresh_token:
            raise CloudError("Sign in to Lumi Cloud first.", code="signed_out")
        try:
            tokens = self._call("POST", f"{self.account_url}/oauth/token", data={
                "grant_type": "refresh_token", "refresh_token": refresh_token, "client_id": CLIENT_ID})
        except CloudError as exc:
            if exc.code == "invalid_grant":
                self._forget_account()
                raise CloudError("Your Lumi Cloud sign-in ended. Sign in again.", code="signed_out") from exc
            raise
        self._store_tokens(tokens)
        return self._access[0]

    def refresh_account(self) -> dict:
        """Fetch the person and their organizations (roles and seats) again, from the Lumi Cloud that signed them in."""
        return self._keep_account(self.account_call("GET", "/api/v1/me"))

    def _keep_account(self, me: dict) -> dict:
        user = me.get("user") if isinstance(me.get("user"), dict) else {}
        account = {
            "user_id": str(user.get("id") or ""),
            "email": str(user.get("email") or ""),
            "name": str(user.get("name") or ""),
            "organizations": [
                {"id": str(o.get("id") or ""), "name": str(o.get("name") or ""), "role": str(o.get("role") or ""),
                 "has_seat": bool(o.get("has_seat"))}
                for o in (me.get("organizations") or []) if isinstance(o, dict)
            ],
            "refreshed_at": _iso(_now()),
        }
        self._save(account=account)
        self._changed()
        return account

    def _forget_account(self) -> None:
        self.settings.set("api_keys", REFRESH_SECRET, "")
        self._access = None
        self._save(account={}, account_url="")

    def _revoke(self, issued: str, refresh: str) -> None:
        """Revoke a refresh token at the Lumi Cloud that issued it; best effort."""
        try:
            self._call("POST", f"{issued}/oauth/revoke", data={"token": refresh})
        except CloudError:
            logger.info("Couldn't reach Lumi Cloud to revoke the sign-in; forgetting it here")

    def sign_out(self) -> None:
        """Sign this app out of Lumi Cloud. An enrolled computer stays enrolled.

        The sign-in is revoked where it was issued (``account_url``), even when
        this computer now uses another Lumi Cloud, and never anywhere else.
        """
        refresh = self.settings.get("api_keys", REFRESH_SECRET) or ""
        issued = self.account_url
        if refresh and issued:
            self._revoke(issued, refresh)
        self._forget_account()
        from . import audit, oversight

        audit.record("cloud.signed_out", url=issued or self.url)
        # The oversight notice is confirmed again after signing back in (lumi/oversight.py).
        oversight.forget_notice("Signed out of Lumi Cloud")
        self._changed()

    # ── Enrolling ──────────────────────────────────────────────────────────
    def enroll(self, organization_id: str) -> dict:
        """Enroll this computer in one of the signed-in person's organizations."""
        if self.managed():
            raise CloudError("Your organization's policy manages this computer's enrollment.")
        payload = {"organization_id": str(organization_id or "")}
        return self._enroll(payload, headers={"Authorization": f"Bearer {self._access_token()}"}, how="joined")

    def enroll_managed(self) -> dict:
        """Enroll with the machine policy's enrollment token (device management).

        The address the person signed in at (``account_url``) stays as it was:
        when the machine policy's Lumi Cloud is another one, the person counts
        as signed out for it.
        """
        managed = self.managed()
        token = str(managed.get("enrollment_token") or "")
        if not (managed.get("url") and token):
            raise CloudError("The machine policy doesn't name a Lumi Cloud and an enrollment token.")
        self._save(url=normalize_url(managed["url"]))
        return self._enroll({"enrollment_token": token}, headers={}, how="managed")

    def _enroll(self, payload: dict, *, headers: dict, how: str) -> dict:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

        if self.device():
            raise CloudError(f"This computer is already enrolled in {self.device().get('organization_name')}.")
        key = Ed25519PrivateKey.generate()
        public = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        private = key.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw,
                                    serialization.NoEncryption())
        body = {**payload, "public_key": base64.b64encode(public).decode("ascii"),
                "name": socket.gethostname()[:120] or "Computer", "platform": _platform_name(),
                "app_version": _app_version()}
        answer = self._call("POST", f"{self.url}/api/v1/devices", json=body, headers=headers)
        organization = answer.get("organization") if isinstance(answer.get("organization"), dict) else {}
        keys = answer.get("trusted_keys") if isinstance(answer.get("trusted_keys"), dict) else {}
        self.settings.set("api_keys", DEVICE_SECRET, base64.b64encode(private).decode("ascii"))
        device = {
            "id": str(answer.get("device_id") or ""),
            "organization_id": str(organization.get("id") or ""),
            "organization_name": str(organization.get("name") or ""),
            "url": self.url,
            "how": how,
            # Whose computer it is in Lumi Cloud: the person who joined here (none when managed).
            "user_id": str((self._section().get("account") or {}).get("user_id") or "") if how == "joined" else "",
            "enrolled_at": _iso(_now()),
            # Pinned now; they verify the organization's policy (lumi/policy.py).
            "trusted_keys": {str(k): str(v) for k, v in keys.items()},
        }
        self._save(device=device, policy_version=None, last_checkin="", usage_since=_iso(_now()))
        self._device_access = None
        from . import audit

        audit.record("cloud.enrolled", url=self.url, organization=device["organization_name"], how=how)
        self._changed()
        self.check_in()
        return self.device()

    def unenroll(self) -> None:
        """Leave the organization on this computer (a computer someone joined, not a managed one)."""
        device = self.device()
        if not device:
            return
        if device.get("how") == "managed":
            raise CloudError("Your organization manages this computer; ask an administrator to remove it.")
        try:
            self._call("POST", f"{self.url}/api/v1/devices/unenroll",
                       headers={"Authorization": f"Bearer {self._device_token()}"})
        except CloudError:
            logger.info("Couldn't tell Lumi Cloud this computer left; forgetting the enrollment here")
        self._drop_device("Left the organization")

    def _drop_device(self, reason: str) -> None:
        from . import audit, policy

        organization = self.device().get("organization_name", "")
        self.settings.set("api_keys", DEVICE_SECRET, "")
        self._save(device={}, policy_version=None)
        self._device_access = None
        try:
            policy.cloud_policy_path().unlink(missing_ok=True)
        except OSError:
            logger.warning("Couldn't delete the downloaded organization policy", exc_info=True)
        policy.load(force=True)
        audit.record("cloud.unenrolled", organization=organization, reason=reason)
        # Oversight records meant for the organization aren't sent anywhere
        # else, and a later enrollment shows its own notice (lumi/oversight.py).
        from . import oversight

        oversight.discard(f"This computer left {organization or 'the organization'} ({reason}).")
        oversight.forget_notice(f"This computer left {organization or 'the organization'} ({reason}).")
        self._changed()

    # ── Device sign-in and check-ins ───────────────────────────────────────
    def _device_key(self):
        """This computer's enrolled Ed25519 key (CloudError when it isn't enrolled)."""
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

        device = self.device()
        secret = self.settings.get("api_keys", DEVICE_SECRET) or ""
        if not device or not secret:
            raise CloudError("This computer isn't enrolled.", code="not_enrolled")
        return Ed25519PrivateKey.from_private_bytes(base64.b64decode(secret))

    def sign_as_device(self, data: bytes) -> str:
        """An Ed25519 signature by this computer's device key over ``data``, base64url with padding.

        Organization oversight signs a person's confirmation of its notice
        with it (lumi/oversight.py); Lumi Cloud checks it against the public
        key the device enrolled with. The key never leaves the credential store.
        """
        return base64.urlsafe_b64encode(self._device_key().sign(bytes(data))).decode("ascii")

    def _device_token(self) -> str:
        if self._device_access and self._device_access[1] > time.monotonic():
            return self._device_access[0]
        device = self.device()
        key = self._device_key()
        now = int(time.time())
        audience = f"{self.url}/api/v1/devices/token"
        assertion = eddsa_jwt({"iss": device["id"], "sub": device["id"], "aud": audience, "iat": now,
                               "exp": now + 120, "jti": uuid.uuid4().hex}, key)
        answer = self._call("POST", audience, json={"assertion": assertion})
        token = str(answer.get("access_token") or "")
        self._device_access = (token, time.monotonic() + max(60, int(answer.get("expires_in") or 3600)) - 60)
        return token

    def account_call(self, method: str, path: str, **kwargs: Any) -> dict:
        """Lumi Cloud's API as the signed-in person (shared sessions, lumi/share.py); {} for no content.

        Only at the Lumi Cloud that issued the sign-in, while this computer uses
        it (CloudError ``signed_out`` otherwise).
        """
        token = self._access_token()
        return self._call(method, f"{self.account_url}{path}", headers={"Authorization": f"Bearer {token}"},
                          **kwargs)

    def account_token(self, *, refresh: bool = False) -> str:
        """The signed-in person's desktop access token, refreshed when it has expired (or ``refresh``).

        A managed computer sends it with the person's confirmation of the
        oversight notice (``Lumi-Account-Token``, lumi/oversight.py), so Lumi
        Cloud can check the confirmation is theirs, and feedback sent while
        signed in carries it (lumi/feedback.py). Only for a request to
        ``account_url``, the Lumi Cloud that issued it. CloudError
        (``signed_out``) when nobody is signed in, or the sign-in is another
        Lumi Cloud's than the one this computer uses.
        """
        return self._access_token(refresh=refresh)

    def device_call(self, method: str, path: str, *, headers: dict | None = None, **kwargs: Any) -> dict:
        """Lumi Cloud's device API as this computer (tasks from chat, lumi/remote_tasks.py); {} for no content."""
        token = self._device_token()
        return self._call(method, f"{self.url}{path}",
                          headers={**(headers or {}), "Authorization": f"Bearer {token}"}, **kwargs)

    def check_in(self) -> dict:
        """Report to Lumi Cloud and apply a new policy if there is one."""
        device = self.device()
        if not device:
            return {}
        section = self._section()
        since = str(section.get("usage_since") or "")
        until = _iso(_now())
        payload = {"app_version": _app_version(), "platform": _platform_name(),
                   "policy_version": section.get("policy_version"), "usage": usage_summary(since, until),
                   "activity": activity_summary(since, until)}
        try:
            answer = self._call("POST", f"{self.url}/api/v1/devices/checkin", json=payload,
                                headers={"Authorization": f"Bearer {self._device_token()}"})
        except CloudError as exc:
            if exc.code == "invalid_token":  # an expired device token: sign the device in again once
                self._device_access = None
                answer = self._call("POST", f"{self.url}/api/v1/devices/checkin", json=payload,
                                    headers={"Authorization": f"Bearer {self._device_token()}"})
            elif exc.code == "device_revoked":
                self._drop_device("Revoked in Lumi Cloud")
                self.last_error = f"{device.get('organization_name') or 'Your organization'} removed this computer."
                self._changed()
                return {"revoked": True}
            else:
                self.last_error = str(exc)
                self._changed()
                raise
        self.last_error = ""
        updates: dict[str, Any] = {"last_checkin": until, "usage_since": until}
        # The organization's shared credit and the month's spend so far (lumi/budgets.py).
        from . import budgets

        shared = answer.get("budget") if isinstance(answer.get("budget"), dict) else None
        budgets.set_shared_credit(shared, since=until)
        updates["shared_credit"] = {"budget": shared, "since": until} if shared else None
        keys = answer.get("trusted_keys")
        if device.get("how") == "joined" and isinstance(keys, dict) and keys:
            # A joined computer trusts the keys its organization lists (key rotation).
            updates["device"] = {**device, "trusted_keys": {str(k): str(v) for k, v in keys.items()}}
        self._save(**updates)
        self.next_checkin = max(300, int(answer.get("next_checkin_seconds") or DEFAULT_CHECKIN_SECONDS))
        latest = answer.get("policy_version")
        if isinstance(latest, int) and self._policy_needs_download(latest):
            self._download_policy()
            # Say at once which policy is in force, so the fleet page is current
            # now rather than at the next hourly check-in.
            try:
                self._call("POST", f"{self.url}/api/v1/devices/checkin", headers={
                    "Authorization": f"Bearer {self._device_token()}"}, json={
                    "app_version": _app_version(), "platform": _platform_name(),
                    "policy_version": self._section().get("policy_version")})
            except CloudError:
                logger.info("Couldn't confirm the new policy version; the next check-in will")
        self._changed()
        return answer

    def _policy_needs_download(self, latest: int) -> bool:
        from . import policy

        if self._section().get("policy_version") != latest:
            return True
        state = policy.load()
        if not state.cloud or not state.policy or not state.policy.expires_at:
            return True
        expires = datetime.fromisoformat(state.policy.expires_at.replace("Z", "+00:00"))
        return expires - _now() < REFRESH_POLICY_BEFORE

    def _download_policy(self) -> None:
        import json

        from . import audit, policy

        envelope = self._call("GET", f"{self.url}/api/v1/devices/policy",
                              headers={"Authorization": f"Bearer {self._device_token()}"})
        try:
            document = policy.parse(envelope, source="Lumi Cloud download", trusted_keys=policy.trusted_cloud_keys(),
                                    require_signature=True)
        except policy.PolicyError as exc:
            raise CloudError(f"The organization's policy wasn't applied: {exc}", code="bad_policy") from exc
        path = policy.cloud_policy_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(envelope, indent=2, sort_keys=True), encoding="utf-8")
        temporary.replace(path)
        version = document.raw.get("policy_version")
        self._save(policy_version=version if isinstance(version, int) else None)
        state = policy.load(force=True)
        audit.record("cloud.policy_applied", organization=document.organization, version=version,
                     in_force=state.cloud)

    # ── Background ─────────────────────────────────────────────────────────
    def background_step(self) -> float:
        """One round of the background loop; returns seconds until the next."""
        try:
            if not self.device() and self.managed().get("enrollment_token"):
                self.enroll_managed()
            elif self.device():
                self.check_in()
            return float(self.next_checkin)
        except CloudError as exc:
            self.last_error = str(exc)
            self._changed()
            return float(RETRY_SECONDS)
        except Exception:
            logger.exception("Lumi Cloud check-in failed")
            return float(RETRY_SECONDS)


def start_background(client: CloudClient, *, first_delay: float = 20.0) -> threading.Thread:
    """Check in now and then on Lumi Cloud's schedule, for the app's lifetime."""

    def loop() -> None:
        time.sleep(first_delay)
        while True:
            time.sleep(client.background_step())

    thread = threading.Thread(target=loop, daemon=True, name="lumi-cloud-checkin")
    thread.start()
    return thread


def usage_summary(since: str, until: str) -> dict | None:
    """Totals per model since the last check-in (counts and cost only)."""
    from . import usage

    rows = usage.ledger().records(since=since, until=until) if since else []
    by_model: dict[str, dict] = {}
    for row in rows:
        key = f"{row.get('provider') or ''}:{row.get('model') or ''}"
        entry = by_model.setdefault(key, {"model": key, "requests": 0, "input_tokens": 0, "output_tokens": 0,
                                          "cost_usd": None})
        entry["requests"] += 1
        entry["input_tokens"] += int(row.get("input_tokens") or 0)
        entry["output_tokens"] += int(row.get("output_tokens") or 0)
        cost = row.get("cost_usd")
        if isinstance(cost, int | float):
            entry["cost_usd"] = round((entry["cost_usd"] or 0.0) + float(cost), 6)
    if not since:
        return None
    return {"period_start": since, "period_end": until, "models": list(by_model.values())}


def activity_summary(since: str, until: str) -> dict | None:
    """Turn outcomes and crashes since the last check-in: counts only (lumi/activity.py)."""
    from . import activity

    if not since:
        return None
    return {"period_start": since, "period_end": until, **activity.summary(since, until)}


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def eddsa_jwt(claims: dict, key: Any) -> str:
    """A compact JWS (RFC 7515) signed with Ed25519, ``alg: EdDSA`` (RFC 8037).

    Written here rather than with a JWT library: it is the one token this
    app signs, and cryptography is already a dependency.
    """
    import json

    header = _b64url(json.dumps({"alg": "EdDSA", "typ": "JWT"}, separators=(",", ":")).encode("utf-8"))
    payload = _b64url(json.dumps(claims, separators=(",", ":")).encode("utf-8"))
    signature = key.sign(f"{header}.{payload}".encode("ascii"))
    return f"{header}.{payload}.{_b64url(signature)}"


def _app_version() -> str:
    from . import __version__

    return __version__


def _platform_name() -> str:
    return f"{platform.system()} {platform.release()}".strip()[:60]
