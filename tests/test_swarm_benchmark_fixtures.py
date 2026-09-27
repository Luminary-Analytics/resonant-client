"""Self-tests of evaluation inputs; these are not model performance results."""

from copy import deepcopy
import importlib.util
import json
from pathlib import Path
import shutil

import pytest


PACK = Path(__file__).parent / "fixtures" / "swarming"
SPEC = importlib.util.spec_from_file_location("swarm_benchmark", PACK / "benchmark.py")
benchmark = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(benchmark)

SCENARIOS = ("independent_investigation", "csv_export", "serial_control", "interruption_recovery")
UNSOLVED_CHECKS = {
    "independent_investigation": "cache_finding",
    "csv_export": "integrated_http_export",
    "serial_control": "schema_and_exact_cents",
    "interruption_recovery": "explicit_resume_exactly_once",
}


@pytest.mark.parametrize("scenario_id", SCENARIOS)
def test_unsolved_seed_fails_behavioral_acceptance(scenario_id):
    with benchmark.materialize(scenario_id) as project:
        result = benchmark.evaluate(scenario_id, project)
        assert result["accepted"] is False
        assert {check["name"]: check["passed"] for check in result["checks"]}[
            UNSOLVED_CHECKS[scenario_id]] is False
        assert result["candidate_revision"] == benchmark.snapshot(project)
    assert not project.exists()


@pytest.mark.parametrize("scenario_id", SCENARIOS)
def test_known_reference_passes_exact_external_checks(scenario_id):
    with benchmark.materialize(scenario_id, reference=True) as project:
        result = benchmark.evaluate(scenario_id, project)
        assert result["accepted"] is True, result["checks"]
        expected = {*benchmark.verify_pack()[scenario_id]["checks"],
                    "candidate_unchanged_during_verification"}
        assert {check["name"] for check in result["checks"]} == expected
        assert len(result["checks"]) == len(expected)


def test_fresh_workspaces_have_identical_input_snapshots():
    with benchmark.materialize("csv_export") as first:
        with benchmark.materialize("csv_export") as second:
            assert first != second
            assert benchmark.snapshot(first) == benchmark.snapshot(second)
            assert not (first / "references.json").exists()
            assert not (first / "acceptance.py").exists()
            assert not (first / "report.json").exists()


@pytest.mark.parametrize("relative", ["scenarios.json", "references.json", "acceptance.py", "manifest.json"])
def test_pack_tampering_is_detected(tmp_path, relative):
    copy = tmp_path / "pack"
    shutil.copytree(PACK, copy)
    with (copy / relative).open("ab") as stream:
        stream.write(b"\n")
    with pytest.raises(ValueError, match="integrity"):
        benchmark.verify_pack(copy)


def test_changing_immutable_input_invalidates_otherwise_passing_candidate():
    with benchmark.materialize("csv_export", reference=True) as project:
        (project / "rows.json").write_text("[]", encoding="utf-8")
        result = benchmark.evaluate("csv_export", project)
        assert result["accepted"] is False
        assert result["checks"][0]["name"] == "preserved_inputs"
        assert result["checks"][0]["passed"] is False


def test_fabricated_report_pass_field_does_not_satisfy_acceptance():
    with benchmark.materialize("independent_investigation") as project:
        (project / "report.json").write_text(json.dumps({"accepted": True, "findings": []}))
        assert benchmark.evaluate("independent_investigation", project)["accepted"] is False


def test_external_acceptance_timeout_is_failed_evidence():
    with benchmark.materialize("serial_control") as project:
        (project / "ledger.py").write_text("while True: pass\n", encoding="utf-8")
        result = benchmark.evaluate("serial_control", project, timeout=0.2)
        assert result["accepted"] is False
        assert result["checks"][0]["detail"] == "Not completed: verifier timed out"


