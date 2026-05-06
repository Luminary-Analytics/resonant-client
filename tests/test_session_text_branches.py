"""Tests for v0.5.17a3 — Session.run() text-handling + plan-mode + CLI-shortcut.

Continuing the streaming-stub harness coverage push. This alpha
covers the branches that fire from the assistant's text content:

- TODOS_UPDATED event (lines 831-839): markdown-checkbox text
  triggers a parse + emit with done/total counts.
- CHOICES event flow (lines 841-863): <choices>...</choices> block
  triggers the CHOICES event + on_choice callback dispatch + the
  selected option becomes the next iteration's user message.
- handles_tools CLI-backend shortcut (lines 871-886): when the
  backend has handles_tools=True, tool_call events are display-only
  and the loop exits cleanly with a synthetic assistant history entry.
- Plan mode early-return (lines 1188-1197): when plan_mode is set,
  PLAN_GENERATED + STEP_END fire and run() returns early, leaving
  caller to handle approval flow.

These are all controlled by the assistant's text shape — exactly
what the streaming-stub harness lets us drive deterministically.
"""
from __future__ import annotations

from typing import Iterator

import pytest

from resonant_client.engine.session import Session
from tests.streaming_stub import (
    StreamingBackend,
    done,
    events_of_kind,
    first_of_kind,
    kinds_of,
    text_delta,
    tool_call,
)


# ── TODOS_UPDATED parsing ──────────────────────────────────────────────


class TestTodosUpdated:
    """Lines 831-839: when the assistant emits markdown-checkbox lines,
    parse_markdown_todos extracts them and Session yields a TODOS_UPDATED
    event with the count of done items + total."""

    def test_todos_event_emitted_for_checkbox_text(self):
        backend = StreamingBackend(events=[
            text_delta("Here's my plan:\n\n"),
            text_delta("- [ ] First step\n"),
            text_delta("- [x] Second step (already done)\n"),
            text_delta("- [ ] Third step\n"),
            done(),
        ])
        session = Session(backend=backend, max_steps=1)
        events = list(session.run("plan it"))

        todos = first_of_kind(events, "todos.updated")
        assert todos is not None
        assert todos["total"] == 3
        assert todos["done"] == 1
        # Round-trip through the parser.
        items = todos["todos"]
        assert items[0]["text"] == "First step"
        assert items[0]["done"] is False
        assert items[1]["text"] == "Second step (already done)"
        assert items[1]["done"] is True

    def test_no_todos_event_when_text_has_no_checkboxes(self):
        backend = StreamingBackend(events=[
            text_delta("Here's some regular text."),
            done(),
        ])
        session = Session(backend=backend, max_steps=1)
        events = list(session.run("hi"))
        assert first_of_kind(events, "todos.updated") is None

    def test_all_done_todos_count_correct(self):
        backend = StreamingBackend(events=[
            text_delta("- [x] one\n- [x] two\n- [X] three\n"),
            done(),
        ])
        session = Session(backend=backend, max_steps=1)
        events = list(session.run("hi"))
        todos = first_of_kind(events, "todos.updated")
        assert todos["done"] == 3
        assert todos["total"] == 3


# ── CHOICES flow ───────────────────────────────────────────────────────


class TestChoicesFlow:
    """Lines 841-863: when the assistant emits a <choices> block, the
    CHOICES event fires + the on_choice callback selects an option
    (or first option as fallback). The selected option becomes the
    user message for the NEXT iteration of the agentic loop."""

    def test_choices_event_emits_options_and_surrounding_text(self):
        backend = StreamingBackend(scripts=[
            [
                text_delta("Pick one:\n<choices>\n- option A\n- option B\n</choices>"),
                done(),
            ],
            [text_delta("Continuing with your pick."), done()],
        ])
        session = Session(backend=backend, max_steps=2, auto_approve=True)
        events = list(session.run("decide"))

        choices = first_of_kind(events, "choices")
        assert choices is not None
        assert choices["before"] == "Pick one:"
        assert choices["options"] == ["option A", "option B"]

    def test_on_choice_callback_selects_option(self):
        backend = StreamingBackend(scripts=[
            [
                text_delta("<choices>\n- A\n- B\n- C\n</choices>"),
                done(),
            ],
            [text_delta("ok"), done()],
        ])
        session = Session(backend=backend, max_steps=2, auto_approve=True)
        called_with = []

        def pick_b(options):
            called_with.append(options)
            return "B"

        list(session.run("decide", on_choice=pick_b))

        assert called_with == [["A", "B", "C"]]
        # Selected option lands as a user message in history.
        roles = [(e["role"], e["content"]) for e in session.conversation_history]
        assert ("user", "B") in roles

    def test_no_callback_picks_first_option(self):
        # Without on_choice, Session falls back to options[0].
        backend = StreamingBackend(scripts=[
            [
                text_delta("<choices>\n- first\n- second\n</choices>"),
                done(),
            ],
            [text_delta("ok"), done()],
        ])
        session = Session(backend=backend, max_steps=2, auto_approve=True)
        list(session.run("decide"))

        roles = [(e["role"], e["content"]) for e in session.conversation_history]
        assert ("user", "first") in roles

    def test_choices_iteration_continues_with_selected_msg(self):
        # The selected choice becomes current_msg for the next
        # iteration → backend.stream() is called AGAIN with the
        # new user_msg.
        backend = StreamingBackend(scripts=[
            [
                text_delta("<choices>\n- chosen\n</choices>"),
                done(),
            ],
            [text_delta("continuing"), done()],
        ])
        session = Session(backend=backend, max_steps=3, auto_approve=True)
        list(session.run("first message"))

        # First stream: user_msg="first message"
        # Second stream: user_msg="chosen" (the selected option)
        assert backend.stream_count == 2
        assert backend.stream_calls[0]["user_msg"] == "first message"
        assert backend.stream_calls[1]["user_msg"] == "chosen"


