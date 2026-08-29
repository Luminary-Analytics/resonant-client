from __future__ import annotations

import json
from pathlib import Path

import pytest

from resonant_client.engine.director import (
    DirectorBenchmarkStore,
    DirectorConfig,
    DirectorDecision,
    DirectorRun,
    DirectorTaskStatus,
    WorkerPerformanceStore,
    WorkerScheduler,
)
from resonant_client.engine.model_roles import ModelRoleRouter
from resonant_client.engine.session import Session
from resonant_client.gui.sessions import SessionRecord


class _Backend:
    name = "ollama"
    model = "frontier"


def _config() -> DirectorConfig:
    return DirectorConfig.from_dict({
        "enabled": True,
        "director": {
            "backend": "codex",
            "model": "frontier",
            "thinking_mode": "max",
        },
        "workers": [
            {
                "id": "glm",
                "backend": "ollama",
                "model": "glm-5.2:cloud",
                "roles": ["explore", "implement"],
                "max_parallel": 2,
                "quality_weight": 0.9,
            },
            {
                "id": "deepseek",
                "backend": "ollama",
                "model": "deepseek-v4-pro:cloud",
                "roles": ["implement", "test", "review"],
                "quality_weight": 1.0,
            },
        ],
        "max_parallel_workers": 8,
    })


def _tasks() -> list[dict]:
    return [
        {
            "id": "inspect",
            "title": "Inspect",
            "objective": "Map the affected code",
            "role": "explore",
            "agent_type": "explore",
        },
        {
            "id": "implement",
            "title": "Implement",
            "objective": "Implement the feature",
            "role": "implement",
            "agent_type": "build",
            "dependencies": ["inspect"],
            "write_scope": ["src/**"],
            "acceptance_checks": ["pytest"],
        },
    ]


def test_director_config_is_session_local_and_unlimited_by_default():
    config = _config()

    assert config.enabled is True
    assert config.director_model == "frontier"
    assert config.max_parallel_workers == 8
    assert [worker.id for worker in config.workers] == ["glm", "deepseek"]
    assert "max_tokens" not in config.to_dict()
    assert DirectorConfig.from_dict(config.to_dict()).to_dict() == config.to_dict()


def test_director_graph_respects_dependencies_and_persists(tmp_path: Path):
    run = DirectorRun(tmp_path, config=_config(), objective="Build it", root=tmp_path / "runtime")
    run.create_plan(_tasks())

    assert [task.id for task in run.ready_tasks()] == ["inspect"]
    worker = run.select_worker("inspect")
    assert worker.id == "glm"
    run.mark_dispatched("inspect", worker_id=worker.id, agent_id="agt_one")
    run.record_handoff("inspect", {"outcome": "completed", "summary": "Mapped it"})
    run.record_validation("inspect", name="read-only review", passed=True, evidence="paths cited")
    run.decide(DirectorDecision(
        task_id="inspect", action="accept", reason="Evidence is complete",
    ))

    assert [task.id for task in run.ready_tasks()] == ["implement"]
    loaded = DirectorRun.load(tmp_path, run.id, root=tmp_path / "runtime")
    assert loaded.tasks["inspect"].status == DirectorTaskStatus.ACCEPTED.value
    assert loaded.tasks["implement"].status == DirectorTaskStatus.READY.value
    assert json.loads(loaded.path.read_text(encoding="utf-8"))["objective"] == "Build it"


def test_director_acceptance_gate_rejects_unverified_work_and_allows_revision(tmp_path: Path):
    run = DirectorRun(tmp_path, config=_config(), root=tmp_path / "runtime")
    run.create_plan([_tasks()[1] | {"dependencies": []}])
    run.mark_dispatched("implement", worker_id="deepseek")
    run.record_handoff("implement", {
        "outcome": "completed",
        "summary": "Implemented",
        "validation": [],
        "blockers": [],
    }, worktree={"status": "ready", "branch": "resonant/agent/test"})

    with pytest.raises(ValueError, match="No deterministic validation"):
        run.decide({"task_id": "implement", "action": "accept", "reason": "looks good"})

    run.record_validation(
        "implement", name="pytest", passed=False, evidence="1 failed", source="tool.result",
    )
    run.decide({
        "task_id": "implement",
        "action": "revise",
        "reason": "Tests failed",
        "requested_changes": ["Fix the failing test"],
    })
    assert run.tasks["implement"].status == DirectorTaskStatus.REVISION.value

    run.mark_dispatched("implement", worker_id="deepseek", agent_id="agt_two")
    run.record_handoff("implement", {
        "outcome": "completed",
        "summary": "Fixed",
        "validation": ["pytest: passed"],
        "blockers": [],
    })
    run.record_validation(
        "implement", name="pytest", passed=True, evidence="all passed", source="tool.result",
    )
    run.decide({"task_id": "implement", "action": "accept", "reason": "Verified"})
    assert run.tasks["implement"].status == DirectorTaskStatus.ACCEPTED.value
    assert [item.passed for item in run.tasks["implement"].validations] == [False, True]


