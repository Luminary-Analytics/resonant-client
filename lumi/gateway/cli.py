"""
CLI entry for the chat gateway:  resonant gateway [options]

Examples:
    resonant gateway                          # settings-driven (token + allowlist)
    resonant gateway --backend ollama --model llama3.1:8b
    resonant gateway --token 123:ABC --allow 987654321
"""

from __future__ import annotations

import argparse
import logging
import sys

from ..backends import create_backend
from ..gui.settings import SettingsManager
from ..network_defaults import resolve_ollama_url
from .service import GatewayService
from .telegram import TelegramChannel

logger = logging.getLogger(__name__)


def _build_backend(settings: SettingsManager, backend_type: str, model: str):
    backend_type = (
        backend_type
        or str(settings.get("gateway", "backend", "") or "")
        or str(settings.get("general", "default_backend", "") or "")
        or "ollama"
    ).strip().lower()
    model = (
        model
        or str(settings.get("gateway", "model", "") or "")
        or str(settings.get("general", "default_model", "") or "")
    ).strip()

    kwargs: dict = {}
    if backend_type == "ollama":
        kwargs["url"] = resolve_ollama_url(settings_data=settings.get_all())
        if not model:
            # Probe for the first available local model.
            import httpx

            response = httpx.get(f"{kwargs['url']}/api/tags", timeout=5)
            models = [m.get("name", "") for m in response.json().get("models", [])]
            if not models:
                raise SystemExit("No Ollama models installed; pass --model or pull one.")
            model = models[0]
    elif backend_type == "kimi":
        kwargs["api_key"] = str(settings.get("api_keys", "kimi", "") or "")

    return create_backend(backend_type, model=model or None, **kwargs)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="resonant gateway",
        description="Chat with the Resonant agent from Telegram.",
    )
    parser.add_argument("--backend", default="", help="Backend type (ollama, kimi, exo, codex, claude-code)")
    parser.add_argument("--model", default="", help="Model name for the backend")
    parser.add_argument("--token", default="", help="Telegram bot token (overrides settings)")
    parser.add_argument("--allow", action="append", default=[],
                        help="Allowed Telegram chat ID (repeatable; overrides settings)")
    parser.add_argument("--max-tokens", type=int, default=None)
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    settings = SettingsManager()
    token = args.token or str(settings.get("api_keys", "telegram_bot", "") or "")
    allowed = args.allow or list(settings.get("gateway", "allowed_chat_ids", []) or [])

    try:
        backend = _build_backend(settings, args.backend, args.model)
    except Exception as exc:
        print(f"Could not start backend: {exc}", file=sys.stderr)
        raise SystemExit(1)

    try:
        adapter = TelegramChannel(bot_token=token, allowed_chat_ids=allowed)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1)

    backend_name = getattr(backend, "name", "?")
    model_name = getattr(backend, "model", "?")
    print(f"Gateway starting: backend={backend_name} model={model_name}")
    if not allowed:
        print(
            "WARNING: no allowed chat IDs configured. The bot will reply to new "
            "chats with their chat ID so you can add them (gateway.allowed_chat_ids)."
        )

    service = GatewayService(
        adapter=adapter,
        backend=backend,
        max_tokens=args.max_tokens,
    )
    try:
        service.run_forever()
    except KeyboardInterrupt:
        print("Gateway stopped.")


if __name__ == "__main__":
    main()
