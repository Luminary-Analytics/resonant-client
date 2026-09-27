"""Panels that capability packs add to the app: listing, opening and serving their files.

An approved, enabled pack can declare ``ui_panels`` (engine/capability_packs.py,
docs/extensions.md#panels): an id, a title and an HTML entry file inside the
pack. The person opens one from View or the command palette, and the page
shows it in ``<iframe sandbox="allow-scripts">`` (static/panels_view.js).

Isolation
---------
Panel content never runs in the app's origin. The page there holds the
launch's access token (gui/local_access.py), and the token runs commands and
changes settings. So:

* The page asks for a panel over its socket, which carries the launch token.
  The server checks the pack again and issues a *panel token*: a random value
  that reads only that panel's files. It lives in memory until the panel
  closes, the page's socket goes away or ``PANEL_TOKEN_TTL`` passes. It is
  not the launch token and grants nothing else; an iframe couldn't send the
  launch token anyway, so this route never sees or accepts it.
* Files are served at ``/panels/<panel token>/<path>``. Every request checks
  again that panels are allowed (Settings, policy), that the pack is still
  installed, approved, enabled and allowed by the organization, and that it is
  unchanged. The path must name a file the approval covered, inside the entry
  file's folder, and the bytes read must hash to the approved ones.
* Every response carries its own Content-Security-Policy: scripts, styles and
  images only from the panel's own path, no network (``connect-src 'none'``),
  no frames, forms, workers or base URL, and ``sandbox allow-scripts``. So a
  panel URL loaded without the frame's sandbox still gets an opaque origin
  without the app's storage. Browsers that send Fetch Metadata are also
  refused a panel URL as a top-level page, where it could navigate its tab.
* The frame's own sandbox leaves out allow-same-origin, allow-top-navigation,
  allow-popups, allow-forms and allow-modals, and the app page's
  ``frame-src 'self'`` keeps the frame from navigating to another site.
* HTML files get one script added before anything else, Lumi's bridge
  (``static/panel_frame.js``), served under the same path. It wraps the
  postMessage protocol that panels_view.js checks: read the project name and
  theme, add text to the message box without sending it, show a notice. It
  also holds a private channel to the page, which it uses only to report a
  real Escape key press, so a panel's own script can't close the panel.
* Adding text to the message box asks this module first (:func:`check`), so
  a panel whose pack was revoked or changed while it was open adds nothing.

Checking a pack hashes its files, so a verified pack is kept for a few seconds
(``VERIFIED_TTL``), keyed by the approval, the publishers and the policy in
force: a panel loading many files hashes its pack once. Every served file's
bytes are still hashed against the approval. Panel files are read on their
own small thread pool, so they never hold up the app socket's commands.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import secrets
import threading
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import quote

logger = logging.getLogger(__name__)

PANEL_TOKEN_TTL = 12 * 3600
MAX_PANEL_TOKENS = 32
MAX_PANEL_FILE_BYTES = 4 * 1024 * 1024
MAX_LISTED_PANELS = 50
# Lumi's bridge script, served in each panel's own path. Pack files can't be
# served at a path with a dot-prefixed part (pack_relative_path), so this name
# never shadows one of the pack's files.
BRIDGE_PATH = ".lumi/bridge.js"
_TOKEN = re.compile(r"[A-Za-z0-9_-]{32}")
_ID = re.compile(r"[a-z0-9][a-z0-9-]{0,39}")
_PACK_ID = re.compile(r"[A-Za-z0-9._-]{1,80}")
# What a panel's files may be: pages, scripts, styles and images, all a panel
# can load (its policy allows no fetch). Others are refused, and
# X-Content-Type-Options stops a browser from reading one type as another.
CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".htm": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".ico": "image/x-icon",
}
# Fetch Metadata (Sec-Fetch-Dest): a panel's files load only as its frame's
# page and that page's scripts, styles and images. A panel URL opened as a
# top-level page (pasted into the address bar) is refused, where it could
# navigate its own tab away, and so is a fetch, which the policy refuses too.
# A browser that doesn't send the header still gets the policy's sandbox.
FETCH_DESTINATIONS = frozenset({"iframe", "frame", "script", "style", "image"})
# Powerful features a panel never needs; the frame's sandbox and opaque origin
# refuse them too, and this covers a panel URL opened outside the frame.
PERMISSIONS_POLICY = ", ".join(f"{name}=()" for name in (
    "accelerometer", "camera", "display-capture", "geolocation", "gyroscope", "hid", "magnetometer",
    "microphone", "midi", "payment", "serial", "usb"))
VERIFIED_TTL = 3.0
# Panel files are read and hashed here, never on the default pool the app
# socket's commands use.
_PANEL_IO = ThreadPoolExecutor(max_workers=2, thread_name_prefix="lumi-panels")


class PanelError(Exception):
    """A panel can't be opened or served; the message says why, for the person."""

    def __init__(self, message: str, status: int = 403):
        super().__init__(message)
        self.status = status


