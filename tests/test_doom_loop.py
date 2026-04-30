"""
Tests for the conversational doom-loop detection and nudge behavior.

The agent loop calls _count_trailing_identical_tool_calls to decide:
  - count >= DOOM_LOOP_NUDGE_AT (2)     → inject corrective prompt next turn
  - count >= DOOM_LOOP_THRESHOLD (4)    → hard-stop with ERROR event
"""

from __future__ import annotations

from unittest.mock import patch

from resonant_client.backends import EVENT_DONE, EVENT_TOOL_CALL
from resonant_client.engine.session import (
    DOOM_LOOP_NUDGE_AT,
    DOOM_LOOP_THRESHOLD,
    Session,
    _check_doom_loop,
    _count_trailing_identical_tool_calls,
)
from resonant_client.engine.tools import ToolResult


def _user(text="x"):
    return {"role": "user", "content": text}


def _call(name, args="{}"):
    return {"role": "tool_call", "name": name, "arguments": args}


def _result(name, content=""):
    return {"role": "tool_result", "name": name, "content": content}


class TestCountTrailingIdenticalToolCalls:
    def test_empty_history_returns_zero(self):
        assert _count_trailing_identical_tool_calls([]) == 0

    def test_history_with_no_tool_calls_returns_zero(self):
        h = [_user("hello"), {"role": "assistant", "content": "hi"}]
        assert _count_trailing_identical_tool_calls(h) == 0

    def test_single_tool_call_returns_one(self):
        h = [_user(), _call("glob", '{"pattern":"*.py"}')]
        assert _count_trailing_identical_tool_calls(h) == 1

    def test_two_identical_calls_with_results_between(self):
        h = [
            _user(),
            _call("glob", '{"pattern":"*.py"}'),
            _result("glob", "found 5"),
            _call("glob", '{"pattern":"*.py"}'),
            _result("glob", "found 5"),
        ]
        assert _count_trailing_identical_tool_calls(h) == 2

    def test_breaks_on_different_args(self):
        h = [
            _user(),
            _call("glob", '{"pattern":"*.py"}'),
            _call("glob", '{"pattern":"*.js"}'),  # different args breaks the streak
            _call("glob", '{"pattern":"*.py"}'),
            _call("glob", '{"pattern":"*.py"}'),
        ]
        # Trailing streak of identical *.py is 2
        assert _count_trailing_identical_tool_calls(h) == 2

    def test_breaks_on_different_tool_name(self):
        h = [
            _user(),
            _call("glob", '{"p":"x"}'),
            _call("glob", '{"p":"x"}'),
            _call("grep", '{"p":"x"}'),  # different tool
        ]
        assert _count_trailing_identical_tool_calls(h) == 1

    def test_does_not_cross_user_message_boundary(self):
        # A user message resets the turn — calls before it shouldn't count.
        h = [
            _user("first"),
            _call("glob", '{"p":"x"}'),
            _call("glob", '{"p":"x"}'),
            _user("second"),  # new turn starts here
            _call("glob", '{"p":"x"}'),
        ]
        # Only the one call after the most recent user message counts
        assert _count_trailing_identical_tool_calls(h) == 1

    def test_at_threshold_returns_threshold(self):
        h = [_user()] + [_call("a", "{}") for _ in range(DOOM_LOOP_THRESHOLD)]
        assert _count_trailing_identical_tool_calls(h) == DOOM_LOOP_THRESHOLD


class TestCheckDoomLoop:
    def test_below_threshold_returns_false(self):
        h = [_user()] + [_call("a", "{}") for _ in range(DOOM_LOOP_THRESHOLD - 1)]
        assert _check_doom_loop(h) is False

    def test_at_threshold_returns_true(self):
        h = [_user()] + [_call("a", "{}") for _ in range(DOOM_LOOP_THRESHOLD)]
        assert _check_doom_loop(h) is True

    def test_above_threshold_returns_true(self):
        h = [_user()] + [_call("a", "{}") for _ in range(DOOM_LOOP_THRESHOLD + 2)]
        assert _check_doom_loop(h) is True

    def test_two_calls_does_not_trigger_hard_stop(self):
        # The new threshold is 4, so 2 identical calls should NOT trigger the
        # hard stop — they should only cross the nudge boundary.
        h = [_user()] + [_call("a", "{}") for _ in range(2)]
        assert _check_doom_loop(h) is False
        assert _count_trailing_identical_tool_calls(h) >= DOOM_LOOP_NUDGE_AT


class TestThresholds:
    def test_nudge_fires_before_hard_stop(self):
        # Sanity: nudge boundary must be below the hard-stop boundary,
        # otherwise the agent gets killed before it has a chance to recover.
        assert DOOM_LOOP_NUDGE_AT < DOOM_LOOP_THRESHOLD

    def test_threshold_gives_room_for_recovery(self):
        # A threshold of 4 means the agent gets called once, told once via the
        # nudge ("try a different approach"), and gets one more chance after
        # that before we hard-stop. Below 3 would be too aggressive.
        assert DOOM_LOOP_THRESHOLD >= 3


