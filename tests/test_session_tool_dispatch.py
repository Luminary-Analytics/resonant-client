"""Tests for v0.5.17a2 — Session.run() tool-dispatch branches.

The streaming-stub harness from v0.5.17a1 lets us drive the agentic
loop with scripted events. This file uses it to cover the tool-
dispatch denial paths + the await_user tool + edge cases — all
the branches inside the `for item in tool_calls:` loop that don't
actually execute a real tool.

Coverage targets in engine/session.py:
- Lines 882-883: malformed-JSON arguments fallback
- Lines 904-926: hook denial (PRE_TOOL_USE hook returns not allowed)
- Lines 928-948: execution policy denial (PolicyAction.DENY)
- Lines 950-974: permission denial (autonomy_tier=suggest + on_permission=False)
- Lines 980-1019: await_user with + without on_user_input callback
- Lines 888-891: cancel during the tool-call for-loop

NOT covered here (intentionally — they need real tool execution):
- Lines 1025-1115: actual `execute_tool()` dispatch (file_read,
  bash, etc.) — needs a stubbed AGENT_TOOLS or actual safe tools.
- Lines 1402-1494: _execute_task subagent forwarding — partial
  coverage already in test_session_run_branches.py for the unknown-
  agent-type branch.
"""
from __future__ import annotations


from resonant_client.engine.session import Session
from tests.streaming_stub import (
    StreamingBackend,
    done,
    events_of_kind,
    first_of_kind,
    tool_call,
)


def test_browser_mcp_tool_uses_the_visual_activity_indicator(monkeypatch):
    from resonant_client.engine import screen_overlay

    backend = StreamingBackend(scripts=[
        [
            tool_call(
                "mcp_chrome_click",
                {"selector": "#play"},
                call_id="mcp-1",
            ),
            done(),
        ],
        [done()],
    ])

    class Manager:
        def call_tool(self, name, arguments):
            return {"content": [{"type": "text", "text": "clicked"}]}

    activity = []
    monkeypatch.setattr(
        screen_overlay, "monitor_index_for_foreground_window", lambda: 1
    )
    monkeypatch.setattr(screen_overlay, "note_activity", activity.append)
    session = Session(backend=backend, max_steps=2, auto_approve=True)
    session._mcp_manager = Manager()

    events = list(session.run("Click in Chrome"))

    assert activity == [1]
    assert first_of_kind(events, "tool.result")["output"] == "clicked"


# ── Malformed JSON arguments ───────────────────────────────────────────


class TestMalformedToolArgs:
    """Lines 896-899 + 780-783: when the model emits a tool_call with
    arguments that aren't valid JSON, Session falls back to {} for
    fn_args. Both the streaming-side parse (line 780-783) and the
    dispatch-side parse (line 896-899) handle this defensively."""

    def test_invalid_json_args_default_to_empty_dict(self):
        # Use the await_user tool because (a) it's testable without
        # spawning a sub-agent and (b) it gracefully handles missing
        # args (returns "(no user available...)" when no callback set).
        backend = StreamingBackend(scripts=[
            [
                tool_call("await_user", arguments="not-json-at-all", call_id="c1"),
                done(),
            ],
            [done()],  # Second iteration after tool result, exits.
        ])
        session = Session(backend=backend, max_steps=2, auto_approve=True)
        events = list(session.run("ask me"))

        # The TOOL_CALL event was yielded with parsed arguments={} (fallback).
        tc_events = events_of_kind(events, "tool.call")
        assert len(tc_events) == 1
        assert tc_events[0]["arguments"] == {}
        # The original arguments_str preserved verbatim.
        assert tc_events[0]["arguments_str"] == "not-json-at-all"
        assert tc_events[0]["presentation"]["kind"] == "generic"


# ── Hook denial path ───────────────────────────────────────────────────


class _DenyingHookRunner:
    """Hook runner stub that always denies. Matches the HookRunner
    surface Session expects (run_hooks(...) returning a result with
    .allowed and .error)."""

    class _Result:
        def __init__(self, allowed, error=""):
            self.allowed = allowed
            self.error = error

    def run_hooks(self, hook_type, *, context, tool_name):
        return self._Result(allowed=False, error="hook says no")


