"""Shared endpoint and default-backend resolution helpers."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Mapping

_SETTINGS_PATH = Path.home() / ".resonant" / "settings.json"
_DEFAULT_OLLAMA_URL = "http://127.0.0.1:11434"
_DEFAULT_EXO_URL = "http://127.0.0.1:52415/v1"


def _load_settings(path: Path | None = None) -> dict[str, Any]:
    target = path or _SETTINGS_PATH
    try:
        if target.exists():
            return json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        pass
    return {}


def _get_setting(
    section: str,
    key: str,
    default: str = "",
    *,
    settings_data: Mapping[str, Any] | None = None,
) -> str:
    data = settings_data if settings_data is not None else _load_settings()
    section_data = data.get(section, {}) if isinstance(data, Mapping) else {}
    if isinstance(section_data, Mapping):
        value = section_data.get(key, default)
        return str(value or default)
    return str(default)


def get_default_backend(*, settings_data: Mapping[str, Any] | None = None) -> str:
    # Ollama is the zero-credential local default; settings can select another
    # installed provider.
    return (
        str(os.environ.get("RESONANT_DEFAULT_BACKEND", "") or "").strip()
        or _get_setting("general", "default_backend", "ollama", settings_data=settings_data).strip()
        or "ollama"
    )


def get_default_model(*, settings_data: Mapping[str, Any] | None = None) -> str:
    """Return an explicitly configured model, or empty for auto-discovery."""
    return (
        str(os.environ.get("RESONANT_DEFAULT_MODEL", "") or "").strip()
        or _get_setting("general", "default_model", "", settings_data=settings_data).strip()
    )


def default_thinking_for_model(model: str | None) -> str:
    """Leave reasoning effort under user and provider control by default."""
    return ""


def resolve_ollama_url(
    explicit: str | None = None,
    *,
    settings_data: Mapping[str, Any] | None = None,
) -> str:
    """Resolve the Ollama base URL.

    Resolution order:
      1. `explicit` argument (e.g. CLI flag).
      2. `OLLAMA_HOST` env var (Ollama's own convention).
      3. `network.ollama_url` in settings.json.
      4. Local Ollama default (`http://127.0.0.1:11434`).
    """
    return (
        str(explicit or "").strip()
        or str(os.environ.get("OLLAMA_HOST", "") or "").strip()
        or _get_setting(
            "network",
            "ollama_url",
            _DEFAULT_OLLAMA_URL,
            settings_data=settings_data,
        ).strip()
        or _DEFAULT_OLLAMA_URL
    ).rstrip("/")


def resolve_exo_url(
    explicit: str | None = None,
    *,
    settings_data: Mapping[str, Any] | None = None,
) -> str:
    """Resolve EXO's OpenAI-compatible API base URL."""
    value = (
        str(explicit or "").strip()
        or str(os.environ.get("EXO_API_URL", "") or "").strip()
        or str(os.environ.get("EXO_BASE_URL", "") or "").strip()
        or _get_setting(
            "network",
            "exo_url",
            _DEFAULT_EXO_URL,
            settings_data=settings_data,
        ).strip()
        or _DEFAULT_EXO_URL
    ).rstrip("/")
    return value if value.endswith("/v1") else f"{value}/v1"


# v0.4.4 (T1.4) — `resolve_resonant_api_url` and
# `resolve_remote_engine_ws_url` were removed in this release.
# ResonantBackend was cut in v0.4.0 and these resolvers had no other
# call sites. If a future feature needs a generic "what's the
# Resonant API URL" lookup, copy the pattern from `resolve_ollama_url`.
