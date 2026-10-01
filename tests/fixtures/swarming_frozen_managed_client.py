"""Fixture-only launcher for an unmodified managed candidate and HTTP provider."""
from __future__ import annotations

from copy import deepcopy
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import socket
import subprocess
import threading
import time
from urllib.request import urlopen

import psutil

from lumi.gui.sessions import ProjectManager
from lumi.gui.settings import DEFAULTS
from lumi.processes import background_process_kwargs


class FrozenManagedClient:
    """Keep fixture controls outside the candidate, including provider release."""

    def __init__(self, root: Path, config_path: Path, executable: Path, *, writer=False, evidence=None, allow_restart=False):
        self.root, self.process = root, None
        self.gate, self.lock = threading.Event(), threading.Lock()
        self.trace, self.observed_children, self.child_identities = [], set(), {}
        self.config = json.loads(config_path.read_text(encoding="utf-8"))
        self.forbidden = [str(config_path), self.config["transport"]["private_key_file"],
            Path(self.config["transport"]["private_key_file"]).read_text(encoding="utf-8"),
            self.config["host_id"], self.config["transport"]["endpoint"]]
        owner = self

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
                    self.respond({"models": [{"name": "chosen", "model": "chosen", "size": 1}]})
                elif self.path == "/api/version":
                    self.respond({"version": "0.0.0-scripted-fixture"})
                elif self.path == "/api/ps":
                    self.respond({"models": []})
                elif self.path == "/__fixture__/evidence":
                    children = owner.children()
                    extra = evidence() if evidence else {}
                    with owner.lock:
                        self.respond({"owned_children": len(owner.observed_children), "children": children,
                            "observed_children": list(owner.child_identities.values()),
                            "provider_entered": bool(owner.trace), "requests": list(owner.trace),
                            "private_configuration_in_model": any(row["private_configuration_in_model"] for row in owner.trace),
                            "peer_proposal_in_model": any(row["peer_proposal_in_model"] for row in owner.trace),
                            "live_providers_called": False, "frozen": True, **extra})
                else:
                    self.respond({"error": "Unknown fixture endpoint"}, status=404)

            def do_POST(self):
                size = int(self.headers.get("Content-Length", "0"))
                if size > 4 * 1024 * 1024:
                    self.respond({"error": "Fixture input too large"}, status=413)
                    return
                value = json.loads(self.rfile.read(size) or b"{}")
                if self.path == "/__fixture__/release":
                    owner.gate.set()
                    self.respond({"released": True})
                elif self.path == "/__fixture__/crash" and allow_restart:
                    self.respond(owner.crash())
                elif self.path == "/__fixture__/restart" and allow_restart:
                    owner.launch()
                    self.respond({"restarted": True, "host_pid": owner.process.pid})
                elif self.path == "/api/show":
                    self.respond({"capabilities": ["completion", "tools"], "template": "{{.Tools}}",
                        "model_info": {"general.architecture": "llama", "llama.context_length": 32768}})
                elif self.path == "/api/chat":
                    encoded = json.dumps(value, sort_keys=True)
                    observed = any(row.get("role") == "tool" for row in value.get("messages", []))
                    owner.children()
                    with owner.lock:
                        owner.trace.append({"input_sha256": hashlib.sha256(encoded.encode()).hexdigest(),
                            "observed_file_result": observed, "model": value.get("model"),
                            "private_configuration_in_model": any(item in encoded or json.dumps(item)[1:-1] in encoded for item in owner.forbidden),
                            "peer_proposal_in_model": "Selected peer proposal: inspect only your own fact" in encoded})
                    if value.get("model") != "chosen" or value.get("stream") is not True:
                        self.respond({"error": "Only declared streaming requests are supported"}, status=400)
                        return
                    if not owner.gate.wait(90):
                        self.respond({"error": "Fixture release not received"}, status=503)
                        return
                    message = ({"role": "assistant", "content": "Receiver-owned result observed. Separate owner review required."}
                        if observed else {"role": "assistant", "content": "", "tool_calls": [
                            {"function": {"name": "file_write" if writer else "file_read", "arguments":
                                {"path": "src/value.txt", "content": "verified change\n"} if writer else {"path": "fact.txt"}}}]})
                    rows = [{"model": "chosen", "message": message, "done": False},
                        {"model": "chosen", "done": True, "done_reason": "stop", "prompt_eval_count": 10, "eval_count": 5}]
                    self.respond("".join(json.dumps(row) + "\n" for row in rows), media="application/x-ndjson")
                else:
                    self.respond({"error": "Unknown fixture endpoint"}, status=404)

        self.api = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.api.daemon_threads = True
        threading.Thread(target=self.api.serve_forever, daemon=True).start()
        endpoint = f"http://127.0.0.1:{self.api.server_port}"
        home, workspace = root / "home", root / "project"
        home.mkdir()
        profile = home / ".lumi"
        profile.mkdir()
        settings = deepcopy(DEFAULTS)
        settings["general"].update(default_backend="ollama", default_model="chosen")
        settings["network"].update(ollama_url=endpoint, exo_url=endpoint, sonn_url="")
        (profile / "settings.json").write_text(json.dumps(settings), encoding="utf-8")
        keys = ("HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA")
        old = {key: os.environ.get(key) for key in keys}
        try:
            for key in keys:
                os.environ[key] = str(home)
            manager = ProjectManager(str(workspace))
            manager.set_project(str(workspace))
            record = manager.create_session("ollama", "chosen")
            record.title, record.title_source = "Independent managed conversation " + root.name, "manual"
            record.append_display_events([{"event": "user_message", "text": "PRIVATE saved conversation " + root.name}])
            record.save()
        finally:
            for key, value in old.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
        environment = {key: value for key, value in os.environ.items()
            if not any(word in key.upper() for word in ("API_KEY", "TOKEN", "SECRET", "PASSWORD", "PROXY"))
            and not key.startswith(("PG", "SONN_GOVERNANCE_", "LUMI_GOVERNANCE_", "SWARM_")) and key not in {"PYTHONPATH", "PYTHONHOME"}}
        environment.update({key: str(home) for key in keys})
        environment.update(OLLAMA_HOST=endpoint, EXO_API_URL=endpoint, EXO_BASE_URL=endpoint, SONN_API_URL="", SONN_BASE_URL="")
        with socket.socket() as reservation:
            reservation.bind(("127.0.0.1", 0))
            port = reservation.getsockname()[1]
        url = f"http://127.0.0.1:{port}"
        self.command = [str(executable), "gui", "--browser", "--debug", "--host", "127.0.0.1", "--port", str(port),
                        "--swarm-managed-config", str(config_path)]
        self.environment, self.workspace, self.url = environment, workspace, url
        self.log = (root / "candidate-stream.log").open("ab")
        try:
            self.launch()
        except BaseException:
            self.close()
            raise
        self.info = {"url": url, "api_url": endpoint, "session_id": record.id, "workspace": str(workspace),
            "executable": str(executable), "executable_sha256": hashlib.sha256(executable.read_bytes()).hexdigest(),
            "host_pid": self.process.pid, "frozen": True}

    def launch(self):
        """Start the exact candidate again with unchanged HOME, config and port."""
        if self.process and self.process.poll() is None:
            raise RuntimeError("Candidate already runs")
        self.process = subprocess.Popen(self.command, cwd=self.workspace, env=self.environment, stdin=subprocess.DEVNULL,
            stdout=self.log, stderr=self.log, **background_process_kwargs())
        for _ in range(300):
            if self.process.poll() is not None:
                raise RuntimeError("Frozen managed GUI exited before readiness")
            try:
                with urlopen(self.url, timeout=.2) as response:
                    if response.status == 200:
                        if len(response.read(1024 * 1024 + 1)) > 1024 * 1024:
                            raise RuntimeError("Candidate startup page exceeds fixture bound")
                        return
            except (OSError, TimeoutError):
                time.sleep(.1)
        raise TimeoutError("Frozen managed GUI readiness timed out")

    def crash(self):
        """Kill only the fixture app; independently observe named-job cleanup."""
        if not self.process or self.process.poll() is not None:
            raise RuntimeError("Candidate is not running")
        parent = psutil.Process(self.process.pid)
        children = parent.children(recursive=True)
        identities = [{"pid": child.pid, "created_at": child.create_time(), "argv": child.cmdline()} for child in children]
        self.process.kill()
        self.process.wait(timeout=5)
        _, alive = psutil.wait_procs(children, timeout=10)
        if alive:
            raise RuntimeError("Owned child cleanup was not confirmed")
        result = {"host_pid": parent.pid, "host_exited": True, "children_stopped": True, "children": identities}
        (self.root / "crash.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
        return result

    def children(self):
        result = []
        if self.process and self.process.poll() is None:
            for child in psutil.Process(self.process.pid).children(recursive=True):
                try:
                    row = {"pid": child.pid, "exe": child.exe(), "argv": child.cmdline()}
                    result.append(row)
                    if "--swarm-worker" in row["argv"]:
                        with self.lock:
                            self.observed_children.add(child.pid)
                            self.child_identities[child.pid] = row
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    pass
        return result

    def close(self):
        self.gate.set()
        if self.process and self.process.poll() is None:
            parent = psutil.Process(self.process.pid)
            owned = parent.children(recursive=True) + [parent]
            for child in owned:
                try:
                    child.terminate()
                except psutil.NoSuchProcess:
                    pass
            _, alive = psutil.wait_procs(owned, timeout=5)
            for child in alive:
                child.kill()
            psutil.wait_procs(alive, timeout=5)
        self.api.shutdown()
        self.api.server_close()
        self.log.close()
        (self.root / "transport.json").write_text(json.dumps(self.trace, indent=2), encoding="utf-8")
