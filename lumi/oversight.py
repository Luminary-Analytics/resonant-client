"""Organization oversight: the activity, messages and security flags an organization asks for.

Nothing here runs unless the organization's policy turns it on (the
``oversight`` section, lumi/policy.py). Then, for every turn that goes through
``Session.run`` (the app, ``lumi run``, scheduled tasks, the terminal UI, the
chat gateway, tasks from chat, plans, missions and Team workers), Lumi builds
a record of the turn and queues it for the organization's Lumi Cloud:

* **activity**: the session, the project's folder name (its full path only
  with ``project_paths``), what started the turn (``trigger``) and whether
  anyone was there to be shown the notice (``unattended``), the computer
  user name, the turn's number, when it ran, the provider and model, the
  permission mode, how it ended, the tools it called and whether each ran,
  and what it cost;
* **messages** (``redacted`` or ``full``): the person's message and Lumi's
  final reply, the session's title (an automatic title is the gist of the
  first message) and the commands, paths and patterns the tools were given.
  ``redacted`` leaves out code blocks, email addresses and web addresses'
  query strings and keeps 2,000 characters; ``full`` keeps 20,000. Secrets
  are removed at every level (``secret_scan.redact_for_sharing``), and paths
  inside excluded files are never named;
* **security flags** (lumi/security_flags.py): refused dangerous commands,
  policy and file denials, declined approvals, removed secrets and signs of
  prompt injection in tool output. A flag's rule is a fixed label; its short
  excerpt goes only with messages. Flags also show in the person's own
  Settings.

Never file contents or tool output (beyond a flag's short excerpt when
messages are shared), never screenshots, keystrokes or anything outside
Lumi's own turns.

**Nothing reaches a model until the person has confirmed the notice.** While
a policy's oversight is in force (it asks for something, and records have
somewhere to go), ``admit`` refuses every turn of a person who hasn't
confirmed the notice for that policy on this computer: ``Session.run``
checks it before a turn and before each model request, and the app, plans,
missions, autonomous sessions, Team, model comparisons and dictation check
it before they start (``refusal``). A confirmation (``acknowledge``) comes
from the notice's own **I've read this** button in the app, a typed yes at an
interactive terminal (``lumi run``, the terminal UI) or, for a chat of the
chat gateway, that chat's button or reply; never from a status push, a timer
or a painted page. Each one is a record (``ACKNOWLEDGMENT_KIND``) signed with
this computer's enrolled device key, kept here and sent to Lumi Cloud in the
background; the person is unblocked at once. A kept confirmation counts only
while its signature verifies with this computer's device key and it covers
the notice in force: its fingerprint (the organization, this computer's
enrollment and the policy's ``oversight`` section as published) and the
SHA-256 of the notice's text. Another enrollment, a changed section or a
changed text (a new Lumi, a renamed organization) needs it confirmed again,
a file written by hand counts for nothing, and leaving the organization or
signing out of Lumi Cloud forgets it.

**Unattended runs** have nobody to show the notice to: a scheduled task or
``lumi run`` whose standard input, output and error are none of them a
terminal, in a process with no controlling terminal (on Windows, no console
window in an interactive session). The run's environment says so, never
what started it. If this computer user confirmed the notice, they run and
are recorded as theirs; otherwise the policy's ``oversight.unattended``
decides: ``record`` (the default) runs them, prints the notice with their
output and records them with the computer user and device as who ran them;
``block`` refuses them.

**Where it goes.** Only to the Lumi Cloud of the organization whose policy
asks for it: the policy must come from that Lumi Cloud (verified for this
enrolled computer), or be a machine policy that enrolled it there. Records
wait in ``~/.lumi/oversight/queue.sqlite3`` (bounded, and deleted unsent
once older than the policy's ``retention_days``) and a background thread
sends them with the device's own sign-in (lumi/cloud.py), retrying with
backoff. If the policy stops asking, or the computer leaves the
organization, queued records are deleted, not sent. Every record dropped,
refused, expired or deleted is counted in Settings.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import sqlite3
import threading
import time
import uuid
from collections import Counter
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from .file_lock import exclusive

logger = logging.getLogger(__name__)

UPLOAD_PATH = "/api/v1/oversight/events"
ACKNOWLEDGMENT_PATH = "/api/v1/oversight/acknowledgments"
ACKNOWLEDGMENT_KIND = "lumi.oversight-acknowledgment/v1"
# What started a turn, on every record (the contract with Lumi Cloud): the
# app's chat, a terminal (the terminal UI, or `lumi run` with someone at a
# terminal), a chat (the chat gateway, tasks from chat), a scheduled task,
# `lumi run` with no terminal, /plan, a mission or autonomous session, or a
# Team worker.
TRIGGERS = ("app", "terminal", "gateway", "schedule", "headless", "plan", "mission", "team")
# Where a confirmation of the notice can come from (an acknowledgment's ``surface``).
ACKNOWLEDGING_SURFACES = ("app", "terminal", "gateway")
# The error code of a refused turn (Session.run) and refused work.
REFUSAL_CODE = "oversight_notice"
# What a chat of the chat gateway replies to confirm its notice; its button sends ``/acknowledge``.
CHAT_PHRASE = "I've read this"
_CHAT_PHRASES = frozenset({"i've read this", "ive read this", "i have read this"})
MESSAGE_LIMITS = {"redacted": 2_000, "full": 20_000}
ARGUMENT_LIMITS = {"redacted": 300, "full": 2_000}
TITLE_LIMIT = 200
# In a record, in place of a message or tool argument the organization's DLP
# rules withhold (dlp.shareable).
WITHHELD = "[withheld by data loss prevention]"
MAX_TOOLS = 100
MAX_FLAGS_PER_TURN = 20
MAX_RECORDS = 5_000
MAX_QUEUE_BYTES = 20 * 1024 * 1024
BATCH_RECORDS = 100
BATCH_BYTES = 900_000
LOCAL_FLAGS = 500
KNOWN_SESSIONS = 2_000
KNOWN_CHATS = 2_000
# Acknowledgments kept once Lumi Cloud has them (or refused them), newest first.
KEPT_ACKNOWLEDGMENTS = 200
ACKNOWLEDGMENTS_PER_STEP = 20
# A claim on a record or acknowledgment another process was sending, past which it is sent again.
SENDING_SECONDS = 600
IDLE_SECONDS = 300.0
BUSY_SECONDS = 2.0
RETRY_SECONDS = (30.0, 60.0, 120.0, 300.0, 900.0, 1800.0, 3600.0)
# Where a session's turns come from, by the audit session id the surface sets.
SURFACES = (("headless:", "lumi run"), ("gateway:", "chat gateway"), ("chat-task:", "task from chat"),
            ("tui:", "terminal"))
# Sessions whose people aren't this computer's person: each chat of the chat
# gateway confirms the notice itself (chat_notice) before its turns run.
CHAT_PREFIX = "gateway:"
TASK_PREFIX = "chat-task:"
# Tools whose output is Lumi's own (a worker's hand-off, a skill, the
# person's answer): not searched for injection.
_NOT_SCANNED = frozenset({"task", "task_batch", "await_user", "search_tools", "skill_view", "artifact_read"})
_PATH_ARGUMENTS = ("path", "cwd", "working_subdir")
_TEXT_ARGUMENTS = ("command", "pattern", "glob", "query", "url", "agent_type")
_WRITE_TOOLS = frozenset({"file_edit", "file_write", "file_replace"})
_CODE_BLOCK = re.compile(r"(?ms)^[ \t]*(```|~~~)[^\n]*\n.*?(?:^[ \t]*\1[ \t]*$|\Z)")
# Starts only where an address can start (not \b, which "a.a.a." has
# everywhere), so a long run of such text isn't rescanned from every dot.
_EMAIL = re.compile(r"(?<![A-Za-z0-9._%+-])[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")

_lock = threading.RLock()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _seconds() -> str:
    """Now as ISO 8601 UTC to the second (an acknowledgment's ``acknowledged_at``)."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _root() -> Path:
    from .paths import state_home

    return state_home() / "oversight"


def _read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError):
        return default


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(data, indent=1, sort_keys=True), encoding="utf-8")
    os.replace(temporary, path)


def os_user() -> str:
    """This computer's user running Lumi (who a confirmation or an unattended run belongs to)."""
    try:
        import getpass

        name = getpass.getuser()
    except Exception:  # no login name in the environment or the account database
        name = os.environ.get("USERNAME") or os.environ.get("USER") or ""
    return str(name or "")[:200]


def _cloud_account() -> str | None:
    """The signed-in Lumi Cloud user's id from settings.json, or None (never a secret)."""
    from .paths import state_home

    data = _read_json(state_home() / "settings.json", {})
    section = data.get("cloud") if isinstance(data, dict) else None
    account = section.get("account") if isinstance(section, dict) else None
    user = account.get("user_id") if isinstance(account, dict) else None
    return str(user)[:200] if user else None


def canonical(document: dict) -> bytes:
    """What a signature covers: sorted keys, no whitespace, UTF-8 (as ``policy.canonical``)."""
    from .policy import canonical as policy_canonical

    return policy_canonical(document)


def notice_fingerprint(organization_id: str, device_id: str, section: Any) -> str:
    """Which notice is in force: the organization, this computer's enrollment and the section as published.

    The SHA-256 hex digest (64 characters) of the canonical JSON of
    ``{"device": <device id>, "organization_id": <organization id>,
    "oversight": <the policy's oversight section exactly as published>}``.
    Lumi Cloud computes the same from the policy versions it published to
    tell which notice was confirmed (docs/organization-oversight.md).
    """
    return hashlib.sha256(canonical({
        "device": str(device_id or ""),
        "organization_id": str(organization_id or ""),
        "oversight": section if isinstance(section, dict) else {}})).hexdigest()


