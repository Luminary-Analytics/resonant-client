"""Compare models on your own tasks before switching to one.

A comparison names a project (a git repository), up to ``MAX_TASKS`` tasks,
each a prompt and a check command (such as a test), and two to ``MAX_MODELS``
models (``provider:model``). Every task runs once per model as an unattended
``lumi run`` (lumi/headless.py) in its own detached git worktree of the
project's committed ``HEAD``, so runs don't see each other's changes or your
uncommitted work. Then the task's check runs in that worktree: exit code 0
passes. The worktree is removed afterwards; its diff is kept with the result.

Runs are ``lumi run`` processes, so the organization policy, budgets, the
audit log, file exclusions and the sandboxes apply, nothing asks a person, and
their model requests appear in Usage & cost. Repository instructions apply
only if the project is trusted in the app. The check is your own command: it
passes the command guardrails and runs in the shell sandbox when that's on.

Comparisons are kept in ``~/.lumi/model_evals/<id>.json``, diffs next to them.
One comparison runs at a time.
"""

from __future__ import annotations

import atexit
import json
import logging
import os
import re
import shutil
import statistics
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from .paths import state_home

if TYPE_CHECKING:
    from .gui.workspace_trust import TrustStatus

logger = logging.getLogger(__name__)

MAX_TASKS = 20
MAX_MODELS = 6
MAX_MINUTES = 60
MODES = ("ask", "auto-edit", "bypass")
CHECK_SECONDS = 600
MAX_DIFF_BYTES = 200_000
MAX_KEPT = 30
_MODEL = re.compile(r"^[A-Za-z0-9._-]+:\S+$")


class EvalError(ValueError):
    """A comparison that can't be saved or run; the message is safe to show."""


@dataclass
class Comparison:
    id: str
    name: str
    project: str
    tasks: list[dict]  # {"prompt", "check"}
    models: list[str]  # "provider:model"
    mode: str = "auto-edit"
    max_minutes: int = 10
    created_at: str = ""
    status: str = "ready"  # ready, running, done, stopped, failed
    started_at: str = ""
    finished_at: str = ""
    error: str = ""
    results: list[dict] = field(default_factory=list)


# ── Storage ─────────────────────────────────────────────────────────────────


def _folder() -> Path:
    return state_home() / "model_evals"


def _file(comparison_id: str) -> Path:
    return _folder() / f"{comparison_id}.json"


def _save(comparison: Comparison) -> None:
    path = _file(comparison.id)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(asdict(comparison), indent=2), encoding="utf-8")
    os.replace(temporary, path)


def get(comparison_id: str) -> Comparison | None:
    if not re.fullmatch(r"[a-f0-9]{10}", comparison_id or ""):
        return None
    try:
        data = json.loads(_file(comparison_id).read_text(encoding="utf-8"))
        return Comparison(**{k: v for k, v in data.items() if k in Comparison.__dataclass_fields__})
    except (OSError, ValueError, TypeError):
        return None


