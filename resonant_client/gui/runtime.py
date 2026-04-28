"""
Runtime helpers shared by the GUI app's session/backend wiring.

Hosts BackendSpec, the serializable backend configuration used to recreate
backends across session loads / reconnects.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Optional

from ..backends import create_backend


def _normalize_source(value: str) -> str:
    return value if value in {"env", "settings", "literal"} else ""


@dataclass
class BackendSpec:
    """Serializable backend configuration for recreating a backend later."""

    backend_type: str
    model: str = ""
    url: str = ""
    base_url: str = ""
    local_root: str = ""
    cwd: str = ""
    permission_mode: str = ""
    api_key_source: str = ""
    api_key_env: str = ""
    api_key_setting: str = ""
    api_key: str = ""
    # deepseek-v4-flash et al. thinking mode: "low" | "med" | "high" | ""
    thinking_mode: str = ""

    def to_dict(self, include_sensitive: bool = False) -> dict[str, Any]:
        data = {
            "backend_type": self.backend_type,
            "model": self.model,
            "url": self.url,
            "base_url": self.base_url,
            "local_root": self.local_root,
            "cwd": self.cwd,
            "permission_mode": self.permission_mode,
            "api_key_source": self.api_key_source,
            "api_key_env": self.api_key_env,
            "api_key_setting": self.api_key_setting,
            "thinking_mode": self.thinking_mode,
        }
        if include_sensitive and self.api_key:
            data["api_key"] = self.api_key
        return data

    @classmethod
    def from_dict(cls, data: Optional[dict[str, Any]]) -> "BackendSpec":
        data = data or {}
        return cls(
            backend_type=str(data.get("backend_type", "")),
            model=str(data.get("model", "")),
            url=str(data.get("url", "")),
            base_url=str(data.get("base_url", "")),
            local_root=str(data.get("local_root", "")),
            cwd=str(data.get("cwd", "")),
            permission_mode=str(data.get("permission_mode", "")),
            api_key_source=_normalize_source(str(data.get("api_key_source", ""))),
            api_key_env=str(data.get("api_key_env", "")),
            api_key_setting=str(data.get("api_key_setting", "")),
            api_key=str(data.get("api_key", "")),
            thinking_mode=str(data.get("thinking_mode", "")),
        )

    def resolve_api_key(self, settings=None) -> str:
        if self.api_key:
            return self.api_key
        if self.api_key_source == "settings" and settings and self.api_key_setting:
            return str(settings.get("api_keys", self.api_key_setting, "") or "")
        if self.api_key_source == "env" and self.api_key_env:
            return os.environ.get(self.api_key_env, "")
        return ""

    def create_backend(self, settings=None):
        backend_type = self.backend_type

        if backend_type == "resonant":
            return create_backend("resonant", url=self.url)

        if backend_type == "ollama":
            return create_backend(
                "ollama",
                url=self.url,
                model=self.model,
                thinking=self.thinking_mode or None,
            )

        if backend_type == "mlx":
            return create_backend("mlx", model=self.model, local_root=self.local_root)

        if backend_type == "claude":
            return create_backend(
                "claude",
                api_key=self.resolve_api_key(settings),
                model=self.model,
            )

        if backend_type == "openai":
            return create_backend(
                "openai",
                api_key=self.resolve_api_key(settings),
                model=self.model,
            )

        if backend_type == "lmstudio":
            api_key = self.resolve_api_key(settings) or "lm-studio"
            return create_backend(
                "lmstudio",
                api_key=api_key,
                model=self.model,
                base_url=self.base_url,
            )

        if backend_type == "claude-code":
            return create_backend(
                "claude-code",
                model=self.model,
                cwd=self.cwd,
                permission_mode=self.permission_mode or "bypassPermissions",
            )

        if backend_type == "codex":
            return create_backend(
                "codex",
                model=self.model,
                cwd=self.cwd,
            )

        raise ValueError(f"Unknown backend: {backend_type}")
