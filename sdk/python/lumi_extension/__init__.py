"""Write Lumi extensions in Python: the Extension SDK v1 (docs/extensions.md in the Lumi repository).

Standard library only, so a pack can carry this folder as it is. A model
provider subclasses ``Provider`` and calls ``serve``::

    from lumi_extension import Provider, done, serve, text

    class Acme(Provider):
        def models(self):
            return [{"id": "acme-1", "context_window": 32768, "tools": True}]

        def stream(self, request):
            reply = call_your_model(request.model, request.messages, request.api_key)
            yield text(reply)
            yield done(input_tokens=..., output_tokens=...)

    if __name__ == "__main__":
        raise SystemExit(serve(Acme()))

Lumi starts the process for each request (the pack's ``providers`` in
``lumi-pack.json``), writes one JSON request to its stdin, and reads one JSON
object per line from its stdout. ``lumi_extension.testing`` runs a provider
the same way, and checks what it writes.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, TextIO

__all__ = ["PROTOCOL", "Provider", "StreamRequest", "data_dir", "done", "error", "serve", "text", "tool_call"]
PROTOCOL = 1


def data_dir() -> Path:
    """A folder of this extension's own for files it keeps, such as caches or sign-in tokens.

    Never write in the pack's folder: any change there turns the pack off
    until the person approves it again.
    """
    folder = Path(os.environ.get("LUMI_EXTENSION_DATA") or Path(tempfile.gettempdir()) / "lumi-extension")
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def text(content: str) -> dict:
    """Part of the answer's text; Lumi shows parts as they arrive."""
    return {"type": "text", "text": str(content)}


def tool_call(name: str, arguments: dict | None = None, id: str = "") -> dict:  # noqa: A002 - the protocol's name
    """Ask Lumi to run one of the request's tools; its result comes back in the next request's messages."""
    return {"type": "tool_call", "id": id, "name": name, "arguments": dict(arguments or {})}


def done(input_tokens: int = 0, output_tokens: int = 0, cost_usd: float | None = None) -> dict:
    """The answer is complete. The token counts, and the cost when you know it, go to Lumi's usage
    records and budgets; without a cost, Lumi prices the model if it knows it, else counts it unpriced."""
    event: dict = {"type": "done", "usage": {"input_tokens": int(input_tokens), "output_tokens": int(output_tokens)}}
    if cost_usd is not None:
        event["cost_usd"] = float(cost_usd)
    return event


def error(message: str) -> dict:
    """The request failed; Lumi shows the message."""
    return {"type": "error", "message": str(message)}


@dataclass
class StreamRequest:
    """One request from Lumi: the model, the conversation, the tools, and the connection's key."""

    model: str
    messages: list[dict] = field(default_factory=list)
    tools: list[dict] = field(default_factory=list)
    max_tokens: int | None = None
    api_key: str = ""


class Provider:
    """A model provider: override ``models`` and ``stream``."""

    def models(self) -> list[dict]:
        """The models to offer: ``{"id", "context_window", "tools"}`` each."""
        return []

    def stream(self, request: StreamRequest) -> Iterable[dict]:
        """Yield ``text`` and ``tool_call`` events, then ``done`` (or ``error``)."""
        raise NotImplementedError


def serve(provider: Provider, stdin: TextIO | None = None, stdout: TextIO | None = None) -> int:
    """Answer one request from Lumi; the process's exit code."""
    stdin, stdout = stdin or sys.stdin, stdout or sys.stdout

    def write(event: dict) -> None:
        stdout.write(json.dumps(event, ensure_ascii=False) + "\n")
        stdout.flush()

    try:
        request = json.loads(stdin.readline() or "{}")
    except ValueError:
        write(error("Lumi's request wasn't JSON."))
        return 2
    if not isinstance(request, dict) or request.get("lumi_extension") != PROTOCOL:
        write(error(f"This provider speaks Lumi extension protocol {PROTOCOL}."))
        return 2
    method, params = request.get("method"), request.get("params") or {}
    try:
        if method == "models":
            write({"type": "models", "models": list(provider.models())})
            return 0
        if method == "stream":
            call = StreamRequest(model=str(params.get("model") or ""), messages=list(params.get("messages") or []),
                                 tools=list(params.get("tools") or []), max_tokens=params.get("max_tokens"),
                                 api_key=os.environ.get("LUMI_PROVIDER_API_KEY", ""))
            for event in provider.stream(call):
                write(event)
                if event.get("type") in ("done", "error"):
                    return 0
            write(done())
            return 0
        write(error(f"Unknown method {method!r}."))
        return 2
    except Exception as exc:  # noqa: BLE001 - whatever failed, Lumi gets a message
        write(error(f"{type(exc).__name__}: {exc}"))
        return 1
