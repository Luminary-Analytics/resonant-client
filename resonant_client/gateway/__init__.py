"""
Chat-channel gateway: drive Resonant sessions from external chat apps.

The gateway runs headless (no GUI/TUI) and bridges channel messages to the
engine's Session loop:

    Telegram message -> ChannelAdapter -> GatewayService -> Session.run()
    engine text events -> GatewayService -> ChannelAdapter -> chat reply

Start it with:  resonant gateway --backend ollama
"""

from .base import ChannelAdapter, InboundMessage
from .service import GatewayService
from .telegram import TelegramChannel

__all__ = [
    "ChannelAdapter",
    "InboundMessage",
    "GatewayService",
    "TelegramChannel",
]
