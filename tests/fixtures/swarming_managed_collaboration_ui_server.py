"""Two isolated source GUIs, two member certificates, one real TLS/PG authority.

Only inference is scripted. The receiver's actual owned child waits for an
explicit fixture release, so origin Stop can be tested during receiver work.
"""
from __future__ import annotations

from dataclasses import asdict
import getpass
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import threading
import time
from types import SimpleNamespace
from urllib.request import Request, urlopen

SOURCE = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(SOURCE), str(SOURCE / "services/governance/src"), str(SOURCE / "services/governance/tests")]


def isolated_network():
    original = socket.socket.connect

    def connect(sock, address):
        if not isinstance(address, tuple) or address[0] not in {"127.0.0.1", "::1", "localhost"}:
            raise OSError("Fixture prohibits external connections")
        return original(sock, address)
    socket.socket.connect = connect


def client_main(root, config_path):
    home, workspace = root / "home", root / "project"
    home.mkdir()
    for key in ("HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA"):
        os.environ[key] = str(home)
    for key in tuple(os.environ):
        if any(word in key.upper() for word in ("API_KEY", "TOKEN", "SECRET", "PASSWORD")):
            os.environ.pop(key)
    os.chdir(workspace)
    isolated_network()
    import uvicorn
    from starlette.responses import JSONResponse
    from starlette.routing import Route
    from lumi.engine import Session
    from lumi.engine.swarming import service, collaboration_desktop
    from lumi.engine.swarming.managed_desktop import ManagedDesktop, load_configuration
    from lumi.engine.swarming.process_worker import ManagedWorkerProcess
    from lumi.engine.swarming.workers import SwarmWorkerRunner
    from lumi.gui import app as gui
    from lumi.gui.runtime import BackendSpec
    from tests.streaming_stub import StreamingBackend

    entered, release = root / "entered", root / "release"
    child = root / "scripted-child.py"
    child.write_text("import sys,time\nfrom pathlib import Path\n" + f"sys.path.insert(0,{str(SOURCE)!r})\n"
        "from lumi.engine.swarming.worker_child import main\n"
        "from tests.streaming_stub import StreamingBackend,done,text_delta,tool_call\n"
        "class Backend(StreamingBackend):\n def stream(self,**kwargs):\n"
        f"  Path({str(entered)!r}).write_text('entered')\n"
        "  deadline=time.monotonic()+90\n"
        f"  while not Path({str(release)!r}).exists():\n"
        "   if time.monotonic()>deadline: raise TimeoutError('Fixture receiver was not released')\n"
        "   time.sleep(.05)\n"
        "  yield from super().stream(**kwargs)\n"
        "def factory(spec):\n return Backend(name=spec.backend_type,model=spec.model,scripts=["
        "[tool_call('file_read',{'path':'fact.txt'},'read-fact'),done()],"
        "[text_delta('Receiver-owned fact inspected. Separate owner review required.'),done()]])\n"
        "raise SystemExit(main(backend_factory=factory))\n", encoding="utf-8")
    initial = []

    class Process(ManagedWorkerProcess):
        def run(self, document, **kwargs):
            initial.append(document)
            yield from super().run(document, **kwargs)

    def runner(*args, **kwargs):
        kwargs["writer_process_factory"] = lambda: Process(command=[sys.executable, str(child)], cancel_grace=.1)
        return SwarmWorkerRunner(*args, **kwargs)

    service.SwarmWorkerRunner = collaboration_desktop.SwarmWorkerRunner = runner
    state = gui.state
    state.project.set_project(str(workspace))
    state.apply_project_context(str(workspace), refresh_index=True)
    record = state.project.create_session("ollama", "chosen")
    record.title = "Independent managed conversation " + root.name
    record.append_display_events([{"event": "user_message", "text": "PRIVATE saved conversation " + root.name}])
    record.save()
    state.backend_spec = BackendSpec("ollama", "chosen")
    state.backend = StreamingBackend(name="ollama", model="chosen")
    state.session = Session(backend=state.backend, project_instructions="PRIVATE project instructions " + root.name)
    state.session.project_path = str(workspace)
    state.available_backends = {"ollama": {"models": ["chosen"]}}
    state.detect_backends = lambda *args, **kwargs: None
    managed = ManagedDesktop(load_configuration(str(config_path)))
    manager = state._swarm_desktop = service.SwarmRuntime(state.settings,
        state_root=lambda _: root / "state", managed_desktop=managed)

    async def evidence(request):
        views = [manager.operate(capture, {"action": "view", "request_id": "fixture-view", "run_id": run_id})
                 for run_id, (capture, _) in tuple(manager._runners.items())]
        encoded = json.dumps(initial, default=str)
        config = json.loads(config_path.read_text())
        return JSONResponse({"views": views, "owned_children": len(initial), "provider_entered": entered.exists(),
            "private_configuration_in_model": any(value in encoded for value in (str(config_path), config["transport"]["private_key_file"],
                config["host_id"], config["transport"]["endpoint"])),
            "peer_proposal_in_model": "Selected peer proposal: inspect only your own fact" in encoded, "live_providers_called": False})

    async def release_worker(request):
        release.write_text("released")
        return JSONResponse({"released": True})

    async def shutdown(request):
        release.write_text("released")
        manager.close()
        server.should_exit = True
        return JSONResponse({"stopping": True})

    gui.app.routes.extend([Route("/__fixture__/evidence", evidence),
        Route("/__fixture__/release", release_worker, methods=["POST"]), Route("/__fixture__/shutdown", shutdown, methods=["POST"])])
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    server = uvicorn.Server(uvicorn.Config(gui.app, host="127.0.0.1", log_level="warning"))
    print(json.dumps({"url": f"http://127.0.0.1:{listener.getsockname()[1]}", "session_id": record.id, "workspace": str(workspace)}), flush=True)
    server.run(sockets=[listener])


