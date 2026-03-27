"""Shared endpoint and default-backend resolution helpers."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Mapping

_SETTINGS_PATH = Path.home() / ".resonant" / "settings.json"
_DEFAULT_RESONANT_API_URL = "http://localhost:8000"
_DEFAULT_REMOTE_ENGINE_WS_URL = "ws://localhost:8765"


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
    return (
        str(os.environ.get("RESONANT_DEFAULT_BACKEND", "") or "").strip()
        or _get_setting("general", "default_backend", "", settings_data=settings_data).strip()
    )


def get_default_model(*, settings_data: Mapping[str, Any] | None = None) -> str:
    return (
        str(os.environ.get("RESONANT_DEFAULT_MODEL", "") or "").strip()
        or _get_setting("general", "default_model", "", settings_data=settings_data).strip()
    )


def resolve_resonant_api_url(
    explicit: str | None = None,
    *,
    settings_data: Mapping[str, Any] | None = None,
) -> str:
    return (
        str(explicit or "").strip()
        or str(os.environ.get("RESONANT_API", "") or "").strip()
        or _get_setting(
            "network",
            "resonant_api_url",
            _DEFAULT_RESONANT_API_URL,
            settings_data=settings_data,
        ).strip()
        or _DEFAULT_RESONANT_API_URL
    ).rstrip("/")


def resolve_remote_engine_ws_url(
    explicit: str | None = None,
    *,
    settings_data: Mapping[str, Any] | None = None,
) -> str:
    return (
        str(explicit or "").strip()
        or str(os.environ.get("RESONANT_ENGINE_WS_URL", "") or "").strip()
        or str(os.environ.get("RESONANT_REMOTE_ENGINE_WS_URL", "") or "").strip()
        or _get_setting(
            "network",
            "remote_engine_ws_url",
            _DEFAULT_REMOTE_ENGINE_WS_URL,
            settings_data=settings_data,
        ).strip()
        or _DEFAULT_REMOTE_ENGINE_WS_URL
    )
