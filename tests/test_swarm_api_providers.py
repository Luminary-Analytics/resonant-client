"""A whole team on the Anthropic and OpenAI APIs: plan, workers, checks and acceptance.

The orchestrator and its workers run through Lumi's own adapters
(lumi/anthropic_api.py, lumi/openai_api.py), built from the team's captured
BackendSpec as the app builds them, against scripted loopback servers that
stream as the providers do (tests/api_provider_stub.py). No real key or
provider is used. Most participants run in this process; one team runs every
participant in its own process, as the app does.
"""

import json
import sys
import threading
import time
import uuid

import pytest

from lumi import audit, usage
from lumi.anthropic_api import API_VERSION
from lumi.audit import AuditLog
from lumi.engine.swarming import Scope
from lumi.engine.swarming.autopilot import PLAN_EVIDENCE, WRITER_EVIDENCE
from lumi.engine.swarming.models import Conflict
from lumi.engine.swarming.service import CapturedSession, SwarmRuntime
from lumi.gui.runtime import BackendSpec
from lumi.gui.settings import SettingsManager
from lumi.usage import UsageLedger
from tests.api_provider_stub import KEY, OUTPUT_TOKENS, Reply, ScriptedProviders
from tests.test_swarm_workers import until
from tests.test_swarm_writers import git

CLAUDE = "claude-sonnet-5"
GPT = "gpt-5"
AZURE_DEPLOYMENT = "gpt-5-deployment"
CHECK = {"key": "value-check", "timeout_seconds": 60, "argv": [sys.executable, "-c",
         "from pathlib import Path; assert Path('src/value.txt').read_text() == 'verified change\\n', 'value is wrong'"]}
WRITER_PLAN = {"summary": "One writer updates src/value.txt and value-check verifies it.", "use_team": True,
               "work_items": [{"id": "value", "objective": "Update src/value.txt", "role": "implement",
                               "dependencies": [], "read_roots": ["src"], "write_roots": ["src"],
                               "criteria": ["value-check"]}]}
WRITER_FINAL = {"summary": "src/value.txt holds the verified value; value-check passed on the applied change.",
                "use_team": False, "work_items": []}
READ_PLAN = {"summary": "Two readers inspect the API and the UI.", "use_team": True, "work_items": [
    {"id": "api", "objective": "Inspect how src/api.py validates input", "role": "explore", "dependencies": [],
     "read_roots": ["src"], "write_roots": [], "criteria": ["owner_review"]},
    {"id": "ui", "objective": "Inspect how src/ui.py escapes output", "role": "explore", "dependencies": [],
     "read_roots": ["src"], "write_roots": [], "criteria": ["owner_review"]}]}
READ_FINAL = {"summary": "The API rejects empty names and the UI escapes HTML; both were read from source.",
              "use_team": False, "work_items": []}
AZURE = {"id": "azure", "name": "Azure OpenAI", "type": "azure-openai", "models": [AZURE_DEPLOYMENT],
         "api_version": "2025-04-01-preview"}
BEDROCK_MODEL = "us.anthropic.claude-sonnet-5-v1:0"
BEDROCK_PATH = "/model/us.anthropic.claude-sonnet-5-v1%3A0/invoke-with-response-stream"


def planning(turn):
    """The orchestrator's planning or closing turn (coordinator.CoordinatorPlans.prompt)."""
    return "Propose useful bounded work" in turn.prompt


def reader(turn, path, finding):
    """A reader: read its file, then report."""
    if turn.step == 0:
        return Reply(tool=("file_read", {"path": path}))
    assert any("def " in result for result in turn.results), "The reader was told what it read"
    return Reply(text=finding)


def read_script(orchestrator, workers):
    """Script a read-only team: ``orchestrator`` answers planning turns, ``workers`` reads and reports."""
    def script(turn):
        if planning(turn):
            return orchestrator(turn)
        if "src/api.py" in turn.prompt:
            return workers(turn, "src/api.py", "src/api.py rejects empty names before saving.")
        if "src/ui.py" in turn.prompt:
            return workers(turn, "src/ui.py", "src/ui.py escapes HTML with html.escape.")
        raise AssertionError("A request no participant sends")
    return script


@pytest.fixture
def records(tmp_path):
    """Usage records and the audit log in this test's folder (conftest resets both)."""
    ledger = UsageLedger(tmp_path / "usage")
    usage.set_for_tests(ledger)
    audit.set_for_tests(AuditLog(tmp_path / "audit"))
    return ledger


