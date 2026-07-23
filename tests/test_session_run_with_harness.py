"""Tests for v0.5.17a1 — exercising Session.run() via the streaming stub.

The half-day investment from v0.5.17 onwards: tests/streaming_stub.py
gives us a reusable backend that yields scripted event sequences,
which lets us drive the agentic loop deterministically without a
real LLM. This module is the FIRST batch of tests that uses it —
the simple end-to-end happy path + the error / backend-status /
empty-stream branches.

Subsequent alphas (v0.5.17a2+) will use the same harness for tool-
dispatch loops, choice handling, doom-loop guards, etc.
"""
from __future__ import annotations

import pytest

from resonant_client.engine.session import Session
from tests.streaming_stub import (
    StreamingBackend,
    backend_status,
    done,
    error,
    events_of_kind,
    first_of_kind,
    kinds_of,
    text_delta,
    tool_call,
)


# ── Happy text-only path ────────────────────────────────────────────────


class TestRunHappyTextPath:
    """One iteration with text-only output. Verifies the full event
    sequence Session.run() yields on the simplest possible flow."""

    def test_yields_session_start_first(self):
        backend = StreamingBackend(events=[
            text_delta("Hello"),
            done(),
        ])
        session = Session(backend=backend, max_steps=1)
        events = list(session.run("hi"))
        kinds = kinds_of(events)
        assert kinds[0] == "session.start"

    def test_session_start_carries_backend_metadata(self):
        backend = StreamingBackend(name="ollama", model="my-test-model")
        session = Session(backend=backend, max_steps=1)
        events = list(session.run("hi"))
        start = first_of_kind(events, "session.start")
        assert start is not None
        assert start["backend"] == "ollama"
        assert start["model"] == "my-test-model"
        assert start["tool_mode"] == "native"

    def test_text_deltas_forwarded(self):
        backend = StreamingBackend(events=[
            text_delta("Hello"),
            text_delta(" "),
            text_delta("world"),
            done(),
        ])
        session = Session(backend=backend, max_steps=1)
        events = list(session.run("hi"))
        deltas = events_of_kind(events, "text.delta")
        assert [e["delta"] for e in deltas] == ["Hello", " ", "world"]

    def test_text_done_emits_concatenated_text(self):
        backend = StreamingBackend(events=[
            text_delta("alpha"),
            text_delta(" beta"),
            done(),
        ])
        session = Session(backend=backend, max_steps=1)
        events = list(session.run("hi"))
        text_done = first_of_kind(events, "text.done")
        assert text_done is not None
        assert text_done["text"] == "alpha beta"

    def test_full_text_appended_to_history(self):
        backend = StreamingBackend(events=[
            text_delta("the answer is 42"),
            done(),
        ])
        session = Session(backend=backend, max_steps=1)
        list(session.run("what's the answer?"))
        # User message + assistant response.
        assert len(session.conversation_history) == 2
        assert session.conversation_history[0]["role"] == "user"
        assert session.conversation_history[1]["role"] == "assistant"
        assert session.conversation_history[1]["content"] == "the answer is 42"

    def test_status_event_carries_done_metadata(self):
        backend = StreamingBackend(events=[
            text_delta("ok"),
            done(model="custom-model", stats={"input_tokens": 99, "output_tokens": 11}),
        ])
        session = Session(backend=backend, max_steps=1)
        events = list(session.run("hi"))
        status = first_of_kind(events, "status")
        assert status is not None
        assert status["model"] == "custom-model"
        assert status["stats"]["input_tokens"] == 99

    def test_session_end_includes_step_count(self):
        backend = StreamingBackend(events=[text_delta("ok"), done()])
        session = Session(backend=backend, max_steps=1)
        events = list(session.run("hi"))
        end = first_of_kind(events, "session.end")
        assert end is not None
        assert end["total_steps"] >= 1

    def test_step_start_and_step_end_emitted(self):
        backend = StreamingBackend(events=[text_delta("ok"), done()])
        session = Session(backend=backend, max_steps=1)
        events = list(session.run("hi"))
        kinds = kinds_of(events)
        assert "step.start" in kinds
        assert "step.end" in kinds

    def test_zero_max_steps_disables_cap(self):
        # 0 used to mean "stop immediately" and emitted a step-limit error.
        # The shipped app now treats missing/zero max_steps as unlimited.
        backend = StreamingBackend(events=[text_delta("ok"), done()])
        session = Session(backend=backend, max_steps=0)
        events = list(session.run("hi"))
        errs = events_of_kind(events, "error")
        assert not any("step limit" in e.get("message", "") for e in errs)


