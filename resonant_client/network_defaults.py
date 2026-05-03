"""Shared endpoint and default-backend resolution helpers."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Mapping

_SETTINGS_PATH = Path.home() / ".resonant" / "settings.json"
# v0.4.0 — Mac Studio at 10.0.0.133 hosts Ollama in the canonical
# Resonant deployment (per the user's infra). The fallback below it is
# the localhost path for users running Ollama on the same box. The
# welcome-screen wizard prompts for a custom URL when neither
# responds. Override via OLLAMA_HOST env or `network.ollama_url`
# in `~/.resonant/settings.json`.
_DEFAULT_OLLAMA_URL_PRIMARY = "http://10.0.0.133:11434"
_DEFAULT_OLLAMA_URL_FALLBACK = "http://localhost:11434"


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
    # Default to Ollama (Mac Studio) — env var or settings can override.
    return (
        str(os.environ.get("RESONANT_DEFAULT_BACKEND", "") or "").strip()
        or _get_setting("general", "default_backend", "ollama", settings_data=settings_data).strip()
        or "ollama"
    )


def get_default_model(*, settings_data: Mapping[str, Any] | None = None) -> str:
    # v0.5.2 — switched default from deepseek-v4-flash:cloud to
    # deepseek-v4-pro:cloud after the v0.5.1 GA smoke showed pro
    # converges on autonomous missions FASTER than flash (135s vs
    # 340s on the wordcount spec) when paired with the PLAN_DEEP
    # specialist. Pro's deeper planning produces tighter implementer
    # subgoals, and total mission time drops despite per-call
    # latency being higher. See docs/v0.5.1-smoke-results.md for the
    # data and `RESONANT.md` for tier guidance.
    #
    # Users who want flash for quick one-shot work can still pick it
    # in the model dropdown (the autonomous daemon auto-routes the
    # planner regardless of which tier the user picks).
    return (
        str(os.environ.get("RESONANT_DEFAULT_MODEL", "") or "").strip()
        or _get_setting("general", "default_model", "deepseek-v4-pro:cloud", settings_data=settings_data).strip()
        or "deepseek-v4-pro:cloud"
    )


def resolve_ollama_url(
    explicit: str | None = None,
    *,
    settings_data: Mapping[str, Any] | None = None,
) -> str:
    """v0.4.0 — resolve the Ollama base URL.

    Resolution order:
      1. `explicit` argument (e.g. CLI flag).
      2. `OLLAMA_HOST` env var (Ollama's own convention).
      3. `network.ollama_url` in settings.json.
      4. Mac Studio default (`http://10.0.0.133:11434`) — see the
         module-level comment for why this is the primary default
         rather than localhost.

    The fallback to localhost is intentionally NOT in this function:
    `detect_backends` probes the resolved URL once, and if that
    fails, the welcome-screen wizard takes over and prompts the user
    for the right URL. Silent-fallback-to-localhost would mask the
    real "Ollama isn't where you think it is" misconfiguration.
    """
    return (
        str(explicit or "").strip()
        or str(os.environ.get("OLLAMA_HOST", "") or "").strip()
        or _get_setting(
            "network",
            "ollama_url",
            _DEFAULT_OLLAMA_URL_PRIMARY,
            settings_data=settings_data,
        ).strip()
        or _DEFAULT_OLLAMA_URL_PRIMARY
    ).rstrip("/")


# v0.4.4 (T1.4) — `resolve_resonant_api_url` and
# `resolve_remote_engine_ws_url` were removed in this release.
# ResonantBackend was cut in v0.4.0 and these resolvers had no other
# call sites. If a future feature needs a generic "what's the
# Resonant API URL" lookup, copy the pattern from `resolve_ollama_url`.
