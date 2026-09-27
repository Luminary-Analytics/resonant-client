"""Real Git and owned subprocess fixtures for isolated candidate integration."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time

import psutil
import pytest

from lumi.engine.swarming import AttemptContext, Command, Scope, SwarmStore, SwarmSupervisor
from lumi.engine.swarming.integration import ApplyApproval, CheckSpec, SwarmIntegration
from lumi.engine.swarming.models import AdmissionClosed, Conflict, LeaseExpired, ScopeDenied
from lumi.engine.swarming.policy import PolicyProfile


def git(path, *args):
    environment = {key: value for key, value in os.environ.items() if not key.upper().startswith("GIT_")}
    environment.update(GIT_AUTHOR_NAME="Fixture", GIT_AUTHOR_EMAIL="fixture@example.invalid",
                       GIT_COMMITTER_NAME="Fixture", GIT_COMMITTER_EMAIL="fixture@example.invalid")
    result = subprocess.run(["git", "-c", "commit.gpgsign=false", *args], cwd=path, env=environment,
                            capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


@pytest.fixture
def setup(tmp_path):
    if os.name != "nt":
        pytest.skip("Supervised integration requires Windows named-job process containment")
    project = tmp_path / "project"
    project.mkdir()
    git(project, "init", "-b", "main")
    (project / "a.txt").write_text("base-a\n")
    (project / "b.txt").write_text("base-b\n")
    (project / "forbidden.txt").write_text("preserve\n")
    git(project, "add", ".")
    git(project, "commit", "-m", "Fixture base")
    base = git(project, "rev-parse", "HEAD")
    store = SwarmStore(tmp_path / "state.sqlite", clock=lambda: 1000)
    supervisor = SwarmSupervisor(store)
    scope = Scope.personal("owner", "project", "session")
    policy = PolicyProfile(version=1, allowed_tools=frozenset({"file_read", "file_write"}),
                           allowed_providers=frozenset({"ollama"}), read_roots=(".",), write_roots=(".",), max_workers=4)
    authority = supervisor.create(scope, supervisor_id="supervisor", objective="Implement fixture", request_limit=12, policy=policy)
    integration = SwarmIntegration(store, project, root=tmp_path / "runtime" / "worktrees")
    return store, supervisor, authority, integration, project, base


def command(supervisor, authority, kind, **payload):
    revision = supervisor.store.snapshot(authority.scope, authority.run_id)["run"]["revision"]
    return supervisor.handle(Command(f"command-{revision}", authority.run_id, revision, authority.epoch, kind, payload), authority)


def writers(setup, names=("a", "b"), *, roots=None):
    store, supervisor, authority, integration, project, base = setup
    specs = [{"id": name, "objective": f"Change {name}", "role": "implement", "tools": ["file_write"],
              "read_roots": [roots[index] if roots else f"{name}.txt"],
              "write_roots": [roots[index] if roots else f"{name}.txt"], "criteria": ["combined"]}
             for index, name in enumerate(names)]
    command(supervisor, authority, "plan", work_items=specs)
    results = []
    for name in names:
        assignment = command(supervisor, authority, "assign", work_item_id=name, worker_id=f"worker-{name}", requests=2,
                             model={"provider": "ollama", "model": "fixture"}).result
        context = AttemptContext(authority.scope, authority.run_id, assignment["attempt_id"], assignment["worker_id"], authority.epoch)
        lease = integration.create_writer(authority, context, base_revision=base)
        command(supervisor, authority, "worker_started", attempt_id=context.attempt_id, attempt_epoch=context.epoch)
        results.append((context, lease))
    return results


def finish(setup, context, lease):
    store, supervisor, authority, integration, project, base = setup
    manifest = integration.finalize_writer(authority, context, lease["id"])
    command(supervisor, authority, "submit", attempt_id=context.attempt_id, attempt_epoch=context.epoch,
            candidate_revision=manifest["result_revision"], handoff="Scoped writer manifest retained")
    command(supervisor, authority, "worker_stopped", attempt_id=context.attempt_id, attempt_epoch=context.epoch,
            outcome="submitted", evidence="fixture execution ended")
    return manifest


def approved(setup, candidate):
    authority = setup[2]
    return ApplyApproval("explicit-approval", authority.scope, authority.run_id, candidate["id"],
                         candidate["base_revision"], candidate["result_revision"], 1100)


def passing_check():
    return CheckSpec("combined", (sys.executable, "-c",
                     "from pathlib import Path; assert Path('a.txt').read_text() == 'new-a\\n'; assert Path('b.txt').read_text() == 'new-b\\n'; print('combined result verified')"), 10)


def candidate(setup):
    store, supervisor, authority, integration, project, base = setup
    records = writers(setup)
    def edit(record):
        context, lease = record
        name = context.worker_id.removeprefix("worker-")
        (Path(lease["path"]) / f"{name}.txt").write_text(f"new-{name}\n")
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(edit, records))
    manifests = [finish(setup, *record) for record in records]
    result = integration.prepare_candidate(authority, writer_ids=tuple(row["id"] for row in manifests), required_checks=(passing_check(),),
                                           criterion_checks={name: {"combined": "combined"} for name in ("a", "b")})
    assert git(project, "rev-parse", "HEAD") == base
    assert (project / "a.txt").read_text() == "base-a\n"
    return result


def test_explicit_writer_acceptance_binds_check_application_and_retained_history(setup):
    store, supervisor, authority, integration, project, base = setup
    result = candidate(setup)
    integration.run_check(authority, result["id"], "combined")
    applied = integration.apply(authority, result["id"], approval=approved(setup, result))
    snapshot = store.snapshot(authority.scope, authority.run_id)
    assert all(work["state"] == "submitted" for work in snapshot["work_items"])
    assert snapshot["writer_acceptances"] == []
    with pytest.raises(Conflict, match="all work accepted"):
        command(supervisor, authority, "complete")
    git(project, "switch", "-c", "owner-next-task")
    assert integration.apply(authority, result["id"], approval=approved(setup, result)) == applied
    assert integration.reconcile_application(authority, applied["id"])["state"] == "applied"
    assert len(store.snapshot(authority.scope, authority.run_id)["integration_processes"]) == len(snapshot["integration_processes"])
    # Later user edits do not rewrite an already observed application, and
    # accepting its historical result never mutates those new edits.
    (project / "a.txt").write_text("owner's subsequent edits\n")
    for attempt in snapshot["attempts"]:
        revision = store.snapshot(authority.scope, authority.run_id)["run"]["revision"]
        envelope = Command(f"accept-{attempt['id']}", authority.run_id, revision, authority.epoch, "accept_writer",
                           {"attempt_id": attempt["id"], "attempt_epoch": attempt["epoch"], "candidate_id": result["id"],
                            "evidence": "Reviewed the combined check and exact application receipts"})
        receipt = supervisor.handle(envelope, authority)
        assert supervisor.handle(envelope, authority) == receipt
        with pytest.raises(Conflict, match="immutable"):
            command(supervisor, authority, "accept_writer", **envelope.payload)
    snapshot = store.snapshot(authority.scope, authority.run_id)
    assert len(snapshot["writer_acceptances"]) == 2
    for acceptance in snapshot["writer_acceptances"]:
        assert acceptance["candidate_revision"] == result["result_revision"]
        assert acceptance["application_id"] == "explicit-approval"
        assert acceptance["owner_id"] == authority.scope.owner_id
        assert json.loads(acceptance["checks_json"])["receipts"]["combined"] == snapshot["integration_checks"][0]["id"]
    command(supervisor, authority, "complete")
    reopened = SwarmStore(store.path, clock=lambda: 1000).snapshot(authority.scope, authority.run_id)
    assert reopened["run"]["state"] == "completed"
    assert reopened["writer_acceptances"] == snapshot["writer_acceptances"]
    assert (project / "a.txt").read_text() == "owner's subsequent edits\n"


@pytest.mark.parametrize("problem", ["unapplied", "wrong_submission", "wrong_application", "failed_check", "unknown_action", "unknown_check", "foreign_candidate"])
def test_writer_acceptance_rejects_incomplete_or_mismatched_evidence(setup, problem):
    store, supervisor, authority, integration, project, base = setup
    result = candidate(setup)
    integration.run_check(authority, result["id"], "combined")
    if problem != "unapplied":
        integration.apply(authority, result["id"], approval=approved(setup, result))
    snapshot = store.snapshot(authority.scope, authority.run_id)
    attempt = snapshot["attempts"][0]
    # Fault injection proves independently persisted boundaries are rechecked,
    # rather than trusting the candidate's aggregate state label.
    with store._connection(write=True) as connection:
        if problem == "wrong_submission":
            connection.execute("UPDATE submissions SET candidate_revision=? WHERE attempt_id=?", (base, attempt["id"]))
        elif problem == "wrong_application":
            connection.execute("UPDATE integration_applications SET observed_revision=?", (base,))
        elif problem == "failed_check":
            connection.execute("UPDATE integration_checks SET exit_code=1")
        elif problem == "unknown_check":
            connection.execute("UPDATE integration_checks SET state='uncertain'")
        elif problem == "unknown_action":
            connection.execute("INSERT INTO model_requests VALUES('fault-request',?,?,'main','completed',1)", (attempt["id"], attempt["epoch"]))
            connection.execute("INSERT INTO action_receipts(id,attempt_id,epoch,request_id,call_id,tool_name,arguments_sha256,state) "
                               "VALUES('fault-action',?,?,'fault-request','call','file_write','hash','uncertain')", (attempt["id"], attempt["epoch"]))
    with pytest.raises((Conflict, ScopeDenied)):
        command(supervisor, authority, "accept_writer", attempt_id=attempt["id"], attempt_epoch=attempt["epoch"],
                candidate_id="foreign" if problem == "foreign_candidate" else result["id"], evidence="Reviewed")
    assert store.snapshot(authority.scope, authority.run_id)["writer_acceptances"] == []


def test_candidate_criterion_contract_cannot_be_added_or_remapped_during_acceptance(setup):
    store, supervisor, authority, integration, project, base = setup
    context, lease = writers(setup, ("a",))[0]
    (Path(lease["path"]) / "a.txt").write_text("new-a\n")
    manifest = finish(setup, context, lease)
    check = CheckSpec("actual", (sys.executable, "-c", "from pathlib import Path; assert Path('a.txt').read_text() == 'new-a\\n'"), 10)
    for invalid in ({}, {"a": {}}, {"a": {"combined": "missing"}}, {"foreign": {"combined": "actual"}}):
        with pytest.raises(Conflict, match="mapping|criterion"):
            integration.prepare_candidate(authority, writer_ids=(manifest["id"],), required_checks=(check,), criterion_checks=invalid)
    result = integration.prepare_candidate(authority, writer_ids=(manifest["id"],), required_checks=(check,))
    with pytest.raises(Conflict, match="different inputs"):
        integration.prepare_candidate(authority, writer_ids=(manifest["id"],), required_checks=(check,), candidate_id=result["id"],
                                      criterion_checks={"a": {"combined": "actual"}})
    integration.run_check(authority, result["id"], "actual")
    integration.apply(authority, result["id"], approval=approved(setup, result))
    with pytest.raises(Conflict, match="declared before"):
        command(supervisor, authority, "accept_writer", attempt_id=context.attempt_id, attempt_epoch=context.epoch,
                candidate_id=result["id"], evidence="Prose cannot create the missing criteria contract")
    with pytest.raises(TypeError):
        command(supervisor, authority, "accept_writer", attempt_id=context.attempt_id, attempt_epoch=context.epoch,
                candidate_id=result["id"], evidence="Reviewed", criterion_checks={"combined": "actual"})


def test_completion_revalidates_writer_acceptance_receipts(setup):
    store, supervisor, authority, integration, project, base = setup
    result = candidate(setup)
    integration.run_check(authority, result["id"], "combined")
    integration.apply(authority, result["id"], approval=approved(setup, result))
    for attempt in store.snapshot(authority.scope, authority.run_id)["attempts"]:
        command(supervisor, authority, "accept_writer", attempt_id=attempt["id"], attempt_epoch=attempt["epoch"],
                candidate_id=result["id"], evidence="Reviewed")
    with store._connection(write=True) as connection:
        connection.execute("UPDATE writer_acceptances SET writer_revision=?", (base,))
    with pytest.raises(Conflict, match="evidence changed"):
        command(supervisor, authority, "complete")
    assert store.snapshot(authority.scope, authority.run_id)["run"]["state"] == "running"


def test_writer_acceptance_is_atomic_and_allows_unrelated_reader_progress(setup, monkeypatch):
    store, supervisor, authority, integration, project, base = setup
    result = candidate(setup)
    integration.run_check(authority, result["id"], "combined")
    integration.apply(authority, result["id"], approval=approved(setup, result))
    snapshot = store.snapshot(authority.scope, authority.run_id)
    specs = [json.loads(row["specification"]) for row in snapshot["work_items"]]
    specs.append({"id": "reader", "objective": "Inspect independent retained file", "role": "explore", "tools": ["file_read"],
                  "read_roots": ["forbidden.txt"], "write_roots": [], "criteria": ["owner_review"]})
    command(supervisor, authority, "plan", work_items=specs)
    reader = command(supervisor, authority, "assign", work_item_id="reader", worker_id="reader", requests=1,
                     model={"provider": "ollama", "model": "fixture"}).result
    command(supervisor, authority, "worker_started", attempt_id=reader["attempt_id"], attempt_epoch=reader["epoch"])
    first = snapshot["attempts"][0]
    original = store._event
    def fail_decision(connection, run_id, kind, payload):
        if kind == "command_accept_writer":
            raise sqlite3.OperationalError("injected acceptance persistence failure")
        return original(connection, run_id, kind, payload)
    with monkeypatch.context() as patch:
        patch.setattr(store, "_event", fail_decision)
        with pytest.raises(sqlite3.OperationalError, match="injected"):
            command(supervisor, authority, "accept_writer", attempt_id=first["id"], attempt_epoch=first["epoch"],
                    candidate_id=result["id"], evidence="Reviewed")
    after_failure = store.snapshot(authority.scope, authority.run_id)
    assert after_failure["writer_acceptances"] == []
    assert after_failure["work_items"][0]["state"] == "submitted"
    for attempt in snapshot["attempts"]:
        command(supervisor, authority, "accept_writer", attempt_id=attempt["id"], attempt_epoch=attempt["epoch"],
                candidate_id=result["id"], evidence="Reviewed")
    assert len(store.snapshot(authority.scope, authority.run_id)["writer_acceptances"]) == 2
    with pytest.raises(Conflict, match="all work accepted"):
        command(supervisor, authority, "complete")


def test_parallel_writers_combined_check_and_explicit_apply_preserve_base_until_approval(setup):
    store, supervisor, authority, integration, project, base = setup
    result = candidate(setup)
    assert result["state"] == "ready"
    assert (Path(result["path"]) / "a.txt").read_text() == "new-a\n"
    with pytest.raises(Conflict, match="checked"):
        integration.apply(authority, result["id"], approval=approved(setup, result))
    receipt = integration.run_check(authority, result["id"], "combined")
    assert receipt["state"] == "passed"
    assert receipt["exit_code"] == 0
    assert receipt["candidate_revision"] == result["result_revision"]
    assert git(project, "rev-parse", "HEAD") == base
    application = integration.apply(authority, result["id"], approval=approved(setup, result))
    assert application["state"] == "applied"
    assert git(project, "rev-parse", "HEAD") == result["result_revision"]
    assert (project / "a.txt").read_text() == "new-a\n"
    assert integration.apply(authority, result["id"], approval=approved(setup, result)) == application
    snapshot = store.snapshot(authority.scope, authority.run_id)
    assert all(row["process_protocol"] == 1 for table in ("writer_worktrees", "integration_candidates", "integration_checks", "integration_applications")
               for row in snapshot[table])
    processes = snapshot["integration_processes"]
    assert {row["effect_kind"] for row in processes} == {"writer", "candidate", "check", "application"}
    assert all(row["state"] == "stopped" and row["invoked"] == 1 and row["pid"] > 0 and row["created_at"] > 0 for row in processes)
    assert receipt["job_id"] in {row["id"] for row in processes if row["effect_kind"] == "check"}


def test_failed_combined_candidate_keeps_user_checkout_and_receipt(setup):
    store, supervisor, authority, integration, project, base = setup
    context, lease = writers(setup, ("a",))[0]
    (Path(lease["path"]) / "a.txt").write_text("new-a\n")
    manifest = finish(setup, context, lease)
    result = integration.prepare_candidate(authority, writer_ids=(manifest["id"],), required_checks=(passing_check(),))
    receipt = integration.run_check(authority, result["id"], "combined")
    assert receipt["state"] == "failed"
    assert receipt["exit_code"] != 0
    with pytest.raises(Conflict):
        integration.apply(authority, result["id"], approval=approved(setup, result))
    assert git(project, "rev-parse", "HEAD") == base
    snapshot = store.snapshot(authority.scope, authority.run_id)
    assert snapshot["integration_candidates"][0]["state"] == "failed"
    assert snapshot["integration_checks"][0]["exit_code"] != 0
    assert Path(result["path"]).is_dir()


def test_dirty_checkout_is_never_stashed_reset_or_overwritten(setup):
    store, supervisor, authority, integration, project, base = setup
    result = candidate(setup)
    integration.run_check(authority, result["id"], "combined")
    (project / "a.txt").write_text("user's unsaved work\n")
    git(project, "add", "a.txt")
    (project / "b.txt").write_text("user's unstaged work\n")
    index_before = git(project, "diff", "--cached")
    with pytest.raises(Conflict, match="dirty"):
        integration.apply(authority, result["id"], approval=approved(setup, result))
    assert (project / "a.txt").read_text() == "user's unsaved work\n"
    assert (project / "b.txt").read_text() == "user's unstaged work\n"
    assert git(project, "diff", "--cached") == index_before
    assert git(project, "rev-parse", "HEAD") == base


def test_changed_user_base_and_tampered_candidate_reject_old_evidence(setup):
    store, supervisor, authority, integration, project, base = setup
    result = candidate(setup)
    integration.run_check(authority, result["id"], "combined")
    git(project, "commit", "--allow-empty", "-m", "User advanced base")
    with pytest.raises(Conflict, match="base changed"):
        integration.apply(authority, result["id"], approval=approved(setup, result))
    assert git(project, "rev-parse", "HEAD") != base
    (Path(result["path"]) / "a.txt").write_text("tampered after checks\n")
    with pytest.raises(Conflict, match="changed after verification"):
        integration.apply(authority, result["id"], approval=approved(setup, result))


@pytest.mark.parametrize("operation", ["delete", "rename", "new"])
def test_scope_enforcement_covers_delete_rename_and_untracked_paths(setup, operation):
    store, supervisor, authority, integration, project, base = setup
    context, lease = writers(setup, ("a",))[0]
    path = Path(lease["path"])
    if operation == "delete":
        (path / "forbidden.txt").unlink()
    elif operation == "rename":
        (path / "a.txt").unlink()
        (path / "forbidden.txt").rename(path / "a.txt")
    else:
        (path / "outside.txt").write_text("not authorized")
    with pytest.raises(Conflict, match="write scope"):
        integration.finalize_writer(authority, context, lease["id"])
    assert git(path, "rev-parse", "HEAD") == base
    assert (project / "forbidden.txt").read_text() == "preserve\n"


def test_index_symlink_escape_is_rejected_even_when_windows_checks_out_plain_file(setup):
    store, supervisor, authority, integration, project, base = setup
    context, lease = writers(setup, ("a",), roots=(".",))[0]
    path = Path(lease["path"])
    # Stage a symlink using Git so this works without Windows symlink privilege.
    link = path / "escape"
    link.write_text("../../private")
    digest = git(path, "hash-object", "-w", "escape")
    git(path, "update-index", "--add", "--cacheinfo", f"120000,{digest},escape")
    # finalize uses add -A, which normalizes this platform's plain file. A real
    # link is necessary for the filesystem boundary; directly exercise the
    # staged-tree validator as well, which operates independent of symlink support.
    with pytest.raises(Conflict, match="escapes"):
        integration._check_tree(path)
    assert git(project, "rev-parse", "HEAD") == base


def test_candidate_requires_observed_writer_termination(setup):
    store, supervisor, authority, integration, project, base = setup
    context, lease = writers(setup, ("a",))[0]
    (Path(lease["path"]) / "a.txt").write_text("new-a\n")
    manifest = integration.finalize_writer(authority, context, lease["id"])
    with pytest.raises(Conflict, match="observed termination"):
        integration.prepare_candidate(authority, writer_ids=(manifest["id"],), required_checks=(passing_check(),))


def test_checks_that_mutate_the_candidate_cannot_verify_it(setup):
    store, supervisor, authority, integration, project, base = setup
    context, lease = writers(setup, ("a",))[0]
    (Path(lease["path"]) / "a.txt").write_text("new-a\n")
    manifest = finish(setup, context, lease)
    result = integration.prepare_candidate(authority, writer_ids=(manifest["id"],), required_checks=(
        CheckSpec("mutating", (sys.executable, "-c", "from pathlib import Path; Path('a.txt').write_text('different')"), 10),))
    receipt = integration.run_check(authority, result["id"], "mutating")
    assert receipt["exit_code"] == 0
    assert receipt["state"] == "input_changed"


def test_files_a_check_creates_do_not_change_the_checked_revision(setup):
    # A live team's Python check wrote __pycache__ into its candidate, was
    # refused as changed input, and would have kept a second check from running.
    store, supervisor, authority, integration, project, base = setup
    context, lease = writers(setup, ("a",))[0]
    (Path(lease["path"]) / "a.txt").write_text("new-a\n")
    manifest = finish(setup, context, lease)
    creates = CheckSpec("report", (sys.executable, "-c",
                                   "from pathlib import Path; Path('check-report.txt').write_text('ok')"), 10)
    reads = CheckSpec("value", (sys.executable, "-c",
                                "from pathlib import Path; assert Path('a.txt').read_text() == 'new-a\\n'"), 10)
    result = integration.prepare_candidate(authority, writer_ids=(manifest["id"],), required_checks=(creates, reads))
    assert integration.run_check(authority, result["id"], "report")["state"] == "passed"
    assert integration.run_check(authority, result["id"], "value")["state"] == "passed"
    state = store.snapshot(authority.scope, authority.run_id)
    assert state["integration_candidates"][0]["state"] == "verified"


def test_each_check_starts_from_the_candidate_without_provider_keys(setup, monkeypatch):
    # Checks run code the writers wrote: what an earlier check left is gone
    # before the next starts, and Lumi's provider keys aren't passed to them.
    store, supervisor, authority, integration, project, base = setup
    monkeypatch.setenv("OPENAI_API_KEY", "fixture-provider-key")
    context, lease = writers(setup, ("a",))[0]
    (Path(lease["path"]) / "a.txt").write_text("new-a\n")
    manifest = finish(setup, context, lease)
    leaves = CheckSpec("leaves", (sys.executable, "-c",
                                  "from pathlib import Path; Path('left-behind.txt').write_text('stale')"), 10)
    clean = CheckSpec("clean", (sys.executable, "-c",
                                "import os; from pathlib import Path; assert not Path('left-behind.txt').exists(); "
                                "assert not {name.upper() for name in os.environ} & {'OPENAI_API_KEY'}"), 10)
    result = integration.prepare_candidate(authority, writer_ids=(manifest["id"],), required_checks=(leaves, clean))
    assert integration.run_check(authority, result["id"], "leaves")["state"] == "passed"
    assert integration.run_check(authority, result["id"], "clean")["state"] == "passed"


def test_check_timeout_stops_owned_process_and_retains_failed_candidate(setup):
    store, supervisor, authority, integration, project, base = setup
    context, lease = writers(setup, ("a",))[0]
    manifest = finish(setup, context, lease)
    marker = integration.root / "must-not-appear"
    pid_file = integration.root / "child.pid"
    child_code = f"import time; from pathlib import Path; time.sleep(10); Path({str(marker)!r}).write_text('escaped')"
    code = ("import subprocess, sys, time; from pathlib import Path; "
            f"child=subprocess.Popen([sys.executable, '-c', {child_code!r}]); "
            f"Path({str(pid_file)!r}).write_text(str(child.pid)); time.sleep(20)")
    result = integration.prepare_candidate(authority, writer_ids=(manifest["id"],), required_checks=(
        CheckSpec("bounded", (sys.executable, "-c", code), 1),))
    receipt = integration.run_check(authority, result["id"], "bounded")
    assert receipt["state"] == "timed_out"
    assert receipt["job_id"]
    child_pid = int(pid_file.read_text())
    deadline = time.monotonic() + 3
    while psutil.pid_exists(child_pid) and time.monotonic() < deadline:
        if psutil.Process(child_pid).status() == psutil.STATUS_ZOMBIE:
            break
        time.sleep(0.05)
    assert not psutil.pid_exists(child_pid) or psutil.Process(child_pid).status() == psutil.STATUS_ZOMBIE
    assert not marker.exists()
    assert store.snapshot(authority.scope, authority.run_id)["integration_candidates"][0]["state"] == "failed"


def test_foreign_approval_scope_and_wrong_revision_are_rejected(setup):
    store, supervisor, authority, integration, project, base = setup
    result = candidate(setup)
    integration.run_check(authority, result["id"], "combined")
    approval = approved(setup, result)
    with pytest.raises(ScopeDenied):
        integration.apply(authority, result["id"], approval=replace(approval, scope=replace(authority.scope, owner_id="other")))
    with pytest.raises(Conflict, match="does not match"):
        integration.apply(authority, result["id"], approval=replace(approval, target_revision=base))
    with pytest.raises(Conflict, match="expired"):
        integration.apply(authority, result["id"], approval=replace(approval, expires_at=999))


def test_creation_failure_has_no_shared_checkout_fallback(setup, monkeypatch):
    store, supervisor, authority, integration, project, base = setup
    original = integration._git
    def fail_worktree(path, *args, **kwargs):
        if args[:2] == ("worktree", "add"):
            raise Conflict("fixture worktree creation failed")
        return original(path, *args, **kwargs)
    monkeypatch.setattr(integration, "_git", fail_worktree)
    with pytest.raises(Conflict, match="creation failed"):
        writers(setup, ("a",))
    assert git(project, "rev-parse", "HEAD") == base
    assert not git(project, "status", "--porcelain")
    record = store.snapshot(authority.scope, authority.run_id)["writer_worktrees"][0]
    assert record["state"] == "failed"
    assert record["process_protocol"] == 1
    assert not store.snapshot(authority.scope, authority.run_id)["integration_processes"]
    assert record["path"] != str(project)


def test_combining_conflicting_writer_revisions_retains_conflict_outside_checkout(setup):
    store, supervisor, authority, integration, project, base = setup
    context, lease = writers(setup, ("a",))[0]
    (Path(lease["path"]) / "a.txt").write_text("first result\n")
    first = finish(setup, context, lease)
    existing = json.loads(store.snapshot(authority.scope, authority.run_id)["work_items"][0]["specification"])
    other = {**existing, "id": "repair", "objective": "Independent competing result"}
    command(supervisor, authority, "plan", work_items=[existing, other])
    assigned = command(supervisor, authority, "assign", work_item_id="repair", worker_id="repair-worker", requests=2,
                       model={"provider": "ollama", "model": "fixture"}).result
    other_context = AttemptContext(authority.scope, authority.run_id, assigned["attempt_id"], assigned["worker_id"], authority.epoch)
    other_lease = integration.create_writer(authority, other_context, base_revision=base)
    command(supervisor, authority, "worker_started", attempt_id=other_context.attempt_id, attempt_epoch=other_context.epoch)
    (Path(other_lease["path"]) / "a.txt").write_text("conflicting result\n")
    second = finish(setup, other_context, other_lease)
    combined = integration.prepare_candidate(authority, writer_ids=(first["id"], second["id"]), required_checks=(passing_check(),))
    assert combined["state"] == "conflict"
    assert "<<<<<<<" in (Path(combined["path"]) / "a.txt").read_text()
    assert (project / "a.txt").read_text() == "base-a\n"
    assert git(project, "rev-parse", "HEAD") == base


def test_failed_post_apply_persistence_retains_uncertainty_until_explicit_inspection(setup):
    store, supervisor, authority, integration, project, base = setup
    result = candidate(setup)
    integration.run_check(authority, result["id"], "combined")
    approval = approved(setup, result)
    with sqlite3.connect(store.path) as connection:
        connection.execute("CREATE TRIGGER fail_apply_receipt BEFORE INSERT ON events WHEN NEW.kind='candidate_applied' "
                           "BEGIN SELECT RAISE(ABORT,'fixture outcome persistence failure'); END")
    with pytest.raises(sqlite3.IntegrityError, match="persistence failure"):
        integration.apply(authority, result["id"], approval=approval)
    assert git(project, "rev-parse", "HEAD") == result["result_revision"]
    assert store.snapshot(authority.scope, authority.run_id)["integration_applications"][0]["state"] == "uncertain"
    with pytest.raises(Conflict, match="explicit inspection"):
        integration.apply(authority, result["id"], approval=approval)
    observed = integration.reconcile_application(authority, approval.id)
    assert observed["state"] == "applied"
    assert observed["observed_revision"] == result["result_revision"]


@pytest.mark.parametrize("destination", ["other_branch", "detached", "linked_worktree"])
def test_interrupted_application_cannot_use_a_different_checkout_at_the_same_target(setup, monkeypatch, destination):
    store, supervisor, authority, integration, project, base = setup
    result = candidate(setup)
    integration.run_check(authority, result["id"], "combined")
    approval = approved(setup, result)
    original = integration._git
    def fail_before_merge(path, *args, **kwargs):
        if args and args[0] == "merge":
            raise Conflict("Fixture stopped after application intent, before merge invocation")
        return original(path, *args, **kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(integration, "_git", fail_before_merge)
        with pytest.raises(Conflict, match="before merge"):
            integration.apply(authority, result["id"], approval=approval)
    assert git(project, "rev-parse", "main") == base
    observer = integration
    if destination == "linked_worktree":
        linked = project.parent / "other-checkout"
        git(project, "worktree", "add", "-b", "other-review", str(linked), result["result_revision"])
        observer = SwarmIntegration(store, linked, root=integration.root)
        assert observer.repo_key == integration.repo_key
    elif destination == "detached":
        git(project, "switch", "--detach", result["result_revision"])
    else:
        git(project, "switch", "-c", "other-review", result["result_revision"])
    with pytest.raises(Conflict, match="captured checkout"):
        observer.reconcile_application(authority, approval.id)
    assert git(project, "rev-parse", "main") == base
    state = store.snapshot(authority.scope, authority.run_id)
    assert state["integration_applications"][0]["state"] == "uncertain"
    assert state["integration_candidates"][0]["state"] == "verified"
    if destination != "linked_worktree":
        git(project, "switch", "main")
    observed = integration.reconcile_application(authority, approval.id)
    assert observed["state"] == "not_applied"
    assert git(project, "rev-parse", "main") == base  # Inspection never retries Git.


def test_apply_retains_uncertainty_if_branch_changes_after_admitted_merge(setup, monkeypatch):
    store, supervisor, authority, integration, project, base = setup
    result = candidate(setup)
    integration.run_check(authority, result["id"], "combined")
    approval = approved(setup, result)
    original = integration._git
    def switch_after_merge(path, *args, **kwargs):
        observed = original(path, *args, **kwargs)
        if args and args[0] == "merge":
            git(project, "switch", "-c", "other-review", result["result_revision"])
        return observed
    with monkeypatch.context() as patch:
        patch.setattr(integration, "_git", switch_after_merge)
        with pytest.raises(Conflict, match="captured checkout branch"):
            integration.apply(authority, result["id"], approval=approval)
    assert git(project, "branch", "--show-current") == "other-review"
    assert store.snapshot(authority.scope, authority.run_id)["integration_applications"][0]["state"] == "uncertain"
    git(project, "switch", "main")
    assert integration.reconcile_application(authority, approval.id)["state"] == "applied"


def test_repository_lock_excludes_a_second_process(setup):
    store, supervisor, authority, integration, project, base = setup
    code = """