# ── Whether panels may show at all ─────────────────────────────────────


def enabled(settings: Any) -> tuple[bool, str]:
    """``(allowed, why not)``: Settings > Privacy & security, and the organization's policy.

    Only an explicit ``true`` turns panels on, so a policy that locks the
    switch to a value it can't be (``"no"``) leaves them off. A policy that
    can't be used, or has expired, turns them off too, as it stops requests.
    So does offline mode (lumi/offline.py): a panel's content security policy
    leaves it no network but WebRTC, which the browser doesn't let a policy
    turn off and offline mode can't check.
    """
    from .. import offline
    from ..policy import blocked_reason, current

    reason = blocked_reason()
    if reason:
        return False, reason
    if offline.enabled():
        return False, ("Offline mode: panels from capability packs can connect by WebRTC, which offline mode "
                       "can't check, so they stay closed while it's on.")
    if settings.get("security", "extension_panels", True) is not True:
        policy = current()
        if policy and policy.locked("security", "extension_panels"):
            return False, f"{policy.organization}'s policy turns off panels from capability packs."
        return False, "Panels from capability packs are off in Settings > Privacy & security."
    return True, ""


def _label(text: Any, limit: int = 80) -> str:
    """A pack's name as the page shows it: one line, no invisible or control characters, bounded."""
    visible = "".join(ch for ch in str(text) if unicodedata.category(ch) not in ("Cc", "Cf"))
    return " ".join(visible.split())[:limit] or "Unnamed"


def _packs(state: Any, project: str):
    """A manager that has just read the packs, with the approvals Settings holds now."""
    from ..engine.capability_packs import CapabilityPackManager

    manager = CapabilityPackManager(project, configured=state.settings.get("plugins") or {},
                                    publishers=state.settings.get("pack_publishers") or {})
    try:
        manager.discover()
    except Exception:  # a broken pack folder mustn't break the app page
        logger.warning("Capability pack discovery failed for panels", exc_info=True)
    return manager


def _find(manager: Any, pack_id: str, panel_id: str, location: str = "") -> tuple[Any, dict]:
    """The approved pack and its panel; PanelError when the panel can't show."""
    from ..engine.capability_packs import pack_location_key

    valid = bool(_PACK_ID.fullmatch(pack_id or ""))
    pack = manager.get(pack_id) if valid else None
    if pack is None or (location and pack_location_key(pack.path) != location):
        raise PanelError(f"The {pack_id if valid else 'panel'} pack isn't installed here any more.", 404)
    name = _label(pack.name)
    if not (pack.trusted and pack.enabled and pack.status == "approved"):
        why = " It changed since you approved it." if pack.status == "changed" else (
            f" {pack.problem}" if pack.problem else "")
        raise PanelError(f"Approve the {name} pack in Settings > Capability packs to use its panels.{why}")
    panel = next((p for p in pack.ui_panels if p["id"] == panel_id), None)
    if panel is None:
        raise PanelError(f"The {name} pack has no panel {panel_id!r}.", 404)
    return pack, panel


