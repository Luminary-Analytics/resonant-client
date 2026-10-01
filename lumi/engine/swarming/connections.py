"""The models a team participant may run on: which providers and connections, and why not.

A participant (a worker or an orchestrator turn) runs under the supervised
request contract (engine/execution_guard.py): each admitted request is one
generation, a refusal says whether anything was generated, and Lumi runs every
tool call against the assignment's paths. An adapter declares that it keeps
the contract with ``supervised_requests`` (lumi/backends.py). The native
providers whose adapters do are ``policy.NATIVE_PROVIDERS``: Anthropic, OpenAI,
OpenRouter, Ollama, EXO, Kimi and SONN. A connection (lumi/connections.py) is
data; it qualifies when its adapter does (OpenAI-compatible Chat Completions,
the OpenAI Responses API including Azure OpenAI, and the Anthropic Messages API
including Claude on Bedrock) and it authenticates with a key or none.

A participant holds a model key and never the person's sign-in: its process
would otherwise sign in with their cloud identity (the AWS credential chain
for Bedrock without a Bedrock API key, Google credentials for Vertex AI), or
exchange a client secret (OAuth, Entra ID) or read a client certificate's key
file from outside its workspace. A capability pack's provider runs its own
process, which nothing yet shows keeps the contract.

Codex and Claude Code run their own tool loops: one turn is many model calls
and tool calls, shell included, that Lumi only observes after the fact, so a
team can't hold them to an assignment's paths and tools, count their requests
or pause them between steps. They can't run a team (docs/swarming.md).
"""
from __future__ import annotations

import os
from typing import Any

from ...connections import (connection_backend_class, connection_id_from_backend, create_connection_backend,
                            find_connection, list_connections, normalize_connection, secret_setting)
from .policy import NATIVE_PROVIDERS, is_connection_provider

_AUTH = frozenset({"bearer", "header", "none"})
_CLI = {"codex": "Codex", "claude-code": "Claude Code", "claude_code": "Claude Code"}
# What a team runs on, for refusals and the Team panel.
SUPPORTED = ("Team runs on Anthropic, OpenAI, OpenRouter, Ollama, EXO, Kimi and SONN, and on connections "
             "that use a key: OpenAI-compatible (such as NVIDIA NIM), OpenAI, Azure OpenAI, Anthropic, or Claude on "
             "Bedrock with a Bedrock API key.")


def connection_refusal(connection: dict[str, Any]) -> str:
    """Why a normalized connection can't run a team participant, or ''."""
    name = connection["name"]
    kind = connection["type"]
    if getattr(connection_backend_class(kind), "supervised_requests", False) is not True:
        return (f"{name} runs a capability pack's provider in its own process, and a team can't count its "
                "requests yet.")
    if kind == "anthropic-vertex":
        return (f"{name} signs in to Vertex AI with your Google account, and a team's participants use a key, "
                "never your sign-in.")
    auth = connection["auth"]
    if auth == "aws":
        return (f"{name} signs in with your AWS credentials, and a team's participants use a key, never your "
                "sign-in. Give the connection a Bedrock API key instead.")
    if auth not in _AUTH:
        return f"{name} signs in with {auth}. A team's participants use a key or no authentication."
    if connection["client_cert"] or connection["client_key"]:
        return f"{name} uses a client certificate, which a team's participants don't support yet."
    return ""


def team_connection(raw: Any) -> dict[str, Any]:
    """Validate a connection for team participants; the ValueError says why it can't be used."""
    connection = normalize_connection(raw)
    reason = connection_refusal(connection)
    if reason:
        raise ValueError(reason)
    return connection


def resolve(settings: Any, backend_type: str) -> dict[str, Any] | None:
    """The validated connection behind ``conn-<id>``; None for a native provider."""
    if not is_connection_provider(backend_type):
        return None
    connection = find_connection(settings, connection_id_from_backend(backend_type))
    if connection is None:
        raise ValueError("This session's connection was removed. Choose another model.")
    return team_connection(connection)


def provider_refusal(provider: str, model: str = "", *, workers: bool = False) -> str:
    """Why a provider can't run a team, saying what can; '' for one that can (a connection is checked apart).

    ``workers``: the owner's choice of the workers' model rather than the conversation's.
    """
    whose = "Team workers" if workers else "Team"
    if provider in _CLI:
        cli = _CLI[provider]
        return (f"{whose} can't run on {cli}: {cli} runs its own tool loop, which a team can't limit to each "
                f"task's folders and tools, count request by request or pause between steps. {SUPPORTED}"
                + ("" if workers else " Switch this conversation to one of them to start a team."))
    if not provider or not model:
        return (f"Choose the workers' provider and model. {SUPPORTED}" if workers
                else f"Choose a model for this conversation before starting a team. {SUPPORTED}")
    if provider not in NATIVE_PROVIDERS and not is_connection_provider(provider):
        return f"{whose} can't run on {provider}. {SUPPORTED}"
    return ""


def participant_key(connection: dict[str, Any] | None, api_key: str) -> str:
    """The key a participant on this captured connection sends.

    Its own key; for Claude on Bedrock without one, the Bedrock API key in
    ``AWS_BEARER_TOKEN_BEDROCK``, read here in the app: a participant's
    process gets its key in its private start message, and its environment
    carries no credentials (process_worker.worker_environment).
    """
    if api_key or connection is None or connection["type"] != "anthropic-bedrock":
        return api_key
    return os.environ.get("AWS_BEARER_TOKEN_BEDROCK", "")


def key_refusal(connection: dict[str, Any] | None, api_key: str) -> str:
    """Why a captured connection has no usable key for a participant, or ''.

    Claude on Bedrock without a Bedrock API key (the connection's key or
    ``AWS_BEARER_TOKEN_BEDROCK``) would sign requests with the AWS credential chain.
    """
    if connection is None or connection["type"] != "anthropic-bedrock" or participant_key(connection, api_key):
        return ""
    return (f"Add {connection['name']}'s Bedrock API key in Settings > Connections: a team's participants use a "
            "key, never your AWS sign-in.")


def participant_refusal(backend: Any) -> str:
    """Why a participant's constructed backend can't run under the team, or ''.

    An adapter takes part only when it declares the supervised request
    contract; one that says nothing (a wrapper, a new adapter) is refused.
    """
    if getattr(backend, "handles_tools", False) or getattr(backend, "supervised_requests", None) is not True:
        return "A team participant needs a model whose tool calls Lumi runs; CLI tool loops can't take part."
    if getattr(backend, "uses_sign_in", False) is True:
        return "A team participant authenticates with a key, never a sign-in."
    return ""


def team_providers(settings: Any) -> list[str]:
    """Every provider and connection name a team can run on, for the Team panel's model choices.

    Claude on Bedrock is offered only with a Bedrock API key to send (key_refusal).
    """
    names = set(NATIVE_PROVIDERS)
    for connection in list_connections(settings):
        if connection_refusal(connection):
            continue
        if connection["type"] == "anthropic-bedrock":
            try:
                saved = str(settings.get("api_keys", secret_setting(connection["id"]), "") or "")
            except TypeError:  # a plain mapping of settings (tests) has no section lookup
                saved = ""
            if key_refusal(connection, saved):
                continue
        names.add("conn-" + connection["id"])
    return sorted(names)


def create_backend(connection: dict[str, Any], spec: Any):
    """A fresh backend for one participant from a validated connection and its captured spec."""
    return create_connection_backend(team_connection(connection), spec.model, spec.api_key,
                                     thinking=spec.thinking_mode or None)