import sys, time
from lumi.engine.swarming import SwarmStore
from lumi.engine.swarming.integration import SwarmIntegration
integration = SwarmIntegration(SwarmStore(sys.argv[1]), sys.argv[2], root=sys.argv[3])
with integration._repository_lock():
    print('locked', flush=True)
    time.sleep(1)
"""
    process = subprocess.Popen([sys.executable, "-c", code, str(store.path), str(project), str(integration.root)],
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        assert process.stdout.readline().strip() == "locked"
        with pytest.raises(Conflict, match="owns this repository"):
            with integration._repository_lock(timeout=0.1):
                pytest.fail("Both processes acquired the repository lock")
        output, error = process.communicate(timeout=10)
        assert process.returncode == 0, error
        with integration._repository_lock(timeout=0.1):
            pass
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        if process.stdout:
            process.stdout.close()
        if process.stderr:
            process.stderr.close()


def test_stop_waits_for_a_live_check_process_to_be_observed_stopped(setup):
    store, supervisor, authority, integration, project, base = setup
    context, lease = writers(setup, ("a",))[0]
    manifest = finish(setup, context, lease)
    result = integration.prepare_candidate(authority, writer_ids=(manifest["id"],), required_checks=(
        CheckSpec("long", (sys.executable, "-c", "import time; time.sleep(20)"), 30),))
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(integration.run_check, authority, result["id"], "long")
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            checks = store.snapshot(authority.scope, authority.run_id)["integration_checks"]
            if checks and checks[0]["job_id"]:
                break
            time.sleep(0.02)
        else:
            pytest.fail("Check process never acquired its owned job handle")
        assert command(supervisor, authority, "stop").state == "stopping"
        with pytest.raises(AdmissionClosed):
            future.result(timeout=10)
    snapshot = store.snapshot(authority.scope, authority.run_id)
    assert snapshot["integration_checks"][0]["state"] == "cancelled"
    assert snapshot["run"]["state"] == "cancelled"


def test_expired_owner_cannot_report_check_stop_as_completed_run(setup):
    store, supervisor, authority, integration, project, base = setup
    context, lease = writers(setup, ("a",))[0]
    manifest = finish(setup, context, lease)
    result = integration.prepare_candidate(authority, writer_ids=(manifest["id"],), required_checks=(
        CheckSpec("long", (sys.executable, "-c", "import time; time.sleep(20)"), 30),))
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(integration.run_check, authority, result["id"], "long")
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            checks = store.snapshot(authority.scope, authority.run_id)["integration_checks"]
            if checks and checks[0]["job_id"]:
                break
            time.sleep(0.02)
        else:
            pytest.fail("Check process never started")
        store.clock = lambda: 1031
        with pytest.raises(LeaseExpired):
            future.result(timeout=10)
    snapshot = store.snapshot(authority.scope, authority.run_id)
    assert snapshot["integration_checks"][0]["state"] == "uncertain"
    assert snapshot["run"]["state"] != "cancelled"


def test_ignored_user_file_is_preserved_when_candidate_adds_same_path(setup):
    store, supervisor, authority, integration, project, base = setup
    context, lease = writers(setup, ("a",), roots=(".",))[0]
    (Path(lease["path"]) / "artifact.txt").write_text("candidate output")
    manifest = finish(setup, context, lease)
    result = integration.prepare_candidate(authority, writer_ids=(manifest["id"],), required_checks=(
        CheckSpec("file", (sys.executable, "-c", "from pathlib import Path; assert Path('artifact.txt').read_text() == 'candidate output'"), 10),))
    integration.run_check(authority, result["id"], "file")
    with (project / ".git" / "info" / "exclude").open("a") as excludes:
        excludes.write("\nartifact.txt\n")
    (project / "artifact.txt").write_text("user's ignored local data")
    assert not git(project, "status", "--porcelain")
    with pytest.raises(Conflict, match="ignored user file"):
        integration.apply(authority, result["id"], approval=approved(setup, result))
    assert (project / "artifact.txt").read_text() == "user's ignored local data"
    assert git(project, "rev-parse", "HEAD") == base


def test_rejection_supersedes_previous_verified_candidate_before_repair(setup):
    store, supervisor, authority, integration, project, base = setup
    result = candidate(setup)
    integration.run_check(authority, result["id"], "combined")
    attempt = store.snapshot(authority.scope, authority.run_id)["attempts"][0]
    command(supervisor, authority, "reject", attempt_id=attempt["id"], attempt_epoch=attempt["epoch"], evidence="Trusted review found a missing requirement")
    command(supervisor, authority, "retry", work_item_id=attempt["work_item_id"], evidence="Repair the review finding")
    with pytest.raises(Conflict, match="checked"):
        integration.apply(authority, result["id"], approval=approved(setup, result))
    snapshot = store.snapshot(authority.scope, authority.run_id)
    assert snapshot["integration_candidates"][0]["state"] == "superseded"
    assert snapshot["integration_checks"][0]["state"] == "passed"
    assert snapshot["submissions"]


def test_writer_validation_rechecks_attempt_identity_and_pinned_history(setup):
    store, supervisor, authority, integration, project, base = setup
    context, lease = writers(setup, ("a",))[0]
    assert integration.validate_writer(context, lease["id"])["path"] == lease["path"]
    with pytest.raises(ScopeDenied):
        integration.validate_writer(replace(context, worker_id="forged"), lease["id"])
    git(Path(lease["path"]), "commit", "--allow-empty", "-m", "Unexpected history")
    with pytest.raises(Conflict, match="pinned input"):
        integration.validate_writer(context, lease["id"])


def test_branch_switch_at_identical_commit_does_not_retarget_application(setup):
    store, supervisor, authority, integration, project, base = setup
    result = candidate(setup)
    integration.run_check(authority, result["id"], "combined")
    git(project, "checkout", "-b", "different-user-branch")
    assert git(project, "rev-parse", "HEAD") == base
    with pytest.raises(Conflict, match="branch changed"):
        integration.apply(authority, result["id"], approval=approved(setup, result))
    assert (project / "a.txt").read_text() == "base-a\n"


def test_arbitrary_handoff_revision_cannot_authorize_writer_candidate(setup):
    store, supervisor, authority, integration, project, base = setup
    context, lease = writers(setup, ("a",))[0]
    (Path(lease["path"]) / "a.txt").write_text("new-a\n")
    manifest = integration.finalize_writer(authority, context, lease["id"])
    command(supervisor, authority, "submit", attempt_id=context.attempt_id, attempt_epoch=context.epoch,
            candidate_revision="model-claimed-revision", handoff="All good")
    command(supervisor, authority, "worker_stopped", attempt_id=context.attempt_id, attempt_epoch=context.epoch,
            outcome="submitted", evidence="fixture execution ended")
    with pytest.raises(Conflict, match="does not bind"):
        integration.prepare_candidate(authority, writer_ids=(manifest["id"],), required_checks=(passing_check(),))