def listing(state: Any) -> dict:
    """The open project's panels, for View and the command palette."""
    project = str(state.project.project_path)
    allowed, reason = enabled(state.settings)
    panels: list[dict] = []
    if allowed:
        # active(): approved, enabled, allowed by policy and unchanged.
        for pack in _packs(state, project).active():
            panels.extend({"pack": pack.id, "pack_name": _label(pack.name), "panel": panel["id"],
                           "title": panel["title"]} for panel in pack.ui_panels)
    panels.sort(key=lambda row: (row["title"].casefold(), row["pack_name"].casefold(), row["panel"]))
    return {"event": "extension_panels", "enabled": allowed, "reason": reason, "project": project,
            "panels": panels[:MAX_LISTED_PANELS]}


# ── Panel tokens ───────────────────────────────────────────────────────


@dataclass(frozen=True)
class PanelGrant:
    """What one panel token reads: one panel of one pack, at one location, for one page."""

    token: str
    project: str
    pack_id: str
    location: str
    panel_id: str
    entry: str
    owner: int
    created: float


class PanelGrants:
    """Live panel tokens, in memory only, bounded in number and age."""

    def __init__(self, *, ttl: float = PANEL_TOKEN_TTL, limit: int = MAX_PANEL_TOKENS) -> None:
        self._ttl = ttl
        self._limit = limit
        self._grants: dict[str, PanelGrant] = {}
        self._lock = threading.Lock()

    def issue(self, *, project: str, pack: Any, panel: dict, owner: int) -> PanelGrant:
        from ..engine.capability_packs import pack_location_key

        grant = PanelGrant(token=secrets.token_urlsafe(24), project=project, pack_id=pack.id,
                           location=pack_location_key(pack.path), panel_id=panel["id"], entry=panel["entry"],
                           owner=owner, created=time.monotonic())
        with self._lock:
            self._expire()
            self._grants[grant.token] = grant
            while len(self._grants) > self._limit:  # the oldest goes first
                self._grants.pop(next(iter(self._grants)))
        return grant

    def get(self, token: str) -> PanelGrant | None:
        with self._lock:
            self._expire()
            return self._grants.get(token)

    def revoke(self, token: str) -> None:
        with self._lock:
            self._grants.pop(token, None)

    def revoke_owner(self, owner: int) -> None:
        """Withdraw a page's tokens when its socket closes."""
        with self._lock:
            for token in [t for t, grant in self._grants.items() if grant.owner == owner]:
                del self._grants[token]

    def clear(self) -> None:
        with self._lock:
            self._grants.clear()

    def _expire(self) -> None:
        cutoff = time.monotonic() - self._ttl
        for token in [t for t, grant in self._grants.items() if grant.created < cutoff]:
            del self._grants[token]


grants = PanelGrants()


def _folder(entry: str) -> str:
    """The entry file's folder in the pack: what the panel's URLs are relative to."""
    parent = PurePosixPath(entry).parent.as_posix()
    return "" if parent == "." else parent


@dataclass(frozen=True)
class _Verified:
    """A grant's pack as checked against its approval: its folder, its name, and each approved file's hash."""

    path: str
    name: str
    hashes: dict[str, str]


_verified: dict[tuple, tuple[float, _Verified]] = {}
_verified_lock = threading.Lock()


def _fingerprint(state: Any, pack_id: str) -> str:
    """What decides a pack's approval besides its files: its approval, the trusted publishers, the policy."""
    from ..policy import current

    policy = current()
    plugins = state.settings.get("plugins") or {}
    material = [plugins.get(pack_id) if isinstance(plugins, dict) else None,
                state.settings.get("pack_publishers") or {}, policy.raw if policy else None]
    return hashlib.sha256(json.dumps(material, sort_keys=True, default=str).encode("utf-8")).hexdigest()


