"""Organization oversight: the activity, messages and security flags an organization asks for.

Nothing here runs unless the organization's policy turns it on (the
``oversight`` section, lumi/policy.py). Then, for every turn that goes through
``Session.run`` (the app, ``lumi run``, the terminal UI, the chat gateway,
tasks from chat), Lumi builds a record of the turn and queues it for the
organization's Lumi Cloud:

* **activity**: the session, the project's folder name (its full path only
  with ``project_paths``), the turn's number, when it ran, the provider and
  model, the permission mode, how it ended, the tools it called and whether
  each ran, and what it cost;
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

**The person always knows.** The app shows a notice naming the organization
and what it receives, which can't be dismissed, and Settings lists exactly
what is collected. Lumi records nothing until the person has confirmed the
notice for the policy in force (``acknowledge``): with the notice's own
button in the app, or by running ``lumi run`` or the terminal UI at an
interactive terminal, which print it first. Its fingerprint covers the
organization, this computer's enrollment and what the policy collects, so
another enrollment or a policy that collects more needs it confirmed again,
and leaving the organization or signing out of Lumi Cloud forgets it (it
never records anything because of what nobody saw). People who reach Lumi
through the chat gateway are sent the notice in their chat before their
first turn is recorded (``chat_notice``); until then their turns aren't.

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
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from .file_lock import exclusive

logger = logging.getLogger(__name__)

UPLOAD_PATH = "/api/v1/oversight/events"
MESSAGE_LIMITS = {"redacted": 2_000, "full": 20_000}
ARGUMENT_LIMITS = {"redacted": 300, "full": 2_000}
TITLE_LIMIT = 200
MAX_TOOLS = 100
MAX_FLAGS_PER_TURN = 20
MAX_RECORDS = 5_000
MAX_QUEUE_BYTES = 20 * 1024 * 1024
BATCH_RECORDS = 100
BATCH_BYTES = 900_000
LOCAL_FLAGS = 500
KNOWN_SESSIONS = 2_000
KNOWN_CHATS = 2_000
IDLE_SECONDS = 300.0
BUSY_SECONDS = 2.0
RETRY_SECONDS = (30.0, 60.0, 120.0, 300.0, 900.0, 1800.0, 3600.0)
# Where a session's turns come from, by the audit session id the surface sets.
SURFACES = (("headless:", "lumi run"), ("gateway:", "chat gateway"), ("chat-task:", "task from chat"),
            ("tui:", "terminal"))
# Sessions whose people aren't this computer's person: each chat of the chat
# gateway is told in the chat (chat_notice) before its turns are recorded.
CHAT_PREFIX = "gateway:"
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


# ── What the policy asks for ────────────────────────────────────────────────


class Scope:
    """The oversight in force here and now, and whether Lumi records it."""

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
        if not self.configured:
            return
        device = policy.enrolled_device()
        # What the notice said, and to which organization and enrollment of
        # this computer records go: any of them changing needs the notice again.
        self.fingerprint = hashlib.sha256(policy.canonical({
            "organization": self.organization,
            "organization_id": str(device.get("organization_id") or ""),
            "device": str(device.get("id") or ""),
            "oversight": self.settings.summary()})).hexdigest()[:16]
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
    def acknowledged(self) -> bool:
        if not self.configured:
            return False
        shown = _read_json(_root() / "notice.json", {})
        return isinstance(shown, dict) and shown.get("fingerprint") == self.fingerprint

    @property
    def active(self) -> bool:
        """Whether turns are recorded now: asked for, a destination, and the notice confirmed."""
        return self.configured and self.destination and self.acknowledged


def notice_text(settings: Any, organization: str, *, where: str = "from Lumi on this computer") -> str:
    """One sentence for the notice beside the message box (and a chat's)."""
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
    """What Settings lists: what is shared, what never is, and for how long."""
    who = organization or "Your organization"
    shared = []
    if settings.activity:
        where = "the project's full path" if settings.project_paths else "the project folder's name"
        shared.append(f"Each turn's activity: the session, {where}, the model and permission mode, when it ran, "
                      "how it ended, which tools ran or were refused, and its cost.")
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
    return {"shared": shared, "not_shared": not_shared,
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
            "organization": scope.organization,
            "settings": settings.summary(),
            "fingerprint": scope.fingerprint,
            "acknowledged": scope.acknowledged,
            "reason": scope.reason,
            # The policy asks for oversight this Lumi can't honor, so it's off (policy.Oversight.error).
            "policy_error": scope.error,
            "notice": notice_text(settings, scope.organization) if scope.configured else "",
            "organization_notice": settings.notice if scope.configured else "",
            **(described(settings, scope.organization) if scope.configured else {}),
        }
    except Exception as exc:  # the page still shows the policy's error elsewhere
        logger.exception("Oversight status failed")
        info = {"configured": False, "active": False, "error": str(exc)}
    info["queue"] = queue_status()
    info["flags"] = [{**flag, "rule_text": rule_text(flag.get("rule"))} for flag in recent_flags(50)]
    return info


def acknowledge(fingerprint: str, surface: str) -> bool:
    """The person confirmed the notice for the policy in force; turns are recorded from now on.

    Refused (False) when ``fingerprint`` isn't the policy in force (a page
    that showed an older notice doesn't start a newer policy's collection),
    or when records have nowhere to go: that notice says nothing is
    collected, so confirming it can't start collection later.
    """
    scope = Scope()
    if not (scope.configured and scope.destination) or not fingerprint or fingerprint != scope.fingerprint:
        return False
    with _lock:
        if scope.acknowledged:
            return True
        _write_json(_root() / "notice.json", {"fingerprint": fingerprint, "organization": scope.organization,
                                              "surface": str(surface)[:40], "shown_at": _now()})
    from . import audit

    audit.record("oversight.notice_shown", organization=scope.organization, surface=str(surface)[:40],
                 **{key: value for key, value in scope.settings.summary().items() if key not in ("notice", "error")})
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


def terminal_notice(interactive: bool, surface: str) -> str:
    """The notice for a terminal surface, confirmed if someone is there to read it; '' when off."""
    try:
        scope = Scope()
        if not scope.configured:
            return scope.error  # a section this Lumi can't honor: say so, nothing is collected
        text = notice_text(scope.settings, scope.organization)
        if scope.settings.notice:
            text += f" {scope.settings.notice}"
        if scope.reason:
            text += f" ({scope.reason})"
        elif interactive:
            acknowledge(scope.fingerprint, surface)
        elif not scope.acknowledged:
            text += " Nothing is recorded until you've confirmed this notice in the Lumi app or at a terminal."
        return text
    except Exception:
        logger.debug("Oversight notice failed", exc_info=True)
        return ""


# ── The chat gateway's chats ────────────────────────────────────────────────


def _chat_key(session: Any) -> str:
    key = str(getattr(session, "audit_session_id", "") or "")
    return key if key.startswith(CHAT_PREFIX) else ""


def _chat_notified(chat: str, fingerprint: str) -> bool:
    known = _read_json(_root() / "chats.json", {})
    entry = known.get(chat) if isinstance(known, dict) else None
    return bool(fingerprint) and isinstance(entry, dict) and entry.get("fingerprint") == fingerprint


def chat_notice(chat: str) -> tuple[str, str] | None:
    """(text, fingerprint): the notice a gateway chat must be sent before its turns are recorded.

    None when nothing would be recorded (no policy asks, or there's nowhere
    to send records) or the chat was already sent this policy's notice. The
    person running the gateway saw its notice at their terminal; the people
    in a chat are told in the chat. Never raises.
    """
    try:
        scope = Scope()
        if not (scope.configured and scope.destination) or _chat_notified(chat, scope.fingerprint):
            return None
        text = notice_text(scope.settings, scope.organization, where="from Lumi through this chat")
        if scope.settings.notice:
            text += f" {scope.settings.notice}"
        text += " Nothing from this chat was shared before this message."
        return f"Organization oversight: {text}", scope.fingerprint
    except Exception:
        logger.debug("Oversight chat notice failed", exc_info=True)
        return None


def chat_notice_sent(chat: str, fingerprint: str) -> None:
    """The chat was sent the notice for ``fingerprint``: its turns are recorded from now on."""
    path = _root() / "chats.json"
    with _lock, exclusive(_root() / ".lock"):
        known = _read_json(path, {})
        known = known if isinstance(known, dict) else {}
        known[str(chat)[:200]] = {"fingerprint": str(fingerprint), "at": _now()}
        if len(known) > KNOWN_CHATS:
            newest = sorted(known.items(), key=lambda item: str(item[1].get("at") or ""), reverse=True)
            known = dict(newest[:KNOWN_CHATS])
        _write_json(path, known)
    from . import audit

    scope = Scope()
    audit.record("oversight.notice_shown", organization=scope.organization, surface="chat gateway",
                 session=str(chat)[:200],
                 **{key: value for key, value in scope.settings.summary().items() if key not in ("notice", "error")})


# ── Recording a turn ────────────────────────────────────────────────────────


def _redact(text: str) -> tuple[str, Counter]:
    from .secret_scan import redact_for_sharing

    return redact_for_sharing(text)


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


def _message(text: Any, level: str) -> tuple[dict | None, Counter]:
    """A message at ``level``, secrets removed; its secret kinds counted.

    Secrets go before anything is cut: a secret cut in half at the limit
    would match neither its pattern nor its saved value.
    """
    value = str(text or "")
    if not value.strip():
        return None, Counter()
    value, found = _redact(value)
    if level == "redacted":
        value = _CODE_BLOCK.sub(_code_block, value)
        value = _EMAIL.sub("[email]", value)
    limit = MESSAGE_LIMITS[level]
    truncated = len(value) > limit
    return {"text": value[:limit] + ("…" if truncated else ""), "chars": len(str(text)), "truncated": truncated}, found


class TurnTracker:
    """Watches one turn's events (Session.run) and records it when the turn ends."""

    def __init__(self, session: Any, user_msg: str, images: int, scope: Scope, input_origin: str = "human") -> None:
        self.scope = scope
        # "generated": the runtime wrote this turn's message, not a person (Session.run).
        self.origin = "generated" if input_origin == "generated" else "human"
        self.settings = scope.settings
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
        for key in _PATH_ARGUMENTS:
            if isinstance(arguments.get(key), str) and arguments[key]:
                summary[key] = self._path(arguments[key])
        for key in _TEXT_ARGUMENTS:
            value = arguments.get(key)
            if isinstance(value, list):
                value = " ".join(str(word) for word in value)
            if isinstance(value, str) and value:
                # The whole value loses its secrets before anything is cut from it.
                text, kinds = _redact(value)
                found.update(kinds)
                if level == "redacted" and key == "url":
                    text = _without_query(text)
                summary[key] = text if len(text) <= limit else text[:limit] + "…"
        return summary

    def _session(self) -> dict:
        from .security_flags import clip

        project = self.project_path
        name = (project if self.settings.project_paths else os.path.basename(project.rstrip("\\/"))) if project else ""
        session = {"id": self.session_id, "project": name, "surface": _surface(self.session_id)}
        # A title is conversation content (an automatic one is the first
        # message's gist): it goes only when messages do, at their level.
        if self.settings.messages != "off" and self.title:
            title = clip(self.title, TITLE_LIMIT)
            session["title"] = _EMAIL.sub("[email]", title) if self.settings.messages == "redacted" else title
        return session

    def finish(self, outcome: str) -> None:
        """Queue the turn's records; never raises."""
        try:
            self._finish(outcome)
        except Exception:
            logger.exception("Oversight couldn't record a turn")

    def _finish(self, outcome: str) -> None:
        from . import security_flags

        settings = self.settings
        turn = _next_turn(self.session_id)
        session = self._session()
        shared_redactions: Counter = Counter()
        record: dict[str, Any] | None = None
        if settings.activity:
            tools = []
            for entry in self.tools:
                item = {"name": entry["name"][:80], "status": entry["status"]}
                if entry["worker"]:
                    item["worker"] = True
                if settings.messages != "off":
                    arguments = self._arguments(entry["arguments"], shared_redactions)
                    if arguments:
                        item["arguments"] = arguments
                tools.append(item)
            record = {
                "type": "turn", "id": uuid.uuid4().hex, "session": session, "turn": turn,
                "started_at": self.started_at, "ended_at": _now(), "provider": self.provider[:80],
                "model": self.model[:120], "mode": self.mode[:20], "outcome": outcome,
                "usage": dict(self.usage), "tools": tools, "tool_calls": len(self.calls),
                "files_changed": self.files_changed, "images": self.images,
            }
            if self.origin == "generated":
                record["origin"] = "generated"

            if settings.messages != "off":
                user, user_found = _message(self.prompt, settings.messages)
                reply, reply_found = _message(self.reply, settings.messages)
                shared_redactions.update(user_found)
                shared_redactions.update(reply_found)
                record["messages"] = {"level": settings.messages, "user": user, "assistant": reply}
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
            entry = {"type": "flag", **flag.to_dict(), "session": session, "turn": turn}
            # Only a label leaves this computer as a flag's rule (security_flags.RULES).
            entry["rule"] = security_flags.label(entry["kind"], entry["rule"])
            if settings.messages == "off":
                entry.pop("excerpt", None)
            elif settings.messages == "redacted":
                entry["excerpt"] = _EMAIL.sub("[email]", entry.get("excerpt") or "")
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


def begin_turn(session: Any, user_msg: str, *, images: int = 0, input_origin: str = "human") -> TurnTracker | None:
    """A tracker for this turn when oversight is recording, else None; never raises.

    A delegated worker's turn is part of its parent's, which sees the
    worker's tool calls, so workers aren't recorded on their own. A chat of
    the chat gateway is recorded only once the chat itself was sent the
    notice for the policy in force (chat_notice), whoever else confirmed it.
    """
    try:
        if getattr(session, "is_subagent", False):
            return None
        scope = Scope()
        chat = _chat_key(session)
        if chat:
            recording = scope.configured and scope.destination and _chat_notified(chat, scope.fingerprint)
        else:
            recording = scope.active
        if not recording:
            return None
        return TurnTracker(session, user_msg, images, scope, input_origin)
    except Exception:
        logger.exception("Oversight couldn't start recording a turn")
        return None
    finally:
        # The typed text belongs to this turn only.
        if getattr(session, "display_prompt", None):
            session.display_prompt = None


def _next_turn(session_id: str) -> int:
    """The session's next turn number (kept per session, across restarts)."""
    path = _root() / "sessions.json"
    with _lock, exclusive(_root() / ".lock"):
        known = _read_json(path, {})
        known = known if isinstance(known, dict) else {}
        entry = known.get(session_id) if isinstance(known.get(session_id), dict) else {}
        turn = int(entry.get("turns") or 0) + 1
        known[session_id] = {"turns": turn, "at": _now()}
        if len(known) > KNOWN_SESSIONS:
            newest = sorted(known.items(), key=lambda item: str(item[1].get("at") or ""), reverse=True)
            known = dict(newest[:KNOWN_SESSIONS])
        _write_json(path, known)
    return turn


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
# queue, so a long time offline doesn't slow every turn's end. The app and
# `lumi run` share it; SQLite's own locking serializes them.


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


def _database() -> sqlite3.Connection:
    path = _root() / "queue.sqlite3"
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=30.0, isolation_level=None)
    connection.execute("CREATE TABLE IF NOT EXISTS records (seq INTEGER PRIMARY KEY AUTOINCREMENT, "
                       "id TEXT UNIQUE NOT NULL, at TEXT NOT NULL, size INTEGER NOT NULL, body TEXT NOT NULL)")
    connection.execute("CREATE INDEX IF NOT EXISTS records_at ON records (at)")
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


