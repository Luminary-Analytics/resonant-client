"""Reusable backend.stream() stub harness for testing the agentic loop.

This module is the v0.5.17a1 investment that the v0.5.11→v0.5.16
hygiene minors kept calling out as the next high-leverage move.
Pre-v0.5.17 every test that wanted to exercise `Session.run()` had
to either mock the entire backend inline (cumbersome) or use the
real one (network-bound). Neither covered the deeper `run()` loop
branches — tool dispatch, doom-loop guards, choice handling, etc.

The pattern this enables:

    from tests.streaming_stub import StreamingBackend, text_delta, done

    backend = StreamingBackend(events=[
        text_delta("Hello"),
        done(),
    ])
    session = Session(backend=backend)
    events = list(session.run("hi"))

For multi-turn flows (tool calls that loop the agentic loop):

    backend = StreamingBackend(scripts=[
        [text_delta("I'll read."), tool_call("file_read", {"path": "x"}), done()],
        [text_delta("Done."), done()],
    ])

Stream-protocol contract (from resonant_client/backends.py):
- "text.delta"      → {"delta": str}
- "tool_call"       → {"name": str, "arguments": str (JSON), "call_id": str}
- "done"            → {"cognitive_state": ?, "stats": dict, "model": str}
- "error"           → {"message": str}
- "backend.status"  → arbitrary kwargs (forwarded as-is)

NOT a test file — pytest discovers test files via `test_*.py`. This
module is a sibling utility imported BY test files.
"""
from __future__ import annotations

import json
from typing import Any, Iterator, Optional


# ── Event-shape constructors ───────────────────────────────────────────


def text_delta(text: str) -> tuple[str, dict]:
    """A streaming text chunk. Multiple deltas concatenate into the
    full text the model emitted before any tool call or DONE."""
    return ("text.delta", {"delta": text})


def tool_call(
    name: str,
    arguments: dict | str | None = None,
    call_id: str = "c1",
) -> tuple[str, dict]:
    """A model-emitted tool call. Arguments can be a dict (auto-JSON-
    serialized) or a pre-serialized string (so tests can inject
    malformed JSON to exercise error paths)."""
    if isinstance(arguments, str):
        args_str = arguments
    else:
        args_str = json.dumps(arguments or {})
    return ("tool_call", {
        "name": name,
        "arguments": args_str,
        "call_id": call_id,
    })


def done(
    *,
    model: str = "test-model",
    stats: dict | None = None,
    cognitive_state: Any = None,
) -> tuple[str, dict]:
    """End-of-stream marker. Carries the final stats + model id."""
    return ("done", {
        "model": model,
        "stats": stats or {"input_tokens": 10, "output_tokens": 20},
        "cognitive_state": cognitive_state,
    })


def error(message: str) -> tuple[str, dict]:
    """Backend-emitted error. Session.run() turns this into ERROR +
    SESSION_END events and exits the loop."""
    return ("error", {"message": message})


def backend_status(**kwargs: Any) -> tuple[str, dict]:
    """Operational status (e.g. v0.5.6a1's Ollama 503 retry banner).
    Forwarded verbatim by Session.run()."""
    return ("backend.status", dict(kwargs))


# ── The stub backend ───────────────────────────────────────────────────


class StreamingBackend:
    """Test backend that yields a scripted sequence of stream events.

    Attributes a real Backend has, that Session relies on:
    - name, model, base_url, api_key, tool_mode  (for SESSION_START
      event payload + compression budget)
    - stream() generator (the main contract)
    - classify() (for should_plan)
    - handles_tools (defaults False; CLI backends like claude-code
      set True to skip Session-side tool execution)

    Construction modes:
    - `events=[...]`: single sequence, replayed every stream() call.
      Useful for "one-shot" tests where the agentic loop only iterates
      once.
    - `scripts=[[...], [...]]`: per-call sequences. The Nth stream()
      call yields scripts[N]. Useful for tool-dispatch tests where
      iter 1 emits a tool_call and iter 2 emits the final text.
    - `raise_on_stream=Exception(...)`: instead of yielding, the
      generator raises on entry. Useful for testing error fallback.

    Exposes:
    - `stream_calls`: list of dicts with the recorded args from each
      stream() invocation (user_msg, history_len, tool_count, etc.)
    - `stream_count`: convenience len(stream_calls)
    """

    def __init__(
        self,
        *,
        name: str = "ollama",
        model: str = "deepseek-v4-flash:cloud",
        events: list[tuple[str, dict]] | None = None,
        scripts: list[list[tuple[str, dict]]] | None = None,
        raise_on_stream: Optional[Exception] = None,
        handles_tools: bool = False,
        classify_response: str = "SIMPLE",
        classify_should_raise: bool = False,
    ):
        self.name = name
        self.model = model
        self.base_url = "http://test"
        self.api_key = None
        self.tool_mode = "native"
        self.handles_tools = handles_tools

        self._events = events
        self._scripts = scripts
        self._raise_on_stream = raise_on_stream
        self._classify_response = classify_response
        self._classify_should_raise = classify_should_raise
        self.stream_calls: list[dict] = []

    @property
    def stream_count(self) -> int:
        return len(self.stream_calls)

    def stream(
        self,
        *,
        user_msg,
        conversation_history,
        instructions,
        tools,
        max_tokens,
        cancel_event=None,
    ) -> Iterator[tuple[str, dict]]:
        """Match the real backend's stream() signature. The exact
        kwargs Session.run() passes."""
        self.stream_calls.append({
            "user_msg": user_msg,
            "history_len": len(conversation_history),
            "tool_count": len(tools),
            "max_tokens": max_tokens,
            "instructions_preview": (instructions or "")[:200],
        })
        if self._raise_on_stream is not None:
            raise self._raise_on_stream

        # Pick the event sequence for THIS call.
        if self._scripts is not None:
            idx = len(self.stream_calls) - 1
            if idx < len(self._scripts):
                events_to_yield = self._scripts[idx]
            else:
                # Out-of-script calls yield just `done` so the loop
                # exits cleanly rather than hanging on no events.
                events_to_yield = [done()]
        else:
            events_to_yield = self._events or [done()]

        for event_type, data in events_to_yield:
            yield event_type, data

    def classify(self, prompt: str, max_tokens: int = 20) -> str:
        """For Session.should_plan()."""
        if self._classify_should_raise:
            raise RuntimeError("classify failed (test stub)")
        return self._classify_response


# ── Convenience: assert-shape helpers ───────────────────────────────────


def kinds_of(events: list[dict]) -> list[str]:
    """Extract the `event` field from each event dict, in order.
    Useful for asserting the flow of an entire Session.run() output."""
    return [e.get("event", "") for e in events]


def events_of_kind(events: list[dict], kind: str) -> list[dict]:
    """All events with the given `event` field."""
    return [e for e in events if e.get("event") == kind]


def first_of_kind(events: list[dict], kind: str) -> dict | None:
    matches = events_of_kind(events, kind)
    return matches[0] if matches else None