def test_director_rejects_cycles_and_unknown_dependencies(tmp_path: Path):
    run = DirectorRun(tmp_path, config=_config(), root=tmp_path / "runtime")
    with pytest.raises(ValueError, match="unknown dependencies"):
        run.create_plan([{"id": "a", "objective": "A", "dependencies": ["missing"]}])
    with pytest.raises(ValueError, match="cycle"):
        run.create_plan([
            {"id": "a", "objective": "A", "dependencies": ["b"]},
            {"id": "b", "objective": "B", "dependencies": ["a"]},
        ])


def test_scheduler_honors_eligibility_preference_capacity_and_history(tmp_path: Path):
    performance = WorkerPerformanceStore(tmp_path, root=tmp_path / "runtime")
    scheduler = WorkerScheduler(performance)
    workers = _config().workers

    assert scheduler.select(workers, role="review").id == "deepseek"
    assert scheduler.select(
        workers, role="implement", preferred_worker_id="glm",
    ).id == "glm"
    assert scheduler.select(
        workers, role="implement", active_counts={"glm": 2},
    ).id == "deepseek"

    for _ in range(4):
        performance.record("glm", "implement", accepted=True, elapsed_seconds=30)
        performance.record("deepseek", "implement", accepted=False, elapsed_seconds=60, revisions=1)
    assert scheduler.select(workers, role="implement").id == "glm"


def test_director_complete_requires_every_task_accepted(tmp_path: Path):
    run = DirectorRun(tmp_path, config=_config(), root=tmp_path / "runtime")
    run.create_plan([{"id": "one", "objective": "One", "acceptance_checks": []}])
    with pytest.raises(ValueError, match="unresolved tasks"):
        run.complete()


def test_benchmark_compares_quality_outcomes_without_token_penalties(tmp_path: Path):
    store = DirectorBenchmarkStore(tmp_path, root=tmp_path / "runtime")
    first = store.record(
        mode="single",
        objective="Implement durable retries",
        outcome="answered",
        elapsed_seconds=10,
        steps=4,
        tool_calls=3,
        validation_tools=0,
        changed_files=1,
        provider_stats={"output_tokens": 100},
        quality_score=0.6,
    )
    store.record(
        mode="director",
        objective="Implement durable retries",
        outcome="changed_verified",
        elapsed_seconds=18,
        steps=7,
        tool_calls=9,
        validation_tools=2,
        changed_files=2,
        director_run_id="dir_one",
        provider_stats={"output_tokens": 9000},
        quality_score=0.95,
    )

    comparison = store.comparison(task_key=first["task_key"])
    assert comparison["samples"] == 2
    assert comparison["modes"]["single"]["average_quality_score"] == pytest.approx(0.6)
    assert comparison["modes"]["director"]["average_quality_score"] == pytest.approx(0.95)
    assert comparison["modes"]["director"]["success_rate"] == 1
    assert "output_tokens" not in comparison["modes"]["director"]


def test_worker_performance_records_each_attempt_once(tmp_path: Path):
    run = DirectorRun(tmp_path, config=_config(), root=tmp_path / "runtime")
    run.create_plan([{"id": "one", "objective": "One", "role": "implement"}])
    run.mark_dispatched("one", worker_id="deepseek")
    run.record_handoff("one", {"outcome": "completed"})
    run.record_validation("one", name="pytest", passed=False, evidence="failed")
    run.decide({"task_id": "one", "action": "revise", "reason": "Fix tests"})

    metrics = run.performance.metrics("deepseek", "implement")
    assert metrics["attempts"] == 1
    assert metrics["revisions"] == 1
    assert run.tasks["one"].performance_recorded_attempts == [1]


