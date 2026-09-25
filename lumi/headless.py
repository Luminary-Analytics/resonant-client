"""Run a task without a UI: ``lumi run``, for servers, containers and CI.

    lumi run "Fix the failing test in tests/test_api.py" --mode bypass --trust-project
    git diff origin/main | lumi run - --prompt-file review.md --output jsonl

The run uses the same engine as the desktop app: organization policy,
budgets, usage records, the audit log, file exclusions and the secret scan all
apply. Nothing asks a person: a tool call the permission mode doesn't allow is
refused, and a budget that needs approval stops the run.

A repository's own instructions, notes and ``lumi-policy.json`` allow rules
apply only to trusted projects: ones trusted in the desktop app, or this run
with ``--trust-project``. Only pass it for repositories you trust, since the
agent then follows instructions the repository gives it.

The result is JSON on stdout (``--output json``, the default), plain text
(``--output text``), or every engine event as a JSON line followed by the
result (``--output jsonl``). Exit codes:

* 0: the task completed (answered, changed files, or no change was needed);
* 1: the run failed;
* 2: the command or its configuration is wrong (no model, no key …);
* 3: it stopped for a person: it needs input, is incomplete, was refused an
  action it tried (``denied_calls``), hit a budget, the request limit or
  ``--timeout``.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
import uuid
from typing import Any, Iterable, TextIO

EXIT_OK, EXIT_FAILED, EXIT_USAGE, EXIT_ATTENTION = 0, 1, 2, 3

MODES = {"ask": "suggest", "auto-edit": "auto-edit", "bypass": "full-auto"}
API_KEY_ENV = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
    "sonn": "SONN_API_KEY",
    "kimi": "MOONSHOT_API_KEY",
    "exo": "EXO_API_KEY",
}
SUCCESS_OUTCOMES = {"answered", "changed_verified", "changed_unverified", "no_changes_needed"}
ATTENTION_OUTCOMES = {"needs_input", "incomplete"}


class UsageError(ValueError):
    """The command or its configuration can't run; the message says how to fix it."""


def build_spec(settings: Any, provider: str, model: str, project: str):
    """The backend for a provider and model, with keys from Settings or the environment."""
    from .connections import connection_id_from_backend, find_connection, secret_setting
    from .gui.runtime import BackendSpec
    from .network_defaults import resolve_exo_url, resolve_ollama_url, resolve_sonn_url

    provider = provider.strip().lower()
    if not model:
        raise UsageError(f"Choose a model for {provider} with --model.")
    if provider in {"codex", "claude-code"}:
        if settings.get("security", "cli_adapters", True) is False:
            raise UsageError("Codex and Claude Code are turned off (Settings > Privacy & security, "
                             "or your organization's policy).")
        return BackendSpec(backend_type=provider, model=model, cwd=project)
    if provider == "ollama":
        return BackendSpec(backend_type="ollama", model=model, url=resolve_ollama_url(settings_data=settings.get_all()))
    connection_id = connection_id_from_backend(provider)
    if connection_id:
        if find_connection(settings, connection_id) is None:
            raise UsageError(f"There is no connection called {connection_id!r} in Settings > Connections.")
        return BackendSpec(backend_type=provider, model=model, api_key_source="settings",
                           api_key_setting=secret_setting(connection_id))
    env = API_KEY_ENV.get(provider)
    if env is None:
        raise UsageError(f"Unknown provider {provider!r}. Use anthropic, openai, openrouter, sonn, kimi, exo, "
                         "ollama, codex, claude-code or a connection (conn-<id>).")
    if settings.get("api_keys", provider, ""):
        source, key_env, key_setting = "settings", "", provider
    elif os.environ.get(env):
        source, key_env, key_setting = "env", env, ""
    elif provider == "exo":
        source, key_env, key_setting = "", "", ""
    else:
        raise UsageError(f"No API key for {provider}: set {env}, or add the key in Settings > API keys.")
    spec = BackendSpec(backend_type=provider, model=model, api_key_source=source, api_key_env=key_env,
                       api_key_setting=key_setting)
    if provider == "sonn":
        from .sonn import SonnBackend

        spec.base_url = SonnBackend.validate_base_url(resolve_sonn_url(settings_data=settings.get_all()))
    elif provider == "exo":
        spec.base_url = resolve_exo_url(settings_data=settings.get_all())
    return spec


