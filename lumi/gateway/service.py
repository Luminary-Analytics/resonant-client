"""
GatewayService: bridges channel messages to engine sessions.

One Session per chat, so each conversation keeps its own history. Messages
are processed by a single worker thread (agent turns are heavyweight —
serializing them avoids competing tool executions on one machine), while the
channel adapter keeps polling so nothing is dropped.

Sessions run with auto_approve=True: there is no interactive approval UI in
a chat channel, so the gateway is effectively full-auto. Restrict who can
reach it via the channel allowlist.
"""

from __future__ import annotations

import logging
import queue
import threading
from typing import Optional

from ..engine import Session
from .base import ChannelAdapter, InboundMessage

logger = logging.getLogger(__name__)

# Session.run events whose payload text becomes part of the chat reply.
_TEXT_EVENTS = {"text.done"}
_ERROR_EVENT = "error"

_HELP_TEXT = (
    "Commands:\n"
    "/clear — start a fresh conversation\n"
    "/model — show the active backend and model\n"
    "/help — this message\n\n"
    "Anything else is sent to the agent."
)


class GatewayService:
    """Owns per-chat sessions and the worker that runs agent turns."""

    def __init__(
        self,
        adapter: ChannelAdapter,
        backend,
        max_tokens: Optional[int] = None,
        project_instructions: Optional[str] = None,
    ):
        self._adapter = adapter
        self._backend = backend
        self._max_tokens = max_tokens
        self._project_instructions = project_instructions
        self._sessions: dict[str, Session] = {}
        self._queue: "queue.Queue[InboundMessage]" = queue.Queue()
        self._stop = threading.Event()

    # ── Lifecycle ────────────────────────────────────────────────────

    def run_forever(self) -> None:
        """Start the worker and block on the channel's receive loop."""
        worker = threading.Thread(target=self._worker, daemon=True, name="gateway-worker")
        worker.start()
        try:
            self._adapter.run(self._queue.put)
        finally:
            self._stop.set()
            self._adapter.stop()

    def stop(self) -> None:
        self._stop.set()
        self._adapter.stop()

    # ── Message handling ─────────────────────────────────────────────

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
        command = msg.text.strip().lower()
        if command in ("/start", "/help"):
            self._adapter.send(msg.chat_id, _HELP_TEXT)
            return
        if command == "/clear":
            self._sessions.pop(msg.chat_id, None)
            self._adapter.send(msg.chat_id, "Conversation cleared.")
            return
        if command == "/model":
            backend_name = getattr(self._backend, "name", "?")
            model = getattr(self._backend, "model", "?")
            self._adapter.send(msg.chat_id, f"Backend: {backend_name}\nModel: {model}")
            return

        self._adapter.notify_busy(msg.chat_id)
        session = self._session_for(msg.chat_id)

        reply_parts: list[str] = []
        error_message = ""
        for event in session.run(msg.text):
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

        if error_message and not reply_parts:
            self._adapter.send(msg.chat_id, f"Agent error: {error_message}")
            return
        self._adapter.send(msg.chat_id, "\n\n".join(reply_parts).strip() or "(no response)")

    def _session_for(self, chat_id: str) -> Session:
        session = self._sessions.get(chat_id)
        if session is None:
            session = Session(
                backend=self._backend,
                max_tokens=self._max_tokens,
                auto_approve=True,
                project_instructions=self._project_instructions,
            )
            self._sessions[chat_id] = session
        return session
