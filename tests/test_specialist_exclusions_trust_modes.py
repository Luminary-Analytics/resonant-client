"""Orchestration specialists get the app's file exclusions, project trust and allowed modes.

LocalSpecialistRunner builds the Session of every specialist that a Mission's
"Build this roadmap" and autonomous sessions run. It left parts of the app's
session setup at the Session defaults:

* ``exclusions`` stayed None, so Settings' excluded paths, the project's
  .lumiignore and the organization's ``files.exclude`` weren't enforced;
* ``project_content_trusted`` stayed True, so an untrusted project's notes
  reached the model and its language servers started (the runner passed on
  the repository's instructions too, whatever the trust);
* ``computer_use_enabled`` stayed True whatever Settings (or a policy lock) said;
* specialists ran in Full-auto even where the organization's
  ``permissions.allowed_modes`` leaves it out.

These run the runner with a scripted model and the real Session, and check
what reaches the tools and the model, and what a refused start leaves behind.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from lumi import policy as lumi_policy
from lumi.engine import lsp
from lumi.engine.project_memory import ProjectMemory
from lumi.engine.tools import AGENT_TOOLS, ToolResult
from lumi.gui import roadmap as roadmap_module
from lumi.gui import ws_commands
from lumi.gui.autonomous_session import build_roadmap_from_spec, resume_autonomous_mission, start_autonomous_mission
from lumi.gui.workspace_trust import WorkspaceTrust
from lumi.orchestration import (
    LocalSpecialistRunner,
    NodeSpecialization,
    NodeStatus,
    PlanGraph,
    PlanNode,
    new_node_id,
    plans_dir,
)
from lumi.orchestration.acceptance_check import BashRunner
from lumi.orchestration.intent_service import IntentService
from tests.streaming_stub import StreamingBackend, done, events_of_kind, text_delta, tool_call
from tests.test_autonomous_loop import (
    _build_roadmap_on_disk,
    _events_of_kind,
    _make_daemon,
    _make_hooks,
    _run_daemon_to_completion,
    _StubCallTracker,
)
from tests.test_autonomous_session import _SPEC_MD, _StubAppState, _StubProject
from tests.test_ws_command_registry import _run, _StubWS

SECRET ="EXCLUDED-CONTENT-7f3a91"
FAKE_LSP = str(Path(__file__).with_name("fake_lsp_server.py"))
REFUSAL = "Acme's policy doesn't allow Full-auto, which missions and autonomous sessions run in."


@pytest.fixture(autouse=True)
def _isolated_state(tmp_path, monkeypatch):
    # Trust decisions and plan graphs live in the state folder: keep them in the test's own.
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("LUMI_STATE_HOME", str(home / ".lumi"))


@pytest.fixture
def project(tmp_path) -> Path:
    root = tmp_path / "project"
    root.mkdir()
    return root


def _policy(**sections) -> None:
    lumi_policy.set_for_tests(lumi_policy.parse(
        {"schema": "lumi.policy/v1", "organization": "Acme", **sections}, source="test"))


def _without_full_auto() -> None:
    _policy(permissions={"allowed_modes": ["ask", "auto-edit"]})


class Settings:
    """The Settings values a runner reads, keyed ``"section.key"`` (or ``"section"``)."""

    def __init__(self, values: dict):
        self.values = values

    def get(self, section, key=None, default=None):
        return self.values.get(section if key is None else f"{section}.{key}", default)


class RecordingBackend(StreamingBackend):
    """A scripted model that keeps everything each request sent it."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.requests: list[str] = []

    def stream(self, *, user_msg, conversation_history, instructions, tools, max_tokens, cancel_event=None):
        self.requests.append(json.dumps(
            {"instructions": instructions, "history": conversation_history, "message": user_msg}, default=str))
        yield from super().stream(user_msg=user_msg, conversation_history=conversation_history,
                                  instructions=instructions, tools=tools, max_tokens=max_tokens,
                                  cancel_event=cancel_event)


