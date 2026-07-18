"""Regression tests for non-blocking repetition guidance.

Long-running work must never fail because it performed many repository reads or
revisited a tool signature. Resonant may steer a model once, but only explicit
budgets and user cancellation terminate the agent loop.
"""

from __future__ import annotations

from unittest.mock import patch

from resonant_client.backends import EVENT_DONE, EVENT_TOOL_CALL
from resonant_client.engine.session import (
    CYCLE_WINDOW,
    CYCLE_WINDOW_REPEAT,
    DOOM_LOOP_NUDGE_AT,
    Session,
    _count_trailing_identical_tool_calls,
    _windowed_cycle_repeat,
)
from resonant_client.engine.tools import ToolResult


def _user(text="x"):
    return {"role": "user", "content": text}


def _call(name, args="{}"):
    return {"role": "tool_call", "name": name, "arguments": args}


def _result(name, content=""):
    return {"role": "tool_result", "name": name, "content": content}


class TestRepetitionSignals:
    def test_trailing_identical_calls_ignore_results(self):
        history = [
            _user(),
            _call("glob", '{"pattern":"*.py"}'),
            _result("glob"),
            _call("glob", '{"pattern":"*.py"}'),
            _result("glob"),
        ]
        assert _count_trailing_identical_tool_calls(history) == 2

    def test_trailing_count_stops_at_user_boundary(self):
        history = [
            _user("first"),
            _call("glob", "X"),
            _call("glob", "X"),
            _user("second"),
            _call("glob", "X"),
        ]
        assert _count_trailing_identical_tool_calls(history) == 1

    def test_windowed_signal_catches_interleaved_calls(self):
        history = [_user()]
        for name in ("A", "B", "A", "C", "A"):
            history.extend((_call(name), _result(name)))
        count, name, _ = _windowed_cycle_repeat(history, window=CYCLE_WINDOW)
        assert count == CYCLE_WINDOW_REPEAT
        assert name == "A"


class _ReadBackend:
    name = "stub"
    model = "stub-model"
    tool_mode = "native"

    def __init__(self, *, repeated: bool = False):
        self.repeated = repeated
        self.calls = 0
        self.user_messages: list[str] = []

    def stream(self, **kwargs):
        self.calls += 1
        self.user_messages.append(kwargs.get("user_msg", ""))
        path = "same.py" if self.repeated else f"file-{self.calls}.py"
        yield EVENT_TOOL_CALL, {
            "name": "file_read",
            "arguments": f'{{"path":"{path}"}}',
            "call_id": f"call-{self.calls}",
        }
        yield EVENT_DONE, {
            "cognitive_state": None,
            "stats": None,
            "model": self.model,
        }


def _run_reads(*, repeated: bool, max_steps: int = 24):
    backend = _ReadBackend(repeated=repeated)
    session = Session(backend, max_steps=max_steps, auto_approve=True)
    with patch("resonant_client.engine.session.execute_tool") as execute:
        execute.return_value = ToolResult("evidence", elapsed=0.0)
        events = list(session.run("Inspect the repository thoroughly"))
    return backend, session, events


class TestLongRunningFreedom:
    def test_more_than_fifteen_distinct_reads_are_allowed(self):
        backend, _, events = _run_reads(repeated=False, max_steps=24)
        assert backend.calls == 24
        messages = [event.get("message", "") for event in events if event.get("event") == "error"]
        assert not any("read-only" in message or "Stopped:" in message for message in messages)

    def test_identical_calls_are_nudged_but_not_hard_stopped(self):
        backend, session, events = _run_reads(repeated=True, max_steps=12)
        assert backend.calls == 12
        assert session._doom_loop_nudged is True
        nudges = [message for message in backend.user_messages if "different approach" in message.lower()]
        assert len(nudges) == 1
        messages = [event.get("message", "") for event in events if event.get("event") == "error"]
        assert not any("Stopped:" in message for message in messages)

    def test_explicit_step_budget_still_terminates_test_run(self):
        backend, _, events = _run_reads(repeated=False, max_steps=3)
        assert backend.calls == 3
        assert any(
            "Reached 3 step limit" in event.get("message", "")
            for event in events
            if event.get("event") == "error"
        )

    def test_repetition_nudge_resets_for_each_user_turn(self):
        backend = _ReadBackend(repeated=True)
        session = Session(backend, max_steps=4, auto_approve=True)
        with patch("resonant_client.engine.session.execute_tool") as execute:
            execute.return_value = ToolResult("evidence", elapsed=0.0)
            list(session.run("first"))
            list(session.run("second"))
        nudges = [message for message in backend.user_messages if "different approach" in message.lower()]
        assert len(nudges) == 2


class _AlternatingBackend(_ReadBackend):
    def stream(self, **kwargs):
        self.calls += 1
        self.user_messages.append(kwargs.get("user_msg", ""))
        pattern = "*.py" if self.calls % 2 else "*.md"
        yield EVENT_TOOL_CALL, {
            "name": "glob",
            "arguments": f'{{"pattern":"{pattern}"}}',
            "call_id": f"call-{self.calls}",
        }
        yield EVENT_DONE, {
            "cognitive_state": None,
            "stats": None,
            "model": self.model,
        }


def test_windowed_guidance_fires_once_without_stopping():
    backend = _AlternatingBackend()
    session = Session(backend, max_steps=14, auto_approve=True)
    with patch("resonant_client.engine.session.execute_tool") as execute:
        execute.return_value = ToolResult("evidence", elapsed=0.0)
        events = list(session.run("Explore"))

    assert backend.calls == 14
    nudges = [message for message in backend.user_messages if "cycling" in message.lower()]
    assert len(nudges) == 1
    assert "guidance, not a run limit" in nudges[0]
    assert not any(
        "Stopped:" in event.get("message", "")
        for event in events
        if event.get("event") == "error"
    )


def test_nudge_threshold_is_early_but_non_terminal():
    assert DOOM_LOOP_NUDGE_AT >= 2
