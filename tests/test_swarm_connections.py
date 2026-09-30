"""Team participants on a Lumi connection: NVIDIA NIM, OpenAI, Azure OpenAI, Anthropic, Claude on Bedrock."""

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


AZURE = {"id": "azure", "name": "Azure OpenAI", "type": "azure-openai", "base_url": "https://acme.openai.azure.com/openai/v1",
         "models": ["gpt-5-deployment"]}
BEDROCK = {"id": "bedrock", "name": "Claude on Bedrock", "type": "anthropic-bedrock", "region": "us-east-1",
           "models": ["us.anthropic.claude-sonnet-5-v1:0"], "auth": "bearer"}
VERTEX = {"id": "vertex", "name": "Claude on Vertex", "type": "anthropic-vertex", "region": "us-east5",
          "project": "acme-ai", "models": ["claude-sonnet-5@20260901"]}


def test_connections_whose_adapter_keeps_the_contract_and_use_a_key_reach_team_participants(tmp_path):
    assert team_connection(_connection())["base_url"] == "https://integrate.api.nvidia.com/v1"
    assert team_connection(_connection(auth="header", auth_header="x-api-key"))["auth"] == "header"
    assert team_connection(_connection(auth="none"))["auth"] == "none"
    # The Responses and Messages adapters keep the supervised request contract too.
    assert team_connection({**_connection(), "type": "anthropic"})["type"] == "anthropic"
    assert team_connection({**_connection(), "type": "openai", "base_url": ""})["type"] == "openai"
    assert team_connection(AZURE)["auth"] == "header"
    assert team_connection(BEDROCK)["type"] == "anthropic-bedrock"
    # A participant uses a key, never the person's sign-in.
    with pytest.raises(ValueError, match="signs in with oauth"):
        team_connection(_connection(auth="oauth", token_url="https://login.example/token", client_id="lumi"))
    with pytest.raises(ValueError, match="signs in with entra"):
        team_connection({**AZURE, "auth": "entra"})
    with pytest.raises(ValueError, match="AWS credentials.*Bedrock API key"):
        team_connection({**BEDROCK, "auth": "aws"})
    with pytest.raises(ValueError, match="Vertex AI with your Google account"):
        team_connection(VERTEX)
    certificate = tmp_path / "client.pem"
    certificate.write_text("fixture", encoding="utf-8")
    with pytest.raises(ValueError, match="client certificate"):
        team_connection(_connection(client_cert=str(certificate)))
    # A capability pack's provider runs its own process.
    with pytest.raises(ValueError, match="capability pack's provider"):
        team_connection({"id": "pack", "name": "Pack model", "type": "extension", "pack": "acme.models",
                         "provider": "local", "auth": "none"})


def test_policy_admits_api_providers_and_connection_names_but_never_cli_loops():
    assert ModelSelection("conn-nim", "moonshotai/kimi-k3").provider == "conn-nim"
    assert PolicyProfile(1, frozenset({"file_read"}), frozenset({"conn-nim", "ollama"})).allowed_providers
    for name in ("anthropic", "openai"):
        assert ModelSelection(name, "model").provider == name
        assert PolicyProfile(1, frozenset({"file_read"}), frozenset({name})).allowed_providers == {name}
    for name in ("codex", "claude-code", "conn-", "conn_nim", "conn-NIM"):
        with pytest.raises(PolicyDenied):
            ModelSelection(name, "model")
        with pytest.raises(ValueError):
            PolicyProfile(1, frozenset({"file_read"}), frozenset({name}))


def test_team_providers_come_from_the_adapters_capability():
    from lumi.backends import native_backend_class
    from lumi.engine.swarming.policy import NATIVE_PROVIDERS

    assert NATIVE_PROVIDERS == {"anthropic", "exo", "kimi", "ollama", "openai", "openrouter", "sonn"}
    assert all(native_backend_class(name).supervised_requests is True for name in NATIVE_PROVIDERS)
    # The CLI adapters run their own tool loops and say so.
    assert native_backend_class("codex").supervised_requests is False
    assert native_backend_class("claude-code").supervised_requests is False


def _initial(tmp_path, backend, connection):
    return {"backend": backend.to_dict(include_sensitive=True), "workspace": str(tmp_path),
            "conversation_key": "swarm:run:attempt", "prompt": "Inspect", "instructions": "", "role": "",
            "request_limit": 3, "tools": ["file_read"], "write_tools": [], "exclusions": [],
            "connection": connection, "secret_scan": False}


