"""Metadata export preserves unknown usage and cannot disclose scoped content."""

from dataclasses import replace
import json

import pytest

from lumi.engine.swarming.models import Conflict, ScopeDenied
from lumi.engine.swarming.report import export_report
from tests.test_swarm_execution import command, fixture as execution_fixture, inputs


@pytest.fixture(name="fixture")
def report_fixture(tmp_path):
    return execution_fixture.__wrapped__(tmp_path)


def test_report_keeps_partial_usage_distinct_and_excludes_freeform_content(fixture):
    guard, now = fixture
    for cost in (.1, .2):
        request = guard.begin_request(purpose="primary", inputs=inputs(user_msg="PRIVATE PROMPT CONTENT"))
        guard.end_request(request, outcome="completed", usage={"input_tokens": 7, "output_tokens": 3,
            "cost_usd": cost, "provider_private": "PRIVATE USAGE CONTENT"}, error="PRIVATE DIAGNOSTIC")
    now[0] += 5
    guard.begin_request(purpose="compression", inputs=inputs(user_msg="PRIVATE AUXILIARY CONTENT"))
    now[0] += 5
    report = export_report(guard.store, guard.context.scope, guard.context.run_id)
    accounting = report["accounting"]
    assert report["run"]["state"] == "running" and report["run"]["elapsed_seconds"] == 10
    assert accounting["observed_used_request_units"] == 2 and accounting["unresolved_model_requests"] == 1
    assert accounting["tool_observations"] == 0
    assert accounting["usage"]["reported_usd"] == {
        "known_subtotal": "0.3", "reported_requests": 2, "unknown_requests": 1, "complete": False}
    assert accounting["usage"]["input_tokens"]["known_subtotal"] == 14
    assert accounting["usage"]["reserved_usd"] is None and accounting["usage"]["estimated_usd"] is None
    assert [row["purpose"] for row in report["requests"]] == ["main", "main", "auxiliary"]
    assert all(row["input_sha256"] for row in report["requests"])
    encoded = json.dumps(report)
    assert "PRIVATE" not in encoded and "supervisor_id" not in encoded and "launch_token" not in encoded
    assert report["content_included"] is False
    assert all(set(event) == {"sequence", "epoch", "kind", "occurred_at", "run_state"} for event in report["events"])


def test_missing_invalid_usage_and_backwards_clock_do_not_become_zero_or_elapsed(fixture):
    guard, now = fixture
    request = guard.begin_request(purpose="primary", inputs=inputs())
    guard.end_request(request, outcome="completed", usage={"input_tokens": True, "output_tokens": 10**400, "cost_usd": "0.2"}, error="")
    now[0] = 900
    report = export_report(guard.store, guard.context.scope, guard.context.run_id)
    assert report["run"]["elapsed_seconds"] is None
    for key in ("input_tokens", "output_tokens", "reported_usd"):
        assert report["accounting"]["usage"][key] == {
            "known_subtotal": None, "reported_requests": 0, "unknown_requests": 1, "complete": False}
    with guard.store._connection(write=True) as connection:
        connection.execute("UPDATE events SET occurred_at=NULL WHERE sequence=1")
    assert export_report(guard.store, guard.context.scope, guard.context.run_id)["run"]["started_at"] is None


def test_owner_guidance_export_includes_hash_and_exact_receipt_without_content(fixture):
    from lumi.engine.swarming.execution import SwarmExecutionGuard
    from lumi.engine.swarming.guidance import OwnerGuidance

    guard, _ = fixture
    context = guard.context
    command(guard.supervisor, guard.authority, "steer_worker", {
        "attempt_id": context.attempt_id, "attempt_epoch": context.epoch, "text": "PRIVATE GUIDANCE"})
    guidance = OwnerGuidance(guard.store, context)
    pending = guidance.pending()
    prepared = inputs()
    prepared["conversation_history"] = [{"role": "user", "input_origin": "generated",
        "content": f"<runtime_message>\n{guidance.generated_text(pending[0])}\n</runtime_message>"}]
    guard = SwarmExecutionGuard(guard.supervisor, guard.authority, context,
                               guard.workspace, guard.grant, input_observer=guidance.record_input)
    request = guard.begin_request(purpose="primary", inputs=prepared)
    report = export_report(guard.store, context.scope, context.run_id)
    assert report["owner_guidance"]["count"] == 1
    assert report["owner_guidance"]["input_prepared_count"] == 1
    assert report["owner_guidance"]["receipts"][0]["request_id"] == request
    assert report["owner_guidance"]["directives"][0]["sha256"]
    assert "PRIVATE GUIDANCE" not in json.dumps(report)


