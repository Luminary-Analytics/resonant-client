from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from resonant_client.engine.agent_runtime import AgentHandoff, AgentRegistry, AgentStatus
from resonant_client.engine.artifacts import ArtifactKind, ArtifactStore
from resonant_client.engine.capability_packs import CapabilityPackManager
from resonant_client.engine.checkpoint_timeline import SessionCheckpointStore
from resonant_client.engine.code_intelligence import parse_code
from resonant_client.engine.context_broker import ContextBroker
from resonant_client.engine.flight_recorder import FlightRecorder
from resonant_client.engine.hooks import HookDefinition, HookRunner, HookType
from resonant_client.engine.model_roles import ModelRoleRouter
from resonant_client.engine.tools import AGENT_TOOLS
from resonant_client.engine.worktrees import WorktreeManager


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=cwd, text=True, capture_output=True, check=True,
        encoding="utf-8",
    )
    return result.stdout.strip()


def _init_repo(path: Path) -> None:
    path.mkdir()
    _git(path, "init")
    _git(path, "config", "user.name", "Harness Test")
    _git(path, "config", "user.email", "harness@test.invalid")
    (path / "README.md").write_text("initial\n", encoding="utf-8")
    _git(path, "add", "README.md")
    _git(path, "commit", "-m", "initial")


def test_artifact_bus_is_modality_neutral_and_content_addressed(tmp_path: Path):
    store = ArtifactStore(tmp_path, root=tmp_path / "state" / "artifacts")
    image = store.put_bytes(
        b"fake-png", kind=ArtifactKind.IMAGE, media_type="image/png", suffix=".png",
        label="browser capture", source="browser_screenshot",
    )
    text = store.put_text("complete terminal output", kind=ArtifactKind.TERMINAL)

    assert store.get(image.id) == image
    assert [item.kind for item in store.list()] == ["image", "terminal"]
    assert f"artifact:{text.id}" in store.reference(text)
    assert Path(image.path).read_bytes() == b"fake-png"


def test_agent_registry_persists_controls_transcript_and_handoff(tmp_path: Path):
    events = []
    registry = AgentRegistry(tmp_path, root=tmp_path / "agents", on_event=events.append)
    record = registry.create(agent_type="explore", prompt="Map the API", role="explore")
    registry.transition(record.id, AgentStatus.RUNNING, current_action="reading")
    registry.append_event(record.id, {"event": "step.end", "step": 1})
    registry.steer(record.id, "Focus on authentication")
    registry.complete(record.id, AgentHandoff(outcome="completed", summary="Mapped"))

    reloaded = AgentRegistry(tmp_path, root=tmp_path / "agents")
    saved = reloaded.get(record.id)
    assert saved is not None
    assert saved.status == "completed"
    assert saved.steps == 1
    assert saved.handoff["summary"] == "Mapped"
    assert registry.transcript(record.id)[0]["event"] == "step.end"
    assert {event["event"] for event in events} >= {"agent.created", "agent.updated", "agent.completed"}


def test_restart_marks_interrupted_agents_stuck_rather_than_running(tmp_path: Path):
    registry = AgentRegistry(tmp_path, root=tmp_path / "agents")
    record = registry.create(agent_type="implement", prompt="Add the parser")
    registry.transition(record.id, AgentStatus.RUNNING, current_action="editing")

    # A new registry stands in for a process restart: the thread is gone.
    reloaded = AgentRegistry(tmp_path, root=tmp_path / "agents")

    assert reloaded.get(record.id).status == "stuck"


def test_resume_refuses_to_resurrect_a_restart_orphaned_agent(tmp_path: Path):
    registry = AgentRegistry(tmp_path, root=tmp_path / "agents")
    record = registry.create(agent_type="implement", prompt="Add the parser")
    registry.transition(record.id, AgentStatus.RUNNING)
    reloaded = AgentRegistry(tmp_path, root=tmp_path / "agents")

    # Flipping this back to "running" would leave the UI waiting forever on a
    # worker with no thread behind it.
    try:
        reloaded.resume(record.id)
    except ValueError as exc:
        assert "restart_assignment" in str(exc)
    else:
        raise AssertionError("expected resume() to reject a stuck agent")

    assert reloaded.get(record.id).status == "stuck"


def test_resume_still_unpauses_a_live_agent(tmp_path: Path):
    registry = AgentRegistry(tmp_path, root=tmp_path / "agents")
    record = registry.create(agent_type="implement", prompt="Add the parser")
    registry.transition(record.id, AgentStatus.RUNNING)
    registry.request_pause(record.id)

    resumed = registry.resume(record.id)

    assert resumed.status == "running"
    assert "pause_requested" not in resumed.control


