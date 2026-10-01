"""Channel adapter contract for the chat gateway."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Callable


@dataclass
class InboundMessage:
    """A user message arriving from a chat channel."""

    chat_id: str        # Channel-native conversation identifier
    sender: str         # Display name or username of the sender
    text: str
    channel: str = ""   # Adapter name, e.g. "telegram"


class ChannelAdapter(ABC):
    """A chat channel the gateway can listen on and reply to.

    Adapters are transport-only: they never touch the engine. The
    GatewayService owns sessions and calls ``send()`` with replies.
    """

    name: str = "channel"

    @abstractmethod
    def run(self, on_message: Callable[[InboundMessage], None]) -> None:
        """Block and poll/listen for messages, invoking on_message for each.

        on_message must return quickly (the service enqueues work); the
        adapter should keep receiving while replies are being generated.
        A button press that answers an approval arrives as the message
        ``/approve <id>`` or ``/deny <id>``; one that confirms the
        organization's oversight notice as ``/acknowledge <token>``.
        """

    @abstractmethod
    def send(self, chat_id: str, text: str) -> bool | None:
        """Deliver a reply to the given conversation; False when it couldn't (None means sent)."""

    def ask(self, chat_id: str, text: str, approval_id: str) -> None:
        """Ask the person to approve an action. Adapters with buttons show them."""
        self.send(chat_id, f"{text}\nReply approve or deny.")

    def notice(self, chat_id: str, text: str, token: str) -> bool | None:
        """Send the organization's oversight notice (lumi/oversight.py); False when it couldn't.

        ``text`` already says how to confirm it by reply. Adapters with
        buttons add an "I've read this" button that arrives as
        ``/acknowledge <token>``.
        """
        return self.send(chat_id, text)

    def notify_busy(self, chat_id: str) -> None:
        """Optional 'agent is working' indicator (e.g. typing status)."""

    def stop(self) -> None:
        """Request the run() loop to exit."""