def _verify(state: Any, grant: PanelGrant, *, fresh: bool = False) -> _Verified:
    """The grant's pack, still allowed, approved, enabled and unchanged; PanelError, grant withdrawn, otherwise.

    A pack checked in the last ``VERIFIED_TTL`` seconds under the same
    approval, publishers and policy is reused unless ``fresh``: its files'
    hashes are the approved ones, and every file served is hashed against them.
    """
    allowed, reason = enabled(state.settings)
    if not allowed:
        grants.revoke(grant.token)
        raise PanelError(reason)
    key = (grant.project, grant.pack_id, grant.location, grant.panel_id, grant.entry,
           _fingerprint(state, grant.pack_id))
    now = time.monotonic()
    if not fresh:
        with _verified_lock:
            cached = _verified.get(key)
        if cached and cached[0] > now:
            return cached[1]
    manager = _packs(state, grant.project)
    try:
        pack, panel = _find(manager, grant.pack_id, grant.panel_id, grant.location)
    except PanelError:
        # A revoked, disabled or removed pack loses its open panels for good.
        grants.revoke(grant.token)
        raise
    hashes = manager.verified_files(pack) if panel["entry"] == grant.entry else None
    if hashes is None:
        grants.revoke(grant.token)
        raise PanelError(f"The {_label(pack.name)} pack changed since you approved it. Review it in Settings > "
                         "Capability packs.")
    verified = _Verified(path=pack.path, name=_label(pack.name), hashes=hashes)
    with _verified_lock:
        for stale in [k for k, (expires, _) in _verified.items() if expires <= now]:
            del _verified[stale]
        _verified[key] = (now + VERIFIED_TTL, verified)
    return verified


def forget_verified() -> None:
    """Drop every kept check (tests; a changed approval or policy misses the cache by itself)."""
    with _verified_lock:
        _verified.clear()


def check(state: Any, token: str, owner: int) -> None:
    """Before a panel adds text to the message box: the panel is this page's, its pack still as approved.

    Checked from scratch, not from what was kept for serving files: a pack
    changed or revoked while its panel was open adds nothing.
    """
    grant = grants.get(token) if _TOKEN.fullmatch(token or "") else None
    if grant is None or grant.owner != owner:
        raise PanelError("This panel has closed.", 404)
    _verify(state, grant, fresh=True)


async def run_io(function: Any, *args: Any) -> Any:
    """``function(*args)`` on the panels' own threads, off the event loop and the default pool."""
    return await asyncio.get_running_loop().run_in_executor(_PANEL_IO, function, *args)


def open_panel(state: Any, pack_id: str, panel_id: str, *, owner: int) -> dict:
    """Check the pack again and issue a token for one of its panels."""
    from .. import audit

    allowed, reason = enabled(state.settings)
    if not allowed:
        raise PanelError(reason)
    if not _ID.fullmatch(panel_id or ""):
        raise PanelError("That isn't a panel.", 404)
    project = str(state.project.project_path)
    pack, panel = _find(_packs(state, project), pack_id, panel_id)
    grant = grants.issue(project=project, pack=pack, panel=panel, owner=owner)
    audit.record("extension.panel_open", pack=pack.id, panel=panel["id"])
    page = quote(PurePosixPath(panel["entry"]).name)
    return {"pack": pack.id, "pack_name": _label(pack.name), "panel": panel["id"], "title": panel["title"],
            "token": grant.token, "url": f"/panels/{grant.token}/{page}"}


# ── Serving a panel's files ────────────────────────────────────────────


def content_security_policy(host: str, token: str, scheme: str = "http") -> str:
    """The policy on every response for a panel: only its own files, and no network.

    Sources are limited to the panel's own path. A bracketed IPv6 host isn't a
    valid source; there ``'self'``, the URL's own origin even in a sandbox,
    stands in. ``sandbox allow-scripts`` gives the document an opaque origin
    however it is opened.
    """
    base = f"{scheme}://{host}/panels/{token}/" if host and not host.startswith("[") else "'self'"
    return "; ".join((
        "default-src 'none'",
        f"script-src {base}",
        f"style-src {base}",
        f"img-src {base} data: blob:",
        "connect-src 'none'",
        "frame-src 'none'",
        "child-src 'none'",
        "worker-src 'none'",
        "object-src 'none'",
        "base-uri 'none'",
        "form-action 'none'",
        "frame-ancestors 'self'",
        "sandbox allow-scripts",
    ))


_DOCTYPE = re.compile(rb"\A(?:\xef\xbb\xbf)?\s*<!doctype[^>]*>", re.I)


