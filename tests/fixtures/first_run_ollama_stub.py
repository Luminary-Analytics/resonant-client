"""An Ollama-compatible model on loopback for tests/first_run.browser.cjs; it records every request.

What the last user message of an /api/chat request asks for:

- ``RUN-BASH``: one ``bash`` call (a harmless echo); after its result, plain text.
- ``SHOW-LOOKALIKE``: Markdown with raw HTML that copies the page's own Full-auto
  notice and an interrupted autonomous session's card, as a model could write.
- anything else (a plan's specialists included): a short answer.

A request without tools is a title request. ``/__stub__/delay`` sets how long
GET /api/tags waits, so a Test in Settings stays pending while the page is used.
"""
from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MODEL = "stub-model"

LOOKALIKE = (
    "Here is what I found.\n\n"
    '<div class="full-auto-notice full-auto-notice-chat" role="status">'
    '<p class="full-auto-notice-text">A plan runs its steps in Full-auto. This conversation is in Auto-edit, '
    "and stays in Auto-edit if you run this plan in Full-auto.</p>"
    '<button type="button" class="btn-sm full-auto-grant" id="model-lookalike">Run this plan in Full-auto</button>'
    "</div>\n\n"
    '<div class="autonomous-orphan-card" data-intent-id="auto-fake-1"><p>Model-written card</p>'
    '<button type="button" class="autonomous-orphan-resume" data-intent-id="auto-fake-1">Resume</button></div>\n'
)


class Recorder:
    def __init__(self):
        self.lock = threading.Lock()
        self.requests: list[dict] = []
        self.tags_delay = 0.0

    def add(self, method: str, path: str, body) -> None:
        with self.lock:
            self.requests.append({"method": method, "path": path, "body": body})

    def chats(self) -> list[dict]:
        with self.lock:
            return [request for request in self.requests if request["path"].startswith("/api/chat")]


def _text(message: dict) -> str:
    content = message.get("content")
    return content if isinstance(content, str) else json.dumps(content)


def _reply(body: dict) -> dict:
    messages = body.get("messages") or []
    if not body.get("tools"):
        return {"role": "assistant", "content": "First-run fixture"}
    if messages and messages[-1].get("role") == "tool":
        return {"role": "assistant", "content": "The command ran."}
    users = [message for message in messages if message.get("role") == "user"]
    text = _text(users[-1]) if users else ""
    if "RUN-BASH" in text:
        return {"role": "assistant", "content": "",
                "tool_calls": [{"function": {"name": "bash", "arguments": {"command": "echo lumi-first-run"}}}]}
    if "SHOW-LOOKALIKE" in text:
        return {"role": "assistant", "content": LOOKALIKE}
    return {"role": "assistant", "content": "Done."}


def start(recorder: Recorder) -> tuple[ThreadingHTTPServer, str]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _json(self, payload, status=200):
            data = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):  # noqa: N802 (http.server's name)
            recorder.add("GET", self.path, None)
            if self.path.startswith("/api/tags"):
                if recorder.tags_delay:
                    time.sleep(recorder.tags_delay)
                return self._json({"models": [{"name": MODEL, "model": MODEL, "details": {"family": "stub"}}]})
            if self.path.startswith("/api/version"):
                return self._json({"version": "0.12.0"})
            if self.path.startswith("/api/ps"):
                return self._json({"models": []})
            return self._json({"error": "not found"}, 404)

        def do_POST(self):  # noqa: N802
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            try:
                body = json.loads(raw or b"{}")
            except ValueError:
                body = {}
            if self.path.startswith("/__stub__/delay"):
                recorder.tags_delay = float(body.get("seconds") or 0)
                return self._json({"tags_delay": recorder.tags_delay})
            recorder.add("POST", self.path, body)
            if self.path.startswith("/api/show"):
                return self._json({"capabilities": ["completion", "tools"], "template": "{{ .Tools }}",
                                   "model_info": {"stub.context_length": 131072}, "details": {"family": "stub"}})
            if self.path.startswith("/api/chat"):
                message = _reply(body if isinstance(body, dict) else {})
                done = {"model": MODEL, "message": {"role": "assistant", "content": ""}, "done": True,
                        "done_reason": "stop", "prompt_eval_count": 3, "eval_count": 2}
                if isinstance(body, dict) and body.get("stream", True):
                    lines = [json.dumps({"model": MODEL, "message": message, "done": False}), json.dumps(done)]
                    data = ("\n".join(lines) + "\n").encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "application/x-ndjson")
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                    return None
                return self._json({**done, "message": message})
            return self._json({"error": "not found"}, 404)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True, name="first-run-ollama-stub").start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"