def _run_specialist(project: Path, calls: list[tuple[str, dict]], *, settings=None, working_subdir=None,
                    instructions: str = "", specialization: str = NodeSpecialization.IMPLEMENT):
    """One specialist whose model makes ``calls`` in turn and then finishes.

    Returns the specialist's result, its tool results in order and the model.
    """
    scripts = [[tool_call(name, args, call_id=f"c{i}"), done()] for i, (name, args) in enumerate(calls, 1)]
    backend = RecordingBackend(scripts=[*scripts, [text_delta("Finished."), done()]])
    events: list[dict] = []
    runner = LocalSpecialistRunner(
        backend=backend, project_path=str(project), all_tools=list(AGENT_TOOLS),
        project_instructions=instructions, settings=settings, on_session_event=events.append,
    )
    graph = PlanGraph.new("look at the project")
    node = PlanNode(id=new_node_id(), intent_id=graph.intent_id, goal="look at the project",
                    specialization=specialization, working_subdir=working_subdir)
    graph.add_node(node)
    result = runner(node, graph)
    return result, events_of_kind(events, "tool.result"), backend


# ── File exclusions ─────────────────────────────────────────────────────

# Read the file, search for its line, list the folder.
READS = [("file_read", {"path": "secrets/api.txt"}), ("grep", {"pattern": "token = "}),
         ("glob", {"pattern": "**/*.txt"})]


def _project_with_secret(project: Path) -> Path:
    (project / "secrets").mkdir()
    (project / "secrets" / "api.txt").write_text(f"token = {SECRET}\n", encoding="utf-8")
    (project / "config.txt").write_text("token = public-value\n", encoding="utf-8")
    return project


@pytest.mark.parametrize("source", ["Settings", ".lumiignore", "organization policy"])
def test_an_excluded_file_never_reaches_a_tool_result_or_the_model(project, source):
    _project_with_secret(project)
    settings = None
    if source == "Settings":
        settings = Settings({"privacy.excluded_paths": ["secrets/**"]})
    elif source == ".lumiignore":
        (project / ".lumiignore").write_text("secrets/**\n", encoding="utf-8")
    else:
        _policy(files={"exclude": ["secrets/**"]})

    _, results, backend = _run_specialist(project, READS, settings=settings)

    read, search, listing = results
    assert read["denied"] is True
    assert f"'secrets/api.txt' is excluded by 'secrets/**' ({source})" in read["output"]
    # The search and the listing ran, and left the file out.
    assert "public-value" in search["output"]
    assert "config.txt" in listing["output"] and "api.txt" not in listing["output"]
    assert not any(SECRET in str(result["output"]) for result in results)
    assert not any(SECRET in request for request in backend.requests)


def test_without_a_rule_the_same_reads_reach_the_model(project):
    # The checks above can see a leak: with nothing excluded, the contents come back.
    _project_with_secret(project)

    _, results, backend = _run_specialist(project, READS)

    assert SECRET in results[0]["output"] and SECRET in results[1]["output"]
    assert "api.txt" in results[2]["output"]
    assert any(SECRET in request for request in backend.requests)


def test_exclusions_are_the_project_roots_in_a_working_subdir(project):
    # The specialist works in web/, but .lumiignore and its anchored pattern are the project's.
    (project / "web" / "config").mkdir(parents=True)
    (project / "web" / "config" / "prod.env").write_text(f"KEY={SECRET}\n", encoding="utf-8")
    (project / ".lumiignore").write_text("/web/config/prod.env\n", encoding="utf-8")

    _, [read], backend = _run_specialist(project, [("file_read", {"path": "config/prod.env"})],
                                         working_subdir="web")

    assert read["denied"] is True
    assert "'web/config/prod.env' is excluded by '/web/config/prod.env' (.lumiignore)" in read["output"]
    assert not any(SECRET in request for request in backend.requests)


# ── Project trust ───────────────────────────────────────────────────────


@pytest.mark.parametrize("trusted", [True, False], ids=["trusted", "untrusted"])
def test_repository_notes_and_instructions_reach_a_specialist_only_in_a_trusted_project(project, trusted):
    ProjectMemory(project).save("NOTE-MARKER-41c2: release with make ship", kind="constraint", source="repository")
    if trusted:
        WorkspaceTrust().trust(str(project))

    _, _, backend = _run_specialist(project, [], instructions="INSTRUCTIONS-MARKER-9d1e")

    assert backend.requests
    assert any("NOTE-MARKER-41c2" in request for request in backend.requests) is trusted
    assert any("INSTRUCTIONS-MARKER-9d1e" in request for request in backend.requests) is trusted