def test_the_child_contract_rebuilds_only_its_matching_connection(tmp_path):
    spec = BackendSpec("conn-nim", "moonshotai/kimi-k3", api_key=KEY)
    checked = _validate_initial(_initial(tmp_path, spec, _connection()))["connection"]
    assert (checked["id"], checked["type"]) == ("nim", "openai-compatible")
    assert _validate_initial(_initial(tmp_path, BackendSpec("ollama", "chosen"), None))["connection"] is None
    for native in ("anthropic", "openai"):
        assert _validate_initial(_initial(tmp_path, BackendSpec(native, "chosen", api_key=KEY), None))["connection"] is None
    claude = _validate_initial(_initial(tmp_path, BackendSpec("conn-nim", "claude-sonnet-5", api_key=KEY),
                                        {**_connection(), "type": "anthropic"}))["connection"]
    assert claude["type"] == "anthropic"
    for backend, connection, reason in (
        (BackendSpec("ollama", "chosen"), _connection(), "native provider takes no connection"),
        (spec, None, "captured connection"),
        (spec, _connection(id="other"), "differs from the captured provider"),
        (BackendSpec("conn-vertex", "claude"), VERTEX, "Google account"),
        (BackendSpec("codex", "gpt"), None, "native provider or connection"),
        (BackendSpec("claude-code", "sonnet"), None, "native provider or connection"),
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
                                                                base_url="https://api.anthropic.com"),
                                      AZURE, BEDROCK, VERTEX])
    runtime = SwarmRuntime(settings, backend_factory=lambda spec: None)
    for native in ("ollama", "anthropic", "openai"):
        assert runtime.team_model(BackendSpec(native, "chosen")) == {}
    captured = runtime.team_model(BackendSpec("conn-nim", "moonshotai/kimi-k3"))
    assert list(captured) == ["conn-nim"] and captured["conn-nim"]["base_url"].endswith("/v1")
    assert runtime.team_model(BackendSpec("conn-claude", "claude-sonnet-5"))["conn-claude"]["type"] == "anthropic"
    assert runtime.team_model(BackendSpec("conn-azure", "gpt-5-deployment"))["conn-azure"]["type"] == "azure-openai"
    for spec, reason in ((BackendSpec("conn-vertex", "claude"), "Google account.*Team runs on Anthropic"),
                         (BackendSpec("conn-gone", "model"), "connection was removed"),
                         (BackendSpec("codex", "gpt"), "Team can't run on Codex: Codex runs its own tool loop"),
                         (BackendSpec("claude-code", "sonnet"), "Team can't run on Claude Code"),
                         (BackendSpec("conn-nim", ""), "Choose a model")):
        with pytest.raises(Conflict, match=reason):
            runtime.team_model(spec)
    assert "connection was removed" in runtime._team_unavailable(BackendSpec("conn-gone", "model"))
    # The panel names a connection by its own name, whether a team can use it or not.
    assert [runtime._provider_label(name) for name in ("conn-vertex", "conn-claude", "conn-gone", "anthropic")] == [
        "Claude on Vertex", "Claude", "conn-gone", "anthropic"]
    # The refusal says which connections work, and what to do.
    refusal = runtime._team_unavailable(BackendSpec("codex", "gpt-5-codex"))
    for supported in ("Anthropic, OpenAI, OpenRouter, Ollama, EXO, Kimi and SONN", "OpenAI-compatible (such as NVIDIA NIM)",
                      "Azure OpenAI", "Claude on Bedrock with a Bedrock API key", "Switch this conversation"):
        assert supported in refusal
    # The panel offers workers' models from these only (swarm_view.js). Claude
    # on Bedrock needs a Bedrock API key to send (next test).
    from lumi.engine.swarming.connections import team_providers
    offered = {"anthropic", "exo", "kimi", "ollama", "openai", "openrouter", "sonn", "conn-nim", "conn-claude",
               "conn-azure"}
    assert team_providers(settings) == sorted(offered)


def test_claude_on_bedrock_needs_its_bedrock_api_key_for_a_team(monkeypatch):
    from lumi.engine.swarming.connections import key_refusal, participant_key, participant_refusal, team_connection as checked
    from lumi.anthropic_api import AnthropicBackend

    bedrock = checked(BEDROCK)
    monkeypatch.delenv("AWS_BEARER_TOKEN_BEDROCK", raising=False)
    assert "Bedrock API key" in key_refusal(bedrock, "")
    assert key_refusal(bedrock, "bedrock-api-key") == "" and key_refusal(checked(_connection()), "") == ""
    monkeypatch.setenv("AWS_BEARER_TOKEN_BEDROCK", "environment-bedrock-key")
    assert key_refusal(bedrock, "") == ""
    # The app reads it for the participant, whose process gets no credentials in its environment.
    assert participant_key(bedrock, "") == "environment-bedrock-key"
    assert participant_key(bedrock, "bedrock-api-key") == "bedrock-api-key"
    assert participant_key(checked(_connection()), "") == "" and participant_key(None, "") == ""
    monkeypatch.delenv("AWS_BEARER_TOKEN_BEDROCK")
    # A participant's backend that would sign in (the AWS chain, Google, OAuth) is refused where it's built.
    keyless = AnthropicBackend("", "m", platform="bedrock", region="us-east-1")
    assert keyless.uses_sign_in and "never a sign-in" in participant_refusal(keyless)
    assert participant_refusal(AnthropicBackend("bedrock-api-key", "m", platform="bedrock", region="us-east-1")) == ""
    assert participant_refusal(AnthropicBackend("", "m", platform="vertex", region="us-east5", project="p"))
    assert participant_refusal(AnthropicBackend("sk-ant-fixture", "claude-sonnet-5")) == ""


