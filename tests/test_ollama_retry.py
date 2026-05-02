"""Tests for v0.5.0a9 — Ollama 5xx retry-with-backoff in
`backends.OllamaBackend`.

Real Ollama Cloud occasionally returns 503 "Server overloaded,
please retry shortly (ref: <uuid>)" under capacity pressure. The
backend now retries up to 3 times with exponential backoff (1.5s,
3s, 6s). These tests pin the contract:

- Transient 5xx → retry, eventually succeed
- Persistent 5xx → exhaust retries → raise the descriptive
  HTTPStatusError as before
- 4xx → no retry (fail fast — the request is bad, not the upstream)
- cancel_event → bail out of the backoff sleep

httpx's Client.stream is mocked via a tiny stub that returns canned
responses in sequence. No live network.
"""
from __future__ import annotations

import threading
from contextlib import contextmanager
from typing import Iterator
from unittest.mock import MagicMock, patch

import pytest

from resonant_client.backends import (
    _OLLAMA_BASE_BACKOFF,
    _OLLAMA_MAX_RETRIES,
    _OLLAMA_RETRYABLE_STATUS,
    OllamaBackend,
    _wait_with_cancel,
)


# ── _wait_with_cancel ──────────────────────────────────────────────────


class TestWaitWithCancel:
    def test_zero_seconds_returns_false_when_no_event(self):
        assert _wait_with_cancel(0.0, None) is False

    def test_zero_seconds_returns_event_state_when_event_present(self):
        ev = threading.Event()
        assert _wait_with_cancel(0.0, ev) is False
        ev.set()
        assert _wait_with_cancel(0.0, ev) is True

    def test_short_sleep_no_cancel_returns_false(self):
        # 50ms — far shorter than the polling interval, but should
        # still complete cleanly without cancel.
        assert _wait_with_cancel(0.05, None) is False

    def test_cancel_during_sleep_returns_true_quickly(self):
        ev = threading.Event()
        # Set the event after a brief delay; the wait should observe
        # it and return True well before the requested sleep elapses.
        threading.Timer(0.05, ev.set).start()
        import time
        started = time.time()
        result = _wait_with_cancel(2.0, ev)
        elapsed = time.time() - started
        assert result is True
        assert elapsed < 1.0  # observed quickly, not full 2s


# ── Stream-retry plumbing ──────────────────────────────────────────────


class _FakeResponse:
    """Tiny stand-in for httpx Response used by the stream context.

    Records `read()` calls so tests can assert what the retry path
    consumed for diagnostics; iter_raw() yields a 'done' chunk so
    the streaming loop terminates promptly when status is 200.
    """

    def __init__(self, status_code: int, body: bytes = b""):
        self.status_code = status_code
        self._body = body
        self._read_called = False
        self.request = MagicMock()
        # Simulate a "done" chunk so the streaming loop in stream()
        # exits cleanly — content doesn't matter for these tests.
        self._chunks = [b'{"done":true,"message":{"content":""}}\n']

    def read(self):
        self._read_called = True
        return self._body

    def iter_raw(self):
        yield from self._chunks


class _FakeStreamCtx:
    """Context manager for `client.stream(...)`. Yields a
    `_FakeResponse` from the queue per call."""

    def __init__(self, responses_queue):
        self._queue = responses_queue
        self._resp = None

    def __enter__(self):
        # Pop the next canned response.
        self._resp = self._queue.pop(0)
        return self._resp

    def __exit__(self, *exc):
        return False


class _FakeClient:
    def __init__(self, responses_queue):
        self._queue = responses_queue
        self.closed = False

    def stream(self, method: str, url: str, json=None):
        return _FakeStreamCtx(self._queue)

    def close(self):
        self.closed = True

    # Context manager interface (some httpx code paths use `with`)
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


def _patch_httpx_client(responses):
    """Patches `httpx.Client` to return a `_FakeClient` that yields
    each `_FakeResponse` in `responses` in turn (one per retry).
    Returns the queue so tests can inspect what's left."""
    queue = list(responses)
    # Each call to httpx.Client(...) returns a new fake client that
    # all share the same queue.
    fake_client_factory = MagicMock(side_effect=lambda *a, **kw: _FakeClient(queue))
    return patch("resonant_client.backends.httpx.Client", fake_client_factory), queue


def _drive_stream(backend: OllamaBackend) -> list:
    """Run the streaming generator to exhaustion; return the events."""
    return list(backend.stream(
        user_msg="ping",
        conversation_history=[],
        instructions="be brief",
        tools=[],
        max_tokens=64,
    ))


# ── Retry semantics ────────────────────────────────────────────────────