@pytest.fixture
def no_language_servers():
    lsp.servers.close()
    yield
    lsp.servers.close()


@pytest.mark.parametrize("trusted", [True, False], ids=["trusted", "untrusted"])
def test_language_servers_start_for_a_specialist_only_in_a_trusted_project(project, trusted, no_language_servers):
    (project / "app.py").write_text("class Greeter:\n    def greet(self):\n        return 'hi'\n", encoding="utf-8")
    if trusted:
        WorkspaceTrust().trust(str(project))
    settings = Settings({"lsp_servers": {"fake": {"command": [sys.executable, FAKE_LSP], "extensions": [".py"]}}})

    _, [symbols], _ = _run_specialist(project, [("code_intel", {"action": "symbols", "path": "app.py"})],
                                      settings=settings, specialization=NodeSpecialization.EXPLORE)

    assert ("configured:fake" in lsp.servers.status()) is trusted
    if trusted:
        assert "Greeter" in symbols["output"]
    else:
        assert "Language servers start only in trusted projects" in symbols["output"]


# ── Computer use ────────────────────────────────────────────────────────


@pytest.mark.parametrize("enabled", [True, False], ids=["on", "off"])
def test_computer_use_follows_settings_for_specialists(project, monkeypatch, enabled):
    taken: list[dict] = []

    def screenshot(args, start):  # never the real screen
        taken.append(args)
        return ToolResult(output="(a screenshot)")

    monkeypatch.setattr("lumi.engine.computer.exec_computer_screenshot", screenshot)
    # On a Mac without Screen Recording access the tool stops earlier (engine/macos_permissions.py).
    monkeypatch.setattr("lumi.engine.macos_permissions.missing_permission", lambda tool_name: "")
    settings = Settings({"security.computer_use": enabled, "general.computer_use_indicator": False})

    _, [result], _ = _run_specialist(project, [("computer_screenshot", {})], settings=settings,
                                     specialization=NodeSpecialization.EXPLORE)

    assert bool(taken) is enabled
    if not enabled:
        assert result["denied"] is True and "Computer use is turned off" in result["output"]


# ── The organization's allowed modes ────────────────────────────────────


@pytest.mark.parametrize("allowed", [True, False], ids=["full-auto allowed", "full-auto not allowed"])
def test_a_specialist_runs_only_where_the_organization_allows_full_auto(project, allowed):
    _policy(permissions={"allowed_modes": ["ask", "auto-edit", "bypass"] if allowed else ["ask", "auto-edit"]})

    result, results, backend = _run_specialist(project, [("bash", {"command": "echo made > ran.txt"})])

    assert (project / "ran.txt").exists() is allowed
    if allowed:
        assert result.status == NodeStatus.DONE
    else:
        # Nothing ran: no model request, no tool call.
        assert (result.status, result.confidence, result.summary) == (NodeStatus.BLOCKED, 0.0, REFUSAL)
        assert backend.requests == [] and results == []


def test_a_mission_doesnt_start_where_the_organization_doesnt_allow_full_auto(project):
    _without_full_auto()
    backend = RecordingBackend(events=[text_delta("planned"), done()])
    service = IntentService(project_path=str(project), backend=backend, all_tools=list(AGENT_TOOLS))

    with pytest.raises(ValueError) as refused:
        service.start_intent("Build the counter component")

    assert str(refused.value) == REFUSAL
    # Nothing saved or started.
    assert list((plans_dir(project) / "current").iterdir()) == []
    assert service.list_active() == [] and backend.requests == []


