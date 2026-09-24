"""
Telegram channel adapter.

Uses the Bot API over plain HTTPS long-polling (getUpdates), so it needs no
third-party SDK — httpx is already a core dependency. Create a bot with
@BotFather, put the token in settings (api_keys.telegram_bot) or pass
--token, and start the gateway.

Security: only chats in the allowlist are served. When a new chat writes to
the bot, the adapter replies with the numeric chat ID and instructions to add
it — it never runs the agent for unknown chats.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Callable, Optional

from .base import ChannelAdapter, InboundMessage

logger = logging.getLogger(__name__)

_API_BASE = "https://api.telegram.org"
_POLL_TIMEOUT_S = 50
_MAX_MESSAGE_CHARS = 4096  # Telegram hard limit per sendMessage


class TelegramChannel(ChannelAdapter):
    name = "telegram"

    def __init__(
        self,
        bot_token: str,
        allowed_chat_ids: Optional[list[str]] = None,
    ):
        if not bot_token:
            raise ValueError(
                "Telegram bot token required. Create a bot with @BotFather, "
                "then set api_keys.telegram_bot in settings or pass --token."
            )
        self._token = bot_token
        self._allowed = {str(c).strip() for c in (allowed_chat_ids or []) if str(c).strip()}
        self._stop = threading.Event()
        self._offset = 0

    # ── Transport helpers ────────────────────────────────────────────

    def _call(self, method: str, _http_timeout: float = 15.0, **params) -> dict:
        """POST a Bot API method. `_http_timeout` is the local socket timeout;
        a `timeout` key in params is Telegram's long-poll duration."""
        import httpx

        url = f"{_API_BASE}/bot{self._token}/{method}"
        response = httpx.post(url, json=params, timeout=_http_timeout)
        response.raise_for_status()
        payload = response.json()
        if not payload.get("ok"):
            raise RuntimeError(f"Telegram {method} failed: {payload.get('description', payload)}")
        return payload.get("result")

    # ── ChannelAdapter interface ─────────────────────────────────────

    def run(self, on_message: Callable[[InboundMessage], None]) -> None:
        me = self._call("getMe")
        logger.info("Telegram gateway online as @%s", me.get("username", "?"))
        backoff = 1.0
        while not self._stop.is_set():
            try:
                updates = self._call(
                    "getUpdates",
                    _http_timeout=_POLL_TIMEOUT_S + 10,
                    offset=self._offset,
                    timeout=_POLL_TIMEOUT_S,
                    allowed_updates=["message"],
                )
                backoff = 1.0
            except Exception as exc:
                if self._stop.is_set():
                    break
                logger.warning("Telegram poll failed (%s); retrying in %.0fs", exc, backoff)
                time.sleep(backoff)
                backoff = min(backoff * 2, 60.0)
                continue

            for update in updates or []:
                self._offset = max(self._offset, int(update.get("update_id", 0)) + 1)
                message = update.get("message") or update.get("edited_message")
                if not message:
                    continue
                text = str(message.get("text", "") or "").strip()
                if not text:
                    continue
                chat_id = str((message.get("chat") or {}).get("id", ""))
                sender = str(
                    (message.get("from") or {}).get("username")
                    or (message.get("from") or {}).get("first_name")
                    or "unknown"
                )
                if not self._is_allowed(chat_id):
                    self._reject_unknown_chat(chat_id, sender)
                    continue
                on_message(InboundMessage(
                    chat_id=chat_id, sender=sender, text=text, channel=self.name,
                ))

    def send(self, chat_id: str, text: str) -> None:
        for chunk in _chunk(text, _MAX_MESSAGE_CHARS):
            try:
                self._call("sendMessage", chat_id=chat_id, text=chunk)
            except Exception:
                logger.exception("Failed to send Telegram reply to chat %s", chat_id)
                return

    def notify_busy(self, chat_id: str) -> None:
        try:
            self._call("sendChatAction", chat_id=chat_id, action="typing")
        except Exception:
            pass  # Cosmetic only

    def stop(self) -> None:
        self._stop.set()

    # ── Allowlist ────────────────────────────────────────────────────

    def _is_allowed(self, chat_id: str) -> bool:
        return chat_id in self._allowed

    def _reject_unknown_chat(self, chat_id: str, sender: str) -> None:
        logger.warning(
            "Rejected message from non-allowlisted Telegram chat %s (@%s)",
            chat_id, sender,
        )
        self.send(
            chat_id,
            f"This chat is not authorized to use the agent.\n\n"
            f"Your chat ID is: {chat_id}\n\n"
            f"To authorize it, add this ID to gateway.allowed_chat_ids in "
            f"Resonant settings (~/.resonant/settings.json) or pass "
            f"--allow {chat_id} when starting the gateway, then restart it.",
        )


def _chunk(text: str, limit: int) -> list[str]:
    text = text or "(empty response)"
    chunks = []
    while text:
        if len(text) <= limit:
            chunks.append(text)
            break
        # Prefer breaking at a newline near the limit so code blocks and
        # paragraphs survive splitting more often than not.
        cut = text.rfind("\n", limit // 2, limit)
        if cut == -1:
            cut = limit
        chunks.append(text[:cut])
        text = text[cut:].lstrip("\n")
    return chunks