def button_token(fingerprint: str) -> str:
    """What a chat's I've read this button carries (``/acknowledge <token>``): enough of the fingerprint to
    tell notices apart, short enough for Telegram's 64-byte button data."""
    return str(fingerprint or "")[:32]


class ConfirmationError(Exception):
    """A person confirmed the notice, but Lumi couldn't make the signed record; nothing was confirmed."""


# The api_keys entry that holds this computer's enrolled device key (cloud.DEVICE_SECRET).
_DEVICE_KEY_NAME = "lumi_cloud_device_key"
_public_keys: dict[tuple, Any] = {}


def _device_public_key(device: dict) -> Any:
    """The public half of this computer's enrolled device key, or None when there's none.

    Read from settings.json, or the credential store its placeholder names,
    once per enrollment and key in this process.
    """
    from .paths import state_home
    from .secrets_store import PLACEHOLDER, SecretStore

    data = _read_json(state_home() / "settings.json", {})
    keys = data.get("api_keys") if isinstance(data, dict) else None
    saved = str(keys.get(_DEVICE_KEY_NAME) or "") if isinstance(keys, dict) else ""
    if not saved or not device.get("id"):
        return None
    cache = (str(device.get("id")), str(device.get("enrolled_at") or ""), saved)
    if cache not in _public_keys:
        secret = SecretStore().get(_DEVICE_KEY_NAME) if saved == PLACEHOLDER else saved
        if not secret:
            return None
        import base64

        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

        _public_keys[cache] = Ed25519PrivateKey.from_private_bytes(base64.b64decode(secret)).public_key()
    return _public_keys[cache]


def _signed_by_this_computer(record: dict, signature: str, device: dict) -> bool:
    """Whether ``signature`` (base64url) is this computer's device key's over the record's canonical JSON."""
    import base64

    try:
        public = _device_public_key(device)
        if public is None or not signature:
            return False
        public.verify(base64.urlsafe_b64decode(signature + "=" * (-len(signature) % 4)), canonical(record))
        return True
    except Exception:
        return False


def _confirmed(entry: Any, scope: "Scope", *, chat: str = "") -> bool:
    """Whether a kept confirmation covers the notice in force.

    The record this computer's device key signed, for this organization and
    enrollment, this notice (its fingerprint, and the SHA-256 of its text as
    the surface shows it) and this person: the computer user (the app, a
    terminal) or ``chat``. Anything else, a file written by hand included,
    counts as no confirmation.
    """
    if not isinstance(entry, dict) or not scope.fingerprint:
        return False
    record, signature = entry.get("record"), entry.get("signature")
    if not isinstance(record, dict) or not isinstance(signature, str):
        return False
    surface = record.get("surface")
    person = record.get("person") if isinstance(record.get("person"), dict) else {}
    if chat:
        if surface != "gateway" or person.get("chat") != chat[len(CHAT_PREFIX):]:
            return False
    elif surface not in ("app", "terminal") or person.get("os_user") != os_user():
        return False
    shown = hashlib.sha256(scope.notice(surface).encode("utf-8")).hexdigest()
    if (record.get("kind") != ACKNOWLEDGMENT_KIND or record.get("notice_fingerprint") != scope.fingerprint
            or record.get("organization") != scope.organization_id or record.get("device_id") != scope.device_id
            or record.get("notice_sha256") != shown):
        return False
    return _signed_by_this_computer(record, signature, scope.device)


# ── What the policy asks for ────────────────────────────────────────────────


class Scope:
    """The oversight in force here and now, and whether this person confirmed its notice."""

    def __init__(self) -> None:
        from . import policy

        state = policy.load()
        current = state.policy
        self.settings = current.oversight if current is not None else policy.Oversight()
        self.organization = current.organization if current is not None else ""
        # A section this Lumi can't honor turns oversight off; Settings says why (policy.Oversight.error).
        self.error = self.settings.error if current is not None else ""
        self.configured = bool(current is not None and self.settings.enabled)
        self.fingerprint = ""
        self.reason = ""
        self.destination = False
        self.organization_id = ""
        self.device_id = ""
        self.device: dict = {}
        self._acknowledged: bool | None = None
        if not self.configured:
            return
        device = policy.enrolled_device()
        self.device = device
        self.organization_id = str(device.get("organization_id") or "")
        self.device_id = str(device.get("id") or "")
        # What the notice said, and to which organization and enrollment of
        # this computer records go: any of them changing needs the notice again.
        self.fingerprint = notice_fingerprint(self.organization_id, self.device_id, current.raw.get("oversight"))
        from_cloud = state.cloud
        managed = device.get("how") == "managed" and isinstance(current.raw.get("cloud"), dict)
        if not device:
            self.reason = (f"This computer isn't enrolled in {self.organization}'s Lumi Cloud, so nothing is "
                           "collected.")
        elif not (from_cloud or managed):
            self.reason = (f"This policy doesn't come from {device.get('organization_name') or 'the'} Lumi Cloud "
                           "this computer is enrolled in, so nothing is collected.")
        else:
            self.destination = True

    @property
    def in_force(self) -> bool:
        """Asked for, with somewhere to send records: nothing reaches a model before its notice is confirmed."""
        return self.configured and self.destination

    def notice(self, surface: str = "app") -> str:
        """The notice, exactly as ``surface`` shows it: Lumi's description, then the organization's words."""
        where = "from Lumi through this chat" if surface == "gateway" else "from Lumi on this computer"
        text = notice_text(self.settings, self.organization, where=where)
        return f"{text} {self.settings.notice}" if self.settings.notice else text

    @property
    def acknowledged(self) -> bool:
        """This computer user confirmed the notice in force (the app or a terminal): ``_confirmed``."""
        if self._acknowledged is None:
            self._acknowledged = self.configured and _confirmed(_read_json(_root() / "notice.json", {}), self)
        return self._acknowledged

    @property
    def active(self) -> bool:
        """Whether this person's turns are recorded now: asked for, a destination, and the notice confirmed."""
        return self.in_force and self.acknowledged

    @property
    def required(self) -> bool:
        """Whether this person must confirm the notice before anything reaches a model."""
        return self.in_force and not self.acknowledged


def notice_text(settings: Any, organization: str, *, where: str = "from Lumi on this computer") -> str:
    """One sentence for the notice beside the message box (and a terminal's, and a chat's)."""
    parts = []
    if settings.activity:
        parts.append("your sessions and what they did")
    if settings.messages == "redacted":
        parts.append("your messages, Lumi's replies and your sessions' titles (shortened, without code or secrets)")
    elif settings.messages == "full":
        parts.append("your messages, Lumi's replies and your sessions' titles (without secrets)")
    if settings.security_flags:
        parts.append("security flags")
    if not parts:
        return ""
    listed = parts[0] if len(parts) == 1 else ", ".join(parts[:-1]) + " and " + parts[-1]
    return f"{organization or 'Your organization'} receives {listed} {where}."


def described(settings: Any, organization: str) -> dict:
    """What Settings lists: what is shared, what never is, who reads it, and for how long."""
    who = organization or "Your organization"
    shared = []
    if settings.activity:
        where = "the project's full path" if settings.project_paths else "the project folder's name"
        shared.append(f"Each turn's activity: the session, {where}, what started it (the app, a terminal, a chat, "
                      "a schedule, a plan, a mission or a team) and whether anyone was at the screen, your computer "
                      "user name, the model and permission mode, when it ran, how it ended, which tools ran or were "
                      "refused, and its cost.")
    if settings.messages == "redacted":
        shared.append("Your message and Lumi's final reply in each turn and the session's title, up to 2,000 "
                      "characters, without code blocks, email addresses or secrets; and the commands, paths and "
                      "search patterns tools were given, shortened, without secrets or web addresses' query "
                      "strings.")
    elif settings.messages == "full":
        shared.append("Your message and Lumi's final reply in each turn and the session's title, as written, up to "
                      "20,000 characters, without secrets; and the commands, paths and search patterns tools were "
                      "given, without secrets.")
    if settings.security_flags:
        excerpt = ", each with a short excerpt without secrets" if settings.messages != "off" else ""
        shared.append("Security flags: dangerous commands Lumi refused, commands and files your organization's "
                      "rules refused, approvals that were declined, secrets Lumi removed and signs of prompt "
                      f"injection in what tools returned{excerpt}. A flag names a fixed rule, never a file or "
                      "what a hook said.")
    shared.append("When you confirm this notice: a record of which notice you confirmed, where and when, signed "
                  "with this computer's key.")
    not_shared = [
        "File contents and what tools returned (command output, web pages).",
        "Screenshots, keystrokes, the clipboard, or anything you do outside Lumi's turns.",
        "Saved API keys and anything that looks like a secret, at every level: keys and tokens, passwords in "
        "commands and web addresses, authorization headers.",
        "The names of files you exclude from Lumi, and the patterns that exclude them.",
    ]
    if settings.messages == "off":
        not_shared.insert(0, "Your messages, Lumi's replies, your sessions' titles, and the commands and paths "
                             "tools were given.")
    readers = (f"In {who}'s Lumi Cloud, owners, security admins and auditors can read messages, replies, titles "
               "and flag excerpts through their role; anyone else only if an owner allows them. Every view is "
               "recorded in the organization's activity log.") if settings.messages != "off" else ""
    if settings.unattended == "block":
        unattended = ("Runs with nobody at the screen (scheduled tasks, lumi run with no terminal) don't start until "
                      "this notice is confirmed on this computer as the computer user they run as.")
    else:
        unattended = ("Runs with nobody at the screen (scheduled tasks, lumi run with no terminal) still run if "
                      "nobody confirmed this notice as their computer user: they print it with their output, and "
                      f"{who} receives them with the computer user name and this computer as who ran them.")
    return {"shared": shared, "not_shared": not_shared, "readers": readers, "unattended_runs": unattended,
            "retention": f"{who} keeps it for {settings.retention_days} days, then Lumi Cloud deletes it. "
                         "Records waiting on this computer longer than that are deleted unsent."}