@pytest.fixture
def providers():
    server = ScriptedProviders()
    yield server
    server.close()


def project(tmp_path, *, git_base=False):
    root = tmp_path / "project"
    (root / "src").mkdir(parents=True)
    (root / "src" / "value.txt").write_text("original\n")
    (root / "src" / "api.py").write_text("def save(name):\n    if not name:\n        raise ValueError('empty')\n")
    (root / "src" / "ui.py").write_text("import html\n\ndef show(text):\n    return html.escape(text)\n")
    if git_base:
        git(root, "init", "-b", "main")
        git(root, "add", ".")
        git(root, "commit", "-m", "Fixture base")
    return root


def runtime(tmp_path, settings, *, in_process=True):
    """The desktop runtime; ``in_process`` builds each participant's backend here from its spec, as the child does."""
    factory = {"backend_factory": lambda spec: spec.create_backend(settings)} if in_process else {}
    service = SwarmRuntime(settings, state_root=lambda _: tmp_path / "state", **factory)
    return service


def keyed_settings(tmp_path, providers, **keys):
    settings = SettingsManager(tmp_path / "settings.json")
    for name in keys:
        settings.set("api_keys", name, KEY)
    settings.set("connections", None, [{**AZURE, "base_url": providers.url + "/openai/v1"}])
    return settings


def bedrock_settings(tmp_path, providers, *, saved_key=True):
    """Claude on Bedrock at the scripted server, with a Bedrock API key saved for it or not."""
    settings = keyed_settings(tmp_path, providers, **({"conn_bedrock": True} if saved_key else {}))
    settings.set("connections", None, [{"id": "bedrock", "name": "Claude on Bedrock", "type": "anthropic-bedrock",
                                        "region": "us-east-1", "base_url": providers.url, "models": [BEDROCK_MODEL],
                                        "auth": "bearer"}])
    return settings


def bedrock_spec():
    return BackendSpec("conn-bedrock", BEDROCK_MODEL, api_key_source="settings", api_key_setting="conn_bedrock")


def start(service, capture, **setup):
    service.operate(capture, {"action": "configure", "request_id": "enable", "enabled": True})
    return service.operate(capture, {"request_id": "setup-request", "action": "start", "plan_mode": "coordinator",
                                     "tasks": None, "request_limit": 20, "max_workers": 2, "coordinator_requests": 3,
                                     "worker_requests": 4, **setup})["run"]["run"]["id"]


def finished(service, capture, run_id, timeout=90):
    def done():
        view = service.operate(capture, {"request_id": f"view-{uuid.uuid4().hex}", "run_id": run_id})
        terminal = view["run"]["run"]["state"] in {"completed", "cancelled", "failed"}
        return view if terminal and not (view["autonomy"] or {}).get("active") else None
    return until(done, timeout=timeout, describe=lambda: json.dumps(service.operate(capture, {
        "request_id": "last-view", "run_id": run_id})["autonomy"], default=str))


def team_usage(ledger):
    return [row for row in ledger.records() if row["purpose"].startswith("team")]