def main(root, database_path, executable=None):
    root.mkdir(exist_ok=True)
    if os.name == "nt":
        subprocess.run(["icacls", str(root), "/inheritance:r", "/grant:r", f"{getpass.getuser()}:(OI)(CI)(F)",
                        "*S-1-5-18:(OI)(CI)(F)", "*S-1-5-32-544:(OI)(CI)(F)"], check=True, capture_output=True, timeout=10)
    isolated_network()
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    import psycopg
    from lumi.engine.swarming.managed_client import HostChannelClient
    from lumi.engine.swarming.service import _READ_TOOLS
    from lumi.processes import background_process_kwargs
    from sonn_governance.content import ContentKeys, ContentStore
    from sonn_governance.hosts import HostGovernance
    from sonn_governance.managed_collaboration import ManagedCollaboration
    from sonn_governance.managed_resources import ManagedResources
    from sonn_governance.models import CommandEnvelope, Principal
    from sonn_governance.monitoring import RunMonitoring
    from sonn_governance.sharing_retention import SharingRetention
    from test_host_http import certificates as certificate_fixture
    from test_managed_collaboration import policy
    from test_managed_collaboration_native import actual_sharing_host
    from test_managed_host_client import configuration
    from test_store import Tenant, uid

    database = json.loads(database_path.read_text(encoding="utf-8-sig"))
    operator = root / "operator"
    operator.mkdir()
    certificates = certificate_fixture.__wrapped__(SimpleNamespace(mktemp=lambda _: operator))
    tenant = Tenant(database)
    owners = [Principal("https://fixture-issuer.example", uid(), time.time() + 3600) for _ in range(2)]
    for owner in owners:
        tenant.member(owner, projects={tenant.project: ["host_admin", "content_read", "content_write", "metadata_read", "control_execute"]})
    custodian = Principal("https://fixture-retention.example", uid(), time.time() + 3600)
    tenant.member(custodian, projects={tenant.project: ["retention_admin"]})
    tenant.store.command(tenant.admin, tenant.command(allowed_models=[{"provider": "ollama", "model": "chosen"}],
        allowed_tools=sorted(_READ_TOOLS), content_mode="explicit", request_limit=20))
    hosts = HostGovernance(tenant.store, minimum_runner_protocol=2)
    monitoring = RunMonitoring(hosts)
    sharing = ManagedCollaboration(hosts, ContentKeys({"fixture": b"p" * 32}, "fixture"))
    sharing.set_policy(tenant.admin, tenant_id=tenant.id, project_id=tenant.project, command_id=uid(), expected_revision=0,
                       policy=policy(max_total_bytes=65536))
    artifact_id, artifact_bytes = uid(), b"Explicit owner-authored fixture evidence"
    ContentStore(tenant.store, ContentKeys({"fixture": b"p" * 32}, "fixture")).publish(owners[0], tenant.id, tenant.project,
        artifact_id, command_id=uid(), content=artifact_bytes, media_type="text/plain", retention_seconds=3600)
    artifact = {"artifact_id": artifact_id, "sha256": hashlib.sha256(artifact_bytes).hexdigest(), "byte_size": len(artifact_bytes)}
    processes, infos, logs, frozen_clients = [], [], [], []

    class Controller(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            if self.path != "/__fixture__/delete-sharing":
                self.send_error(404)
                return
            value = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
            result = SharingRetention(tenant.store).retain(custodian, tenant.id, value["kind"], value["resource_id"],
                command_id=uid(), expected_revision=1, operation="delete_sharing_content")
            data = json.dumps(result).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if self.path != "/__fixture__/evidence":
                self.send_error(404)
                return
            with psycopg.connect(database["owner_dsn"], row_factory=psycopg.rows.dict_row) as connection:
                counts = {name: connection.execute(f"SELECT count(*) AS count FROM sonn_governance.{name} WHERE tenant_id=%s", (tenant.id,)).fetchone()["count"]
                          for name in ("sharing_grants", "sharing_messages", "sharing_acceptances", "worker_slots", "host_requests")}
                grants = [dict(row) for row in connection.execute("SELECT grant_id,approved_at,revoked_at FROM sonn_governance.sharing_grants WHERE tenant_id=%s", (tenant.id,))]
            data = json.dumps({"counts": counts, "grants": grants, "owners_distinct": owners[0].actor_id != owners[1].actor_id,
                               "gui_pids": [process.pid for process in processes], "live_providers_called": False}, default=str).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    controller = ThreadingHTTPServer(("127.0.0.1", 0), Controller)
    controller_thread = threading.Thread(target=controller.serve_forever, daemon=True)
    controller_thread.start()
    try:
        with actual_sharing_host(hosts, certificates, sharing, monitoring, ManagedResources(hosts)) as (url, _):
            for index, (owner, certificate) in enumerate(zip(owners, (certificates.first, certificates.second))):
                directory = root / f"owner-{index}"
                directory.mkdir()
                workspace = directory / "project"
                workspace.mkdir()
                (workspace / "fact.txt").write_text(f"PRIVATE receiver-owned fact {index}\n")
                transport = configuration(url, certificates, certificate)
                pending = hosts.owner_command(owner, CommandEnvelope(1, uid(), tenant.id, tenant.project, 0, "enroll_host",
                    {"host_id": uid(), "certificate_sha256": certificate.fingerprint}))
                active = HostChannelClient(transport).activate(pending["challenge"])
                config = operator / f"managed-{index}.json"
                config.write_text(json.dumps({"version": 1, "transport": asdict(transport), "tenant_id": tenant.id,
                    "project_id": tenant.project, "host_id": active["host_id"], "host_generation": active["host_generation"],
                    "owner_id": owner.actor_id, "local_owner_id": "local:" + getpass.getuser(), "workspace": str(workspace), "policy_revision": 1}))
                for protected in operator.iterdir():
                    protected.chmod(0o600)
                if executable is not None:
                    from swarming_frozen_managed_client import FrozenManagedClient
                    client = FrozenManagedClient(directory, config, executable)
                    frozen_clients.append(client)
                    processes.append(client.process)
                    infos.append(client.info)
                    continue
                log = (directory / "server.log").open("w", encoding="utf-8")
                logs.append(log)
                process = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "--client", str(directory), str(config)],
                    # This GUI has no operator stdin. Inheriting the parent's
                    # live control pipe can leave Windows shell probes waiting.
                    cwd=SOURCE, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=log, text=True, **background_process_kwargs())
                processes.append(process)
                lines = []
                thread = threading.Thread(target=lambda pipe=process.stdout, target=lines: target.extend(iter(pipe.readline, "")), daemon=True)
                thread.start()
                deadline = time.monotonic() + 30
                while not any(line.startswith('{"url":') for line in lines):
                    if process.poll() is not None or time.monotonic() >= deadline:
                        raise RuntimeError(f"Isolated GUI {index} did not start; inspect {directory / 'server.log'}")
                    time.sleep(.05)
                infos.append(json.loads(next(line for line in lines if line.startswith('{"url":'))))
            print(json.dumps({"clients": infos, "control": f"http://127.0.0.1:{controller.server_address[1]}", "artifact": artifact}), flush=True)
            sys.stdin.read()
    finally:
        for info in infos:
            if info.get("frozen"):
                continue
            try:
                with urlopen(Request(info["url"] + "/__fixture__/shutdown", method="POST"), timeout=4):
                    pass
            except OSError:
                pass
        for client in frozen_clients:
            client.close()
        for process in processes:
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        controller.shutdown()
        controller.server_close()
        controller_thread.join(timeout=2)
        for log in logs:
            log.close()


if __name__ == "__main__":
    if sys.argv[1] == "--client":
        client_main(Path(sys.argv[2]).resolve(), Path(sys.argv[3]).resolve())
    else:
        candidate = Path(sys.argv[4]).resolve(strict=True) if len(sys.argv) == 5 and sys.argv[3] == "--candidate" else None
        main(Path(sys.argv[1]).resolve(), Path(sys.argv[2]).resolve(), candidate)