def load_all() -> list[Comparison]:
    found = []
    for path in sorted(_folder().glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
        comparison = get(path.stem)
        if comparison is not None:
            found.append(comparison)
    return found


def _git(project: str, *args: str, timeout: float = 60) -> subprocess.CompletedProcess:
    from .processes import background_process_kwargs

    return subprocess.run(["git", "-C", project, *args], capture_output=True, text=True, timeout=timeout,
                          encoding="utf-8", errors="replace", **background_process_kwargs())


def clean(raw: dict[str, Any]) -> Comparison:
    """Validate a comparison from Settings or the command line."""
    from .engine import guardrails
    from .policy import current as current_policy

    name = " ".join(str(raw.get("name") or "").split())[:80]
    if not name:
        raise EvalError("Give the comparison a name.")
    project = os.path.abspath(str(raw.get("project") or "").strip() or ".")
    if not os.path.isdir(project):
        raise EvalError(f"{project} isn't a folder.")
    try:
        head = _git(project, "rev-parse", "--verify", "HEAD^{commit}")
    except (OSError, subprocess.TimeoutExpired):
        raise EvalError("Git isn't available, and each run needs its own copy of the project.") from None
    if head.returncode != 0:
        raise EvalError(f"{project} needs to be a git repository with a commit: each run starts from HEAD.")
    tasks = []
    for item in raw.get("tasks") or []:
        prompt = str((item or {}).get("prompt") or "").strip()
        check = str((item or {}).get("check") or "").strip()
        if not prompt and not check:
            continue
        if not prompt or len(prompt) > 20_000:
            raise EvalError("Each task needs its prompt (up to 20,000 characters).")
        if not check or len(check) > 2000 or "\n" in check:
            raise EvalError("Each task needs a one-line check command, such as a test, that passes with exit code 0.")
        reason = guardrails.blocked(check)
        if reason:
            raise EvalError(guardrails.refusal(reason))
        tasks.append({"prompt": prompt, "check": check})
    if not 1 <= len(tasks) <= MAX_TASKS:
        raise EvalError(f"Add from 1 to {MAX_TASKS} tasks.")
    models = []
    for model in raw.get("models") or []:
        model = str(model).strip()
        if model and model not in models:
            if not _MODEL.match(model):
                raise EvalError(f"{model!r} isn't provider:model.")
            models.append(model)
    if not 2 <= len(models) <= MAX_MODELS:
        raise EvalError(f"Choose from 2 to {MAX_MODELS} models to compare.")
    policy = current_policy()
    for model in models:
        provider, _, name_ = model.partition(":")
        if policy and not policy.model_allowed(provider, name_):
            raise EvalError(f"{policy.organization}'s policy doesn't allow {model}.")
    mode = str(raw.get("mode") or "auto-edit")
    if mode not in MODES:
        raise EvalError("Choose ask, auto-edit or bypass.")
    if policy and not policy.mode_allowed(mode):
        raise EvalError(f"{policy.organization}'s policy doesn't allow the {mode} mode.")
    minutes = raw.get("max_minutes")
    try:
        max_minutes = 10 if minutes in (None, "") else int(minutes)
    except (TypeError, ValueError):
        max_minutes = 0
    if not 1 <= max_minutes <= MAX_MINUTES:
        raise EvalError(f"A run can last from 1 to {MAX_MINUTES} minutes.")
    return Comparison(id=uuid.uuid4().hex[:10], name=name, project=project, tasks=tasks, models=models,
                      mode=mode, max_minutes=max_minutes,
                      created_at=datetime.now(timezone.utc).isoformat(timespec="seconds"))


def create(raw: dict[str, Any]) -> Comparison:
    comparison = clean(raw)
    existing = load_all()
    for old in existing[MAX_KEPT - 1:]:  # keep the newest MAX_KEPT
        if runner.running_id != old.id:
            remove(old.id)
    _save(comparison)
    return comparison


def remove(comparison_id: str) -> None:
    comparison = get(comparison_id)
    if comparison is None:
        raise EvalError("That comparison no longer exists.")
    if runner.running_id == comparison_id:
        raise EvalError("Stop the comparison before removing it.")
    _file(comparison_id).unlink(missing_ok=True)
    shutil.rmtree(_folder() / comparison_id, ignore_errors=True)


# ── Results ─────────────────────────────────────────────────────────────────


def summary(comparison: Comparison) -> list[dict]:
    """Per model: tasks passed, runs, cost (None when no run was priced) and median time."""
    rows = []
    for model in comparison.models:
        results = [r for r in comparison.results if r.get("model") == model]
        costs = [r["cost_usd"] for r in results if isinstance(r.get("cost_usd"), (int, float))]
        times = [r["elapsed"] for r in results if isinstance(r.get("elapsed"), (int, float))]
        rows.append({
            "model": model,
            "passed": sum(1 for r in results if r.get("passed")),
            "runs": len(results),
            "tasks": len(comparison.tasks),
            "cost_usd": round(sum(costs), 6) if costs else None,
            "median_seconds": round(statistics.median(times), 1) if times else None,
        })
    return rows


def overview() -> dict[str, Any]:
    """The comparisons for Settings, newest first, with their summaries."""
    items = []
    for comparison in load_all():
        if comparison.status == "running" and runner.running_id != comparison.id:
            comparison.status = "interrupted"  # Lumi closed while it ran
        items.append({**asdict(comparison), "summary": summary(comparison)})
    return {"running": runner.running_id, "items": items}


# ── Running ─────────────────────────────────────────────────────────────────


def _lumi_command() -> tuple[list[str], str | None]:
    """How to start ``lumi run``: the installed app, or Python with this checkout."""
    if getattr(sys, "frozen", False):
        return [sys.executable], None
    return [sys.executable, "-m", "lumi"], str(Path(__file__).resolve().parent.parent)


class Runner:
    """Runs one comparison at a time in a background thread."""

    def __init__(self):
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._process: subprocess.Popen | None = None
        self.running_id = ""

    def start(self, comparison_id: str, on_update: Callable[[], None] = lambda: None) -> Comparison:
        comparison = get(comparison_id)
        if comparison is None:
            raise EvalError("That comparison no longer exists.")
        with self._lock:
            if self.running_id:
                raise EvalError("Another comparison is running; stop it or wait for it to finish.")
            comparison.status, comparison.results, comparison.error = "running", [], ""
            comparison.started_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
            comparison.finished_at = ""
            _save(comparison)
            self.running_id = comparison.id
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, args=(comparison, on_update), daemon=True,
                                            name=f"model-eval-{comparison.id}")
            self._thread.start()
        return comparison

    def stop(self) -> bool:
        """Stop the running comparison after ending its current run; False when none runs."""
        with self._lock:
            if not self.running_id:
                return False
            self._stop.set()
            process = self._process
        if process is not None and process.poll() is None:
            _end(process)
        return True

    def join(self, timeout: float | None = None) -> None:
        thread = self._thread
        if thread is not None:
            thread.join(timeout)

    def _run(self, comparison: Comparison, on_update: Callable[[], None]) -> None:
        from .gui.workspace_trust import WorkspaceTrust

        try:
            trust = WorkspaceTrust().status(comparison.project)
            work = _work_folder(comparison)
            for task_index, task in enumerate(comparison.tasks):
                for model in comparison.models:
                    if self._stop.is_set():
                        break
                    result = self._one(comparison, task_index, task, model, work, trust)
                    comparison.results.append(result)
                    _save(comparison)
                    _notify(on_update)
            comparison.status = "stopped" if self._stop.is_set() else "done"
        except Exception as exc:  # recorded, so the page says why
            logger.exception("Comparison %s failed", comparison.id)
            comparison.status, comparison.error = "failed", f"{exc.__class__.__name__}: {exc}"[:500]
        finally:
            comparison.finished_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
            _save(comparison)
            with self._lock:
                self.running_id, self._process = "", None
            _git(comparison.project, "worktree", "prune")
            _notify(on_update)

    def _one(self, comparison: Comparison, task_index: int, task: dict, model: str, work: Path,
             trust: TrustStatus) -> dict:
        provider, _, model_name = model.partition(":")
        result: dict[str, Any] = {"task": task_index, "model": model, "status": "", "passed": False,
                                  "exit_code": None, "cost_usd": None, "requests": 0, "elapsed": None,
                                  "changed_files": 0, "check_output": "", "error": "", "diff": ""}
        worktree = work / f"{task_index + 1}-{re.sub(r'[^A-Za-z0-9._-]+', '-', model)}"
        added = _git(comparison.project, "worktree", "add", "--detach", str(worktree), "HEAD", timeout=120)
        if added.returncode != 0:
            result.update(status="failed", error=f"git worktree add failed: {added.stderr.strip()[:500]}")
            return result
        try:
            command, cwd = _lumi_command()
            argv = [*command, "run", "--project", str(worktree), "--provider", provider, "--model", model_name,
                    "--mode", comparison.mode, "--timeout", str(comparison.max_minutes * 60),
                    "--output", "json"]
            if trust.trusted:
                # The run works on the last commit, whose lumi-policy.json may not
                # be the version the user trusted: its allow rules, which run
                # commands without asking in auto-edit, apply only if it is.
                argv += ["--trust-project", "--policy-digest",
                         trust.policy_digest if trust.honor_policy_allows else ""]
            started = time.monotonic()
            summary_json, error, code = self._lumi_run([*argv, task["prompt"]], cwd, comparison.max_minutes)
            result.update(exit_code=code, elapsed=round(time.monotonic() - started, 1), error=error)
            usage = summary_json.get("usage") or {}
            result.update(status=summary_json.get("status") or ("stopped" if self._stop.is_set() else "failed"),
                          cost_usd=usage.get("cost_usd"), requests=int(usage.get("calls") or 0))
            if self._stop.is_set():
                return result
            passed, output = _check(task["check"], str(worktree))
            result.update(passed=passed, check_output=output)
            result.update(_keep_diff(comparison, worktree))
            return result
        finally:
            removed = _git(comparison.project, "worktree", "remove", "--force", str(worktree), timeout=120)
            if removed.returncode != 0:
                shutil.rmtree(worktree, ignore_errors=True)

    def _lumi_run(self, argv: list[str], cwd: str | None, max_minutes: int) -> tuple[dict, str, int | None]:
        from .processes import background_process_kwargs

        process = subprocess.Popen(argv, cwd=cwd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace",
                                   **background_process_kwargs(new_process_group=True))
        with self._lock:
            self._process = process
        try:
            out, err = process.communicate(timeout=max_minutes * 60 + 120)
        except subprocess.TimeoutExpired:
            _end(process)
            out, err = process.communicate()
            err = (err or "") + "\nThe run didn't end in time and was stopped."
        finally:
            with self._lock:
                self._process = None
        try:
            parsed = json.loads(out or "{}")
        except ValueError:
            parsed = {}
        problems = "; ".join(str(e.get("message") or "") for e in parsed.get("errors") or [] if isinstance(e, dict))
        return parsed, (problems or (err or "").strip())[:1000], process.returncode