# ── EVENT_ERROR mid-stream ──────────────────────────────────────────────


class TestRunBackendErrorEvent:
    """Lines 795-800: when the backend yields an `error` event,
    Session.run() emits ERROR + SESSION_END and exits the loop early.
    No tool calls are dispatched; subsequent stream events are not
    consumed."""

    def test_error_event_yields_error_then_session_end_and_returns(self):
        backend = StreamingBackend(events=[
            text_delta("partial"),
            error("backend exploded"),
            text_delta("not reached"),  # Never consumed; loop exited.
            done(),
        ])
        session = Session(backend=backend, max_steps=5)
        events = list(session.run("hi"))
        kinds = kinds_of(events)

        # ERROR + SESSION_END landed.
        err_idx = kinds.index("error")
        end_idx = kinds.index("session.end")
        assert err_idx < end_idx

        # Specific message bubbled through.
        err = events_of_kind(events, "error")[0]
        assert "backend exploded" in err["message"]

    def test_error_event_skips_subsequent_iterations(self):
        # Only one stream() call should have happened — the loop
        # didn't iterate after the error.
        backend = StreamingBackend(events=[error("kaboom"), done()])
        session = Session(backend=backend, max_steps=5)
        list(session.run("hi"))
        assert backend.stream_count == 1


# ── EVENT_BACKEND_STATUS pass-through ──────────────────────────────────


def test_error_event_forwards_partial_output_discard_signal():
    backend = StreamingBackend(events=[
        text_delta("malformed partial"),
        (
            "error",
            {
                "message": "malformed generation",
                "discard_partial_output": True,
            },
        ),
    ])
    session = Session(backend=backend, max_steps=5)

    events = list(session.run("hi"))

    err = events_of_kind(events, "error")[0]
    assert err["discard_partial_output"] is True


class TestRunBackendStatusPassThrough:
    """Line 802-809 (v0.5.6a1): backend.status events are forwarded
    verbatim. The autonomous-mission daemon + GUI key off these to
    surface "still alive, retrying" banners during transparent retries."""

    def test_backend_status_event_yielded_through(self):
        backend = StreamingBackend(events=[
            backend_status(kind="ollama_retry", attempt=2, max=4,
                           model="deepseek-v4-flash:cloud", status_code=503,
                           backoff_seconds=8),
            text_delta("recovered"),
            done(),
        ])
        session = Session(backend=backend, max_steps=1)
        events = list(session.run("hi"))
        bs = first_of_kind(events, "backend.status")
        assert bs is not None
        assert bs["kind"] == "ollama_retry"
        assert bs["attempt"] == 2
        assert bs["status_code"] == 503

    def test_backend_status_does_not_terminate_loop(self):
        # Unlike `error`, a backend.status event keeps the loop running.
        backend = StreamingBackend(events=[
            backend_status(kind="info", message="ping"),
            text_delta("done"),
            done(),
        ])
        session = Session(backend=backend, max_steps=1)
        events = list(session.run("hi"))
        # text.done landed → loop completed past the backend.status.
        assert first_of_kind(events, "text.done") is not None