def queued_records(limit: int | None = None) -> list[dict]:
    """The records waiting to be sent, oldest first."""
    if not (_root() / "queue.sqlite3").exists():
        return []
    with closing(_database()) as connection:
        rows = connection.execute("SELECT body FROM records ORDER BY seq" + (" LIMIT ?" if limit else ""),
                                  (limit,) if limit else ()).fetchall()
    records = []
    for (body,) in rows:
        try:
            record = json.loads(body)
        except ValueError:
            continue
        if isinstance(record, dict) and record.get("id"):
            records.append(record)
    return records


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


# ── Sending ─────────────────────────────────────────────────────────────────


def _batch() -> list[dict]:
    batch, size = [], 0
    for record in queued_records(BATCH_RECORDS):
        length = len(json.dumps(record, separators=(",", ":")))
        if batch and size + length > BATCH_BYTES:
            break
        batch.append(record)
        size += length
    return batch


def _remove(ids: set[str], *, outcome: str, error: str = "") -> None:
    with _lock, exclusive(_root() / ".lock"), closing(_database()) as connection:
        listed = sorted(ids)
        for start in range(0, len(listed), 500):
            chunk = listed[start:start + 500]
            connection.execute(f"DELETE FROM records WHERE id IN ({','.join('?' * len(chunk))})", chunk)
        changes: dict[str, Any] = {outcome: len(ids)}
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


