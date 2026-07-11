from __future__ import annotations

import time

import pytest

from resonant_client.gui.evaluation_dashboard import EvaluationManager
from resonant_client.smoke.runner import MODELS, SmokeResult
from resonant_client.smoke.variance import VarianceReport


def _report(spec_name: str, model_label: str, *, converged: bool = True):
    run = SmokeResult(
        spec_name=spec_name,
        model_label=model_label,
        model_id=MODELS[model_label],
        started_at_epoch=1.0,
        total_elapsed_seconds=2.0,
        daemon_elapsed_seconds=1.5,
        verdict="satisfied" if converged else "stuck",
        stop_reason="satisfied" if converged else "stuck",
        iter_count=1,
    )
    return VarianceReport.from_runs(
        spec_name=spec_name,
        model_label=model_label,
        model_id=MODELS[model_label],
        runs=[run],
    )


def test_evaluation_runs_in_background_and_persists(tmp_path):
    events = []

    def runner(**kwargs):
        report = _report(kwargs["spec_name"], kwargs["model_label"])
        kwargs["on_run_complete"](1, report.runs[0])
        return report

    manager = EvaluationManager(
        storage_dir=tmp_path / "records",
        runner=runner,
        on_event=events.append,
    )
    started = manager.start(
        model_label="glm", spec_name="minimal", n=1, project_path=tmp_path
    )

    deadline = time.time() + 2
    while manager.snapshot()["active_id"] and time.time() < deadline:
        time.sleep(0.01)

    record = manager.snapshot()["records"][0]
    assert record["id"] == started["id"]
    assert record["status"] == "passed"
    assert record["result"]["convergence_rate"] == 1.0
    assert events
    assert (tmp_path / "records" / f"{started['id']}.json").is_file()


def test_evaluation_validates_inputs_and_prevents_overlap(tmp_path):
    release = False

    def runner(**kwargs):
        deadline = time.time() + 2
        while not release and time.time() < deadline:
            time.sleep(0.01)
        return _report(kwargs["spec_name"], kwargs["model_label"])

    manager = EvaluationManager(storage_dir=tmp_path, runner=runner)
    manager.start(model_label="pro", spec_name="minimal", n=1, project_path=tmp_path)

    with pytest.raises(RuntimeError, match="already running"):
        manager.start(model_label="glm", spec_name="minimal", n=1, project_path=tmp_path)
    with pytest.raises(ValueError, match="Unknown evaluation model"):
        EvaluationManager(storage_dir=tmp_path / "other").start(
            model_label="nope", spec_name="minimal", n=1, project_path=tmp_path
        )
    release = True


def test_interactive_turn_telemetry_is_redacted_persisted_and_summarized(tmp_path):
    manager = EvaluationManager(storage_dir=tmp_path)
    manager.record_turn_telemetry({
        "model": "glm-5.2:cloud",
        "outcome": "incomplete",
        "elapsed_seconds": 4.0,
        "empty_response_attempts": 1,
        "promise_continuations": 2,
        "prompt": "must not persist",
        "response": "must not persist",
    })

    snapshot = manager.snapshot()
    record = snapshot["turn_telemetry"][0]
    assert "prompt" not in record
    assert "response" not in record
    assert record["outcome"] == "incomplete"
    metrics = snapshot["turn_summary"]["by_model"]["glm-5.2:cloud"]
    assert metrics["empty_response_rate"] == 1.0
    assert metrics["incomplete_rate"] == 1.0
    assert metrics["promise_continuations"] == 2
    assert (tmp_path / "turn-telemetry.jsonl").is_file()

    reloaded = EvaluationManager(storage_dir=tmp_path)
    assert reloaded.snapshot()["turn_summary"]["turns"] == 1
