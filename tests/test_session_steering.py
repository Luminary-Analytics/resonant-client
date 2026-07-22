import threading

from resonant_client.backends import EVENT_DONE, EVENT_TEXT_DELTA
from resonant_client.engine.session import Session


class _BoundaryBackend:
    name = "test"
    model = "test-model"
    tool_mode = "native"
    effective_context_tokens = 100_000

    def __init__(self):
        self.first_started = threading.Event()
        self.release_first = threading.Event()
        self.prompts = []

    def stream(self, user_msg, **_kwargs):
        self.prompts.append(user_msg)
        if len(self.prompts) == 1:
            self.first_started.set()
            assert self.release_first.wait(2)
            yield EVENT_TEXT_DELTA, {"delta": "Initial answer."}
        else:
            yield EVENT_TEXT_DELTA, {"delta": "Adjusted with live direction."}
        yield EVENT_DONE, {"model": self.model, "stats": {}}


def test_steer_is_applied_at_next_boundary_without_cancelling_run():
    backend = _BoundaryBackend()
    session = Session(backend)
    events = []

    thread = threading.Thread(
        target=lambda: events.extend(session.run("Do the original task")),
        daemon=True,
    )
    thread.start()
    assert backend.first_started.wait(1)

    assert session.steer("Use PostgreSQL instead", message_id="steer-1") is True
    assert session.cancel_requested is False
    backend.release_first.set()
    thread.join(3)

    assert not thread.is_alive()
    assert len(backend.prompts) == 2
    assert backend.prompts[1] == ""
    steer_entries = [
        item for item in session.conversation_history
        if (
        item.get("role") == "user"
        and "<user_steer>" in str(item.get("content"))
        and "Use PostgreSQL instead" in str(item.get("content"))
        )
    ]
    assert len(steer_entries) == 1
    assert [event for event in events if event.get("event") == "steer.applied"] == [{
        "event": "steer.applied",
        "message_id": "steer-1",
        "text": "Use PostgreSQL instead",
        "step": 2,
    }]
    assert not any(
        event.get("event") == "error" and event.get("message") == "Interrupted"
        for event in events
    )
    assert events[-1]["event"] == "session.end"


def test_empty_steer_is_rejected():
    session = Session(_BoundaryBackend())
    assert session.steer("   ") is False
    assert session._drain_steering() == []


def test_cancel_discards_direction_that_was_not_yet_applied():
    session = Session(_BoundaryBackend())
    assert session.steer("late direction", message_id="late-1") is True
    session.cancel()
    assert session.cancel_requested is True
    assert session._drain_steering() == []