def test_an_adapter_that_does_not_declare_the_supervised_contract_is_refused():
    # A wrapper (lumi/smoke/flaky.py) or a new adapter that says nothing about
    # the contract may retry inside one counted request: it can't take part.
    from lumi.engine.swarming.connections import participant_refusal

    class Undeclared:
        handles_tools = False

    class Declared(Undeclared):
        supervised_requests = True

    assert "tool calls Lumi runs" in participant_refusal(Undeclared())
    assert participant_refusal(Declared()) == ""
    assert participant_refusal(type("Refusing", (Declared,), {"supervised_requests": False})())


def test_the_worker_model_list_offers_claude_on_bedrock_only_with_its_key(tmp_path, monkeypatch):
    from lumi.engine.swarming.connections import team_providers
    from lumi.gui.settings import SettingsManager

    monkeypatch.delenv("AWS_BEARER_TOKEN_BEDROCK", raising=False)
    settings = SettingsManager(tmp_path / "settings.json")
    settings.set("connections", None, [BEDROCK, {**BEDROCK, "id": "bedrock-aws", "auth": "aws"}])
    assert not {"conn-bedrock", "conn-bedrock-aws"} & set(team_providers(settings))
    # AWS_BEARER_TOKEN_BEDROCK is a key for the connection that uses a Bedrock API key, never for AWS sign-in.
    monkeypatch.setenv("AWS_BEARER_TOKEN_BEDROCK", "environment-bedrock-key")
    assert {"conn-bedrock", "conn-bedrock-aws"} & set(team_providers(settings)) == {"conn-bedrock"}
    monkeypatch.delenv("AWS_BEARER_TOKEN_BEDROCK")
    settings.set("api_keys", "conn_bedrock", "saved-bedrock-key")
    assert {"conn-bedrock", "conn-bedrock-aws"} & set(team_providers(settings)) == {"conn-bedrock"}


def _keyless_bedrock_runtime(tmp_path, monkeypatch):
    from lumi.engine.swarming.service import CapturedSession
    from lumi.gui.settings import SettingsManager

    monkeypatch.delenv("AWS_BEARER_TOKEN_BEDROCK", raising=False)
    settings = SettingsManager(tmp_path / "settings.json")
    settings.set("connections", None, [BEDROCK])
    settings.set("swarming", None, {"version": 1, "enabled": True})
    project = tmp_path / "project"
    project.mkdir()
    (project / "fact.txt").write_text("fact\n", encoding="utf-8")
    runtime = SwarmRuntime(settings, state_root=lambda _: tmp_path / "state")
    capture = CapturedSession(Scope.personal("owner", "project", "session"), str(project),
                              BackendSpec("conn-bedrock", "us.anthropic.claude-sonnet-5-v1:0",
                                          api_key_source="settings", api_key_setting="conn_bedrock"))
    return runtime, capture


def test_a_shared_work_team_on_claude_on_bedrock_without_its_key_is_refused_before_it_exists(tmp_path, monkeypatch):
    # Preparing a collaboration team starts no model, but work it accepts
    # later would: without the key it could only run as a failed worker.
    runtime, capture = _keyless_bedrock_runtime(tmp_path, monkeypatch)
    try:
        with pytest.raises(Conflict, match="Bedrock API key.*never your AWS sign-in"):
            runtime.operate(capture, {"action": "collaboration_prepare", "request_id": "prepare",
                                      "objective": "Receive shared work", "request_limit": 4})
        assert not runtime.busy and not runtime._runners and not runtime._active
        assert runtime.operate(capture, {"request_id": "view"})["run"] is None
    finally:
        runtime.close()