def test_an_anthropic_orchestrator_plans_its_writer_checks_applies_and_reports(tmp_path, providers, records):
    root = project(tmp_path, git_base=True)
    base = git(root, "rev-parse", "HEAD")

    def script(turn):
        if planning(turn):
            if "This is your closing turn" in turn.prompt:
                # Prose before the bare JSON, as Claude tends to write it.
                return Reply(text="The change is applied and checked. Final report:\n\n" + json.dumps(WRITER_FINAL))
            if turn.step == 0:
                # Current Claude models think by default and sign what they thought.
                return Reply(thinking="Read the value before planning.", tool=("file_read", {"path": "src/value.txt"}))
            assert any("original" in result for result in turn.results)
            return Reply(text="I read src/value.txt. Here is my proposal:\n\n```json\n"
                              + json.dumps(WRITER_PLAN, indent=2) + "\n```\n\nThe check verifies the change.")
        assert "Update src/value.txt" in turn.prompt
        if turn.step == 0:
            return Reply(tool=("file_write", {"path": "src/value.txt", "content": "verified change\n"}))
        return Reply(text="Wrote src/value.txt.")

    providers.script = script
    settings = keyed_settings(tmp_path, providers, anthropic=True)
    service = runtime(tmp_path, settings)
    capture = CapturedSession(Scope.personal("fixture-owner", "project", "session"), str(root),
                              BackendSpec("anthropic", CLAUDE, base_url=providers.url, api_key_source="settings",
                                          api_key_setting="anthropic"))
    try:
        run_id = start(service, capture, objective="Change the scoped value", write_roots=["src"], checks=[CHECK],
                       autonomy={"rounds": 1, "apply": True})
        view = finished(service, capture, run_id)
    finally:
        service.close()
    run = view["run"]
    assert providers.errors == []
    assert run["run"]["state"] == "completed", view["autonomy"]
    assert view["autonomy"]["final_report"] == WRITER_FINAL["summary"]
    # Checked and applied through the owner's own integration path, then accepted under the grant.
    assert [(row["kind"], row["state"]) for row in run["integration_operations"]] == [
        ("prepare_candidate", "completed"), ("run_check", "completed"), ("apply", "completed")]
    candidate, = run["integration_candidates"]
    assert candidate["state"] == "applied" and candidate["base_revision"] == base
    assert git(root, "rev-parse", "HEAD") == candidate["result_revision"]
    assert (root / "src" / "value.txt").read_text() == "verified change\n"
    acceptance, = run["writer_acceptances"]
    assert acceptance["owner_id"] == "autonomy:fixture-owner" and acceptance["evidence"] == WRITER_EVIDENCE
    # One HTTP request per admitted request, every one of them settled: the
    # orchestrator's two (a read, then the plan), the writer's two, the report.
    calls = providers.of("anthropic")
    assert len(providers.requests) == len(calls) == 5
    assert [row["state"] for row in run["model_requests"]] == ["completed"] * 5
    for call in calls:
        assert call["path"] == "/v1/messages" and call["body"]["model"] == CLAUDE and call["body"]["stream"]
        assert call["headers"]["x-api-key"] == KEY and call["headers"]["anthropic-version"] == API_VERSION
    # Tools in the Messages API's own format, only those each participant was granted.
    planner_tools = {tool["name"] for tool in calls[0]["body"]["tools"]}
    writer_tools = {tool["name"] for tool in calls[2]["body"]["tools"]}
    assert "file_read" in planner_tools and "file_write" not in planner_tools
    assert {"file_write", "file_edit"} <= writer_tools
    assert all("input_schema" in tool for tool in calls[0]["body"]["tools"])
    # The tool loop continues with the signed thinking and the call's own result.
    assistant = next(message for message in calls[1]["body"]["messages"] if message["role"] == "assistant")
    assert assistant["content"][0] == {"type": "thinking", "thinking": "Read the value before planning.",
                                       "signature": "sig-1"}
    assert assistant["content"][-1]["type"] == "tool_use" and assistant["content"][-1]["id"] == "toolu_fixture_1"
    result = calls[1]["body"]["messages"][-1]["content"][0]
    assert result["type"] == "tool_result" and result["tool_use_id"] == "toolu_fixture_1"
    # Each request is priced once from the catalog under the team's purpose:
    # 1,000 input tokens, 200 read from and 100 written to the cache, and 50 output.
    rows = team_usage(records)
    assert len(rows) == 5 and {(row["provider"], row["model"]) for row in rows} == {("anthropic", CLAUDE)}
    expected = (1000 * 2 + 200 * 0.2 + 100 * 2.5 + OUTPUT_TOKENS * 10) / 1_000_000
    assert {row["price_source"] for row in rows} == {"catalog"}
    assert all(row["cost_usd"] == pytest.approx(expected) for row in rows)


