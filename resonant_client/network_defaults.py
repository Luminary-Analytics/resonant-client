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
    # v0.6.5 — switched the flagship default to glm-5.2:cloud (756B,
    # 1M context, native tool calling). GLM-5.2 is the out-of-the-box
    # model on the Mac Studio now; the deepseek-v4 tiers stay one
    # click away in the model dropdown as the secondary high-quality
    # option (and on a separate cloud quota, so they double as the
    # 503 fallback when the GLM endpoint is overloaded).
    #
    # History: v0.5.2 moved the default flash → pro after the v0.5.1
    # GA smoke (pro's PLAN_DEEP convergence beat flash on the
    # benchmarked specs — see docs/v0.5.1-smoke-results.md and
    # RESONANT.md for tier guidance). That reasoning still holds for
    # the deepseek pair; GLM-5.2 now leads.
    #
    # Override via the RESONANT_DEFAULT_MODEL env var or the
    # `general.default_model` setting.
    return (
        str(os.environ.get("RESONANT_DEFAULT_MODEL", "") or "").strip()
        or _get_setting("general", "default_model", "glm-5.2:cloud", settings_data=settings_data).strip()
        or "glm-5.2:cloud"
    )


def default_thinking_for_model(model: str | None) -> str:
    """v0.6.5 — default reasoning-effort token for a freshly-built
    backend. GLM-5.x is a reasoning model that produces its best
    agentic output with thinking ON (tools + thinking are confirmed
    compatible — verified live 2026-06-17 against glm-5.2:cloud), so it
    defaults to "high". Every other model defaults to "" (off); the user
    opts in via the thinking toggle. Returns an INTERNAL token
    ("high"/"med"/"low"/"") — OllamaBackend maps it to the model-correct
    wire value (deepseek wants "med", standard Ollama models want
    "medium")."""
    base = (model or "").split(":")[0].lower()
    if base.startswith("glm-5"):
        return "high"
    return ""


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
