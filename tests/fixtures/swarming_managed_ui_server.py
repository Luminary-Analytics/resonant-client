"""Disposable real GUI + mTLS governance + PostgreSQL browser fixture.

Inference alone is scripted. Requires an explicitly supplied private disposable
database configuration; no default database, user state or external provider.
"""
from __future__ import annotations

from dataclasses import asdict
import getpass
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import threading
import time
from types import SimpleNamespace


def main():
    root = Path(sys.argv[1]).resolve()
    root.mkdir(exist_ok=True)
    database = json.loads(Path(sys.argv[2]).read_text(encoding="utf-8-sig"))
    hold = "--hold" in sys.argv[3:]
    recover = "--recovery" in sys.argv[3:]
    missing_admission = "--missing-admission" in sys.argv[3:]
    writer = "--writer" in sys.argv[3:]
    if os.name == "nt":
        subprocess.run(["icacls", str(root), "/inheritance:r", "/grant:r", f"{getpass.getuser()}:(OI)(CI)(F)",
                        "*S-1-5-18:(OI)(CI)(F)", "*S-1-5-32-544:(OI)(CI)(F)"], check=True, capture_output=True, timeout=10)
    home, workspace, operator = root / "home", root / "project", root / "operator"
    for directory in (home, workspace, operator):
        directory.mkdir()
    (workspace / "fact.txt").write_text("PRIVATE managed browser fact: quoted CSV fields preserve commas.\n", encoding="utf-8")
    for key in ("HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA"):
        os.environ[key] = str(home)
    for key in tuple(os.environ):
        if any(word in key.upper() for word in ("API_KEY", "TOKEN", "SECRET", "PASSWORD")):
            os.environ.pop(key)
    source = Path(__file__).resolve().parents[2]
    sys.path[:0] = [str(source), str(source / "services" / "governance" / "src"), str(source / "services" / "governance" / "tests")]
    os.chdir(workspace)
    connect = socket.socket.connect
    def loopback(sock, address):
        if not isinstance(address, tuple) or address[0] not in {"127.0.0.1", "::1", "localhost"}:
            raise OSError("Fixture prohibits external connections")
        return connect(sock, address)
    socket.socket.connect = loopback

    import uvicorn
    from starlette.responses import JSONResponse
    from starlette.routing import Route
    from lumi.engine import Session
    from lumi.engine.swarming.managed_client import HostChannelClient
    from lumi.engine.swarming.managed_desktop import ManagedDesktop, load_configuration
    from lumi.engine.swarming.service import SwarmRuntime, _READ_TOOLS, _WRITE_TOOLS
    from lumi.gui import app as gui
    from lumi.gui.runtime import BackendSpec
    from lumi.gui.swarming import _capture
    from sonn_governance.hosts import HostGovernance
    from sonn_governance.managed_resources import ManagedResources
    from sonn_governance.models import CommandEnvelope
    from sonn_governance.monitoring import RunMonitoring
    from test_host_http import actual_host, certificates as certificate_fixture
    from test_managed_host_client import configuration
    from test_store import Tenant, uid
    from tests.streaming_stub import StreamingBackend, done, text_delta, tool_call

    if writer:
        from tests.test_swarm_integration import git
        (workspace / "src").mkdir()
        (workspace / "src" / "value.txt").write_text("original\n")
        (workspace / "personal.txt").write_text("committed personal\n")
        (workspace / "verify_value.py").write_text("from pathlib import Path\n"
            "assert Path('src/value.txt').read_text() == 'verified change\\n'\n"
            "print('Exact managed writer value verified')\n")
        git(workspace, "init", "-b", "main")
        git(workspace, "add", ".")
        git(workspace, "commit", "-m", "Managed browser fixture baseline")

    certificates = certificate_fixture.__wrapped__(SimpleNamespace(mktemp=lambda _: operator))
    tenant = Tenant(database)
    tenant.member(tenant.admin, tenant=["membership_admin", "policy_admin", "host_admin", "metadata_read", "control_execute"])
    tenant.store.command(tenant.admin, tenant.command(allowed_models=[{"provider": "ollama", "model": "chosen"}],
        allowed_tools=sorted(_READ_TOOLS | (_WRITE_TOOLS if writer else frozenset())), request_limit=20,
        **({"policy_version": 2, "allowed_effects": ["writer_git", "candidate_git", "candidate_check", "checkout_apply"]} if writer else {})))
    hosts = HostGovernance(tenant.store, minimum_runner_protocol=2)
    monitoring = RunMonitoring(hosts)
    pending = hosts.owner_command(tenant.admin, CommandEnvelope(1, uid(), tenant.id, tenant.project, 0, "enroll_host",
        {"host_id": uid(), "certificate_sha256": certificates.first.fingerprint}))
    gate = threading.Event()
    if not hold:
        gate.set()
    instances, model_inputs = [], []
    class Backend(StreamingBackend):
        def stream(self, **kwargs):
            model_inputs.append(kwargs)
            if not self.stream_count and not gate.wait(45):
                raise TimeoutError("Fixture provider was not released")
            yield from super().stream(**kwargs)
    def factory(spec):
        backend = Backend(name=spec.backend_type, model=spec.model, scripts=[
            [tool_call("file_read", {"path": "fact.txt"}), done(model=spec.model)],
            [text_delta("Managed scripted finding: quoted CSV fields preserve commas. Owner review remains required."), done(model=spec.model)]])
        instances.append(backend)
        return backend

    child_inputs = []
    if writer:
        from lumi.engine.swarming import service as swarm_service
        from lumi.engine.swarming.process_worker import ManagedWorkerProcess
        from lumi.engine.swarming.workers import SwarmWorkerRunner
        child_script = root / "scripted-writer-child.py"
        child_script.write_text("import sys\n" + f"sys.path.insert(0,{str(source)!r})\n"
            "from lumi.engine.swarming.worker_child import main\n"
            "from tests.streaming_stub import StreamingBackend, done, text_delta, tool_call\n"
            "def factory(spec):\n"
            " return StreamingBackend(name=spec.backend_type,model=spec.model,scripts=["
            "[tool_call('file_write',{'path':'src/value.txt','content':'verified change\\n'},'write-1'),done()],"
            "[text_delta('Managed isolated writer result awaits exact owner verification.'),done()]])\n"
            "raise SystemExit(main(backend_factory=factory))\n", encoding="utf-8")
        class Process(ManagedWorkerProcess):
            def run(self, initial, **kwargs):
                child_inputs.append(initial)
                yield from super().run(initial, **kwargs)
        def fixture_runner(*args, **kwargs):
            kwargs["writer_process_factory"] = lambda: Process(command=[sys.executable, str(child_script)], cancel_grace=.1)
            return SwarmWorkerRunner(*args, **kwargs)
        swarm_service.SwarmWorkerRunner = fixture_runner

    with actual_host(hosts, certificates, monitoring=monitoring, resources=ManagedResources(hosts)) as (tls_url, tls_server):
        transport = configuration(tls_url, certificates, certificates.first)
        active = HostChannelClient(transport).activate(pending["challenge"])
        config_path = operator / "managed.json"
        config_path.write_text(json.dumps({"version": 1, "transport": asdict(transport), "tenant_id": tenant.id,
            "project_id": tenant.project, "host_id": active["host_id"], "host_generation": 1, "owner_id": tenant.admin.actor_id,
            "local_owner_id": "local:" + getpass.getuser(), "workspace": str(workspace), "policy_revision": 1}), encoding="utf-8")
        for protected in operator.iterdir():
            protected.chmod(0o600)
        managed = ManagedDesktop(load_configuration(str(config_path)))
        # This computer user accepted Lumi's terms already, as in the app (lumi/terms.py): the checks here
        # are about other things, and the terms dialog would lock the message box first. Workers
        # started from here share this home, so the acceptance counts for them too.
        from lumi import terms as lumi_terms

        lumi_terms.accept({doc.id: doc.version for doc in lumi_terms.required()}, "app")
        state = gui.state
        state.project.set_project(str(workspace))
        state.apply_project_context(str(workspace), refresh_index=True)
        record = state.project.create_session("ollama", "chosen")
        record.title = "Managed fixture conversation"
        record.append_display_events([{"event": "user_message", "text": "PRIVATE saved personal browser conversation."}])
        record.save()
        spec = BackendSpec("ollama", "chosen")
        state.backend_spec, state.backend = spec, StreamingBackend(name="ollama", model="chosen")
        state.session = Session(backend=state.backend, project_instructions="PRIVATE project instruction, local only.")
        state.session.project_path = str(workspace)
        state.available_backends = {"ollama": {"models": ["chosen"]}}
        state.detect_backends = lambda *args, **kwargs: None
        manager = state._swarm_desktop = SwarmRuntime(state.settings, backend_factory=None if writer else factory,
            state_root=lambda _: root / "state", managed_desktop=managed)
        capture = _capture(state, {"project": str(workspace), "session_id": record.id}, manager)
        store = manager._store(capture)
        history = store.create_run(capture.scope, supervisor_id="private-fixture-history", run_id="personal-retained-team",
                                   objective="PRIVATE retained personal objective", request_limit=1)
        store.stop(history, command_id="stop-personal-history")
        if recover:
            from test_managed_restart import _HOST
            from lumi.processes import background_process_kwargs
            host_path, child_path = root / "crashed-host.py", root / "blocked-child.py"
            entered, identity_path = root / "provider-entered", root / "crashed-identity.json"
            host_path.write_text(_HOST.replace("root/'native.sqlite'", "root/'swarm'/'state.sqlite'")
                                 .replace("'saved-session'", repr(record.id)), encoding="utf-8")
            child_path.write_text("import os,sys,time\nfrom pathlib import Path\nsys.path.insert(0,os.getcwd())\n"
                "from lumi.engine.swarming.worker_child import main\n"
                "from tests.streaming_stub import StreamingBackend\n"
                "class Backend(StreamingBackend):\n def stream(self,**kwargs):\n"
                f"  Path({str(entered)!r}).write_text('entered')\n"
                "  while True: time.sleep(.1)\n  yield\n"
                "raise SystemExit(main(backend_factory=lambda spec:Backend(name=spec.backend_type,model=spec.model)))\n", encoding="utf-8")
            launch = root / "crashed-launch.json"
            document = json.loads(config_path.read_text())
            document.pop("version")
            launch.write_text(json.dumps({**document, "mode": "worker_no_commit" if missing_admission else "blocked", "state_root": str(root / "state"),
                "child": str(child_path), "info": str(identity_path)}), encoding="utf-8")
            process = subprocess.Popen([sys.executable, "-I", str(host_path), str(launch)], cwd=source,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **background_process_kwargs())
            try:
                deadline = time.monotonic() + 15
                while not (identity_path.is_file() and process.poll() is not None if missing_admission else entered.is_file()):
                    if process.poll() is not None or time.monotonic() >= deadline:
                        raise RuntimeError("Managed recovery fixture did not reach its provider boundary")
                    time.sleep(.05)
            finally:
                if process.poll() is None:
                    process.kill()
                process.wait(timeout=5)
            identity = json.loads(identity_path.read_text())
            with store._connection(write=True) as connection:
                store._remember(connection, identity["run_id"], "desktop-setup", "fixture-desktop-setup",
                    {"model": {"provider": "ollama", "model": "chosen"}, "worker_requests": 3, "write_roots": []},
                    {"run_id": identity["run_id"]})
            time.sleep(1.1)  # Explicitly wait out the killed fixture's one-second ownership lease.
        offline = False

        async def evidence(request):
            runs = [manager.operate(captured, {"action": "view", "request_id": "fixture-view", "run_id": run_id})
                    for run_id, (captured, _) in tuple(manager._runners.items())]
            remote = monitoring.inspect(tenant.admin, tenant.id, tenant.project)
            encoded_inputs = json.dumps(child_inputs if writer else model_inputs, default=str)
            private_values = [str(config_path), str(operator), certificates.first.private_key_file, tls_url,
                              Path(certificates.first.private_key_file).read_text()]
            effect_rows = []
            if writer:
                import psycopg
                with psycopg.connect(database["owner_dsn"], row_factory=psycopg.rows.dict_row) as connection:
                    effect_rows = [{key: str(value) for key, value in row.items()} for row in connection.execute(
                        "SELECT effect_id,kind,state,semantics_sha256 FROM sonn_governance.owner_effects WHERE tenant_id=%s", (tenant.id,))]
            return JSONResponse({"kind": "source-managed-tls-postgresql-scripted-browser", "live_providers_called": False,
                "backend_instances": len(child_inputs) if writer else len(instances),
                "backend_requests": sum(len(item["run"]["model_requests"]) for item in runs) if writer else sum(item.stream_count for item in instances),
                "provider_entered": bool(model_inputs), "private_configuration_in_model": any(value in encoded_inputs for value in private_values),
                "remote_contains_content": any(value in json.dumps(remote) for value in ["PRIVATE", str(workspace), record.id]),
                "remote": remote, "runs": runs, "offline": offline, "owner_effects": effect_rows,
                "owned_child": writer})

        async def remote_stop(request):
            views = monitoring.inspect(tenant.admin, tenant.id, tenant.project)["runs"]
            view = views[0]
            result = monitoring.request_control(tenant.admin, tenant.id, tenant.project,
                binding_id=view["binding_id"], command_id=uid(), expected_revision=view["revision"],
                expected_epoch=view["projection"]["epoch"], expected_local_revision=view["projection"]["local_revision"], operation="stop")
            return JSONResponse(result)

        async def disconnect(request):
            nonlocal offline
            if not offline:
                import asyncio
                await asyncio.to_thread(tls_server.shutdown)
                tls_server.server_close()
                offline = True
            return JSONResponse({"offline": True})

        async def release(request):
            gate.set()
            return JSONResponse({"released": True})

        async def shutdown(request):
            gate.set()
            server.should_exit = True
            return JSONResponse({"stopping": True})

        async def fixture_launch(request):
            # The app page redeems a one-time launch code for this process's
            # access token (lumi/gui/local_access.py); every page load needs one.
            from lumi.gui.local_access import access
            return JSONResponse({"url": access.launch_url(str(request.base_url))})
        gui.app.routes.append(Route("/__fixture__/launch", fixture_launch))
        gui.app.routes.extend([Route("/__fixture__/evidence", evidence), Route("/__fixture__/remote-stop", remote_stop, methods=["POST"]),
            Route("/__fixture__/offline", disconnect, methods=["POST"]), Route("/__fixture__/release", release, methods=["POST"]),
            Route("/__fixture__/shutdown", shutdown, methods=["POST"])])
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.bind(("127.0.0.1", 0))
        server = uvicorn.Server(uvicorn.Config(gui.app, host="127.0.0.1", log_level="warning"))
        print(json.dumps({"url": f"http://127.0.0.1:{listener.getsockname()[1]}", "session_id": record.id,
                          "workspace": str(workspace), "python": sys.executable}), flush=True)
        server.run(sockets=[listener])


if __name__ == "__main__":
    main()