class TestRunEmptyResponseRecovery:
    """An empty successful stream must never become a green blank turn."""

    def test_retries_empty_response_without_consuming_step_budget(self):
        backend = StreamingBackend(scripts=[
            [done()],
            [text_delta("Recovered answer"), done()],
        ])
        session = Session(backend=backend, max_steps=1)

        events = list(session.run("rewrite it"))

        assert backend.stream_count == 2
        retry = first_of_kind(events, "backend.status")
        assert retry is not None
        assert retry["kind"] == "empty_response_retry"
        assert retry["attempt"] == 1
        assert first_of_kind(events, "text.done")["text"] == "Recovered answer"
        assert not events_of_kind(events, "error")
        assert "no user-visible text" in backend.stream_calls[1]["user_msg"]

    def test_repeated_empty_responses_fail_visibly(self):
        backend = StreamingBackend(events=[done()])
        session = Session(backend=backend, max_steps=1)

        events = list(session.run("rewrite it"))

        assert backend.stream_count == 3
        assert len(events_of_kind(events, "backend.status")) == 2
        errors = events_of_kind(events, "error")
        assert len(errors) == 1
        assert "empty response 3 times" in errors[0]["message"]
        assert not events_of_kind(events, "text.done")
        assert session.conversation_history == [
            {"role": "user", "content": "rewrite it"}
        ]



class TestRunCompletionIntegrity:
    def test_promise_without_action_continues_through_edit_and_validation(self, tmp_path):
        target = tmp_path / "sample.py"
        backend = StreamingBackend(scripts=[
            [text_delta("Let me rewrite it cleanly, then run it to verify."), done()],
            [tool_call("file_write", {"path": str(target), "content": "value = 1\n"}), done()],
            [tool_call("bash", {"command": f'python -m py_compile "{target}"'}), done()],
            [text_delta("Updated sample.py and verified it compiles."), done()],
        ])
        session = Session(backend=backend, max_steps=3, auto_approve=True)
        session.project_path = str(tmp_path)

        events = list(session.run("rewrite sample.py"))

        continuation = next(
            event for event in events
            if event.get("event") == "backend.status"
            and event.get("kind") == "action_promise_continuation"
        )
        assert continuation["attempt"] == 1
        end = first_of_kind(events, "session.end")
        assert end["outcome"] == "changed_verified"
        assert end["evidence"]["changed_files"] == [str(target)]
        assert end["evidence"]["validation_tools"] == ["bash"]
        assert end["telemetry"]["promise_continuations"] == 1
        assert target.read_text(encoding="utf-8") == "value = 1\n"

    def test_change_request_without_change_is_incomplete_not_done(self):
        backend = StreamingBackend(events=[text_delta("Done."), done()])
        session = Session(backend=backend, max_steps=1)

        events = list(session.run("fix the parser"))

        end = first_of_kind(events, "session.end")
        assert end["outcome"] == "incomplete"
        assert end["evidence"]["requires_workspace_change"] is True

    def test_repeated_file_read_is_compacted_only_in_model_context(self):
        session = Session(backend=StreamingBackend())
        output = "large payload" * 100

        first, first_meta = session._compact_tool_result_for_context(
            "file_read", {"path": "sample.py"}, "read-1", output, is_error=False
        )
        session.conversation_history.append({
            "role": "tool_result", "call_id": "read-1", "content": first
        })
        second, second_meta = session._compact_tool_result_for_context(
            "file_read", {"path": "sample.py"}, "read-2", output, is_error=False
        )

        assert first == output
        assert first_meta == {}
        assert len(second) < len(output)
        assert "identical to call read-1" in second
        assert second_meta["context_deduplicated"] is True


# ── Stream raises an exception ──────────────────────────────────────────


class TestRunStreamException:
    """Lines 815-820: if backend.stream() raises (network failure,
    backend crash, etc.), Session.run() catches, emits ERROR with the
    exception message, then SESSION_END."""

    def test_stream_exception_yields_error_and_session_end(self):
        backend = StreamingBackend(
            raise_on_stream=RuntimeError("simulated upstream crash"),
        )
        session = Session(backend=backend, max_steps=1)
        events = list(session.run("hi"))
        kinds = kinds_of(events)
        assert "error" in kinds
        err = first_of_kind(events, "error")
        assert "simulated upstream crash" in err["message"]
        assert "session.end" in kinds

    def test_stream_exception_logged_in_error_message(self):
        # The error message format is "Stream error: <exception>".
        backend = StreamingBackend(raise_on_stream=ValueError("bad data"))
        session = Session(backend=backend, max_steps=1)
        events = list(session.run("hi"))
        err = first_of_kind(events, "error")
        assert "Stream error:" in err["message"]
        assert "bad data" in err["message"]