def status() -> dict:
    """What the notice and Settings show; never raises."""
    from .security_flags import rule_text

    try:
        scope = Scope()
        settings = scope.settings
        info = {
            "configured": scope.configured,
            "active": scope.active,
            "destination": scope.destination,
            # The app locks its message box until this person confirms the notice.
            "required": scope.required,
            "organization": scope.organization,
            "settings": settings.summary(),
            "fingerprint": scope.fingerprint,
            "acknowledged": scope.acknowledged,
            "reason": scope.reason,
            # The policy asks for oversight this Lumi can't honor, so it's off (policy.Oversight.error).
            "policy_error": scope.error,
            "notice": notice_text(settings, scope.organization) if scope.configured else "",
            "organization_notice": settings.notice if scope.configured else "",
            # Exactly what the notice beside the message box shows, and what confirming it covers.
            "notice_text": scope.notice("app") if scope.configured else "",
            "unattended": settings.unattended,
            "acknowledgment": _acknowledgment_status(scope),
            **(described(settings, scope.organization) if scope.configured else {}),
        }
    except Exception as exc:  # the page still shows the policy's error elsewhere
        logger.exception("Oversight status failed")
        info = {"configured": False, "active": False, "required": False, "error": str(exc)}
    info["queue"] = queue_status()
    info["flags"] = [{**flag, "rule_text": rule_text(flag.get("rule"))} for flag in recent_flags(50)]
    return info


# ── Admitting turns ─────────────────────────────────────────────────────────


@dataclass
class Admission:
    """Whether a turn may reach a model, and how it is recorded.

    ``refusal`` says why it may not (``''`` when it may); ``scope`` is set
    when the turn is recorded.
    """

    refusal: str = ""
    scope: Scope | None = None
    trigger: str = "app"
    unattended: bool = False
    acknowledged: bool = False


def _surface_of(session: Any) -> tuple[str, bool, str]:
    """(trigger, unattended, chat key) of a session, from what its surface set (Session.oversight_*)."""
    key = str(getattr(session, "audit_session_id", "") or "")
    trigger = str(getattr(session, "oversight_trigger", "") or "")
    if trigger not in TRIGGERS:
        trigger = ("gateway" if key.startswith((CHAT_PREFIX, TASK_PREFIX)) else "terminal" if key.startswith("tui:")
                   else "headless" if key.startswith("headless:") else "app")
    # Only a surface that says so is unattended: anything else needs a person's confirmation.
    unattended = getattr(session, "oversight_unattended", False) is True
    chat = key if key.startswith(CHAT_PREFIX) else ""
    return trigger, unattended, chat


def _place(trigger: str, session_key: str = "") -> str:
    if session_key.startswith(TASK_PREFIX):
        return "chat_task"
    return "terminal" if trigger in ("terminal", "headless", "schedule") else "app"


def _person_refusal(scope: Scope, place: str) -> str:
    who = scope.organization or "Your organization"
    if place == "app":
        return (f"Lumi won't send anything to a model until you confirm {who}'s oversight notice: read it above the "
                "message box and choose I've read this.")
    if place == "chat_task":
        return (f"Lumi on this computer won't run requests until you confirm {who}'s oversight notice in the Lumi "
                "app.")
    return (f"Lumi won't send anything to a model until you confirm {who}'s oversight notice: confirm it in the "
            "Lumi app, or type yes when lumi run or the terminal UI shows it at an interactive terminal.")


def _unattended_refusal(scope: Scope) -> str:
    who = scope.organization or "Your organization"
    return (f"{who}'s policy doesn't let Lumi run unattended until its oversight notice is confirmed on this "
            f"computer as {os_user() or 'this user'}: in the Lumi app, or by typing yes when lumi run or the "
            "terminal UI shows it at an interactive terminal.")


def _chat_refusal(scope: Scope) -> str:
    who = scope.organization or "Your organization"
    return (f"Lumi won't run requests from this chat until someone here confirms {who}'s oversight notice: press "
            f"I've read this, or reply \"{CHAT_PHRASE}\".")


def _admission(scope: Scope, *, trigger: str, unattended: bool, chat: str = "", session_key: str = "") -> Admission:
    if not scope.in_force:
        # Nothing is collected (not asked for, or nowhere to send it): nothing to confirm.
        return Admission(trigger=trigger, unattended=unattended)
    if chat:
        if _chat_acknowledged(chat, scope):
            return Admission(scope=scope, trigger=trigger, acknowledged=True)
        return Admission(refusal=_chat_refusal(scope), trigger=trigger)
    if scope.acknowledged:
        return Admission(scope=scope, trigger=trigger, unattended=unattended, acknowledged=True)
    if unattended:
        if scope.settings.unattended == "record":
            return Admission(scope=scope, trigger=trigger, unattended=True)
        return Admission(refusal=_unattended_refusal(scope), trigger=trigger, unattended=True)
    return Admission(refusal=_person_refusal(scope, _place(trigger, session_key)), trigger=trigger)


def _failed_check(exc: Exception) -> Admission:
    """A check that failed: closed when a policy asks for oversight, open otherwise."""
    logger.exception("Couldn't check the organization's oversight notice")
    try:
        from . import policy

        if policy.oversight_settings().enabled:
            return Admission(refusal=f"Lumi couldn't check your organization's oversight notice ({exc}), so "
                                     "nothing is sent to a model.")
    except Exception:
        return Admission(refusal="Lumi couldn't check your organization's oversight notice, so nothing is sent "
                                 "to a model.")
    return Admission()


def admit(session: Any) -> Admission:
    """Whether ``session``'s next turn (or model request) may reach a model; never raises.

    ``Session.run`` asks before every turn and before each model request, so
    every surface is covered by running through it. A delegated worker
    follows its parent's surface and is recorded with its parent's turn.
    A surface is attended unless it sets ``oversight_unattended``: a new
    surface that forgets needs the person's confirmation, never less.
    """
    root, depth = session, 0
    while getattr(root, "parent_session", None) is not None and depth < 50:
        root, depth = root.parent_session, depth + 1
    try:
        trigger, unattended, chat = _surface_of(root)
        admission = _admission(Scope(), trigger=trigger, unattended=unattended, chat=chat,
                               session_key=str(getattr(root, "audit_session_id", "") or ""))
    except Exception as exc:
        return _failed_check(exc)
    if root is not session:
        admission.scope = None  # part of its parent's turn, which sees its calls
    return admission


def refusal(trigger: str = "app") -> str:
    """Why this computer's person can't start work that reaches a model now; '' when they can.

    For entry points outside a turn: the app's message box, /plan, missions,
    autonomous sessions, Team, model comparisons, dictation and evaluations.
    Never raises.
    """
    try:
        return _admission(Scope(), trigger=trigger if trigger in TRIGGERS else "app", unattended=False).refusal
    except Exception as exc:
        return _failed_check(exc).refusal


# ── Terminals ───────────────────────────────────────────────────────────────


@dataclass
class Terminal:
    """What a terminal surface (``lumi run``, the terminal UI) shows and asks before it starts."""

    notice: str = ""  # what to print ('' when no policy asks for oversight)
    text: str = ""  # the notice itself: what a confirmation covers
    fingerprint: str = ""
    organization: str = ""
    confirm: bool = False  # the person at this terminal must type yes first
    refusal: str = ""  # the run can't start (unattended, under "block")
    recorded: bool = False
    acknowledged: bool = False
    in_force: bool = False  # asked for, with somewhere to send records


def for_terminal(*, unattended: bool) -> Terminal:
    """What a terminal surface prints and asks. Never raises; never confirms anything itself."""
    try:
        scope = Scope()
        if not scope.configured:
            return Terminal(notice=scope.error)  # a section this Lumi can't honor: say so, nothing is collected
        text = scope.notice("terminal")
        base = Terminal(notice=text, text=text, fingerprint=scope.fingerprint, organization=scope.organization)
        if not scope.destination:
            base.notice = f"{text} ({scope.reason})"
            return base
        base.in_force = True
        if scope.acknowledged:
            base.recorded = base.acknowledged = True
        elif not unattended:
            base.confirm = True
        elif scope.settings.unattended == "record":
            base.recorded = True
        else:
            base.refusal = _unattended_refusal(scope)
        return base
    except Exception:
        logger.exception("Oversight notice failed")
        return Terminal()


def is_yes(answer: Any) -> bool:
    """A typed confirmation at a terminal."""
    return str(answer or "").strip().lower() in ("y", "yes")


# ── Confirming the notice ───────────────────────────────────────────────────


def _acknowledgment_record(scope: Scope, surface: str, notice: str, chat: str) -> dict:
    """The record a confirmation produces (the contract with Lumi Cloud)."""
    return {
        "kind": ACKNOWLEDGMENT_KIND,
        "organization": scope.organization_id,
        "notice_fingerprint": scope.fingerprint,
        "notice_sha256": hashlib.sha256(notice.encode("utf-8")).hexdigest(),
        "surface": surface,
        "person": {
            # A chat's people aren't this computer's Lumi Cloud account.
            "account": None if surface == "gateway" else _cloud_account(),
            "os_user": os_user(),
            "chat": chat[len(CHAT_PREFIX):] if surface == "gateway" else None,
        },
        "device_id": scope.device_id,
        "acknowledged_at": _seconds(),
    }