class TestHookDenial:
    """Lines 904-926: when the PRE_TOOL_USE hook returns allowed=False,
    Session emits a TOOL_RESULT with denied=True + records the call+
    result in conversation_history + continues to the next tool (no
    actual execution)."""

    def test_hook_denial_yields_denied_tool_result(self):
        backend = StreamingBackend(scripts=[
            [
                tool_call("await_user", {"question": "?"}, call_id="c1"),
                done(),
            ],
            [done()],
        ])
        session = Session(backend=backend, max_steps=2, auto_approve=True)
        session.hook_runner = _DenyingHookRunner()

        events = list(session.run("hi"))

        tr = first_of_kind(events, "tool.result")
        assert tr is not None
        assert tr["denied"] is True
        assert "Blocked by hook" in tr["output"]
        assert "hook says no" in tr["output"]
        assert tr["is_error"] is False  # denials aren't classified as errors

    def test_hook_denial_records_history(self):
        backend = StreamingBackend(scripts=[
            [
                tool_call("await_user", {"question": "?"}, call_id="c1"),
                done(),
            ],
            [done()],
        ])
        session = Session(backend=backend, max_steps=2, auto_approve=True)
        session.hook_runner = _DenyingHookRunner()
        list(session.run("hi"))

        # History has user msg + assistant text (none — collected_text
        # was empty) + tool_call + tool_result entries.
        roles = [e.get("role") for e in session.conversation_history]
        assert "tool_call" in roles
        assert "tool_result" in roles


# ── Execution policy denial ────────────────────────────────────────────


class _DenyingPolicy:
    """Stub ExecutionPolicy. Returns PolicyAction.DENY for every tool."""

    def evaluate(self, tool_name, args):
        from resonant_client.engine.policies import PolicyAction
        return PolicyAction.DENY

    def get_reason(self, tool_name, args):
        return f"policy says no for {tool_name}"


class TestExecutionPolicyDenial:
    """Lines 928-948: when execution_policy.evaluate returns DENY,
    Session emits a TOOL_RESULT with denied=True + is_error=True."""

    def test_policy_denial_yields_denied_tool_result(self):
        backend = StreamingBackend(scripts=[
            [
                tool_call("await_user", {"question": "?"}, call_id="c1"),
                done(),
            ],
            [done()],
        ])
        session = Session(backend=backend, max_steps=2, auto_approve=True)
        session.execution_policy = _DenyingPolicy()

        events = list(session.run("hi"))

        tr = first_of_kind(events, "tool.result")
        assert tr is not None
        assert tr["denied"] is True
        assert tr["is_error"] is True
        assert "Blocked by policy" in tr["output"]
        assert "policy says no" in tr["output"]


# ── Permission denial (autonomy tier) ──────────────────────────────────


class TestPermissionDenial:
    """Lines 950-974: when _should_auto_approve returns False AND the
    on_permission callback also returns False (or auto_approve is
    False without a callback), the tool is denied with output
    'Tool execution denied by user.'"""

    def test_suggest_tier_denies_non_read_only_tool_without_callback(self):
        # auto_approve=False maps to autonomy_tier="suggest" which
        # only auto-approves read-only tools. await_user is not read-
        # only. Without an on_permission callback, falls back to the
        # auto_approve=False flag → denied.
        backend = StreamingBackend(scripts=[
            [
                tool_call("await_user", {"question": "?"}, call_id="c1"),
                done(),
            ],
            [done()],
        ])
        session = Session(backend=backend, max_steps=2, auto_approve=False)
        events = list(session.run("hi"))

        tr = first_of_kind(events, "tool.result")
        assert tr is not None
        assert tr["denied"] is True
        assert "denied by user" in tr["output"]

    def test_on_permission_callback_can_approve(self):
        # When the suggest-tier check fails, the on_permission callback
        # gets the final say. Returning True approves; the tool then
        # executes (await_user fallback path, since we don't pass
        # on_user_input).
        backend = StreamingBackend(scripts=[
            [
                tool_call("await_user", {"question": "?"}, call_id="c1"),
                done(),
            ],
            [done()],
        ])
        session = Session(backend=backend, max_steps=2, auto_approve=False)
        permission_calls = []

        def on_permission(tool_name, fn_args):
            permission_calls.append((tool_name, fn_args))
            return True

        events = list(session.run("hi", on_permission=on_permission))

        # Callback was called.
        assert len(permission_calls) == 1
        assert permission_calls[0][0] == "await_user"

        # Tool result is NOT a denial — it's the await_user fallback
        # text.
        tr = first_of_kind(events, "tool.result")
        assert tr["denied"] is False
        assert "no user available" in tr["output"]

    def test_on_permission_callback_can_deny(self):
        backend = StreamingBackend(scripts=[
            [
                tool_call("await_user", {"question": "?"}, call_id="c1"),
                done(),
            ],
            [done()],
        ])
        session = Session(backend=backend, max_steps=2, auto_approve=False)

        def on_permission(tool_name, fn_args):
            return False

        events = list(session.run("hi", on_permission=on_permission))
        tr = first_of_kind(events, "tool.result")
        assert tr["denied"] is True
        assert "denied by user" in tr["output"]


