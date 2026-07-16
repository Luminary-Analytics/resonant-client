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

    def test_tool_description_routes_all_user_questions_to_native_prompt(self):
        schema = next(t for t in AGENT_TOOLS if t["function"]["name"] == "await_user")
        description = schema["function"]["description"]
        assert "EVERY question directed to the user" in description
        assert "Last-resort clarification tool" in description
        assert "git history" in description
        assert "Do not use it to ask for confirmation" in description
        assert "make the best evidence-based recommendation yourself" in description
        assert "After implementation starts" in description
        assert "Never ask what is next" in description
        assert "2-5 options" in description
        assert "recommended_option to the exact option" in description

    def test_schema_has_required_question(self):
        schema = next(t for t in AGENT_TOOLS if t["function"]["name"] == "await_user")
        params = schema["function"]["parameters"]
        assert "question" in params["properties"]
        assert "question" in params["required"]
        assert "unresolved_reason" not in params["required"]
        assert "Always provide this before asking" in params["properties"]["unresolved_reason"]["description"]
        assert "not shown in the decision prompt" in params["properties"]["unresolved_reason"]["description"]
        description = params["properties"]["question"]["description"]
        assert "single sentence" in description
        assert "Do not include rationale" in description

    def test_schema_has_optional_options(self):
        schema = next(t for t in AGENT_TOOLS if t["function"]["name"] == "await_user")
        params = schema["function"]["parameters"]
        assert "options" in params["properties"]
        assert "options" not in params.get("required", [])

    def test_schema_supports_explicit_recommendation(self):
        schema = next(t for t in AGENT_TOOLS if t["function"]["name"] == "await_user")
        params = schema["function"]["parameters"]
        assert params["properties"]["recommended_option"]["type"] == "string"
        assert "recommended_option" not in params.get("required", [])

    def test_schema_reserves_post_start_questions_for_catastrophes(self):
        schema = next(t for t in AGENT_TOOLS if t["function"]["name"] == "await_user")
        urgency = schema["function"]["parameters"]["properties"]["urgency"]
        assert urgency["enum"] == ["alignment", "catastrophic"]
        assert "Ordinary blockers" in urgency["description"]


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


class _WriteThenAwaitBackend:
    """Write once, then request input on the next agent iteration."""

    name = "write-then-await"
    model = "stub-model"
    tool_mode = "native"
    handles_tools = False

    def __init__(self, path, question, urgency="alignment"):
        self.path = str(path)
        self.question = question
        self.urgency = urgency
        self.calls = 0

    def stream(self, **kwargs):
        self.calls += 1
        if self.calls == 1:
            yield (EVENT_TOOL_CALL, {
                "name": "file_write",
                "arguments": json.dumps({"path": self.path, "content": "done\n"}),
                "call_id": "write-1",
            })
        elif self.calls == 2:
            yield (EVENT_TOOL_CALL, {
                "name": "await_user",
                "arguments": json.dumps({
                    "question": self.question,
                    "urgency": self.urgency,
                }),
                "call_id": "await-2",
            })
        else:
            yield (EVENT_DONE, {"text": "completed"})
            return
        yield (EVENT_DONE, {})


def _run_with_callback(callback, tool_args=None):
    """Run a Session that calls await_user once with the given args
    and the given callback. Returns the list of yielded events."""
    backend = _AwaitUserBackend(tool_args or {"question": "Pick one"})
    session = Session(backend, max_steps=4, auto_approve=True)
    return list(session.run("hi", on_user_input=callback))


