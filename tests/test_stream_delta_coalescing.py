from __future__ import annotations

import queue
from collections import deque

from resonant_client.gui.app import _get_coalesced_stream_event


def test_adjacent_text_deltas_are_combined_without_reordering():
    events: queue.Queue = queue.Queue()
    deferred = deque()
    events.put({"event": "text.delta", "delta": "one"})
    events.put({"event": "text.delta", "delta": " two"})
    events.put({"event": "step.end", "step": 1})
    events.put(None)

    combined = _get_coalesced_stream_event(events, deferred)

    assert combined == {"event": "text.delta", "delta": "one two"}
    assert _get_coalesced_stream_event(events, deferred) == {
        "event": "step.end",
        "step": 1,
    }
    assert _get_coalesced_stream_event(events, deferred) is None


def test_different_delta_types_remain_separate():
    events: queue.Queue = queue.Queue()
    deferred = deque()
    events.put({"event": "thinking.delta", "delta": "reason"})
    events.put({"event": "text.delta", "delta": "answer"})

    assert _get_coalesced_stream_event(events, deferred) == {
        "event": "thinking.delta",
        "delta": "reason",
    }
    assert _get_coalesced_stream_event(events, deferred) == {
        "event": "text.delta",
        "delta": "answer",
    }


def test_non_stream_events_are_forwarded_immediately():
    events: queue.Queue = queue.Queue()
    event = {"event": "tool.call", "name": "file_read"}
    events.put(event)

    assert _get_coalesced_stream_event(events, deque()) is event
