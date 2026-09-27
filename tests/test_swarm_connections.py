"""Team workers on a Lumi connection: OpenAI-compatible endpoints such as NVIDIA NIM."""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
import uuid

import pytest

from lumi.engine.swarming import AttemptContext, Command, Scope, SwarmStore
from lumi.engine.swarming.connections import team_connection
from lumi.engine.swarming.models import Conflict, ScopeDenied
from lumi.engine.swarming.policy import ModelSelection, PolicyDenied, PolicyProfile
from lumi.engine.swarming.process_worker import ManagedWorkerProcess
from lumi.engine.swarming.processes import ProcessObservations
from lumi.engine.swarming.service import SwarmRuntime
from lumi.engine.swarming.supervisor import SwarmSupervisor
from lumi.engine.swarming.tools import SWARM_TOOL_NAMES
from lumi.engine.swarming.worker_child import _validate_initial
from lumi.engine.swarming.workers import SwarmWorkerRunner
from lumi.gui.runtime import BackendSpec
from tests.test_swarm_process_workers import child_script, until

KEY = "fixture-connection-key"
HEADER = "fixture-header-credential"


def _connection(**changes):
    return {"id": "nim", "name": "NVIDIA NIM", "type": "openai-compatible",
            "base_url": "https://integrate.api.nvidia.com/v1", "auth": "bearer", **changes}


def test_only_openai_compatible_connections_without_sign_in_reach_team_workers(tmp_path):
    assert team_connection(_connection())["base_url"] == "https://integrate.api.nvidia.com/v1"
    assert team_connection(_connection(auth="header", auth_header="x-api-key"))["auth"] == "header"
    assert team_connection(_connection(auth="none"))["auth"] == "none"
    with pytest.raises(ValueError, match="OpenAI-compatible"):
        team_connection({**_connection(), "type": "anthropic"})
    with pytest.raises(ValueError, match="signs in with oauth"):
        team_connection(_connection(auth="oauth", token_url="https://login.example/token", client_id="lumi"))
    certificate = tmp_path / "client.pem"
    certificate.write_text("fixture", encoding="utf-8")
    with pytest.raises(ValueError, match="client certificate"):
        team_connection(_connection(client_cert=str(certificate)))


def test_policy_admits_connection_names_but_never_cli_loops():
    assert ModelSelection("conn-nim", "moonshotai/kimi-k3").provider == "conn-nim"
    assert PolicyProfile(1, frozenset({"file_read"}), frozenset({"conn-nim", "ollama"})).allowed_providers
    for name in ("codex", "claude-code", "conn-", "conn_nim", "conn-NIM", "anthropic"):
        with pytest.raises(PolicyDenied):
            ModelSelection(name, "model")
        with pytest.raises(ValueError):
            PolicyProfile(1, frozenset({"file_read"}), frozenset({name}))


def _initial(tmp_path, backend, connection):
    return {"backend": backend.to_dict(include_sensitive=True), "workspace": str(tmp_path),
            "conversation_key": "swarm:run:attempt", "prompt": "Inspect", "instructions": "", "role": "",
            "request_limit": 3, "tools": ["file_read"], "write_tools": [], "exclusions": [],
            "connection": connection}


def test_the_child_contract_rebuilds_only_its_matching_connection(tmp_path):
    spec = BackendSpec("conn-nim", "moonshotai/kimi-k3", api_key=KEY)
    checked = _validate_initial(_initial(tmp_path, spec, _connection()))["connection"]
    assert (checked["id"], checked["type"]) == ("nim", "openai-compatible")
    assert _validate_initial(_initial(tmp_path, BackendSpec("ollama", "chosen"), None))["connection"] is None
    for backend, connection, reason in (
        (BackendSpec("ollama", "chosen"), _connection(), "native provider takes no connection"),
        (spec, None, "captured connection"),
        (spec, _connection(id="other"), "differs from the captured provider"),
        (spec, {**_connection(), "type": "anthropic"}, "OpenAI-compatible"),
        (BackendSpec("codex", "gpt"), None, "native provider or connection"),
    ):
        with pytest.raises(ValueError, match=reason):
            _validate_initial(_initial(tmp_path, backend, connection))
    missing = _initial(tmp_path, spec, _connection())
    del missing["connection"]
    with pytest.raises(ValueError, match="initialization fields"):
        _validate_initial(missing)


