"""Source-only controlled native benchmark; never an automatic paid pilot.

The CLI prepares a pinned protocol by default. --execute-live explicitly invokes
the selected native provider. Tests inject scripted backends; those records are
harness self-tests and cannot establish speedup or model quality. This harness
executes candidate Python for verification with the operator's permissions.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
import platform
import random
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from urllib.parse import urlsplit
import uuid

ROOT = Path(__file__).resolve().parents[1]
PACK = ROOT / "tests" / "fixtures" / "swarming"
SCENARIOS = ("csv_export", "serial_control", "independent_investigation", "interruption_recovery")
MODES = {"single": "single_agent", "current-batch": "batch_delegation", "swarm": "swarm"}
TOOLS = ("file_read", "file_write", "file_edit", "glob", "grep", "artifact_read", "swarm_status", "swarm_send", "swarm_receive")
READ_TOOLS = tuple(tool for tool in TOOLS if tool not in {"file_write", "file_edit"})
# Native adapters, plus any OpenAI-compatible Chat Completions endpoint (NVIDIA
# NIM, vLLM, a gateway), run as a Lumi connection (engine/swarming/connections.py).
PROVIDERS = ("ollama", "exo", "kimi", "openrouter", "sonn", "openai-compatible")
UNSUPPORTED_BATCH = "Native task_batch exists, but this harness has no equivalent bounded accounting/integration adapter; no batch execution is emulated."


class HarnessStop(RuntimeError):
    """A stop this harness declares itself.

    Its text is written here, never taken from a provider or a command, so the
    case's observations can keep it as the reason the case stopped.
    """


def fixture_pack():
    spec = importlib.util.spec_from_file_location("swarm_benchmark_fixtures", PACK / "benchmark.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _digest(value):
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _write(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def _now():
    return datetime.now(timezone.utc).isoformat()


def source_identity():
    """Pin executable source, including uncommitted/new runtime modules."""
    paths = [*ROOT.joinpath("lumi").rglob("*.py"), Path(__file__).resolve()]
    contents = {path.relative_to(ROOT).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
                for path in sorted(paths)}
    commit = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "HEAD"], capture_output=True,
                            text=True, check=True, timeout=10).stdout.strip()
    return f"{commit}+source-sha256:{_digest(contents)}"


def prepare_protocol(*, provider, model, endpoint, request_limit, wall_seconds, resource_control,
                     modes, scenarios, repetitions, order_seed, thinking_mode=""):
    """Validate predeclared inputs; does not construct a backend or call a model."""
    pack = fixture_pack()
    fixtures = pack.verify_pack()
    if provider not in PROVIDERS or not isinstance(model, str) or not model.strip():
        raise ValueError("Select an explicit supported provider and model")
    url = urlsplit(endpoint)
    if url.scheme not in {"http", "https"} or not url.hostname or url.username or url.password or url.query or url.fragment:
        raise ValueError("An explicit HTTP endpoint without embedded credentials/query/fragment is required")
    if provider == "openrouter" and endpoint.rstrip("/") != "https://openrouter.ai/api/v1":
        raise ValueError("The native OpenRouter adapter requires its canonical API endpoint")
    if type(request_limit) is not int or not 2 <= request_limit <= 1000:
        raise ValueError("Request budget must be an explicit integer from 2 to 1000")
    if type(wall_seconds) not in (int, float) or not math.isfinite(wall_seconds) or not 1 <= wall_seconds <= 1200:
        raise ValueError("Wall cancellation deadline must be explicit, from 1 to 1200 seconds")
    if resource_control not in {"operational", "equal_resource"}:
        raise ValueError("Declare operational or equal_resource control")
    if not modes or len(set(modes)) != len(modes) or set(modes) - MODES.keys():
        raise ValueError("Declare unique single/current-batch/swarm modes")
    if not scenarios or len(set(scenarios)) != len(scenarios) or set(scenarios) - set(SCENARIOS):
        raise ValueError("Select only the four pinned fixture scenarios")
    if type(repetitions) is not int or not 1 <= repetitions <= 100 or type(order_seed) is not int:
        raise ValueError("Declare 1..100 repetitions and an integer ordering seed")
    if thinking_mode not in {"", "off", "low", "med", "high", "max", "default"}:
        raise ValueError("Unsupported explicit thinking mode")
    schedule = [{"run_id": uuid.uuid4().hex, "scenario": scenario, "mode": mode, "repetition": repeat}
                for repeat in range(1, repetitions + 1) for scenario in scenarios for mode in modes if mode != "current-batch"]
    random.Random(order_seed).shuffle(schedule)
    protocol = {"format": "sonn-swarm-benchmark-protocol", "version": 1,
        "source_identity": source_identity(), "manifest_sha256": pack.MANIFEST_SHA256,
        "provider": provider, "model": model, "endpoint_sha256": hashlib.sha256(endpoint.encode()).hexdigest(),
        "endpoint_origin": f"{url.scheme}://{url.netloc}", "thinking_mode": thinking_mode,
        "request_limit": request_limit, "wall_seconds": wall_seconds, "cost_limit_usd": None,
        "resource_control": resource_control, "repetitions": repetitions, "order_seed": order_seed,
        "modes": list(modes), "scenarios": list(scenarios),
        "unsupported_modes": {"current-batch": UNSUPPORTED_BATCH} if "current-batch" in modes else {},
        "inputs": {scenario: {"input_revision": pack.input_revision(scenario), "checks": fixtures[scenario]["checks"]}
                   for scenario in scenarios}, "schedule": schedule,
        "single_definition": "One guarded native Session using the scenario's read/write scope; not unrestricted chat or CLI execution",
        "swarm_definition": "Two CSV writers or investigation readers; deliberately one unsplit serial/control or processor-repair writer; no model coordinator",
        "interruption_definition": "Pinned processor repair checks inject an application exception after a durable effect, then explicitly resume; this is not an OS or worker crash",
        "benefit_threshold": None, "benefit_claim_permitted": False}
    protocol["protocol_sha256"] = _digest(protocol)
    return protocol


def assignment_plan(scenario_id, mode):
    scenario = fixture_pack().verify_pack()[scenario_id]
    if scenario_id == "independent_investigation":
        parts = [("cache_ttl", "cache.py"), ("repeated_tags", "urls.py")]
        assignments = [[part] for part in parts] if mode == "swarm" else [parts]
        return [{"id": f"task-{index}", "objective": scenario["prompt"]
            + "\n\nControlled read-only assignment. Return exactly one JSON object with a findings array as your final response instead of writing report.json. "
            + "The trusted host materializes the exact combined report and independently reproduces its observations. "
            + "Assigned finding IDs: " + ", ".join(part[0] for part in assignment) + ". Include only those findings; no prose wrappers.",
            "role": "explore", "read_roots": [part[1] for part in assignment], "write_roots": [],
            "tools": list(READ_TOOLS), "criteria": ["fixture_acceptance"]} for index, assignment in enumerate(assignments, 1)]
    partitions = [[path] for path in scenario["editable"]] if mode == "swarm" and scenario_id == "csv_export" else [scenario["editable"]]
    return [{"id": f"task-{index}", "objective": scenario["prompt"] + "\n\nYour assigned write scope: " + ", ".join(roots)
             + ". Implement only this scope. The trusted host combines results and verifies the complete contract.",
             "role": "implement", "read_roots": sorted(set(roots) | (set(scenario["seed"]) - set(scenario["editable"]))), "write_roots": roots,
             "tools": list(TOOLS), "criteria": ["fixture_acceptance"]} for index, roots in enumerate(partitions, 1)]


def combine_findings(plan, submissions, project):
    """Materialize only strict model-submitted findings, never an oracle answer."""
    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Duplicate finding JSON key")
            result[key] = value
        return result

    def reject_constant(_):
        raise ValueError("Non-finite finding")

    findings = []
    expected_by_source = {"cache.py": "cache_ttl", "urls.py": "repeated_tags"}
    if set(submissions) != {item["id"] for item in plan}:
        raise ValueError("Every declared reader needs one stopped submission")
    for item in plan:
        report = json.loads(submissions[item["id"]], object_pairs_hook=unique_object,
                            parse_constant=reject_constant)
        if type(report) is not dict or set(report) != {"findings"} or type(report["findings"]) is not list:
            raise ValueError("Reader handoff must contain exactly a findings array")
        expected = {expected_by_source[source] for source in item["read_roots"]}
        seen = set()
        for finding in report["findings"]:
            if type(finding) is not dict or set(finding) != {"id", "source", "line", "observed", "expected", "explanation"}:
                raise ValueError("A finding needs the exact predeclared fields")
            identifier = finding["id"]
            if type(identifier) is not str or identifier not in expected or identifier in seen:
                raise ValueError("Missing, duplicate or out-of-assignment finding")
            if finding["source"] not in item["read_roots"] or expected_by_source[finding["source"]] != identifier:
                raise ValueError("Finding source differs from its scoped assignment")
            if type(finding["line"]) is not int or finding["line"] < 1 or not isinstance(finding["explanation"], str) or not finding["explanation"].strip():
                raise ValueError("Finding needs a source line and explanation")
            value_type = bool if identifier == "cache_ttl" else str
            if type(finding["observed"]) is not value_type or type(finding["expected"]) is not value_type:
                raise ValueError("Finding values must use the declared result type")
            seen.add(identifier)
            findings.append(finding)
        if seen != expected:
            raise ValueError("Reader omitted a required finding")
    result = {"findings": sorted(findings, key=lambda row: row["id"])}
    _write(Path(project) / "report.json", result)
    return result


def export_candidate(project, destination):
    """Copy the complete candidate without Git administration or followed links."""
    destination.mkdir(parents=True, exist_ok=False)
    for source in sorted(project.rglob("*")):
        relative = source.relative_to(project)
        if relative.parts[0] == ".git":
            continue
        if source.is_symlink() or getattr(source, "is_junction", lambda: False)():
            raise ValueError("Candidate export refuses links and junctions")
        target = destination / relative
        if source.is_dir():
            target.mkdir(exist_ok=True)
        elif source.is_file():
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(source.read_bytes())
    return destination


def verify_candidate(scenario, project):
    """External named check: no verifier or reference file is copied to a worker."""
    pack = fixture_pack()
    with tempfile.TemporaryDirectory(prefix="sonn-benchmark-verifier-") as temporary:
        candidate = export_candidate(Path(project).resolve(), Path(temporary) / "candidate")
        return pack.evaluate(scenario, candidate)


def _git(project, *arguments, env=None):
    from lumi.processes import background_process_kwargs
    return subprocess.run(["git", "-C", str(project), *arguments], check=True, capture_output=True, text=True,
                          timeout=20, env=env, **background_process_kwargs()).stdout.strip()


def _commit_seed(project):
    _git(project, "init", "-b", "main")
    for key, value in (("user.name", "SONN Benchmark Fixture"), ("user.email", "fixture@example.invalid"),
                       ("core.autocrlf", "false"), ("core.hooksPath", str(project.parent / "empty-hooks")),
                       ("commit.gpgsign", "false")):
        _git(project, "config", key, value)
    env = {**os.environ, "GIT_AUTHOR_DATE": "2000-01-01T00:00:00Z", "GIT_COMMITTER_DATE": "2000-01-01T00:00:00Z"}
    _git(project, "add", ".")
    _git(project, "commit", "-m", "Pinned benchmark seed", env=env)
    return _git(project, "rev-parse", "HEAD")


def _command(supervisor, authority, kind, payload):
    from lumi.engine.swarming.models import Command, RevisionConflict
    for _ in range(20):
        revision = supervisor.store.snapshot(authority.scope, authority.run_id)["run"]["revision"]
        try:
            return supervisor.handle(Command(uuid.uuid4().hex, authority.run_id, revision, authority.epoch, kind, payload), authority)
        except RevisionConflict:
            continue  # Known pre-mutation conflict only; never replay an ambiguous effect.
    raise RevisionConflict("Concurrent observations prevented the benchmark command")


def _usage(report):
    accounting = report["accounting"]
    measured = accounting["usage"]
    return {"known": {"requests": accounting["observed_used_request_units"],
        "input_tokens": measured["input_tokens"]["known_subtotal"], "output_tokens": measured["output_tokens"]["known_subtotal"],
        "cost_usd": float(measured["reported_usd"]["known_subtotal"]) if measured["reported_usd"]["known_subtotal"] is not None else None,
        "source": "Durable request observations; subtotals may be partial. Per-request missing quantities are retained in runtime-report.json."},
        "reserved": {"requests": accounting["active_or_uncertain_reserved_units"], "input_tokens": None,
            "output_tokens": None, "cost_usd": None, "source": "Held request reservations; no token or dollar reservation estimator"},
        "uncertain": {"requests": accounting["unresolved_model_requests"], "input_tokens": None, "output_tokens": None,
            "cost_usd": None, "source": "Unresolved model requests and unknown quantities; unknown is not zero"},
        "provider_failures": [{"request_id": row["id"], "outcome": row["state"], "observation": row.get("observation_outcome")}
                              for row in report["requests"] if row["state"] in {"failed", "uncertain"}]}


def _unresolved_effects(report):
    """A stopped model does not prove named-check or Git subprocess cleanup."""
    settled = {
        "integration_processes": {"stopped", "not_started"},
        "candidates": {"ready", "verified", "failed", "conflict", "applied"},
        "checks": {"passed", "failed", "timed_out", "cancelled", "not_started", "input_changed"},
        "applications": {"applied", "not_applied"},
        "operations": {"completed", "failed", "cancelled"},
    }
    return any(row["state"] not in states for category, states in settled.items()
               for row in report[category])


def _backend(protocol, endpoint, api_key):
    """The captured spec and any connection; an OpenAI-compatible endpoint runs as a connection."""
    from lumi.connections import backend_key
    from lumi.engine.swarming.connections import team_connection
    from lumi.gui.runtime import BackendSpec

    provider = protocol["provider"]
    if provider == "openai-compatible":
        connection = team_connection({"id": "benchmark", "name": "Benchmark endpoint", "type": "openai-compatible",
                                      "base_url": endpoint, "auth": "bearer" if api_key else "none"})
        name = backend_key(connection["id"])
        return (BackendSpec(name, protocol["model"], api_key=api_key, thinking_mode=protocol["thinking_mode"]),
                {name: connection})
    return (BackendSpec(provider, protocol["model"], url=endpoint if provider == "ollama" else "",
                        base_url=endpoint if provider != "ollama" else "", api_key=api_key,
                        thinking_mode=protocol["thinking_mode"]), {})


def _event_log(events, api_key):
    """Worker events for diagnosis: long text shortened, and the key never kept."""
    def clip(value):
        if isinstance(value, str):
            value = value.replace(api_key, "[redacted]") if api_key else value
            return value if len(value) <= 2000 else value[:2000] + f"... [{len(value) - 2000} more characters]"
        if isinstance(value, dict):
            return {key: clip(item) for key, item in value.items()}
        if isinstance(value, list):
            return [clip(item) for item in value]
        return value
    kept = [event for event in events if event.get("event") not in {"text.delta", "thinking.delta", "context.state"}]
    return {"note": "Local diagnosis only: may contain fixture file contents and model text", "events": clip(kept)}


def _configuration(protocol, plan):
    """The exact comparable configuration; platform observations are additional."""
    return {"actors": [{"role": f"guarded_{'writer' if item['write_roots'] else 'reader'}_{index + 1}", "provider": protocol["provider"],
        "model": protocol["model"], "parameters": {"thinking_mode": protocol["thinking_mode"], "other_generation_parameters": "provider defaults"}}
        for index, item in enumerate(plan)],
        "tool_settings": {"tools": [tool for tool in TOOLS if any(tool in item["tools"] for item in plan)], "shell": False, "model_network": "declared provider endpoint only; not an OS network sandbox"},
        "permission_settings": {"read_roots_by_assignment": [row["read_roots"] for row in plan],
            "write_roots_by_assignment": [row["write_roots"] for row in plan], "verification": "trusted external code outside worker scope"},
        "worker_slots": len(plan), "total_request_concurrency": 1 if protocol["resource_control"] == "equal_resource" else len(plan),
        "resource_control": protocol["resource_control"],
        "limits": {"requests": protocol["request_limit"], "wall_seconds": protocol["wall_seconds"], "cost_usd": None,
            "wall_semantics": "cancel admission at deadline; measure and retain actual cleanup/verification overrun"}}


def execute_case(protocol, case, output, *, endpoint, api_key="", backend_factory=None, process_factory=None):
    """Run one pinned case. Injected backends are always labelled harness-only."""
    from lumi.engine.swarming.integration import ApplyApproval, CheckSpec, SwarmIntegration
    from lumi.engine.swarming.models import Scope
    from lumi.engine.swarming.policy import PolicyProfile
    from lumi.engine.swarming.report import export_report
    from lumi.engine.swarming.scheduler import SwarmScheduler
    from lumi.engine.swarming.store import SwarmStore
    from lumi.engine.swarming.supervisor import SwarmSupervisor
    from lumi.engine.swarming.workers import SwarmWorkerRunner

    pack = fixture_pack()
    scenario_id, mode, run_id = case["scenario"], case["mode"], case["run_id"]
    if mode not in {"single", "swarm"}:
        raise ValueError(UNSUPPORTED_BATCH)
    if protocol["source_identity"] != source_identity() or protocol["manifest_sha256"] != pack.MANIFEST_SHA256:
        raise ValueError("Executable source or fixture identity changed after protocol declaration")
    if hashlib.sha256(endpoint.encode()).hexdigest() != protocol["endpoint_sha256"]:
        raise ValueError("Endpoint differs from the declared protocol")
    directory = Path(output) / run_id
    directory.mkdir(parents=True, exist_ok=False)
    temporary = Path(tempfile.mkdtemp(prefix="sonn-benchmark-runtime-"))
    project = temporary / "project"
    with pack.materialize(scenario_id) as seed:
        shutil.copytree(seed, project)
    base = _commit_seed(project)
    plan = assignment_plan(scenario_id, mode)
    count = len(plan)
    read_only = scenario_id == "independent_investigation"
    tools = READ_TOOLS if read_only else TOOLS
    ceiling = 1 if protocol["resource_control"] == "equal_resource" else count
    spec, connections = _backend(protocol, endpoint, api_key)
    store = SwarmStore(temporary / "state" / "swarm.sqlite3")
    supervisor = SwarmSupervisor(store)
    scope = Scope.personal("benchmark-owner", "fixture-project", run_id)
    started, started_clock = _now(), time.monotonic()
    authority = supervisor.create(scope, supervisor_id=uuid.uuid4().hex, objective=pack.verify_pack()[scenario_id]["prompt"],
        request_limit=protocol["request_limit"], run_id=run_id, lease_seconds=60,
        policy=PolicyProfile(1, frozenset(tools), frozenset({spec.backend_type}), max_workers=count,
                             write_roots=() if read_only else tuple(pack.verify_pack()[scenario_id]["editable"])))
    integration = SwarmIntegration(store, project, root=temporary / "worktrees")
    arguments = ({"writer_process_factory": process_factory} if process_factory is not None else
                 {"writer_process_factory": None} if backend_factory is not None else {})
    runner = SwarmWorkerRunner(supervisor, authority, project,
        backend_factory=backend_factory or (lambda selected: selected.create_backend()), integration=integration,
        managed_readers=process_factory is not None or backend_factory is None, connections=connections,
        project_instructions="Controlled benchmark project. Implement the assignment only; do not claim verification or acceptance.", **arguments)
    each, extra = divmod(protocol["request_limit"], count)
    allowances = {item["id"]: each + (index < extra) for index, item in enumerate(plan)}
    scheduler = SwarmScheduler(runner, spec, requests_per_worker=each, requests_by_work=allowances, base_revision=base)
    expired = threading.Event()
    interventions = []
    error = None
    verification = None
    verified_at = None
    accepted = False
    reason = None
    stop_errors = []

    def deadline():
        expired.set()
        interventions.append({"at": _now(), "kind": "enforced_deadline", "detail": "Host revoked new dispatch and requested owned execution stop at the declared wall deadline"})
        scheduler.close()
        try:
            runner.stop()
        except Exception as exc:
            stop_errors.append(type(exc).__name__)

    timer = threading.Timer(protocol["wall_seconds"], deadline)
    timer.daemon = True
    _write(directory / "execution.json", {"case": case, "protocol_sha256": protocol["protocol_sha256"],
        "evidence_class": "scripted_harness" if backend_factory is not None or process_factory is not None else "native_provider_execution",
        "runtime_boundary": "managed_process" if process_factory is not None or backend_factory is None else "trusted_thread_fixture",
        "temporary_runtime": str(temporary), "base_revision": base, "assignment_plan": plan,
        "concurrency_ceiling": ceiling, "request_allowances": allowances})
    timer.start()
    try:
        _command(supervisor, authority, "plan", {"work_items": plan})
        if ceiling != count:
            _command(supervisor, authority, "set_concurrency", {"max_workers": ceiling})
        scheduler.start()
        while not expired.is_set():
            snapshot = store.snapshot(scope, run_id)
            if len(snapshot["submissions"]) == count and all(row["process_state"] == "stopped" for row in snapshot["attempts"]):
                break
            ended = sorted({row["state"] for row in snapshot["attempts"] if row["state"] in {"failed", "cancelled", "uncertain"}})
            if ended:
                raise HarnessStop("A participant did not provide a verified, fully observed submission "
                                  f"(attempt {', '.join(ended)})")
            if scheduler.inspect()["error"]:
                # The scheduler's own diagnostic: a fixed text and an exception type.
                raise HarnessStop(scheduler.inspect()["error"])
            time.sleep(.03)
        scheduler.close()
        if expired.is_set():
            raise TimeoutError("Declared wall cancellation deadline reached")
        snapshot = store.snapshot(scope, run_id)
        if read_only:
            submissions = {attempt["work_item_id"]: next(row["handoff"] for row in snapshot["submissions"] if row["attempt_id"] == attempt["id"])
                           for attempt in snapshot["attempts"]}
            combine_findings(plan, submissions, project)
        remaining = max(1, min(15, protocol["wall_seconds"] - (time.monotonic() - started_clock)))
        if not read_only:
            check = CheckSpec("fixture-acceptance", (sys.executable, "-I", "-B", str(Path(__file__).resolve()),
                            "verify", "--scenario", scenario_id), timeout_seconds=remaining)
            candidate = integration.prepare_candidate(authority, writer_ids=tuple(row["id"] for row in snapshot["writer_worktrees"]),
                required_checks=(check,), criterion_checks={item["id"]: {"fixture_acceptance": check.key} for item in plan})
            if candidate["state"] != "ready":
                raise HarnessStop(f"Combined candidate is not ready ({candidate['state']})")
            receipt = integration.run_check(authority, candidate["id"], check.key)
            if receipt["state"] != "passed":
                raise HarnessStop(f"The predeclared external check failed ({receipt['state']}, exit code {receipt['exit_code']})")
            if expired.is_set():
                raise TimeoutError("Deadline expired before application")
            approval = ApplyApproval(uuid.uuid4().hex, scope, run_id, candidate["id"], base,
                candidate["result_revision"], time.time() + max(1, protocol["wall_seconds"] - (time.monotonic() - started_clock)))
            integration.apply(authority, candidate["id"], approval=approval)
        artifact = export_candidate(project, directory / "candidate")
        verification = pack.evaluate(scenario_id, artifact, timeout=max(.1, min(15, protocol["wall_seconds"] - (time.monotonic() - started_clock))))
        verified_at = _now()
        if not verification["accepted"]:
            raise HarnessStop("Independent applied-artifact verification failed")
        for attempt in snapshot["attempts"]:
            identity = {"attempt_id": attempt["id"], "attempt_epoch": attempt["epoch"]}
            if read_only:
                submission = next(row for row in snapshot["submissions"] if row["attempt_id"] == attempt["id"])
                _command(supervisor, authority, "record_check", {**identity, "check_id": uuid.uuid4().hex,
                    "criterion_id": "fixture_acceptance", "candidate_revision": submission["candidate_revision"],
                    "executor_id": "benchmark-external-verifier", "check_name": "Pinned investigation reproduction",
                    "exit_code": 0, "evidence": _json({"assembled_from_exact_submissions": True, "verification": verification})})
                _command(supervisor, authority, "accept", identity)
            else:
                _command(supervisor, authority, "accept_writer", {**identity,
                    "candidate_id": candidate["id"], "evidence": "Predeclared host fixture checks passed on the exact combined candidate and applied artifact; no human review is claimed"})
        _command(supervisor, authority, "complete", {})
        accepted = not expired.is_set()
    except KeyboardInterrupt:
        interventions.append({"at": _now(), "kind": "operator_stop", "detail": "Keyboard interruption; observations and uncertainty retained"})
        error = "KeyboardInterrupt"
    except Exception as exc:
        # Exception text can contain provider bodies/credentials. Retain typed
        # failure plus durable sanitized diagnostics, never arbitrary repr;
        # only the harness's own stops keep their text.
        error = type(exc).__name__
        reason = str(exc) if isinstance(exc, HarnessStop) else None
    finally:
        timer.cancel()
        scheduler.close()
        # The workers' own event stream (keys already redacted by the runner):
        # the only place a live failure's tool calls and error text survive.
        events = runner.poll(limit=1000)["events"]
        controls = runner.close(timeout=3)
        timer.join(timeout=3)

    snapshot = store.snapshot(scope, run_id)
    report = export_report(store, scope, run_id)
    if not (directory / "candidate").exists():
        candidates = [row for row in snapshot["integration_candidates"] if row["state"] in {"ready", "failed", "verified", "applied"}]
        source = Path(candidates[-1]["path"]) if candidates else project
        if all(not row["alive"] and row["termination_recorded"] for row in controls):
            try:
                artifact = export_candidate(source, directory / "candidate")
                verification = pack.evaluate(scenario_id, artifact)
                verified_at = _now()
            except Exception as exc:
                error = error or type(exc).__name__
    accepted = bool(accepted and snapshot["run"]["state"] == "completed" and verification and verification["accepted"]
        and report["accounting"]["unresolved_model_requests"] == 0
        and report["accounting"]["active_or_uncertain_reserved_units"] == 0
        and not _unresolved_effects(report)
        and all(not row["alive"] and row["termination_recorded"] for row in controls))
    interrupted = (expired.is_set() or error == "KeyboardInterrupt"
        or report["accounting"]["unresolved_model_requests"]
        or report["accounting"]["active_or_uncertain_reserved_units"]
        or report["run"]["state"] not in {"completed", "cancelled", "failed"}
        or _unresolved_effects(report)
        or any(row["alive"] or not row["termination_recorded"] for row in controls))
    outcome = "accepted" if accepted else "interrupted" if interrupted else "failed"
    finish = _now()
    record = {"schema_version": 1, "run_id": run_id, "scenario_id": scenario_id, "strategy": MODES[mode],
        "input_revision": pack.input_revision(scenario_id), "manifest_sha256": pack.MANIFEST_SHA256,
        "prompt": pack.verify_pack()[scenario_id]["prompt"], "client_revision": protocol["source_identity"],
        "configuration": {**_configuration(protocol, plan),
            "platform": {"os": platform.platform(), "python": platform.python_version(), "machine": platform.machine()}},
        "usage": _usage(report), "interventions": interventions,
        "timing": {"started_at": started, "finished_at": finish, "verified_at": verified_at, "elapsed_seconds": time.monotonic() - started_clock},
        "outcome": outcome, "verification": verification}
    pack.validate_run_record(record)
    _write(directory / "runtime-report.json", report)
    _write(directory / "worker-events.json", _event_log(events, api_key))
    _write(directory / "run.json", record)
    _write(directory / "observations.json", {"error_type": error, "reason": reason, "stop_errors": stop_errors,
        "runtime_state": snapshot["run"]["state"],
        "workers": [{key: row[key] for key in ("attempt_id", "epoch", "state", "alive", "termination_recorded")}
                    for row in controls], "deadline_reached": expired.is_set(),
        "runtime_retained": str(temporary), "no_automatic_repair_or_replay": True})
    return record


def aggregate(output):
    """Recheck artifacts and declarations; incomplete or scripted data has no benefit claim."""
    root = Path(output)
    protocol = json.loads((root / "protocol.json").read_text(encoding="utf-8"))
    unhashed = {key: value for key, value in protocol.items() if key != "protocol_sha256"}
    if _digest(unhashed) != protocol["protocol_sha256"]:
        raise ValueError("Protocol content changed after declaration")
    pack = fixture_pack()
    rows = []
    invalid = []
    for case in protocol["schedule"]:
        directory = root / case["run_id"]
        try:
            record = json.loads((directory / "run.json").read_text(encoding="utf-8"))
            execution = json.loads((directory / "execution.json").read_text(encoding="utf-8"))
            runtime = json.loads((directory / "runtime-report.json").read_text(encoding="utf-8"))
            observations = json.loads((directory / "observations.json").read_text(encoding="utf-8"))
            pack.validate_run_record(record)
            expected_plan = assignment_plan(case["scenario"], case["mode"])
            expected_ceiling = 1 if protocol["resource_control"] == "equal_resource" else len(expected_plan)
            each, extra = divmod(protocol["request_limit"], len(expected_plan))
            expected_allowances = {item["id"]: each + (index < extra) for index, item in enumerate(expected_plan)}
            if (execution["case"] != case or execution["protocol_sha256"] != protocol["protocol_sha256"]
                    or execution["assignment_plan"] != expected_plan or execution["concurrency_ceiling"] != expected_ceiling
                    or execution["request_allowances"] != expected_allowances
                    or execution["evidence_class"] not in {"scripted_harness", "native_provider_execution"}
                    or record["client_revision"] != protocol["source_identity"] or record["run_id"] != case["run_id"]
                    or record["scenario_id"] != case["scenario"] or record["strategy"] != MODES[case["mode"]]
                    or {key: value for key, value in record["configuration"].items() if key != "platform"} != _configuration(protocol, expected_plan)
                    or record["usage"] != _usage(runtime)):
                raise ValueError("Run configuration differs from its predeclared comparison")
            independent = None
            if (directory / "candidate").is_dir():
                independent = pack.evaluate(case["scenario"], directory / "candidate")
            artifact_matches = bool(independent and record["verification"]
                and independent["candidate_revision"] == record["verification"]["candidate_revision"])
            correct = bool(artifact_matches and independent["accepted"])
            completed = bool(record["outcome"] == "accepted" and correct and runtime["run"]["id"] == record["run_id"]
                and runtime["run"]["state"] == "completed" and runtime["accounting"]["unresolved_model_requests"] == 0
                and runtime["accounting"]["active_or_uncertain_reserved_units"] == 0
                and not _unresolved_effects(runtime)
                and len(observations["workers"]) == len(expected_plan)
                and {row["attempt_id"] for row in observations["workers"]} == {row["id"] for row in runtime["attempts"]}
                and all(row["alive"] is False and row["termination_recorded"] is True for row in observations["workers"]))
            rows.append({"run_id": case["run_id"], "scenario": case["scenario"], "mode": case["mode"],
                "repetition": case["repetition"], "evidence_class": execution["evidence_class"],
                "recorded_outcome": record["outcome"], "independently_correct": correct, "verified_completed": completed,
                "elapsed_seconds": record["timing"]["elapsed_seconds"], "usage": record["usage"],
                "base_revision": execution["base_revision"]})
        except (OSError, ValueError, KeyError, TypeError) as exc:
            invalid.append({"run_id": case["run_id"], "reason": type(exc).__name__})
    baselines = {scenario: {row["base_revision"] for row in rows if row["scenario"] == scenario}
                 for scenario in protocol["scenarios"]}
    if any(len(values) > 1 for values in baselines.values()):
        invalid.append({"run_id": "comparison", "reason": "Input Git baselines differ"})
    groups = []
    for scenario in protocol["scenarios"]:
        for mode in protocol["modes"]:
            values = [row for row in rows if row["scenario"] == scenario and row["mode"] == mode]
            completed = [row for row in values if row["verified_completed"]]
            groups.append({"scenario": scenario, "mode": mode, "supported": mode != "current-batch", "observed": len(values),
                "expected": protocol["repetitions"] if mode != "current-batch" else 0,
                "verified_completed": len(completed), "independently_correct": sum(row["independently_correct"] for row in values),
                "outcomes": dict(Counter(row["recorded_outcome"] for row in values)),
                "elapsed_seconds_all_attempts": [row["elapsed_seconds"] for row in values],
                "elapsed_seconds_verified_completed": [row["elapsed_seconds"] for row in completed]})
    result = {"format": "sonn-swarm-benchmark-comparison", "version": 1,
        "protocol_sha256": protocol["protocol_sha256"], "resource_control": protocol["resource_control"],
        "complete_records": not invalid and len(rows) == len(protocol["schedule"]), "invalid_or_missing": invalid,
        "unsupported_modes": protocol["unsupported_modes"], "groups": groups, "runs": rows,
        "speedup": None, "benefit_claim": None,
        "qualification": "No numerical benefit threshold/reference baseline is established; scripted harness timings are never model-performance evidence",
        "record_trust": "Local operator-reported runtime records, not authenticated execution receipts; exported candidates are independently reevaluated"}
    _write(root / "comparison.json", result)
    lines = ["# Controlled benchmark observations", "", result["qualification"], "",
        "| Scenario | Mode | Verified complete | Independently correct | Observed / expected |",
        "| --- | --- | ---: | ---: | ---: |"]
    for row in groups:
        lines.append(f"| {row['scenario']} | {row['mode']} | {row['verified_completed']} | {row['independently_correct']} | {row['observed']} / {row['expected']} |")
    lines.extend(["", "Incomplete/invalid records: " + str(len(invalid)), "", "No speedup or release qualification is claimed.", ""])
    (root / "comparison.md").write_text("\n".join(lines), encoding="utf-8")
    return result


def run_study(protocol, output, *, endpoint, api_key="", backend_factory=None, process_factory=None):
    """Execute a predeclared order. No reference solution enters a live provider."""
    output = Path(output).expanduser().resolve()
    if output.is_relative_to(ROOT) or output == ROOT:
        raise ValueError("Benchmark runtime artifacts must stay outside the repository")
    output.mkdir(parents=True, exist_ok=False)
    _write(output / "protocol.json", protocol)
    for case in protocol["schedule"]:
        try:
            record = execute_case(protocol, case, output, endpoint=endpoint, api_key=api_key,
                                  backend_factory=backend_factory, process_factory=process_factory)
            observed = json.loads((output / case["run_id"] / "observations.json").read_text(encoding="utf-8"))
            if (record["outcome"] == "interrupted" or record["usage"]["uncertain"]["requests"]
                    or record["usage"]["reserved"]["requests"]
                    or any(row["alive"] or not row["termination_recorded"] for row in observed["workers"])):
                # Stop means stop the study, not just its current assignment.
                # An unresolved participant also invalidates the resource
                # assumptions for later cases, even in another directory.
                _write(output / "study-interruption.json", {"at": _now(), "run_id": case["run_id"],
                    "outcome": record["outcome"], "reason": "Interrupted execution or unresolved observations",
                    "remaining_cases_not_replayed": True})
                break
        except (Exception, KeyboardInterrupt) as exc:
            # Pre-dispatch/setup failure is not a model run. Preserve the
            # scheduled omissions and stop the study instead of silently
            # replacing its baseline or narrowing the selected cases.
            _write(output / "study-interruption.json", {"at": _now(), "run_id": case["run_id"],
                "outcome": "blocked", "error_type": type(exc).__name__, "remaining_cases_not_replayed": True})
            break
    return aggregate(output)


def main(argv=None):
    # A trusted check invokes only this branch. No engine/provider import or
    # reference pack content is placed in the candidate workspace.
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "verify":
        parser = argparse.ArgumentParser()
        parser.add_argument("--scenario", choices=SCENARIOS, required=True)
        args = parser.parse_args(argv[1:])
        result = verify_candidate(args.scenario, Path.cwd())
        print(_json(result))
        return 0 if result["accepted"] else 1
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider", choices=PROVIDERS, required=True,
                        help="openai-compatible: any Chat Completions endpoint, such as NVIDIA NIM")
    parser.add_argument("--model", required=True)
    parser.add_argument("--endpoint", required=True)
    parser.add_argument("--request-limit", type=int, required=True)
    parser.add_argument("--wall-seconds", type=float, required=True)
    parser.add_argument("--resource-control", choices=("operational", "equal_resource"), required=True)
    parser.add_argument("--modes", choices=tuple(MODES), nargs="+", required=True)
    parser.add_argument("--scenarios", choices=SCENARIOS, nargs="+", required=True)
    parser.add_argument("--repetitions", type=int, required=True)
    parser.add_argument("--order-seed", type=int, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--thinking-mode", default="")
    parser.add_argument("--api-key-env", help="Read a key from this named environment variable; never put the key in arguments")
    parser.add_argument("--execute-live", action="store_true", help="Actually invoke the declared provider; omitted means protocol only")
    args = parser.parse_args(argv)
    try:
        protocol = prepare_protocol(provider=args.provider, model=args.model, endpoint=args.endpoint,
            request_limit=args.request_limit, wall_seconds=args.wall_seconds, resource_control=args.resource_control,
            modes=tuple(args.modes), scenarios=tuple(args.scenarios), repetitions=args.repetitions,
            order_seed=args.order_seed, thinking_mode=args.thinking_mode)
        if args.execute_live:
            key = os.environ.get(args.api_key_env, "") if args.api_key_env else ""
            if args.provider in {"sonn", "kimi", "openrouter"} and not key:
                raise ValueError("The selected hosted provider requires an explicit nonempty key environment source")
            sys.path.insert(0, str(ROOT))
            result = run_study(protocol, args.output, endpoint=args.endpoint, api_key=key)
            print(f"Comparison retained at {args.output.resolve() / 'comparison.json'}")
            return 0 if result["runs"] and result["complete_records"] and all(row["verified_completed"] for row in result["runs"]) else 1
        output = args.output.expanduser().resolve()
        if output.is_relative_to(ROOT):
            raise ValueError("Protocol output must stay outside the repository")
        output.mkdir(parents=True, exist_ok=False)
        _write(output / "protocol.json", protocol)
        print(f"Protocol prepared; no provider invoked: {output / 'protocol.json'}")
        return 0
    except (ValueError, OSError) as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    raise SystemExit(main())