@pytest.mark.parametrize("field", ["tenant_id", "owner_id", "project_id", "session_id"])
def test_report_does_not_cross_owner_or_conversation_scope(fixture, field):
    guard, _ = fixture
    with pytest.raises(ScopeDenied):
        export_report(guard.store, replace(guard.context.scope, **{field: "foreign"}), guard.context.run_id)


def terminal_fixture(guard, now):
    command(guard.supervisor, guard.authority, "stop")
    now[0] = 1005
    command(guard.supervisor, guard.authority, "worker_stopped", {
        "attempt_id": guard.context.attempt_id, "attempt_epoch": guard.context.epoch,
        "outcome": "cancelled", "evidence": "Fixture host observed resource cleanup"})


def test_terminal_elapsed_is_fixed_at_first_terminal_transition_not_later_controls(fixture):
    guard, now = fixture
    terminal_fixture(guard, now)
    report = export_report(guard.store, guard.context.scope, guard.context.run_id)
    assert report["run"]["state"] == "cancelled"
    assert report["run"]["ended_at"] == 1005 and report["run"]["elapsed_seconds"] == 5
    now[0] = 1010
    with pytest.raises(Conflict, match="terminal"):
        command(guard.supervisor, guard.authority, "stop")
    now[0] = 1020
    command(guard.supervisor, guard.authority, "renew")
    later = export_report(guard.store, guard.context.scope, guard.context.run_id)
    assert later["run"]["ended_at"] == 1005 and later["run"]["elapsed_seconds"] == 5
    assert later["events"][-1]["occurred_at"] == 1020


@pytest.mark.parametrize("missing", ["state", "timestamp"])
def test_missing_terminal_provenance_does_not_fall_back_to_export_clock_or_later_event(fixture, missing):
    guard, now = fixture
    terminal_fixture(guard, now)
    with guard.store._connection(write=True) as connection:
        if missing == "state":
            connection.execute("UPDATE events SET run_state=NULL")
        else:
            first = connection.execute("SELECT MIN(sequence) FROM events WHERE run_state='cancelled'").fetchone()[0]
            connection.execute("UPDATE events SET occurred_at=NULL WHERE sequence=?", (first,))
    now[0] = 1020
    report = export_report(guard.store, guard.context.scope, guard.context.run_id)
    assert report["run"]["started_at"] == 1000
    assert report["run"]["ended_at"] is None and report["run"]["elapsed_seconds"] is None


def test_effect_checkpoint_records_terminal_state_after_its_observation_event(fixture):
    guard, now = fixture
    # Emulate the owned-effect observer ordering: event, then control checkpoint.
    command(guard.supervisor, guard.authority, "worker_stopped", {
        "attempt_id": guard.context.attempt_id, "attempt_epoch": guard.context.epoch,
        "outcome": "cancelled", "evidence": "Fixture cleanup"})
    now[0] = 1007
    with guard.store._connection(write=True) as connection:
        connection.execute("UPDATE runs SET state='stopping',stop_requested=1 WHERE id=?", (guard.context.run_id,))
        guard.store._event(connection, guard.context.run_id, "fixture_observation", {})
        guard.supervisor._control_checkpoint(connection, guard.context.run_id)
    events = guard.store.events(guard.context.scope, guard.context.run_id)
    assert events[-2].run_state == "stopping" and events[-1].run_state == "cancelled"
    assert events[-1].kind == "run_terminal"
    assert export_report(guard.store, guard.context.scope, guard.context.run_id)["run"]["elapsed_seconds"] == 7


@pytest.mark.parametrize("endpoint", [float("inf"), float("nan"), 999])
def test_nonfinite_or_backwards_terminal_clock_cannot_claim_elapsed(fixture, endpoint):
    guard, now = fixture
    terminal_fixture(guard, now)
    with guard.store._connection(write=True) as connection:
        connection.execute("UPDATE events SET occurred_at=? WHERE run_state='cancelled'", (endpoint,))
    report = export_report(guard.store, guard.context.scope, guard.context.run_id)
    assert report["run"]["elapsed_seconds"] is None
    json.dumps(report, allow_nan=False)
