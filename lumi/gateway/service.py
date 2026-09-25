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
    "clear — start a fresh conversation\n"
    "help — this message\n\n"
    "Anything else is sent to the agent."
)
_COMMANDS = {"approve", "deny", "stop", "status", "clear", "help", "start", "model"}


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
    if name not in _COMMANDS or (len(words) == 2 and name not in ("approve", "deny")):
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
    ):
        self._adapter = adapter
        self._factory = session_factory
        self._describe = describe or (lambda: "")
        self._approval_seconds = approval_seconds
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

        self._adapter.notify_busy(msg.chat_id)
        session = self._session_for(msg.chat_id)
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
