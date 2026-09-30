"""An Ollama-compatible server on loopback that records every request, for tests of what reaches a model.

Nothing here is a real model: ``/api/chat`` answers with fixed text and is recorded, so a test can tell
whether Lumi sent a model request, and when (the PR #104 review found warm-ups sent before the terms were
accepted). ``/api/tags``, ``/api/show`` and ``/api/version`` answer as a local Ollama with one model would.
"""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MODEL = "stub-model"
MODEL_PATHS = ("/api/chat", "/api/generate", "/v1/chat/completions")


class Recorder:
    """Every request the server received, in order, and markers a test adds between them."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.requests: list[dict] = []

    def add(self, method: str, path: str, body) -> None:
        with self.lock:
            self.requests.append({"at": time.time(), "method": method, "path": path, "body": body})

    def mark(self, label: str) -> None:
        """A point in the sequence (such as "terms accepted"), to tell what came before it."""
        self.add("MARK", label, None)

    def chats(self) -> list[dict]:
        """The model requests: what reached the model."""
        with self.lock:
            return [item for item in self.requests if item["path"] in MODEL_PATHS]

    def clear(self) -> None:
        with self.lock:
            self.requests.clear()


def start(recorder: Recorder) -> tuple[ThreadingHTTPServer, str]:
    """Serve on a free loopback port in a daemon thread; (server, base URL). Call server.shutdown() after."""

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

        def do_GET(self):
            recorder.add("GET", self.path, None)
            if self.path.startswith("/api/tags"):
                return self._json({"models": [{"name": MODEL, "model": MODEL, "details": {"family": "stub"}}]})
            if self.path.startswith("/api/version"):
                return self._json({"version": "0.12.0"})
            if self.path.startswith("/api/ps"):
                return self._json({"models": []})
            return self._json({"error": "not found"}, 404)

        def do_POST(self):
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            try:
                body = json.loads(raw or b"{}")
            except ValueError:
                body = raw.decode("utf-8", "replace")
            recorder.add("POST", self.path, body)
            if self.path.startswith("/api/show"):
                return self._json({"capabilities": ["completion", "tools"], "template": "{{ .Tools }}",
                                   "model_info": {"stub.context_length": 8192}, "details": {"family": "stub"}})
            if self.path.startswith("/api/chat"):
                message = {"role": "assistant", "content": "stub reply"}
                final = {"model": MODEL, "message": {"role": "assistant", "content": ""}, "done": True,
                         "done_reason": "stop", "prompt_eval_count": 3, "eval_count": 2}
                if isinstance(body, dict) and body.get("stream", True):
                    data = (json.dumps({"model": MODEL, "message": message, "done": False}) + "\n"
                            + json.dumps(final) + "\n").encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "application/x-ndjson")
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                    return None
                return self._json({**final, "message": message})
            return self._json({"error": "not found"}, 404)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True, name="ollama-recorder").start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"
