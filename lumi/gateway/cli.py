"""
CLI entry for the chat gateway:  lumi gateway [options]

Examples:
    lumi gateway                                   # Telegram, from Settings
    lumi gateway --channel slack --project ~/code/app
    lumi gateway --token 123:ABC --allow 987654321 --mode auto-edit

Each chat's session is built like ``lumi run``'s (lumi/headless.py): the
project (``--project``, gateway.project, or this folder), its trust, file
exclusions, organization policy and budgets apply. In ``--mode ask`` (the
default) the agent asks in the chat before changing anything; ``auto-edit``
edits files and asks about the rest; ``bypass`` asks nothing. A project's own
instructions apply only if it is trusted in the app: the gateway never
trusts one itself. See docs/chat-gateway.md.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys

from ..gui.settings import SettingsManager
from ..network_defaults import resolve_ollama_url
from .service import GatewayService

logger = logging.getLogger(__name__)

CHANNELS = ("telegram", "slack")
# The tier each mode runs in. Unlike `lumi run`, a chat can answer, so Ask
# asks there instead of refusing every change.
TIERS = {"ask": "ask", "auto-edit": "auto-edit", "bypass": "full-auto"}


class GatewayError(ValueError):
    """The gateway can't start; the message says how to fix it."""


def _provider_and_model(settings: SettingsManager, provider: str, model: str) -> tuple[str, str]:
    default = str(settings.get("general", "default_backend", "") or "")
    provider = (provider or str(settings.get("gateway", "backend", "") or "") or default or "ollama").strip().lower()
    model = (model or str(settings.get("gateway", "model", "") or "")
             or (str(settings.get("general", "default_model", "") or "") if provider == default else "")).strip()
    if provider == "ollama" and not model:
        # The first installed local model, as before.
        import httpx

        url = resolve_ollama_url(settings_data=settings.get_all())
        response = httpx.get(f"{url}/api/tags", timeout=5)
        models = [m.get("name", "") for m in response.json().get("models", [])]
        if not models:
            raise GatewayError("No Ollama models installed; pass --model or pull one.")
        model = models[0]
    return provider, model


def _setting_or_env(settings: SettingsManager, key: str, env: str) -> str:
    return str(settings.get("api_keys", key, "") or "") or os.environ.get(env, "")


def build_adapter(args: argparse.Namespace, settings: SettingsManager):
    """The Telegram or Slack adapter the arguments and Settings describe."""
    from .slack import SlackChannel
    from .telegram import TelegramChannel

    telegram_token = args.token or _setting_or_env(settings, "telegram_bot", "TELEGRAM_BOT_TOKEN")
    slack_bot = args.slack_bot_token or _setting_or_env(settings, "slack_bot", "SLACK_BOT_TOKEN")
    slack_app = args.slack_app_token or _setting_or_env(settings, "slack_app", "SLACK_APP_TOKEN")
    channel = (args.channel or str(settings.get("gateway", "channel", "") or "")).strip().lower()
    if not channel:
        channel = "slack" if (slack_bot and slack_app and not telegram_token) else "telegram"
    if channel not in CHANNELS:
        raise GatewayError(f"Choose a channel: {', '.join(CHANNELS)}.")
    if channel == "slack":
        allowed = args.allow or list(settings.get("gateway", "slack_allowed", []) or [])
        return SlackChannel(slack_bot, slack_app, allowed), allowed
    allowed = args.allow or list(settings.get("gateway", "allowed_chat_ids", []) or [])
    api_url = str(settings.get("gateway", "telegram_api_url", "") or "")
    return TelegramChannel(bot_token=telegram_token, allowed_chat_ids=allowed, api_url=api_url), allowed


