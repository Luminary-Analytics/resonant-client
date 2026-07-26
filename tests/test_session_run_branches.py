"""Tests for v0.5.13a2 — engine/session.py specific branches in run() and _execute_task.

After v0.5.13a1's small-method pass moved session.py to 70% project-
wide, the remaining gaps are concentrated in run()'s loop body and
_execute_task. This alpha hits the cleanly-isolatable branches that
don't require a full agentic-loop stub:

- Multimodal image content shape (lines 628-639): user_msg with
  images list lands as a list-of-content-parts in conversation_history.
- Cancellation BEFORE the loop fires (lines 654-656): cancel(),
  run(), first events are ERROR + SESSION_END, no backend.stream call.
- _execute_task unknown-agent-type early return (lines 1381-1400):
  bad agent_type yields a TOOL_RESULT with is_error=True, records
  the call in conversation_history, and returns without spawning
  a child Session.

The deeper run()-loop branches (compression, tool dispatch, doom-
loop, choice handling, etc.) need a proper backend.stream() stub
harness and are deferred to a future alpha.
"""
from __future__ import annotations

from typing import Iterator


from resonant_client.engine.session import Session


class _StubBackend:
    """Minimal duck-typed backend. stream() never gets called by the
    cancellation-before-loop test; the multimodal test verifies the
    history shape BEFORE stream is called by raising from stream so
    we don't have to maintain a full stream protocol stub."""

    def __init__(self, name: str = "ollama", model: str = "deepseek-v4-flash:cloud"):
        self.name = name
        self.model = model
        self.tool_mode = "native"
        self.base_url = "http://test"
        self.api_key = None
        self.stream_called = False

    def stream(
        self, *, user_msg, conversation_history, instructions,
        tools, max_tokens, cancel_event=None,
    ) -> Iterator[tuple[str, dict]]:
        self.stream_called = True
        # Raise so run() exits on the exception path. The test only
        # cares about state BEFORE this raise (history shape, etc.).
        raise RuntimeError("stub stream — test stops here")


# ── Cancellation before the loop ────────────────────────────────────────


class TestCancellationBeforeLoop:
    """The early-out at session.py:654-656 — if cancel_requested is True
    BEFORE the loop starts, run() yields ERROR + SESSION_END from
    _cancelled_events and returns immediately. backend.stream() is NOT
    called."""

    def test_cancel_before_run_yields_error_and_session_end(self):
        backend = _StubBackend()
        s = Session(backend=backend)
        s.cancel()  # Set cancel_event BEFORE run().

        events = list(s.run("hello"))

        # Two events: ERROR ("Interrupted") + session.end.
        assert len(events) == 2
        assert events[0]["event"] == "error"
        assert "Interrupted" in events[0]["message"]
        assert events[1]["event"] == "session.end"
        # backend.stream was never invoked.
        assert backend.stream_called is False

    def test_cancel_before_run_records_user_message_in_history(self):
        # The cancel-out happens AFTER history.append, so the user
        # message IS recorded — just no model response is generated.
        backend = _StubBackend()
        s = Session(backend=backend)
        s.cancel()

        list(s.run("question"))

        assert len(s.conversation_history) == 1
        assert s.conversation_history[0]["role"] == "user"
        assert s.conversation_history[0]["content"] == "question"


# ── Multimodal image content shape ──────────────────────────────────────