def _sign(record: dict, signer: Callable[[bytes], str] | None, scope: Scope) -> str:
    """The device key's signature over the record's canonical JSON; ConfirmationError when there's none."""
    if signer is None:
        raise ConfirmationError("Lumi couldn't sign your confirmation: this computer's Lumi Cloud key isn't "
                                "available here. Nothing was confirmed.")
    try:
        signature = str(signer(canonical(record)) or "")
    except Exception as exc:
        logger.warning("Couldn't sign an oversight acknowledgment", exc_info=True)
        raise ConfirmationError(f"Lumi couldn't sign your confirmation with this computer's key ({exc}). "
                                "Nothing was confirmed; try again.") from exc
    if not _signed_by_this_computer(record, signature, scope.device):
        raise ConfirmationError("Your confirmation's signature doesn't match this computer's Lumi Cloud key. "
                                "Nothing was confirmed.")
    return signature


def acknowledge(fingerprint: str, surface: str, *, notice: str | None = None, chat: str = "",
                signer: Callable[[bytes], str] | None = None) -> bool:
    """A person confirmed the notice for the policy in force; they're unblocked from now on.

    ``surface`` is ``app`` (the notice's own button), ``terminal`` (a typed yes
    at an interactive terminal) or ``gateway`` (a chat's button or reply;
    ``chat`` is its session key). ``notice`` is the text that was shown, and
    is required: a page, terminal or chat that showed another text (the
    policy changed) or doesn't say what it showed confirms nothing. The
    record (``ACKNOWLEDGMENT_KIND``) is signed with ``signer`` (the device
    key; ``CloudClient.sign_as_device``), kept with the confirmation and
    queued for Lumi Cloud. Without a signature this computer's key verifies,
    nothing is confirmed: ConfirmationError says why.

    Refused (False) when ``fingerprint`` isn't the policy in force, or when
    records have nowhere to go: that notice says nothing is collected, so
    confirming it can't start collection later.
    """
    if surface not in ACKNOWLEDGING_SURFACES:
        raise ValueError(f"Unknown surface {surface!r}")
    scope = Scope()
    if not scope.in_force or not fingerprint or fingerprint != scope.fingerprint:
        return False
    if surface == "gateway" and not str(chat).startswith(CHAT_PREFIX):
        return False
    shown = scope.notice(surface)
    if not isinstance(notice, str) or notice != shown:
        return False

    def confirmed() -> bool:
        if surface == "gateway":
            return _chat_acknowledged(chat, scope)
        return _confirmed(_read_json(_root() / "notice.json", {}), scope)

    if confirmed():
        return True
    record = _acknowledgment_record(scope, surface, shown, chat)
    signature = _sign(record, signer, scope)
    ack_id = uuid.uuid4().hex
    entry = {"id": ack_id, "fingerprint": fingerprint, "organization": scope.organization, "surface": surface,
             "os_user": record["person"]["os_user"], "shown_at": record["acknowledged_at"], "notice": shown,
             "record": record, "signature": signature}
    with _lock, exclusive(_root() / ".lock"):
        # Confirmed meanwhile (a second click, another process): one record, not two.
        if confirmed():
            return True
        if surface == "gateway":
            known = _read_json(_root() / "chats.json", {})
            known = known if isinstance(known, dict) else {}
            previous = known.get(str(chat)[:200]) if isinstance(known.get(str(chat)[:200]), dict) else {}
            known[str(chat)[:200]] = {**previous, **entry, "acknowledged": fingerprint, "at": _now()}
            _write_json(_root() / "chats.json", _bounded_chats(known))
        else:
            _write_json(_root() / "notice.json", entry)
        _queue_acknowledgment(ack_id, record, signature)
    from . import audit

    audit.record("oversight.notice_shown", organization=scope.organization, surface=surface,
                 acknowledgment=ack_id, fingerprint=fingerprint, notice_sha256=record["notice_sha256"],
                 os_user=record["person"]["os_user"], signed=bool(signature),
                 **({"session": str(chat)[:200]} if surface == "gateway" else {}),
                 **{key: value for key, value in scope.settings.summary().items() if key not in ("notice", "error")})
    wake()
    return True


def forget_notice(reason: str) -> None:
    """Forget every confirmed notice, the app's and the chats' (the computer left, or signed out); never raises."""
    root = _root()
    if not root.is_dir():
        return
    forgotten = []
    try:
        with _lock, exclusive(root / ".lock"):
            for name in ("notice.json", "chats.json"):
                path = root / name
                if path.exists():
                    path.unlink()
                    forgotten.append(name)
    except OSError:
        logger.warning("Couldn't forget the oversight notice", exc_info=True)
        return
    if forgotten:
        from . import audit

        audit.record("oversight.notice_forgotten", reason=str(reason)[:300])


def _acknowledgment_status(scope: Scope) -> dict | None:
    """This person's last confirmation for Settings: when, which notice, and whether Lumi Cloud has it."""
    shown = _read_json(_root() / "notice.json", {})
    if not isinstance(shown, dict) or not shown.get("id") or not isinstance(shown.get("record"), dict):
        return None
    upload = acknowledgment_upload(str(shown["id"]))
    return {"id": shown["id"], "at": str(shown.get("shown_at") or ""), "surface": str(shown.get("surface") or ""),
            "notice": str(shown.get("notice") or ""), "fingerprint": str(shown.get("fingerprint") or ""),
            "notice_sha256": str(shown["record"].get("notice_sha256") or ""),
            "current": _confirmed(shown, scope), "signed": bool(shown.get("signature")), "upload": upload}


# ── The chat gateway's chats ────────────────────────────────────────────────


def _bounded_chats(known: dict) -> dict:
    if len(known) <= KNOWN_CHATS:
        return known
    newest = sorted(known.items(), key=lambda item: str(item[1].get("at") or ""), reverse=True)
    return dict(newest[:KNOWN_CHATS])


def _chat_entry(chat: str) -> dict:
    known = _read_json(_root() / "chats.json", {})
    entry = known.get(str(chat)[:200]) if isinstance(known, dict) else None
    return entry if isinstance(entry, dict) else {}


def _chat_acknowledged(chat: str, scope: Scope) -> bool:
    """The chat's people confirmed the notice in force (``_confirmed``)."""
    return _confirmed(_chat_entry(chat), scope, chat=chat)


def chat_notified(chat: str, fingerprint: str) -> bool:
    """The chat was sent the notice for ``fingerprint`` (its reply can confirm it)."""
    return bool(fingerprint) and _chat_entry(chat).get("notified") == fingerprint


def chat_notice(chat: str) -> tuple[str, str] | None:
    """(notice, fingerprint): what a gateway chat must confirm before its turns run.

    None when nothing needs confirming: no policy asks, records have nowhere
    to go, or the chat already confirmed this policy's notice. The person
    running the gateway is shown its notice at their terminal; the people in
    a chat confirm it in the chat. Never raises.
    """
    try:
        scope = Scope()
        if not scope.in_force or _chat_acknowledged(chat, scope):
            return None
        return scope.notice("gateway"), scope.fingerprint
    except Exception:
        logger.exception("Oversight chat notice failed")
        return None


def chat_message(notice: str) -> str:
    """The message a chat is sent: the notice, and how to confirm it."""
    return (f"Organization oversight: {notice}\n\nLumi won't run requests from this chat, or send anything from it "
            f"to a model, until someone here confirms they've read this: press I've read this, or reply "
            f"\"{CHAT_PHRASE}\".")


def is_acknowledgment(text: Any) -> bool:
    """A chat's reply that confirms the notice (``CHAT_PHRASE``, any case, curly apostrophe or not)."""
    words = " ".join(str(text or "").replace("’", "'").lower().split()).strip(" .!")
    return words in _CHAT_PHRASES


def chat_notice_sent(chat: str, fingerprint: str) -> None:
    """The chat was sent the notice for ``fingerprint``; a confirmation from it counts from now on."""
    path = _root() / "chats.json"
    with _lock, exclusive(_root() / ".lock"):
        known = _read_json(path, {})
        known = known if isinstance(known, dict) else {}
        key = str(chat)[:200]
        previous = known.get(key) if isinstance(known.get(key), dict) else {}
        known[key] = {**previous, "notified": str(fingerprint), "notified_at": _now(), "at": _now()}
        _write_json(path, _bounded_chats(known))


# ── Recording a turn ────────────────────────────────────────────────────────


def _redact(text: str) -> tuple[str, Counter]:
    from .secret_scan import redact_for_sharing

    return redact_for_sharing(text)


def _shareable(text: str, kind: str = "mixed") -> str | None:
    """``text`` as the organization's DLP lets it leave (redactions applied); None to withhold it."""
    from . import dlp

    return dlp.shareable(text, kind)


def _without_query(url: str) -> str:
    """A web address without its query string and fragment (messages at ``redacted``)."""
    base, question, _query = url.partition("?")
    base = base.split("#", 1)[0]
    return base + ("?…" if question else "")


def _code_block(match: re.Match[str]) -> str:
    lines = match.group(0).rstrip("\n").split("\n")[1:]  # without the opening fence
    if lines and lines[-1].strip() == match.group(1):
        lines = lines[:-1]  # nor the closing one
    return f"[code block, {len(lines)} line{'s' if len(lines) != 1 else ''}]"


def _message(text: Any, level: str, kind: str = "mixed") -> tuple[dict | None, Counter, bool]:
    """A message at ``level``, secrets removed and DLP applied; its secret kinds counted, and whether DLP
    withheld or changed it.

    Secrets go, then the DLP rules for ``kind`` apply, before anything is
    cut: a secret or a match cut in half at the limit would no longer be
    recognized. A message DLP withholds reads ``WITHHELD``.
    """
    value = str(text or "")
    if not value.strip():
        return None, Counter(), False
    value, found = _redact(value)
    shared = _shareable(value, kind)
    if shared is None:
        return {"text": WITHHELD, "chars": len(str(text)), "truncated": False}, found, True
    touched = shared != value
    value = shared
    if level == "redacted":
        value = _CODE_BLOCK.sub(_code_block, value)
        value = _EMAIL.sub("[email]", value)
    limit = MESSAGE_LIMITS[level]
    truncated = len(value) > limit
    return ({"text": value[:limit] + ("…" if truncated else ""), "chars": len(str(text)), "truncated": truncated},
            found, touched)


