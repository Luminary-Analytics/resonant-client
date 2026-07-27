"""Per-connection chat-turn state for the WebSocket endpoint.

This was six locals and two closures living inside `websocket_endpoint`:

    pending_chat_messages, chat_runner, cancel_request_id,
    clear_request_cache, _drain_chat_queue(), _enqueue_chat_message()

and that is the entire reason a set of commands could not be moved into
`ws_commands.py`. They were not conceptually entangled with the run loop — they
simply needed to read or replace a variable that only code textually inside one
2,200-line function could reach.

Naming the state makes the dependency ordinary: a handler takes `ctx.runs` and
asks it whether a turn is in flight, queues a message, or cancels. It is also
now testable without a socket, which the closures never were.

The message processor is injected rather than imported. `app.py` owns
`_process_chat_message`, and importing it here would close a cycle
(app -> ws_commands -> chat_loop -> app).
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any, Awaitable, Callable

logger = logging.getLogger(__name__)

# Raised by Starlette when the peer goes away mid-turn. Passed through rather
# than swallowed so the endpoint's own handler can end the connection.
try:  # pragma: no cover - import shape differs across Starlette versions
    from starlette.websockets import WebSocketDisconnect
except Exception:  # pragma: no cover
    class WebSocketDisconnect(Exception):
        pass


class ChatRunLoop:
    """Owns the in-flight chat turn and the queue of messages behind it."""

    def __init__(
        self,
        ws: Any,
        process: Callable[[Any, dict[str, Any]], Awaitable[None]],
    ) -> None:
        self.ws = ws
        self._process = process
        self.pending: list[dict[str, Any]] = []
        self.task: asyncio.Task | None = None
        # Set when a cancel is requested so the drain loop can acknowledge it
        # once the turn actually finishes, rather than when it was asked to.
        self.cancel_request_id: str | None = None
        # Per-connection memo for `clear`, which is idempotent by request id.
        self.clear_cache: dict[str, dict[str, Any]] = {}

    @property
    def busy(self) -> bool:
        """True while a turn is streaming."""
        return self.task is not None and not self.task.done()

    async def enqueue(self, msg: dict[str, Any]) -> None:
        """Queue a chat message, starting the drain loop if it is idle.

        A message arriving mid-turn is queued and acknowledged as such rather
        than racing the running turn.
        """
        message_id = str(msg.get("message_id") or uuid.uuid4())
        running = self.busy
        queued = dict(msg, message_id=message_id, _was_queued=running)
        self.pending.append(queued)
        if running:
            await self.ws.send_json({
                "event": "message.queued",
                "message_id": message_id,
                "text": queued.get("text", ""),
                "position": len(self.pending),
                "steering": False,
            })
        else:
            self.task = asyncio.create_task(self._drain())

    async def _drain(self) -> None:
        try:
            while self.pending:
                queued = self.pending.pop(0)
                try:
                    if queued.get("_was_queued"):
                        await self.ws.send_json({
                            "event": "message.started",
                            "message_id": queued.get("message_id", ""),
                            "text": queued.get("text", ""),
                        })
                    await self._process(self.ws, queued)
                except WebSocketDisconnect:
                    raise
                except Exception as exc:
                    logger.exception("queued chat turn failed")
                    try:
                        await self.ws.send_json({"event": "error", "message": str(exc)})
                    except Exception:
                        pass
        except WebSocketDisconnect:
            raise
        finally:
            self.task = None
            if self.cancel_request_id:
                completed_id = self.cancel_request_id
                self.cancel_request_id = None
                try:
                    await self.ws.send_json({
                        "event": "cancel.completed",
                        "cancel_id": completed_id,
                    })
                except Exception:
                    pass

    def adopt(self, task: asyncio.Task) -> None:
        """Track a turn started outside the queue (an agent restart).

        Such a run streams through the same machinery and must be visible to
        `busy`, or a concurrent message would start a second turn on top of it.
        """
        self.task = task
