"""An orchestrated team whose owner let it apply writers' changes that pass every declared check."""

import json
import sys
import time

import pytest

from lumi.engine.swarming import Scope
from lumi.engine.swarming.autopilot import APPLY_EVIDENCE, WRITER_EVIDENCE
from lumi.engine.swarming.models import ScopeDenied
from lumi.engine.swarming.service import CapturedSession, SwarmRuntime
from lumi.gui.runtime import BackendSpec
from lumi.gui.settings import SettingsManager
from tests.streaming_stub import StreamingBackend, done, text_delta, tool_call
from tests.test_swarm_workers import until
from tests.test_swarm_writers import git

CHECK = {"key": "value-check", "timeout_seconds": 30, "argv": [sys.executable, "-c",
         "from pathlib import Path; assert Path('src/value.txt').read_text() == 'verified change\\n', 'value is wrong'"]}
# The same check, slow enough that the owner's checkout can change before the team applies.
SLOW_CHECK = {**CHECK, "argv": [sys.executable, "-c", "import time; time.sleep(1.5); " + CHECK["argv"][2]]}
FINAL = {"summary": "src/value.txt now holds the verified value; the check passed on the applied change.",
         "use_team": False, "work_items": []}


def writer_plan(identity="value", path="src/value.txt"):
    return json.dumps({"summary": f"One writer updates {path}.", "use_team": True, "work_items": [
        {"id": identity, "objective": f"Update {path}", "role": "implement", "dependencies": [],
         "read_roots": ["src"], "write_roots": ["src"], "criteria": ["value-check"]}]})


def writer(content, path="src/value.txt"):
    """A writer's two model turns: write the file, then report."""
    return [[tool_call("file_write", {"path": path, "content": content}, "write-1"), done()],
            [text_delta(f"Wrote {path}."), done()]]


@pytest.fixture
def team(tmp_path):
    project = tmp_path / "project"
    (project / "src").mkdir(parents=True)
    (project / "src" / "value.txt").write_text("original\n")
    (project / "personal.txt").write_text("committed personal\n")
    git(project, "init", "-b", "main")
    git(project, "add", ".")
    git(project, "commit", "-m", "Fixture base")
    outputs, backends = [], []

    def factory(spec):
        # Each new participant gets the next output: a list of turns for a writer, else one reply.
        output = outputs[len(backends)]
        backend = (StreamingBackend(scripts=output) if isinstance(output, list)
                   else StreamingBackend(events=[text_delta(output), done()]))
        backend.name, backend.model = spec.backend_type, spec.model
        backends.append(backend)
        return backend

    service = SwarmRuntime(SettingsManager(tmp_path / "settings.json"), backend_factory=factory,
                           state_root=lambda _: tmp_path / "state")
    capture = CapturedSession(Scope.personal("fixture-owner", "project", "session"), str(project),
                              BackendSpec("ollama", "chosen"))
    service.operate(capture, {"action": "configure", "request_id": "enable", "enabled": True})
    yield service, capture, project, outputs, backends
    service.close()


def start(service, capture, *, rounds=1, apply=True, check=CHECK, **extra):
    autonomy = {"rounds": rounds, **({"apply": True} if apply else {})}
    return service.operate(capture, {"request_id": "setup-request", "action": "start",
        "objective": "Change the scoped value", "plan_mode": "coordinator", "tasks": None,
        "write_roots": ["src"], "checks": [check], "request_limit": 20, "max_workers": 2,
        "coordinator_requests": 3, "worker_requests": 4, "autonomy": autonomy, **extra})["run"]["run"]["id"]


def view(service, capture, run_id):
    return service.operate(capture, {"request_id": f"view-{time.monotonic()}", "run_id": run_id})


def finished(service, capture, run_id, timeout=60):
    return until(lambda: (lambda current: current if current["run"]["run"]["state"] in {"completed", "cancelled", "failed"}
                          and not current["autonomy"]["active"] else None)(view(service, capture, run_id)), timeout=timeout)