class TurnTracker:
    """Watches one turn's events (Session.run) and records it when the turn ends."""

    def __init__(self, session: Any, user_msg: str, images: int, scope: Scope, input_origin: str = "human", *,
                 trigger: str = "app", unattended: bool = False) -> None:
        self.scope = scope
        # "generated": the runtime wrote this turn's message, not a person (Session.run).
        self.origin = "generated" if input_origin == "generated" else "human"
        self.settings = scope.settings
        self.trigger = trigger if trigger in TRIGGERS else "app"
        self.unattended = bool(unattended)
        self.os_user = os_user()
        self.started_at = _now()
        self.session_id = str(getattr(session, "audit_session_id", "") or "")
        if not self.session_id:
            self.session_id = getattr(session, "_oversight_session_id", "") or f"session-{uuid.uuid4().hex[:12]}"
            session._oversight_session_id = self.session_id
        self.title = str(getattr(session, "browser_session_name", "") or "")
        self.project_path = str(getattr(session, "project_path", "") or "")
        self.exclusions = getattr(session, "exclusions", None)
        self.provider = str(getattr(getattr(session, "backend", None), "name", "") or "")
        self.model = str(getattr(getattr(session, "backend", None), "model", "") or "")
        self.mode = str(getattr(session, "autonomy_tier", "") or "")
        # What the person typed, when the model gets it wrapped (gui/app.py).
        shown = getattr(session, "display_prompt", None)
        self.prompt = str(shown if shown else user_msg or "")
        self.images = int(images or 0)
        self.reply = ""
        self.calls: dict[str, dict] = {}
        self.tools: list[dict] = []
        self.flags: dict[tuple, Any] = {}
        self.model_redactions: Counter = Counter()
        self.files_changed = 0
        # DLP refused a request of this turn (dlp.Blocked, Session._run_turn).
        self.dlp_blocked = False
        self.usage_ids: set[str] = set()
        self.usage = {"requests": 0, "input_tokens": 0, "output_tokens": 0, "cost_usd": None}

    # ── Events ─────────────────────────────────────────────────────────────
    def observe(self, event: dict) -> None:
        """One engine event; cheap for the ones that don't matter (text deltas)."""
        kind = event.get("event")
        if kind == "text.delta":
            return
        try:
            if kind == "tool.call":
                self._call(event)
            elif kind == "tool.result":
                self._result(event)
            elif kind == "text.done" and not event.get("_subagent") and str(event.get("text") or "").strip():
                self.reply = str(event["text"])
            elif kind == "backend.status" and event.get("kind") == "secrets_redacted":
                self.model_redactions.update({str(k): int(v) for k, v in (event.get("kinds") or {}).items()})
            elif kind == "status" and isinstance(event.get("stats"), dict):
                self._usage(event["stats"])
            elif kind == "error" and event.get("code") == "dlp_blocked":
                self.dlp_blocked = True
        except Exception:
            logger.debug("Oversight couldn't read an event", exc_info=True)

    def _call(self, event: dict) -> None:
        call_id = str(event.get("call_id") or f"call-{len(self.calls)}")
        arguments = event.get("arguments") if isinstance(event.get("arguments"), dict) else {}
        known = self.calls.get(call_id)
        if known is not None:  # the same call, seen again with its full arguments
            known.update(arguments=arguments)
            return
        entry = {"name": str(event.get("name") or ""), "arguments": arguments, "status": "not_run",
                 "worker": bool(event.get("_subagent")), "external": bool(event.get("external"))}
        self.calls[call_id] = entry
        if len(self.tools) < MAX_TOOLS:
            self.tools.append(entry)

    def _result(self, event: dict) -> None:
        from . import security_flags

        call_id = str(event.get("call_id") or "")
        entry = self.calls.get(call_id) or {"name": str(event.get("name") or ""), "arguments": {},
                                            "worker": bool(event.get("_subagent")), "external": False}
        name = entry["name"] or str(event.get("name") or "")
        if event.get("denied"):
            entry["status"] = "denied"
            if self.settings.security_flags:
                self._add(security_flags.for_denial(name, entry["arguments"], event, worker=entry["worker"]))
            return
        entry["status"] = "error" if event.get("is_error") else "ok"
        if entry["status"] == "ok":
            if name in _WRITE_TOOLS:
                self.files_changed += 1
            self.files_changed += len([p for p in event.get("changed_files") or () if isinstance(p, str)])
        if self.settings.security_flags and name not in _NOT_SCANNED and not name.startswith("director_"):
            for flag in security_flags.for_tool_output(name, event.get("output"), external=entry["external"],
                                                       worker=entry["worker"]):
                self._add(flag)

    def _usage(self, stats: dict) -> None:
        usage_id = str(stats.get("_usage_id") or "")
        if usage_id:
            if usage_id in self.usage_ids:
                return
            self.usage_ids.add(usage_id)
        from . import usage

        counts = usage.token_counts(stats)
        self.usage["requests"] += 1
        self.usage["input_tokens"] += int(counts.get("input_tokens") or 0)
        self.usage["output_tokens"] += int(counts.get("output_tokens") or 0)
        cost = stats.get("cost_usd")
        if isinstance(cost, (int, float)) and not isinstance(cost, bool):
            self.usage["cost_usd"] = round((self.usage["cost_usd"] or 0.0) + float(cost), 6)

    def _add(self, flag: Any) -> None:
        if flag is not None and flag.key() not in self.flags and len(self.flags) < MAX_FLAGS_PER_TURN:
            self.flags[flag.key()] = flag

    # ── The record ─────────────────────────────────────────────────────────
    def _path(self, value: Any) -> str:
        """A tool's path: relative to the project, never an excluded file's name."""
        text = str(value or "")
        if not text:
            return ""
        full = text if os.path.isabs(text) or not self.project_path else os.path.join(self.project_path, text)
        try:
            if self.exclusions and self.exclusions.match(full):
                return "[excluded file]"
        except Exception:
            return "[excluded file]"
        if self.project_path:
            try:
                relative = os.path.relpath(full, self.project_path)
            except ValueError:  # another drive
                relative = ""
            if relative and not relative.startswith(".."):
                return relative.replace("\\", "/")
            return full if self.settings.project_paths else "[outside the project]"
        return full if self.settings.project_paths else os.path.basename(full)

    def _arguments(self, arguments: dict, found: Counter) -> dict:
        level = self.settings.messages
        limit = ARGUMENT_LIMITS[level]
        summary: dict[str, str] = {}
        # The model wrote them: DLP checks them as it does its output in requests.
        for key in _PATH_ARGUMENTS:
            if isinstance(arguments.get(key), str) and arguments[key]:
                shown = _shareable(self._path(arguments[key]), "model_output")
                summary[key] = WITHHELD if shown is None else shown
        for key in _TEXT_ARGUMENTS:
            value = arguments.get(key)
            if isinstance(value, list):
                value = " ".join(str(word) for word in value)
            if isinstance(value, str) and value:
                # The whole value loses its secrets, and meets DLP, before anything is cut from it.
                text, kinds = _redact(value)
                found.update(kinds)
                shown = _shareable(text, "model_output")
                if shown is None:
                    summary[key] = WITHHELD
                    continue
                text = shown
                if level == "redacted" and key == "url":
                    text = _without_query(text)
                summary[key] = text if len(text) <= limit else text[:limit] + "…"
        return summary

    def _session(self, *, title: bool = True) -> dict:
        from .security_flags import clip

        project = self.project_path
        name = (project if self.settings.project_paths else os.path.basename(project.rstrip("\\/"))) if project else ""
        session = {"id": self.session_id, "project": name, "surface": _surface(self.session_id)}
        # A title is conversation content (an automatic one is the first
        # message's gist): it goes only when messages do, at their level, and
        # not when DLP withholds it (clip checks it) or ``title`` is False.
        title = clip(self.title, TITLE_LIMIT) if title and self.settings.messages != "off" and self.title else ""
        if title:
            session["title"] = _EMAIL.sub("[email]", title) if self.settings.messages == "redacted" else title
        return session

    def _who(self) -> dict:
        """What started the turn and who ran it, on every record (turns and flags alike)."""
        return {"trigger": self.trigger, "unattended": self.unattended, "os_user": self.os_user}

    def finish(self, outcome: str) -> None:
        """Queue the turn's records; never raises."""
        try:
            self._finish(outcome)
        except Exception:
            logger.exception("Oversight couldn't record a turn")

    def _service_blocked(self) -> bool:
        """Whether a request of this turn was refused under a DLP service, which needn't say what it
        blocked: then none of the turn's text is shared (the rules alone are rechecked exactly)."""
        if not self.dlp_blocked:
            return False
        from . import dlp

        try:
            rules = dlp.active()
        except Exception:
            return True
        return rules is None or rules.service is not None

    def _finish(self, outcome: str) -> None:
        from . import security_flags

        settings = self.settings
        # No text from a turn a DLP service refused (the activity still goes).
        textless = self._service_blocked()
        messages: dict[str, Any] | None = None
        message_redactions: Counter = Counter()
        touched = False
        if settings.activity and settings.messages != "off":
            # As DLP reads them in requests: what the person typed (a message
            # Lumi wrote meets every rule) and the model's reply.
            user, user_found, touched = _message(self.prompt, settings.messages,
                                                 "mixed" if self.origin == "generated" else "prompt")
            reply, reply_found, _ = _message(self.reply, settings.messages, "model_output")
            if textless:
                user = {"text": WITHHELD, "chars": user["chars"], "truncated": False} if user else None
                reply = {"text": WITHHELD, "chars": reply["chars"], "truncated": False} if reply else None
            message_redactions.update(user_found)
            message_redactions.update(reply_found)
            messages = {"level": settings.messages, "user": user, "assistant": reply}
        # A session's title is the gist of its first message, shortened and
        # capitalized, where DLP's own checks can't recognize what it matched:
        # once DLP withheld or changed a message of the session, or a DLP
        # service refused one of its turns, its title stays out of every record.
        turn, title_withheld = _next_turn(self.session_id, withhold_title=textless or touched)
        session = self._session(title=not title_withheld)
        shared_redactions: Counter = Counter()
        record: dict[str, Any] | None = None
        if settings.activity:
            tools = []
            for entry in self.tools:
                item = {"name": entry["name"][:80], "status": entry["status"]}
                if entry["worker"]:
                    item["worker"] = True
                if settings.messages != "off" and not textless:
                    arguments = self._arguments(entry["arguments"], shared_redactions)
                    if arguments:
                        item["arguments"] = arguments
                tools.append(item)
            record = {
                "type": "turn", "id": uuid.uuid4().hex, "session": session, "turn": turn, **self._who(),
                "started_at": self.started_at, "ended_at": _now(), "provider": self.provider[:80],
                "model": self.model[:120], "mode": self.mode[:20], "outcome": outcome,
                "usage": dict(self.usage), "tools": tools, "tool_calls": len(self.calls),
                "files_changed": self.files_changed, "images": self.images,
            }
            if self.origin == "generated":
                record["origin"] = "generated"

            if messages is not None:
                shared_redactions.update(message_redactions)
                record["messages"] = messages
            if shared_redactions:
                record["redactions"] = dict(shared_redactions)
        flags: list[Any] = list(self.flags.values())
        if settings.security_flags:
            # One flag per turn for removed secrets, whichever step removed them.
            combined = Counter({kind: max(self.model_redactions[kind], shared_redactions[kind])
                                for kind in set(self.model_redactions) | set(shared_redactions)})
            secret = security_flags.for_redaction(combined, "before sending to the model or sharing")
            if secret is not None and len(flags) < MAX_FLAGS_PER_TURN:
                flags.append(secret)
        else:
            flags = []
        records = [record] if record is not None else []
        if record is not None and flags:
            record["flags"] = [flag.id for flag in flags]
        for flag in flags:
            entry = {"type": "flag", **flag.to_dict(), "session": session, "turn": turn, **self._who()}
            # Only a label leaves this computer as a flag's rule (security_flags.RULES).
            entry["rule"] = security_flags.label(entry["kind"], entry["rule"])
            # The excerpt met DLP whole when the flag was made; checked again for
            # what a DLP service decided since (it judges tool output when the
            # next request carries it).
            excerpt = "" if textless else (_shareable(entry.get("excerpt") or "") or "")
            if settings.messages == "off":
                entry.pop("excerpt", None)
            elif settings.messages == "redacted":
                entry["excerpt"] = _EMAIL.sub("[email]", excerpt)
            else:
                entry["excerpt"] = excerpt
            records.append(entry)
        if flags:
            _save_flags([{**flag.to_dict(), "session": session, "turn": turn} for flag in flags])
        if records:
            enqueue(records, retention_days=settings.retention_days)
            wake()


