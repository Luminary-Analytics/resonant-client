"""
Tests for v0.3.5 `await_user` tool.

The agent calls `await_user(question, options=None)` when it needs human
input — typically to escape a situation that would otherwise trip the
v0.3.3 cycle guards (e.g. "I can't tell which of these two paths the
user meant"). The Session calls back into the GUI via the `on_user_input`
callback, blocks until a reply, and returns the answer as the tool result.

Coverage:
- Tool registered in AGENT_TOOLS with the right schema
- Session dispatches `await_user` to the callback (not to execute_tool)
- Session emits a tool_result with the answer
- Missing callback returns a sentinel (CLI / headless safety)
- Callback exceptions surface as readable errors
- Options passed through to callback
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from resonant_client.backends import EVENT_DONE, EVENT_TOOL_CALL
from resonant_client.engine.session import Session
from resonant_client.engine.tools import AGENT_TOOLS, ToolResult


# ── Tool registration ────────────────────────────────────────────────────


class TestAwaitUserToolRegistration:
    def test_registered_in_agent_tools(self):
        names = [t["function"]["name"] for t in AGENT_TOOLS]
        assert "await_user" in names

    def test_schema_has_required_question(self):
        schema = next(t for t in AGENT_TOOLS if t["function"]["name"] == "await_user")
        params = schema["function"]["parameters"]
        assert "question" in params["properties"]
        assert "question" in params["required"]

    def test_schema_has_optional_options(self):
        schema = next(t for t in AGENT_TOOLS if t["function"]["name"] == "await_user")
        params = schema["function"]["parameters"]
        assert "options" in params["properties"]
        assert "options" not in params.get("required", [])


# ── Session dispatch ─────────────────────────────────────────────────────


class _AwaitUserBackend:
    """Mock backend that emits one `await_user` tool call on the first
    turn and a normal text completion on subsequent turns. Mirrors the
    `_StuckBackend` style from test_doom_loop.py — backends use tuple
    events `(EVENT_NAME, payload_dict)` and expose `name`/`model`/
    `tool_mode` class attributes.
    """
    name = "stub"
    model = "stub-model"
    tool_mode = "native"
    handles_tools = False

    def __init__(self, tool_args):
        self.tool_args = tool_args
        self.user_msgs_received: list[str] = []

    def stream(self, **kwargs):
        self.user_msgs_received.append(kwargs.get("user_msg", ""))
        if len(self.user_msgs_received) == 1:
            yield (
                EVENT_TOOL_CALL,
                {
                    "name": "await_user",
                    "arguments": json.dumps(self.tool_args),
                    "call_id": "call_abc123",
                },
            )
            yield (EVENT_DONE, {})
        else:
            # Second turn: emit a final assistant text so the loop
            # doesn't keep churning.
            yield (EVENT_DONE, {"text": "got it"})


def _run_with_callback(callback, tool_args=None):
    """Run a Session that calls await_user once with the given args
    and the given callback. Returns the list of yielded events."""
    backend = _AwaitUserBackend(tool_args or {"question": "Pick one"})
    session = Session(backend, max_steps=4, auto_approve=True)
    return list(session.run("hi", on_user_input=callback))


class TestAwaitUserDispatch:
    def test_callback_invoked_with_question(self):
        captured = {}

        def cb(question, options):
            captured["question"] = question
            captured["options"] = options
            return "answer-a"

        events = _run_with_callback(cb, {"question": "What approach?"})
        assert captured.get("question") == "What approach?"
        assert captured.get("options") == []

    def test_callback_invoked_with_options(self):
        captured = {}

        def cb(question, options):
            captured["options"] = list(options)
            return options[0] if options else ""

        _run_with_callback(cb, {
            "question": "Pick one",
            "options": ["alpha", "beta", "gamma"],
        })
        assert captured["options"] == ["alpha", "beta", "gamma"]

    def test_answer_lands_in_tool_result_event(self):
        events = _run_with_callback(lambda q, o: "user-said-this")
        results = [e for e in events if e.get("event") == "tool.result"]
        assert any(r.get("name") == "await_user" for r in results)
        await_results = [r for r in results if r.get("name") == "await_user"]
        assert await_results[0].get("output") == "user-said-this"

    def test_answer_appended_to_conversation_history(self):
        backend = _AwaitUserBackend({"question": "?"})
        session = Session(backend, max_steps=4, auto_approve=True)
        list(session.run("hi", on_user_input=lambda q, o: "the-answer"))
        # The history should have a tool_result with the answer text.
        results = [m for m in session.conversation_history
                   if m.get("role") == "tool_result"]
        assert any(r.get("content") == "the-answer" for r in results)

    def test_no_callback_returns_sentinel_not_crash(self):
        # Headless / CLI usage: no on_user_input wired. The agent
        # should still receive *some* tool result (a sentinel string)
        # rather than the session crashing.
        events = _run_with_callback(None, {"question": "?"})
        results = [e for e in events if e.get("event") == "tool.result"
                   and e.get("name") == "await_user"]
        assert len(results) == 1
        assert "(no user available" in results[0].get("output", "")

    def test_callback_exception_returned_as_error_string(self):
        # If the callback raises (e.g. WS disconnected mid-await), we
        # want the agent to see a readable error rather than the
        # whole session aborting.
        def cb(q, o):
            raise RuntimeError("ws closed")

        events = _run_with_callback(cb, {"question": "?"})
        results = [e for e in events if e.get("event") == "tool.result"
                   and e.get("name") == "await_user"]
        assert len(results) == 1
        out = results[0].get("output", "")
        assert "error" in out.lower()
        assert "ws closed" in out

    def test_question_metadata_attached_to_tool_result(self):
        # The `question` shows up in the tool_result event's metadata
        # so the GUI can correlate the response visually if needed
        # (e.g. show the question above the answer in the chat).
        events = _run_with_callback(lambda q, o: "x", {"question": "Which approach?"})
        results = [e for e in events if e.get("event") == "tool.result"
                   and e.get("name") == "await_user"]
        assert results[0].get("metadata", {}).get("question") == "Which approach?"


# ── Allowlist coverage — every specialist can call it ────────────────────


class TestSpecialistAllowlists:
    def test_implement_allows_await_user(self):
        from resonant_client.orchestration.specialists import (
            SPECIALISTS,
            NodeSpecialization,
        )
        profile = SPECIALISTS[NodeSpecialization.IMPLEMENT]
        assert "await_user" in profile.tool_allowlist

    def test_explore_allows_await_user(self):
        from resonant_client.orchestration.specialists import (
            SPECIALISTS,
            NodeSpecialization,
        )
        profile = SPECIALISTS[NodeSpecialization.EXPLORE]
        assert "await_user" in profile.tool_allowlist

    def test_verify_allows_await_user(self):
        from resonant_client.orchestration.specialists import (
            SPECIALISTS,
            NodeSpecialization,
        )
        profile = SPECIALISTS[NodeSpecialization.VERIFY]
        assert "await_user" in profile.tool_allowlist

    def test_research_allows_await_user(self):
        from resonant_client.orchestration.specialists import (
            SPECIALISTS,
            NodeSpecialization,
        )
        profile = SPECIALISTS[NodeSpecialization.RESEARCH]
        assert "await_user" in profile.tool_allowlist

    def test_plan_allows_await_user(self):
        from resonant_client.orchestration.specialists import (
            SPECIALISTS,
            NodeSpecialization,
        )
        profile = SPECIALISTS[NodeSpecialization.PLAN]
        assert "await_user" in profile.tool_allowlist
