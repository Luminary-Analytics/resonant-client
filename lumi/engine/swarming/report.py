"""Owner-scoped metadata reports with explicit accounting uncertainty.

This is a local export, not a protected enterprise audit or a quality score.
Freeform prompts, messages, handoffs, check output, tool arguments, credentials,
and private operating-system handles are deliberately absent from the format.
"""

from __future__ import annotations

from collections import Counter
from decimal import Decimal
import json
import math

from .models import Scope
from .store import SwarmStore


def _pick(row, *fields):
    return {field: row.get(field) for field in fields}


def _number(value, *, integral=False):
    if (type(value) not in (int, float) or value < 0 or value > 2**53 - 1
            or not math.isfinite(value)):
        return None
    return int(value) if integral and value == int(value) else (None if integral else value)


def _usage(row):
    try:
        usage = json.loads(row.get("usage_json") or "null")
    except (TypeError, ValueError):
        usage = None
    if type(usage) is not dict:
        usage = {}
    return {"input_tokens": _number(usage.get("input_tokens", usage.get("prompt_tokens")), integral=True),
            "output_tokens": _number(usage.get("output_tokens", usage.get("completion_tokens")), integral=True),
            "reported_usd": _number(usage.get("cost_usd"))}


def export_report(store: SwarmStore, scope: Scope, run_id: str) -> dict:
    """Export one captured owner's run, without model/session content access."""
    snapshot = store.snapshot(scope, run_id)
    run = snapshot["run"]
    policy = json.loads(run["policy_json"])
    inputs = {row["request_id"]: row for row in snapshot["request_inputs"]}
    requests = []
    for row in snapshot["model_requests"]:
        source = inputs.get(row["id"], {})
        requests.append({**_pick(row, "id", "attempt_id", "epoch", "purpose", "state", "used"),
                         **_pick(source, "input_sha256", "observation_outcome"), "usage": _usage(source)})
    usage_summary = {}
    for key in ("input_tokens", "output_tokens", "reported_usd"):
        known = [row["usage"][key] for row in requests if row["usage"][key] is not None]
        subtotal = sum(known) if key != "reported_usd" else str(sum((Decimal(str(value)) for value in known), Decimal(0)))
        usage_summary[key] = {"known_subtotal": subtotal if known else None,
                              "reported_requests": len(known), "unknown_requests": len(requests) - len(known),
                              "complete": bool(requests) and len(known) == len(requests)}
    usage_summary.update(estimated_usd=None, reserved_usd=None)
    # Restrict timeline queries to the atomic snapshot's cursor. Later events
    # cannot make this report claim a completion that its run snapshot lacks.
    with store._connection() as connection:
        store._run(connection, scope, run_id)
        rows = connection.execute("SELECT sequence,epoch,kind,occurred_at,run_state FROM events WHERE run_id=? AND sequence<=? "
                                  "ORDER BY sequence", (run_id, run["event_sequence"])).fetchall()
    events = [{**dict(row), "occurred_at": _number(row["occurred_at"])} for row in rows]
    started = events[0]["occurred_at"] if events else None
    terminal = run["state"] in {"completed", "cancelled", "failed"}
    terminal_event = next((event for event in events if event["run_state"] in {"completed", "cancelled", "failed"}), None)
    ended = terminal_event["occurred_at"] if terminal and terminal_event else None
    now = _number(store.clock())
    # A legacy/missing terminal marker says nothing about when the run ended.
    # Never substitute the current clock or a later terminal audit observation.
    endpoint = ended if terminal else now
    elapsed = (endpoint - started if _number(started) is not None and _number(endpoint) is not None
               and endpoint >= started else None)
    attempts = []
    for row in snapshot["attempts"]:
        grant = json.loads(row["grant_json"])
        attempts.append({**_pick(row, "id", "work_item_id", "worker_id", "kind", "epoch", "state", "process_state", "pause_requested", "cancel_requested"),
                         "model": _pick(grant.get("model", {}), "provider", "model"),
                         "policy_digest": grant.get("policy_digest")})
    report = {
        "format": "sonn-swarm-report", "version": 1, "generated_at": now, "content_included": False,
        "evidence_class": "local runtime records; not independent model-quality or enterprise audit qualification",
        "run": {**_pick(run, "id", "tenant_id", "owner_id", "project_id", "session_id", "epoch", "revision", "state", "stop_requested", "worker_limit"),
                "event_cursor": run["event_sequence"], "started_at": started, "ended_at": ended, "elapsed_seconds": elapsed},
        "policy": _pick(policy, "version", "max_workers", "allowed_providers", "allowed_tools", "read_roots", "write_roots"),
        "work": {"states": dict(Counter(row["state"] for row in snapshot["work_items"])),
                 "items": [_pick(row, "id", "state", "revision") for row in snapshot["work_items"]],
                 "dependencies": snapshot["dependencies"]},
        "accounting": {"request_limit": run["request_limit"], "remaining_request_allowance": snapshot["remaining_requests"],
            "observed_used_request_units": sum(row["used"] or 0 for row in snapshot["model_requests"]),
            "unresolved_model_requests": sum(row["state"] in {"reserved", "started", "uncertain"} for row in snapshot["model_requests"]),
            "active_or_uncertain_reserved_units": sum(row["amount"] for row in snapshot["reservations"] if row["state"] != "settled"),
            "tool_observations": len(snapshot["action_receipts"]), "usage": usage_summary},
        "attempts": attempts, "requests": requests,
        "submissions": [_pick(row, "attempt_id", "candidate_revision", "work_revision") for row in snapshot["submissions"]],
        # A result the orchestrator accepted under the owner's autonomy grant is
        # neither the owner's review nor a check that ran (autopilot.py).
        "decisions": [{**_pick(row, "id", "attempt_id", "criterion_id", "candidate_revision", "check_name", "exit_code"),
                       "kind": "autonomy_grant" if row["executor_id"].startswith("autonomy:")
                       else "owner_review" if (row["criterion_id"] == "owner_review"
                                               and row["check_name"] == "Explicit owner review") else "trusted_check"}
                      for row in snapshot["check_receipts"]],
        "writer_acceptances": [{**_pick(row, "attempt_id", "writer_id", "candidate_id", "application_id", "work_revision",
                                        "writer_revision", "candidate_revision", "epoch"),
                                "decision": "autonomy_grant" if row["owner_id"].startswith("autonomy:") else "owner"}
                               for row in snapshot["writer_acceptances"]],
        "candidates": [_pick(row, "id", "epoch", "state", "base_revision", "result_revision") for row in snapshot["integration_candidates"]],
        "checks": [_pick(row, "id", "candidate_id", "check_key", "candidate_revision", "state", "exit_code")
                   for row in snapshot["integration_checks"]],
        "applications": [_pick(row, "id", "candidate_id", "expected_base", "target_revision", "observed_revision", "state")
                         for row in snapshot["integration_applications"]],
        "operations": [_pick(row, "id", "epoch", "kind", "state", "effect_id") for row in snapshot["integration_operations"]],
        "integration_processes": [_pick(row, "id", "epoch", "effect_kind", "effect_id", "argv_sha256", "state", "exit_code")
                                  for row in snapshot.get("integration_processes", [])],
        "artifacts": [_pick(row, "id", "attempt_id", "epoch", "origin", "model_request_id", "tool_call_id", "sha256", "size", "kind", "media_type")
                      for row in snapshot["artifact_refs"]],
        "messages": {"count": len(snapshot["messages"]), "runtime_receipts": sum(row["stage"] == "runtime" for row in snapshot["receipts"]),
                     "input_context_receipts": sum(row["stage"] == "context" for row in snapshot["receipts"])},
        "owner_guidance": {
            "count": len(snapshot["owner_directives"]),
            "input_prepared_count": len(snapshot["owner_directive_receipts"]),
            "directives": [_pick(row, "id", "attempt_id", "epoch", "sha256") for row in snapshot["owner_directives"]],
            "receipts": [_pick(row, "directive_id", "attempt_id", "epoch", "request_id", "input_sha256", "input_revision")
                         for row in snapshot["owner_directive_receipts"]],
        },
        "events": events,
    }
    return report