def test_an_openai_orchestrator_runs_readers_in_rounds_and_reports(tmp_path, providers, records):
    root = project(tmp_path)

    def orchestrator(turn):
        if "If the findings already meet the objective" in turn.prompt:
            assert "src/api.py rejects empty names" in turn.prompt and "html.escape" in turn.prompt
            return Reply(text=json.dumps(READ_FINAL))
        return Reply(text=json.dumps(READ_PLAN))

    providers.script = read_script(orchestrator, reader)
    settings = keyed_settings(tmp_path, providers, openai=True)
    service = runtime(tmp_path, settings)
    capture = CapturedSession(Scope.personal("fixture-owner", "project", "session"), str(root),
                              BackendSpec("openai", GPT, base_url=providers.url + "/v1", api_key_source="settings",
                                          api_key_setting="openai"))
    try:
        run_id = start(service, capture, objective="Check how input is handled", autonomy={"rounds": 2})
        view = finished(service, capture, run_id)
    finally:
        service.close()
    run = view["run"]
    assert providers.errors == []
    assert run["run"]["state"] == "completed" and view["autonomy"]["final_report"] == READ_FINAL["summary"]
    assert [row["state"] for row in run["coordinator_proposals"]] == ["accepted", "accepted"]
    assert {row["decision_evidence"] for row in run["coordinator_proposals"]} == {PLAN_EVIDENCE}
    assert all(row["state"] == "accepted" for row in run["work_items"])
    assert {row["executor_id"] for row in run["check_receipts"]} == {"autonomy:fixture-owner"}
    calls = providers.of("openai")
    # The plan, two requests per reader, the follow-up: one HTTP request each.
    assert len(providers.requests) == len(calls) == 6
    assert [row["state"] for row in run["model_requests"]] == ["completed"] * 6
    for call in calls:
        assert call["path"] == "/v1/responses" and call["headers"]["authorization"] == f"Bearer {KEY}"
        assert call["body"]["model"] == GPT and call["body"]["store"] is False and call["body"]["stream"]
    # A reader's second request replays its encrypted reasoning, its call and the call's output.
    second = [call for call in calls if any(item.get("type") == "function_call_output" for item in call["body"]["input"])]
    assert len(second) == 2
    for call in second:
        kinds = [item.get("type") or item.get("role") for item in call["body"]["input"]]
        assert kinds[-3:] == ["reasoning", "function_call", "function_call_output"]
        reasoning = call["body"]["input"][-3]
        assert reasoning["encrypted_content"].startswith("enc-") and "provider" not in reasoning
        assert call["body"]["input"][-1]["call_id"] == call["body"]["input"][-2]["call_id"]
    rows = team_usage(records)
    expected = (800 * 1.25 + 200 * 0.125 + OUTPUT_TOKENS * 10) / 1_000_000
    assert len(rows) == 6 and {row["provider"] for row in rows} == {"openai"}
    assert all(row["cost_usd"] == pytest.approx(expected) for row in rows)


def test_a_claude_orchestrator_with_workers_on_an_azure_openai_connection(tmp_path, providers, records):
    root = project(tmp_path)

    def orchestrator(turn):
        assert turn.protocol == "anthropic", "The orchestrator keeps the conversation's model"
        if "This is your closing turn" in turn.prompt:
            return Reply(text=json.dumps(READ_FINAL) + "\n\nBoth findings came from the source files.")
        return Reply(text=json.dumps(READ_PLAN))

    def workers(turn, path, finding):
        assert turn.protocol == "openai", "Workers run on the Azure connection"
        return reader(turn, path, finding)

    providers.script = read_script(orchestrator, workers)
    settings = keyed_settings(tmp_path, providers, anthropic=True, conn_azure=True)
    service = runtime(tmp_path, settings)
    capture = CapturedSession(Scope.personal("fixture-owner", "project", "session"), str(root),
                              BackendSpec("anthropic", CLAUDE, base_url=providers.url, api_key_source="settings",
                                          api_key_setting="anthropic"))
    try:
        run_id = start(service, capture, objective="Check how input is handled", autonomy={"rounds": 1},
                       worker_model={"provider": "conn-azure", "model": AZURE_DEPLOYMENT})
        view = finished(service, capture, run_id)
    finally:
        service.close()
    run = view["run"]
    assert providers.errors == []
    assert run["run"]["state"] == "completed" and view["autonomy"]["final_report"] == READ_FINAL["summary"]
    assert run["worker_model"] == {"provider": "conn-azure", "model": AZURE_DEPLOYMENT, "label": "Azure OpenAI"}
    grants = {row["kind"]: json.loads(row["grant_json"])["model"] for row in run["attempts"]}
    assert grants == {"coordinator": {"provider": "anthropic", "model": CLAUDE},
                      "worker": {"provider": "conn-azure", "model": AZURE_DEPLOYMENT}}
    azure = providers.of("openai")
    assert len(azure) == 4 and len(providers.of("anthropic")) == 2
    for call in azure:
        # The Azure deployment endpoint, its API version and key header; never a bearer token.
        assert call["path"] == "/openai/v1/responses?api-version=2025-04-01-preview"
        assert call["headers"]["api-key"] == KEY and "authorization" not in call["headers"]
    # A deployment name has no catalog price: recorded unpriced, never as $0.
    rows = team_usage(records)
    unpriced = [row for row in rows if row["provider"] == "conn-azure"]
    assert len(unpriced) == 4 and {(row["cost_usd"], row["price_source"]) for row in unpriced} == {(None, "unpriced")}
    assert {row["model"] for row in rows if row["provider"] == "anthropic"} == {CLAUDE}


