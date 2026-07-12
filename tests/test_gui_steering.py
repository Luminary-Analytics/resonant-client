import asyncio
import threading

from starlette.testclient import TestClient

from resonant_client.gui import app as gui_app


class _CancellableSession:
    def __init__(self):
        self.cancelled = threading.Event()
        self.steered = threading.Event()
        self.steering_messages = []

    def cancel(self):
        self.cancelled.set()

    def steer(self, text, *, message_id=""):
        self.steering_messages.append((message_id, text))
        self.steered.set()
        return True


def test_await_user_choice_is_acknowledged_immediately():
    gui_app.state.user_input_response.clear()
    gui_app.state.user_input_result[0] = ""

    with TestClient(gui_app.app) as client:
        with client.websocket_connect("/ws") as websocket:
            websocket.send_json({"command": "user_input", "response": "Recommended choice"})
            received = None
            for _ in range(4):
                candidate = websocket.receive_json()
                if candidate.get("event") == "user_input_received":
                    received = candidate
                    break
            assert received == {
                "event": "user_input_received",
                "response": "Recommended choice",
            }

    assert gui_app.state.user_input_result[0] == "Recommended choice"
    assert gui_app.state.user_input_response.is_set()
    gui_app.state.user_input_response.clear()


def test_websocket_injects_steer_without_cancelling_active_turn(monkeypatch):
    first_started = threading.Event()
    release_first = threading.Event()
    processed = []
    session = _CancellableSession()

    async def fake_process(_ws, message):
        processed.append(message["text"])
        if len(processed) == 1:
            first_started.set()
            await asyncio.to_thread(release_first.wait, 5)

    monkeypatch.setattr(gui_app, "_process_chat_message", fake_process)
    monkeypatch.setattr(gui_app.state, "available_backends", [object()])
    monkeypatch.setattr(gui_app.state, "backend", object())
    monkeypatch.setattr(gui_app.state, "session", session)
    monkeypatch.setattr(gui_app.state, "codebase_index", object())

    with TestClient(gui_app.app) as client:
        with client.websocket_connect("/ws") as websocket:
            websocket.send_json({"command": "message", "text": "first"})
            assert first_started.wait(2), "first chat turn did not start"

            websocket.send_json({
                "command": "steer",
                "text": "change direction",
                "message_id": "steer-1",
            })
            queued = websocket.receive_json()

            assert queued == {
                "event": "message.queued",
                "message_id": "steer-1",
                "text": "change direction",
                "position": 0,
                "steering": True,
            }
            assert session.steered.wait(1), "steer was not handed to the active session"
            assert session.steering_messages == [("steer-1", "change direction")]
            assert not session.cancelled.is_set()

            release_first.set()

    assert processed == ["first"]


def test_websocket_queues_followup_without_interrupting_active_turn(monkeypatch):
    first_started = threading.Event()
    release_first = threading.Event()
    processed = []
    session = _CancellableSession()

    async def fake_process(_ws, message):
        processed.append(message["text"])
        if len(processed) == 1:
            first_started.set()
            await asyncio.to_thread(release_first.wait, 5)

    monkeypatch.setattr(gui_app, "_process_chat_message", fake_process)
    monkeypatch.setattr(gui_app.state, "available_backends", [object()])
    monkeypatch.setattr(gui_app.state, "backend", object())
    monkeypatch.setattr(gui_app.state, "session", session)
    monkeypatch.setattr(gui_app.state, "codebase_index", object())

    with TestClient(gui_app.app) as client:
        with client.websocket_connect("/ws") as websocket:
            websocket.send_json({"command": "message", "text": "first"})
            assert first_started.wait(2)

            websocket.send_json({
                "command": "message",
                "text": "do this afterward",
                "message_id": "followup-1",
            })
            assert websocket.receive_json() == {
                "event": "message.queued",
                "message_id": "followup-1",
                "text": "do this afterward",
                "position": 1,
                "steering": False,
            }
            assert not session.cancelled.wait(0.1)

            release_first.set()
            assert websocket.receive_json() == {
                "event": "message.started",
                "message_id": "followup-1",
                "text": "do this afterward",
            }

    assert processed == ["first", "do this afterward"]


def test_queued_followup_can_be_promoted_to_steer(monkeypatch):
    first_started = threading.Event()
    release_first = threading.Event()
    processed = []
    session = _CancellableSession()

    async def fake_process(_ws, message):
        processed.append(message["text"])
        if len(processed) == 1:
            first_started.set()
            await asyncio.to_thread(release_first.wait, 5)

    monkeypatch.setattr(gui_app, "_process_chat_message", fake_process)
    monkeypatch.setattr(gui_app.state, "available_backends", [object()])
    monkeypatch.setattr(gui_app.state, "backend", object())
    monkeypatch.setattr(gui_app.state, "session", session)
    monkeypatch.setattr(gui_app.state, "codebase_index", object())

    with TestClient(gui_app.app) as client:
        with client.websocket_connect("/ws") as websocket:
            websocket.send_json({"command": "message", "text": "first"})
            assert first_started.wait(2)
            websocket.send_json({
                "command": "message",
                "text": "change direction",
                "message_id": "promote-1",
            })
            assert websocket.receive_json()["steering"] is False
            assert not session.cancelled.is_set()

            websocket.send_json({
                "command": "steer_queued",
                "message_id": "promote-1",
            })
            assert websocket.receive_json() == {
                "event": "message.queued",
                "message_id": "promote-1",
                "text": "change direction",
                "position": 0,
                "steering": True,
            }
            assert session.steered.wait(1)
            assert session.steering_messages == [("promote-1", "change direction")]
            assert not session.cancelled.is_set()

            release_first.set()

    assert processed == ["first"]


def test_stop_clears_queued_followups(monkeypatch):
    first_started = threading.Event()
    release_first = threading.Event()
    session = _CancellableSession()

    async def fake_process(_ws, _message):
        first_started.set()
        await asyncio.to_thread(release_first.wait, 5)

    monkeypatch.setattr(gui_app, "_process_chat_message", fake_process)
    monkeypatch.setattr(gui_app.state, "available_backends", [object()])
    monkeypatch.setattr(gui_app.state, "backend", object())
    monkeypatch.setattr(gui_app.state, "session", session)
    monkeypatch.setattr(gui_app.state, "codebase_index", object())

    with TestClient(gui_app.app) as client:
        with client.websocket_connect("/ws") as websocket:
            websocket.send_json({"command": "message", "text": "first"})
            assert first_started.wait(2)
            websocket.send_json({
                "command": "message",
                "text": "later",
                "message_id": "later-1",
            })
            assert websocket.receive_json()["event"] == "message.queued"

            websocket.send_json({"command": "cancel", "cancel_id": "cancel-1"})
            requested = websocket.receive_json()
            assert requested == {
                "event": "cancel.requested",
                "cancel_id": "cancel-1",
            }
            cleared = websocket.receive_json()
            assert cleared == {
                "event": "message.queue_cleared",
                "message_ids": ["later-1"],
            }
            assert session.cancelled.wait(1)
            release_first.set()
            completed = websocket.receive_json()
            assert completed == {
                "event": "cancel.completed",
                "cancel_id": "cancel-1",
            }
