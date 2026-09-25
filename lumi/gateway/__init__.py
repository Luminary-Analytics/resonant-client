"""
Chat-channel gateway: work with Lumi sessions from Telegram or Slack.

The gateway runs headless (no GUI/TUI) and bridges channel messages to the
engine's Session loop:

    chat message -> ChannelAdapter -> GatewayService -> Session.run()
    engine text events and approval requests -> GatewayService -> chat

Start it with:  lumi gateway [--channel slack] [--project PATH] [--mode ask]
See docs/chat-gateway.md.
"""

from .base import ChannelAdapter, InboundMessage
from .service import GatewayService
from .slack import SlackChannel
from .telegram import TelegramChannel

__all__ = [
    "ChannelAdapter",
    "InboundMessage",
    "GatewayService",
    "SlackChannel",
    "TelegramChannel",
]