@pytest.mark.parametrize("protocol", ["anthropic", "openai", "bedrock"])
def test_a_participants_last_request_keeps_its_tools_and_lets_none_run(tmp_path, providers, records, protocol):
    # A participant's last request offers no tools, so the model answers
    # (engine/session.py). The APIs still need the definitions its history
    # refers to (the Messages API refuses tool calls without them), and a Claude
    # thinking block's signature binds the tool set: the conversation's own
    # definitions go out again, with tool_choice none. On Bedrock that is the
    # request Lumi sends; no live Bedrock request has confirmed that Bedrock
    # accepts none (docs/known-issues.md), and the scripted server assumes it.
    root = project(tmp_path)
    providers.script = lambda turn: (Reply(tool=("file_read", {"path": "src/api.py"})) if turn.step == 0
                                     else Reply(text="src/api.py rejects empty names."))
    if protocol == "bedrock":
        settings, spec = bedrock_settings(tmp_path, providers), bedrock_spec()
    else:
        settings = keyed_settings(tmp_path, providers, **{protocol: True})
        spec = (BackendSpec("anthropic", CLAUDE, base_url=providers.url, api_key_source="settings",
                            api_key_setting="anthropic") if protocol == "anthropic" else
                BackendSpec("openai", GPT, base_url=providers.url + "/v1", api_key_source="settings",
                            api_key_setting="openai"))
    service = runtime(tmp_path, settings)
    capture = CapturedSession(Scope.personal("fixture-owner", "project", "session"), str(root), spec)

    def settled():
        view = service.operate(capture, {"request_id": f"view-{uuid.uuid4().hex}", "run_id": run_id})
        workers = view["run"]["workers"]
        return view if workers and all(row["termination_recorded"] for row in workers) else None
    try:
        service.operate(capture, {"action": "configure", "request_id": "enable", "enabled": True})
        run_id = service.operate(capture, {"request_id": "manual", "action": "start", "objective": "Inspect",
                                           "tasks": [{"objective": "Inspect src/api.py", "read_roots": ["src"]}],
                                           "request_limit": 2, "max_workers": 1})["run"]["run"]["id"]
        view = until(settled, timeout=60)
    finally:
        service.close()
    assert providers.errors == []
    first, last = providers.of(protocol)
    assert last["body"]["tools"] == first["body"]["tools"] and first["body"]["tools"]
    assert last["body"]["tool_choice"] == ("none" if protocol == "openai" else {"type": "none"})
    assert first["body"].get("tool_choice") in (None, "auto")
    assert [row["state"] for row in view["run"]["model_requests"]] == ["completed", "completed"]
    submission, = view["run"]["submissions"]
    assert "rejects empty names" in submission["handoff"]


def test_every_participant_runs_in_its_own_process_on_the_anthropic_api(tmp_path, providers, records):
    # The app's own path: no backend built in the app. Each participant's
    # process rebuilds the native Anthropic backend from its captured spec.
    root = project(tmp_path)
    one_reader = {**READ_PLAN, "summary": "One reader inspects the API.", "work_items": READ_PLAN["work_items"][:1]}

    def orchestrator(turn):
        if "This is your closing turn" in turn.prompt:
            return Reply(text=json.dumps(READ_FINAL))
        return Reply(text=json.dumps(one_reader))

    providers.script = read_script(orchestrator, reader)
    settings = keyed_settings(tmp_path, providers, anthropic=True)
    service = runtime(tmp_path, settings, in_process=False)
    capture = CapturedSession(Scope.personal("fixture-owner", "project", "session"), str(root),
                              BackendSpec("anthropic", CLAUDE, base_url=providers.url, api_key_source="settings",
                                          api_key_setting="anthropic"))
    try:
        run_id = start(service, capture, objective="Check how input is handled", autonomy={"rounds": 1})
        view = finished(service, capture, run_id, timeout=240)
        events = service.operate(capture, {"request_id": "events", "action": "events", "run_id": run_id})
    finally:
        service.close()
    run = view["run"]
    assert providers.errors == []
    assert run["run"]["state"] == "completed" and view["autonomy"]["final_report"] == READ_FINAL["summary"]
    # Three processes (the plan, the reader, the report), each seen starting and stopping.
    assert len(run["process_observations"]) == 3
    assert {row["state"] for row in run["process_observations"]} == {"stopped"}
    assert len(providers.of("anthropic")) == 4 and [row["state"] for row in run["model_requests"]] == ["completed"] * 4
    assert {call["headers"]["x-api-key"] for call in providers.of("anthropic")} == {KEY}
    assert KEY not in json.dumps(view, default=str) and KEY not in json.dumps(events, default=str)
    assert len(team_usage(records)) == 4


