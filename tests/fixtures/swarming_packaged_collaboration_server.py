"""Unmodified personal GUI with two saved conversations and scripted Ollama.

Only fixture infrastructure and saved user configuration are created here. The
executable, native child entrypoint, collaboration store and GUI are unmodified.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import threading
import time
import urllib.request


def main():
    root, executable = Path(sys.argv[1]).resolve(strict=True), Path(sys.argv[2]).resolve(strict=True)
    home, workspace = root / "home", root / "project"
    home.mkdir()
    workspace.mkdir()
    (workspace / "fact.txt").write_text("Quoted CSV fields preserve commas.\n", encoding="utf-8")
    for key in ("HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA"):
        os.environ[key] = str(home)
    for key in tuple(os.environ):
        if any(part in key.upper() for part in ("API_KEY", "TOKEN", "SECRET", "PASSWORD", "PROXY")):
            os.environ.pop(key)
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    import psutil
    from lumi.gui.sessions import ProjectManager
    from lumi.gui.settings import DEFAULTS
    from lumi.processes import background_process_kwargs

    process = None
    owned, trace = [], []
    gate, lock = threading.Event(), threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def respond(self, value, *, status=200, media="application/json"):
            raw = value.encode() if isinstance(value, str) else json.dumps(value).encode()
            self.send_response(status)
            self.send_header("Content-Type", media)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            try:
                self.wfile.write(raw)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass

        def do_GET(self):
            if self.path == "/api/tags":
                self.respond({"models": [{"name": "fixture-native", "model": "fixture-native", "size": 1}]})
            elif self.path == "/api/version":
                self.respond({"version": "0.0.0-scripted-fixture"})
            elif self.path == "/api/ps":
                self.respond({"models": []})
            elif self.path == "/__fixture__/evidence":
                children = []
                if process and process.poll() is None:
                    for child in psutil.Process(process.pid).children(recursive=True):
                        try:
                            children.append({"pid": child.pid, "exe": child.exe(), "argv": child.cmdline()})
                        except (psutil.NoSuchProcess, psutil.AccessDenied):
                            pass
                with lock:
                    self.respond({"requests": list(trace), "children": children, "host_pid": process.pid})
            else:
                self.respond({"error": "Unknown fixture endpoint"}, status=404)

        def do_POST(self):
            size = int(self.headers.get("Content-Length", "0"))
            if size > 4 * 1024 * 1024:
                self.respond({"error": "Fixture input too large"}, status=413)
                return
            value = json.loads(self.rfile.read(size) or b"{}")
            if self.path == "/__fixture__/release":
                gate.set()
                self.respond({"released": True})
            elif self.path == "/api/show":
                self.respond({"capabilities": ["completion", "tools"], "template": "{{.Tools}}",
                    "model_info": {"general.architecture": "llama", "llama.context_length": 32768}})
            elif self.path == "/api/chat":
                observed = any(row.get("role") == "tool" for row in value.get("messages", []))
                with lock:
                    trace.append({"model": value.get("model"), "stream": value.get("stream"),
                        "observed_file_result": observed,
                        "input_sha256": hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()})
                if value.get("model") != "fixture-native" or value.get("stream") is not True:
                    self.respond({"error": "Only declared streaming worker calls are supported"}, status=400)
                    return
                if not observed and not gate.wait(60):
                    self.respond({"error": "Fixture release not received"}, status=503)
                    return
                message = ({"role": "assistant", "content": "Packaged collaborative finding: quoted CSV fields preserve commas. Owner review remains required."}
                    if observed else {"role": "assistant", "content": "", "tool_calls": [
                        {"function": {"name": "file_read", "arguments": {"path": "fact.txt"}}}]})
                response = [{"model": "fixture-native", "message": message, "done": False},
                    {"model": "fixture-native", "done": True, "done_reason": "stop", "prompt_eval_count": 10, "eval_count": 5}]
                self.respond("".join(json.dumps(row) + "\n" for row in response), media="application/x-ndjson")
            else:
                self.respond({"error": "Unknown fixture endpoint"}, status=404)

    api = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    api.daemon_threads = True
    threading.Thread(target=api.serve_forever, daemon=True).start()
    endpoint = f"http://127.0.0.1:{api.server_port}"
    settings = deepcopy(DEFAULTS)
    settings["general"].update(default_backend="ollama", default_model="fixture-native")
    settings["network"].update(ollama_url=endpoint, exo_url=endpoint, sonn_url="")
    profile = home / ".lumi"
    profile.mkdir(exist_ok=True)
    (profile / "settings.json").write_text(json.dumps(settings), encoding="utf-8")
    manager = ProjectManager(str(workspace))
    manager.set_project(str(workspace))
    sessions = []
    for title in ("Packaged origin conversation", "Packaged receiver conversation"):
        session = manager.create_session("ollama", "fixture-native")
        session.title, session.title_source = title, "manual"
        session.append_display_events([{"event": "user_message", "text": "Private ordinary fixture conversation."}])
        session.save()
        sessions.append(session)
    environment = dict(os.environ)
    environment.update(OLLAMA_HOST=endpoint, EXO_API_URL=endpoint, EXO_BASE_URL=endpoint, SONN_API_URL="", SONN_BASE_URL="")
    for key in tuple(environment):
        if key.startswith(("SONN_GOVERNANCE_", "SWARM_", "PG")) or key in {"PYTHONPATH", "PYTHONHOME"}:
            environment.pop(key)
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    url = f"http://127.0.0.1:{port}"
    try:
        with (root / "candidate-stream.log").open("ab") as output:
            process = subprocess.Popen([str(executable), "gui", "--browser", "--debug", "--host", "127.0.0.1", "--port", str(port)],
                cwd=workspace, env=environment, stdin=subprocess.DEVNULL, stdout=output, stderr=output,
                **background_process_kwargs())
        for _ in range(300):
            if process.poll() is not None:
                raise RuntimeError(f"Candidate exited during startup: {process.returncode}")
            try:
                with urllib.request.urlopen(url, timeout=.2) as response:
                    if response.status == 200:
                        if len(response.read(1024 * 1024 + 1)) > 1024 * 1024:
                            raise RuntimeError("Candidate startup page exceeds fixture bound")
                        break
            except (OSError, TimeoutError):
                time.sleep(.1)
        else:
            raise TimeoutError("Candidate startup timed out")
        receipt = {"url": url, "api_url": endpoint, "project": str(workspace), "host_pid": process.pid,
            "session_id": sessions[0].id, "other_session_id": sessions[1].id, "executable": str(executable),
            "executable_sha256": hashlib.sha256(executable.read_bytes()).hexdigest()}
        (root / "candidate.json").write_text(json.dumps(receipt, indent=2), encoding="utf-8")
        print(json.dumps(receipt), flush=True)
        sys.stdin.readline()
    finally:
        gate.set()
        if process and process.poll() is None:
            parent = psutil.Process(process.pid)
            owned.extend(parent.children(recursive=True))
            owned.append(parent)
        for child in owned:
            try:
                child.terminate()
            except psutil.NoSuchProcess:
                pass
        if owned:
            _, alive = psutil.wait_procs(owned, timeout=5)
            for child in alive:
                child.kill()
            psutil.wait_procs(alive, timeout=5)
        api.shutdown()
        (root / "transport.json").write_text(json.dumps(trace, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