# ── handles_tools CLI-backend shortcut ─────────────────────────────────


class TestHandlesToolsShortcut:
    """Lines 871-886: when the backend has handles_tools=True (CLI
    backends like claude-code / codex that execute tools internally),
    tool_call events are display-only. Session adds a synthetic history
    entry and exits the loop instead of dispatching."""

    def test_handles_tools_shortcut_skips_execution(self):
        backend = StreamingBackend(
            handles_tools=True,
            events=[
                text_delta("calling tools internally"),
                tool_call("file_read", {"path": "x"}, call_id="c1"),
                done(),
            ],
        )
        session = Session(backend=backend, max_steps=5, auto_approve=True)
        events = list(session.run("read x"))

        # Tool-call event YIELDED (for TUI display).
        assert first_of_kind(events, "tool.call") is not None
        # No tool RESULT — Session didn't execute it.
        assert first_of_kind(events, "tool.result") is None

    def test_handles_tools_appends_synthetic_history_entry(self):
        backend = StreamingBackend(
            handles_tools=True,
            events=[
                tool_call("bash", {"cmd": "ls"}, call_id="c1"),
                tool_call("file_read", {"path": "y"}, call_id="c2"),
                done(),
            ],
        )
        session = Session(backend=backend, max_steps=5, auto_approve=True)
        list(session.run("hi"))

        # Find the synthetic assistant entry.
        assistants = [e for e in session.conversation_history if e["role"] == "assistant"]
        assert any("[CLI executed tools" in e["content"] for e in assistants)
        # Both tool names mentioned.
        synth = next(e for e in assistants if "[CLI executed tools" in e["content"])
        assert "bash" in synth["content"]
        assert "file_read" in synth["content"]

    def test_handles_tools_breaks_out_of_loop(self):
        # The CLI shortcut breaks the agentic loop after the first
        # iteration — only one stream() call regardless of max_steps.
        backend = StreamingBackend(
            handles_tools=True,
            scripts=[
                [
                    tool_call("bash", {"cmd": "ls"}, call_id="c1"),
                    done(),
                ],
                # Should never be reached.
                [text_delta("never seen"), done()],
            ],
        )
        session = Session(backend=backend, max_steps=10, auto_approve=True)
        events = list(session.run("hi"))

        # Only one stream() call.
        assert backend.stream_count == 1
        # No second-iteration text leaked through.
        all_deltas = [e["delta"] for e in events_of_kind(events, "text.delta")]
        assert "never seen" not in all_deltas


# ── Plan-mode return path ──────────────────────────────────────────────


class TestPlanModeReturn:
    """Lines 1188-1197: when plan_mode=True, after the first iteration
    Session yields PLAN_GENERATED + STEP_END and returns. The caller
    is expected to handle approval flow + re-call run()."""

    def test_plan_mode_yields_plan_generated_event(self):
        backend = StreamingBackend(events=[
            text_delta("Here's my plan: do X then Y."),
            done(),
        ])
        session = Session(backend=backend, max_steps=5, auto_approve=True)
        session.plan_mode = True

        events = list(session.run("how would you approach this?"))

        plan = first_of_kind(events, "plan.generated")
        assert plan is not None
        assert plan["plan"] == "Here's my plan: do X then Y."

    def test_plan_mode_returns_after_first_iteration(self):
        # Even with max_steps=10, plan_mode causes return after
        # iter 1. Backend stream is called only once.
        backend = StreamingBackend(scripts=[
            [text_delta("a plan"), done()],
            [text_delta("never reached"), done()],
        ])
        session = Session(backend=backend, max_steps=10, auto_approve=True)
        session.plan_mode = True

        list(session.run("plan"))
        assert backend.stream_count == 1

    def test_plan_mode_does_not_emit_session_end(self):
        # Plan mode returns early (line 1197) without yielding
        # SESSION_END — caller is expected to re-call run() after
        # plan approval.
        backend = StreamingBackend(events=[text_delta("plan"), done()])
        session = Session(backend=backend, max_steps=5, auto_approve=True)
        session.plan_mode = True

        events = list(session.run("plan it"))
        # SESSION_END NOT in events.
        assert first_of_kind(events, "session.end") is None
        # PLAN_GENERATED IS in events.
        assert first_of_kind(events, "plan.generated") is not None