def test_build_this_roadmap_shows_the_refusal_and_the_mission_stays_in_drafting(project):
    _without_full_auto()
    advanced: list[tuple] = []
    mission = SimpleNamespace(id="s1", mission_state={"phase": "drafting"},
                              advance_mission_phase=lambda *args, **kwargs: advanced.append(args),
                              save=lambda: None)
    service = IntentService(project_path=str(project), backend=RecordingBackend(), all_tools=[])
    state = SimpleNamespace(project=SimpleNamespace(project_path=str(project), current_session=mission),
                            get_intent_service=lambda on_event=None: service)
    ctx = ws_commands.CommandContext(ws=_StubWS(), state=state, msg={"spec_markdown": _SPEC_MD}, runs=None)

    sent = _run(ws_commands.HANDLERS["mission_dispatch_roadmap"], ctx)

    # The source tells the page to put its Build button back (autonomous_view.js).
    assert sent == [{"event": "error", "message": f"Roadmap dispatch failed: {REFUSAL}", "source": "mission_dispatch"}]
    assert advanced == [] and mission.mission_state["phase"] == "drafting"


def test_an_autonomous_session_doesnt_start_or_resume_where_full_auto_isnt_allowed(project):
    state = _StubAppState(project=_StubProject(str(project)))
    # A session from before the policy arrived, with its roadmap on disk.
    _, earlier = build_roadmap_from_spec(feature="counter", intent_id="auto-1", spec_markdown=_SPEC_MD,
                                         project_path=str(project))
    before = earlier.read_bytes()
    _without_full_auto()

    with pytest.raises(ValueError, match=re.escape(REFUSAL)):
        start_autonomous_mission(state=state, intent_id="auto-2", feature="counter", spec_markdown=_SPEC_MD,
                                 on_event=lambda event: None)
    with pytest.raises(ValueError, match=re.escape(REFUSAL)):
        resume_autonomous_mission(state=state, intent_id="auto-1", on_event=lambda event: None)

    assert not roadmap_module.default_path(str(project), "auto-2").exists()  # nothing saved
    assert earlier.read_bytes() == before
    assert state._autonomous_daemons == {} and state._intent_service is None


@pytest.mark.parametrize("policy_arrives", [True, False], ids=["policy arrives", "no policy"])
def test_a_running_autonomous_session_stops_before_its_bash_checks(tmp_path, policy_arrives):
    # The loop runs its [bash] acceptance checks itself; a policy that arrives
    # during an iteration stops it before the reflect pass would run them.
    path = _build_roadmap_on_disk(tmp_path, items=[(1, "T1.1", ""), (1, "T1.2", "")],
                                  criteria=[("bash", "`echo checked` exits 0")])
    calls = _StubCallTracker()
    commands: list[str] = []

    def run(command, **kwargs):
        commands.append(command)
        return 1, "", "not yet"  # a failing check, so the session doesn't converge and stop there

    hooks = _make_hooks(calls, bash_runner=BashRunner(_run=run))
    dispatch = hooks.dispatch_item

    def dispatch_while_the_policy_arrives(item):
        handle = dispatch(item)
        if policy_arrives:
            _without_full_auto()
        return handle

    hooks.dispatch_item = dispatch_while_the_policy_arrives
    daemon, events = _make_daemon(path, hooks, full_reflect_cadence=1, max_iterations=1)
    _run_daemon_to_completion(daemon)

    [paused] = _events_of_kind(events, "autonomous_mission_paused")
    assert len(calls.dispatched_items) == 1
    if policy_arrives:
        assert (paused["stop_reason"], paused["stop_message"]) == ("mode_not_allowed", REFUSAL.rstrip("."))
        assert commands == [] and calls.full_reflects == 0
        assert roadmap_module.load(path).status == "paused"
    else:
        assert paused["stop_reason"] == "iteration_cap"
        assert commands == ["echo checked"]


def test_an_autonomous_session_that_starts_without_full_auto_dispatches_nothing(tmp_path):
    path = _build_roadmap_on_disk(tmp_path, items=[(1, "T1.1", "")], criteria=[("bash", "`echo checked` exits 0")])
    calls = _StubCallTracker()
    daemon, events = _make_daemon(path, _make_hooks(calls))
    _without_full_auto()

    _run_daemon_to_completion(daemon)

    [paused] = _events_of_kind(events, "autonomous_mission_paused")
    assert paused["stop_reason"] == "mode_not_allowed"
    assert calls.dispatched_items == [] and calls.full_reflects == 0