# ---------------------------------------------------------------------------
# End-to-end smoke test — drives Session.run() with a stuck mock backend
# and verifies the full nudge → hard-stop event flow that the GUI consumes.
# ---------------------------------------------------------------------------

class _StuckBackend:
    """Mock backend that always emits the same tool_call regardless of input."""

    name = "stub"
    model = "stub-model"
    tool_mode = "native"

    def __init__(self):
        self.user_msgs_received: list[str] = []

    def stream(self, **kwargs):
        # Record what user_msg the agent loop is sending us each turn — the
        # nudge mechanism rewrites this on the first repeat.
        self.user_msgs_received.append(kwargs.get("user_msg", ""))
        yield (
            EVENT_TOOL_CALL,
            {
                "name": "glob",
                "arguments": '{"pattern": "*.py"}',
                "call_id": f"call-{len(self.user_msgs_received)}",
            },
        )
        yield (
            EVENT_DONE,
            {"cognitive_state": None, "stats": None, "model": "stub-model"},
        )


class TestStuckBackendSmoke:
    """Drive Session.run() with a backend that never breaks out of repeating itself."""

    def _run_stuck(self, max_steps: int = 10):
        backend = _StuckBackend()
        session = Session(backend, max_steps=max_steps, auto_approve=True)
        with patch("resonant_client.engine.session.execute_tool") as mock_exec:
            mock_exec.return_value = ToolResult(
                output="found 0 files", is_error=False, elapsed=0.0
            )
            events = list(session.run("explore the repo"))
        return backend, session, events

    def test_nudge_injected_on_second_identical_call(self):
        backend, _, _ = self._run_stuck()
        msgs = backend.user_msgs_received

        # First call gets the original user message.
        assert msgs[0] == "explore the repo"
        # Second call (after one repeat, count=1) is the standard continue prompt.
        assert msgs[1] == "Continue based on the tool results above."
        # Third call (after the second identical tool call, count=2) is the nudge.
        assert "different" in msgs[2].lower(), msgs[2]
        assert "glob" in msgs[2].lower(), msgs[2]

    def test_nudge_fires_only_once_per_turn(self):
        backend, _, _ = self._run_stuck()
        msgs = backend.user_msgs_received
        # Once the nudge has fired, subsequent calls (within the same turn)
        # should fall back to the standard continue prompt — we don't spam
        # the model with the same warning over and over.
        nudge_count = sum(1 for m in msgs if "different" in m.lower())
        assert nudge_count == 1, msgs

    def test_hard_stop_fires_with_friendly_message(self):
        _, _, events = self._run_stuck()
        errors = [e for e in events if e.get("event") == "error"]
        assert len(errors) == 1
        msg = errors[0]["message"]
        # New message no longer says "Doom loop detected" — uses user-facing language.
        assert "Stopped" in msg
        assert "glob" in msg  # references the actually-stuck tool
        assert "stronger model" in msg or "rephrasing" in msg
        assert "Doom loop" not in msg  # old message is gone

    def test_session_ends_cleanly_after_hard_stop(self):
        _, _, events = self._run_stuck()
        end_events = [e for e in events if e.get("event") == "session.end"]
        assert len(end_events) == 1

    def test_hard_stop_at_threshold_not_max_steps(self):
        # The stuck backend would loop forever if not for the doom-loop check.
        # We give it max_steps=10 — but the hard-stop should fire well before
        # that, capping stream() calls at roughly DOOM_LOOP_THRESHOLD.
        backend, _, _ = self._run_stuck(max_steps=10)
        assert len(backend.user_msgs_received) <= DOOM_LOOP_THRESHOLD + 1
        assert len(backend.user_msgs_received) >= DOOM_LOOP_THRESHOLD

    def test_doom_loop_nudged_flag_resets_per_turn(self):
        # Simulate two turns: each turn should get its own nudge attempt.
        backend = _StuckBackend()
        session = Session(backend, max_steps=10, auto_approve=True)
        with patch("resonant_client.engine.session.execute_tool") as mock_exec:
            mock_exec.return_value = ToolResult(output="x", is_error=False, elapsed=0.0)
            list(session.run("first request"))
            assert session._doom_loop_nudged is True

            # Second turn — flag should reset so a fresh nudge can fire.
            list(session.run("second request"))
            # The flag will be True again at the end of the second turn (it nudged again),
            # but the key check is that it was reset to False at the start of run().
            # Easiest way: count nudges across both turns — should be 2, one per turn.
        nudge_count = sum(
            1 for m in backend.user_msgs_received if "different" in m.lower()
        )
        assert nudge_count == 2, backend.user_msgs_received