def _surface(session_id: str) -> str:
    for prefix, name in SURFACES:
        if session_id.startswith(prefix):
            return name
    return "app"


def begin_turn(session: Any, user_msg: str, *, images: int = 0, input_origin: str = "human",
               admission: Admission | None = None) -> TurnTracker | None:
    """A tracker for this turn when oversight records it, else None; never raises.

    ``admission`` is ``admit(session)``, which ``Session.run`` already asked;
    a turn it refused never gets here. A delegated worker's turn is part of
    its parent's, which sees the worker's tool calls, so workers aren't
    recorded on their own.
    """
    try:
        if getattr(session, "is_subagent", False):
            return None
        admission = admission if admission is not None else admit(session)
        if admission.refusal or admission.scope is None:
            return None
        return TurnTracker(session, user_msg, images, admission.scope, input_origin, trigger=admission.trigger,
                           unattended=admission.unattended)
    except Exception:
        logger.exception("Oversight couldn't start recording a turn")
        return None
    finally:
        # The typed text belongs to this turn only.
        if getattr(session, "display_prompt", None):
            session.display_prompt = None


def _next_turn(session_id: str, *, withhold_title: bool = False) -> tuple[int, bool]:
    """The session's next turn number, and whether its title is withheld (both kept per session, across
    restarts); ``withhold_title`` withholds it from now on."""
    path = _root() / "sessions.json"
    with _lock, exclusive(_root() / ".lock"):
        known = _read_json(path, {})
        known = known if isinstance(known, dict) else {}
        entry = known.get(session_id) if isinstance(known.get(session_id), dict) else {}
        turn = int(entry.get("turns") or 0) + 1
        withheld = bool(entry.get("title_withheld")) or withhold_title
        known[session_id] = {"turns": turn, "at": _now(), **({"title_withheld": True} if withheld else {})}
        if len(known) > KNOWN_SESSIONS:
            newest = sorted(known.items(), key=lambda item: str(item[1].get("at") or ""), reverse=True)
            known = dict(newest[:KNOWN_SESSIONS])
        _write_json(path, known)
    return turn, withheld


# ── The person's own flags ──────────────────────────────────────────────────


def _save_flags(flags: list[dict]) -> None:
    path = _root() / "flags.jsonl"
    with _lock, exclusive(_root() / ".lock"):
        path.parent.mkdir(parents=True, exist_ok=True)
        lines = []
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            pass
        lines += [json.dumps(flag, separators=(",", ":")) for flag in flags]
        path.write_text("\n".join(lines[-LOCAL_FLAGS:]) + "\n", encoding="utf-8")


def recent_flags(limit: int = 50) -> list[dict]:
    """The flags raised on this computer, newest first."""
    try:
        lines = (_root() / "flags.jsonl").read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    flags = []
    for line in reversed(lines):
        try:
            flag = json.loads(line)
        except ValueError:
            continue
        if isinstance(flag, dict):
            flags.append(flag)
        if len(flags) >= limit:
            break
    return flags


# ── The queue ───────────────────────────────────────────────────────────────
#
# A SQLite table rather than a file rewritten per turn and per batch: adding
# a record, taking a batch and removing a sent one don't read the rest of the
# queue, so a long time offline doesn't slow every turn's end. The app,
# `lumi run`, the terminal UI and the chat gateway share it; SQLite's own
# locking serializes them. Records, and confirmations of the notice in their
# own table, are claimed before sending, so two processes never send one
# twice; a claim older than SENDING_SECONDS (its sender stopped) is taken over.


def _counters() -> dict:
    data = _read_json(_root() / "state.json", {})
    return data if isinstance(data, dict) else {}


def _count(**changes: Any) -> None:
    """Add to (or set) the queue's counters; call with the lock held."""
    data = _counters()
    for key, value in changes.items():
        if isinstance(value, int) and not isinstance(value, bool):
            data[key] = int(data.get(key) or 0) + value
        else:
            data[key] = value
    data.pop("pending", None)  # counted from the queue itself (queue_status)
    _write_json(_root() / "state.json", data)


def _has_column(connection: sqlite3.Connection, table: str, column: str) -> bool:
    return any(row[1] == column for row in connection.execute(f"PRAGMA table_info({table})"))


def _database() -> sqlite3.Connection:
    path = _root() / "queue.sqlite3"
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=30.0, isolation_level=None)
    # sending_until: when the claim of the process sending the record runs
    # out (_claim_batch); '' while nobody is sending it.
    connection.execute("CREATE TABLE IF NOT EXISTS records (seq INTEGER PRIMARY KEY AUTOINCREMENT, "
                       "id TEXT UNIQUE NOT NULL, at TEXT NOT NULL, size INTEGER NOT NULL, body TEXT NOT NULL, "
                       "sending_until TEXT NOT NULL DEFAULT '')")
    if not _has_column(connection, "records", "sending_until"):
        # A queue from before records were claimed.
        try:
            connection.execute("ALTER TABLE records ADD COLUMN sending_until TEXT NOT NULL DEFAULT ''")
        except sqlite3.OperationalError:
            if not _has_column(connection, "records", "sending_until"):  # not another process adding it too
                raise
    connection.execute("CREATE INDEX IF NOT EXISTS records_at ON records (at)")
    # state: pending (to send), sending (claimed by a process), sent, refused
    # (Lumi Cloud said no) or not_sent (this computer left before it went).
    connection.execute("CREATE TABLE IF NOT EXISTS acknowledgments (id TEXT PRIMARY KEY, at TEXT NOT NULL, "
                       "body TEXT NOT NULL, state TEXT NOT NULL, cloud_id TEXT NOT NULL DEFAULT '', "
                       "error TEXT NOT NULL DEFAULT '', updated TEXT NOT NULL DEFAULT '')")
    return connection


def _record_time(record: dict) -> str:
    """When the record's turn ended or its flag was raised, as a sortable UTC time (never in the future)."""
    value = record.get("ended_at") if record.get("type") == "turn" else record.get("at")
    now = datetime.now(timezone.utc)
    try:
        moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        moment = moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)
    except ValueError:
        moment = now
    moment = min(moment.astimezone(timezone.utc), now)
    return moment.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _pending(connection: sqlite3.Connection) -> int:
    return int(connection.execute("SELECT COUNT(*) FROM records").fetchone()[0])


def _expire_locked(connection: sqlite3.Connection, retention_days: int) -> int:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=int(retention_days))).isoformat(
        timespec="milliseconds").replace("+00:00", "Z")
    return connection.execute("DELETE FROM records WHERE at < ?", (cutoff,)).rowcount or 0