def test_restart_assignment_carries_everything_needed_to_re_dispatch(tmp_path: Path):
    registry = AgentRegistry(tmp_path, root=tmp_path / "agents")
    record = registry.create(
        agent_type="implement",
        prompt="Add the parser",
        model="glm-5.2",
        role="generator",
        workspace=str(tmp_path / "wt"),
        policy="guarded",
        max_steps=12,
    )
    registry.transition(record.id, AgentStatus.RUNNING)
    registry.append_event(record.id, {"event": "step.end", "step": 1})
    reloaded = AgentRegistry(tmp_path, root=tmp_path / "agents")

    assignment = reloaded.restart_assignment(record.id)

    assert assignment["prompt"] == "Add the parser"
    assert assignment["agent_type"] == "implement"
    assert assignment["workspace"] == str(tmp_path / "wt")
    assert assignment["policy"] == "guarded"
    assert assignment["max_steps"] == 12
    # The retry must know what its predecessor already finished.
    assert assignment["completed_steps"] == 1
    assert "Runtime restarted" in assignment["interrupted_reason"]


def test_restart_assignment_rejects_a_completed_agent(tmp_path: Path):
    registry = AgentRegistry(tmp_path, root=tmp_path / "agents")
    record = registry.create(agent_type="explore", prompt="Map the API")
    registry.complete(record.id, AgentHandoff(outcome="completed", summary="done"))

    try:
        registry.restart_assignment(record.id)
    except ValueError as exc:
        assert "nothing to restart" in str(exc)
    else:
        raise AssertionError("expected restart_assignment() to reject a completed agent")


def test_adopt_restart_preserves_the_interrupted_run_as_evidence(tmp_path: Path):
    registry = AgentRegistry(tmp_path, root=tmp_path / "agents")
    original = registry.create(agent_type="implement", prompt="Add the parser")
    registry.transition(original.id, AgentStatus.RUNNING)
    registry.append_event(original.id, {"event": "step.end", "step": 1})
    reloaded = AgentRegistry(tmp_path, root=tmp_path / "agents")

    retry = reloaded.adopt_restart(original.id)

    assert retry.id != original.id
    assert retry.prompt == original.prompt
    assert retry.status == "queued"
    assert retry.metadata["resumed_from"] == original.id
    assert retry.metadata["resumed_after_steps"] == 1
    # The interrupted transcript survives its own retry.
    assert reloaded.get(original.id).status == "stuck"
    assert reloaded.transcript(original.id)[0]["event"] == "step.end"


def test_checkpoint_timeline_restores_conversation_and_non_git_files(tmp_path: Path):
    project = tmp_path / "project"
    project.mkdir()
    target = project / "state.txt"
    target.write_text("before", encoding="utf-8")
    store = SessionCheckpointStore(project, session_id="s1", root=tmp_path / "checkpoints")
    checkpoint = store.create(
        conversation_history=[{"role": "user", "content": "before"}],
        display_events=[{"event": "user_message", "text": "before"}],
        reason="before edit",
    )
    target.write_text("after", encoding="utf-8")

    restored = store.restore(checkpoint.id, "both")
    assert target.read_text(encoding="utf-8") == "before"
    assert restored["conversation_history"][0]["content"] == "before"
    assert Path(restored["workspace"]["recovery_archive"]).is_file()


def test_structured_hook_can_modify_args_and_request_retry(tmp_path: Path):
    script = tmp_path / "hook.py"
    script.write_text(
        "import json,sys\n"
        "payload=json.load(sys.stdin)\n"
        "print(json.dumps({'decision':'allow','modified_args':{'path':'safe.txt'},"
        "'additional_context':payload['hook_event_name'],'retry':True}))\n",
        encoding="utf-8",
    )
    runner = HookRunner()
    runner.add_hooks([HookDefinition(
        hook_type=HookType.PRE_TOOL_USE,
        command=f'"{sys.executable}" "{script}"',
        matcher="file_*",
        input_format="json",
    )])
    result = runner.emit(
        HookType.PRE_TOOL_USE,
        {"project_path": str(tmp_path), "tool_args": {"path": "unsafe.txt"}},
        tool_name="file_write",
    )
    assert result.allowed
    assert result.modified_args == {"path": "safe.txt"}
    assert result.additional_context == "pre_tool_use"
    assert result.retry


def test_worktree_writer_isolation_and_serialized_integration(tmp_path: Path):
    project = tmp_path / "repo"
    _init_repo(project)
    manager = WorktreeManager(project, root=tmp_path / "managed-worktrees")
    lease = manager.create("writer-1")
    (Path(lease.path) / "feature.txt").write_text("isolated\n", encoding="utf-8")
    manager.finalize(lease)
    assert not (project / "feature.txt").exists()
    manager.integrate(lease)
    assert lease.status == "integrated"
    assert (project / "feature.txt").read_text(encoding="utf-8") == "isolated\n"
    manager.remove(lease, delete_branch=True)