def build_session(settings: Any, spec: Any, *, project: str, mode: str, trust_project: bool,
                  max_requests: int | None, run_id: str):
    """A session set up as the desktop app would, for one unattended run."""
    from .engine import Session
    from .engine.exclusions import ExclusionRules
    from .engine.policies import project_execution_policy
    from .engine.sandbox import PathSandbox
    from .gui.project_instructions import load_project_instructions
    from .gui.workspace_trust import WorkspaceTrust
    from .policy import current as current_policy

    status = WorkspaceTrust().status(project)
    trusted = trust_project or status.trusted
    backend = spec.create_backend(settings)
    session = Session(
        backend,
        auto_approve=MODES[mode] == "full-auto",
        project_instructions=load_project_instructions(project) if trusted else None,
        max_model_requests=max_requests,
    )
    session.project_path = project
    # File tools stay inside the project, as in the app (gui/app.py).
    session.sandbox = PathSandbox(project, enabled=True)
    session.autonomy_tier = MODES[mode]
    session.execution_policy = project_execution_policy(
        session.autonomy_tier, project, honor_allows=trust_project or status.honor_policy_allows)
    session.exclusions = ExclusionRules.for_project(
        project,
        settings_patterns=lambda: settings.get("privacy", "excluded_paths", []) or [],
        policy_patterns=lambda: current_policy().exclude if current_policy() else (),
    )
    session.project_content_trusted = trusted
    # Tools read Settings as in the app: the autonomy floor, Settings'
    # language servers (engine/lsp.py) and pack skills.
    session._settings_ref = settings
    # Nobody is watching the screen of an unattended run.
    session.computer_use_enabled = False
    session.audit_session_id = f"headless:{run_id}"
    return session


def _fallbacks(settings: Any, requested: list[str], project: str, mode: str):
    """``--fallback`` models, then Settings', each built only if it's needed."""
    from .engine.model_roles import parse_fallback_models, parse_model_ref

    refs = parse_fallback_models(list(requested or []) + list(settings.get("general", "fallback_models", []) or []))
    chain = []
    for ref in refs:
        provider, model = parse_model_ref(ref)

        def factory(provider=provider, model=model):
            spec = build_spec(settings, provider, model, project)
            if provider in {"codex", "claude-code"}:
                spec.permission_mode = mode
            return spec.create_backend(settings)

        chain.append((ref, factory))
    return lambda: chain


def _configure(settings: Any) -> None:
    from . import audit, budgets, net, pricing, secret_scan, usage

    net.configure(settings)
    secret_scan.configure(settings)
    audit.configure(settings)
    pricing.configure(settings)
    usage.configure(settings)
    budgets.configure(settings)
    from .engine import github_tools, os_sandbox

    github_tools.configure(settings)
    os_sandbox.configure(settings)
    from . import connections, policy

    policy.set_zero_retention_resolver(connections.zero_retention_resolver(settings))


def _read_prompt(args: argparse.Namespace, stdin: TextIO) -> str:
    parts = []
    if args.prompt_file:
        with open(args.prompt_file, encoding="utf-8") as handle:
            parts.append(handle.read())
    if args.prompt == "-":
        parts.append(stdin.read())
    elif args.prompt:
        parts.append(args.prompt)
    prompt = "\n\n".join(part.strip() for part in parts if part and part.strip())
    if not prompt:
        raise UsageError("Give the task as an argument, with --prompt-file, or on stdin with '-'.")
    return prompt


