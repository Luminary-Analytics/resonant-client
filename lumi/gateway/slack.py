"""
Slack channel adapter, over Socket Mode.

Socket Mode keeps a WebSocket open from this computer to Slack, so the
gateway needs no public address. It takes two tokens from a Slack app made
for the gateway (see docs/chat-gateway.md for its settings):

- the bot token (``xoxb-``: ``api_keys.slack_bot`` in settings.json or
  ``SLACK_BOT_TOKEN``) posts replies;
- the app-level token (``xapp-`` with ``connections:write``:
  ``api_keys.slack_app`` or ``SLACK_APP_TOKEN``) opens the socket.

In a direct message every message goes to the agent; in a channel, only
messages that mention the bot. Only the channels and people in
``gateway.slack_allowed`` are served: a channel ID lets everyone in it use
the agent, a user ID lets that person use it wherever the bot is. Anyone
else gets their IDs back and nothing runs. Approval buttons are checked the
same way.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from typing import Callable, Optional

from .base import ChannelAdapter, InboundMessage
from .telegram import _chunk

logger = logging.getLogger(__name__)

_API_BASE = "https://slack.com/api"
_MAX_MESSAGE_CHARS = 3900
_ACTIONS = {"lumi_approve": "approve", "lumi_deny": "deny"}


class SlackChannel(ChannelAdapter):
    name = "slack"

    def __init__(
        self,
        bot_token: str,
        app_token: str,
        allowed: Optional[list[str]] = None,
        *,
        client=None,
        connect: Optional[Callable] = None,
    ):
        if not bot_token.startswith("xoxb-"):
            raise ValueError("Slack needs the app's bot token (xoxb-...): api_keys.slack_bot in "
                             "~/.lumi/settings.json, SLACK_BOT_TOKEN or --slack-bot-token.")
        if not app_token.startswith("xapp-"):
            raise ValueError("Slack needs an app-level token (xapp-...) with connections:write: api_keys.slack_app "
                             "in ~/.lumi/settings.json, SLACK_APP_TOKEN or --slack-app-token.")
        self._bot_token = bot_token
        self._app_token = app_token
        self._allowed = {str(item).strip() for item in (allowed or []) if str(item).strip()}
        self._stop = threading.Event()
        self._client = client  # an httpx.Client; tests pass one with a mock transport
        self._connect = connect
        self._bot_user = ""
        self._socket = None

    # ── Transport helpers ────────────────────────────────────────────

    def _api(self, method: str, *, token: str = "", **params) -> dict:
        import httpx

        if self._client is None:
            self._client = httpx.Client()
        response = self._client.post(
            f"{_API_BASE}/{method}", json=params, timeout=15.0,
            headers={"Authorization": f"Bearer {token or self._bot_token}",
                     "Content-Type": "application/json; charset=utf-8"})
        response.raise_for_status()
        payload = response.json()
        if not payload.get("ok"):
            raise RuntimeError(f"Slack {method} failed: {payload.get('error', payload)}")
        return payload

    def _open(self, url: str):
        if self._connect is not None:
            return self._connect(url)
        from websockets.sync.client import connect

        return connect(url, open_timeout=15, close_timeout=5)

    # ── ChannelAdapter interface ─────────────────────────────────────

    def run(self, on_message: Callable[[InboundMessage], None]) -> None:
        me = self._api("auth.test")
        self._bot_user = str(me.get("user_id") or "")
        logger.info("Slack gateway online as %s in %s", me.get("user", "?"), me.get("team", "?"))
        backoff = 1.0
        while not self._stop.is_set():
            try:
                url = self._api("apps.connections.open", token=self._app_token)["url"]
                with self._open(url) as socket:
                    self._socket = socket
                    backoff = 1.0
                    self._listen(socket, on_message)
            except Exception as exc:
                if self._stop.is_set():
                    break
                logger.warning("Slack connection failed (%s); retrying in %.0fs", exc, backoff)
                time.sleep(backoff)
                backoff = min(backoff * 2, 60.0)
            finally:
                self._socket = None

    def _listen(self, socket, on_message: Callable[[InboundMessage], None]) -> None:
        """Read envelopes until Slack asks to reconnect or the gateway stops."""
        while not self._stop.is_set():
            try:
                raw = socket.recv(timeout=1.0)
            except TimeoutError:
                continue
            envelope = json.loads(raw)
            if envelope.get("envelope_id"):
                # Slack redelivers anything not acknowledged within 3 seconds.
                socket.send(json.dumps({"envelope_id": envelope["envelope_id"]}))
            kind = envelope.get("type")
            if kind == "disconnect":
                return
            payload = envelope.get("payload") or {}
            if kind == "events_api":
                self._event(payload.get("event") or {}, on_message)
            elif kind == "interactive" and payload.get("type") == "block_actions":
                self._buttons(payload, on_message)

    def _event(self, event: dict, on_message: Callable[[InboundMessage], None]) -> None:
        if event.get("type") != "message" or event.get("subtype") or event.get("bot_id"):
            return  # edits, joins, and messages from bots (this one included)
        user, channel = str(event.get("user") or ""), str(event.get("channel") or "")
        if not user or user == self._bot_user or not channel:
            return
        text = str(event.get("text") or "")
        mention = f"<@{self._bot_user}>" if self._bot_user else ""
        if event.get("channel_type") != "im":
            if not mention or mention not in text:
                return  # in a channel, only messages that mention the bot
        text = text.replace(mention, " ").strip() if mention else text.strip()
        if not text:
            return
        if not self._is_allowed(channel, user):
            self._reject(channel, user)
            return
        on_message(InboundMessage(chat_id=channel, sender=user, text=_plain(text), channel=self.name))

    def _buttons(self, payload: dict, on_message: Callable[[InboundMessage], None]) -> None:
        """Approve and Deny buttons, answered as ``/approve <id>`` or ``/deny <id>``."""
        user = str((payload.get("user") or {}).get("id") or "")
        channel = str((payload.get("channel") or {}).get("id") or "")
        for action in payload.get("actions") or []:
            verb = _ACTIONS.get(str(action.get("action_id") or ""))
            if not verb:
                continue
            if not self._is_allowed(channel, user):
                logger.warning("Ignored an approval button from Slack user %s in %s", user, channel)
                return
            on_message(InboundMessage(chat_id=channel, sender=user, text=f"/{verb} {action.get('value') or ''}",
                                      channel=self.name))

    def send(self, chat_id: str, text: str) -> None:
        for chunk in _chunk(text, _MAX_MESSAGE_CHARS):
            try:
                self._api("chat.postMessage", channel=chat_id, text=chunk)
            except Exception:
                logger.exception("Failed to send a Slack reply to %s", chat_id)
                return

    def ask(self, chat_id: str, text: str, approval_id: str) -> None:
        text = text[:_MAX_MESSAGE_CHARS]
        blocks = [
            {"type": "section", "text": {"type": "plain_text", "text": text}},
            {"type": "actions", "elements": [
                {"type": "button", "action_id": "lumi_approve", "value": approval_id, "style": "primary",
                 "text": {"type": "plain_text", "text": "Approve"}},
                {"type": "button", "action_id": "lumi_deny", "value": approval_id, "style": "danger",
                 "text": {"type": "plain_text", "text": "Deny"}},
            ]},
        ]
        try:
            self._api("chat.postMessage", channel=chat_id, text=f"{text} Reply approve or deny.", blocks=blocks)
        except Exception:
            logger.exception("Failed to send a Slack approval request to %s", chat_id)
            super().ask(chat_id, text, approval_id)

    def stop(self) -> None:
        self._stop.set()
        socket = self._socket
        if socket is not None:
            try:
                socket.close()
            except Exception:
                pass

    # ── Allowlist ────────────────────────────────────────────────────

    def _is_allowed(self, channel: str, user: str) -> bool:
        return channel in self._allowed or user in self._allowed

    def _reject(self, channel: str, user: str) -> None:
        logger.warning("Rejected a Slack message from user %s in %s", user, channel)
        self.send(
            channel,
            "This Slack channel or person isn't allowed to use the agent.\n"
            f"Channel ID: {channel}\nYour user ID: {user}\n"
            "To allow one, add it to gateway.slack_allowed in Lumi's settings (~/.lumi/settings.json) or pass "
            f"--allow {user} when starting the gateway, then restart it.",
        )


def _plain(text: str) -> str:
    """Slack's markup for links and mentions, as the text a person typed."""
    text = re.sub(r"<(https?://[^|>]+)\|([^>]+)>", r"\2 (\1)", text)
    text = re.sub(r"<(https?://[^>]+)>", r"\1", text)
    return text.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")
