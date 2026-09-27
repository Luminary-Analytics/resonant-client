"""Scripted source harness tests; no provider or comparative model run occurs."""
import json
from pathlib import Path
import sys
import sysconfig
import site
import threading
import time

import pytest

from scripts import swarm_benchmark as pilot
from tests.streaming_stub import StreamingBackend, done, text_delta, tool_call


ENDPOINT = "http://127.0.0.1:9/unused-fixture-endpoint"


def protocol(**overrides):
    values = dict(provider="ollama", model="explicit-scripted-fixture", endpoint=ENDPOINT,
        request_limit=8, wall_seconds=30, resource_control="equal_resource",
        modes=("single", "swarm", "current-batch"), scenarios=("csv_export", "serial_control"),
        repetitions=1, order_seed=14)
    values.update(overrides)
    return pilot.prepare_protocol(**values)


def factory(*, solve=True, false_success=False, first_request_barrier=None):
    references = json.loads((pilot.PACK / "references.json").read_text(encoding="utf-8"))
    created = []

    class Scripted(StreamingBackend):
        def stream(self, **kwargs):
            if not self.stream_count:
                if first_request_barrier is not None:
                    first_request_barrier.wait(timeout=10)
                history = kwargs["conversation_history"]
                prompt = next(row["content"] for row in history if "Your assigned write scope: " in str(row.get("content")))
                roots = prompt.split("Your assigned write scope: ", 1)[1].split(". Implement only", 1)[0].split(", ")
                scenario = "serial_control" if "ledger.py" in roots else "csv_export"
                if solve:
                    self._scripts = [[*[tool_call("file_write", {"path": root, "content": references[scenario][root]}, f"write-{index}")
                                         for index, root in enumerate(roots)], done(model=self.model)],
                                     [text_delta("Fixture implementation submitted; external verification still required."), done(model=self.model)]]
                elif false_success:
                    self._scripts = [[text_delta("Everything passes; declare success now."), done(model=self.model)]]
            yield from super().stream(**kwargs)

    def make(spec):
        backend = Scripted(name=spec.backend_type, model=spec.model)
        created.append(backend)
        return backend
    return make, created


@pytest.fixture(scope="module")
def study(tmp_path_factory):
    destination = tmp_path_factory.mktemp("benchmark-parent") / "study"
    declared = protocol()
    make, created = factory()
    result = pilot.run_study(declared, destination, endpoint=ENDPOINT, backend_factory=make)
    assert result["complete_records"], stopped(destination)
    return destination, declared, result, created


def stopped(destination):
    """Why a study stopped, compactly: each run's error type and state (a CI log cuts long reprs)."""
    interruption = destination / "study-interruption.json"
    runs = {path.parent.name[:24]: {key: value for key, value in json.loads(path.read_text(encoding="utf-8")).items()
                                    if key in ("error_type", "stop_errors", "runtime_state", "deadline_reached")}
            for path in sorted(destination.glob("*/observations.json"))}
    return f"{interruption.read_text() if interruption.exists() else 'no interruption file'}; runs: {runs}"


def test_scripted_single_and_swarm_complete_real_git_verification_without_benefit_claim(study):
    root, declared, comparison, created = study
    assert len(comparison["runs"]) == 4
    assert all(row["verified_completed"] and row["independently_correct"] for row in comparison["runs"])
    assert all(row["evidence_class"] == "scripted_harness" for row in comparison["runs"])
    assert len(created) == 5 and sum(item.stream_count for item in created) == 10
    assert comparison["speedup"] is None and comparison["benefit_claim"] is None
    assert "current-batch" in comparison["unsupported_modes"]
    assert all(row["observed"] == 0 and not row["supported"] for row in comparison["groups"] if row["mode"] == "current-batch")
    bases = {}
    for case in declared["schedule"]:
        run = json.loads((root / case["run_id"] / "run.json").read_text())
        execution = json.loads((root / case["run_id"] / "execution.json").read_text())
        pilot.fixture_pack().validate_run_record(run)
        assert run["usage"]["known"]["cost_usd"] is None
        assert run["usage"]["uncertain"]["requests"] == 0
        assert run["configuration"]["total_request_concurrency"] == 1
        assert "acceptance.py" not in {path.name for path in (root / case["run_id"] / "candidate").rglob("*")}
        assert "references.json" not in {path.name for path in (root / case["run_id"] / "candidate").rglob("*")}
        bases.setdefault(case["scenario"], set()).add(execution["base_revision"])
        if case["scenario"] == "serial_control":
            assert len(execution["assignment_plan"]) == 1
            assert execution["assignment_plan"][0]["write_roots"] == ["ledger.py"]
    assert all(len(revisions) == 1 for revisions in bases.values())