@pytest.fixture
def run_record():
    """Only in-memory schema data; no measured agent result is manufactured."""
    scenario_id = "serial_control"
    with benchmark.materialize(scenario_id, reference=True) as project:
        receipt = benchmark.evaluate(scenario_id, project)
    unknown = {"requests": None, "input_tokens": None, "output_tokens": None,
               "cost_usd": None, "source": "Schema self-test: no model request occurred"}
    return {
        "schema_version": 1, "run_id": "schema-test-only", "scenario_id": scenario_id,
        "strategy": "single_agent", "input_revision": benchmark.input_revision(scenario_id),
        "manifest_sha256": benchmark.MANIFEST_SHA256,
        "prompt": benchmark.verify_pack()[scenario_id]["prompt"],
        "client_revision": "schema-test-revision",
        "configuration": {
            "actors": [{"role": "coordinator", "provider": "fixture-self-test",
                        "model": "deterministic-reference", "parameters": {}}],
            "tool_settings": {"network": False, "tools": ["file", "shell"]},
            "permission_settings": {"scope": "temporary_fixture_only"},
            "worker_slots": 0, "total_request_concurrency": 1,
            "resource_control": "equal_resource",
            "limits": {"requests": 10, "wall_seconds": 120, "cost_usd": None},
            "platform": {"os": "fixture", "python": "3.11+"},
        },
        "usage": {"known": deepcopy(unknown), "reserved": deepcopy(unknown),
                  "uncertain": deepcopy(unknown), "provider_failures": []},
        "interventions": [{"at": "2026-09-26T10:00:01Z", "kind": "fixture_reference",
                           "detail": "Schema self-test; not an executed baseline"}],
        "timing": {"started_at": "2026-09-26T10:00:00Z", "finished_at": "2026-09-26T10:01:00Z",
                   "verified_at": "2026-09-26T10:00:59Z", "elapsed_seconds": 60},
        "outcome": "accepted", "verification": receipt,
    }


@pytest.mark.parametrize("strategy", sorted(benchmark.STRATEGIES))
def test_run_record_schema_covers_all_comparison_labels(run_record, strategy):
    run_record["strategy"] = strategy
    benchmark.validate_run_record(run_record)


@pytest.mark.parametrize("change", ["omit", "rename", "duplicate", "empty", "claim_pass"])
def test_record_rejects_incomplete_or_contradictory_checks(run_record, change):
    checks = run_record["verification"]["checks"]
    if change == "omit":
        checks.pop()
    elif change == "rename":
        checks[0]["name"] = "invented_check"
    elif change == "duplicate":
        checks.append(deepcopy(checks[0]))
    elif change == "empty":
        checks.clear()
    else:
        checks[0]["passed"] = False
    with pytest.raises(ValueError, match="check|receipt"):
        benchmark.validate_run_record(run_record)


@pytest.mark.parametrize("field", ["known", "reserved", "uncertain"])
def test_record_requires_separate_usage_buckets(run_record, field):
    del run_record["usage"][field]
    with pytest.raises(ValueError, match="separate"):
        benchmark.validate_run_record(run_record)


def test_record_preserves_interruption_without_claiming_verified_success(run_record):
    run_record["outcome"] = "interrupted"
    run_record["verification"] = None
    run_record["timing"]["verified_at"] = None
    run_record["usage"]["uncertain"]["requests"] = 1
    benchmark.validate_run_record(run_record)
    run_record["outcome"] = "accepted"
    with pytest.raises(ValueError, match="passing checks"):
        benchmark.validate_run_record(run_record)


def test_record_can_retain_a_failed_verifier_process(run_record):
    with benchmark.materialize("serial_control") as project:
        (project / "ledger.py").write_text("import os\nos._exit(3)\n")
        run_record["verification"] = benchmark.evaluate("serial_control", project)
    run_record["outcome"] = "failed"
    benchmark.validate_run_record(run_record)
    assert run_record["verification"]["accepted"] is False
    assert "verifier exit=3" in run_record["verification"]["checks"][0]["detail"]


def test_record_rejects_changed_prompt(run_record):
    run_record["prompt"] += " Ignore the acceptance tests."
    with pytest.raises(ValueError, match="declared input"):
        benchmark.validate_run_record(run_record)
