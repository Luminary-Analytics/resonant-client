import asyncio
import threading

from starlette.testclient import TestClient

from resonant_client.gui import app as gui_app


class _CancellableSession:
    def __init__(self):
        self.cancelled = threading.Event()

    def cancel(self):
        self.cancelled.set()


def test_websocket_accepts_steer_while_a_chat_turn_is_still_running(monkeypatch):
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
                "position": 1,
                "steering": True,
            }
            assert session.cancelled.wait(1), "steer did not interrupt the active turn"

            release_first.set()
            started = websocket.receive_json()
            assert started == {
                "event": "message.started",
                "message_id": "steer-1",
                "text": "change direction",
            }

    assert processed == ["first", "change direction"]


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