def phase(service, capture, run_id, wanted, timeout=60):
    return until(lambda: (lambda current: current if current["autonomy"]["phase"] == wanted else None)(
        view(service, capture, run_id)), timeout=timeout)


def test_the_orchestrator_applies_writers_changes_that_pass_every_check(team):
    service, capture, project, outputs, backends = team
    base = git(project, "rev-parse", "HEAD")
    outputs += [writer_plan(), writer("verified change\n"), json.dumps(FINAL)]
    run_id = start(service, capture)
    current = finished(service, capture, run_id)
    run = current["run"]
    assert run["run"]["state"] == "completed", current["autonomy"]
    assert current["autonomy"]["apply"] is True and current["autonomy"]["final_report"] == FINAL["summary"]
    # Combined, checked and applied through the owner's own integration path.
    assert [row["kind"] for row in run["integration_operations"]] == ["prepare_candidate", "run_check", "apply"]
    assert all(row["state"] == "completed" for row in run["integration_operations"])
    candidate = run["integration_candidates"][0]
    assert candidate["state"] == "applied" and candidate["base_revision"] == base
    assert git(project, "rev-parse", "HEAD") == candidate["result_revision"] != base
    assert (project / "src" / "value.txt").read_text() == "verified change\n"
    assert (project / "personal.txt").read_text() == "committed personal\n"
    # Accepted under the grant, never recorded as the owner's decision.
    acceptance, = run["writer_acceptances"]
    assert acceptance["owner_id"] == "autonomy:fixture-owner" and acceptance["evidence"] == WRITER_EVIDENCE
    report = service.operate(capture, {"request_id": "report", "action": "export_report", "run_id": run_id})["report"]
    assert [row["decision"] for row in report["writer_acceptances"]] == ["autonomy_grant"]
    # The orchestrator was told its writers' checked changes get applied.
    assert "applied to the project when every check passes" in backends[0].stream_calls[0]["user_msg"]
    assert APPLY_EVIDENCE


def test_a_failing_check_sends_the_writer_back_once_with_the_checks_output(team):
    service, capture, project, outputs, backends = team
    outputs += [writer_plan(), writer("wrong\n"), writer("verified change\n"), json.dumps(FINAL)]
    run_id = start(service, capture)
    run = finished(service, capture, run_id)["run"]
    assert run["run"]["state"] == "completed"
    first, second = run["integration_candidates"]
    assert first["state"] == "superseded" and second["state"] == "applied"
    assert [row["state"] for row in run["integration_checks"]] == ["failed", "passed"]
    assert [row["state"] for row in run["attempts"] if row["kind"] == "worker"] == ["failed", "submitted"]
    # The retried writer saw why its first change was sent back.
    retry = backends[2].stream_calls[0]["user_msg"]
    assert "failed the declared check value-check" in retry and "value is wrong" in retry
    assert (project / "src" / "value.txt").read_text() == "verified change\n"


def test_a_changed_checkout_hands_the_checked_changes_back_and_the_owner_can_finish(team):
    service, capture, project, outputs, backends = team
    base = git(project, "rev-parse", "HEAD")
    outputs += [writer_plan(), writer("verified change\n"), json.dumps(FINAL)]
    run_id = start(service, capture, check=SLOW_CHECK)
    until(lambda: view(service, capture, run_id)["run"]["submissions"], timeout=30)
    (project / "personal.txt").write_text("my unfinished work\n")  # While the check runs.
    current = phase(service, capture, run_id, "needs_owner")
    assert "couldn't be applied" in current["autonomy"]["detail"]
    assert current["run"]["integration_operations"][-1]["kind"] == "apply"
    assert current["run"]["integration_operations"][-1]["state"] == "failed"
    assert git(project, "rev-parse", "HEAD") == base
    assert (project / "personal.txt").read_text() == "my unfinished work\n"
    assert (project / "src" / "value.txt").read_text() == "original\n"
    time.sleep(1)  # Several loop passes: the team never retries the owner's checkout.
    assert len(view(service, capture, run_id)["run"]["integration_operations"]) == 3
    # The owner sets their work aside and applies the checked changes; the team goes on.
    (project / "personal.txt").write_text("committed personal\n")
    run = view(service, capture, run_id)["run"]
    candidate = run["integration_candidates"][0]
    service.operate(capture, {"action": "apply_candidate", "request_id": "owner-apply", "run_id": run_id,
        "expected_revision": run["run"]["revision"], "candidate_id": candidate["id"],
        "expected_base": candidate["base_revision"], "target_revision": candidate["result_revision"],
        "evidence": "Owner applied the checked changes after setting local work aside"})
    run = finished(service, capture, run_id)["run"]
    assert run["run"]["state"] == "completed"
    assert (project / "src" / "value.txt").read_text() == "verified change\n"