class TestOllamaRetrySemantics:
    def _make_backend(self):
        # Skip _detect_tool_support() and supports_vision() probes —
        # these would hit the network. We force tool_support=False
        # (text mode) so the stream path is the simple buffered one.
        b = OllamaBackend("http://stub", "test-model")
        b._use_native_tools = False
        b._vision_support_cache[b.model] = False
        return b

    def test_transient_503_then_200_succeeds(self):
        """One 503 followed by a 200 — should retry once and complete."""
        responses = [
            _FakeResponse(503, b'{"error":"Server overloaded (ref:abc)"}'),
            _FakeResponse(200),
        ]
        ctx, queue = _patch_httpx_client(responses)
        with patch("resonant_client.backends._wait_with_cancel",
                   return_value=False) as wait_mock, ctx:
            backend = self._make_backend()
            events = _drive_stream(backend)
        # No error event → success
        assert all(ev[0] != "error" for ev in events), \
            f"unexpected error event in: {events}"
        # Slept once between attempts
        assert wait_mock.call_count == 1
        assert wait_mock.call_args.args[0] == _OLLAMA_BASE_BACKOFF
        # Both responses consumed
        assert queue == []

    def test_two_transient_503_then_200_succeeds(self):
        responses = [
            _FakeResponse(503, b'{"error":"overload"}'),
            _FakeResponse(502, b'{"error":"upstream"}'),
            _FakeResponse(200),
        ]
        ctx, queue = _patch_httpx_client(responses)
        with patch("resonant_client.backends._wait_with_cancel",
                   return_value=False) as wait_mock, ctx:
            backend = self._make_backend()
            events = _drive_stream(backend)
        assert all(ev[0] != "error" for ev in events)
        # Two backoffs: 1.5s then 3s
        assert wait_mock.call_count == 2
        delays = [c.args[0] for c in wait_mock.call_args_list]
        assert delays == [_OLLAMA_BASE_BACKOFF, _OLLAMA_BASE_BACKOFF * 2]

    def test_persistent_503_exhausts_retries_then_errors(self):
        """4 consecutive 503s — initial attempt + 3 retries — yields
        an error event with the descriptive message."""
        responses = [
            _FakeResponse(503, b'{"error":"overload (ref:1)"}')
            for _ in range(_OLLAMA_MAX_RETRIES + 1)
        ]
        ctx, queue = _patch_httpx_client(responses)
        with patch("resonant_client.backends._wait_with_cancel",
                   return_value=False) as wait_mock, ctx:
            backend = self._make_backend()
            events = _drive_stream(backend)

        # All 4 attempts consumed
        assert queue == []
        # 3 backoff sleeps (between attempts 1→2, 2→3, 3→4)
        assert wait_mock.call_count == _OLLAMA_MAX_RETRIES
        # An error event was yielded with the 503 message
        errors = [ev for ev in events if ev[0] == "error"]
        assert len(errors) == 1
        assert "503" in errors[0][1]["message"]

    def test_4xx_does_not_retry(self):
        """400 (bad request) is NOT in retryable set — should fail
        fast on the first attempt."""
        responses = [_FakeResponse(400, b'{"error":"bad json"}')]
        ctx, queue = _patch_httpx_client(responses)
        with patch("resonant_client.backends._wait_with_cancel",
                   return_value=False) as wait_mock, ctx:
            backend = self._make_backend()
            events = _drive_stream(backend)
        # No backoff sleeps
        assert wait_mock.call_count == 0
        # Error yielded
        errors = [ev for ev in events if ev[0] == "error"]
        assert len(errors) == 1
        assert "400" in errors[0][1]["message"]

    def test_cancel_during_backoff_aborts_cleanly(self):
        """If cancel_event fires during a backoff sleep, the helper
        bails out without consuming further attempts."""
        responses = [
            _FakeResponse(503, b'{"error":"overload"}'),
            # If we reach this, cancel didn't work — second response
            # would be consumed.
            _FakeResponse(200),
        ]
        ev = threading.Event()
        ctx, queue = _patch_httpx_client(responses)

        # Stub the wait helper to set cancel_event mid-sleep and
        # report cancelled.
        def fake_wait(seconds, cancel_event):
            ev.set()
            return True

        with patch("resonant_client.backends._wait_with_cancel",
                   side_effect=fake_wait), ctx:
            backend = self._make_backend()
            # Pass a real cancel_event so the helper can observe it.
            events = list(backend.stream(
                user_msg="ping",
                conversation_history=[],
                instructions="",
                tools=[],
                max_tokens=64,
                cancel_event=ev,
            ))
        # After cancel, the stream method's inner `if client is None: return`
        # triggers — no further events. Whether the very first 503 also
        # produces a downstream error event depends on how iteration
        # exits, but the SECOND response should NOT have been consumed.
        # Concrete invariant: the second response is still in the queue.
        assert len(queue) == 1, f"second response was consumed: {queue}"

    def test_retryable_status_set_includes_common_5xx(self):
        # Pin the specific codes we retry — drift would silently
        # change behavior.
        assert 502 in _OLLAMA_RETRYABLE_STATUS
        assert 503 in _OLLAMA_RETRYABLE_STATUS
        assert 504 in _OLLAMA_RETRYABLE_STATUS
        # 4xx never retried
        assert 400 not in _OLLAMA_RETRYABLE_STATUS
        assert 401 not in _OLLAMA_RETRYABLE_STATUS
        assert 404 not in _OLLAMA_RETRYABLE_STATUS
        # 200 obviously not retried
        assert 200 not in _OLLAMA_RETRYABLE_STATUS

    def test_max_retries_constant(self):
        # 3 retries = 4 total attempts. Pin so a future bump is
        # deliberate.
        assert _OLLAMA_MAX_RETRIES == 3
        assert _OLLAMA_BASE_BACKOFF == 1.5
