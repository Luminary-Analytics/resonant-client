"""Comparing models on your own tasks (lumi/model_evals.py), with a fake `lumi run`."""

from __future__ import annotations

import asyncio
import hashlib
import json
import pathlib
import subprocess
import sys
import textwrap
import threading
from types import SimpleNamespace

import pytest

from lumi import model_evals
from lumi.gui import ws_commands

FAKE_RUN = textwrap.dedent("""\
    import json, pathlib, subprocess, sys, time
    args = sys.argv[1:]
    assert args[0] == "run"
    option = lambda name: args[args.index(name) + 1]
    project, model = pathlib.Path(option("--project")), option("--model")
    git = lambda *more: subprocess.run(["git", "-C", str(project), "-c", "user.email=t@example.com",
                                        "-c", "user.name=T", *more], check=True, capture_output=True)
    if model == "slow":
        time.sleep(120)
    if model == "good":
        (project / "fixed.txt").write_text("fixed", encoding="utf-8")
    if model == "committer":  # commits as it goes: a model in bypass can run git commit
        (project / "fixed.txt").write_text("fixed", encoding="utf-8")
        git("add", "fixed.txt")
        git("commit", "-q", "-m", "Fix")
        (project / "app.py").write_text("print('fixed')", encoding="utf-8")
        git("commit", "-q", "-am", "Tidy")
    if model == "bulky":  # commits more than a kept diff holds
        (project / "fixed.txt").write_text("x" * 300_000, encoding="utf-8")
        git("add", "fixed.txt")
        git("commit", "-q", "-m", "Big")
    (project / "notes.txt").write_text(args[-1], encoding="utf-8")  # the prompt
    print(json.dumps({"status": "completed", "errors": [],
                      "usage": {"cost_usd": 0.02 if model == "good" else 0.01, "calls": 2}}))
""")
CHECK = f'"{sys.executable}" -c "import pathlib, sys; sys.exit(0 if pathlib.Path(\'fixed.txt\').exists() else 1)"'


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    (root / "app.py").write_text("print('hi')\n", encoding="utf-8")
    for args in (["init", "-q"], ["add", "."],
                 ["-c", "user.email=t@example.com", "-c", "user.name=T", "commit", "-q", "-m", "start"]):
        subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)
    return str(root)


@pytest.fixture(autouse=True)
def fake_lumi(tmp_path, monkeypatch):
    script = tmp_path / "fake_lumi.py"
    script.write_text(FAKE_RUN, encoding="utf-8")
    monkeypatch.setattr(model_evals, "_lumi_command", lambda: ([sys.executable, str(script)], None))
    yield
    model_evals.runner.stop()
    model_evals.runner.join(10)


def _raw(repo_path, **extra):
    return {"name": "Fixes", "project": repo_path, "models": ["ollama:good", "ollama:bad"],
            "tasks": [{"prompt": "Create fixed.txt", "check": CHECK}], **extra}


def test_a_comparison_is_checked_before_it_is_saved(repo, tmp_path, monkeypatch):
    def refused(match, **extra):
        with pytest.raises(model_evals.EvalError, match=match):
            model_evals.clean(_raw(repo, **extra))

    refused("name", name=" ")
    refused("isn't a folder", project=str(tmp_path / "missing"))
    not_git = tmp_path / "plain"
    not_git.mkdir()
    refused("git repository", project=str(not_git))
    refused("prompt", tasks=[{"prompt": "", "check": "true"}])
    refused("one-line check", tasks=[{"prompt": "x", "check": ""}])
    refused("one-line check", tasks=[{"prompt": "x", "check": "a\nb"}])
    refused("never allowed", tasks=[{"prompt": "x", "check": "rm -rf /"}])
    refused("from 1 to 20 tasks", tasks=[])
    refused("from 2 to 6 models", models=["ollama:good"])
    refused("provider:model", models=["ollama:good", "just-a-name"])
    refused("ask, auto-edit or bypass", mode="yolo")
    refused("from 1 to 60 minutes", max_minutes=0)

    from lumi import policy

    acme = SimpleNamespace(organization="Acme", model_allowed=lambda provider, model: model != "bad",
                           mode_allowed=lambda mode: mode != "bypass")
    monkeypatch.setattr(policy, "current", lambda: acme)
    refused("Acme's policy doesn't allow ollama:bad")
    refused("Acme's policy doesn't allow the bypass mode", models=["ollama:good", "ollama:other"], mode="bypass")

    monkeypatch.setattr(policy, "current", lambda: None)
    comparison = model_evals.clean(_raw(repo, tasks=[{"prompt": "x", "check": CHECK}, {"prompt": "", "check": ""}]))
    assert len(comparison.tasks) == 1 and comparison.models == ["ollama:good", "ollama:bad"]
    assert (comparison.mode, comparison.max_minutes, comparison.status) == ("auto-edit", 10, "ready")