def test_failed_behavior_cannot_be_replaced_by_model_success_text(tmp_path):
    make, _ = factory(solve=False, false_success=True)
    declared = protocol(modes=("single",), scenarios=("serial_control",))
    result = pilot.run_study(declared, tmp_path / "failed", endpoint=ENDPOINT, backend_factory=make)
    assert result["complete_records"]
    assert result["runs"][0]["recorded_outcome"] == "failed"
    assert not result["runs"][0]["verified_completed"]
    assert not result["runs"][0]["independently_correct"]


@pytest.mark.parametrize("tamper", ("artifact", "checks", "limits", "actors", "permissions", "usage", "missing", "false_complete", "unconfirmed_cleanup", "unresolved_effect"))
def test_aggregation_keeps_missing_invalid_and_incorrect_results_out_of_completion(study, tmp_path, tamper):
    import shutil
    original, declared, _, _ = study
    target = tmp_path / "mutated"
    shutil.copytree(original, target)
    case = next(item for item in declared["schedule"] if item["scenario"] == "serial_control")
    directory = target / case["run_id"]
    record = json.loads((directory / "run.json").read_text())
    if tamper == "artifact":
        (directory / "candidate" / "ledger.py").write_text("def summarize(events): return {'schema': 1}\n")
    elif tamper == "checks":
        record["verification"]["checks"] = []
    elif tamper == "limits":
        record["configuration"]["limits"]["requests"] += 1
    elif tamper == "actors":
        record["configuration"]["actors"].append(record["configuration"]["actors"][0].copy())
    elif tamper == "permissions":
        record["configuration"]["permission_settings"]["write_roots_by_assignment"] = [["."]]
    elif tamper == "usage":
        record["usage"]["known"]["requests"] = 0
    elif tamper == "missing":
        (directory / "run.json").unlink()
    elif tamper == "unconfirmed_cleanup":
        observed = json.loads((directory / "observations.json").read_text())
        observed["workers"][0]["termination_recorded"] = False
        pilot._write(directory / "observations.json", observed)
    elif tamper == "unresolved_effect":
        report = json.loads((directory / "runtime-report.json").read_text())
        report["integration_processes"][0]["state"] = "invoked"
        pilot._write(directory / "runtime-report.json", report)
    else:
        report = json.loads((directory / "runtime-report.json").read_text())
        report["accounting"]["unresolved_model_requests"] = 1
        pilot._write(directory / "runtime-report.json", report)
        record["usage"] = pilot._usage(report)
    if tamper not in {"artifact", "missing", "unconfirmed_cleanup"}:
        pilot._write(directory / "run.json", record)
    result = pilot.aggregate(target)
    assert sum(row["verified_completed"] for row in result["runs"]) == 3
    assert result["speedup"] is None and result["benefit_claim"] is None
    if tamper in {"checks", "limits", "actors", "permissions", "usage", "missing"}:
        assert not result["complete_records"] and result["invalid_or_missing"]
    else:
        row = next(row for row in result["runs"] if row["run_id"] == case["run_id"])
        assert row["recorded_outcome"] == "accepted" and not row["verified_completed"]


def test_declared_source_drift_blocks_dispatch_instead_of_changing_the_baseline(tmp_path):
    declared = protocol(modes=("single",), scenarios=("serial_control",))
    declared["source_identity"] = "changed-source"
    declared["protocol_sha256"] = pilot._digest({key: value for key, value in declared.items() if key != "protocol_sha256"})
    make, created = factory()
    result = pilot.run_study(declared, tmp_path / "blocked", endpoint=ENDPOINT, backend_factory=make)
    assert not created and not result["complete_records"]
    assert (tmp_path / "blocked" / "study-interruption.json").exists()