# ── await_user tool (lines 980-1019) ───────────────────────────────────


class TestAwaitUserTool:
    """The v0.3.5 await_user tool — pauses the agentic loop until the
    user answers via the on_user_input callback. If no callback is
    wired (CLI mode, tests), returns a sentinel string and lets the
    agent decide how to proceed."""

    def test_no_callback_returns_sentinel(self):
        backend = StreamingBackend(scripts=[
            [
                tool_call("await_user", {"question": "what now?"}, call_id="c1"),
                done(),
            ],
            [done()],
        ])
        session = Session(backend=backend, max_steps=2, auto_approve=True)
        events = list(session.run("hi"))

        tr = first_of_kind(events, "tool.result")
        assert tr is not None
        assert "no user available" in tr["output"]
        assert tr["is_error"] is False
        assert tr["denied"] is False
        # Metadata carries the question for downstream logging.
        assert tr["metadata"]["question"] == "what now?"

    def test_callback_answer_returned_as_tool_result(self):
        backend = StreamingBackend(scripts=[
            [
                tool_call("await_user", {"question": "yes or no?"}, call_id="c1"),
                done(),
            ],
            [done()],
        ])
        session = Session(backend=backend, max_steps=2, auto_approve=True)
        captured = []

        def on_user_input(question, options):
            captured.append((question, options))
            return "yes please"

        events = list(session.run("hi", on_user_input=on_user_input))

        # Callback received the question.
        assert captured == [("yes or no?", [])]
        # Tool result carries the answer.
        tr = first_of_kind(events, "tool.result")
        assert tr["output"] == "yes please"

    def test_callback_exception_is_caught(self):
        # Defensive: if the on_user_input callback raises, Session
        # logs and returns "(error obtaining user input: ...)".
        backend = StreamingBackend(scripts=[
            [
                tool_call("await_user", {"question": "?"}, call_id="c1"),
                done(),
            ],
            [done()],
        ])
        session = Session(backend=backend, max_steps=2, auto_approve=True)

        def boom(question, options):
            raise RuntimeError("user UI exploded")

        events = list(session.run("hi", on_user_input=boom))
        tr = first_of_kind(events, "tool.result")
        assert "error obtaining user input" in tr["output"]
        assert "user UI exploded" in tr["output"]
        # Still NOT classified as is_error — the contract is that the
        # agent sees a string answer and decides what to do.
        assert tr["is_error"] is False


# ── Cancel during the tool-call for-loop ───────────────────────────────


class TestCancelDuringToolLoop:
    """Lines 888-891: between iterations of the `for item in tool_calls:`
    loop, Session checks cancel_requested. If set, emits the cancelled
    events and returns. This catches the case where the user clicks
    Stop while multiple tool calls are queued."""

    def test_cancel_between_tool_calls_yields_cancelled_events(self):
        # Set up TWO tool calls in one stream. Use a hook that flips
        # cancel after the FIRST tool's denial fires, so the loop's
        # second-iteration check picks it up.
        backend = StreamingBackend(scripts=[
            [
                tool_call("await_user", {"question": "first"}, call_id="c1"),
                tool_call("await_user", {"question": "second"}, call_id="c2"),
                done(),
            ],
        ])
        session = Session(backend=backend, max_steps=2, auto_approve=True)

        # Wire a callback that cancels after the first answer.
        call_count = {"n": 0}

        def on_user_input(question, options):
            call_count["n"] += 1
            if call_count["n"] == 1:
                session.cancel()  # Will be picked up at top of next iter
            return f"answer {call_count['n']}"

        events = list(session.run("hi", on_user_input=on_user_input))

        # Only the first await_user produced a tool.result.
        tool_results = events_of_kind(events, "tool.result")
        assert len(tool_results) == 1
        # Cancellation path emitted.
        err = first_of_kind(events, "error")
        assert err is not None
        assert "Interrupted" in err["message"]
