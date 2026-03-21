"""
WebSocket server for the Resonant Engine.

Allows remote clients (TUI, web, VS Code extension) to connect and
interact with the engine over WebSocket.

Usage:
    resonant serve [--port 8765]
"""

import asyncio
import json
import logging
import os
from typing import Optional

from ..events import EngineEvent, ClientCommand, make_event
from ..backends import create_backend, OllamaBackend, ClaudeBackend, OpenAIBackend
from .session import Session

logger = logging.getLogger(__name__)


class EngineServer:
    """
    WebSocket server that wraps Session and streams events to clients.
    """

    def __init__(
        self,
        backend,
        host: str = "0.0.0.0",
        port: int = 8765,
        max_steps: int = 25,
        max_tokens: int = 4096,
    ):
        self.backend = backend
        self.host = host
        self.port = port
        self.session = Session(
            backend=backend,
            max_steps=max_steps,
            max_tokens=max_tokens,
            auto_approve=True,  # Server mode: auto-approve by default
        )
        self._clients = set()

    async def handle_client(self, websocket):
        """Handle a single WebSocket client connection."""
        try:
            import websockets  # noqa: F401
        except ImportError:
            raise ImportError(
                "websockets package required for server mode. "
                "Install with: pip install websockets"
            )

        self._clients.add(websocket)
        logger.info("Client connected: %s", websocket.remote_address)

        # Send initial state
        await websocket.send(json.dumps(make_event(
            EngineEvent.STATUS,
            backend=self.backend.name,
            model=self.backend.model,
            cwd=os.getcwd(),
        )))

        try:
            async for raw_msg in websocket:
                try:
                    msg = json.loads(raw_msg)
                except json.JSONDecodeError:
                    await websocket.send(json.dumps(
                        make_event(EngineEvent.ERROR, message="Invalid JSON")
                    ))
                    continue

                cmd = msg.get("command", "")

                if cmd == ClientCommand.MESSAGE.value:
                    user_msg = msg.get("text", "")
                    if user_msg:
                        # Run the session in a thread to not block the event loop
                        await self._run_session(websocket, user_msg)

                elif cmd == ClientCommand.CLEAR.value:
                    self.session.clear()
                    await websocket.send(json.dumps(
                        make_event(EngineEvent.STATUS, message="Conversation cleared")
                    ))

                elif cmd == ClientCommand.SWITCH_MODEL.value:
                    model = msg.get("model", "")
                    if model and hasattr(self.backend, 'model'):
                        self.backend = create_backend(
                            self.backend.name,
                            url=getattr(self.backend, 'base_url', None),
                            model=model,
                            api_key=getattr(self.backend, 'api_key', None),
                        )
                        self.session.set_backend(self.backend)
                        await websocket.send(json.dumps(
                            make_event(EngineEvent.STATUS,
                                      message=f"Switched to {model}",
                                      model=model)
                        ))

                elif cmd == ClientCommand.CANCEL.value:
                    # TODO: Implement cancellation via threading event
                    pass

                else:
                    await websocket.send(json.dumps(
                        make_event(EngineEvent.ERROR, message=f"Unknown command: {cmd}")
                    ))

        except Exception as e:
            logger.error("Client error: %s", e)
        finally:
            self._clients.discard(websocket)
            logger.info("Client disconnected")

    async def _run_session(self, websocket, user_msg: str):
        """Run session.run() in a thread and stream events via WebSocket."""
        loop = asyncio.get_event_loop()

        def _generate_events():
            return list(self.session.run(user_msg))

        events = await loop.run_in_executor(None, _generate_events)

        for event in events:
            try:
                await websocket.send(json.dumps(event))
            except Exception:
                break

    async def start(self):
        """Start the WebSocket server."""
        try:
            import websockets
        except ImportError:
            raise ImportError(
                "websockets package required for server mode. "
                "Install with: pip install websockets"
            )

        logger.info("Starting Resonant Engine server on ws://%s:%d", self.host, self.port)

        async with websockets.serve(self.handle_client, self.host, self.port):
            print(f"\n  Resonant Engine server running on ws://{self.host}:{self.port}")
            print(f"  Backend: {self.backend.name} · {self.backend.model}")
            print(f"  CWD: {os.getcwd()}")
            print(f"\n  Connect with: resonant connect ws://localhost:{self.port}")
            print(f"  Press Ctrl+C to stop\n")
            await asyncio.Future()  # Run forever