def test_stop_ends_a_worker_streaming_from_the_anthropic_api(tmp_path, providers, records):
    root = project(tmp_path)
    held = threading.Event()
    providers.script = lambda turn: Reply(text="Never finished.", hold=held)
    settings = keyed_settings(tmp_path, providers, anthropic=True)
    service = runtime(tmp_path, settings)
    capture = CapturedSession(Scope.personal("fixture-owner", "project", "session"), str(root),
                              BackendSpec("anthropic", CLAUDE, base_url=providers.url, api_key_source="settings",
                                          api_key_setting="anthropic"))
    try:
        service.operate(capture, {"action": "configure", "request_id": "enable", "enabled": True})
        run_id = service.operate(capture, {"request_id": "manual", "action": "start", "objective": "Inspect",
                                           "tasks": [{"objective": "Inspect src/api.py", "read_roots": ["src"]}],
                                           "request_limit": 4, "max_workers": 1})["run"]["run"]["id"]
        until(lambda: providers.requests, timeout=30)
        revision = service.operate(capture, {"request_id": "before-stop", "run_id": run_id})["run"]["run"]["revision"]
        began = time.monotonic()
        service.operate(capture, {"request_id": "stop", "action": "stop", "run_id": run_id,
                                  "expected_revision": revision})
        def current():
            return service.operate(capture, {"request_id": f"view-{uuid.uuid4().hex}", "run_id": run_id})

        view = until(lambda: (lambda now: now if now["run"]["workers"] and all(
            row["termination_recorded"] and not row["alive"] for row in now["run"]["workers"]) else None)(current()),
                     timeout=30, describe=lambda: json.dumps(current()["run"]["workers"], default=str))
        stopped_after = time.monotonic() - began
        # The provider sees the connection close while its response is still going.
        until(lambda: providers.abandoned, timeout=10)
    finally:
        held.set()
        service.close()
    # The worker left its stream at the next event and nothing was sent again.
    assert stopped_after < 20
    assert len(providers.requests) == 1
    # The interrupted request stays uncertain (its outcome isn't known) and holds
    # its allowance, so the stopped team waits for that to be reconciled.
    assert [row["state"] for row in view["run"]["model_requests"]] == ["uncertain"]
    assert view["run"]["run"]["stop_requested"] and view["run"]["run"]["state"] == "stopping"
    assert team_usage(records) == []


def _one_reader_orchestrator(protocol):
    one_reader = {**READ_PLAN, "summary": "One reader inspects the API.", "work_items": READ_PLAN["work_items"][:1]}

    def orchestrator(turn):
        assert turn.protocol == protocol
        if "This is your closing turn" in turn.prompt:
            return Reply(text=json.dumps(READ_FINAL))
        return Reply(text=json.dumps(one_reader))
    return orchestrator