def _mode(settings: Any, requested: str) -> str:
    from .policy import current as current_policy

    policy = current_policy()
    if requested:
        if policy and not policy.mode_allowed(requested):
            raise UsageError(f"{policy.organization}'s policy doesn't allow --mode {requested}; "
                             f"allowed: {', '.join(m for m in policy.allowed_modes if m in MODES)}.")
        return requested
    # Unattended runs default to accepting file edits and refusing other
    # actions, whatever the desktop default is.
    mode = "auto-edit"
    if policy and not policy.mode_allowed(mode):
        allowed = [m for m in policy.allowed_modes if m in MODES]
        if not allowed:
            raise UsageError(f"{policy.organization}'s policy allows no mode a headless run can use.")
        mode = allowed[0]
    return mode


def summarize(events: Iterable[dict], *, run_id: str, provider: str, model: str, project: str, mode: str,
              started: float, timed_out: bool) -> dict:
    """The run's result from its events and usage records."""
    from . import usage

    text = ""
    errors: list[dict] = []
    end: dict = {}
    denied = 0
    for event in events:
        kind = event.get("event")
        if event.get("_subagent"):
            continue
        if kind == "text.done" and event.get("text"):
            text = str(event["text"])
        elif kind == "tool.result" and event.get("denied"):
            denied += 1
        elif kind == "error":
            errors.append({"message": str(event.get("message") or ""), "code": str(event.get("code") or "")})
        elif kind == "session.end":
            end = event
    evidence = end.get("evidence") or {}
    outcome = str(end.get("outcome") or ("failed" if errors else "incomplete"))
    codes = {error["code"] for error in errors}
    if timed_out:
        status = "timeout"
    elif "budget_exceeded" in codes:
        status = "budget_exceeded"
    elif evidence.get("request_limit_reached") or "request_limit_reached" in codes:
        status = "request_limit"
    elif outcome in SUCCESS_OUTCOMES and denied and outcome != "changed_verified":
        # The agent was refused something it tried; nobody could approve it.
        status = "needs_attention"
    elif outcome in SUCCESS_OUTCOMES and not [e for e in errors if e["code"] not in {"", "empty_response"}]:
        status = "completed"
    elif outcome in ATTENTION_OUTCOMES:
        status = "needs_attention"
    else:
        status = "failed"
    rows = [row for row in usage.ledger().records() if row.get("session") == f"headless:{run_id}"]
    totals = usage.totals(rows)
    return {
        "version": 1,
        "run_id": run_id,
        "status": status,
        "outcome": outcome,
        "text": text,
        "errors": errors,
        "provider": provider,
        "model": model,
        "mode": mode,
        "project": project,
        "changed_files": list(evidence.get("changed_files") or []),
        "checks": list(evidence.get("checks") or []),
        "tool_calls": int(evidence.get("tool_calls") or 0),
        "denied_calls": denied,
        "model_requests": int(evidence.get("model_requests") or 0),
        "usage": {key: totals[key] for key in ("calls", "input_tokens", "output_tokens", "cost_usd", "unpriced_calls")},
        "elapsed": round(time.time() - started, 3),
    }


def exit_code(result: dict) -> int:
    return {"completed": EXIT_OK, "failed": EXIT_FAILED}.get(result["status"], EXIT_ATTENTION)