def test_the_runtime_captures_the_connection_behind_a_team_model():
    class _Settings(dict):
        pass

    settings = _Settings(connections=[_connection(), _connection(id="claude", name="Claude", type="anthropic",
                                                                base_url="https://api.anthropic.com")])
    runtime = SwarmRuntime(settings, backend_factory=lambda spec: None)
    assert runtime.team_model(BackendSpec("ollama", "chosen")) == {}
    captured = runtime.team_model(BackendSpec("conn-nim", "moonshotai/kimi-k3"))
    assert list(captured) == ["conn-nim"] and captured["conn-nim"]["base_url"].endswith("/v1")
    for spec, reason in ((BackendSpec("conn-claude", "claude"), "isn't an OpenAI-compatible connection"),
                         (BackendSpec("conn-gone", "model"), "connection was removed"),
                         (BackendSpec("codex", "gpt"), "OpenAI-compatible connection"),
                         (BackendSpec("conn-nim", ""), "and a model")):
        with pytest.raises(Conflict, match=reason):
            runtime.team_model(spec)
    assert "connection was removed" in runtime._team_unavailable(BackendSpec("conn-gone", "model"))


# ── A real child worker on a loopback Chat Completions endpoint ──────────────


def _command(supervisor, authority, kind, payload=None):
    revision = supervisor.store.snapshot(authority.scope, authority.run_id)["run"]["revision"]
    return supervisor.handle(Command(uuid.uuid4().hex, authority.run_id, revision,
                                     authority.epoch, kind, payload or {}), authority)


@pytest.fixture
def team(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "fact.txt").write_text("Actual isolated fact", encoding="utf-8")
    supervisor = SwarmSupervisor(SwarmStore(tmp_path / "runtime" / "state.sqlite"))
    tools = frozenset({"file_read", "glob", "grep", "artifact_read"}) | SWARM_TOOL_NAMES
    authority = supervisor.create(Scope.personal("owner", "project", "session"),
        supervisor_id="supervisor", objective="Inspect isolated facts", request_limit=20,
        policy=PolicyProfile(1, tools, frozenset({"conn-fixture"})), lease_seconds=300)
    _command(supervisor, authority, "plan", {"work_items": [
        {"id": "first", "objective": "Inspect first facts", "read_roots": ["."], "write_roots": [],
         "tools": sorted(tools), "criteria": ["fact"]}]})
    result = _command(supervisor, authority, "assign", {"work_item_id": "first", "worker_id": "first",
        "requests": 3, "model": {"provider": "conn-fixture", "model": "fixture/model"}}).result
    context = AttemptContext(authority.scope, authority.run_id, result["attempt_id"], "first", authority.epoch)
    return supervisor, authority, workspace, context


class _ChatCompletions(BaseHTTPRequestHandler):
    """Scripted SSE replies: a file_read call, then a final answer."""

    requests: list = []

    def do_POST(self):  # noqa: N802 - http.server naming
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        type(self).requests.append({"path": self.path, "authorization": self.headers.get("Authorization"),
                                    "extra": self.headers.get("X-Fixture"), "body": body})
        if len(type(self).requests) == 1:
            chunks = [{"choices": [{"index": 0, "delta": {"role": "assistant", "reasoning_content": "Read it."}}]},
                      {"choices": [{"index": 0, "delta": {"tool_calls": [{"index": 0, "id": "file_read:0",
                          "type": "function", "function": {"name": "file_read",
                                                           "arguments": json.dumps({"path": "fact.txt"})}}]}}]},
                      {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]}]
        else:
            chunks = [{"choices": [{"index": 0, "delta": {"content": "The fact file is read."}}]},
                      {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}]
        chunks.append({"choices": [], "usage": {"prompt_tokens": 50, "completion_tokens": 7, "total_tokens": 57}})
        payload = "".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks) + "data: [DONE]\n\n"
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(payload.encode())))
        self.end_headers()
        self.wfile.write(payload.encode())

    def log_message(self, *_):
        pass


