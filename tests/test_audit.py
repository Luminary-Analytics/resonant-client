"""Tests for the per-intent append-only audit log."""

from __future__ import annotations

import time

import pytest

from resonant_client.orchestration import (
    KIND_DECISION,
    KIND_FLOOR,
    KIND_PLAN_CHANGE,
    KIND_TOOL_CALL,
    audit_path,
    log_decision,
    log_floor_violation,
    log_plan_change,
    log_tool_call,
    read_audit_events,
    stream_audit_events,
)


@pytest.fixture
def state_home(tmp_path, monkeypatch):
    home = tmp_path / "state"
    home.mkdir()
    monkeypatch.setenv("RESONANT_STATE_HOME", str(home))
    return home


def test_audit_path_lives_under_state_home(state_home, tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    p = audit_path(project, "intent-xyz")
    assert state_home in p.parents
    assert p.name == "audit.jsonl"
    assert "intent-xyz" in str(p)


def test_audit_path_rejects_blank_intent(state_home, tmp_path):
    with pytest.raises(ValueError):
        audit_path(tmp_path, "")


def test_round_trip_decision_log(state_home, tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    log_decision(project, "i-1", summary="picked specialist=implement",
                 node_id="n1")
    events = read_audit_events(project, "i-1")
    assert len(events) == 1
    assert events[0]["kind"] == KIND_DECISION
    assert events[0]["payload"]["summary"] == "picked specialist=implement"
    assert events[0]["payload"]["node_id"] == "n1"


def test_tool_call_log_redacts_secrets(state_home, tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    log_tool_call(project, "i-1",
                  tool_name="some_tool",
                  args={"api_key": "sk-secret", "name": "ok"},
                  result_summary="ran fine")
    event = read_audit_events(project, "i-1")[0]
    assert event["payload"]["args"]["api_key"] == "[redacted]"
    assert event["payload"]["args"]["name"] == "ok"


def test_tool_call_log_truncates_huge_args(state_home, tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    huge = "x" * 5000
    log_tool_call(project, "i-1",
                  tool_name="file_write",
                  args={"path": "src/x.py", "content": huge})
    event = read_audit_events(project, "i-1")[0]
    truncated = event["payload"]["args"]["content"]
    assert truncated.endswith("...[truncated]")
    assert len(truncated) <= 1100


def test_floor_violation_log(state_home, tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    log_floor_violation(project, "i-1",
                        rule="protected_branch_force_push",
                        reason="git push --force origin main",
                        tool_name="bash")
    event = read_audit_events(project, "i-1")[0]
    assert event["kind"] == KIND_FLOOR
    assert event["payload"]["rule"] == "protected_branch_force_push"


def test_kind_filter_scopes_results(state_home, tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    log_decision(project, "i", summary="d1")
    log_tool_call(project, "i", tool_name="bash", args={"command": "ls"})
    log_plan_change(project, "i", node_id="n1", change="status:done")

    decisions_only = read_audit_events(project, "i", kind_filter={KIND_DECISION})
    assert len(decisions_only) == 1
    assert decisions_only[0]["kind"] == KIND_DECISION

    tool_and_plan = read_audit_events(project, "i", kind_filter={KIND_TOOL_CALL, KIND_PLAN_CHANGE})
    kinds = {e["kind"] for e in tool_and_plan}
    assert kinds == {KIND_TOOL_CALL, KIND_PLAN_CHANGE}


def test_limit_caps_to_most_recent(state_home, tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    for i in range(5):
        log_decision(project, "i", summary=f"d{i}")
    most_recent_two = read_audit_events(project, "i", limit=2)
    assert len(most_recent_two) == 2
    # newest-first
    assert most_recent_two[0]["payload"]["summary"] == "d4"
    assert most_recent_two[1]["payload"]["summary"] == "d3"


def test_read_missing_intent_returns_empty(state_home, tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    assert read_audit_events(project, "never-existed") == []


def test_stream_events_yields_in_chronological_order(state_home, tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    # Wait small intervals so timestamps differ
    log_decision(project, "i", summary="first")
    time.sleep(0.01)
    log_decision(project, "i", summary="second")
    time.sleep(0.01)
    log_decision(project, "i", summary="third")
    summaries = [e["payload"]["summary"] for e in stream_audit_events(project, "i")]
    assert summaries == ["first", "second", "third"]


def test_corrupt_line_is_skipped_not_crashing(state_home, tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    log_decision(project, "i", summary="ok")
    # Inject a malformed JSONL line
    p = audit_path(project, "i")
    with p.open("a", encoding="utf-8") as fh:
        fh.write("this is not json\n")
    log_decision(project, "i", summary="also ok")
    events = read_audit_events(project, "i")
    summaries = sorted(e["payload"]["summary"] for e in events)
    assert summaries == ["also ok", "ok"]