def _end(process: subprocess.Popen) -> None:
    """End a run and whatever it started."""
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(process.pid)], capture_output=True, timeout=30)
        else:
            import signal

            os.killpg(process.pid, signal.SIGTERM)
    except (OSError, subprocess.TimeoutExpired):
        process.kill()


def _check(command: str, worktree: str) -> tuple[bool, str]:
    """Run the task's check in the worktree: (passed, the end of its output)."""
    from .engine import os_sandbox
    from .processes import background_process_kwargs
    from .secrets_store import child_env

    try:
        wrapped = os_sandbox.prepare_shell(command, roots=[worktree], cwd=worktree)
    except ValueError as exc:  # the sandbox is on and can't run here
        return False, str(exc)
    try:
        done = subprocess.run(wrapped or command, shell=wrapped is None, cwd=worktree, capture_output=True,
                              text=True, encoding="utf-8", errors="replace", timeout=CHECK_SECONDS,
                              env=child_env(), **background_process_kwargs())
    except subprocess.TimeoutExpired:
        return False, f"The check didn't finish within {CHECK_SECONDS // 60} minutes."
    except OSError as exc:
        return False, f"The check didn't start: {exc}"
    output = ((done.stdout or "") + (done.stderr or "")).strip()
    return done.returncode == 0, f"exit {done.returncode}\n{output[-2000:]}".strip()