@pytest.fixture
def endpoint():
    _ChatCompletions.requests = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _ChatCompletions)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/v1"
    finally:
        server.shutdown()
        server.server_close()


def test_a_child_worker_runs_on_its_captured_connection(team, endpoint, tmp_path):
    supervisor, authority, workspace, context = team
    connection = {"id": "fixture", "name": "Fixture gateway", "type": "openai-compatible",
                  "base_url": endpoint, "auth": "bearer", "headers": {"X-Fixture": HEADER}}
    # The real child entry point: no injected backend, so it builds the
    # connection's adapter from the captured contract.
    command = child_script(tmp_path, "from lumi.engine.swarming.worker_child import main\nraise SystemExit(main())\n")
    runner = SwarmWorkerRunner(supervisor, authority, workspace, managed_readers=True,
        backend_factory=lambda _: pytest.fail("A managed reader builds its backend in its own process"),
        writer_process_factory=lambda: ManagedWorkerProcess(command=command),
        process_observations=ProcessObservations(supervisor.store, host_id="fixture-host"),
        connections={"conn-fixture": connection})
    try:
        runner.start(context, BackendSpec("conn-fixture", "fixture/model", api_key=KEY))
        until(lambda: not runner.inspect(context.attempt_id)["alive"], timeout=30)
        events = runner.poll()["events"]
        text = json.dumps(events, default=str)
        assert "Actual isolated fact" in text and "The fact file is read." in text
        assert KEY not in text and HEADER not in text
    finally:
        runner.close()
    requests = _ChatCompletions.requests
    assert [request["path"] for request in requests] == ["/v1/chat/completions"] * 2
    assert {request["authorization"] for request in requests} == {f"Bearer {KEY}"}
    assert {request["extra"] for request in requests} == {HEADER}
    assert all(request["body"]["model"] == "fixture/model" and request["body"]["stream"] for request in requests)
    snapshot = supervisor.store.snapshot(authority.scope, authority.run_id)
    assert [row["state"] for row in snapshot["model_requests"]] == ["completed", "completed"]


def test_a_connection_worker_needs_the_connection_its_run_captured(team, tmp_path):
    supervisor, authority, workspace, context = team
    with pytest.raises(ScopeDenied, match="must match its backend name"):
        SwarmWorkerRunner(supervisor, authority, workspace, backend_factory=lambda _: None,
                          connections={"conn-other": {**_connection(), "id": "fixture"}})
    runner = SwarmWorkerRunner(supervisor, authority, workspace, backend_factory=lambda _: None)
    try:
        with pytest.raises(ScopeDenied, match="captured no connection"):
            runner.start(context, BackendSpec("conn-fixture", "fixture/model", api_key=KEY))
    finally:
        runner.close()


def test_a_worker_process_never_moves_the_users_state_folder(tmp_path):
    # Every Team participant starts `python -m lumi --swarm-worker`. Only the
    # app itself may move ~/.resonant to ~/.lumi; a worker must not.
    import os
    import subprocess

    from lumi.engine.swarming.process_worker import _source_command

    home = tmp_path / "home"
    (home / ".resonant").mkdir(parents=True)
    (home / ".resonant" / "settings.json").write_text("{}", encoding="utf-8")
    env = {**os.environ, "HOME": str(home), "USERPROFILE": str(home), "LUMI_KEYCHAIN": "off"}
    env.pop("LUMI_STATE_HOME", None)
    env.pop("RESONANT_STATE_HOME", None)
    for entrypoint in ("--swarm-worker", "--swarm-effect"):
        # No host frames arrive, so the child refuses to initialize and exits.
        subprocess.run(_source_command(entrypoint), stdin=subprocess.DEVNULL, capture_output=True,
                       env=env, timeout=120, check=False)
        assert (home / ".resonant" / "settings.json").is_file(), entrypoint
        assert not (home / ".lumi").exists(), entrypoint