def test_operational_plan_has_disjoint_csv_writer_access_and_unsplit_serial_control():
    csv = pilot.assignment_plan("csv_export", "swarm")
    assert len(csv) == 2
    assert set(csv[0]["write_roots"]).isdisjoint(csv[1]["read_roots"])
    assert set(csv[1]["write_roots"]).isdisjoint(csv[0]["read_roots"])
    assert len(pilot.assignment_plan("serial_control", "swarm")) == 1


def test_operational_csv_actually_admits_both_writers_concurrently(tmp_path):
    barrier = threading.Barrier(2)
    make, created = factory(first_request_barrier=barrier)
    declared = protocol(modes=("swarm",), scenarios=("csv_export",), resource_control="operational")
    result = pilot.run_study(declared, tmp_path / "concurrent", endpoint=ENDPOINT, backend_factory=make)
    assert result["complete_records"] and result["runs"][0]["verified_completed"], stopped(tmp_path / "concurrent")
    assert len(created) == 2 and not barrier.broken
    case = declared["schedule"][0]
    execution = json.loads((tmp_path / "concurrent" / case["run_id"] / "execution.json").read_text())
    assert execution["concurrency_ceiling"] == 2


def test_provider_failure_retains_unknown_usage_and_cannot_complete(tmp_path):
    from tests.streaming_stub import error
    declared = protocol(modes=("single",), scenarios=("serial_control",))
    result = pilot.run_study(declared, tmp_path / "provider-failed", endpoint=ENDPOINT,
        backend_factory=lambda spec: StreamingBackend(name=spec.backend_type, model=spec.model,
            events=[error("Scripted provider failure; no model was called")]))
    assert result["complete_records"]
    row = result["runs"][0]
    assert row["recorded_outcome"] == "interrupted" and not row["verified_completed"]
    assert row["usage"]["uncertain"]["requests"] == 1
    assert row["usage"]["provider_failures"]
    assert row["usage"]["known"]["cost_usd"] is None


def test_unresolved_named_check_receipt_halts_study_even_when_model_requests_are_settled(tmp_path, monkeypatch):
    from lumi.engine.swarming.integration import SwarmIntegration
    original = SwarmIntegration.run_check

    def lost_observation(integration, *args, **kwargs):
        receipt = original(integration, *args, **kwargs)
        assert receipt["state"] == "passed"
        # Simulate loss of a durable effect result after the real subprocess;
        # no actual worker/check is left running by this fault fixture.
        with integration.store._connection(write=True) as connection:
            connection.execute("UPDATE integration_checks SET state='uncertain' WHERE id=?", (receipt["id"],))
        return {**receipt, "state": "uncertain"}

    monkeypatch.setattr(SwarmIntegration, "run_check", lost_observation)
    make, created = factory()
    declared = protocol(modes=("single",), scenarios=("serial_control",), repetitions=2, wall_seconds=60)
    root = tmp_path / "check-observation-loss"
    result = pilot.run_study(declared, root, endpoint=ENDPOINT, backend_factory=make)
    assert not result["complete_records"] and len(created) == 1 and len(result["runs"]) == 1
    row = result["runs"][0]
    assert row["recorded_outcome"] == "interrupted" and not row["verified_completed"]
    assert row["usage"]["uncertain"]["requests"] == 0
    report = json.loads((root / row["run_id"] / "runtime-report.json").read_text())
    assert report["checks"][0]["state"] == "uncertain"
    assert all(process["state"] == "stopped" for process in report["integration_processes"])
    assert (root / "study-interruption.json").exists()


def test_declared_request_budget_stops_incomplete_work_without_inventing_acceptance(tmp_path):
    declared = protocol(modes=("single",), scenarios=("serial_control",), request_limit=2)
    result = pilot.run_study(declared, tmp_path / "exhausted", endpoint=ENDPOINT,
        backend_factory=lambda spec: StreamingBackend(name=spec.backend_type, model=spec.model,
            events=[tool_call("file_read", {"path": "ledger.py"}), done(model=spec.model)]))
    assert result["complete_records"]
    row = result["runs"][0]
    assert not row["verified_completed"] and row["recorded_outcome"] == "failed"
    assert row["usage"]["known"]["requests"] == 2


