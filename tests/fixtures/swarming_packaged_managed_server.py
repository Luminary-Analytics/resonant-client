"""Unmodified managed GUI CLI against real mTLS/PostgreSQL and scripted HTTP.

The infrastructure process alone owns database credentials and certificate
issuance. The candidate is launched normally with protected operator config;
no application or worker implementation is patched. Pass --source in place of
an executable to qualify the same CLI through the isolated fixture Python.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict
import getpass
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
from types import SimpleNamespace
import urllib.request


def main():
    root = Path(sys.argv[1]).resolve(strict=True)
    database = json.loads(Path(sys.argv[2]).read_text(encoding="utf-8-sig"))
    source_mode = sys.argv[3] == "--source"
    executable = Path(sys.executable if source_mode else sys.argv[3]).resolve(strict=True)
    if os.name == "nt":
        subprocess.run(["icacls", str(root), "/inheritance:r", "/grant:r", f"{getpass.getuser()}:(OI)(CI)(F)",
                        "*S-1-5-18:(OI)(CI)(F)", "*S-1-5-32-544:(OI)(CI)(F)"],
                       check=True, capture_output=True, timeout=10)
    home, workspace, operator = root / "home", root / "project", root / "operator"
    for directory in (home, workspace, operator):
        directory.mkdir()
    (workspace / "fact.txt").write_text("PRIVATE_PACKAGED quoted CSV fields preserve commas.\n", encoding="utf-8")
    for key in ("HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA"):
        os.environ[key] = str(home)
    for key in tuple(os.environ):
        if any(part in key.upper() for part in ("API_KEY", "TOKEN", "SECRET", "PASSWORD", "PROXY")):
            os.environ.pop(key)
    source = Path(__file__).resolve().parents[2]
    from governance_source import governance_paths
    sys.path[:0] = [str(source), *governance_paths()]
    import psutil
    from lumi.engine.swarming.managed_client import HostChannelClient
    from lumi.engine.swarming.service import _READ_TOOLS
    from lumi.gui.sessions import ProjectManager
    from lumi.gui.settings import DEFAULTS
    from lumi.processes import background_process_kwargs
    from sonn_governance.hosts import HostGovernance
    from sonn_governance.managed_resources import ManagedResources
    from sonn_governance.models import CommandEnvelope
    from sonn_governance.monitoring import RunMonitoring
    from test_host_http import actual_host, certificates as certificate_fixture
    from test_managed_host_client import configuration
    from test_store import Tenant, uid

    certificates = certificate_fixture.__wrapped__(SimpleNamespace(mktemp=lambda _: operator))
    tenant = Tenant(database)
    tenant.member(tenant.admin, tenant=["membership_admin", "policy_admin", "host_admin", "metadata_read", "control_execute"])
    tenant.store.command(tenant.admin, tenant.command(
        allowed_models=[{"provider": "ollama", "model": "fixture-managed-native"}],
        allowed_tools=sorted(_READ_TOOLS), request_limit=20))
    hosts = HostGovernance(tenant.store, minimum_runner_protocol=2)
    monitoring = RunMonitoring(hosts)
    pending = hosts.owner_command(tenant.admin, CommandEnvelope(1, uid(), tenant.id, tenant.project, 0, "enroll_host",
        {"host_id": uid(), "certificate_sha256": certificates.first.fingerprint}))
    process, api = None, None
    trace, owned, forbidden = [], [], []
    gate, trace_lock = threading.Event(), threading.Lock()
    offline = False

    with actual_host(hosts, certificates, monitoring=monitoring, resources=ManagedResources(hosts)) as (tls_url, tls_server):
        transport = configuration(tls_url, certificates, certificates.first)
        active = HostChannelClient(transport).activate(pending["challenge"])
        config_path = operator / "managed.json"
        config_path.write_text(json.dumps({"version": 1, "transport": asdict(transport), "tenant_id": tenant.id,
            "project_id": tenant.project, "host_id": active["host_id"], "host_generation": 1,
            "owner_id": tenant.admin.actor_id, "local_owner_id": "local:" + getpass.getuser(),
            "workspace": str(workspace), "policy_revision": 1}), encoding="utf-8")
        for protected in operator.iterdir():
            protected.chmod(0o600)
        forbidden.extend([str(config_path), str(operator), tls_url,
                          Path(certificates.first.private_key_file).read_text(encoding="utf-8"),
                          database["owner_dsn"], database["application_dsn"]])

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
                    self.respond({"models": [{"name": "fixture-managed-native", "model": "fixture-managed-native", "size": 1}]})
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
                    remote = monitoring.inspect(tenant.admin, tenant.id, tenant.project)
                    encoded = json.dumps(remote)
                    with trace_lock:
                        self.respond({"requests": list(trace), "children": children, "host_pid": process.pid,
                            "offline": offline, "remote": remote,
                            "remote_contains_private_content": any(value in encoded or json.dumps(value)[1:-1] in encoded for value in
                                ["PRIVATE_PACKAGED", str(workspace), session.id, *forbidden]),
                            "live_providers_called": False})
                else:
                    self.respond({"error": "Unknown fixture operation"}, status=404)

            def do_POST(self):
                nonlocal offline
                size = int(self.headers.get("Content-Length", "0"))
                if size > 4 * 1024 * 1024:
                    self.respond({"error": "Fixture request too large"}, status=413)
                    return
                value = json.loads(self.rfile.read(size) or b"{}")
                if self.path == "/__fixture__/release":
                    gate.set()
                    self.respond({"released": True})
                elif self.path == "/__fixture__/offline":
                    if not offline:
                        tls_server.shutdown()
                        tls_server.server_close()
                        offline = True
                    self.respond({"offline": True})
                elif self.path == "/api/show":
                    self.respond({"capabilities": ["completion", "tools"], "template": "{{.Tools}}",
                        "model_info": {"general.architecture": "llama", "llama.context_length": 32768}})
                elif self.path == "/api/chat":
                    encoded = json.dumps(value, sort_keys=True)
                    observed = any(row.get("role") == "tool" for row in value.get("messages", []))
                    with trace_lock:
                        trace.append({"model": value.get("model"), "stream": value.get("stream"),
                            "observed_file_result": observed,
                            "input_sha256": hashlib.sha256(encoded.encode()).hexdigest(),
                            "private_configuration_in_model": any(secret in encoded or json.dumps(secret)[1:-1] in encoded for secret in forbidden)})
                    if value.get("model") != "fixture-managed-native" or value.get("stream") is not True:
                        self.respond({"error": "Only declared worker calls are supported"}, status=400)
                        return
                    if not observed and not gate.wait(45):
                        self.respond({"error": "Fixture release not received"}, status=503)
                        return
                    message = ({"role": "assistant", "content": "Packaged managed finding: quoted CSV fields preserve commas. Owner review remains required."}
                        if observed else {"role": "assistant", "content": "", "tool_calls": [
                            {"function": {"name": "file_read", "arguments": {"path": "fact.txt"}}}]})
                    response = [{"model": "fixture-managed-native", "message": message, "done": False},
                        {"model": "fixture-managed-native", "done": True, "done_reason": "stop", "prompt_eval_count": 10, "eval_count": 5}]
                    self.respond("".join(json.dumps(row) + "\n" for row in response), media="application/x-ndjson")
                else:
                    self.respond({"error": "Unknown fixture operation"}, status=404)

        api = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        api.daemon_threads = True
        threading.Thread(target=api.serve_forever, daemon=True).start()
        api_url = f"http://127.0.0.1:{api.server_port}"
        settings = deepcopy(DEFAULTS)
        settings["general"].update(default_backend="ollama", default_model="fixture-managed-native")
        settings["network"].update(ollama_url=api_url, exo_url=api_url, sonn_url="")
        profile = home / ".lumi"
        profile.mkdir(exist_ok=True)
        (profile / "settings.json").write_text(json.dumps(settings), encoding="utf-8")
        manager = ProjectManager(str(workspace))
        manager.set_project(str(workspace))
        session = manager.create_session("ollama", "fixture-managed-native")
        session.title, session.title_source = "Packaged managed fixture", "manual"
        session.append_display_events([{"event": "user_message", "text": "PRIVATE_PACKAGED personal conversation."}])
        session.save()
        environment = dict(os.environ)
        environment.update(OLLAMA_HOST=api_url, EXO_API_URL=api_url, EXO_BASE_URL=api_url, SONN_API_URL="", SONN_BASE_URL="")
        for key in tuple(environment):
            if key.startswith(("SONN_GOVERNANCE_", "LUMI_GOVERNANCE_", "SWARM_", "PG")) or key in {"PYTHONPATH", "PYTHONHOME"}:
                environment.pop(key)
        if source_mode:
            environment["PYTHONPATH"] = str(source)
        with socket.socket() as reservation:
            reservation.bind(("127.0.0.1", 0))
            port = reservation.getsockname()[1]
        url = f"http://127.0.0.1:{port}"
        command = [str(executable), *(["-m", "lumi"] if source_mode else []), "gui", "--browser", "--debug",
                   "--host", "127.0.0.1", "--port", str(port), "--swarm-managed-config", str(config_path)]
        try:
            with (root / "candidate-stream.log").open("ab") as output:
                process = subprocess.Popen(command, cwd=workspace, env=environment, stdin=subprocess.DEVNULL,
                    stdout=output, stderr=output, **background_process_kwargs())
            for _ in range(300):
                if process.poll() is not None:
                    raise RuntimeError(f"Candidate exited during startup: {process.returncode}")
                try:
                    with urllib.request.urlopen(url, timeout=.2) as response:
                        if response.status == 200:
                            # Closing a readiness probe with unread HTML can
                            # provoke a Windows reset in the app's event loop.
                            if len(response.read(1024 * 1024 + 1)) > 1024 * 1024:
                                raise RuntimeError("Candidate startup page exceeds fixture bound")
                            break
                except (OSError, TimeoutError):
                    time.sleep(.1)
            else:
                raise TimeoutError("Candidate startup timed out")
            receipt = {"url": url, "api_url": api_url, "project": str(workspace), "session_id": session.id,
                "executable": str(executable), "host_pid": process.pid, "source_mode": source_mode,
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
