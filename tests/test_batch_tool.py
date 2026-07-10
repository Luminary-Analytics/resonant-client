"""Tests for the `batch` tool — engine/tools.py::_exec_batch.

Batch fans tool calls out across a thread pool. It was previously
untested. These tests mock `execute_tool` so no real tools run; they
pin the contract: aggregation, the recursion guard (no nested
batch/task), the 25-call cap, failure accounting, and cancellation.
"""
from __future__ import annotations

import threading
import time

from unittest.mock import patch

from resonant_client.engine import tools as tools_mod
from resonant_client.engine.tools import (
    BATCH_MAX_CALLS,
    ToolResult,
    _exec_batch,
)


def _ok(output="ok"):
    return ToolResult(output, elapsed=0.0)


def _fail(output="boom"):
    return ToolResult(output, is_error=True, elapsed=0.0)


def _calls(n, name="glob"):
    return [{"name": name, "arguments": {"pattern": "*.py"}} for _ in range(n)]


class TestBatch:
    def test_empty_calls_errors(self):
        r = _exec_batch({"calls": []}, time.time())
        assert r.is_error
        assert "No calls" in r.output

    def test_runs_and_aggregates_successes(self):
        with patch.object(tools_mod, "execute_tool", return_value=_ok("done")):
            r = _exec_batch({"calls": _calls(3)}, time.time())
        assert not r.is_error
        assert r.metadata["total"] == 3
        assert r.metadata["successes"] == 3
        assert r.metadata["failures"] == 0
        assert "3/3 succeeded" in r.output

    def test_any_failure_marks_batch_error(self):
        outs = iter([_ok(), _fail("nope"), _ok()])
        with patch.object(tools_mod, "execute_tool", side_effect=lambda *a, **k: next(outs)):
            r = _exec_batch({"calls": _calls(3)}, time.time())
        assert r.is_error
        assert r.metadata["successes"] == 2
        assert r.metadata["failures"] == 1

    def test_forbidden_tools_are_not_executed(self):
        # batch + task must be refused per-call, and execute_tool never
        # called for them (prevents fan-out recursion).
        calls = [{"name": "batch", "arguments": {}}, {"name": "task", "arguments": {}}]
        with patch.object(tools_mod, "execute_tool",
                          side_effect=AssertionError("forbidden tool must not run")):
            r = _exec_batch({"calls": calls}, time.time())
        assert r.metadata["failures"] == 2
        for res in r.metadata["results"]:
            assert res["status"] == "error"
            assert "Cannot batch" in res["output"]

    def test_caps_at_max_calls(self):
        with patch.object(tools_mod, "execute_tool", return_value=_ok()):
            r = _exec_batch({"calls": _calls(BATCH_MAX_CALLS + 10)}, time.time())
        assert r.metadata["total"] == BATCH_MAX_CALLS

    def test_cancel_event_short_circuits(self):
        ev = threading.Event()
        ev.set()
        with patch.object(tools_mod, "execute_tool",
                          side_effect=AssertionError("must not run when cancelled")):
            r = _exec_batch({"calls": _calls(1)}, time.time(), cancel_event=ev)
        assert r.is_error
        assert r.metadata["cancelled"] is True

    def test_one_call_exception_isolated(self):
        # A worker raising must become that call's error, not crash the batch.
        def boom(name, args, cancel, **kwargs):
            if name == "grep":
                raise RuntimeError("kaboom")
            return _ok()
        calls = [{"name": "glob", "arguments": {}}, {"name": "grep", "arguments": {}}]
        with patch.object(tools_mod, "execute_tool", side_effect=boom):
            r = _exec_batch({"calls": calls}, time.time())
        assert r.metadata["successes"] == 1
        assert r.metadata["failures"] == 1
        grep_res = next(x for x in r.metadata["results"] if x["name"] == "grep")
        assert "kaboom" in grep_res["output"]
