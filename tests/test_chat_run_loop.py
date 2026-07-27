"""The connection's chat-turn state, exercised without a WebSocket.

This logic previously lived as closures inside `websocket_endpoint`, so none of
it could be tested at all — queue ordering, the cancel acknowledgement, and the
"is a turn running" check were only reachable by driving a real socket through
a real turn.
"""

from __future__ import annotations

import asyncio

from resonant_client.gui.chat_loop import ChatRunLoop


class _StubWS:
    def __init__(self):
        self.sent = []

    async def send_json(self, payload):
        self.sent.append(payload)


def _loop(process=None):
    ws = _StubWS()
    processed = []

    async def _default(_ws, msg):
        processed.append(msg)

    return ChatRunLoop(ws, process or _default), ws, processed


def test_a_fresh_loop_is_not_busy():
    runs, _, _ = _loop()
    assert runs.busy is False


def test_the_first_message_starts_a_turn_and_is_processed():
    async def scenario():
        runs, ws, processed = _loop()
        await runs.enqueue({"text": "hello"})
        await runs.task
        return runs, ws, processed

    runs, ws, processed = asyncio.run(scenario())

    assert [m["text"] for m in processed] == ["hello"]
    # Nothing was queued, so the user is not told it was.
    assert not any(p["event"] == "message.queued" for p in ws.sent)
    assert runs.busy is False


def test_a_message_arriving_mid_turn_is_queued_not_raced():
    async def scenario():
        gate = asyncio.Event()
        processed = []

        async def slow(_ws, msg):
            processed.append(msg["text"])
            if msg["text"] == "first":
                await gate.wait()

        runs, ws, _ = _loop(slow)
        await runs.enqueue({"text": "first"})
        await asyncio.sleep(0)  # let the drain loop start
        await runs.enqueue({"text": "second"})
        queued = [p for p in ws.sent if p["event"] == "message.queued"]
        gate.set()
        await runs.task
        return processed, queued, ws

    processed, queued, ws = asyncio.run(scenario())

    assert processed == ["first", "second"]
    assert [p["text"] for p in queued] == ["second"]
    # A queued message announces itself when it actually starts.
    assert [p["text"] for p in ws.sent if p["event"] == "message.started"] == ["second"]


def test_a_failing_turn_reports_and_does_not_stall_the_queue():
    async def scenario():
        async def boom(_ws, msg):
            if msg["text"] == "bad":
                raise RuntimeError("turn exploded")

        runs, ws, _ = _loop(boom)
        await runs.enqueue({"text": "bad"})
        await runs.task
        # The loop must still accept work afterwards.
        await runs.enqueue({"text": "good"})
        await runs.task
        return ws, runs

    ws, runs = asyncio.run(scenario())

    errors = [p for p in ws.sent if p["event"] == "error"]
    assert "turn exploded" in errors[0]["message"]
    assert runs.busy is False


def test_a_cancel_is_acknowledged_when_the_turn_actually_ends():
    """Not when it was requested — the UI waits on cancel.completed."""
    async def scenario():
        runs, ws, _ = _loop()
        await runs.enqueue({"text": "hello"})
        runs.cancel_request_id = "cancel-7"
        await runs.task
        return ws, runs

    ws, runs = asyncio.run(scenario())

    done = [p for p in ws.sent if p["event"] == "cancel.completed"]
    assert done == [{"event": "cancel.completed", "cancel_id": "cancel-7"}]
    # Cleared, so the next turn does not re-announce a stale cancel.
    assert runs.cancel_request_id is None


def test_no_cancel_acknowledgement_when_none_was_requested():
    async def scenario():
        runs, ws, _ = _loop()
        await runs.enqueue({"text": "hello"})
        await runs.task
        return ws

    ws = asyncio.run(scenario())
    assert not any(p["event"] == "cancel.completed" for p in ws.sent)


def test_an_adopted_run_counts_as_busy():
    """An agent restart streams outside the queue; if `busy` missed it, a
    concurrent message would start a second turn on top of it."""
    async def scenario():
        runs, _, _ = _loop()
        started = asyncio.Event()

        async def long_run():
            started.set()
            await asyncio.sleep(0.05)

        runs.adopt(asyncio.ensure_future(long_run()))
        await started.wait()
        busy_during = runs.busy
        await runs.task
        return busy_during, runs.busy

    busy_during, busy_after = asyncio.run(scenario())

    assert busy_during is True
    assert busy_after is False


def test_the_clear_cache_is_per_connection_state():
    runs, _, _ = _loop()
    assert runs.clear_cache == {}
    runs.clear_cache["req-1"] = {"event": "cleared"}
    assert _loop()[0].clear_cache == {}, "cache must not be shared between connections"