class TestAwaitUserDispatch:
    @pytest.mark.parametrize("question", [
        "What's next?",
        "What's the next move?",
        "What should we do next?",
        "How should I proceed?",
        "Should I continue?",
        "Would you like me to add more tests?",
    ])
    def test_generic_next_step_questions_are_suppressed(self, question):
        callback = MagicMock(return_value="should-not-be-used")
        events = _run_with_callback(callback, {"question": question})

        callback.assert_not_called()
        result = next(
            event for event in events
            if event.get("event") == "tool.result" and event.get("name") == "await_user"
        )
        assert "question suppressed by Resonant policy" in result["output"]
        assert result["metadata"]["suppressed"] is True

    def test_ordinary_question_after_implementation_is_suppressed(self, tmp_path):
        callback = MagicMock(return_value="should-not-be-used")
        backend = _WriteThenAwaitBackend(
            tmp_path / "result.txt",
            "Which implementation should I try now?",
        )
        events = list(Session(backend, max_steps=5, auto_approve=True).run(
            "Implement it", on_user_input=callback,
        ))

        callback.assert_not_called()
        result = next(
            event for event in events
            if event.get("event") == "tool.result" and event.get("name") == "await_user"
        )
        assert "Implementation has already started" in result["output"]
        assert result["metadata"]["suppressed"] is True

    def test_failed_implementation_attempt_still_closes_alignment(self, tmp_path):
        callback = MagicMock(return_value="should-not-be-used")
        backend = _WriteThenAwaitBackend(
            tmp_path / "result.txt",
            "Which recovery strategy best preserves existing behavior?",
        )
        failed = ToolResult(output="write failed", is_error=True, elapsed=0.0)
        with patch("resonant_client.engine.session.execute_tool", return_value=failed):
            events = list(Session(backend, max_steps=5, auto_approve=True).run(
                "Implement it", on_user_input=callback,
            ))

        callback.assert_not_called()
        result = next(
            event for event in events
            if event.get("event") == "tool.result" and event.get("name") == "await_user"
        )
        assert "Implementation has already started" in result["output"]
        assert result["metadata"]["suppressed"] is True

    def test_catastrophic_question_after_implementation_reaches_user(self, tmp_path):
        callback = MagicMock(return_value="stop")
        backend = _WriteThenAwaitBackend(
            tmp_path / "result.txt",
            "Continuing will irreversibly destroy production data; should execution stop?",
            urgency="catastrophic",
        )
        events = list(Session(backend, max_steps=5, auto_approve=True).run(
            "Implement it", on_user_input=callback,
        ))

        callback.assert_called_once()
        result = next(
            event for event in events
            if event.get("event") == "tool.result" and event.get("name") == "await_user"
        )
        assert result["output"] == "stop"
        assert result["metadata"]["suppressed"] is False

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
        assert captured["options"] == ["alpha (Recommended)", "beta", "gamma"]

    def test_invalid_recommendation_falls_back_to_first_option(self):
        captured = {}

        def cb(question, options):
            captured["options"] = list(options)
            return options[0]

        _run_with_callback(cb, {
            "question": "Pick one",
            "options": ["alpha", "beta", "gamma"],
            "recommended_option": "not one of the options",
        })
        assert captured["options"] == ["alpha (Recommended)", "beta", "gamma"]

    def test_option_level_recommendation_is_preserved_without_field(self):
        captured = {}

        def cb(question, options):
            captured["options"] = list(options)
            return options[1]

        _run_with_callback(cb, {
            "question": "Pick one",
            "options": ["alpha", "beta (recommended)", "gamma"],
        })
        assert captured["options"] == ["alpha", "beta (Recommended)", "gamma"]

    def test_multiple_option_markers_are_normalized_to_exactly_one(self):
        captured = {}

        def cb(question, options):
            captured["options"] = list(options)
            return options[0]

        _run_with_callback(cb, {
            "question": "Pick one",
            "options": ["alpha (Recommended)", "beta (Recommended)", "gamma"],
        })
        assert captured["options"] == ["alpha (Recommended)", "beta", "gamma"]

    def test_recommended_option_is_annotated_for_all_frontends(self):
        captured = {}

        def cb(question, options):
            captured["options"] = list(options)
            return options[1]

        _run_with_callback(cb, {
            "question": "Which path?",
            "options": ["alpha", "beta", "gamma"],
            "recommended_option": "beta",
        })
        assert captured["options"] == ["alpha", "beta (Recommended)", "gamma"]

    def test_recommended_option_matching_is_case_insensitive(self):
        captured = {}

        def cb(question, options):
            captured["options"] = list(options)
            return options[0]

        _run_with_callback(cb, {
            "question": "Which path?",
            "options": ["SQLite", "PostgreSQL"],
            "recommended_option": "sqlite",
        })
        assert captured["options"] == ["SQLite (Recommended)", "PostgreSQL"]

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
