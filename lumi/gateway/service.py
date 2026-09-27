"""
GatewayService: bridges channel messages to engine sessions.

One Session per chat, so each conversation keeps its own history. Turns run
on a single worker thread (agent turns are heavyweight; serializing them
avoids competing tool executions on one machine), while the channel adapter
keeps receiving. That is what lets a person answer an approval or stop a turn
while it runs: /approve, /deny, /stop and /status are handled as they arrive,
never queued behind the turn they concern.

Sessions come from a factory that builds them like ``lumi run``'s
(gateway/cli.py, lumi/headless.py): the project, its trust, file exclusions,
the organization's policy and the permission mode apply. In Ask and
Auto-edit modes an action the mode doesn't allow is sent to the chat for
approval, and nobody answering in time refuses it. Restrict who can reach the
gateway with the channel's allowlist.

When the organization's policy has Lumi share work with its Lumi Cloud
(lumi/oversight.py), a chat's requests don't run until someone in that chat
confirms the organization's notice: the chat is sent it, with an "I've read
this" button, and a press or the reply "I've read this" confirms it for that
chat and that policy (signed with this computer's device key and sent to
Lumi Cloud). Until then each request is answered with the notice instead.
"""

from __future__ import annotations

import json
import logging
import queue
import threading
import uuid
from dataclasses import dataclass, field
from typing import Callable, Optional

from .base import ChannelAdapter, InboundMessage

logger = logging.getLogger(__name__)

# Session.run events whose payload text becomes part of the chat reply.
_TEXT_EVENTS = {"text.done"}
_ERROR_EVENT = "error"

_HELP_TEXT = (
    "Commands (send one on its own, with or without the slash; in Slack, without):\n"
    "status — the project, permission mode and model, and what's running\n"
    "stop — stop the request that's running\n"
    "approve, deny — answer a request to run an action\n"
    "acknowledge — confirm your organization's oversight notice, when one is shown\n"
    "clear — start a fresh conversation\n"
    "help — this message\n\n"
    "Anything else is sent to the agent."
)
_COMMANDS = {"approve", "deny", "stop", "status", "clear", "help", "start", "model", "acknowledge"}
# Commands that take an argument: an approval's id, the notice's token.
_WITH_ARGUMENT = ("approve", "deny", "acknowledge")


def parse_command(text: str) -> tuple[str, str]:
    """``(command, argument)`` when a message is a gateway command, else ``("", "")``.

    Telegram sends ``/stop`` (``/stop@bot`` in groups). Slack's app keeps a
    leading slash for its own commands, so a message that is only the word
    (``stop``, ``approve 1a2b``) counts too. Anything longer goes to the agent.
    """
    words = text.strip().split()
    if not words or len(words) > 2:
        return "", ""
    name = words[0].lower().lstrip("/").split("@", 1)[0]
    if name not in _COMMANDS or (len(words) == 2 and name not in _WITH_ARGUMENT):
        return "", ""
    return name, words[1] if len(words) == 2 else ""


@dataclass
class _Approval:
    id: str
    event: threading.Event = field(default_factory=threading.Event)
    approved: bool = False


def _short(value: object, limit: int = 300) -> str:
    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


def describe_call(tool_name: str, tool_args: dict) -> str:
    """What an action would do, in a line a person can approve or deny."""
    args = tool_args if isinstance(tool_args, dict) else {}
    path = args.get("path") or args.get("file_path") or ""
    if tool_name == "bash" and args.get("command"):
        text = f"run `{_short(args['command'])}`"
    elif tool_name == "file_write" and path:
        text = f"write {path} ({len(str(args.get('content') or ''))} characters)"
    elif tool_name in ("file_edit", "file_replace") and path:
        text = f"edit {path}"
    elif path:
        text = f"use {tool_name} on {path}"
    else:
        shown = json.dumps(args, ensure_ascii=False, sort_keys=True, default=str)
        text = f"use {tool_name} ({_short(shown)})" if args else f"use {tool_name}"
    # The chat is outside this computer: never show a saved key's value.
    from .. import secret_scan

    return secret_scan.redact_text(text)[0]