def _keep_diff(comparison: Comparison, worktree: Path) -> dict:
    """The run's changes: how many files, and the diff saved next to the comparison."""
    _git(str(worktree), "add", "-A")
    names = _git(str(worktree), "diff", "--cached", "--name-only").stdout.split("\n")
    changed = [name for name in names if name.strip()]
    diff = _git(str(worktree), "diff", "--cached").stdout
    folder = _folder() / comparison.id
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{worktree.name}.diff"
    data = diff.encode("utf-8", "replace")
    path.write_bytes(data[:MAX_DIFF_BYTES] + (b"\n... (diff truncated)\n" if len(data) > MAX_DIFF_BYTES else b""))
    return {"changed_files": len(changed), "diff": str(path)}


def _work_folder(comparison: Comparison) -> Path:
    from .engine.artifacts import project_state_dir

    work = project_state_dir(Path(comparison.project)) / "evals" / comparison.id
    shutil.rmtree(work, ignore_errors=True)  # anything a stopped app left behind
    _git(comparison.project, "worktree", "prune")
    work.mkdir(parents=True, exist_ok=True)
    return work


def _notify(on_update: Callable[[], None]) -> None:
    try:
        on_update()
    except Exception:
        logger.debug("Comparison update callback failed", exc_info=True)


runner = Runner()
atexit.register(runner.stop)