def enqueue(records: list[dict], *, retention_days: int | None = None) -> None:
    """Add records for Lumi Cloud; the oldest go (counted) when the queue is full.

    With ``retention_days``, records older than the organization keeps
    records go too: Lumi Cloud would only delete them.
    """
    with _lock, exclusive(_root() / ".lock"), closing(_database()) as connection:
        connection.execute("BEGIN IMMEDIATE")
        try:
            for record in records:
                body = json.dumps(record, separators=(",", ":"))
                connection.execute("INSERT OR IGNORE INTO records (id, at, size, body) VALUES (?, ?, ?, ?)",
                                   (str(record.get("id") or uuid.uuid4().hex), _record_time(record),
                                    len(body.encode("utf-8")), body))
            expired = _expire_locked(connection, retention_days) if retention_days else 0
            count, size = connection.execute("SELECT COUNT(*), COALESCE(SUM(size), 0) FROM records").fetchone()
            dropped = 0
            if count > MAX_RECORDS or size > MAX_QUEUE_BYTES:
                oldest = []
                for seq, length in connection.execute("SELECT seq, size FROM records ORDER BY seq"):
                    if count <= MAX_RECORDS and size <= MAX_QUEUE_BYTES:
                        break
                    oldest.append(seq)
                    count, size = count - 1, size - length
                for start in range(0, len(oldest), 500):
                    chunk = oldest[start:start + 500]
                    connection.execute(f"DELETE FROM records WHERE seq IN ({','.join('?' * len(chunk))})", chunk)
                dropped = len(oldest)
            connection.execute("COMMIT")
        except BaseException:
            connection.execute("ROLLBACK")
            raise
        if dropped:
            logger.warning("The oversight queue is full; dropped the %d oldest records", dropped)
        _count(dropped=dropped, recorded=len(records), expired=expired)


def expire(retention_days: int) -> int:
    """Delete queued records older than the organization keeps them; how many (counted in Settings)."""
    root = _root()
    if not (root / "queue.sqlite3").exists():
        return 0
    with _lock, exclusive(root / ".lock"), closing(_database()) as connection:
        expired = _expire_locked(connection, retention_days)
        if expired:
            _count(expired=expired)
    return expired


def _parsed(body: str) -> dict | None:
    """A queued record, or None when it can't be read or has no id (it's never sent)."""
    try:
        record = json.loads(body)
    except ValueError:
        return None
    return record if isinstance(record, dict) and record.get("id") else None


def queued_records(limit: int | None = None) -> list[dict]:
    """The records waiting to be sent, oldest first (those a process is sending too)."""
    if not (_root() / "queue.sqlite3").exists():
        return []
    with closing(_database()) as connection:
        rows = connection.execute("SELECT body FROM records ORDER BY seq" + (" LIMIT ?" if limit else ""),
                                  (limit,) if limit else ()).fetchall()
    parsed = (_parsed(body) for (body,) in rows)
    return [record for record in parsed if record is not None]


def discard(reason: str) -> int:
    """Delete everything queued (oversight ended); returns how many, which Settings shows."""
    if not (_root() / "queue.sqlite3").exists():
        return 0
    with _lock, exclusive(_root() / ".lock"), closing(_database()) as connection:
        count = connection.execute("DELETE FROM records").rowcount or 0
        if not count:
            return 0
        _count(discarded=count, last_discard_reason=str(reason)[:300], last_discard=_now())
    from . import audit

    audit.record("oversight.discarded", records=count, reason=str(reason)[:300])
    return count


def queue_status() -> dict:
    data = _counters()
    info = {key: data.get(key, default) for key, default in (
        ("recorded", 0), ("uploaded", 0), ("dropped", 0), ("discarded", 0), ("rejected", 0), ("expired", 0),
        ("last_upload", ""), ("last_error", ""), ("last_discard_reason", ""), ("next_attempt", ""))}
    try:
        if (_root() / "queue.sqlite3").exists():
            with closing(_database()) as connection:
                info["pending"] = _pending(connection)
        else:
            info["pending"] = 0
    except sqlite3.Error:
        logger.warning("Couldn't count the oversight queue", exc_info=True)
        info["pending"] = 0
    return info


# ── Confirmations waiting for Lumi Cloud ────────────────────────────────────


def _queue_acknowledgment(ack_id: str, record: dict, signature: str) -> None:
    """Keep a confirmation's record and signature, to send; the oldest settled ones go past KEPT_ACKNOWLEDGMENTS."""
    body = json.dumps({"record": record, "signature": signature}, separators=(",", ":"), sort_keys=True)
    with closing(_database()) as connection:
        connection.execute("BEGIN IMMEDIATE")
        try:
            connection.execute("INSERT OR REPLACE INTO acknowledgments (id, at, body, state, error, updated) "
                               "VALUES (?, ?, ?, 'pending', '', ?)", (ack_id, _now(), body, _now()))
            connection.execute("DELETE FROM acknowledgments WHERE state IN ('sent', 'refused', 'not_sent') AND id "
                               "NOT IN (SELECT id FROM acknowledgments ORDER BY at DESC LIMIT ?)",
                               (KEPT_ACKNOWLEDGMENTS,))
            connection.execute("COMMIT")
        except BaseException:
            connection.execute("ROLLBACK")
            raise


def acknowledgment_upload(ack_id: str) -> dict:
    """Whether Lumi Cloud has a confirmation: {state, id (Lumi Cloud's), error, at, signed}."""
    if not ack_id or not (_root() / "queue.sqlite3").exists():
        return {"state": "unknown"}
    try:
        with closing(_database()) as connection:
            row = connection.execute("SELECT state, cloud_id, error, updated, body FROM acknowledgments WHERE id = ?",
                                     (ack_id,)).fetchone()
    except sqlite3.Error:
        logger.warning("Couldn't read an oversight acknowledgment", exc_info=True)
        return {"state": "unknown"}
    if row is None:
        return {"state": "unknown"}
    state, cloud_id, error, updated, body = row
    try:
        signed = bool(json.loads(body).get("signature"))
    except (ValueError, AttributeError):
        signed = False
    # A claim in progress is still waiting, as far as the person can tell.
    return {"state": "pending" if state == "sending" else state, "id": cloud_id, "error": error, "at": updated,
            "signed": signed}


def acknowledgments_waiting() -> int:
    """Confirmations not yet sent to Lumi Cloud (nor refused)."""
    if not (_root() / "queue.sqlite3").exists():
        return 0
    with closing(_database()) as connection:
        return int(connection.execute("SELECT COUNT(*) FROM acknowledgments WHERE state IN ('pending', 'sending')")
                   .fetchone()[0])


def _claim_acknowledgments(limit: int) -> list[tuple[str, str]]:
    """(id, body) of confirmations to send now, claimed so no other process sends them too."""
    if not (_root() / "queue.sqlite3").exists():
        return []
    stale = (datetime.now(timezone.utc) - timedelta(seconds=SENDING_SECONDS)).isoformat(
        timespec="milliseconds").replace("+00:00", "Z")
    with closing(_database()) as connection:
        connection.execute("BEGIN IMMEDIATE")
        try:
            rows = connection.execute("SELECT id, body FROM acknowledgments WHERE state = 'pending' OR "
                                      "(state = 'sending' AND updated < ?) ORDER BY at LIMIT ?",
                                      (stale, limit)).fetchall()
            for ack_id, _body in rows:
                connection.execute("UPDATE acknowledgments SET state = 'sending', updated = ? WHERE id = ?",
                                   (_now(), ack_id))
            connection.execute("COMMIT")
        except BaseException:
            connection.execute("ROLLBACK")
            raise
    return [(str(ack_id), str(body)) for ack_id, body in rows]


def _settle_acknowledgment(ack_id: str, state: str, *, error: str = "", cloud_id: str = "",
                           body: str | None = None) -> None:
    with closing(_database()) as connection:
        if body is None:
            connection.execute("UPDATE acknowledgments SET state = ?, error = ?, cloud_id = ?, updated = ? "
                               "WHERE id = ?", (state, str(error)[:300], str(cloud_id)[:200], _now(), ack_id))
        else:
            connection.execute("UPDATE acknowledgments SET state = ?, error = ?, cloud_id = ?, updated = ?, body = ? "
                               "WHERE id = ?", (state, str(error)[:300], str(cloud_id)[:200], _now(), body, ack_id))


def _upload_acknowledgments(client: Any) -> str:
    """Send confirmations to Lumi Cloud: "idle", or "retry" when one should be tried again later.

    A 409 (a notice the organization's policy didn't produce) or 422 (a
    signature that doesn't verify) is a refusal Settings shows, never sent
    again. A confirmation made under another enrollment isn't sent to this one.
    """
    from . import audit, policy
    from .cloud import CloudError

    outcome = "idle"
    for ack_id, raw in _claim_acknowledgments(ACKNOWLEDGMENTS_PER_STEP):
        try:
            body = json.loads(raw)
            record, signature = dict(body["record"]), str(body.get("signature") or "")
        except (ValueError, KeyError, TypeError):
            _settle_acknowledgment(ack_id, "refused", error="The saved record couldn't be read.")
            continue
        device = policy.enrolled_device()
        if not device or str(device.get("id") or "") != str(record.get("device_id") or ""):
            _settle_acknowledgment(ack_id, "not_sent", error="This computer left the organization before Lumi "
                                                             "Cloud received it.")
            continue
        if not signature:
            # Every confirmation is signed when it's made (acknowledge); an unsigned one isn't one.
            _settle_acknowledgment(ack_id, "not_sent", error="It wasn't signed with this computer's key.")
            continue
        try:
            answer = client.device_call("POST", ACKNOWLEDGMENT_PATH, json={"record": record, "signature": signature})
        except CloudError as exc:
            status = int(getattr(exc, "status", 0) or 0)
            # Refusals sending again won't change: shown in Settings, never resent.
            if status in (400, 403, 409, 413, 422) or exc.code in ("notice_mismatch", "invalid_signature",
                                                                   "invalid_request", "oversight_off"):
                _settle_acknowledgment(ack_id, "refused", error=f"Lumi Cloud refused it ({exc.code}): {exc}")
                audit.record("oversight.acknowledgment_refused", acknowledgment=ack_id, code=str(exc.code)[:60])
                continue
            _settle_acknowledgment(ack_id, "pending", error=str(exc))
            with _lock, exclusive(_root() / ".lock"):
                _count(last_error=str(exc)[:300])
            outcome = "retry"
            continue
        _settle_acknowledgment(ack_id, "sent", cloud_id=str((answer or {}).get("id") or ""))
    return outcome