def test_director_tools_and_parallel_limit_are_opt_in(tmp_path: Path):
    session = Session(_Backend())
    ordinary_names = [tool["function"]["name"] for tool in session.tools]
    assert "director_plan" not in ordinary_names

    session.director_run = DirectorRun(
        tmp_path, config=_config(), root=tmp_path / "runtime",
    )
    tools = {tool["function"]["name"]: tool for tool in session.tools}
    assert "director_plan" in tools
    assert tools["task_batch"]["function"]["parameters"]["properties"]["tasks"]["maxItems"] == 8

    child = Session(_Backend(), parent_session=session)
    child.director_run = session.director_run
    assert "director_plan" not in [tool["function"]["name"] for tool in child.tools]


def test_role_router_routes_an_explicit_worker_without_affecting_role_defaults():
    captured = []
    router = ModelRoleRouter(
        {},
        workers=[{
            "id": "glm-worker",
            "backend_type": "ollama",
            "model": "glm-5.2:cloud",
            "thinking_mode": "max",
        }],
        backend_factory=lambda profile: captured.append(profile) or profile.model,
    )

    assert router.backend_for("implement", _Backend(), worker_id="glm-worker") == "glm-5.2:cloud"
    assert captured[-1].thinking_mode == "max"
    assert router.backend_for("implement", "fallback") == "fallback"

    failing = ModelRoleRouter(
        {},
        workers=[{"id": "broken", "backend_type": "ollama", "model": "missing"}],
        backend_factory=lambda profile: (_ for _ in ()).throw(RuntimeError("offline")),
    )
    with pytest.raises(RuntimeError, match="offline"):
        failing.backend_for("implement", "fallback", worker_id="broken")


def test_reassign_clears_the_old_worker_preference(tmp_path: Path):
    run = DirectorRun(tmp_path, config=_config(), root=tmp_path / "runtime")
    run.create_plan([{
        "id": "one",
        "objective": "One",
        "role": "implement",
        "preferred_worker_id": "glm",
    }])
    run.mark_dispatched("one", worker_id="glm")
    run.record_handoff("one", {"outcome": "completed"})
    run.record_validation("one", name="review", passed=False, evidence="weak")
    run.decide({"task_id": "one", "action": "reassign", "reason": "Use another model"})

    assert run.tasks["one"].preferred_worker_id == ""
    assert run.tasks["one"].assigned_worker_id == ""
    assert run.select_worker("one").id == "deepseek"


def test_session_record_round_trips_director_configuration():
    config = _config().to_dict()
    record = SessionRecord(
        session_id="director-session",
        orchestration_mode="director",
        director_config=config,
        director_run_id="dir_123",
    )

    restored = SessionRecord.from_dict(record.to_dict())
    assert restored.orchestration_mode == "director"
    assert restored.director_config == config
    assert restored.director_run_id == "dir_123"
    assert restored.to_summary()["orchestration_mode"] == "director"


def test_director_gui_contract_is_retired():
    root = Path(__file__).parents[1]
    template = (root / "resonant_client/gui/templates/index.html").read_text(encoding="utf-8")
    script = (root / "resonant_client/gui/static/app.js").read_text(encoding="utf-8")
    styles = (root / "resonant_client/gui/static/styles.css").read_text(encoding="utf-8")

    assert 'id="director-mode-btn"' not in template
    assert 'data-pane="agents"' not in template
    assert 'id="agent-activity-pane"' not in template
    assert "openDirectorComposer()" not in script
    assert "renderDirectorRuntime()" not in script
    assert "director_configure" not in script


def test_plan_updates_preserve_active_and_accepted_work(tmp_path: Path):
    run = DirectorRun(tmp_path, config=_config(), root=tmp_path / "runtime")
    run.create_plan([
        {"id": "active", "objective": "Original", "role": "implement"},
        {"id": "later", "objective": "Later", "dependencies": ["active"]},
    ])
    run.mark_dispatched("active", worker_id="deepseek", agent_id="agt_one")

    with pytest.raises(ValueError, match="cannot remove active"):
        run.create_plan([{"id": "replacement", "objective": "Replacement"}])

    run.create_plan([
        {"id": "active", "objective": "Updated scope", "role": "implement"},
        {"id": "later", "objective": "Later", "dependencies": ["active"]},
        {"id": "new", "objective": "New independent work"},
    ])
    active = run.tasks["active"]
    assert active.status == DirectorTaskStatus.RUNNING.value
    assert active.agent_ids == ["agt_one"]
    assert active.objective == "Original"
    assert run.tasks["new"].status == DirectorTaskStatus.READY.value