def test_flight_recorder_replays_and_finds_first_causal_divergence(tmp_path: Path):
    root = tmp_path / "traces"
    left = FlightRecorder(tmp_path, run_id="left", root=root)
    right = FlightRecorder(tmp_path, run_id="right", root=root)
    left.record({"event": "tool.call", "name": "file_read"})
    right.record({"event": "tool.call", "name": "file_read"})
    left.record({"event": "tool.result", "output": "A"})
    right.record({"event": "tool.result", "output": "B"})
    left.close()
    right.close()

    comparison = FlightRecorder.compare(left, right)
    assert comparison["first_causal_divergence"]["index"] == 1
    assert left.export_otel()["resourceSpans"]
    loaded = FlightRecorder.load(root / "left")
    assert loaded.events()[0]["name"] == "file_read"
    assert loaded.manifest.status == "completed"
    persisted = json.loads((root / "left" / "manifest.json").read_text(encoding="utf-8"))
    assert persisted["status"] == "completed"


def test_context_broker_resolves_provenance_attachments(tmp_path: Path):
    (tmp_path / "module.py").write_text("VALUE = 42\n", encoding="utf-8")
    artifacts = ArtifactStore(tmp_path, root=tmp_path / "artifact-state")
    artifact = artifacts.put_text("full log", kind=ArtifactKind.TERMINAL)
    broker = ContextBroker(tmp_path, artifact_store=artifacts)
    items = broker.resolve_mentions(f"Review @file:module.py and @artifact:{artifact.id}")
    rendered = broker.render(items)
    assert {item.provider for item in items} == {"file", "artifact"}
    assert "provenance=" in rendered
    assert "VALUE = 42" in rendered


def test_role_router_is_explicit_and_falls_back_safely():
    fallback = object()
    routed = object()
    router = ModelRoleRouter(
        {"review": {"model": "review-model", "thinking_mode": "max"}},
        backend_factory=lambda profile: routed if profile.role == "review" else fallback,
    )
    assert router.backend_for("review", fallback) is routed
    assert router.backend_for("explore", fallback) is fallback
    assert router.profile("review").model == "review-model"


def test_capability_pack_unifies_agents_skills_hooks_and_mcp(tmp_path: Path):
    pack = tmp_path / "packs" / "quality"
    pack.mkdir(parents=True)
    (pack / "reviewer.md").write_text(
        "---\nname: pack-reviewer\ndescription: Deep review\ntools: [file_read, grep]\nmodel_role: review\n---\nReview independently.",
        encoding="utf-8",
    )
    (pack / "skill.md").write_text("Validate behavior and cite evidence.", encoding="utf-8")
    manifest = {
        "id": "quality", "name": "Quality Pack", "version": "1.0.0",
        "enabled": True, "trust": "local", "agents": ["reviewer.md"],
        "skills": ["skill.md"],
        "hooks": [{"hook_type": "session_start", "command": "echo ready"}],
        "mcp_servers": {"docs": {"command": "docs-server", "enabled": True}},
    }
    (pack / "resonant-pack.json").write_text(json.dumps(manifest), encoding="utf-8")
    manager = CapabilityPackManager(tmp_path, roots=[tmp_path / "packs"])
    discovered = manager.discover()
    agent = manager.get_agent_type("pack-reviewer")
    assert discovered[0].trusted and discovered[0].enabled
    assert agent and agent.model_role == "review"
    assert "Validate behavior" in manager.skill_context("validate behavior")
    assert "quality-docs" in manager.mcp_servers()
    assert manager.hook_definitions()[0].hook_type == HookType.SESSION_START


def test_ast_code_intelligence_and_parallel_task_contract():
    parsed = parse_code(
        "import os\nclass Runner:\n    def run(self):\n        return os.getcwd()\n",
        "python",
    )
    assert parsed.parser == "python-ast"
    assert {"Runner", "run"} <= set(parsed.symbols)
    assert "os" in parsed.imports
    assert "os.getcwd" in parsed.calls
    batch = next(tool for tool in AGENT_TOOLS if tool["function"]["name"] == "task_batch")
    assert batch["function"]["parameters"]["properties"]["tasks"]["maxItems"] == 4


def test_gui_exposes_runtime_control_plane_contract():
    """The runtime control plane must be reachable from the UI.

    The backend half checks the dispatch registry rather than grepping
    app.py's source: these handlers now live in gui/ws_commands.py, and a
    substring search over one file asserts where the code sits rather than
    whether the command is actually routable.
    """
    from resonant_client.gui import ws_commands
    from resonant_client.gui.app import websocket_endpoint  # noqa: F401

    root = Path(__file__).parents[1]
    frontend = (root / "resonant_client" / "gui" / "static" / "app.js").read_text(encoding="utf-8")
    endpoint_source = (root / "resonant_client" / "gui" / "app.py").read_text(encoding="utf-8")

    for command in (
        "agent_runtime_control", "session_timeline_restore", "flight_recorder_export",
        "artifact_list", "capability_pack_list", "context_catalog",
    ):
        assert command in ws_commands.HANDLERS or command in endpoint_source, command
        assert command in frontend, command