@pytest.mark.parametrize("in_process", [True, False], ids=["in-process", "own-processes"])
def test_an_orchestrated_team_on_claude_on_bedrock(tmp_path, providers, records, monkeypatch, in_process):
    # Bedrock streams the Messages API's events in AWS's event-stream framing,
    # at the model's own path, with the Bedrock API key as a bearer token. In
    # its own process a participant gets that key in its start message: its
    # environment carries no AWS_BEARER_TOKEN_BEDROCK (worker_environment), so
    # the key the app read from the environment reaches it only that way.
    root = project(tmp_path)
    if in_process:
        monkeypatch.delenv("AWS_BEARER_TOKEN_BEDROCK", raising=False)
    else:
        monkeypatch.setenv("AWS_BEARER_TOKEN_BEDROCK", KEY)
    providers.script = read_script(_one_reader_orchestrator("bedrock"), reader)
    service = runtime(tmp_path, bedrock_settings(tmp_path, providers, saved_key=in_process), in_process=in_process)
    capture = CapturedSession(Scope.personal("fixture-owner", "project", "session"), str(root), bedrock_spec())
    try:
        run_id = start(service, capture, objective="Check how input is handled", autonomy={"rounds": 1})
        view = finished(service, capture, run_id, timeout=90 if in_process else 240)
        events = service.operate(capture, {"request_id": "events", "action": "events", "run_id": run_id})
    finally:
        service.close()
    run = view["run"]
    assert providers.errors == []
    assert run["run"]["state"] == "completed" and view["autonomy"]["final_report"] == READ_FINAL["summary"]
    # The plan, the reader's two requests and the report: one HTTP request each.
    calls = providers.of("bedrock")
    assert len(providers.requests) == len(calls) == 4
    assert [row["state"] for row in run["model_requests"]] == ["completed"] * 4
    for call in calls:
        assert call["path"] == BEDROCK_PATH and call["headers"]["authorization"] == f"Bearer {KEY}"
        assert call["body"]["anthropic_version"] == "bedrock-2023-05-31"
        assert "model" not in call["body"] and "stream" not in call["body"]
    assert KEY not in json.dumps(view, default=str) and KEY not in json.dumps(events, default=str)
    assert len(team_usage(records)) == 4
    if not in_process:
        assert {row["state"] for row in run["process_observations"]} == {"stopped"}


@pytest.mark.parametrize("error, settled, sent", [
    (("api_error", "Internal server error"), "uncertain", 1),  # may have come after generation began
    (("overloaded_error", "Overloaded"), "completed", 4),  # refused before generating: waited out, then known
])
def test_a_workers_in_stream_error_before_output_is_known_only_when_the_provider_refused(
        tmp_path, providers, records, monkeypatch, error, settled, sent):
    monkeypatch.setattr("lumi.anthropic_api._wait_with_cancel", lambda *_: False)
    root = project(tmp_path)
    providers.script = lambda turn: Reply(error=error)
    service = runtime(tmp_path, keyed_settings(tmp_path, providers, anthropic=True))
    capture = CapturedSession(Scope.personal("fixture-owner", "project", "session"), str(root),
                              BackendSpec("anthropic", CLAUDE, base_url=providers.url, api_key_source="settings",
                                          api_key_setting="anthropic"))

    def ended():
        view = service.operate(capture, {"request_id": f"view-{uuid.uuid4().hex}", "run_id": run_id})
        workers = view["run"]["workers"]
        return view if workers and all(row["termination_recorded"] for row in workers) else None
    try:
        service.operate(capture, {"action": "configure", "request_id": "enable", "enabled": True})
        run_id = service.operate(capture, {"request_id": "manual", "action": "start", "objective": "Inspect",
                                           "tasks": [{"objective": "Inspect src/api.py", "read_roots": ["src"]}],
                                           "request_limit": 2, "max_workers": 1})["run"]["run"]["id"]
        view = until(ended, timeout=60)
    finally:
        service.close()
    assert providers.errors == [] and len(providers.of("anthropic")) == sent
    assert [row["state"] for row in view["run"]["model_requests"]] == [settled]


def test_a_team_on_claude_on_bedrock_needs_its_bedrock_api_key(tmp_path, monkeypatch):
    monkeypatch.delenv("AWS_BEARER_TOKEN_BEDROCK", raising=False)
    root = project(tmp_path)
    settings = SettingsManager(tmp_path / "settings.json")
    settings.set("connections", None, [{"id": "bedrock", "name": "Claude on Bedrock", "type": "anthropic-bedrock",
                                        "region": "us-east-1", "models": ["us.anthropic.claude-sonnet-5-v1:0"],
                                        "auth": "bearer"}])
    service = runtime(tmp_path, settings)
    capture = CapturedSession(Scope.personal("fixture-owner", "project", "session"), str(root),
                              BackendSpec("conn-bedrock", "us.anthropic.claude-sonnet-5-v1:0",
                                          api_key_source="settings", api_key_setting="conn_bedrock"))
    try:
        service.operate(capture, {"action": "configure", "request_id": "enable", "enabled": True})
        with pytest.raises(Conflict, match="Bedrock API key.*never your AWS sign-in"):
            start(service, capture, objective="Check input", autonomy={"rounds": 1})
        assert not service.busy
        assert service.operate(capture, {"request_id": "view"})["run"] is None
    finally:
        service.close()