class TestMultimodalImageInput:
    """Lines 628-639: when run() is called with images, the user
    message in conversation_history is a list of content parts
    (image + text), not a flat string. The image is base64-encoded
    inline."""

    def test_single_image_creates_list_content(self):
        backend = _StubBackend()
        s = Session(backend=backend)
        # Cancel BEFORE run so we exit cleanly without needing a
        # working stream implementation.
        s.cancel()

        png_bytes = b"\x89PNG\r\n\x1a\n" + b"x" * 50
        list(s.run("describe this", images=[(png_bytes, "image/png")]))

        # User message lands as list-of-parts.
        msg = s.conversation_history[0]
        assert msg["role"] == "user"
        assert isinstance(msg["content"], list)
        assert len(msg["content"]) == 2
        # Image part first, then text part.
        assert msg["content"][0]["type"] == "image"
        assert msg["content"][0]["media_type"] == "image/png"
        # data field is base64-encoded.
        import base64
        assert base64.b64decode(msg["content"][0]["data"]) == png_bytes
        # Text part second.
        assert msg["content"][1]["type"] == "text"
        assert msg["content"][1]["text"] == "describe this"

    def test_multiple_images_each_recorded(self):
        backend = _StubBackend()
        s = Session(backend=backend)
        s.cancel()

        img1 = (b"\xff\xd8\xff" + b"a" * 30, "image/jpeg")
        img2 = (b"\x89PNG\r\n\x1a\n" + b"b" * 30, "image/png")
        list(s.run("compare", images=[img1, img2]))

        msg = s.conversation_history[0]
        # Two image parts + one text part.
        assert len(msg["content"]) == 3
        assert msg["content"][0]["media_type"] == "image/jpeg"
        assert msg["content"][1]["media_type"] == "image/png"
        assert msg["content"][2]["type"] == "text"
        assert msg["content"][2]["text"] == "compare"

    def test_no_images_keeps_string_content(self):
        # The else-branch on line 640: when images is None, content
        # is the plain string, not a list.
        backend = _StubBackend()
        s = Session(backend=backend)
        s.cancel()

        list(s.run("plain text request"))

        msg = s.conversation_history[0]
        assert msg["content"] == "plain text request"
        assert isinstance(msg["content"], str)

    def test_empty_images_list_keeps_string_content(self):
        # `images=[]` is falsy in the `if images:` check, so the
        # else-branch fires and content stays a string.
        backend = _StubBackend()
        s = Session(backend=backend)
        s.cancel()

        list(s.run("text only", images=[]))

        msg = s.conversation_history[0]
        assert msg["content"] == "text only"
        assert isinstance(msg["content"], str)


# ── _execute_task unknown agent_type early return ──────────────────────


class TestExecuteTaskUnknownAgentType:
    """Lines 1381-1400: when fn_args.get('agent_type') doesn't match
    any registered agent type, _execute_task yields a TOOL_RESULT with
    is_error=True, records the call, and returns without trying to
    spawn a child Session."""

    def _drive_execute_task(self, agent_type_name: str):
        backend = _StubBackend()
        s = Session(backend=backend)
        events = list(s._execute_task(
            fn_args={
                "prompt": "do a thing",
                "agent_type": agent_type_name,
            },
            fn_args_str='{"prompt": "do a thing"}',
            call_id="call-1",
        ))
        return s, events

    def test_unknown_agent_type_yields_tool_result_error(self):
        s, events = self._drive_execute_task("not_a_real_agent")
        assert len(events) == 1
        ev = events[0]
        assert ev["event"] == "tool.result"
        assert ev["name"] == "task"
        assert ev["call_id"] == "call-1"
        assert ev["is_error"] is True
        assert "not_a_real_agent" in ev["output"]
        assert "build, explore, or plan" in ev["output"]

    def test_unknown_agent_type_records_call_and_result_in_history(self):
        s, _ = self._drive_execute_task("typo_agent")
        # Two history entries: tool_call + tool_result
        assert len(s.conversation_history) == 2
        call = s.conversation_history[0]
        result = s.conversation_history[1]
        assert call["role"] == "tool_call"
        assert call["name"] == "task"
        assert call["call_id"] == "call-1"
        assert result["role"] == "tool_result"
        assert result["call_id"] == "call-1"
        assert "typo_agent" in result["content"]

    def test_unknown_agent_type_does_not_call_backend(self):
        # Sanity: backend.stream is NEVER invoked because we never
        # spawn a child Session.
        s, _ = self._drive_execute_task("nope")
        assert s.backend.stream_called is False
