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
        """

    @abstractmethod
    def send(self, chat_id: str, text: str) -> None:
        """Deliver a reply to the given conversation."""

    def notify_busy(self, chat_id: str) -> None:
        """Optional 'agent is working' indicator (e.g. typing status)."""

    def stop(self) -> None:
        """Request the run() loop to exit."""
