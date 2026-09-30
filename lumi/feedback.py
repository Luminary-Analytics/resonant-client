"""Feedback from the app to a Lumi Cloud's feedback inbox (docs/feedback.md).

Help > Send feedback… (also the command palette, About Lumi and the profile
menu) opens a dialog whose report goes to ``POST <destination>/api/v1/feedback``
as JSON::

    {"kind": "bug" | "idea" | "other", "message": "...", "reply_to": "you@example.com" | null,
     "app": {"version": "...", "channel": "...", "os": "...", "arch": "..."},
     "install_id": "...", "diagnostics": {...} | null}

with the header ``Idempotency-Key: <the report's id>`` (a UUID made when the
report is written, the same for every try) and, while the person is signed in
to that very Lumi Cloud, ``Authorization: Bearer <access token>``.

**Delivered** means Lumi Cloud acknowledged this report: 201 (new) or 200 (a
replay of the same key) whose JSON ``report`` is the key. Anything else,
such as a captive portal's page, isn't delivered and the report is kept. A
401 for the token presented is refreshed once where the token was issued
and tried again; then the report waits for the person to sign in again. 429
waits out ``Retry-After`` (at most an hour, with jitter). 400, 413 and 404
(this Lumi Cloud doesn't take feedback) keep the report where the person
sees it, with Copy and Discard.

**A report written with the account goes only with that account.** It is
sent with its writer's token for its destination, asked for at the moment
it's sent (``CloudClient.account_token(destination, user_id=...)``, which
refuses when that Lumi Cloud didn't issue the sign-in or someone else is
signed in there). Otherwise it waits for its writer, and goes without the
account only when the person chooses that (``send_without_account``).

* **Where reports go** (``destination``): ``privacy.feedback_url`` (Settings
  › Privacy & security, or locked by an organization's policy), else the
  build's own address (``BUILD_DESTINATION``: ``lumi/_build_config.py``, which
  the release build writes from LUMI_BUILD_FEEDBACK_URL; none from source),
  else the Lumi Cloud this computer uses. A report is bound to its destination
  when it's written: it never goes anywhere else, and one written with no
  destination waits until the person sends it with the destination shown
  (``send_held``), or copies it and emails it to SUPPORT_EMAIL.
* **Always sent:** the app's version, update channel, operating system and
  architecture, and an install id derived from a random secret made on
  first use (``feedback/install-id``), separately for each destination and
  for signed out or each account (``install_id``): reports sent signed out
  can't be linked to an account's, nor one Lumi Cloud's to another's. The id
  matches who sends the report at the time it's sent.
* **Diagnostics, only when the person checks them** and the organization
  allows (``privacy.feedback_diagnostics``): Python's version, the platform,
  whether this is a packaged build, the provider type and model in use (never
  a key), whether offline mode is on, and the end of Lumi's startup log,
  which can hold parts of conversations, without saved keys, secret-looking
  values or the home folder. The dialog shows the exact report first
  (``preview``), and Send sends that previewed report after checking it
  again, so what was shown is what goes.
* **The account, only while the person is signed in** to the Lumi Cloud that
  issued the sign-in (``CloudClient.account_url``) and reports go there; and
  a report written with it keeps it (see above).

**Lumi Cloud's limits.** A report is fitted, before it's shown and again
before it goes, to the limits its Lumi Cloud publishes in
``GET /api/v1/feedback/info`` (``limits``: the body and the diagnostics in
bytes, with an account and without one), never beyond this app's own
(MAX_BODY_BYTES, MAX_DIAGNOSTICS_BYTES). A Lumi Cloud that publishes none
gets those, and a report sent without an account the smaller ANONYMOUS_*
ones (``limits_for``). The log tail gives up its oldest lines first.

Before anything leaves the computer, offline mode refuses the report unless
the destination is allowed (``offline_refusal``, before anything else looks
at it; Copy then gives only what the person typed). Then ``prepare`` runs
``secret_scan`` with its patterns on over every text, the reply-to address
included, and the organization's DLP rules (``dlp.check_text``, purpose
``feedback``: the message and reply-to as ``prompt``, diagnostics as
``mixed``). A draft shown while the person types is checked with the rules on
this computer only; the organization's DLP service sees a report only when
the person sends it, and a report it changes is shown again before it goes.
The check at Send runs again on the report as it was before any DLP rule
touched it (``Prepared.source``), and compares, so a redaction's own marker
never counts as a change. A report kept to send later is checked again when
it goes only if the rules changed meanwhile (``_dlp_rules``), and then on
what was reviewed, so it never carries more than the person saw. An
organization can turn feedback off (``privacy.feedback``).

Sending uses ``net.client_options`` (proxy, certificates, offline mode). A
report that can't go now waits in ``feedback/queue.json`` (at most MAX_QUEUED
reports and MAX_QUEUE_BYTES), under a lock every Lumi process on this
computer takes, waited for at most ``file_lock.LOCK_SECONDS`` (then the
action fails with ``busy``, rather than touching the files unlocked);
``flush``, run by the thread ``start_background`` starts, sends it later. A person sends at most MAX_RECENT reports in RECENT_SECONDS.
The audit log records ``feedback.sent``, ``feedback.queued``,
``feedback.held``, ``feedback.dropped`` and ``feedback.refused`` with the kind
and size, never the text.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import platform
import random
import re
import secrets
import sys
import threading
import time
import unicodedata
import uuid
from collections import OrderedDict
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .file_lock import LOCK_SECONDS, LockTimeout, exclusive
from .paths import state_home

logger = logging.getLogger(__name__)

ENDPOINT = "/api/v1/feedback"
INFO_ENDPOINT = "/api/v1/feedback/info"
FEATURE = "sending feedback"
PURPOSE = "feedback"
KINDS = ("bug", "idea", "other")
KIND_LABELS = {"bug": "Bug", "idea": "Idea", "other": "Other"}
# Luminary Analytics support, where the dialog suggests emailing a report no address takes: the
# address Lumi's terms give for notices and support (lumi/legal/terms.json's notices_email).
SUPPORT_EMAIL = "rich.bellantoni@luminaryanalytics.com"


def _build_destination() -> str:
    """This build's own feedback address: ``lumi/_build_config.py``'s FEEDBACK_URL, or "".

    The release builds write that module (packaging/build_config.py) from
    LUMI_BUILD_FEEDBACK_URL, which release.yml takes from the repository
    variable LUMI_FEEDBACK_URL, so setting the address needs no code change.
    A source checkout has no such module. ``destination`` checks the address
    again before any report goes there.
    """
    try:
        from . import _build_config  # type: ignore[attr-defined]  # written by the build, never committed
    except ImportError:
        return ""
    return str(getattr(_build_config, "FEEDBACK_URL", "") or "").strip()


# Used when neither Settings nor the organization names an address (privacy.feedback_url).
BUILD_DESTINATION = _build_destination()

# What the dialog takes.
MAX_MESSAGE = 5000
MAX_REPLY_TO = 254
# What Lumi Cloud takes, enforced here before anything is sent.
MAX_SENT_MESSAGE = 8000                  # redaction markers can make a message longer than what was typed
MAX_BODY_BYTES = 64 * 1024               # the request body, UTF-8
MAX_DIAGNOSTICS_BYTES = 32 * 1024        # the diagnostics object as JSON (Python's default separators), UTF-8
# Without an account, when a Lumi Cloud doesn't publish its limits: what Lumi Cloud takes from anyone.
ANONYMOUS_BODY_BYTES = 48 * 1024
ANONYMOUS_DIAGNOSTICS_BYTES = 16 * 1024
MIN_PUBLISHED_BYTES = 4 * 1024           # a published limit smaller than this is taken as a mistake, not obeyed
MAX_SERVER_DIAGNOSTIC_TEXT = 16000       # characters in one diagnostic text
LOG_TAIL_LINES = 60
LOG_TAIL_CHARS = 6000
MAX_DIAGNOSTIC_TEXT = 12000              # after redaction markers; its end is kept
_LOG_READ_BYTES = 64 * 1024
MAX_QUEUED = 20
MAX_QUEUE_BYTES = 512 * 1024
QUEUE_DAYS = 30
MAX_AGE_STEP = 600.0                     # the most one round adds to a waiting report's age (see _age)
MAX_RECENT, RECENT_SECONDS = 5, 600
PREVIEW_SECONDS = 900
MAX_PREVIEWS = 20
FIRST_TRY_SECONDS = 60.0
LOOP_SECONDS = 60.0
RETRY_SECONDS = 600.0
MAX_BACKOFF_SECONDS = 6 * 3600.0
MAX_RETRY_AFTER = 3600                   # the contract's most; a longer one is clamped
TIMEOUT_SECONDS = 20.0
INFO_SECONDS = 600.0
MAX_OPERATOR = 80

SWITCH = "feedback"                      # privacy.feedback: "on" | "off"
DIAGNOSTICS = "feedback_diagnostics"     # privacy.feedback_diagnostics: "allowed" | "never"
ADDRESS = "feedback_url"                 # privacy.feedback_url: "" or a Lumi Cloud address

# Reply-to addresses: a common subset of RFC 5322 in ASCII, one @, and a domain
# of dot-separated labels ending in letters. Lumi Cloud checks the same rule,
# and nothing in it (such as ? or &) can add to a mailto: link.
EMAIL = re.compile(
    r"(?=.{3,254}$)(?!\.)(?!.*\.\.)[A-Za-z0-9._%+'-]{1,64}(?<!\.)@"
    r"(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+[A-Za-z]{2,63}")
INSTALL_ID = re.compile(r"[A-Za-z0-9_-]{16,64}")
REPORT_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}")
_SERVER_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")
_RETRY_AFTER = re.compile(r"[0-9]{1,6}")
# Bidirectional embeddings, overrides and isolates: they can make text read
# differently than it's stored. (Left-to-right and right-to-left marks stay.)
_BIDI_CONTROLS = frozenset({"LRE", "RLE", "PDF", "LRO", "RLO", "LRI", "RLI", "FSI", "PDI"})
# A sign-in that ended: the report goes as the person is at that moment, signed out.
_SIGNED_OUT = frozenset({"signed_out", "invalid_grant"})

# Queue states: "waiting" goes by itself when it's due; "sign_in" when the person signs in again (or Send now);
# "held" only when the person acts (send it to the destination shown, Copy, Discard).
WAITING, SIGN_IN, HELD = "waiting", "sign_in", "held"
_HELD_WHY = {
    "no_destination": "Written before a feedback address was set. It goes only when you send it to the address shown.",
    "not_accepting": "This Lumi Cloud doesn't accept feedback.",
    "too_large": "It's too large for this Lumi Cloud.",
    "refused": "This Lumi Cloud couldn't take it.",
    "dlp": "Your organization's data loss prevention rules now keep it on this computer.",
    "expired": f"It waited {QUEUE_DAYS} days and isn't sent any more.",
}

_lock = threading.RLock()          # the previews and what destinations said about themselves
_files = threading.RLock()         # this process's side of the files in feedback/, and what's being sent
_submit_lock = threading.Lock()    # one report at a time from this app
_flush_lock = threading.Lock()     # one round of sending at a time
_wake = threading.Event()
_transport: Any = None             # tests install an httpx.MockTransport
_monotonic = time.monotonic        # tests turn the clock
_run = uuid.uuid4().hex            # this process: backoff deadlines are monotonic within it
_previews: OrderedDict[str, _Preview] = OrderedDict()
_sending: set[str] = set()         # waiting reports this process is sending now; Discard leaves them
_info: dict[str, tuple[float, dict]] = {}


class FeedbackError(Exception):
    """A report that can't be sent now; ``message`` is for the person, ``code`` says why.

    Codes: ``invalid`` (a field, named by ``field``), ``preview`` (review the
    report again), ``review`` (checked again at Send, it changed: ``preview``
    holds the new one to review), ``dlp``, ``offline``, ``disabled`` and
    ``diagnostics_off`` (the organization's switches), ``rate_limited``,
    ``queue_full``, ``busy`` (another Lumi process is using the reports on
    this computer), ``gone`` (a waiting report isn't there any more), and
    Lumi Cloud's own: ``not_accepting`` (404),
    ``too_large`` (413) and ``refused`` (``status`` is its answer). ``copy`` is
    the report as text to offer instead, "" for none.
    """

    def __init__(self, message: str, *, code: str, field: str = "", status: int = 0, copy: str = "",
                 preview: dict | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.field = field
        self.status = status
        self.copy = copy
        self.preview = preview

    def as_dict(self) -> dict:
        return {"code": self.code, "message": self.message, "field": self.field}


class _Later(Exception):
    """Lumi Cloud couldn't take the report now; keep it and try again."""

    def __init__(self, reason: str, retry_after: float | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.retry_after = retry_after


@dataclass
class Outcome:
    """What happened to a report the person sent: ``sent`` (with Lumi Cloud's id) or ``queued``.

    ``notices`` says what the checks changed (secrets removed, a redaction, a
    reply-to address left out), for the dialog to show with the outcome. A
    report kept because no feedback address is set carries ``copy``, the
    report as text, and ``support_email``, where it can be emailed instead.
    """

    status: str
    message: str
    id: str = ""
    reason: str = ""
    notices: list[str] = field(default_factory=list)
    copy: str = ""
    support_email: str = ""

    def as_dict(self) -> dict:
        return {"status": self.status, "message": self.message, "id": self.id, "reason": self.reason,
                "notices": list(self.notices), "copy_text": self.copy, "support_email": self.support_email}


def set_transport_for_tests(transport: Any) -> None:
    """Send through ``transport`` (an ``httpx.MockTransport``) instead of the network; None undoes it."""
    global _transport
    _transport = transport


def set_clock_for_tests(clock: Any = None, *, run: str = "") -> None:
    """Use ``clock`` for monotonic time (None: the real one), and ``run`` as this process's id."""
    global _monotonic, _run
    _monotonic = clock or time.monotonic
    _run = run or uuid.uuid4().hex


def reset_for_tests() -> None:
    set_transport_for_tests(None)
    set_clock_for_tests(None)
    with _lock:
        _previews.clear()
        _info.clear()
    with _files:
        _sending.clear()
    _wake.clear()


# ── The organization's switches, and where reports go ──────────────────────


def _setting(settings: Any, key: str, default: Any) -> Any:
    if settings is None:
        return default
    try:
        return settings.get("privacy", key, default)
    except Exception:  # an unreadable setting is the default
        return default


def _locked(settings: Any, key: str) -> bool:
    try:
        return key in (settings.locked_values("privacy") if settings is not None else {})
    except Exception:
        return False


def validate_policy_settings(settings: dict) -> None:
    """ValueError for a feedback setting an organization's policy locks to something Lumi can't use."""
    if f"privacy.{SWITCH}" in settings and settings[f"privacy.{SWITCH}"] not in ("on", "off"):
        raise ValueError(f"'privacy.{SWITCH}' must be \"on\" or \"off\".")
    if f"privacy.{DIAGNOSTICS}" in settings and settings[f"privacy.{DIAGNOSTICS}"] not in ("allowed", "never"):
        raise ValueError(f"'privacy.{DIAGNOSTICS}' must be \"allowed\" or \"never\".")
    if f"privacy.{ADDRESS}" in settings:
        value = settings[f"privacy.{ADDRESS}"]
        if not isinstance(value, str):
            raise ValueError(f"'privacy.{ADDRESS}' must be a Lumi Cloud address, or \"\" for none.")
        if value:
            from .cloud import CloudError, normalize_url

            try:
                normalize_url(value)
            except CloudError as exc:
                raise ValueError(f"'privacy.{ADDRESS}': {exc}") from exc


def check_address(value: Any) -> str:
    """A feedback address Settings may keep (privacy.feedback_url): "" for none, else the address, normalized.

    The rules a policy's address meets (``validate_policy_settings``): a Lumi
    Cloud address, https (http only for one on this computer), without a user
    name or password, a query or a fragment. ValueError says what's wrong.
    """
    from .cloud import CloudError, normalize_url

    if value is not None and not isinstance(value, str):
        raise ValueError("Enter a Lumi Cloud address, such as https://cloud.example.com, or leave it empty.")
    text = (value or "").strip()
    if not text:
        return ""
    if len(text) > 2048:
        raise ValueError("That address is too long. Enter the Lumi Cloud address, such as https://cloud.example.com.")
    try:
        return normalize_url(text)
    except CloudError as exc:
        raise ValueError(str(exc)) from exc


def disabled_reason(settings: Any) -> str:
    """Why feedback can't be sent at all ("" when it can): the organization, or Settings, turned it off."""
    if _setting(settings, SWITCH, "on") != "off":
        return ""
    if _locked(settings, SWITCH):
        return "Your organization turned off sending feedback from Lumi."
    return f"Sending feedback is turned off in Lumi's settings (privacy.{SWITCH})."


def diagnostics_refusal(settings: Any) -> str:
    """Why reports can't include diagnostics ("" when they can)."""
    if _setting(settings, DIAGNOSTICS, "allowed") != "never":
        return ""
    if _locked(settings, DIAGNOSTICS):
        return "Your organization doesn't allow diagnostics in feedback."
    return f"Diagnostics in feedback are turned off in Lumi's settings (privacy.{DIAGNOSTICS})."


@dataclass(frozen=True)
class Destination:
    """Where reports go now: ``url`` ("" for nowhere yet) and what chose it."""

    url: str
    source: str  # "policy", "setting", "build", "cloud" or ""


def cloud_url(cloud: Any) -> str:
    """The Lumi Cloud address this computer uses, or "" when none is set (or it isn't one Lumi uses)."""
    from .cloud import CloudError, normalize_url

    raw = str(getattr(cloud, "url", "") or "")
    if not raw:
        return ""
    try:
        return normalize_url(raw)
    except CloudError:
        return ""


def destination(cloud: Any, settings: Any = None) -> Destination:
    """Where a report written now goes: the feedback address, the build's, else this computer's Lumi Cloud."""
    from .cloud import CloudError, normalize_url

    chosen = str(_setting(settings, ADDRESS, "") or "").strip()
    if chosen:
        source = "policy" if _locked(settings, ADDRESS) else "setting"
        try:
            return Destination(normalize_url(chosen), source)
        except CloudError:
            return Destination("", source)  # an address Lumi wouldn't use counts as none
    if BUILD_DESTINATION:
        try:
            return Destination(normalize_url(BUILD_DESTINATION), "build")
        except CloudError:
            logger.warning("This build's feedback address isn't one Lumi can use")
    url = cloud_url(cloud)
    return Destination(url, "cloud" if url else "")


def _host(url: str) -> str:
    return urlsplit(url).netloc if url else ""


def offline_refusal(url: str) -> str:
    """Why offline mode keeps a report from ``url``, or "" when it may go (or offline mode is off)."""
    from . import offline

    if not offline.enabled():
        return ""
    if not url:
        return ("Offline mode: sending feedback needs a Lumi Cloud, and no feedback address is set; set one "
                "and allow it, or turn offline mode off.")
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


def _signin_marker(cloud: Any) -> str:
    """Changes whenever the person signs in again (or their account is refreshed); "" while signed out."""
    try:
        status = cloud.status()
    except Exception:
        return ""
    account = status.get("account") if isinstance(status.get("account"), dict) else {}
    if not status.get("signed_in") or not account.get("user_id"):
        return ""
    seen = f"{getattr(cloud, 'account_url', '')}|{account.get('user_id')}|{account.get('refreshed_at') or ''}"
    return hashlib.sha256(seen.encode("utf-8")).hexdigest()[:24]


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


def _switched_off(form: Form, settings: Any, *, copy: str = "") -> None:
    """FeedbackError when the organization's switches refuse ``form``."""
    reason = disabled_reason(settings)
    if reason:
        raise FeedbackError(reason, code="disabled", copy=copy)
    if form.diagnostics:
        reason = diagnostics_refusal(settings)
        if reason:
            raise FeedbackError(reason, code="diagnostics_off", field="include_diagnostics")


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


_BUSY = ("Another Lumi window or process is using the reports saved on this computer. Try again in a moment "
         "(nothing was changed).")


@contextmanager
def _state() -> Iterator[None]:
    """This process's lock, then the one every Lumi process on this computer takes, over feedback/'s files.

    Each is waited for at most LOCK_SECONDS, on every platform: a process
    stuck on a drive that stopped answering (a network share, say) ties up no
    more of Lumi's threads than its own. FeedbackError ``busy`` then, and
    nothing is read or written unlocked. Never taken twice in one thread: the
    OS lock isn't reentrant.
    """
    if not _files.acquire(timeout=LOCK_SECONDS):
        raise FeedbackError(_BUSY, code="busy")
    try:
        try:
            with exclusive(_folder() / ".lock", required=True):
                yield
        except LockTimeout as exc:
            raise FeedbackError(_BUSY, code="busy") from exc
    finally:
        _files.release()


def _write(path: Path, text: str) -> None:
    """Replace ``path`` with ``text`` in one step, waiting briefly while another process has it open."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    temporary.write_text(text, encoding="utf-8")
    for attempt in range(40):
        try:
            temporary.replace(path)
            return
        except PermissionError:  # Windows: a reader has it open for a moment
            if attempt == 39:
                temporary.unlink(missing_ok=True)
                raise
            time.sleep(0.05)


def _read_secret(path: Path) -> str:
    try:
        return path.read_text(encoding="ascii").strip()
    except (OSError, ValueError):
        return ""


def _install_secret() -> str:
    """This install's random secret, made on first use from nothing on the computer or the account.

    Made with an exclusive create, so two Lumi processes starting together agree on one.
    """
    path = _folder() / "install-id"
    value = _read_secret(path)
    if INSTALL_ID.fullmatch(value):
        return value
    with _state():
        value = _read_secret(path)
        if INSTALL_ID.fullmatch(value):
            return value
        value = secrets.token_urlsafe(32)
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with open(path, "x", encoding="ascii") as handle:
                handle.write(value)
        except FileExistsError:
            existing = _read_secret(path)
            if INSTALL_ID.fullmatch(existing):
                return existing
            _write(path, value)  # damaged: replaced, never sent
        return value


def install_id(url: str = "", account: str = "") -> str:
    """The install id a report carries: one for each destination (``url``), and for signed out or each account.

    Derived from the install's secret with HMAC-SHA256, so staff can tell that
    reports came from one install, while an id seen with an account's reports
    never matches the id of the same install's reports sent signed out.

    Each try carries the id for the account it goes with, worked out when
    it goes. Lumi Cloud recognizes a report sent again by its
    ``Idempotency-Key`` alone (a random UUID made when the report is
    written), whatever install id a try carries, and keeps the first report
    with that key. So the person's own choice to send a report without their
    account (``send_without_account``) carries the signed-out id, and Lumi
    Cloud can't link it to the account's reports; if an earlier try with the
    account did arrive (its answer lost on the way back), Lumi Cloud keeps
    that one and answers with its id, and no second copy is made.
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
    exists in packaged builds; running from source, this is empty. It can hold
    parts of conversations (warnings quote model output), so DLP checks it as
    mixed content.
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
    """A report as it will be sent, and what the person should know about it.

    ``source`` is the report after the secret scan and before the DLP rules:
    what the check at Send runs on again, so a redaction's own marker is
    never checked (a rule named after its keyword would match its marker).
    It stays in memory; the queue keeps only what is sent.
    """

    body: dict
    notices: list[str] = field(default_factory=list)
    source: dict = field(default_factory=dict)


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


def _encode(body: dict) -> bytes:
    return json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _size(body: dict) -> int:
    return len(_encode(body))


def _diagnostics_bytes(value: dict) -> int:
    # As Lumi Cloud counts them: json.dumps with its default separators.
    return len(json.dumps(value, ensure_ascii=False).encode("utf-8"))


def _shorter(text: str) -> str:
    """A log tail without its first line (one line: without its first half)."""
    _head, newline, rest = text.partition("\n")
    return rest if newline else text[len(text) // 2:]


@dataclass(frozen=True)
class Limits:
    """The most a report may be: its body, and its diagnostics, in bytes (as Lumi Cloud counts them)."""

    body: int = MAX_BODY_BYTES
    diagnostics: int = MAX_DIAGNOSTICS_BYTES


ANONYMOUS = Limits(ANONYMOUS_BODY_BYTES, ANONYMOUS_DIAGNOSTICS_BYTES)


def _published(answer: Any) -> dict | None:
    """The limits a Lumi Cloud's ``/info`` publishes, within this app's own; None when it names none (or badly)."""
    raw = answer.get("limits") if isinstance(answer, dict) else None
    if not isinstance(raw, dict):
        return None
    ceilings = {"body_bytes": MAX_BODY_BYTES, "diagnostics_bytes": MAX_DIAGNOSTICS_BYTES,
                "anonymous_body_bytes": MAX_BODY_BYTES, "anonymous_diagnostics_bytes": MAX_DIAGNOSTICS_BYTES}
    limits = {}
    for name, ceiling in ceilings.items():
        value = raw.get(name)
        if isinstance(value, bool) or not isinstance(value, int) or value < MIN_PUBLISHED_BYTES:
            return None
        limits[name] = min(value, ceiling)
    return limits


def limits_for(url: str, account: str) -> Limits:
    """What a report to ``url`` sent as ``account`` ("" without an account) is fitted to.

    The limits ``url`` published when it was last asked (``info``), else this
    app's own, and for a report without an account the smaller ANONYMOUS ones.
    """
    with _lock:
        cached = _info.get(url)
    published = cached[1].get("limits") if cached else None
    if published:
        if account:
            return Limits(published["body_bytes"], published["diagnostics_bytes"])
        return Limits(published["anonymous_body_bytes"], published["anonymous_diagnostics_bytes"])
    return Limits() if account else ANONYMOUS


def _fit(body: dict, limits: Limits = Limits()) -> dict:
    """``body`` within ``limits``: the log tail loses its oldest lines as needed.

    FeedbackError ``invalid`` when the report is too large even so.
    """
    value = body.get("diagnostics")
    if isinstance(value, dict):
        value = {name: (text[-MAX_SERVER_DIAGNOSTIC_TEXT:] if isinstance(text, str) else text)
                 for name, text in value.items()}
        while _diagnostics_bytes(value) > limits.diagnostics and value.get("log_tail"):
            value["log_tail"] = _shorter(value["log_tail"])
        body = {**body, "diagnostics": value}
        while _size(body) > limits.body and value.get("log_tail"):
            value = {**value, "log_tail": _shorter(value["log_tail"])}
            body = {**body, "diagnostics": value}
        if _diagnostics_bytes(value) > limits.diagnostics:
            raise FeedbackError("The diagnostics are too large to send. Leave them out.", code="invalid",
                                field="include_diagnostics")
    if _size(body) > limits.body:
        raise FeedbackError("The report is too large to send. Shorten the message, or leave diagnostics out.",
                            code="invalid", field="message")
    return body


def _scan_secrets(body: dict, settings: Any) -> tuple[dict, list[str]]:
    """``body`` with saved keys and secret-looking values taken out of every text (patterns on); what it says.

    Running it again changes nothing: its markers never look like secrets.
    A reply-to address it would change is left out.
    """
    from collections import Counter

    found: Counter = Counter()
    known = _known(settings)
    notices = []
    reply_to = body.get("reply_to")
    if reply_to and _scan(reply_to, known, Counter()) != reply_to:
        reply_to = None
        notices.append("The reply-to address looked like a secret, so it was left out.")
    scanned = {**body, "message": _scan(body["message"], known, found), "reply_to": reply_to,
               "diagnostics": _map_strings(body.get("diagnostics"), lambda text: _scan(text, known, found))}
    if found:
        total = sum(found.values())
        kinds = ", ".join(kind for kind, _ in found.most_common())
        notices.insert(0, f"Removed {total} secret{'' if total == 1 else 's'} ({kinds}) from the report.")
    return scanned, notices


def _checked(body: dict, settings: Any, *, service: bool = True, limits: Limits = Limits()) -> Prepared:
    """``body`` after the secret scan (patterns on), the organization's DLP rules and Lumi Cloud's ``limits``.

    DLP's block refuses the report (FeedbackError ``dlp``); its redactions
    apply to what is sent. A reply-to address either would change is left
    out. ``service=False`` applies only the rules on this computer, never the
    organization's DLP service (a draft the person hasn't chosen to send).
    The result's ``source`` is the report between the two.
    """
    from . import dlp

    scanned, notices = _scan_secrets(body, settings)
    reply_to = scanned.get("reply_to")

    def check(text: str, kind: str) -> str:
        return dlp.check_text(text, purpose=PURPOSE, kind=kind, provider="lumi-cloud", service=service)

    try:
        message = check(scanned["message"], "prompt")
        if reply_to and check(reply_to, "prompt") != reply_to:
            reply_to = None
            notices.append("Your organization's data loss prevention rules don't let the reply-to address leave "
                           "this computer, so it was left out.")
        # A log can quote the model and tool output: every rule checks it, whatever its scope.
        diagnostics_value = _map_strings(scanned.get("diagnostics"), lambda text: check(text, dlp.MIXED))
    except dlp.Blocked as exc:
        raise FeedbackError(exc.message, code="dlp") from exc
    if len(message) > MAX_SENT_MESSAGE:
        # Redaction markers can be longer than what they replace; Lumi Cloud takes up to MAX_SENT_MESSAGE.
        raise FeedbackError("With secrets and your organization's matches replaced, the message is too long to "
                            "send. Shorten it.", code="invalid", field="message")
    # The same for a diagnostic text, which the person didn't write: its end is kept.
    diagnostics_value = _map_strings(diagnostics_value, lambda text: text[-MAX_DIAGNOSTIC_TEXT:])
    if message != scanned["message"] or diagnostics_value != scanned.get("diagnostics"):
        notices.append("Your organization's data loss prevention rules redacted part of the report.")
    fitted = _fit({**scanned, "message": message, "reply_to": reply_to, "diagnostics": diagnostics_value}, limits)
    if isinstance(fitted.get("diagnostics"), dict) and fitted["diagnostics"] != diagnostics_value:
        notices.append("The oldest lines of the log were left out to keep the report within what Lumi Cloud takes.")
    return Prepared(fitted, notices, source=scanned)


def _dlp_rules() -> str:
    """What the DLP check would do to a report now, as a fingerprint: the rules (and service) in force, or why
    the policy can't be used. A report kept to send later is checked again only when this changed."""
    from . import dlp
    from .policy import blocked_reason, current

    refusal = blocked_reason()
    if refusal:
        return "blocked:" + hashlib.sha256(refusal.encode("utf-8")).hexdigest()[:24]
    if dlp.active() is None:
        return "none"
    policy = current()
    section = policy.raw.get("dlp") if policy is not None else None
    return hashlib.sha256(json.dumps(section, sort_keys=True, ensure_ascii=False, default=str)
                          .encode("utf-8")).hexdigest()[:32]


def prepare(form: Form, *, settings: Any = None, provider: str = "", model: str = "", url: str = "",
            account: str = "", service: bool = True) -> Prepared:
    """The report ``form`` makes, exactly as it would be sent to ``url`` as ``account`` ("" signed out)."""
    body = {
        "kind": form.kind,
        "message": form.message,
        "reply_to": form.reply_to,
        "app": app_info(),
        "install_id": install_id(url, account),
        "diagnostics": diagnostics(settings, provider=provider, model=model) if form.diagnostics else None,
    }
    return _checked(body, settings, service=service, limits=limits_for(url, account))


# ── Copies ─────────────────────────────────────────────────────────────────


def _copy_from_body(body: dict) -> str:
    app = body.get("app") or {}
    lines = [f"Lumi feedback: {KIND_LABELS.get(body.get('kind'), 'Other')}", "", str(body.get("message") or ""), ""]
    if body.get("reply_to"):
        lines.append(f"Reply to: {body['reply_to']}")
    if app.get("version"):
        lines.append(f"Lumi {app['version']} ({app.get('channel')}), {app.get('os')}, {app.get('arch')}")
    if body.get("diagnostics") is not None:
        lines += ["", "Diagnostics:", json.dumps(body["diagnostics"], indent=2, ensure_ascii=False)]
    return "\n".join(lines).rstrip() + "\n"


def typed_copy(form_data: Any, settings: Any = None) -> str:
    """Only what the person typed, as text to paste elsewhere: no diagnostics, and no DLP check.

    For a report that couldn't be prepared at all (offline mode refuses it
    before anything looks at it). Saved keys and secret-looking values are
    still taken out, on this computer. "" for a form that isn't filled in.
    """
    from collections import Counter

    try:
        form = Form.read({**(form_data if isinstance(form_data, dict) else {}), "include_diagnostics": False})
    except FeedbackError:
        return ""
    known = _known(settings)
    lines = [f"Lumi feedback: {KIND_LABELS[form.kind]}", "", _scan(form.message, known, Counter()), ""]
    if form.reply_to and _scan(form.reply_to, known, Counter()) == form.reply_to:
        lines.append(f"Reply to: {form.reply_to}")
    return "\n".join(lines).rstrip() + "\n"


# ── Lumi Cloud ─────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class _Delivered:
    server_id: str
    attributed: bool


def _retry_after(response: Any) -> float | None:
    """Lumi Cloud's Retry-After, in ASCII digits only, clamped to an hour, with jitter; None without one."""
    value = str(response.headers.get("retry-after") or "").strip()
    if not _RETRY_AFTER.fullmatch(value):
        return None
    wait = float(min(max(int(value), 1), MAX_RETRY_AFTER))
    return wait + random.uniform(0, min(60.0, wait / 5))


def _refusal(status: int, response: Any) -> str:
    try:
        data = response.json()
    except ValueError:
        data = {}
    detail = _clip(data.get("error_description"), 300) if isinstance(data, dict) else ""
    if status == 400 and detail:
        return f"Lumi Cloud couldn't take the report: {detail}"
    return f"Lumi Cloud couldn't take the report (it answered {status})."


def _send(cloud: Any, url: str, body: dict, report_id: str, *, account: str = "") -> _Delivered:
    """Post one report to ``url``; Lumi Cloud's id for it, and whether it went with the account.

    ``account`` ("" for none) is the person the report goes as: their token
    for ``url`` is asked for now, together with the check that ``url``
    issued it and that they are the one signed in there. When they aren't,
    it waits for them (``_Later("sign_in")``): never sent without the account
    or as someone else. ``_Later`` to keep it, FeedbackError when Lumi Cloud
    won't take it.
    """
    import httpx

    from . import __version__, offline
    from .cloud import CloudError
    from .net import client_options

    token = ""
    if account:
        try:
            token = cloud.account_token(url, user_id=account)
        except CloudError as exc:
            # Its writer isn't signed in to that Lumi Cloud now (or the token can't be had): kept for them.
            raise _Later("sign_in" if exc.code in _SIGNED_OUT else "unreachable") from exc
    body = {**body, "install_id": install_id(url, account)}
    headers = {"Content-Type": "application/json", "Accept": "application/json", "Idempotency-Key": report_id,
               "User-Agent": f"Lumi/{__version__} ({platform.system()})"}
    for attempt in (1, 2):
        if token:
            headers["Authorization"] = f"Bearer {token}"
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
        if response.status_code == 401 and token and attempt == 1:
            # The token presented was refused: refresh it once where it was issued, and try again.
            try:
                token = cloud.account_token(url, user_id=account, refused=token)
            except CloudError as exc:
                raise _Later("sign_in") from exc
            continue
        break
    status = response.status_code
    if status in (200, 201):
        try:
            answer = response.json()
        except ValueError:
            answer = None
        if isinstance(answer, dict) and answer.get("report") == report_id:
            server_id = str(answer.get("id") or "")
            return _Delivered(server_id if _SERVER_ID.fullmatch(server_id) else "",
                              attributed=bool(token) and answer.get("account") is True)
        raise _Later("unconfirmed")  # a page that isn't Lumi Cloud's acknowledgment (a captive portal's, say)
    if status == 401 and token:
        raise _Later("sign_in")  # refused even after a refresh: the person signs in again; never sent anonymously
    if status == 429:
        raise _Later("busy", _retry_after(response))
    if status >= 500 or status == 408:
        raise _Later("unreachable")
    if status in (401, 403):
        raise _Later("unauthorized")  # the contract never asks for a sign-in: tried again after the person signs in
    if status < 400:
        raise _Later("unconfirmed")  # 202, 204, a redirect: not Lumi Cloud's acknowledgment
    if status == 404:
        raise FeedbackError("This Lumi Cloud doesn't accept feedback.", code="not_accepting", status=404)
    if status == 413:
        raise FeedbackError("The report is too large for this Lumi Cloud.", code="too_large", status=413)
    raise FeedbackError(_refusal(status, response), code="refused", status=status)


def _record(event: str, body: dict, **extra: Any) -> None:
    """An audit record: the kind and size, never the text."""
    from . import audit

    audit.record(event, kind=str(body.get("kind") or ""), size=_size(body),
                 diagnostics=body.get("diagnostics") is not None, **extra)


def info(cloud: Any, settings: Any = None) -> dict:
    """What the destination says about itself (``GET /api/v1/feedback/info``), or {} when it can't be asked.

    Asked only when there is a destination, offline mode allows it and the
    organization hasn't turned feedback off; kept for INFO_SECONDS. The
    answer: ``accepting`` and ``operator``, the name of who reads the reports,
    and ``limits``, what reports to it are fitted to (None when it publishes
    none: see ``limits_for``).
    """
    target = destination(cloud, settings).url
    if not target or disabled_reason(settings) or offline_refusal(target):
        return {}
    return _asked(target)


def _asked(target: str) -> dict:
    """``target``'s answer to ``GET /api/v1/feedback/info``, kept for INFO_SECONDS; {} when it can't be asked."""
    import httpx

    from .net import client_options

    with _lock:
        cached = _info.get(target)
    if cached and _monotonic() - cached[0] < INFO_SECONDS:
        return dict(cached[1])
    try:
        with httpx.Client(**client_options(timeout=5.0, transport=_transport, feature=FEATURE)) as http:
            response = http.get(target + INFO_ENDPOINT, headers={"Accept": "application/json"})
        answer = response.json() if response.status_code == 200 else None
    except (httpx.HTTPError, ValueError):
        answer = None
    if not isinstance(answer, dict):
        return {}
    operator = " ".join(clean_text(answer.get("operator") if isinstance(answer.get("operator"), str) else "")
                        .split())[:MAX_OPERATOR]
    result = {"destination": _host(target), "accepting": answer.get("accepting") is not False, "operator": operator,
              "limits": _published(answer)}
    with _lock:
        _info[target] = (_monotonic(), result)
    return dict(result)


# ── The queue ──────────────────────────────────────────────────────────────

_QUEUED = {
    "unreachable": "Lumi Cloud couldn't be reached, so your feedback is saved on this computer. Lumi will send "
                   "it when it can.",
    "busy": "Lumi Cloud is busy, so your feedback is saved on this computer. Lumi will send it a little later.",
    "unconfirmed": "Lumi Cloud didn't confirm it got the report (another page answered), so it's saved on this "
                   "computer. Lumi will try again.",
    "unauthorized": "Lumi Cloud didn't accept the report just now, so it's saved on this computer. Lumi tries "
                    "again after you sign in.",
    "sign_in": "Lumi Cloud didn't accept your sign-in, so your feedback is saved on this computer. Sign in again "
               "in Settings > Lumi account, and Lumi sends it with your account (or send it without your account "
               "from Send feedback).",
    "no_destination": "Saved on this computer. No feedback address is set yet, so it isn't sent: once one is, "
                      "Send feedback shows where it would go, and it goes only when you send it there.",
}
_WHY_QUEUED = {"no_destination": "No feedback address is set", "unreachable": "Lumi Cloud couldn't be reached",
               "busy": "Lumi Cloud is busy", "unconfirmed": "Lumi Cloud didn't confirm the report",
               "unauthorized": "Lumi Cloud didn't accept the report", "sign_in": "Lumi Cloud didn't accept your "
               "sign-in"}


def _queue_path() -> Path:
    return _folder() / "queue.json"


def _valid(item: Any) -> bool:
    """Whether a queue entry holds a report Lumi can send (a damaged one is dropped, never sent)."""
    if not isinstance(item, dict) or not isinstance(item.get("id"), str) or not REPORT_ID.fullmatch(item["id"]):
        return False
    body = item.get("body")
    if not isinstance(body, dict) or body.get("kind") not in KINDS or not isinstance(body.get("message"), str):
        return False
    if item.get("state") not in (WAITING, SIGN_IN, HELD) or not isinstance(item.get("url"), str):
        return False
    for name in ("queued_at", "age", "aged_at"):
        if not isinstance(item.get(name), int | float) or isinstance(item.get(name), bool):
            return False
    return True


def _load_queue() -> list[dict]:
    """The waiting reports (under ``_state``); damaged entries are dropped with a record, never stop the rest."""
    try:
        data = json.loads(_queue_path().read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    except (OSError, ValueError):
        logger.warning("The waiting feedback couldn't be read; it's set aside", exc_info=True)
        _set_aside()
        return []
    items = data.get("items") if isinstance(data, dict) else None
    if not isinstance(items, list):
        _set_aside()
        return []
    kept = [item for item in items if _valid(item)]
    if len(kept) != len(items):
        from . import audit

        for _ in range(len(items) - len(kept)):
            audit.record("feedback.dropped", reason="damaged")
        _save_queue(kept)
    return kept


def _set_aside() -> None:
    path = _queue_path()
    try:
        path.replace(path.with_name(f"queue.damaged.{int(time.time())}.json"))
    except OSError:
        logger.warning("Couldn't set the damaged feedback queue aside", exc_info=True)


def _save_queue(items: list[dict]) -> None:
    path = _queue_path()
    if not items:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            logger.warning("Couldn't delete the sent feedback's queue file", exc_info=True)
        return
    _write(path, json.dumps({"version": 2, "items": items}, ensure_ascii=False))


def waiting() -> int:
    """How many reports wait on this computer."""
    with _state():
        return len(_load_queue())


def _backoff(item: dict, retry_after: float | None, attempts: int) -> dict:
    # A failed try waits RETRY_SECONDS, doubling each time up to MAX_BACKOFF_SECONDS, or Lumi Cloud's Retry-After.
    wait = retry_after or min(RETRY_SECONDS * 2 ** max(attempts - 1, 0), MAX_BACKOFF_SECONDS)
    return {"run": _run, "next_mono": _monotonic() + wait}


def _keep(report_id: str, body: dict, *, state: str, reason: str, account: str, url: str, now: float,
          retry_after: float | None = None, marker: str = "", email: str = "", rules: str = "") -> Outcome:
    """Save a report on this computer; FeedbackError ``queue_full`` when there's no room.

    ``account`` is its writer's (``email`` for showing whose), and it goes only
    with that account; ``rules`` the DLP rules it was checked with.
    """
    size = _size(body)
    with _state():
        items = _load_queue()
        full = len(items) >= MAX_QUEUED or sum(int(item.get("size") or 0) for item in items) + size > MAX_QUEUE_BYTES
        if not full:
            # ``url`` is where it was written for: it goes there and nowhere else ("": nowhere until the person
            # sends it to a destination they see).
            item = {"id": report_id, "body": body, "size": size, "url": url, "account": account,
                    "email": email if account else "", "rules": rules, "state": state,
                    "reason": reason, "detail": "", "queued_at": now, "age": 0.0, "aged_at": now, "attempts": 1,
                    "marker": marker, **_backoff({}, retry_after, 1)}
            items.append(item)
            _save_queue(items)
    if full:
        _record("feedback.refused", body, reason="queue_full")
        count = len(items)
        raise FeedbackError(f"{_WHY_QUEUED.get(reason, 'It can’t be sent now')}, and {count} report"
                            f"{'' if count == 1 else 's'} {'is' if count == 1 else 'are'} already waiting on this "
                            "computer, so this one wasn't saved. Copy it instead, or discard the waiting ones.",
                            code="queue_full", copy=_copy_from_body(body))
    _record("feedback.held" if state == HELD else "feedback.queued", body, reason=reason)
    return Outcome("queued", _QUEUED[reason], reason=reason)


def _update(report_id: str, change: dict | None) -> None:
    """Apply one report's outcome to the queue file (``None`` removes it); under ``_state``."""
    items = _load_queue()
    kept = []
    for item in items:
        if item["id"] != report_id:
            kept.append(item)
        elif change is not None:
            kept.append({**item, **change})
    _save_queue(kept)


def discard(ids: Any = None) -> int:
    """Delete waiting reports (all, or those in ``ids``); how many. One being sent right now can't be taken back."""
    wanted = {str(value) for value in ids} if ids else None
    with _state():
        items = _load_queue()
        removed = [item for item in items if item["id"] not in _sending
                   and (wanted is None or item["id"] in wanted)]
        _save_queue([item for item in items if item not in removed])
    for item in removed:
        _record("feedback.dropped", item["body"], reason="discarded")
    return len(removed)


def held_copy(report_id: str) -> str:
    """A waiting report as text to paste elsewhere; "" when there's none, or DLP keeps it here."""
    with _state():
        item = next((entry for entry in _load_queue() if entry["id"] == str(report_id)), None)
    if item is None or item.get("reason") == "dlp":
        return ""
    return _copy_from_body(item["body"])


def _age(items: list[dict], now: float) -> list[dict]:
    """Count the time each report has waited while Lumi ran, and hold those past QUEUE_DAYS.

    Each round adds the wall-clock time since the report was last looked at,
    at most MAX_AGE_STEP: moving the clock forward (or a computer asleep)
    adds little, and moving it back adds nothing, so it never wipes the queue.
    Expired reports are held, not deleted: the person copies or discards them.
    """
    changed = []
    for item in items:
        delta = now - float(item["aged_at"])
        item["age"] = float(item["age"]) + min(max(delta, 0.0), MAX_AGE_STEP)
        item["aged_at"] = now
        if item["state"] != HELD and item["age"] > QUEUE_DAYS * 86400:
            item.update(state=HELD, reason="expired", detail="")
            changed.append(item)
    for item in changed:
        _record("feedback.held", item["body"], reason="expired")
    return items


def flush(cloud: Any, settings: Any = None, *, force: bool = False, now: float | None = None,
          only: Any = None) -> dict:
    """Send the waiting reports that are due (``force``: without waiting out backoff); counts of what happened.

    A report goes only to the destination it was written for, is checked again
    first (secret scan and DLP, since the rules may have changed), goes with
    the account only when the person who wrote it is still the one signed in
    there, and is held for the person when Lumi Cloud or DLP won't take it.
    The first report Lumi Cloud can't take now ends the round, so an
    unreachable server isn't asked once per report. ``force`` (Send now)
    doesn't wait out backoff or a sign-in, but does wait out Retry-After.
    ``only`` limits the round to those report ids.
    """
    if not _flush_lock.acquire(blocking=False):
        return {"sent": 0, "held": 0, "waiting": waiting(), "busy": True}
    try:
        return _flush(cloud, settings, force=force, now=time.time() if now is None else now,
                      only={str(value) for value in only} if only else None)
    finally:
        _flush_lock.release()


def _due(item: dict, url: str, marker: str, force: bool) -> bool:
    from .cloud import same_address

    if not item["url"] or not same_address(item["url"], url):
        return False  # written for another destination (or none): it goes nowhere else
    if item["state"] == HELD:
        return force and item.get("reason") == "not_accepting"  # this Lumi Cloud may take feedback now
    if item["state"] == SIGN_IN:
        return force or bool(marker and marker != item.get("marker"))
    if item.get("run") != _run:
        return True  # another run's backoff: tried at this run's first round
    if _monotonic() >= float(item.get("next_mono") or 0):
        return True
    return force and item.get("reason") != "busy"


def _flush(cloud: Any, settings: Any, *, force: bool, now: float, only: set | None) -> dict:
    with _state():
        items = _age(_load_queue(), now)
        _save_queue(items)
    result = {"sent": 0, "held": 0, "waiting": len(items)}
    if not items:
        return result
    reason = disabled_reason(settings)
    if reason:
        return {**result, "disabled": reason}
    url = destination(cloud, settings).url
    if not url or offline_refusal(url):
        return result
    marker = _signin_marker(cloud)
    rules = _dlp_rules()
    _asked(url)  # the limits it publishes, fresh: each report is fitted to them again before it goes
    for item in items:
        if only is not None and item["id"] not in only:
            continue
        if not _due(item, url, marker, force):
            continue
        with _state():
            if not any(entry["id"] == item["id"] for entry in _load_queue()):
                continue  # discarded meanwhile
            _sending.add(item["id"])
        change: dict | None = {}
        stop = False
        body = item["body"]
        try:
            # Fitted to the limits for the account it goes with: without one (the person's choice included,
            # send_without_account) the smaller anonymous limits, so its log tail can lose more lines than shown.
            limits = limits_for(url, item.get("account") or "")
            if item.get("rules") != rules:
                # The DLP rules changed since it was checked: the ones in force now apply, to what the person
                # reviewed, so it never carries more than they saw.
                body = _checked(body, settings, limits=limits).body
            else:
                body = _fit(_scan_secrets(body, settings)[0], limits)  # a key saved since is still taken out
            # Only as its writer (or without an account when it was written so): _send checks who's signed in.
            delivered = _send(cloud, url, body, item["id"], account=item.get("account") or "")
        except _Later as later:
            attempts = int(item.get("attempts") or 1) + 1
            state = SIGN_IN if later.reason in ("sign_in", "unauthorized") else WAITING
            change = {"state": state, "reason": later.reason, "attempts": attempts, "marker": marker,
                      **_backoff(item, later.retry_after, attempts)}
            stop = state == WAITING  # Lumi Cloud can't take reports now: don't ask it once per report
        except FeedbackError as exc:
            if exc.code == "offline":
                change, stop = {}, True  # offline mode came on meanwhile: keep it for later
            else:
                reason = exc.code if exc.code in _HELD_WHY else "refused"
                change = {"state": HELD, "reason": reason, "detail": exc.message[:300]}
                result["held"] += 1
                _record("feedback.held", body, reason=reason, **({"status": exc.status} if exc.status else {}))
        except Exception:  # never lose the round's outcomes to one report
            logger.exception("Sending a waiting feedback report failed")
            attempts = int(item.get("attempts") or 1) + 1
            change = {"reason": "unreachable", "attempts": attempts, **_backoff(item, None, attempts)}
            stop = True
        else:
            change = None
            result["sent"] += 1
            _record("feedback.sent", body, queued=True, attributed=delivered.attributed)
        finally:
            try:
                with _state():
                    _sending.discard(item["id"])
                    if change is None or change:
                        _update(item["id"], change)
            except FeedbackError:
                # Busy: the outcome isn't saved, so the report is tried again later, and Lumi Cloud answers a
                # report sent again with its first answer.
                _sending.discard(item["id"])
                logger.warning("Couldn't save a waiting feedback report's outcome; it's tried again")
                stop = True
        if stop:
            break
    try:
        with _state():
            result["waiting"] = len(_load_queue())
    except FeedbackError:
        pass  # the count from the round's start
    return result


def send_held(cloud: Any, settings: Any, report_id: str, shown: str, *, shown_as: str = "",
              now: float | None = None) -> dict:
    """Send a report written before a feedback address was set, to the destination and as whom the person was shown.

    ``shown`` is the address the dialog displayed and ``shown_as`` the account
    its button named ("" for "without an account"): both must still be so,
    or nothing is sent. It goes with that account, and only with it.
    """
    from .cloud import same_address

    url = destination(cloud, settings).url
    if not url or not same_address(url, shown):
        raise FeedbackError("Where reports go changed. Check the address shown, then send it again.",
                            code="preview")
    refusal = disabled_reason(settings) or offline_refusal(url)
    if refusal:
        raise FeedbackError(refusal, code="disabled" if disabled_reason(settings) else "offline")
    account, email = _account(cloud, url)
    if (email if account else "") != str(shown_as or ""):
        raise FeedbackError("Who you're signed in as there changed. Check the button, then send it again.",
                            code="preview")
    with _state():
        item = next((entry for entry in _load_queue() if entry["id"] == str(report_id)), None)
        if item is None or item["url"] or item.get("reason") != "no_destination":
            raise FeedbackError("That report isn't waiting for an address any more.", code="gone")
        # Bound now to the destination, and the account, the person was shown.
        _update(item["id"], {"url": url, "account": account, "email": email if account else "", "state": WAITING,
                             "reason": "unreachable", "run": _run, "next_mono": 0.0})
    return flush(cloud, settings, force=True, now=now, only={str(report_id)})


def send_without_account(cloud: Any, settings: Any, report_id: str, *, now: float | None = None) -> dict:
    """The person's choice: send a report written with their account, which waits for them, without it instead.

    It then carries the install id of reports sent signed out and keeps its
    key, so a report whose earlier try with the account arrived isn't stored
    twice (see ``install_id``). It goes to the destination it was written
    for, as any report does.
    """
    from .cloud import same_address

    url = destination(cloud, settings).url
    refusal = disabled_reason(settings) or (offline_refusal(url) if url else "")
    if refusal:
        raise FeedbackError(refusal, code="disabled" if disabled_reason(settings) else "offline")
    with _state():
        item = next((entry for entry in _load_queue() if entry["id"] == str(report_id)), None)
        if item is None or not item.get("account") or item["state"] == HELD \
                or not (url and same_address(item["url"], url)):
            raise FeedbackError("That report isn't waiting for your sign-in any more.", code="gone")
        _update(item["id"], {"account": "", "email": "", "state": WAITING, "reason": "unreachable", "run": _run,
                             "next_mono": 0.0})
    _record("feedback.queued", item["body"], reason="without_account")
    return flush(cloud, settings, force=True, now=now, only={str(report_id)})


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
    """The times of reports sent or kept in the last RECENT_SECONDS (under ``_state``)."""
    try:
        values = json.loads(_recent_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        values = []
    return [float(v) for v in values if isinstance(v, int | float) and now - RECENT_SECONDS < float(v) <= now + 60] \
        if isinstance(values, list) else []


def _preview_data(preview_id: str, prepared: Prepared, url: str, email: str, *, provisional: bool) -> dict:
    return {"preview_id": preview_id, "body": prepared.body, "notices": prepared.notices,
            "destination": _host(url), "account": email, "offline": "", "provisional": provisional}


def _remember(form: Form, prepared: Prepared, url: str, account: str) -> str:
    preview_id = secrets.token_urlsafe(16)
    with _lock:
        _previews[preview_id] = _Preview(form.digest(), prepared, _monotonic(), url, account)
        while len(_previews) > MAX_PREVIEWS:
            _previews.popitem(last=False)
    return preview_id


def preview(cloud: Any, form_data: Any, *, settings: Any = None, provider: str = "", model: str = "",
            full: bool = False) -> dict:
    """The report the form makes, as it would be sent, and where and as whom it would go.

    Its ``preview_id`` sends this very report (``submit``) while the form, the
    destination and the account stay as they are. Only the rules on this
    computer check it unless ``full`` asks the organization's DLP service too
    (``provisional`` says the service will see it at Send). In offline mode a
    report that couldn't go is refused first, before anything else looks at it.
    """
    from . import dlp

    form = Form.read(form_data)
    _switched_off(form, settings)
    url = destination(cloud, settings).url
    refusal = offline_refusal(url)
    if refusal:
        raise FeedbackError(refusal, code="offline", copy=typed_copy(form_data, settings))
    user_id, email = _account(cloud, url)
    prepared = prepare(form, settings=settings, provider=provider, model=model, url=url, account=user_id,
                       service=full)
    preview_id = _remember(form, prepared, url, user_id)
    return _preview_data(preview_id, prepared, url, email if user_id else "",
                         provisional=not full and dlp.service_configured())


def _reviewed(preview_id: str, form: Form, url: str, account: str, email: str, settings: Any) -> Prepared:
    """The report the person reviewed, checked again with the rules in force now (and the DLP service).

    FeedbackError ``preview`` when there's no such review or things changed
    since, ``dlp`` when the rules now refuse it, and ``review`` (with the
    report as it is now) when the check changed it.
    """
    with _lock:
        item = _previews.get(str(preview_id or ""))
    if item is None or _monotonic() - item.made > PREVIEW_SECONDS or item.digest != form.digest():
        raise FeedbackError("The report changed since you reviewed it. Review what will be sent, then send it.",
                            code="preview")
    if (item.url, item.account) != (url, account):
        raise FeedbackError("Where the report goes, or as whom, changed since you reviewed it. Review it again, "
                            "then send it.", code="preview")
    # Again from the report as it was before DLP (source), with the service now: the same answer means nothing
    # changed. Checking the redacted report instead would find a rule's own marker when the rule is named
    # after its keyword, and never agree.
    rechecked = _checked(item.prepared.source or item.prepared.body, settings, limits=limits_for(url, account))
    if rechecked.body != item.prepared.body:
        again = rechecked
        new_id = _remember(form, again, url, account)
        with _lock:
            _previews.pop(str(preview_id), None)
        raise FeedbackError("Your organization's data loss prevention check changed the report. Review it as it "
                            "is now, then choose Send again.", code="review",
                            preview=_preview_data(new_id, again, url, email, provisional=False))
    return item.prepared


def submit(cloud: Any, form_data: Any, *, settings: Any = None, provider: str = "", model: str = "",
           preview_id: str = "", now: float | None = None) -> Outcome:
    """Send a report now, or keep it to send later; FeedbackError when it can't be either.

    With diagnostics, the report is the one ``preview`` showed (``preview_id``),
    checked again first. A FeedbackError's ``copy`` is the report as text to
    offer instead.
    """
    from . import audit

    form = Form.read(form_data)
    _switched_off(form, settings, copy=typed_copy(form_data, settings))
    with _submit_lock:
        now = time.time() if now is None else now
        url = destination(cloud, settings).url
        refusal = offline_refusal(url)
        if refusal:
            # Before anything looks at the report: one that can't leave isn't prepared or DLP-checked.
            audit.record("feedback.refused", kind=form.kind, reason="offline")
            raise FeedbackError(refusal, code="offline", copy=typed_copy(form_data, settings))
        user_id, email = _account(cloud, url)
        rules = _dlp_rules()  # before the check: a change during it is caught when a kept report goes
        try:
            prepared = _reviewed(preview_id, form, url, user_id, email, settings) if form.diagnostics else prepare(
                form, settings=settings, provider=provider, model=model, url=url, account=user_id)
        except FeedbackError as exc:
            if exc.code == "dlp":  # DLP records which rule (dlp.finding); this records that nothing went
                audit.record("feedback.refused", kind=form.kind, reason="dlp")
            elif exc.code == "busy":  # nothing was prepared: what the person typed, to keep
                exc.copy = exc.copy or typed_copy(form_data, settings)
            raise
        body = prepared.body
        copy = _copy_from_body(body)
        try:
            with _state():
                recent = _recent(now)
        except FeedbackError as exc:
            exc.copy = copy
            raise
        if len(recent) >= MAX_RECENT:
            _record("feedback.refused", body, reason="rate_limited")
            raise FeedbackError(f"You've sent {MAX_RECENT} reports in the last {RECENT_SECONDS // 60} minutes. Wait a "
                                "few minutes before sending another, or copy this one.", code="rate_limited", copy=copy)
        report_id = str(uuid.uuid4())
        if not url:
            outcome = _keep(report_id, body, state=HELD, reason="no_destination", account="", url="", now=now,
                            rules=rules)
            # Nowhere to send it yet: the dialog offers it to copy and email to Luminary instead.
            outcome.copy, outcome.support_email = copy, SUPPORT_EMAIL
        else:
            try:
                delivered = _send(cloud, url, body, report_id, account=user_id)
            except _Later as later:
                state = SIGN_IN if later.reason in ("sign_in", "unauthorized") else WAITING
                outcome = _keep(report_id, body, state=state, reason=later.reason, account=user_id, url=url,
                                now=now, retry_after=later.retry_after, marker=_signin_marker(cloud), email=email,
                                rules=rules)
            except FeedbackError as exc:
                _record("feedback.refused", body, reason=exc.code, **({"status": exc.status} if exc.status else {}))
                exc.copy = exc.copy or copy
                raise
            else:
                _record("feedback.sent", body, queued=False, attributed=delivered.attributed)
                outcome = Outcome("sent", "Thanks. Your feedback is in Lumi Cloud"
                                  + (f" (reference {delivered.server_id})." if delivered.server_id else "."),
                                  id=delivered.server_id)
        try:
            with _state():
                _write(_recent_path(), json.dumps([*_recent(now), now]))
        except (OSError, FeedbackError):  # the report went, or waits, all the same: don't tell the person it failed
            logger.warning("Couldn't count a feedback report for the rate limit", exc_info=True)
        with _lock:
            _previews.pop(str(preview_id or ""), None)
    outcome.notices = list(prepared.notices)
    if outcome.status == "queued":
        wake()
    return outcome


def status(cloud: Any, settings: Any = None) -> dict:
    """What the dialog shows before anything is sent: where reports go, as whom, and what waits."""
    from . import dlp
    from .cloud import same_address

    target = destination(cloud, settings)
    user_id, email = _account(cloud, target.url)
    busy = ""
    try:
        with _state():
            items = _load_queue()
    except FeedbackError as exc:
        items, busy = [], exc.message
    reports = []
    sendable = 0
    for item in items:
        here = bool(item["url"]) and same_address(item["url"], target.url)
        # Written with an account that isn't the one signed in there now: it waits for its writer.
        waits_for_writer = item["state"] != HELD and bool(item.get("account")) and item.get("account") != user_id
        if here and item["state"] != HELD and not waits_for_writer:
            sendable += 1
        reports.append({
            "id": item["id"], "kind": item["body"].get("kind"), "written": item.get("queued_at"),
            "state": item["state"], "reason": item.get("reason") or "", "detail": item.get("detail") or "",
            "destination": _host(item["url"]), "here": here,
            "copy": item.get("reason") != "dlp",
            "send": item.get("reason") == "no_destination" and bool(target.url),
            # Whose it is: it goes only with that account ("" for a report written without one).
            "writer": (item.get("email") or "") if item.get("account") else "",
            # The choices the person makes themselves: which account a report written without an address goes
            # with ("" for none, as its button says), and sending one that waits for a sign-in without it.
            "send_as": email if user_id else "",
            "without_account": waits_for_writer and here,
        })
    refusal = diagnostics_refusal(settings)
    return {"destination": _host(target.url), "destination_url": target.url, "source": target.source,
            "configured": bool(target.url), "account": email if user_id else "", "offline": offline_refusal(target.url),
            "disabled": disabled_reason(settings), "diagnostics": {"allowed": not refusal, "reason": refusal},
            "waiting": len(items), "sendable": sendable, "reports": reports, "app": app_info(), "busy": busy,
            "dlp_service": dlp.service_configured(), "limits": {"message": MAX_MESSAGE, "reply_to": MAX_REPLY_TO},
            # Where a report written with no address can be emailed instead (Copy beside it).
            "support_email": SUPPORT_EMAIL}


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