def upload_pending(client: Any, *, max_batches: int = 20) -> str:
    """Send queued records to Lumi Cloud: "idle", "more", "discarded" or "retry".

    Checks the policy before every batch, so nothing goes once the
    organization stops asking for it or the computer leaves, and nothing
    older than the organization keeps records.
    """
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
        batch = _batch()
        if not batch:
            return "idle"
        state = policy.load()
        version = state.policy.raw.get("policy_version") if state.policy else None
        try:
            _send(client, batch, version)
        except _OversightOff as exc:
            discard(f"{scope.organization}'s Lumi Cloud doesn't have oversight on ({exc}).")
            return "discarded"
        except CloudError as exc:
            with _lock, exclusive(_root() / ".lock"):
                _count(last_error=str(exc)[:300])
            return "retry"
    return "more"


class _Uploader:
    """Sends the queue in the background for the app's lifetime; never on the UI's thread."""

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
    """Send queued records to Lumi Cloud from a background thread (the app calls this once)."""
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
    """Try to send what's queued before a short-lived process exits (``lumi run``); never raises."""
    try:
        if not queued_records(1):
            return
        deadline = time.monotonic() + seconds
        client = client_factory()
        while time.monotonic() < deadline:
            if upload_pending(client, max_batches=1) != "more":
                return
    except Exception:
        logger.debug("Couldn't send oversight records before exiting", exc_info=True)