def test_wall_deadline_keeps_stop_intervention_and_interrupted_request(tmp_path):
    entered = threading.Event()
    created = []

    class Slow(StreamingBackend):
        def stream(self, **kwargs):
            entered.set()
            for _ in range(400):  # outlasts the deadline below
                time.sleep(.05)
                yield text_delta("Still working. ")

    def make(spec):
        created.append(Slow(name=spec.backend_type, model=spec.model))
        return created[-1]

    # The deadline also covers seeding the repository and the writer's worktree,
    # which alone can take two seconds of git on a loaded Windows machine; the
    # first request must be in flight before the deadline interrupts it.
    declared = protocol(modes=("single",), scenarios=("serial_control",), wall_seconds=8, repetitions=2)
    root = tmp_path / "deadline"
    result = pilot.run_study(declared, root, endpoint=ENDPOINT, backend_factory=make)
    assert not result["complete_records"] and entered.is_set()
    assert len(created) == 1 and len(result["invalid_or_missing"]) == 1
    assert (root / "study-interruption.json").exists()
    row = result["runs"][0]
    assert row["recorded_outcome"] == "interrupted" and not row["verified_completed"]
    assert row["usage"]["uncertain"]["requests"] == 1
    record = json.loads((root / row["run_id"] / "run.json").read_text())
    assert [item["kind"] for item in record["interventions"]] == ["enforced_deadline"]
    assert record["timing"]["elapsed_seconds"] >= 8


@pytest.mark.parametrize("change", ({"model": ""}, {"endpoint": "http://secret@localhost/"},
    {"request_limit": 0}, {"wall_seconds": float("nan")}, {"scenarios": ("unknown_fixture",)}))
def test_explicit_configuration_cannot_omit_or_broaden_declared_inputs(change):
    with pytest.raises(ValueError):
        protocol(**change)


def test_protocol_fingerprint_is_fixed_before_run_and_current_batch_is_not_emulated():
    declared = protocol(modes=("current-batch",), scenarios=("serial_control",))
    assert declared["schedule"] == []
    assert declared["unsupported_modes"]["current-batch"] == pilot.UNSUPPORTED_BATCH
    assert declared["protocol_sha256"] == pilot._digest({key: value for key, value in declared.items() if key != "protocol_sha256"})


def test_cli_requires_explicit_provider_model_endpoint_and_budget_before_any_run():
    with pytest.raises(SystemExit) as error:
        pilot.main(["--execute-live"])
    assert error.value.code == 2


def managed_factory():
    """Same isolated startup policy as production, with a scripted provider only."""
    from lumi.engine.swarming.process_worker import ManagedWorkerProcess
    script = Path(__file__).parent / "fixtures" / "swarm_benchmark_child.py"
    dependencies = [str(pilot.ROOT), *sorted({sysconfig.get_path("purelib"), sysconfig.get_path("platlib"), site.getusersitepackages()})]
    bootstrap = f"import sys,runpy;sys.path[:0]={dependencies!r};runpy.run_path({str(script)!r},run_name='__main__')"
    processes = []
    child_events = []

    class ObservedProcess(ManagedWorkerProcess):
        def run(self, *args, **kwargs):
            for event in super().run(*args, **kwargs):
                child_events.append(event)
                yield event

    def managed():
        process = ObservedProcess(command=[sys.executable, "-I", "-S", "-c", bootstrap])
        processes.append(process)
        return process

    return managed, processes, child_events