# ── Sending ─────────────────────────────────────────────────────────────────


def _claim_batch() -> tuple[list[dict], str]:
    """The next batch to send, oldest first, claimed so no other process sends it too; and the claim.

    Up to BATCH_RECORDS records and BATCH_BYTES (the first goes whatever its
    size). Records another process claimed are skipped until its claim runs
    out, SENDING_SECONDS after it was made: a sender that stopped or hung
    doesn't keep them. The claim is when it runs out (``sending_until``).
    """
    if not (_root() / "queue.sqlite3").exists():
        return [], ""
    claim = (datetime.now(timezone.utc) + timedelta(seconds=SENDING_SECONDS)).isoformat(
        timespec="milliseconds").replace("+00:00", "Z")
    batch: list[dict] = []
    size = 0
    with _lock, exclusive(_root() / ".lock"), closing(_database()) as connection:
        connection.execute("BEGIN IMMEDIATE")
        try:
            # Unclaimed ('', before every time) or claimed by a sender whose claim ran out.
            rows = connection.execute("SELECT body, size FROM records WHERE sending_until < ? ORDER BY seq LIMIT ?",
                                      (_now(), BATCH_RECORDS)).fetchall()
            for body, length in rows:
                record = _parsed(body)
                if record is None:
                    continue
                if batch and size + length > BATCH_BYTES:
                    break
                batch.append(record)
                size += length
            ids = [str(record["id"]) for record in batch]
            for start in range(0, len(ids), 500):
                chunk = ids[start:start + 500]
                connection.execute(f"UPDATE records SET sending_until = ? WHERE id IN "
                                   f"({','.join('?' * len(chunk))})", [claim, *chunk])
            connection.execute("COMMIT")
        except BaseException:
            connection.execute("ROLLBACK")
            raise
    return batch, claim


def _release(batch: list[dict], claim: str) -> None:
    """Give up this process's claim on a batch it couldn't send, so the next try (anyone's) sends it at once.

    Only while the claim is still this one: once it ran out, another process
    may have claimed the records again.
    """
    ids = [str(record["id"]) for record in batch]
    with _lock, exclusive(_root() / ".lock"), closing(_database()) as connection:
        for start in range(0, len(ids), 500):
            chunk = ids[start:start + 500]
            connection.execute(f"UPDATE records SET sending_until = '' WHERE sending_until = ? AND id IN "
                               f"({','.join('?' * len(chunk))})", [claim, *chunk])


def _remove(ids: set[str], *, outcome: str, error: str = "") -> None:
    """Delete records Lumi Cloud took (``uploaded``) or refused (``rejected``) and count them.

    Only records still queued are counted: one deleted meanwhile (discarded,
    expired, dropped) was counted then, and one sent again after its claim
    ran out is counted by whichever sender deletes it first.
    """
    with _lock, exclusive(_root() / ".lock"), closing(_database()) as connection:
        listed = sorted(ids)
        removed = 0
        for start in range(0, len(listed), 500):
            chunk = listed[start:start + 500]
            removed += connection.execute(f"DELETE FROM records WHERE id IN ({','.join('?' * len(chunk))})",
                                          chunk).rowcount or 0
        changes: dict[str, Any] = {outcome: removed}
        if outcome == "uploaded":
            changes.update(last_upload=_now(), last_error="")
        else:
            changes.update(last_error=error)
        _count(**changes)


class _OversightOff(Exception):
    """Lumi Cloud says the organization doesn't have oversight on."""


def _send(client: Any, batch: list[dict], version: Any) -> None:
    """Send one batch; a batch too large is sent in halves, and a record Lumi Cloud refuses is counted.

    Raises _OversightOff, or CloudError for failures worth retrying.
    """
    from .cloud import CloudError

    try:
        client.device_call("POST", UPLOAD_PATH, json={
            "policy_version": version if isinstance(version, int) and not isinstance(version, bool) else None,
            "events": batch})
    except CloudError as exc:
        if exc.code == "oversight_off":
            raise _OversightOff(str(exc)) from exc
        if exc.code in ("too_large", "413") and len(batch) > 1:
            middle = len(batch) // 2
            _send(client, batch[:middle], version)
            _send(client, batch[middle:], version)
            return
        if exc.code in ("invalid_request", "too_large", "400", "413"):
            # Sending it again won't change the answer; it's counted, not lost silently.
            _remove({record["id"] for record in batch}, outcome="rejected",
                    error=f"Lumi Cloud refused {len(batch)} record{'s' if len(batch) != 1 else ''}: {exc}")
            return
        raise
    _remove({record["id"] for record in batch}, outcome="uploaded")


def _upload_records(client: Any, max_batches: int) -> str:
    from . import policy
    from .cloud import CloudError

    for _ in range(max_batches):
        if not queued_records(1):
            return "idle"
        scope = Scope()
        if not scope.configured:
            discard("Your organization's policy no longer asks for oversight.")
            return "discarded"
        if not scope.destination:
            discard(scope.reason)
            return "discarded"
        expire(scope.settings.retention_days)
        batch, claim = _claim_batch()
        if not batch:
            return "idle"  # nothing, or only what another process is sending
        try:
            state = policy.load()
            version = state.policy.raw.get("policy_version") if state.policy else None
            _send(client, batch, version)
        except _OversightOff as exc:
            discard(f"{scope.organization}'s Lumi Cloud doesn't have oversight on ({exc}).")
            return "discarded"
        except CloudError as exc:
            _release(batch, claim)
            with _lock, exclusive(_root() / ".lock"):
                _count(last_error=str(exc)[:300])
            return "retry"
        except BaseException:
            _release(batch, claim)
            raise
    return "more"


def upload_pending(client: Any, *, max_batches: int = 20) -> str:
    """Send what's waiting to Lumi Cloud: "idle", "more", "discarded" or "retry".

    Confirmations of the notice go first. Records: the policy is checked
    before every batch, so nothing goes once the organization stops asking
    for it or the computer leaves, and nothing older than the organization
    keeps records.
    """
    acknowledgments = _upload_acknowledgments(client)
    records = _upload_records(client, max_batches)
    return "retry" if "retry" in (acknowledgments, records) else records


class _Uploader:
    """Sends the queue in the background for the process's lifetime; never on the UI's thread."""

    def __init__(self, client: Any) -> None:
        self.client = client
        self.wakeup = threading.Event()
        self.failures = 0
        self.thread = threading.Thread(target=self._loop, daemon=True, name="lumi-oversight-upload")
        self.thread.start()

    def _loop(self) -> None:
        delay = BUSY_SECONDS
        while True:
            self.wakeup.wait(delay)
            self.wakeup.clear()
            delay = self.step()

    def wake(self, *, urgent: bool = False) -> None:
        """Send soon; while it backs off after a failure, only an urgent wake (a policy change) cuts the wait."""
        if urgent or not self.failures:
            self.wakeup.set()

    def step(self) -> float:
        try:
            outcome = upload_pending(self.client)
        except Exception as exc:
            logger.exception("Sending oversight records failed")
            with _lock, exclusive(_root() / ".lock"):
                _count(last_error=f"Sending failed: {exc}"[:300])
            outcome = "retry"
        if outcome == "retry":
            delay = RETRY_SECONDS[min(self.failures, len(RETRY_SECONDS) - 1)]
            self.failures += 1
            with _lock, exclusive(_root() / ".lock"):
                _count(next_attempt=datetime.fromtimestamp(time.time() + delay, timezone.utc)
                       .isoformat(timespec="seconds").replace("+00:00", "Z"))
            return delay
        if self.failures:
            with _lock, exclusive(_root() / ".lock"):
                _count(next_attempt="")
        self.failures = 0
        return BUSY_SECONDS if outcome == "more" else IDLE_SECONDS


_uploader: _Uploader | None = None


def start_uploader(client: Any) -> None:
    """Send queued records and confirmations to Lumi Cloud from a background thread (once per process)."""
    global _uploader
    with _lock:
        if _uploader is None:
            _uploader = _Uploader(client)


def wake(*, urgent: bool = False) -> None:
    """A record was queued (send soon), or the policy changed (``urgent``: check now, even while backing off)."""
    uploader = _uploader
    if uploader is not None:
        waker = getattr(uploader, "wake", None)
        if callable(waker):
            waker(urgent=urgent)
        else:
            uploader.wakeup.set()


def set_uploader_for_tests(uploader: Any) -> None:
    global _uploader
    _uploader = uploader


def flush(client_factory: Callable[[], Any], *, seconds: float = 10.0) -> None:
    """Try to send what's waiting before a short-lived process exits (``lumi run``); never raises."""
    try:
        if not queued_records(1) and not acknowledgments_waiting():
            return
        deadline = time.monotonic() + seconds
        client = client_factory()
        while time.monotonic() < deadline:
            if upload_pending(client, max_batches=1) != "more":
                return
    except Exception:
        logger.debug("Couldn't send oversight records before exiting", exc_info=True)