class GatewayService:
    """Owns per-chat sessions, the worker that runs turns, and waiting approvals."""

    def __init__(
        self,
        adapter: ChannelAdapter,
        session_factory: Callable[[str], object],
        *,
        describe: Optional[Callable[[], str]] = None,
        approval_seconds: float = 600.0,
        oversight_signer: Optional[Callable[[bytes], str]] = None,
    ):
        self._adapter = adapter
        self._factory = session_factory
        self._describe = describe or (lambda: "")
        self._approval_seconds = approval_seconds
        # Signs a chat's confirmation of the oversight notice with the device key (CloudClient.sign_as_device).
        self._oversight_signer = oversight_signer
        self._sessions: dict[str, object] = {}
        self._queue: "queue.Queue[InboundMessage]" = queue.Queue()
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._running: dict[str, object] = {}
        self._approvals: dict[str, _Approval] = {}

    # ── Lifecycle ────────────────────────────────────────────────────

    def run_forever(self) -> None:
        """Start the worker and block on the channel's receive loop."""
        worker = threading.Thread(target=self._worker, daemon=True, name="gateway-worker")
        worker.start()
        try:
            self._adapter.run(self.receive)
        finally:
            self.stop()

    def stop(self) -> None:
        self._stop.set()
        with self._lock:
            sessions = list(self._running.values())
            approvals = list(self._approvals.values())
        for session in sessions:
            session.cancel()
        for approval in approvals:
            approval.event.set()
        self._adapter.stop()

    # ── Receiving (the adapter's thread) ─────────────────────────────

    def receive(self, msg: InboundMessage) -> None:
        """Handle controls at once; queue everything else for the worker."""
        command, argument = parse_command(msg.text)
        if command in ("approve", "deny"):
            self._answer(msg.chat_id, command == "approve", argument)
        elif command == "stop":
            self._stop_turn(msg.chat_id)
        elif command == "status":
            self._status(msg.chat_id)
        else:
            with self._lock:
                busy = bool(self._running) or not self._queue.empty()
            self._queue.put(msg)
            if busy and not command:
                self._adapter.send(msg.chat_id, "Queued: Lumi is working on another request.")

    def _answer(self, chat_id: str, approved: bool, approval_id: str) -> None:
        with self._lock:
            pending = self._approvals.get(chat_id)
        if pending is None or (approval_id and approval_id != pending.id):
            self._adapter.send(chat_id, "Nothing is waiting for an answer.")
            return
        pending.approved = approved
        pending.event.set()

    def _stop_turn(self, chat_id: str) -> None:
        with self._lock:
            session = self._running.get(chat_id)
            pending = self._approvals.get(chat_id)
        if session is None:
            self._adapter.send(chat_id, "Nothing is running.")
            return
        session.cancel()
        if pending is not None:
            pending.event.set()  # unanswered counts as denied
        self._adapter.send(chat_id, "Stopping.")

    def _status(self, chat_id: str) -> None:
        with self._lock:
            running = chat_id in self._running
            waiting = chat_id in self._approvals
        state = ("Waiting for your approval." if waiting else "Working on your request." if running
                 else "Idle.")
        queued = self._queue.qsize()
        extra = f" {queued} more request{'s' if queued != 1 else ''} queued." if queued else ""
        self._adapter.send(chat_id, f"{self._describe()}\n{state}{extra}".strip())

    # ── Turns (the worker's thread) ──────────────────────────────────

    def _worker(self) -> None:
        while not self._stop.is_set():
            try:
                msg = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                self._handle(msg)
            except Exception:
                logger.exception("Gateway turn failed for chat %s", msg.chat_id)
                self._adapter.send(
                    msg.chat_id,
                    "Something went wrong running that request. Check the gateway logs.",
                )

    def _handle(self, msg: InboundMessage) -> None:
        command, _ = parse_command(msg.text)
        if command in ("start", "help"):
            self._adapter.send(msg.chat_id, _HELP_TEXT)
            return
        if command == "clear":
            self._sessions.pop(msg.chat_id, None)
            self._adapter.send(msg.chat_id, "Conversation cleared.")
            return
        if command == "model":
            self._status(msg.chat_id)
            return

        session = self._session_for(msg.chat_id)
        if not self._oversight_admits(msg, session):
            return
        if command == "acknowledge":
            self._adapter.send(msg.chat_id, "Nothing here needs confirming.")
            return
        self._adapter.notify_busy(msg.chat_id)
        # A /stop cancels one turn, not every later one in this chat.
        session.reset_cancel()
        with self._lock:
            self._running[msg.chat_id] = session
        reply_parts: list[str] = []
        error_message = ""
        try:
            for event in session.run(msg.text, on_permission=self._asker(msg.chat_id)):
                etype = event.get("event", "")
                if etype in _TEXT_EVENTS:
                    text = str(event.get("text", "") or "")
                    if text:
                        reply_parts.append(text)
                elif etype == _ERROR_EVENT:
                    error_message = str(event.get("message", "") or "unknown error")
                elif etype == "tool.call":
                    # Keep the chat responsive during long tool phases.
                    self._adapter.notify_busy(msg.chat_id)
        finally:
            with self._lock:
                self._running.pop(msg.chat_id, None)

        if session.cancel_requested:
            self._adapter.send(msg.chat_id, "Stopped.")
            return
        if error_message and not reply_parts:
            self._adapter.send(msg.chat_id, f"Agent error: {error_message}")
            return
        self._adapter.send(msg.chat_id, "\n\n".join(reply_parts).strip() or "(no response)")

    def _oversight_admits(self, msg: InboundMessage, session: object) -> bool:
        """Whether this chat's request may run: its people confirmed the organization's notice (lumi/oversight.py).

        While they haven't, the request doesn't run: the chat is sent the
        notice with an "I've read this" button. The button (``/acknowledge
        <token>``) or the reply "I've read this", once the chat was sent the
        notice for the policy in force, confirms it for this chat and this
        policy; the person then sends the request again.
        """
        from .. import oversight

        chat_id = msg.chat_id
        key = str(getattr(session, "audit_session_id", "") or "") or f"{oversight.CHAT_PREFIX}{chat_id}"
        pending = oversight.chat_notice(key)
        if pending is None:
            return True
        notice, fingerprint = pending
        command, token = parse_command(msg.text)
        confirming = (command == "acknowledge" and token in ("", fingerprint)) or oversight.is_acknowledgment(msg.text)
        if confirming and oversight.chat_notified(key, fingerprint) and oversight.acknowledge(
                fingerprint, "gateway", chat=key, notice=notice, signer=self._oversight_signer):
            self._adapter.send(chat_id, "Thanks. Lumi runs this chat's requests from now on: send yours again.")
            return False
        try:
            delivered = self._adapter.notice(chat_id, oversight.chat_message(notice), fingerprint) is not False
        except Exception:
            logger.warning("Couldn't send the oversight notice to chat %s", chat_id, exc_info=True)
            delivered = False
        if delivered:
            oversight.chat_notice_sent(key, fingerprint)
        return False

    def _asker(self, chat_id: str) -> Callable[[str, dict], bool]:
        """The session's permission prompt: ask in the chat and wait for the answer."""

        def ask(tool_name: str, tool_args: dict) -> bool:
            approval = _Approval(uuid.uuid4().hex[:8])
            with self._lock:
                self._approvals[chat_id] = approval
            try:
                self._adapter.ask(chat_id, f"Lumi wants to {describe_call(tool_name, tool_args)}.", approval.id)
                answered = approval.event.wait(self._approval_seconds)
            finally:
                with self._lock:
                    self._approvals.pop(chat_id, None)
            if self._stop.is_set():
                return False
            if not answered:
                minutes = max(1, round(self._approval_seconds / 60))
                self._adapter.send(chat_id, f"No answer within {minutes} minute{'s' if minutes != 1 else ''}, "
                                            "so it wasn't done.")
                return False
            with self._lock:
                session = self._running.get(chat_id)
            if approval.approved:
                self._adapter.send(chat_id, "Approved.")
            elif not getattr(session, "cancel_requested", False):
                self._adapter.send(chat_id, "Denied.")
            return approval.approved

        return ask

    def _session_for(self, chat_id: str):
        session = self._sessions.get(chat_id)
        if session is None:
            session = self._factory(chat_id)
            self._sessions[chat_id] = session
        return session