# ── Cancellation inside the stream ──────────────────────────────────────


class TestRunCancellationInsideStream:
    """Lines 766-768: cancel_requested check INSIDE the stream-event
    consumer. Lets a long-running stream be cancelled mid-flight."""

    def test_cancel_during_stream_yields_cancelled_events(self):
        # The trick: build a backend whose stream yields a few text
        # deltas, then we externally trigger cancel via the session's
        # cancel_event between the test thread and the stream consumer.
        # Simplest reliable approach: use a backend whose stream
        # callback flips the cancel mid-stream.

        cancel_after_delta = []

        class _CancellingBackend(StreamingBackend):
            def stream(self, **kw):
                # Yield one delta, flip cancel, yield another (which
                # should NEVER be processed because the cancel check
                # fires first).
                yield text_delta("first chunk")
                # Flip cancel between yields. Session.run() checks
                # cancel_requested AFTER each event in the loop.
                cancel_after_delta.append(self)
                if self._session is not None:
                    self._session.cancel()
                yield text_delta("never seen")
                yield done()

        backend = _CancellingBackend()
        session = Session(backend=backend, max_steps=1)
        backend._session = session  # Inject for the cancel-flip closure.

        events = list(session.run("hi"))
        kinds = kinds_of(events)
        # Cancellation path emits ERROR ("Interrupted") + SESSION_END.
        err = first_of_kind(events, "error")
        assert err is not None
        assert "Interrupted" in err["message"]
        assert "session.end" in kinds


# ── Stub backend behavior validation ────────────────────────────────────


class TestStreamingBackendItself:
    """A few sanity checks on the stub itself — ensures the harness
    behaves the way the rest of these tests assume."""

    def test_records_stream_call_args(self):
        backend = StreamingBackend(events=[text_delta("ok"), done()])
        session = Session(backend=backend, max_steps=1)
        list(session.run("test message"))

        assert backend.stream_count == 1
        call = backend.stream_calls[0]
        assert call["user_msg"] == "test message"
        assert call["max_tokens"] == session.max_tokens
        # tool_count = len(self.tools) — non-zero by default since
        # we use AGENT_TOOLS.
        assert call["tool_count"] > 0

    def test_scripts_dispatched_per_call(self):
        # If we put a tool_call in script[0], the loop should iterate
        # to a second stream() call with script[1]. But tool execution
        # is heavy — so for THIS test, we just verify the script
        # dispatch shape: even an empty script[1] keeps the test from
        # hanging because the stub returns `done` for out-of-script
        # calls.
        # Easier-to-test angle: confirm stream_count grows when the
        # script has multiple entries. Use raise_on_stream to bail
        # out cleanly on the SECOND call.
        backend = StreamingBackend(scripts=[
            [text_delta("ok"), done()],  # First call: exit with a valid answer
        ])
        session = Session(backend=backend, max_steps=1)
        list(session.run("hi"))
        # Only one stream call (max_steps=1 prevents iteration 2).
        assert backend.stream_count == 1

    def test_classify_returns_simple_by_default(self):
        backend = StreamingBackend()
        assert backend.classify("any prompt") == "SIMPLE"

    def test_classify_can_be_scripted_to_return_complex(self):
        backend = StreamingBackend(classify_response="COMPLEX")
        assert backend.classify("any prompt") == "COMPLEX"

    def test_classify_can_be_scripted_to_raise(self):
        backend = StreamingBackend(classify_should_raise=True)
        with pytest.raises(RuntimeError, match="classify failed"):
            backend.classify("anything")