def main(argv: list[str] | None = None, *, stdin: TextIO | None = None, stdout: TextIO | None = None,
         stderr: TextIO | None = None) -> int:
    stdin, stdout, stderr = stdin or sys.stdin, stdout or sys.stdout, stderr or sys.stderr
    parser = argparse.ArgumentParser(prog="lumi run", description="Run one task without a UI and report the result.")
    parser.add_argument("prompt", nargs="?", default="", help="the task, or '-' to read it from stdin")
    parser.add_argument("--prompt-file", help="read the task (or extra context) from a file")
    parser.add_argument("--project", default="", help="the project folder (default: the current folder)")
    parser.add_argument("--provider", default=os.environ.get("LUMI_PROVIDER", ""),
                        help="anthropic, openai, openrouter, sonn, kimi, exo, ollama, codex, claude-code or conn-<id>")
    parser.add_argument("--model", default=os.environ.get("LUMI_MODEL", ""))
    parser.add_argument("--mode", choices=sorted(MODES), default="",
                        help="what the agent may do without asking (default: auto-edit)")
    parser.add_argument("--trust-project", action="store_true",
                        help="apply the repository's instructions, notes and policy allow rules for this run")
    parser.add_argument("--fallback", action="append", default=[], metavar="PROVIDER:MODEL",
                        help="a model to continue with if a request fails (repeatable; adds to Settings')")
    parser.add_argument("--max-requests", type=int, default=0, help="stop after this many model requests")
    parser.add_argument("--timeout", type=float, default=0, help="stop after this many seconds")
    parser.add_argument("--output", choices=("json", "text", "jsonl"), default="json")
    args = parser.parse_args(argv)

    started = time.time()
    run_id = uuid.uuid4().hex
    try:
        from .gui.settings import SettingsManager
        from .policy import blocked_reason
        from .policy import current as current_policy

        prompt = _read_prompt(args, stdin)
        project = os.path.abspath(args.project or os.getcwd())
        if not os.path.isdir(project):
            raise UsageError(f"{project} is not a folder.")
        settings = SettingsManager()
        _configure(settings)
        refusal = blocked_reason()
        if refusal:
            raise UsageError(refusal)
        provider = (args.provider or str(settings.get("general", "default_backend", "") or "")).strip().lower()
        if not provider:
            raise UsageError("Choose a provider with --provider (or set LUMI_PROVIDER).")
        model = args.model or (str(settings.get("general", "default_model", "") or "")
                               if provider == str(settings.get("general", "default_backend", "") or "") else "")
        policy = current_policy()
        if policy and model and not policy.model_allowed(provider, model):
            raise UsageError(f"{policy.organization}'s policy doesn't allow {model} on {provider}.")
        mode = _mode(settings, args.mode)
        spec = build_spec(settings, provider, model, project)
        if provider in {"codex", "claude-code"}:
            spec.permission_mode = mode
        session = build_session(settings, spec, project=project, mode=mode, trust_project=args.trust_project,
                                max_requests=args.max_requests or None, run_id=run_id)
        session.fallback_provider = _fallbacks(settings, args.fallback, project, mode)
    except (UsageError, ValueError, OSError) as exc:
        stderr.write(f"lumi run: {exc}\n")
        return EXIT_USAGE

    timed_out = threading.Event()
    timer = None
    if args.timeout and args.timeout > 0:
        def stop() -> None:
            timed_out.set()
            session.cancel()

        timer = threading.Timer(args.timeout, stop)
        timer.daemon = True
        timer.start()

    events: list[dict] = []
    try:
        for event in session.run(prompt):
            events.append(event)
            if args.output == "jsonl":
                stdout.write(json.dumps(event, default=str) + "\n")
                stdout.flush()
            elif args.output == "text" and event.get("event") == "text.delta" and not event.get("_subagent"):
                stdout.write(str(event.get("text") or event.get("delta") or ""))
                stdout.flush()
    except KeyboardInterrupt:
        session.cancel()
        timed_out.clear()
    finally:
        if timer is not None:
            timer.cancel()

    result = summarize(events, run_id=run_id, provider=provider, model=model, project=project, mode=mode,
                       started=started, timed_out=timed_out.is_set())
    from . import activity

    activity.record_turn(events, cancelled=timed_out.is_set())
    if args.output == "json":
        stdout.write(json.dumps(result, indent=2) + "\n")
    elif args.output == "jsonl":
        stdout.write(json.dumps({"event": "lumi.result", **result}) + "\n")
    else:
        stdout.write("\n")
        stderr.write(f"lumi run: {result['status']} ({result['outcome']}); "
                     f"{result['usage']['calls']} model calls, ${result['usage']['cost_usd']:.4f}\n")
    return exit_code(result)