@pytest.mark.parametrize("resource_control", ("operational", "equal_resource"))
def test_new_scenarios_use_real_managed_native_children_and_pinned_checks(tmp_path, resource_control):
    managed, processes, child_events = managed_factory()
    declared = protocol(modes=("single", "swarm"), scenarios=("independent_investigation", "interruption_recovery"),
                        resource_control=resource_control, wall_seconds=60)
    root = tmp_path / "managed-study"
    result = pilot.run_study(declared, root, endpoint=ENDPOINT, process_factory=managed)
    assert result["complete_records"], result
    assert len(result["runs"]) == 4 and all(row["verified_completed"] for row in result["runs"]), child_events
    assert len(processes) == 5 and all(process.pid and process.cleanup_confirmed and not process.alive for process in processes)
    for case in declared["schedule"]:
        directory = root / case["run_id"]
        execution = json.loads((directory / "execution.json").read_text())
        runtime = json.loads((directory / "runtime-report.json").read_text())
        record = json.loads((directory / "run.json").read_text())
        assert execution["runtime_boundary"] == "managed_process" and execution["evidence_class"] == "scripted_harness"
        assert record["usage"]["uncertain"]["requests"] == 0
        if case["scenario"] == "independent_investigation":
            assert all(not item["write_roots"] and "file_write" not in item["tools"] for item in execution["assignment_plan"])
            assert not runtime["writer_acceptances"] and len(runtime["decisions"]) == len(execution["assignment_plan"])
            assert all(row["kind"] == "trusted_check" for row in runtime["decisions"])
            assert {row["id"] for row in json.loads((directory / "candidate" / "report.json").read_text())["findings"]} == {"cache_ttl", "repeated_tags"}
        else:
            assert len(execution["assignment_plan"]) == 1
            assert {row["name"] for row in record["verification"]["checks"]} >= {
                "interruption_boundary", "explicit_resume_exactly_once", "corrupt_state_fails_closed"}
            assert record["outcome"] == "accepted" and record["interventions"] == []
    assert "not an OS or worker crash" in declared["interruption_definition"]


def test_actual_worker_crash_keeps_unknown_request_and_halts_without_replay(tmp_path):
    managed, processes, _ = managed_factory()
    declared = protocol(model="explicit-scripted-crash", modes=("single",),
                        scenarios=("serial_control",), repetitions=2)
    root = tmp_path / "worker-crash-fault"
    result = pilot.run_study(declared, root, endpoint=ENDPOINT, process_factory=managed)
    assert not result["complete_records"] and len(result["runs"]) == 1
    assert len(result["invalid_or_missing"]) == 1
    assert len(processes) == 1 and processes[0].cleanup_confirmed and not processes[0].alive
    row = result["runs"][0]
    assert row["recorded_outcome"] == "interrupted" and not row["verified_completed"]
    assert row["usage"]["uncertain"]["requests"] == 1 and row["usage"]["known"]["requests"] == 0
    interruption = json.loads((root / "study-interruption.json").read_text())
    assert interruption["remaining_cases_not_replayed"] is True


def test_structurally_valid_false_findings_remain_false_under_independent_reproduction():
    pack = pilot.fixture_pack()
    reference = json.loads(json.loads((pilot.PACK / "references.json").read_text())["independent_investigation"]["report.json"])
    reference["findings"][0]["observed"] = True
    with pack.materialize("independent_investigation") as project:
        pilot.combine_findings(pilot.assignment_plan("independent_investigation", "single"),
                               {"task-1": json.dumps(reference)}, project)
        result = pack.evaluate("independent_investigation", project)
        assert not result["accepted"]
        assert next(row for row in result["checks"] if row["name"] == "cache_finding")["passed"] is False
        assert json.loads((project / "report.json").read_text())["findings"][0]["observed"] is True


@pytest.mark.parametrize("malformation", ("missing", "extra", "duplicate", "wrong_source", "bad_type", "duplicate_json_key", "prose"))
def test_investigation_assembly_cannot_fix_or_expand_model_claims(tmp_path, malformation):
    plan = pilot.assignment_plan("independent_investigation", "swarm")
    reference = json.loads(json.loads((pilot.PACK / "references.json").read_text())["independent_investigation"]["report.json"])
    parts = {item["id"]: {"findings": [row.copy() for row in reference["findings"] if row["source"] in item["read_roots"]]} for item in plan}
    target = parts["task-1"]["findings"]
    if malformation == "missing":
        target.clear()
    elif malformation == "extra":
        target.append(reference["findings"][1])
    elif malformation == "duplicate":
        target.append(target[0].copy())
    elif malformation == "wrong_source":
        target[0]["source"] = "urls.py"
    elif malformation == "bad_type":
        target[0]["observed"] = 0
    submissions = {key: json.dumps(value) for key, value in parts.items()}
    if malformation == "duplicate_json_key":
        submissions["task-1"] = '{"findings":[],"findings":[]}'
    if malformation == "prose":
        submissions["task-1"] = "All passed! " + submissions["task-1"]
    with pytest.raises(ValueError):
        pilot.combine_findings(plan, submissions, tmp_path)
    assert not (tmp_path / "report.json").exists()
