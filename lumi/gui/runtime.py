"""
Runtime helpers shared by the GUI app's session/backend wiring.

Hosts BackendSpec, the serializable backend configuration used to recreate
backends across session loads / reconnects.
"""

from __future__ import annotations

import os
import hashlib
import json
from dataclasses import dataclass
from typing import Any, Optional

from ..backends import create_backend
from ..connections import connection_id_from_backend, create_connection_backend, find_connection


def bind_sonn_conversation(backend, project_path: str, session_id: str) -> bool:
    """Bind SONN requests to a saved conversation without exposing local paths.

    Recompute from durable GUI identity before each turn, including restored
    sessions and provider switches. Other providers keep their own contracts.
    """
    from ..sonn import SonnBackend

    if not isinstance(backend, SonnBackend):
        return False
    if not str(project_path or "").strip() or not str(session_id or "").strip():
        raise ValueError("SONN requires a project and saved session before generation")
    project = os.path.normcase(os.path.abspath(project_path))
    identity = json.dumps([project, str(session_id)], ensure_ascii=False)
    backend.conversation_id = "sonn-client:" + hashlib.sha256(identity.encode("utf-8")).hexdigest()
    return True


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
    # Thinking mode: "low" | "med" | "high" | "max" | "off" | "default" | ""
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
        if backend_type == "ollama":
            return create_backend(
                "ollama",
                url=self.url,
                model=self.model,
                thinking=self.thinking_mode or None,
            )
        if backend_type == "codex":
            backend = create_backend(
                "codex",
                model=self.model,
                cwd=self.cwd or None,
                permission_mode=self.permission_mode or None,
            )
            backend._editor_settings = settings
            return backend
        if backend_type == "claude-code":
            backend = create_backend(
                "claude-code",
                model=self.model,
                cwd=self.cwd or None,
                permission_mode=self.permission_mode or None,
            )
            backend._editor_settings = settings
            return backend
        if backend_type in ("anthropic", "openai"):
            return create_backend(backend_type, model=self.model, api_key=self.resolve_api_key(settings),
                                  base_url=self.base_url or None, thinking=self.thinking_mode or None)
        connection_id = connection_id_from_backend(backend_type)
        if connection_id:
            connection = find_connection(settings, connection_id)
            if connection is None:
                raise ValueError("This session's connection was removed. Choose another model.")
            return create_connection_backend(connection, self.model, self.resolve_api_key(settings),
                                             thinking=self.thinking_mode or None)
        if backend_type == "openrouter":
            return create_backend("openrouter", model=self.model,
                                  api_key=self.resolve_api_key(settings), thinking=self.thinking_mode or None)
        if backend_type == "sonn":
            return create_backend("sonn", model=self.model, base_url=self.base_url,
                                  api_key=self.resolve_api_key(settings))
        if backend_type == "kimi":
            return create_backend(
                "kimi",
                model=self.model,
                api_key=self.resolve_api_key(settings),
                base_url=self.base_url or None,
                thinking=self.thinking_mode or None,
            )
        if backend_type == "exo":
            return create_backend(
                "exo",
                model=self.model,
                api_key=self.resolve_api_key(settings),
                base_url=self.base_url or None,
            )

        raise ValueError(
            f"Backend '{backend_type}' is not supported. Lumi supports Anthropic, OpenAI, "
            f"Ollama, EXO, Kimi, OpenRouter, SONN, Codex, Claude Code and custom connections."
        )