def test_continuing_a_managed_team_checks_its_keys_before_a_new_epoch_attaches(tmp_path, monkeypatch):
    # Continuing starts participants. A managed team attaches a new epoch to
    # its organization's control plane (ManagedRecovery.prepare_resume), so the
    # keys are checked first, where Lumi prepares writer integration and again
    # where it continues.
    from types import SimpleNamespace

    runtime, capture = _keyless_bedrock_runtime(tmp_path, monkeypatch)
    attached = []
    setup = {"model": {"provider": "conn-bedrock", "model": "us.anthropic.claude-sonnet-5-v1:0"},
             "write_roots": ["src"], "worker_requests": 4}
    monkeypatch.setattr(runtime, "_setup", lambda *args, **kwargs: (setup, {}))
    monkeypatch.setattr(runtime, "_execution_mode", lambda _capture: "managed")
    monkeypatch.setattr(runtime, "_governance", lambda *args: SimpleNamespace(dispatch_refusal=lambda: ""))
    runtime._managed_recoveries["swarm_run"] = SimpleNamespace(prepare_resume=lambda: attached.append(True))
    recovery = SimpleNamespace(run_id="swarm_run", authority=SimpleNamespace(run_id="swarm_run"), close=lambda: None)
    runtime._recoveries["swarm_run"] = (capture, recovery)
    message = {"action": "continue_recovered", "run_id": "swarm_run", "request_id": "continue"}
    try:
        with pytest.raises(Conflict, match="Bedrock API key"):
            runtime._prepare_recovery_integration(capture, message)
        with pytest.raises(Conflict, match="Bedrock API key"):
            runtime._continue_recovered(capture, runtime._store(capture), recovery, message)
        assert attached == [] and not runtime._runners
    finally:
        runtime.close()


# ── A real child worker on a loopback Chat Completions endpoint ──────────────


def _command(supervisor, authority, kind, payload=None):
    revision = supervisor.store.snapshot(authority.scope, authority.run_id)["run"]["revision"]
    return supervisor.handle(Command(uuid.uuid4().hex, authority.run_id, revision,
                                     authority.epoch, kind, payload or {}), authority)


def _one_worker(tmp_path, provider="conn-fixture", model="fixture/model"):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "fact.txt").write_text("Actual isolated fact", encoding="utf-8")
    supervisor = SwarmSupervisor(SwarmStore(tmp_path / "runtime" / "state.sqlite"))
    tools = frozenset({"file_read", "glob", "grep", "artifact_read"}) | SWARM_TOOL_NAMES
    authority = supervisor.create(Scope.personal("owner", "project", "session"),
        supervisor_id="supervisor", objective="Inspect isolated facts", request_limit=20,
        policy=PolicyProfile(1, tools, frozenset({provider})), lease_seconds=300)
    _command(supervisor, authority, "plan", {"work_items": [
        {"id": "first", "objective": "Inspect first facts", "read_roots": ["."], "write_roots": [],
         "tools": sorted(tools), "criteria": ["fact"]}]})
    result = _command(supervisor, authority, "assign", {"work_item_id": "first", "worker_id": "first",
        "requests": 3, "model": {"provider": provider, "model": model}}).result
    context = AttemptContext(authority.scope, authority.run_id, result["attempt_id"], "first", authority.epoch)
    return supervisor, authority, workspace, context


@pytest.fixture
def team(tmp_path):
    return _one_worker(tmp_path)


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


def test_a_worker_process_says_why_it_refused_and_holds_no_key_from_the_apps_environment(tmp_path, monkeypatch):
    # The app gives a participant its key in its start message
    # (SwarmRuntime._participant_keys); its process inherits no credentials.
    # So a Bedrock worker whose start message has no key refuses, though the
    # app's own environment has AWS_BEARER_TOKEN_BEDROCK, and says why in
    # Lumi's words, not only "Native worker failed (ValueError)".
    monkeypatch.setenv("AWS_BEARER_TOKEN_BEDROCK", "app-environment-bedrock-key")
    model = "us.anthropic.claude-sonnet-5-v1:0"
    supervisor, authority, workspace, context = _one_worker(tmp_path, "conn-bedrock", model)
    command = child_script(tmp_path, "from lumi.engine.swarming.worker_child import main\nraise SystemExit(main())\n")
    runner = SwarmWorkerRunner(supervisor, authority, workspace, managed_readers=True,
        backend_factory=lambda _: pytest.fail("A managed reader builds its backend in its own process"),
        writer_process_factory=lambda: ManagedWorkerProcess(command=command),
        process_observations=ProcessObservations(supervisor.store, host_id="fixture-host"),
        connections={"conn-bedrock": BEDROCK})
    try:
        runner.start(context, BackendSpec("conn-bedrock", model))
        until(lambda: not runner.inspect(context.attempt_id)["alive"], timeout=90)
        status = runner.inspect(context.attempt_id)
        assert status["error"] == ("Add Claude on Bedrock's Bedrock API key in Settings > Connections: a team's "
                                   "participants use a key, never your AWS sign-in."), status
        assert status["state"] == "failed"
    finally:
        runner.close()
    snapshot = supervisor.store.snapshot(authority.scope, authority.run_id)
    assert snapshot["model_requests"] == []


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
