"""Ask a running Lumi GUI a question over its own WebSocket, as the page does (CI smoke tests).

    python3 packaging/smoke_gui.py --port 8975 --token TOKEN update_status

Connects to ``/ws`` with the launch's access token (gui/local_access.py: the
exact Host, the server's own Origin and the ``lumi.access.<token>``
subprotocol), sends ``{"command": <command>}`` and prints the ``data`` of the
first event of the same name as JSON. Standard library only, so it runs with
any Python 3 on a build runner, next to the packaged app it checks.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import socket
import struct
import sys
import time


class Socket:
    """Just enough of RFC 6455 for a client: masked text out, whole messages in."""

    def __init__(self, port: int, token: str, timeout: float):
        self.sock = socket.create_connection(("127.0.0.1", port), timeout=timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        request = (
            f"GET /ws HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\nOrigin: http://127.0.0.1:{port}\r\n"
            f"Sec-WebSocket-Protocol: lumi.v1, lumi.access.{token}\r\n\r\n")
        self.sock.sendall(request.encode())
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise SystemExit("The server closed the connection during the handshake")
            head += chunk
        head, _, self.buffer = head.partition(b"\r\n\r\n")
        status = head.split(b"\r\n", 1)[0]
        if b" 101 " not in status + b" ":
            raise SystemExit(f"The WebSocket handshake was refused: {status.decode(errors='replace')}")

    def send(self, text: str) -> None:
        self._frame(0x1, text.encode())

    def _frame(self, opcode: int, data: bytes) -> None:
        mask = os.urandom(4)
        size = len(data)
        if size < 126:
            header = struct.pack("!BB", 0x80 | opcode, 0x80 | size)
        elif size < 1 << 16:
            header = struct.pack("!BBH", 0x80 | opcode, 0x80 | 126, size)
        else:
            header = struct.pack("!BBQ", 0x80 | opcode, 0x80 | 127, size)
        self.sock.sendall(header + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(data)))

    def _exact(self, size: int) -> bytes:
        while len(self.buffer) < size:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise SystemExit("The server closed the WebSocket")
            self.buffer += chunk
        data, self.buffer = self.buffer[:size], self.buffer[size:]
        return data

    def receive(self) -> str:
        """The next whole text message; answers pings on the way."""
        parts: list[bytes] = []
        while True:
            first, second = self._exact(2)
            opcode, size = first & 0x0F, second & 0x7F
            if size == 126:
                (size,) = struct.unpack("!H", self._exact(2))
            elif size == 127:
                (size,) = struct.unpack("!Q", self._exact(8))
            data = self._exact(size)  # a server doesn't mask
            if opcode == 0x8:
                raise SystemExit("The server closed the WebSocket")
            if opcode == 0x9:
                self._frame(0xA, data)
                continue
            if opcode in (0x0, 0x1):
                parts.append(data)
                if first & 0x80:
                    return b"".join(parts).decode()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--token", required=True, help="The access token the one-time launch code was redeemed for")
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("command", help="A socket command that answers with an event of the same name")
    args = parser.parse_args(argv)
    ws = Socket(args.port, args.token, args.timeout)
    ws.send(json.dumps({"command": args.command}))
    deadline = time.monotonic() + args.timeout
    while time.monotonic() < deadline:
        message = json.loads(ws.receive())
        if isinstance(message, dict) and message.get("event") == args.command:
            print(json.dumps(message.get("data"), indent=2, sort_keys=True))
            return 0
    print(f"No {args.command} event within {args.timeout:.0f} seconds", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