def with_bridge(html: bytes, token: str) -> bytes:
    """``html`` with Lumi's bridge script before anything else, after the doctype if there is one.

    It must run before any of the panel's scripts: it registers its message
    listener first, which keeps the page's private channel from the panel's
    own code. Browsers build the head around a script placed here.
    """
    tag = f'<script src="/panels/{token}/{BRIDGE_PATH}"></script>'.encode("ascii")
    match = _DOCTYPE.search(html)
    if match:
        return html[:match.end()] + tag + html[match.end():]
    bom = b"\xef\xbb\xbf" if html.startswith(b"\xef\xbb\xbf") else b""
    return bom + tag + html[len(bom):]


def _read(state: Any, grant: PanelGrant, path: str, bridge_file: Path) -> tuple[bytes, str]:
    """The bytes and type to serve for ``path``; PanelError otherwise. Runs on the panels' threads."""
    from ..engine.capability_packs import pack_relative_path

    verified = _verify(state, grant)
    if path == BRIDGE_PATH:
        return bridge_file.read_bytes(), CONTENT_TYPES[".js"]
    relative = pack_relative_path(path)
    folder = _folder(grant.entry)
    in_pack = f"{folder}/{relative}" if folder and relative else relative
    expected = verified.hashes.get(in_pack) if in_pack else None
    kind = CONTENT_TYPES.get(PurePosixPath(in_pack or "").suffix.lower())
    if expected is None or kind is None:
        raise PanelError("This panel has no such file.", 404)
    with open(Path(verified.path) / in_pack, "rb") as handle:
        data = handle.read(MAX_PANEL_FILE_BYTES + 1)
    if len(data) > MAX_PANEL_FILE_BYTES:
        raise PanelError("This panel file is larger than 4 MB.", 413)
    # What was read, not only what was checked: a file changed since is refused.
    if hashlib.sha256(data).hexdigest() != expected:
        grants.revoke(grant.token)
        raise PanelError(f"The {verified.name} pack changed since you approved it.")
    if kind.startswith("text/html"):
        data = with_bridge(data, grant.token)
    return data, kind


def _headers(request: Any, token: str) -> dict[str, str]:
    host = request.headers.get("host", "").strip().lower()
    scheme = "https" if request.url.scheme in ("https", "wss") else "http"
    return {
        "Content-Security-Policy": content_security_policy(host, token, scheme),
        "X-Content-Type-Options": "nosniff",
        "Referrer-Policy": "no-referrer",
        # A panel whose pack is withdrawn must not load from a cache.
        "Cache-Control": "no-store",
        "X-DNS-Prefetch-Control": "off",
        "Permissions-Policy": PERMISSIONS_POLICY,
    }


async def serve(request: Any, state: Any, *, bridge_file: Path) -> Any:
    """``GET /panels/{token}/{path}``: one file of an open panel, or why not, as plain text."""
    from starlette.responses import Response

    from .local_access import same_origin

    token = str(request.path_params.get("token") or "")
    path = str(request.path_params.get("path") or "")

    def refuse(message: str, status: int) -> Any:
        return Response(message, status_code=status, media_type="text/plain; charset=utf-8",
                        headers=_headers(request, token if _TOKEN.fullmatch(token) else "none"))

    # A sandboxed frame's requests carry no Origin or "null". LocalHostGuard
    # has already checked Host; another site's pages are refused here too,
    # though without the panel token they could read nothing.
    origin = request.headers.get("origin")
    if origin is not None and origin.strip() != "null" and not same_origin(request, require_origin=True):
        return refuse("Forbidden", 403)
    destination = request.headers.get("sec-fetch-dest")
    if destination is not None and destination.strip().lower() not in FETCH_DESTINATIONS:
        return refuse("A panel opens inside Lumi, from View > Panels.", 403)
    grant = grants.get(token) if _TOKEN.fullmatch(token) else None
    if grant is None:
        return refuse("This panel has closed. Open it again from View > Panels.", 404)
    try:
        data, kind = await run_io(_read, state, grant, path, bridge_file)
    except PanelError as exc:
        return refuse(str(exc), exc.status)
    except OSError:
        logger.warning("Couldn't read a panel file", exc_info=True)
        return refuse("This panel file couldn't be read.", 404)
    return Response(data, media_type=kind, headers=_headers(request, token))