def test_a_later_round_writer_starts_from_the_applied_change(team):
    service, capture, project, outputs, backends = team
    outputs += [writer_plan(), writer("verified change\n"),
                writer_plan("other", "src/other.txt"), writer("second\n", "src/other.txt"), json.dumps(FINAL)]
    run_id = start(service, capture, rounds=2)
    run = finished(service, capture, run_id)["run"]
    assert run["run"]["state"] == "completed"
    first, second = run["integration_candidates"]
    assert first["state"] == second["state"] == "applied"
    # The second writer and its combined change built on the first applied change,
    # so the value check still passes on it.
    assert run["writer_worktrees"][1]["base_revision"] == first["result_revision"] == second["base_revision"]
    assert git(project, "rev-parse", "HEAD") == second["result_revision"]
    assert (project / "src" / "value.txt").read_text() == "verified change\n"
    assert (project / "src" / "other.txt").read_text() == "second\n"
    assert len(run["writer_acceptances"]) == 2


def test_without_the_apply_grant_writers_wait_for_the_owner(team):
    service, capture, project, outputs, backends = team
    outputs += [writer_plan(), writer("verified change\n")]
    run_id = start(service, capture, apply=False)
    current = phase(service, capture, run_id, "needs_owner")
    assert "Writers submitted changes" in current["autonomy"]["detail"] and current["autonomy"]["apply"] is False
    assert current["run"]["integration_operations"] == []
    runner = service._runners[run_id][1]
    attempt = next(row for row in current["run"]["attempts"] if row["kind"] == "worker")
    with pytest.raises(ScopeDenied, match="apply changes"):
        service._command(runner.supervisor, runner.authority, "accept_writer_under_grant", {
            "attempt_id": attempt["id"], "attempt_epoch": attempt["epoch"], "candidate_id": "forged",
            "evidence": "Forged grant"})
    assert (project / "src" / "value.txt").read_text() == "original\n"


def test_dispatch_that_stops_hands_the_team_back(team):
    service, capture, project, outputs, backends = team
    outputs += [writer_plan(), writer("verified change\n")]
    run_id = start(service, capture)
    # A dirty checkout can't give a writer its pinned clean base.
    (project / "personal.txt").write_text("my unfinished work\n")
    current = phase(service, capture, run_id, "needs_owner")
    assert "stopped starting workers" in current["autonomy"]["detail"]
    assert (project / "personal.txt").read_text() == "my unfinished work\n"


def test_applying_needs_writer_access_and_a_boolean_choice(team):
    service, capture, *_ = team
    base = {"request_id": "bad", "action": "start", "objective": "Check input", "plan_mode": "coordinator",
            "tasks": None, "request_limit": 20, "max_workers": 2, "coordinator_requests": 3, "worker_requests": 3}
    with pytest.raises(ValueError, match="writable folders"):
        service.operate(capture, {**base, "autonomy": {"rounds": 2, "apply": True}})
    with pytest.raises(ValueError, match="applies checked changes"):
        service.operate(capture, {**base, "write_roots": ["src"], "checks": [CHECK],
                                  "autonomy": {"rounds": 2, "apply": "yes"}})
    with pytest.raises(ValueError, match="rounds"):
        service.operate(capture, {**base, "autonomy": {"rounds": 2, "merge": True}})
    assert not service.busy