def build_service(args: argparse.Namespace, settings: SettingsManager, adapter) -> GatewayService:
    """The service, with sessions built like ``lumi run``'s for the project and mode."""
    from ..headless import UsageError, _configure, _mode, build_session, build_spec
    from ..policy import blocked_reason
    from ..policy import current as current_policy

    project = os.path.abspath(os.path.expanduser(
        args.project or str(settings.get("gateway", "project", "") or "") or os.getcwd()))
    if not os.path.isdir(project):
        raise GatewayError(f"{project} is not a folder.")
    _configure(settings)
    refusal = blocked_reason()
    if refusal:
        raise GatewayError(refusal)
    try:
        mode = _mode(settings, args.mode or str(settings.get("gateway", "mode", "") or "") or "ask")
        if mode not in TIERS:
            raise GatewayError(f"Choose a mode: {', '.join(TIERS)}.")
        provider, model = _provider_and_model(settings, args.backend, args.model)
        policy = current_policy()
        if policy and model and not policy.model_allowed(provider, model):
            raise GatewayError(f"{policy.organization}'s policy doesn't allow {model} on {provider}.")
        spec = build_spec(settings, provider, model, project)
    except UsageError as exc:
        raise GatewayError(str(exc)) from None
    if provider in {"codex", "claude-code"}:
        spec.permission_mode = mode

    def session_for(chat_id: str):
        session = build_session(settings, spec, project=project, mode=mode, trust_project=False,
                                max_requests=None, run_id=f"gateway-{adapter.name}-{chat_id}", tier=TIERS[mode])
        if args.max_tokens:
            session.max_tokens = args.max_tokens
        # Audit and usage records name the chat.
        session.audit_session_id = f"gateway:{chat_id}"
        return session

    def describe() -> str:
        return f"Project: {project}\nPermission mode: {mode}\nModel: {model} ({provider})"

    minutes = args.approval_minutes or float(settings.get("gateway", "approval_minutes", 10) or 10)
    return GatewayService(adapter, session_for, describe=describe, approval_seconds=max(1.0, minutes) * 60)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="lumi gateway",
        description="Work with the Lumi agent from Telegram or Slack.",
    )
    parser.add_argument("--channel", choices=CHANNELS, default="", help="telegram (default) or slack")
    parser.add_argument("--project", default="", help="the project folder (default: gateway.project or this folder)")
    parser.add_argument("--mode", choices=sorted(TIERS), default="",
                        help="ask (default): ask in the chat before changes; auto-edit: edit files, ask about "
                             "the rest; bypass: ask about nothing")
    parser.add_argument("--backend", default="", help="Provider (ollama, anthropic, openai, conn-<id>, ...)")
    parser.add_argument("--model", default="", help="Model name for the provider")
    parser.add_argument("--token", default="", help="Telegram bot token (overrides settings)")
    parser.add_argument("--slack-bot-token", default="", help="Slack bot token, xoxb-... (overrides settings)")
    parser.add_argument("--slack-app-token", default="", help="Slack app-level token, xapp-... (overrides settings)")
    parser.add_argument("--allow", action="append", default=[],
                        help="Allowed Telegram chat ID, or Slack channel or user ID (repeatable; overrides settings)")
    parser.add_argument("--approval-minutes", type=float, default=0,
                        help="How long to wait for an approval before refusing (default 10)")
    parser.add_argument("--max-tokens", type=int, default=None)
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    settings = SettingsManager()
    if settings.get("security", "chat_gateway", True) is False:
        print(
            "The chat gateway is turned off in Settings > Privacy & security "
            "(or by your organization's policy).",
            file=sys.stderr,
        )
        raise SystemExit(1)
    try:
        adapter, allowed = build_adapter(args, settings)
        service = build_service(args, settings, adapter)
    except (GatewayError, ValueError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1)
    except Exception as exc:
        print(f"Could not start the gateway: {exc}", file=sys.stderr)
        raise SystemExit(1)

    print(f"Gateway starting on {adapter.name}.\n{service._describe()}")
    if not allowed:
        print(
            "WARNING: nothing is allowed yet. The bot will reply to new chats with their IDs so you "
            "can add them (see docs/chat-gateway.md)."
        )
    try:
        service.run_forever()
    except KeyboardInterrupt:
        print("Gateway stopped.")


if __name__ == "__main__":
    main()