def test_each_model_runs_each_task_in_its_own_copy(repo):
    comparison = model_evals.create(_raw(repo, tasks=[{"prompt": "Create fixed.txt", "check": CHECK},
                                                      {"prompt": "Again", "check": CHECK}]))
    updates = []
    model_evals.runner.start(comparison.id, lambda: updates.append(model_evals.runner.running_id))
    model_evals.runner.join(60)

    done = model_evals.get(comparison.id)
    assert done.status == "done" and len(done.results) == 4
    assert [(r["task"], r["model"], r["passed"]) for r in done.results] == [
        (0, "ollama:good", True), (0, "ollama:bad", False), (1, "ollama:good", True), (1, "ollama:bad", False)]
    good = done.results[0]
    assert good["status"] == "completed" and good["cost_usd"] == 0.02 and good["requests"] == 2
    assert good["changed_files"] == 2 and good["check_output"].startswith("exit 0")
    diff = open(good["diff"], encoding="utf-8").read()
    assert "fixed.txt" in diff and "Create fixed.txt" in diff  # the prompt reached the run
    assert done.results[1]["check_output"].startswith("exit 1")

    # Your checkout is untouched and no worktree is left behind.
    status = subprocess.run(["git", "-C", repo, "status", "--porcelain"], capture_output=True, text=True).stdout
    worktrees = subprocess.run(["git", "-C", repo, "worktree", "list"], capture_output=True, text=True).stdout
    assert status == "" and len(worktrees.strip().splitlines()) == 1

    rows = model_evals.summary(done)
    assert [{k: v for k, v in row.items() if k != "median_seconds"} for row in rows] == [
        {"model": "ollama:good", "passed": 2, "runs": 2, "tasks": 2, "cost_usd": 0.04},
        {"model": "ollama:bad", "passed": 0, "runs": 2, "tasks": 2, "cost_usd": 0.02},
    ]
    assert all(isinstance(row["median_seconds"], float) for row in rows)
    assert len(updates) == 5  # after each run, and at the end
    assert model_evals.overview()["items"][0]["summary"][0]["passed"] == 2


