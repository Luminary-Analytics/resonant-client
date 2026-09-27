"""Feedback from the app to Lumi Cloud's feedback inbox (docs/feedback.md).

Help > Send feedback… (also the command palette, About Lumi and the profile
menu) opens a dialog whose report goes to ``POST <Lumi Cloud>/api/v1/feedback``
as JSON::

    {"kind": "bug" | "idea" | "other", "message": "...", "reply_to": "you@example.com" | null,
     "app": {"version": "...", "channel": "...", "os": "...", "arch": "..."},
     "install_id": "...", "diagnostics": {...} | null}

Lumi Cloud answers 201 ``{"id": ...}``, 400 when it refuses the report, 413
when it's too large and 429 with ``Retry-After`` when it's busy.

* **Always sent:** the app's version, update channel, operating system and
  architecture, and an install id. The id is derived from a random secret
  made on first use (``feedback/install-id``), separately for each Lumi Cloud
  and for signed out or each signed-in account (``install_id``): reports sent
  signed out can't be linked to an account's, nor one Lumi Cloud's to
  another's.
* **Diagnostics, only when the person checks them** (``diagnostics``): Python's
  version, the platform, whether this is a packaged build, the provider type
  and model in use (never a key), whether offline mode is on, and the end of
  Lumi's startup log without saved keys, secret-looking values or the home
  folder. The dialog shows the exact report first (``preview``), and sending
  uses that previewed report, so what was shown is what goes.
* **The account, only while the person is signed in** to that very Lumi
  Cloud: the request then carries their access token, so staff see who sent
  it. A report written while signed out is never attributed later.

Before anything leaves the computer, offline mode refuses the report unless
the Lumi Cloud host is allowed (``offline_refusal``, before anything else
looks at it). Then ``prepare`` runs ``secret_scan`` with its patterns on over
every text, and the organization's DLP rules when a policy applies
(``dlp.check_text``, purpose ``feedback``: the message and reply-to as
``prompt``, diagnostics as ``attachment``). A refusal says why, and the
dialog offers the report as text to copy instead (``copy_text``), except when
DLP refused it.

Sending uses ``net.client_options`` (proxy, certificates, offline mode). With
no Lumi Cloud address, or when it can't be reached or is busy, the report
waits in ``feedback/queue.json`` (at most MAX_QUEUED reports and
MAX_QUEUE_BYTES, for QUEUE_DAYS) and ``flush``, run by the thread
``start_background`` starts, sends it later, to the Lumi Cloud it was written
for. A person sends at most MAX_RECENT reports in RECENT_SECONDS. The audit
log records ``feedback.sent``, ``feedback.queued``, ``feedback.dropped`` and
``feedback.refused`` with the kind and size, never the text.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import platform
import re
import secrets
import sys
import threading
import time
import unicodedata
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .paths import state_home

logger = logging.getLogger(__name__)

ENDPOINT = "/api/v1/feedback"
FEATURE = "sending feedback"
PURPOSE = "feedback"
KINDS = ("bug", "idea", "other")
KIND_LABELS = {"bug": "Bug", "idea": "Idea", "other": "Other"}
MAX_MESSAGE = 5000
# What Lumi Cloud takes: room for redaction markers, which can be longer than what they replace.
MAX_SENT_MESSAGE = 8000
MAX_REPLY_TO = 254
LOG_TAIL_LINES = 60
LOG_TAIL_CHARS = 6000
MAX_DIAGNOSTIC_TEXT = 12000  # after redaction markers; Lumi Cloud takes up to 16,000 characters per text
_LOG_READ_BYTES = 64 * 1024
MAX_QUEUED = 20
MAX_QUEUE_BYTES = 512 * 1024
QUEUE_DAYS = 30
MAX_RECENT, RECENT_SECONDS = 5, 600
PREVIEW_SECONDS = 900
MAX_PREVIEWS = 20
FIRST_TRY_SECONDS = 60.0
LOOP_SECONDS = 60.0
RETRY_SECONDS = 600.0
MAX_BACKOFF_SECONDS = 6 * 3600.0
MAX_RETRY_AFTER = 24 * 3600
TIMEOUT_SECONDS = 20.0

# Reply-to addresses: a common subset of RFC 5322 in ASCII, one @, and a domain
# of dot-separated labels ending in letters. Lumi Cloud checks the same rule,
# and nothing in it (such as ? or &) can add to a mailto: link.
EMAIL = re.compile(
    r"(?=.{3,254}$)(?!\.)(?!.*\.\.)[A-Za-z0-9._%+'-]{1,64}(?<!\.)@"
    r"(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+[A-Za-z]{2,63}")
INSTALL_ID = re.compile(r"[A-Za-z0-9_-]{16,64}")
_REPORT_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")
_RETRY_AFTER = re.compile(r"[0-9]{1,6}")
# Bidirectional embeddings, overrides and isolates: they can make text read
# differently than it's stored. (Left-to-right and right-to-left marks stay.)
_BIDI_CONTROLS = frozenset({"LRE", "RLE", "PDF", "LRO", "RLO", "LRI", "RLI", "FSI", "PDI"})
# A sign-in that ended: the report goes without the account. Any other failure to get the token keeps the report.
_SIGNED_OUT = frozenset({"signed_out", "invalid_grant"})

_lock = threading.RLock()          # the files in feedback/, the previews and what's being sent
_submit_lock = threading.Lock()    # one report at a time from this app
_flush_lock = threading.Lock()     # one flush at a time
_wake = threading.Event()
_transport: Any = None             # tests install an httpx.MockTransport
_previews: OrderedDict[str, _Preview] = OrderedDict()
_sending: set[str] = set()         # waiting reports a flush is sending now; Discard leaves them


class FeedbackError(Exception):
    """A report that can't be sent now; ``message`` is for the person, ``code`` says why.

    Codes: ``invalid`` (a field, named by ``field``), ``preview`` (review the
    report again), ``dlp``, ``offline``, ``rate_limited``, ``queue_full`` and
    ``refused`` (Lumi Cloud won't take it; ``status`` is its answer).
    """

    def __init__(self, message: str, *, code: str, field: str = "", status: int = 0) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.field = field
        self.status = status

    def as_dict(self) -> dict:
        return {"code": self.code, "message": self.message, "field": self.field}


class _Later(Exception):
    """Lumi Cloud couldn't take the report now; keep it and try again."""

    def __init__(self, reason: str, retry_after: float | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.retry_after = retry_after


@dataclass(frozen=True)
class Outcome:
    """What happened to a report the person sent: ``sent`` (with Lumi Cloud's id) or ``queued``."""

    status: str
    message: str
    id: str = ""
    reason: str = ""

    def as_dict(self) -> dict:
        return {"status": self.status, "message": self.message, "id": self.id, "reason": self.reason}


def set_transport_for_tests(transport: Any) -> None:
    """Send through ``transport`` (an ``httpx.MockTransport``) instead of the network; None undoes it."""
    global _transport
    _transport = transport


def reset_for_tests() -> None:
    set_transport_for_tests(None)
    with _lock:
        _previews.clear()
        _sending.clear()
    _wake.clear()


# ── The form ───────────────────────────────────────────────────────────────


def clean_text(value: Any) -> str:
    """Text as it's sent: line breaks as newlines, no control characters but tabs and
    line breaks, no bidirectional controls, and no space at either end."""
    kept = []
    for character in str(value if value is not None else "").replace("\r\n", "\n").replace("\r", "\n"):
        category = unicodedata.category(character)
        if character in "\n\t":
            kept.append(character)
        elif category in ("Zl", "Zp"):
            kept.append("\n")
        elif category in ("Cc", "Cs") or unicodedata.bidirectional(character) in _BIDI_CONTROLS:
            continue
        else:
            kept.append(character)
    return "".join(kept).strip()


def clean_reply_to(value: Any) -> str | None:
    """A reply-to address as sent (its domain in lower case), None for none; FeedbackError otherwise."""
    text = str(value if value is not None else "").strip()
    if not text:
        return None
    if len(text) > MAX_REPLY_TO or not EMAIL.fullmatch(text):
        raise FeedbackError("Enter an email address such as you@example.com, or leave it empty.", code="invalid",
                            field="reply_to")
    local, _, domain = text.rpartition("@")
    return f"{local}@{domain.lower()}"


@dataclass(frozen=True)
class Form:
    """What the person filled in, checked."""

    kind: str
    message: str
    reply_to: str | None
    diagnostics: bool

    @classmethod
    def read(cls, data: Any) -> Form:
        data = data if isinstance(data, dict) else {}
        kind = data.get("kind")
        if kind not in KINDS:
            raise FeedbackError("Choose Bug, Idea or Other.", code="invalid", field="kind")
        message = clean_text(data.get("message"))
        if not message:
            raise FeedbackError("Write what happened, or what you'd like.", code="invalid", field="message")
        if len(message) > MAX_MESSAGE:
            raise FeedbackError(f"Keep the message to {MAX_MESSAGE:,} characters.", code="invalid", field="message")
        return cls(kind=kind, message=message, reply_to=clean_reply_to(data.get("reply_to")),
                   diagnostics=data.get("include_diagnostics") is True)

    def digest(self) -> str:
        """Identifies the form's contents, to match a report with its preview."""
        text = json.dumps([self.kind, self.message, self.reply_to, self.diagnostics], ensure_ascii=False)
        return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ── What is always sent ────────────────────────────────────────────────────


def _clip(value: Any, limit: int) -> str:
    return " ".join(str(value if value is not None else "").split())[:limit]


def _channel() -> str:
    try:
        from .update_channels import read

        channel = read().channel
    except Exception:  # the report still goes without the update settings
        logger.debug("Couldn't read the update channel for feedback", exc_info=True)
        channel = "stable"
    return channel if channel in ("stable", "beta") else "stable"


def _os_name() -> str:
    system = platform.system() or ""
    release = platform.release() or ""
    if system == "Darwin":
        system, release = "macOS", platform.mac_ver()[0] or release
    text = re.sub(r"[^A-Za-z0-9 ._()-]", "", f"{system} {release}").strip()
    return text[:60].strip() or "unknown"


def _arch() -> str:
    return re.sub(r"[^a-z0-9_.-]", "", (platform.machine() or "").lower())[:20] or "unknown"


def app_info() -> dict:
    """The app's version, update channel, operating system and architecture, sent with every report."""
    from . import __version__

    version = re.sub(r"[^0-9A-Za-z.+-]", "", __version__)[:40] or "unknown"
    return {"version": version, "channel": _channel(), "os": _os_name(), "arch": _arch()}


def _folder() -> Path:
    return state_home() / "feedback"


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".{os.getpid()}.tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(path)


def _install_secret() -> str:
    """This install's random secret, made on first use from nothing on the computer or the account."""
    path = _folder() / "install-id"
    with _lock:
        try:
            value = path.read_text(encoding="ascii").strip()
        except (OSError, ValueError):
            value = ""
        if not INSTALL_ID.fullmatch(value):
            value = secrets.token_urlsafe(32)
            _write(path, value)
        return value


def install_id(url: str = "", account: str = "") -> str:
    """The install id a report carries: one for each Lumi Cloud (``url``), and for signed out or each account.

    Derived from the install's secret with HMAC-SHA256, so staff can tell that
    reports came from one install, while an id seen with an account's reports
    never matches the id of the same install's reports sent signed out.
    """
    context = f"lumi-feedback install|{url}|{account}".encode()
    digest = hmac.new(_install_secret().encode("ascii"), context, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")[:32]


# ── Diagnostics ────────────────────────────────────────────────────────────


def provider_type(backend_type: Any) -> str:
    """The kind of provider in use: a connection's id stays on this computer."""
    text = str(backend_type or "")
    if text.startswith("conn-"):
        return "connection"
    return re.sub(r"[^a-z0-9_.-]", "", text.lower())[:40]


def _without_home(text: str) -> str:
    """``text`` with the home folder written as ``~``: it names the person's account on this computer."""
    homes = {str(Path.home()), os.path.expanduser("~")}
    flags = re.IGNORECASE if sys.platform == "win32" else 0
    for home in sorted((h.rstrip("\\/") for h in homes if h and len(h.rstrip("\\/")) > 3), key=len, reverse=True):
        # As written, with either separator, and escaped as in JSON or a repr (C:\\Users\\ann).
        forms = {home, home.replace("\\", "/"), home.replace("/", "\\"), home.replace("/", "\\").replace("\\", "\\\\")}
        for written in sorted(forms, key=len, reverse=True):
            # Only the whole folder name: C:\Users\ann doesn't turn C:\Users\anna into ~a.
            text = re.sub(re.escape(written) + r"(?![\w.-])", "~", text, flags=flags)
    return text


def log_tail(settings: Any = None) -> str:
    """The end of Lumi's startup log, with saved keys, secret-looking values and the home folder removed.

    Uses the diagnostics bundle's redaction (gui/diagnostics.py) over what it
    reads, before cutting it to LOG_TAIL_LINES whole lines and LOG_TAIL_CHARS, so
    a cut never leaves part of a secret that no longer looks like one. The log
    exists in packaged builds; running from source, this is empty.
    """
    from . import secret_scan
    from .gui.diagnostics import MIN_KNOWN_SECRET_LENGTH, redact

    logs = state_home() / "logs"
    path = next((logs / name for name in ("lumi-startup.log", "resonant-startup.log") if (logs / name).is_file()), None)
    if path is None:
        return ""
    try:
        with open(path, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - _LOG_READ_BYTES))
            raw = handle.read()
    except OSError:
        return ""
    lines = raw.decode("utf-8", errors="replace").splitlines()
    if size > _LOG_READ_BYTES and lines:
        lines = lines[1:]  # the first line read may be the end of a longer one
    known = secret_scan.secret_values(settings, min_length=MIN_KNOWN_SECRET_LENGTH) if settings is not None else ()
    text = _without_home(redact("\n".join(line.rstrip() for line in lines), known))
    kept: list[str] = []
    size = 0
    for line in reversed(text.splitlines()[-LOG_TAIL_LINES:]):
        if size + len(line) + 1 > LOG_TAIL_CHARS:
            if not kept:
                kept.append(line[-LOG_TAIL_CHARS:])  # one long line: its end, redacted already
            break
        kept.append(line)
        size += len(line) + 1
    return "\n".join(reversed(kept)).strip()


def diagnostics(settings: Any = None, *, provider: str = "", model: str = "") -> dict:
    """What "Include diagnostics" adds, before ``prepare`` scans it for secrets and checks it with DLP.

    Its texts are cleaned as the message is (``clean_text``): a log can hold
    terminal escape codes, which Lumi Cloud refuses as control characters.
    """
    from . import offline

    return {
        "python": platform.python_version(),
        "platform": clean_text(_clip(platform.platform(), 120)),
        "packaged": bool(getattr(sys, "frozen", False)),
        "provider": provider_type(provider),
        "model": clean_text(_clip(model, 120)),
        "offline_mode": offline.enabled(),
        "log_tail": clean_text(log_tail(settings)),
    }


# ── Before anything leaves ─────────────────────────────────────────────────


@dataclass
class Prepared:
    """A report as it will be sent, and what the person should know about it."""

    body: dict
    notices: list[str] = field(default_factory=list)


def _known(settings: Any) -> tuple[str, ...]:
    from . import secret_scan

    return tuple(secret_scan.secret_values(settings)) if settings is not None else ()


def _scan(text: str, known: tuple[str, ...], found) -> str:
    from . import secret_scan

    cleaned, counts = secret_scan.redact_text(text, patterns=True, known=known)
    found.update(counts)
    return cleaned


def _map_strings(value: Any, change) -> Any:
    if isinstance(value, str):
        return change(value)
    if isinstance(value, dict):
        return {key: _map_strings(item, change) for key, item in value.items()}
    if isinstance(value, list):
        return [_map_strings(item, change) for item in value]
    return value


def _checked(body: dict, settings: Any) -> Prepared:
    """``body`` after the secret scan (patterns on) and the organization's DLP rules.

    DLP's block refuses the report (FeedbackError ``dlp``); its redactions
    apply to what is sent. A reply-to address DLP would change is left out.
    """
    from collections import Counter

    from . import dlp

    found: Counter = Counter()
    known = _known(settings)
    scanned = {**body, "message": _scan(body["message"], known, found),
               "diagnostics": _map_strings(body.get("diagnostics"), lambda text: _scan(text, known, found))}
    notices = []
    if found:
        total = sum(found.values())
        kinds = ", ".join(kind for kind, _ in found.most_common())
        notices.append(f"Removed {total} secret{'' if total == 1 else 's'} ({kinds}) from the report.")

    def check(text: str, kind: str) -> str:
        return dlp.check_text(text, purpose=PURPOSE, kind=kind, provider="lumi-cloud")

    try:
        message = check(scanned["message"], "prompt")
        reply_to = scanned.get("reply_to")
        if reply_to and check(reply_to, "prompt") != reply_to:
            reply_to = None
            notices.append("Your organization's data loss prevention rules don't let the reply-to address leave "
                           "this computer, so it was left out.")
        diagnostics_value = _map_strings(scanned.get("diagnostics"), lambda text: check(text, "attachment"))
    except dlp.Blocked as exc:
        raise FeedbackError(exc.message, code="dlp") from exc
    if len(message) > MAX_SENT_MESSAGE:
        # Redaction markers can be longer than what they replace; Lumi Cloud takes up to MAX_SENT_MESSAGE.
        raise FeedbackError("With secrets and your organization's matches replaced, the message is too long to "
                            "send. Shorten it.", code="invalid", field="message")
    # The same for a diagnostic text, which the person didn't write: its end is kept (Lumi Cloud takes 16,000).
    diagnostics_value = _map_strings(diagnostics_value, lambda text: text[-MAX_DIAGNOSTIC_TEXT:])
    if message != scanned["message"] or diagnostics_value != scanned.get("diagnostics"):
        notices.append("Your organization's data loss prevention rules redacted part of the report.")
    return Prepared({**scanned, "message": message, "reply_to": reply_to, "diagnostics": diagnostics_value}, notices)


def prepare(form: Form, *, settings: Any = None, provider: str = "", model: str = "", url: str = "",
            account: str = "") -> Prepared:
    """The report ``form`` makes, exactly as it would be sent to ``url`` as ``account`` ("" signed out)."""
    body = {
        "kind": form.kind,
        "message": form.message,
        "reply_to": form.reply_to,
        "app": app_info(),
        "install_id": install_id(url, account),
        "diagnostics": diagnostics(settings, provider=provider, model=model) if form.diagnostics else None,
    }
    return _checked(body, settings)


def _size(body: dict) -> int:
    return len(_encode(body))


def _encode(body: dict) -> bytes:
    return json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


# ── Lumi Cloud ─────────────────────────────────────────────────────────────


def cloud_url(cloud: Any) -> str:
    """The Lumi Cloud address reports go to, or "" when none is set (or it isn't one Lumi uses)."""
    from .cloud import CloudError, normalize_url

    raw = str(getattr(cloud, "url", "") or "")
    if not raw:
        return ""
    try:
        url = normalize_url(raw)
        urlsplit(url).port  # noqa: B018 - a port that isn't a number raises ValueError here, not mid-send
    except (CloudError, ValueError):
        return ""
    return url


def _host(url: str) -> str:
    return urlsplit(url).netloc if url else ""


def offline_refusal(url: str) -> str:
    """Why offline mode keeps a report from ``url``, or "" when it may go (or offline mode is off)."""
    from . import offline

    if not offline.enabled():
        return ""
    if not url:
        return ("Offline mode: sending feedback needs Lumi Cloud, and no Lumi Cloud address is set; set one in "
                "Settings > Lumi account and allow it, or turn offline mode off.")
    return offline.refusal(url + ENDPOINT, FEATURE)


def _account(cloud: Any, url: str) -> tuple[str, str]:
    """The Lumi Cloud id and email of the person signed in to ``url``, or ("", "").

    A sign-in counts only for the Lumi Cloud that issued it (the client's
    ``account_url``, recorded when the sign-in completed): when reports go
    anywhere else, they go without the account, and its token never goes to a
    host that didn't issue it.
    """
    from .cloud import same_address

    if not url:
        return "", ""
    try:
        status = cloud.status()
        issued = str(getattr(cloud, "account_url", "") or "")
    except Exception:  # no account is the safe answer to any problem here
        logger.debug("Couldn't read the Lumi Cloud sign-in for feedback", exc_info=True)
        return "", ""
    account = status.get("account") if isinstance(status.get("account"), dict) else {}
    if not status.get("signed_in") or not account.get("user_id") or not same_address(issued, url):
        return "", ""
    return str(account.get("user_id")), str(account.get("email") or "")


def _retry_after(response: Any) -> float | None:
    value = str(response.headers.get("retry-after") or "").strip()
    return float(min(max(int(value), 1), MAX_RETRY_AFTER)) if _RETRY_AFTER.fullmatch(value) else None


def _refusal(status: int, response: Any) -> str:
    if status == 413:
        return "The report is too large for Lumi Cloud. Shorten the message, or leave diagnostics out."
    if status == 404:
        return "This Lumi Cloud doesn't take feedback (it answered 404)."
    try:
        data = response.json()
    except ValueError:
        data = {}
    detail = _clip(data.get("error_description"), 300) if isinstance(data, dict) else ""
    if status == 400 and detail:
        return f"Lumi Cloud refused the report: {detail}"
    return f"Lumi Cloud refused the report (it answered {status})."


def _send(cloud: Any, url: str, body: dict, *, attribute: bool) -> tuple[str, bool]:
    """Post one report: Lumi Cloud's id for it, and whether it went with the account.

    ``_Later`` to keep it, FeedbackError when it's refused.
    """
    import httpx

    from . import __version__, offline
    from .cloud import CloudError
    from .net import client_options

    headers = {"Content-Type": "application/json", "Accept": "application/json",
               "User-Agent": f"Lumi/{__version__} ({platform.system()})"}
    if attribute:
        try:
            headers["Authorization"] = f"Bearer {cloud.account_token()}"
        except CloudError as exc:
            if exc.code not in _SIGNED_OUT:
                raise _Later("unreachable") from exc  # the token can't be had now: keep the report for later
            # The sign-in ended meanwhile: the report goes without the account.
            logger.info("Sending feedback without the account: the sign-in ended")
    try:
        with httpx.Client(**client_options(timeout=TIMEOUT_SECONDS, transport=_transport, feature=FEATURE)) as http:
            response = http.post(url + ENDPOINT, content=_encode(body), headers=headers)
    except httpx.HTTPError as exc:
        reason = offline.message_for(exc)
        if reason:
            raise FeedbackError(reason, code="offline") from exc
        raise _Later("unreachable") from exc
    except (httpx.InvalidURL, ValueError) as exc:  # an address httpx can't use: keep it until it's fixed
        raise _Later("unreachable") from exc
    attributed = "Authorization" in headers
    status = response.status_code
    if status in (200, 201):
        try:
            answer = response.json()
        except ValueError:
            answer = {}
        report = str(answer.get("id") or "") if isinstance(answer, dict) else ""
        return (report if _REPORT_ID.fullmatch(report) else ""), attributed
    if status == 429:
        raise _Later("busy", _retry_after(response) or RETRY_SECONDS)
    if status >= 500 or status == 408:
        raise _Later("unreachable")
    if status in (401, 403):
        # The contract never asks for a sign-in: a Lumi Cloud that does may take it later, so it's kept.
        raise _Later("unauthorized")
    raise FeedbackError(_refusal(status, response), code="refused", status=status)


def _record(event: str, body: dict, **extra: Any) -> None:
    """An audit record: the kind and size, never the text."""
    from . import audit

    audit.record(event, kind=str(body.get("kind") or ""), size=_size(body),
                 diagnostics=body.get("diagnostics") is not None, **extra)


# ── The queue ──────────────────────────────────────────────────────────────

_QUEUED = {
    "no_cloud": "Saved on this computer. Lumi sends it once a Lumi Cloud address is set in Settings > Lumi account.",
    "unreachable": ("Lumi Cloud couldn't be reached, so your feedback is saved on this computer. Lumi will send "
                    "it when it can."),
    "busy": "Lumi Cloud is busy, so your feedback is saved on this computer. Lumi will send it in a few minutes.",
    "unauthorized": ("Lumi Cloud didn't accept the report just now, so it's saved on this computer. Lumi will try "
                     "again later."),
}
_WHY_QUEUED = {"no_cloud": "No Lumi Cloud address is set", "unreachable": "Lumi Cloud couldn't be reached",
               "busy": "Lumi Cloud is busy", "unauthorized": "Lumi Cloud didn't accept the report"}


def _queue_path() -> Path:
    return _folder() / "queue.json"


def _load_queue() -> list[dict]:
    try:
        data = json.loads(_queue_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    items = data.get("items") if isinstance(data, dict) else None
    return [item for item in items if isinstance(item, dict) and isinstance(item.get("body"), dict)
            and item.get("id")] if isinstance(items, list) else []


def _save_queue(items: list[dict]) -> None:
    path = _queue_path()
    if not items:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            logger.warning("Couldn't delete the sent feedback's queue file", exc_info=True)
        return
    _write(path, json.dumps({"version": 1, "items": items}, ensure_ascii=False))


def waiting() -> int:
    """How many reports wait on this computer."""
    with _lock:
        return len(_load_queue())


def _queue(body: dict, *, reason: str, account: str, url: str, now: float,
           retry_after: float | None = None) -> Outcome:
    size = _size(body)
    with _lock:
        items = _load_queue()
        full = len(items) >= MAX_QUEUED or sum(int(item.get("size") or 0) for item in items) + size > MAX_QUEUE_BYTES
        if not full:
            wait = 0.0 if reason == "no_cloud" else (retry_after or RETRY_SECONDS)
            # ``url`` is where it was written for: it goes there, and to no other Lumi Cloud ("" for the first set).
            items.append({"id": secrets.token_hex(8), "body": body, "size": size, "queued_at": now,
                          "next_try": now + wait, "attempts": 0, "account": account, "url": url, "reason": reason})
            _save_queue(items)
    if full:
        _record("feedback.refused", body, reason="queue_full")
        waiting_now = len(items)
        raise FeedbackError(f"{_WHY_QUEUED[reason]}, and {waiting_now} report{'' if waiting_now == 1 else 's'} "
                            f"{'is' if waiting_now == 1 else 'are'} already waiting on this computer, so this one "
                            "wasn't saved. Copy it instead, or discard the waiting ones.", code="queue_full")
    _record("feedback.queued", body, reason=reason)
    return Outcome("queued", _QUEUED[reason], reason=reason)


def discard() -> int:
    """Delete the waiting reports; how many. One a flush is sending right now can't be taken back and stays."""
    with _lock:
        items = _load_queue()
        kept = [item for item in items if item["id"] in _sending]
        removed = [item for item in items if item["id"] not in _sending]
        _save_queue(kept)
    for item in removed:
        _record("feedback.dropped", item["body"], reason="discarded")
    return len(removed)


def flush(cloud: Any, settings: Any = None, *, force: bool = False, now: float | None = None) -> dict:
    """Send the waiting reports that are due (all of them with ``force``); counts of what happened.

    A report goes only to the Lumi Cloud it was written for, is checked again
    first (secret scan and DLP, since the rules may have changed), goes with
    the account only when the person who wrote it is still the one signed in
    there, and is dropped when Lumi Cloud refuses it, DLP blocks it or it has
    waited QUEUE_DAYS. The first report Lumi Cloud can't take now ends the
    round, so an unreachable server isn't asked once per report. ``force``
    (Send now) doesn't wait out backoff, but does wait out a 429's Retry-After.
    """
    if not _flush_lock.acquire(blocking=False):
        return {"sent": 0, "dropped": 0, "waiting": waiting(), "busy": True}
    try:
        return _flush(cloud, settings, force=force, now=time.time() if now is None else now)
    finally:
        _flush_lock.release()


def _due(item: dict, now: float, force: bool) -> bool:
    if float(item.get("next_try") or 0) <= now:
        return True
    return force and item.get("reason") != "busy"


def _flush(cloud: Any, settings: Any, *, force: bool, now: float) -> dict:
    with _lock:
        items = _load_queue()
    if not items:
        return {"sent": 0, "dropped": 0, "waiting": 0}
    url = cloud_url(cloud)
    blocked = not url or bool(offline_refusal(url))
    signed_in_as = _account(cloud, url)[0] if not blocked else ""
    done: dict[str, str] = {}
    tried: dict[str, dict] = {}
    sent: dict[str, tuple[dict, bool]] = {}

    def later(item: dict, reason: str, retry_after: float | None) -> None:
        attempts = int(item.get("attempts") or 0) + 1
        # Queued, it waited RETRY_SECONDS; each failed try doubles the wait, up to MAX_BACKOFF_SECONDS.
        wait = retry_after or min(RETRY_SECONDS * 2 ** attempts, MAX_BACKOFF_SECONDS)
        tried[item["id"]] = {"attempts": attempts, "next_try": now + wait, "reason": reason}

    try:
        for item in items:
            if now - float(item.get("queued_at") or 0) > QUEUE_DAYS * 86400:
                done[item["id"]] = "expired"
                continue
            if blocked or (item.get("url") and item.get("url") != url) or not _due(item, now, force):
                continue
            with _lock:
                if not any(entry["id"] == item["id"] for entry in _load_queue()):
                    continue  # discarded meanwhile
                _sending.add(item["id"])
            try:
                body = _checked(item["body"], settings).body
                attribute = bool(item.get("account")) and item.get("account") == signed_in_as
                _report, attributed = _send(cloud, url, body, attribute=attribute)
            except _Later as exc:
                later(item, exc.reason, exc.retry_after)
                blocked = True
            except FeedbackError as exc:
                if exc.code == "offline":
                    blocked = True  # offline mode came on meanwhile: keep it for later
                else:
                    done[item["id"]] = "dlp" if exc.code == "dlp" else "refused"
            except Exception:  # never lose the round's outcomes to one report
                logger.exception("Sending a waiting feedback report failed")
                later(item, "unreachable", None)
                blocked = True
            else:
                done[item["id"]] = "sent"
                sent[item["id"]] = (body, attributed)
            finally:
                with _lock:
                    _sending.discard(item["id"])
    finally:
        with _lock:
            kept = []
            for item in _load_queue():
                if item["id"] in done:
                    continue
                item.update(tried.get(item["id"], {}))
                kept.append(item)
            _save_queue(kept)
    for item in items:
        outcome = done.get(item["id"])
        if outcome == "sent":
            body, attributed = sent[item["id"]]
            _record("feedback.sent", body, queued=True, attributed=attributed)
        elif outcome:
            _record("feedback.dropped", item["body"], reason=outcome)
    count = sum(1 for outcome in done.values() if outcome == "sent")
    return {"sent": count, "dropped": len(done) - count, "waiting": len(kept)}


# ── Sending ────────────────────────────────────────────────────────────────


@dataclass
class _Preview:
    digest: str
    prepared: Prepared
    made: float
    url: str
    account: str


def _recent_path() -> Path:
    return _folder() / "recent.json"


def _recent(now: float) -> list[float]:
    try:
        values = json.loads(_recent_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        values = []
    return [float(v) for v in values if isinstance(v, (int, float)) and now - RECENT_SECONDS < float(v) <= now + 60] \
        if isinstance(values, list) else []


def preview(cloud: Any, form_data: Any, *, settings: Any = None, provider: str = "", model: str = "",
            now: float | None = None) -> dict:
    """The report the form makes, exactly as it would be sent, and where and as whom it would go.

    Its ``preview_id`` sends this very report (``submit``) while the form, the
    Lumi Cloud and the account stay as they are. In offline mode a report that
    couldn't go is refused first, before anything else looks at it.
    """
    now = time.time() if now is None else now
    form = Form.read(form_data)
    url = cloud_url(cloud)
    refusal = offline_refusal(url)
    if refusal:
        raise FeedbackError(refusal, code="offline")
    user_id, email = _account(cloud, url)
    prepared = prepare(form, settings=settings, provider=provider, model=model, url=url, account=user_id)
    preview_id = secrets.token_urlsafe(16)
    with _lock:
        _previews[preview_id] = _Preview(form.digest(), prepared, now, url, user_id)
        while len(_previews) > MAX_PREVIEWS:
            _previews.popitem(last=False)
    return {"preview_id": preview_id, "body": prepared.body, "notices": prepared.notices,
            "destination": _host(url), "account": email if user_id else "", "offline": ""}


def _previewed(preview_id: str, form: Form, now: float, url: str, account: str) -> Prepared:
    with _lock:
        item = _previews.get(str(preview_id or ""))
    if item is None or now - item.made > PREVIEW_SECONDS or item.digest != form.digest():
        raise FeedbackError("The report changed since you reviewed it. Review what will be sent, then send it.",
                            code="preview")
    if (item.url, item.account) != (url, account):
        raise FeedbackError("Where the report goes, or as whom, changed since you reviewed it. Review it again, "
                            "then send it.", code="preview")
    return item.prepared


def submit(cloud: Any, form_data: Any, *, settings: Any = None, provider: str = "", model: str = "",
           preview_id: str = "", now: float | None = None) -> Outcome:
    """Send a report now, or keep it to send later; FeedbackError when it can't be either.

    With diagnostics, the report is the one ``preview`` showed (``preview_id``).
    """
    from . import audit

    form = Form.read(form_data)
    with _submit_lock:
        now = time.time() if now is None else now
        url = cloud_url(cloud)
        refusal = offline_refusal(url)
        if refusal:
            # Before anything looks at the report: one that can't leave isn't DLP-checked (docs/feedback.md).
            audit.record("feedback.refused", kind=form.kind, reason="offline")
            raise FeedbackError(refusal, code="offline")
        user_id = _account(cloud, url)[0]
        try:
            prepared = _previewed(preview_id, form, now, url, user_id) if form.diagnostics else prepare(
                form, settings=settings, provider=provider, model=model, url=url, account=user_id)
        except FeedbackError as exc:
            if exc.code == "dlp":  # DLP records which rule (dlp.finding); this records that nothing went
                audit.record("feedback.refused", kind=form.kind, reason="dlp")
            raise
        body = prepared.body
        with _lock:
            recent = _recent(now)
        if len(recent) >= MAX_RECENT:
            _record("feedback.refused", body, reason="rate_limited")
            raise FeedbackError(f"You've sent {MAX_RECENT} reports in the last {RECENT_SECONDS // 60} minutes. Wait a "
                                "few minutes before sending another, or copy this one.", code="rate_limited")
        if not url:
            outcome = _queue(body, reason="no_cloud", account=user_id, url="", now=now)
        else:
            try:
                report, attributed = _send(cloud, url, body, attribute=bool(user_id))
            except _Later as later:
                outcome = _queue(body, reason=later.reason, account=user_id, url=url, now=now,
                                 retry_after=later.retry_after)
            except FeedbackError as exc:
                _record("feedback.refused", body, reason=exc.code, **({"status": exc.status} if exc.status else {}))
                raise
            else:
                _record("feedback.sent", body, queued=False, attributed=attributed)
                outcome = Outcome("sent", "Thanks. Your feedback is in Lumi Cloud"
                                  + (f" (reference {report})." if report else "."), id=report)
        with _lock:
            try:
                _write(_recent_path(), json.dumps([*_recent(now), now]))
            except OSError:  # the report went, or waits, all the same: don't tell the person it failed
                logger.warning("Couldn't count a feedback report for the rate limit", exc_info=True)
            _previews.pop(str(preview_id or ""), None)
    if outcome.status == "queued":
        wake()
    return outcome


def copy_text(form_data: Any, *, settings: Any = None, provider: str = "", model: str = "",
              preview_id: str = "") -> str:
    """The report as text to paste somewhere else when it can't be sent; nothing is sent.

    It's what would have been sent: secrets removed and the organization's DLP
    rules applied (the previewed report, when there is one). When those rules
    don't let the report leave, there's no copy either: "".
    """
    try:
        form = Form.read(form_data)
    except FeedbackError:
        return ""
    with _lock:
        item = _previews.get(str(preview_id or ""))
    if item is not None and item.digest == form.digest():
        body = item.prepared.body
    else:
        try:
            body = prepare(form, settings=settings, provider=provider, model=model).body
        except FeedbackError:
            return ""
    app = body["app"]
    lines = [f"Lumi feedback: {KIND_LABELS[body['kind']]}", "", body["message"], ""]
    if body.get("reply_to"):
        lines.append(f"Reply to: {body['reply_to']}")
    lines.append(f"Lumi {app['version']} ({app['channel']}), {app['os']}, {app['arch']}")
    if body.get("diagnostics") is not None:
        lines += ["", "Diagnostics:", json.dumps(body["diagnostics"], indent=2, ensure_ascii=False)]
    return "\n".join(lines).rstrip() + "\n"


def status(cloud: Any) -> dict:
    """What the dialog shows before anything is sent: where reports go, as whom, and what waits."""
    url = cloud_url(cloud)
    user_id, email = _account(cloud, url)
    return {"destination": _host(url), "configured": bool(url), "account": email if user_id else "",
            "offline": offline_refusal(url), "waiting": waiting(), "app": app_info(),
            "limits": {"message": MAX_MESSAGE, "reply_to": MAX_REPLY_TO}}


# ── In the background ──────────────────────────────────────────────────────


def wake() -> None:
    """Try the waiting reports soon: one was queued, or Lumi Cloud's address or sign-in changed."""
    _wake.set()


def start_background(cloud: Any, settings: Any = None, *, first_delay: float = FIRST_TRY_SECONDS,
                     stop: threading.Event | None = None) -> threading.Thread:
    """Send waiting reports when they're due, for the app's lifetime (or until ``stop`` is set)."""
    stop = stop or threading.Event()

    def loop() -> None:
        _wake.wait(first_delay)
        while not stop.is_set():
            _wake.clear()
            try:
                if waiting():
                    flush(cloud, settings)
            except Exception:  # keep trying after one bad round
                logger.exception("Sending waiting feedback failed")
            _wake.wait(LOOP_SECONDS)

    thread = threading.Thread(target=loop, daemon=True, name="lumi-feedback")
    thread.start()
    return thread
