"""Scripted Anthropic Messages and OpenAI Responses servers for Team tests.

A loopback HTTP server that answers both APIs as the providers stream them:
server-sent events, tool calls with streamed arguments, signed thinking
(Anthropic) or encrypted reasoning (OpenAI), and usage. A test gives it a
``script``: a function from what a request asked (a ``Turn``) to the ``Reply``
it gets. Every request is recorded (path, headers, body), so tests can check
what Lumi's adapters (lumi/anthropic_api.py, lumi/openai_api.py) sent.

A request without the fixture key (``x-api-key``, ``api-key`` or a bearer
token) is refused with 401, and a Messages request whose history holds tool
calls but defines no tools with 400, as the providers would.

NOT a test file: test files import it.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
import time
from typing import Callable

KEY = "fixture-provider-key"
# Every reply reports the same usage, so a test can price it.
ANTHROPIC_USAGE = {"input_tokens": 1000, "cache_read_input_tokens": 200, "cache_creation_input_tokens": 100}
OPENAI_USAGE = {"input_tokens": 1000, "input_tokens_details": {"cached_tokens": 200},
                "output_tokens": 50, "output_tokens_details": {"reasoning_tokens": 10}}
OUTPUT_TOKENS = 50


@dataclass
class Turn:
    """What one request asked: its protocol, model, the user text and the tool results so far."""

    protocol: str
    model: str
    prompt: str
    results: list[str]
    body: dict

    @property
    def step(self) -> int:
        """How many tool calls this participant has made so far."""
        return len(self.results)


@dataclass
class Reply:
    """One streamed answer: prose, a tool call, reasoning; or a hold, or an HTTP error."""

    text: str = ""
    tool: tuple[str, dict] | None = None
    thinking: str = ""
    # Keep the stream open with pings (Anthropic) until this is set or the client leaves.
    hold: threading.Event | None = None
    status: int = 200
    extra: dict = field(default_factory=dict)


def _anthropic_turn(body: dict) -> Turn:
    prompt, results = [], []
    for message in body.get("messages") or []:
        for block in message.get("content") or []:
            if message.get("role") == "user" and block.get("type") == "text":
                prompt.append(str(block.get("text") or ""))
            elif block.get("type") == "tool_result":
                content = block.get("content")
                results.append(content if isinstance(content, str) else json.dumps(content))
    return Turn("anthropic", str(body.get("model") or ""), "\n".join(prompt), results, body)


def _openai_turn(body: dict) -> Turn:
    prompt, results = [], []
    for item in body.get("input") or []:
        if item.get("role") == "user":
            prompt += [str(part.get("text") or "") for part in item.get("content") or []
                       if part.get("type") == "input_text"]
        elif item.get("type") == "function_call_output":
            results.append(str(item.get("output") or ""))
    return Turn("openai", str(body.get("model") or ""), "\n".join(prompt), results, body)


def _anthropic_events(reply: Reply, number: int, model: str) -> list[dict]:
    message_id = f"msg_fixture_{number}"
    events = [{"type": "message_start", "message": {"id": message_id, "type": "message", "role": "assistant",
               "model": model, "content": [], "usage": {**ANTHROPIC_USAGE, "output_tokens": 1}}}]
    index = 0

    def block(start: dict, *deltas: dict) -> None:
        nonlocal index
        events.append({"type": "content_block_start", "index": index, "content_block": start})
        events.extend({"type": "content_block_delta", "index": index, "delta": delta} for delta in deltas)
        events.append({"type": "content_block_stop", "index": index})
        index += 1

    if reply.thinking:
        block({"type": "thinking", "thinking": ""}, {"type": "thinking_delta", "thinking": reply.thinking},
              {"type": "signature_delta", "signature": f"sig-{number}"})
    if reply.text:
        half = len(reply.text) // 2
        block({"type": "text", "text": ""}, {"type": "text_delta", "text": reply.text[:half]},
              {"type": "text_delta", "text": reply.text[half:]})
    if reply.tool:
        name, arguments = reply.tool
        encoded = json.dumps(arguments)
        block({"type": "tool_use", "id": f"toolu_fixture_{number}", "name": name, "input": {}},
              {"type": "input_json_delta", "partial_json": encoded[:5]},
              {"type": "input_json_delta", "partial_json": encoded[5:]})
    events.append({"type": "message_delta", "delta": {"stop_reason": "tool_use" if reply.tool else "end_turn"},
                   "usage": {"output_tokens": OUTPUT_TOKENS}})
    events.append({"type": "message_stop"})
    return events


def _openai_events(reply: Reply, number: int) -> list[dict]:
    response_id = f"resp_fixture_{number}"
    events = [{"type": "response.created", "response": {"id": response_id, "status": "in_progress"}}]
    output: list[dict] = []

    def item(added: dict, done: dict, *between: dict) -> None:
        index = len(output)
        events.append({"type": "response.output_item.added", "output_index": index, "item": added})
        events.extend({**event, "output_index": index} for event in between)
        events.append({"type": "response.output_item.done", "output_index": index, "item": done})
        output.append(done)

    if reply.thinking:
        reasoning = {"type": "reasoning", "id": f"rs_fixture_{number}",
                     "summary": [{"type": "summary_text", "text": reply.thinking}],
                     "encrypted_content": f"enc-{number}"}
        item({"type": "reasoning", "id": reasoning["id"]}, reasoning,
             {"type": "response.reasoning_summary_text.delta", "delta": reply.thinking})
    if reply.text:
        half = len(reply.text) // 2
        message = {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": reply.text}]}
        item({"type": "message"}, message, {"type": "response.output_text.delta", "delta": reply.text[:half]},
             {"type": "response.output_text.delta", "delta": reply.text[half:]})
    if reply.tool:
        name, arguments = reply.tool
        call = {"type": "function_call", "id": f"fc_fixture_{number}", "call_id": f"call_fixture_{number}",
                "name": name, "arguments": json.dumps(arguments)}
        item({**call, "arguments": ""}, call,
             {"type": "response.function_call_arguments.delta", "delta": call["arguments"]})
    events.append({"type": "response.completed", "response": {"id": response_id, "status": "completed",
                                                              "output": output, "usage": OPENAI_USAGE}})
    return events


class _Handler(BaseHTTPRequestHandler):
    server: ScriptedProviders

    def log_message(self, *_):
        pass

    def _send(self, text: bytes) -> bool:
        try:
            self.wfile.write(text)
            self.wfile.flush()
            return True
        except OSError:
            return False  # The client went away (Stop).

    def do_POST(self):  # noqa: N802 - http.server naming
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        path = self.path.split("?", 1)[0]
        protocol = "anthropic" if path.endswith("/messages") else "openai" if path.endswith("/responses") else ""
        headers = {name.lower(): value for name, value in self.headers.items()}
        with self.server.lock:
            self.server.requests.append({"path": self.path, "protocol": protocol, "headers": headers, "body": body})
            number = len(self.server.requests)
        credentials = {headers.get("x-api-key"), headers.get("api-key"), headers.get("authorization")}
        if not protocol or not credentials & {KEY, f"Bearer {KEY}"}:
            self._error(401 if protocol else 404, "authentication_error", "invalid x-api-key")
            return
        if protocol == "anthropic" and not body.get("tools") and any(
                block.get("type") in {"tool_use", "tool_result"}
                for message in body.get("messages") or [] for block in message.get("content") or []):
            # As the Messages API answers a history with tool calls and no tool definitions.
            self._error(400, "invalid_request_error",
                        "Requests which include `tool_use` or `tool_result` blocks must define tools.")
            return
        turn = (_anthropic_turn if protocol == "anthropic" else _openai_turn)(body)
        try:
            reply = self.server.script(turn)
        except Exception as exc:  # noqa: BLE001 - an unscripted request says so to the test
            self.server.errors.append(f"request {number}: {type(exc).__name__}: {exc}")
            self._error(400, "invalid_request_error", "The fixture has no reply scripted for this request")
            return
        if not reply.thinking and (body.get("thinking") or body.get("reasoning")):
            # A request that asks for thinking (Claude) or reasoning (GPT) gets
            # it first, signed or encrypted, as the providers answer.
            reply = replace(reply, thinking="Weighing the request.")
        if reply.status != 200:
            self._error(reply.status, "overloaded_error" if reply.status == 529 else "api_error", "fixture refusal")
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()  # No length: the stream ends when the connection closes (HTTP/1.0).
        events = (_anthropic_events(reply, number, turn.model) if protocol == "anthropic"
                  else _openai_events(reply, number))
        if reply.hold is not None:
            # A long response: the first event, then keep-alive pings until released.
            if not self._send(_sse(events[0])):
                return
            while not reply.hold.wait(0.05):
                if not self._send(b"event: ping\ndata: {\"type\": \"ping\"}\n\n"):
                    with self.server.lock:
                        self.server.abandoned.append(number)
                    return
            events = events[1:]
        for event in events:
            if not self._send(_sse(event)):
                return

    def _error(self, status: int, kind: str, message: str) -> None:
        payload = json.dumps({"type": "error", "error": {"type": kind, "message": message}}).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self._send(payload)


def _sse(event: dict) -> bytes:
    return f"event: {event['type']}\ndata: {json.dumps(event)}\n\n".encode()


class ScriptedProviders(ThreadingHTTPServer):
    """Both APIs on one loopback address: ``/v1/messages`` and ``/v1/responses`` (or ``/openai/v1/responses``)."""

    daemon_threads = True

    def __init__(self, script: Callable[[Turn], Reply] | None = None) -> None:
        super().__init__(("127.0.0.1", 0), _Handler)
        self.script = script or (lambda turn: Reply(text="Fixture answer."))
        self.lock = threading.Lock()
        self.requests: list[dict] = []
        self.errors: list[str] = []
        self.abandoned: list[int] = []
        self._thread = threading.Thread(target=self.serve_forever, daemon=True, name="provider-fixture")
        self._thread.start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server_address[1]}"

    def of(self, protocol: str) -> list[dict]:
        with self.lock:
            return [request for request in self.requests if request["protocol"] == protocol]

    def close(self) -> None:
        self.shutdown()
        self.server_close()
        self._thread.join(timeout=5)
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and self._thread.is_alive():
            time.sleep(0.01)