def test_the_kept_diff_includes_what_a_run_committed(repo):
    # A run can commit its work: a model in bypass can run git commit. Its diff
    # and changed files are taken against the commit it started from, not the
    # worktree's HEAD at the end.
    def head() -> str:
        return subprocess.run(["git", "-C", repo, "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()

    start = head()
    comparison = model_evals.create(_raw(repo, models=["ollama:committer", "ollama:bulky"]))
    model_evals.runner.start(comparison.id)
    model_evals.runner.join(60)

    done = model_evals.get(comparison.id)
    committer, bulky = done.results
    # committer: fixed.txt and app.py in two commits, then notes.txt uncommitted.
    assert [(r["model"], r["status"], r["passed"], r["changed_files"]) for r in done.results] == [
        ("ollama:committer", "completed", True, 3), ("ollama:bulky", "completed", True, 2)]
    lines = pathlib.Path(committer["diff"]).read_text(encoding="utf-8").splitlines()
    assert {"+fixed", "-print('hi')", "+print('fixed')", "+Create fixed.txt"} <= set(lines)
    assert committer["start_commit"] == bulky["start_commit"] == start

    # What a run committed counts toward the 200 KB a kept diff holds.
    marker = b"\n... (diff truncated)\n"
    kept = pathlib.Path(bulky["diff"]).read_bytes()
    assert b"+" + b"x" * 1000 in kept and kept.endswith(marker)
    assert len(kept) == model_evals.MAX_DIFF_BYTES + len(marker)

    # The commits stayed in the runs' worktrees: your checkout is where it was.
    status = subprocess.run(["git", "-C", repo, "status", "--porcelain"], capture_output=True, text=True).stdout
    assert head() == start and status == ""


def test_a_file_named_like_the_start_commit_does_not_empty_the_diff(repo):
    # git refuses an argument that names both a revision and a file, and the
    # kept diff would be empty.
    start = subprocess.run(["git", "-C", repo, "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    (pathlib.Path(repo) / start).write_text("named like the commit", encoding="utf-8")
    kept = model_evals._keep_diff(SimpleNamespace(id="0123456789"), pathlib.Path(repo), start)
    assert kept["changed_files"] == 1
    assert "+named like the commit" in pathlib.Path(kept["diff"]).read_text(encoding="utf-8").splitlines()


def test_runs_trust_only_the_policy_version_the_user_trusted(repo, tmp_path, monkeypatch):
    # A run works on the last commit. Its lumi-policy.json allow rules, which
    # run commands without asking in auto-edit, apply only in the version the
    # user trusted in the app, and not at all while a change awaits review.
    from lumi.gui.workspace_trust import WorkspaceTrust

    log = tmp_path / "argv.log"
    script = tmp_path / "logging_lumi.py"
    script.write_text(f"import sys\nopen({str(log)!r}, 'a', encoding='utf-8').write('\\t'.join(sys.argv[1:]) + '\\n')\n"
                      + FAKE_RUN, encoding="utf-8")
    monkeypatch.setattr(model_evals, "_lumi_command", lambda: ([sys.executable, str(script)], None))
    policy = pathlib.Path(repo) / "lumi-policy.json"
    policy.write_text(json.dumps({"rules": [{"tool_pattern": "bash", "action": "allow"}]}), encoding="utf-8")
    for args in (["add", "."], ["-c", "user.email=t@example.com", "-c", "user.name=T", "commit", "-q", "-m", "policy"]):
        subprocess.run(["git", "-C", repo, *args], check=True, capture_output=True)

    def digests_passed() -> list:
        comparison = model_evals.create(_raw(repo, mode="auto-edit"))
        model_evals.runner.start(comparison.id)
        model_evals.runner.join(60)
        runs = [line.split("\t") for line in log.read_text(encoding="utf-8").splitlines()]
        log.unlink()
        return [args[args.index("--policy-digest") + 1] if "--trust-project" in args else None for args in runs]

    assert digests_passed() == [None, None]  # not trusted
    WorkspaceTrust().trust(repo)
    assert digests_passed() == [hashlib.sha256(policy.read_bytes()).hexdigest()] * 2
    policy.write_text(json.dumps({"rules": [{"tool_pattern": "*", "action": "allow"}]}), encoding="utf-8")
    assert digests_passed() == ["", ""]  # trusted, but this version isn't reviewed


def test_stopping_ends_the_current_run(repo):
    comparison = model_evals.create(_raw(repo, models=["ollama:slow", "ollama:good"]))
    started = threading.Event()
    model_evals.runner.start(comparison.id, started.set)
    with pytest.raises(model_evals.EvalError, match="Another comparison is running"):
        model_evals.runner.start(comparison.id)
    with pytest.raises(model_evals.EvalError, match="Stop the comparison"):
        model_evals.remove(comparison.id)
    import time

    deadline = time.monotonic() + 20
    while model_evals.runner._process is None and time.monotonic() < deadline:
        time.sleep(0.05)
    assert model_evals.runner.stop() is True
    model_evals.runner.join(30)
    stopped = model_evals.get(comparison.id)
    assert stopped.status == "stopped" and len(stopped.results) == 1
    assert stopped.results[0]["passed"] is False and model_evals.runner.running_id == ""
    assert model_evals.runner.stop() is False


def test_an_interrupted_comparison_is_shown_as_such_and_can_be_removed(repo):
    comparison = model_evals.create(_raw(repo))
    comparison.status = "running"  # as Lumi left it when it closed
    model_evals._save(comparison)
    assert model_evals.overview()["items"][0]["status"] == "interrupted"
    model_evals.remove(comparison.id)
    assert model_evals.get(comparison.id) is None and model_evals.load_all() == []
    with pytest.raises(model_evals.EvalError, match="no longer exists"):
        model_evals.remove(comparison.id)


class _WS:
    def __init__(self):
        self.sent = []

    async def send_json(self, payload):
        self.sent.append(payload)


def _command(name, **msg):
    ctx = ws_commands.CommandContext(ws=_WS(), state=SimpleNamespace(), msg={"command": name, **msg},
                                     runs=SimpleNamespace(busy=False))
    asyncio.run(ws_commands.HANDLERS[name](ctx))
    return ctx.ws.sent


def test_the_settings_page_commands(repo):
    assert _command("model_evals_list")[0] == {"event": "model_evals", "data": {"running": "", "items": []}}
    sent = _command("model_eval_save", comparison=_raw(repo))
    saved = sent[0]["data"]["saved"]
    assert sent[0]["data"]["items"][0]["id"] == saved
    assert _command("model_eval_save", comparison=_raw(repo, models=[]))[0]["event"] == "model_eval_error"
    assert _command("model_eval_save", comparison="nope")[0]["event"] == "error"
    assert _command("model_eval_change", id=saved, action="explode")[0]["event"] == "model_eval_error"
    assert _command("model_eval_change", id=saved, action="remove")[0]["data"]["items"] == []
