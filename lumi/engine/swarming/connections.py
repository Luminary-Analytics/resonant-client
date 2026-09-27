"""Lumi connections a team worker may use: OpenAI-compatible endpoints only.

A connection (lumi/connections.py) is data: a Chat Completions endpoint such as
NVIDIA NIM, vLLM or an internal gateway, how to authenticate and which models to
offer. Its backend (``OpenAICompatibleBackend``) derives from the Kimi-format
adapter the native providers use, so a worker's generation stays exactly one
request while the team supervises it (``_supervised_single_request``).

Other connection types keep their own wire formats, which the team's request
accounting and failure tests don't cover yet. Sign-in flows (OAuth, Entra ID)
and client certificates stay unavailable too: a worker process would have to
exchange a client secret or read a key file from outside its workspace.
"""
from __future__ import annotations

from typing import Any

from ...connections import connection_id_from_backend, create_connection_backend, find_connection, normalize_connection
from .policy import is_connection_provider

_AUTH = frozenset({"bearer", "header", "none"})


def team_connection(raw: Any) -> dict[str, Any]:
    """Validate a connection for team workers; the ValueError says why it can't be used."""
    connection = normalize_connection(raw)
    name = connection["name"]
    if connection["type"] != "openai-compatible":
        raise ValueError(f"{name} isn't an OpenAI-compatible connection, which team workers need.")
    if connection["auth"] not in _AUTH:
        raise ValueError(f"{name} signs in with {connection['auth']}. Team workers use a key or no authentication.")
    if connection["client_cert"] or connection["client_key"]:
        raise ValueError(f"{name} uses a client certificate, which team workers don't support yet.")
    return connection


def resolve(settings: Any, backend_type: str) -> dict[str, Any] | None:
    """The validated connection behind ``conn-<id>``; None for a native provider."""
    if not is_connection_provider(backend_type):
        return None
    connection = find_connection(settings, connection_id_from_backend(backend_type))
    if connection is None:
        raise ValueError("This session's connection was removed. Choose another model.")
    return team_connection(connection)


def create_backend(connection: dict[str, Any], spec: Any):
    """A fresh backend for one worker from a validated connection and its captured spec."""
    return create_connection_backend(team_connection(connection), spec.model, spec.api_key,
                                     thinking=spec.thinking_mode or None)
