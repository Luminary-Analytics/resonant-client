"""A dead socket must not take down the WebSocket endpoint.

Observed in a shipped build: switching project while Ollama was unreachable
produced a legitimate ValueError, the handler's own error reply then hit an
already-closed socket, and the resulting

    Unexpected ASGI message 'websocket.send', after sending 'websocket.close'

escaped the command handler and killed the endpoint loop. The frontend saw the
connection drop, reconnected, and repeated — so the sidebar populated from the
first burst and nothing else ever loaded. The user's symptom was "I'm clicking
sessions and nothing happens".

The provider being down was real and correctly reported. The bug is that
failing to *report* a failure is fatal.
"""

import asyncio

import pytest

from resonant_client.gui.ws_commands import CommandContext


class _ClosedSocket:
    """Starlette's behaviour once the peer has gone: every send raises."""

    def __init__(self):
        self.attempts = 0

    async def send_json(self, payload):
        self.attempts += 1
        raise RuntimeError(
            "Unexpected ASGI message 'websocket.send', after sending 'websocket.close'."
        )


class _OpenSocket:
    def __init__(self):
        self.sent = []

    async def send_json(self, payload):
        self.sent.append(payload)


def _context(ws):
    return CommandContext(ws=ws, state=object(), msg={}, runs=None)


def test_send_on_a_closed_socket_does_not_raise():
    """Otherwise the exception unwinds into the endpoint loop and ends it."""
    ws = _ClosedSocket()
    ctx = _context(ws)

    asyncio.run(ctx.send({"event": "init"}))

    assert ws.attempts == 1, "the send should still be attempted once"


def test_error_reply_on_a_closed_socket_does_not_raise():
    """The error path is the one that actually killed the endpoint.

    A handler catches a real failure, tries to tell the user about it, and the
    telling is what proves fatal.
    """
    ctx = _context(_ClosedSocket())

    asyncio.run(ctx.send_error("Ollama is not reachable"))


def test_further_sends_are_skipped_once_the_socket_is_known_dead():
    """No point writing to a socket that has already refused a write.

    Several handlers send two or three messages in a row; retrying each one
    turns a single dead connection into a burst of identical exceptions in the
    log.
    """
    ws = _ClosedSocket()
    ctx = _context(ws)

    async def _three():
        await ctx.send({"event": "init"})
        await ctx.send({"event": "ui_notice"})
        await ctx.send({"event": "error"})

    asyncio.run(_three())

    assert ws.attempts == 1, f"expected one attempt, got {ws.attempts}"


def test_a_healthy_socket_is_unaffected():
    ws = _OpenSocket()
    ctx = _context(ws)

    async def _two():
        await ctx.send({"event": "init"})
        await ctx.send_error("boom")

    asyncio.run(_two())

    assert [p["event"] for p in ws.sent] == ["init", "error"]


def test_an_unrelated_send_failure_still_propagates():
    """Only a closed connection is swallowed.

    A serialisation bug or a programming error must not be hidden behind the
    same handler — that would trade one silent failure for another.
    """
    class _Broken:
        async def send_json(self, payload):
            raise TypeError("Object of type set is not JSON serializable")

    ctx = _context(_Broken())

    with pytest.raises(TypeError):
        asyncio.run(ctx.send({"event": "init"}))
