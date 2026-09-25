"""Scheduled tasks: a saved prompt that runs unattended at set times.

A schedule names a project, a prompt, a model and a permission mode, and the
days and time (this computer's local time) it runs. Schedules live in
``~/.lumi/schedules.json``. The operating system's scheduler starts
``lumi schedule run <id>``, so the desktop app doesn't need to be open:

* **Windows:** a Task Scheduler task ``Lumi\\<id>`` for this user (``schtasks``);
* **macOS:** a LaunchAgent, ``~/Library/LaunchAgents/ai.lumi.schedule.<id>.plist``
  (``launchctl``);
* **Linux:** a line in this user's crontab, marked with the schedule's id.

A run is ``lumi run`` (lumi/headless.py) with the schedule's settings, so the
organization policy, budgets, the audit log, file exclusions, the sandboxes
and the person's Settings hooks apply as for any unattended run, and nothing
asks a person. Repository instructions apply only if the project is trusted in
the app; a schedule never trusts one itself. A run stops after the schedule's
``max_minutes`` (a hook already running finishes first), and a
schedule never runs twice at once (``running.json`` names the process). Each
run's result (the ``lumi run`` JSON summary) is kept under
``~/.lumi/schedules/<id>/``, the last ``KEEP_RUNS`` of them.

``security.scheduled_tasks`` (Settings, or locked by an organization policy)
turns the feature off: nothing can be added or resumed, and a run the
operating system still starts is refused and recorded as such.
"""

from __future__ import annotations

import io
import json
import logging
import os
import plistlib
import re
import shlex
import shutil
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .paths import state_home

logger = logging.getLogger(__name__)

DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
MODES = ("ask", "auto-edit", "bypass")
KEEP_RUNS = 30
MAX_SCHEDULES = 50
MAX_MINUTES = 720
MARKER = "# lumi-schedule:"
CLAIM_ENV = "LUMI_SCHEDULE_CLAIM"
_TIME = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")
_lock = threading.Lock()


class ScheduleError(ValueError):
    """A schedule that can't be saved or registered; the message is safe to show."""


@dataclass
class Schedule:
    id: str
    name: str
    prompt: str
    project: str
    time: str  # "HH:MM", local time
    days: list[str] = field(default_factory=list)  # empty: every day
    provider: str = ""
    model: str = ""
    mode: str = "ask"
    max_minutes: int = 60
    enabled: bool = True
    created_at: str = ""

    def describe(self) -> str:
        days = "every day" if not self.days or len(self.days) == 7 else ", ".join(d.capitalize() for d in self.days)
        return f"{days} at {self.time}"


# ── Storage ─────────────────────────────────────────────────────────────────


def _home() -> Path:
    return state_home()


def _file() -> Path:
    return _home() / "schedules.json"


def runs_folder(schedule_id: str) -> Path:
    return _home() / "schedules" / schedule_id


def load() -> list[Schedule]:
    try:
        data = json.loads(_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    result = []
    for item in data if isinstance(data, list) else []:
        try:
            result.append(Schedule(**{k: v for k, v in item.items() if k in Schedule.__dataclass_fields__}))
        except TypeError:
            continue
    return result


def _write(items: list[Schedule]) -> None:
    path = _file()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps([asdict(s) for s in items], indent=2), encoding="utf-8")
    os.replace(temporary, path)


def get(schedule_id: str) -> Schedule | None:
    return next((s for s in load() if s.id == schedule_id), None)


def clean(raw: dict[str, Any], *, existing: Schedule | None = None) -> Schedule:
    """Validate a schedule from the CLI or Settings."""
    name = " ".join(str(raw.get("name") or "").split())[:80]
    if not name:
        raise ScheduleError("Give the schedule a name.")
    prompt = str(raw.get("prompt") or "").strip()
    if not prompt or len(prompt) > 20_000:
        raise ScheduleError("Write the task to run (up to 20,000 characters).")
    project = os.path.abspath(str(raw.get("project") or "").strip() or ".")
    if not os.path.isdir(project):
        raise ScheduleError(f"{project} isn't a folder.")
    time = str(raw.get("time") or "").strip()
    if not _TIME.match(time):
        raise ScheduleError("Enter the time as HH:MM, for example 02:30.")
    days = raw.get("days") or []
    if isinstance(days, str):
        days = [d for d in re.split(r"[\s,]+", days) if d]
    days = [str(d).lower()[:3] for d in days]
    if any(d not in DAYS for d in days):
        raise ScheduleError("Days are mon, tue, wed, thu, fri, sat and sun.")
    mode = str(raw.get("mode") or "ask")
    if mode not in MODES:
        raise ScheduleError("Choose ask (read only), auto-edit or bypass.")
    minutes = raw.get("max_minutes")
    try:
        max_minutes = 60 if minutes in (None, "") else int(minutes)
    except (TypeError, ValueError):
        max_minutes = 0
    if not 1 <= max_minutes <= MAX_MINUTES:
        raise ScheduleError(f"A run can last from 1 to {MAX_MINUTES} minutes.")
    return Schedule(
        id=existing.id if existing else uuid.uuid4().hex[:10],
        name=name, prompt=prompt, project=project, time=time,
        days=sorted(set(days), key=DAYS.index) if len(set(days)) < 7 else [],
        provider=str(raw.get("provider") or "").strip(), model=str(raw.get("model") or "").strip(),
        mode=mode, max_minutes=max_minutes,
        enabled=raw.get("enabled", existing.enabled if existing else True) is not False,
        created_at=existing.created_at if existing else datetime.now(timezone.utc).isoformat(timespec="seconds"),
    )


def check_allowed(mode: str = "", settings: Any = None) -> None:
    """Refuse when scheduled tasks are turned off (here or by policy), or the policy doesn't allow ``mode``."""
    from .policy import current as current_policy

    if settings is None:
        from .gui.settings import SettingsManager

        settings = SettingsManager()
    if settings.get("security", "scheduled_tasks", True) is False:
        raise ScheduleError("Scheduled tasks are turned off in Settings > Privacy & security "
                            "(or by your organization's policy).")
    policy = current_policy()
    if mode and policy and not policy.mode_allowed(mode):
        raise ScheduleError(f"{policy.organization}'s policy doesn't allow the {mode} mode.")


def save(raw: dict[str, Any], schedule_id: str = "", *, settings: Any = None) -> Schedule:
    """Add or change a schedule and register it with the operating system."""
    with _lock:
        items = load()
        existing = next((s for s in items if s.id == schedule_id), None) if schedule_id else None
        if schedule_id and existing is None:
            raise ScheduleError("That schedule no longer exists.")
        if existing is None and len(items) >= MAX_SCHEDULES:
            raise ScheduleError(f"Lumi keeps up to {MAX_SCHEDULES} schedules. Remove one first.")
        schedule = clean(raw, existing=existing)
        if schedule.enabled:
            check_allowed(schedule.mode, settings)  # pausing is always possible
        registrar().unregister(schedule.id)
        if schedule.enabled:
            registrar().register(schedule)
        _write([s for s in items if s.id != schedule.id] + [schedule])
    return schedule


def set_enabled(schedule_id: str, enabled: bool, *, settings: Any = None) -> Schedule:
    schedule = get(schedule_id)
    if schedule is None:
        raise ScheduleError("That schedule no longer exists.")
    return save({**asdict(schedule), "enabled": enabled}, schedule_id, settings=settings)


def remove(schedule_id: str) -> None:
    """Delete a schedule, its operating system entry and its kept results."""
    with _lock:
        items = load()
        if not any(s.id == schedule_id for s in items):
            raise ScheduleError("That schedule no longer exists.")
        registrar().unregister(schedule_id)
        _write([s for s in items if s.id != schedule_id])
    shutil.rmtree(runs_folder(schedule_id), ignore_errors=True)


# ── Running ─────────────────────────────────────────────────────────────────


def argv_for(schedule: Schedule) -> list[str]:
    """The ``lumi run`` arguments a schedule runs with."""
    args = ["--project", schedule.project, "--mode", schedule.mode, "--output", "json",
            "--timeout", str(schedule.max_minutes * 60)]
    if schedule.provider:
        args += ["--provider", schedule.provider]
    if schedule.model:
        args += ["--model", schedule.model]
    return [*args, schedule.prompt]


def _running_file(schedule_id: str) -> Path:
    return runs_folder(schedule_id) / "running.json"


def _owner(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return {"pid": int(data["pid"]), "created": float(data["created"]), "token": str(data.get("token", ""))}
    except (OSError, ValueError, KeyError, TypeError):
        return None


def running(schedule_id: str) -> bool:
    """Whether a run of this schedule is in progress: the process in ``running.json`` is alive."""
    import psutil

    path = _running_file(schedule_id)
    owner = _owner(path)
    if owner is None:
        # Missing; or being written by a run that is starting this moment.
        try:
            return time.time() - path.stat().st_mtime < 10
        except OSError:
            return False
    try:
        # The creation time tells a reused process id apart.
        return abs(psutil.Process(owner["pid"]).create_time() - owner["created"]) < 1
    except (psutil.Error, OSError):
        return False


def _claim(schedule_id: str, *, pid: int = 0, token: str = "") -> bool:
    """Record the process running the schedule; False when another run is in progress.

    ``start`` claims for the process it starts (``pid``), with a one-time token
    that process finds in ``CLAIM_ENV`` and takes the claim over with, so two
    quick "Run now"s can't both start a run.
    """
    import psutil

    path = _running_file(schedule_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    process = psutil.Process(pid) if pid else psutil.Process()
    record = json.dumps({"pid": process.pid, "created": process.create_time(), "token": token})
    for _ in range(2):
        try:
            handle = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            owner = _owner(path)
            if not pid and token and owner and owner["token"] == token:
                temporary = path.with_name(f"running.{os.getpid()}.tmp")
                temporary.write_text(record, encoding="utf-8")
                os.replace(temporary, path)
                return True
            if running(schedule_id):
                return False
            path.unlink(missing_ok=True)  # left by a run that ended without cleaning up
            continue
        with os.fdopen(handle, "w", encoding="utf-8") as file:
            file.write(record)
        return True
    return False


def run(schedule_id: str) -> int:
    """Run a schedule now, keep its result and return ``lumi run``'s exit code."""
    # Taken out of the environment so the agent's own commands don't inherit it.
    token = os.environ.pop(CLAIM_ENV, "")
    schedule = get(schedule_id)
    if schedule is None:
        raise ScheduleError("That schedule no longer exists.")
    try:
        check_allowed(schedule.mode)
    except ScheduleError as exc:
        _keep(schedule_id, datetime.now(timezone.utc), 2, {"status": "refused"}, str(exc))
        raise
    if not _claim(schedule_id, token=token):
        raise ScheduleError("This schedule is already running.")
    try:
        return _run_claimed(schedule)
    finally:
        _running_file(schedule_id).unlink(missing_ok=True)


def _run_claimed(schedule: Schedule) -> int:
    from . import headless

    schedule_id = schedule.id
    started = datetime.now(timezone.utc)
    out, err = io.StringIO(), io.StringIO()
    try:
        code = headless.main(argv_for(schedule), stdin=io.StringIO(""), stdout=out, stderr=err)
    except Exception as exc:  # keep a record of the failure, whatever it was
        logger.exception("Scheduled run %s failed", schedule_id)
        code, err = 1, io.StringIO(f"{exc.__class__.__name__}: {exc}")
    try:
        summary = json.loads(out.getvalue() or "{}")
    except ValueError:
        summary = {}
    _keep(schedule_id, started, code, summary, err.getvalue().strip())
    return code


def _keep(schedule_id: str, started: datetime, code: int, summary: dict, stderr: str) -> None:
    """Write a run's result and drop the oldest past ``KEEP_RUNS``."""
    # The run's own errors (a refused request, an exhausted budget) when it
    # got as far as a summary; otherwise what ``lumi run`` printed.
    problems = "; ".join(str(e.get("message") or "") for e in summary.get("errors") or [] if isinstance(e, dict))
    record = {"started_at": started.isoformat(timespec="seconds"),
              "finished_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
              "exit_code": code, "status": summary.get("status") or ("failed" if code else "done"),
              "outcome": summary.get("outcome", ""), "answer": str(summary.get("text") or "")[:4000],
              "changed_files": list(summary.get("changed_files") or [])[:50],
              "error": (problems or stderr)[:2000],
              "cost_usd": (summary.get("usage") or {}).get("cost_usd")}
    folder = runs_folder(schedule_id)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{started.strftime('%Y%m%dT%H%M%SZ')}.json").write_text(
        json.dumps({**record, "summary": summary}, indent=2), encoding="utf-8")
    for old in sorted(folder.glob("2*.json"))[:-KEEP_RUNS]:
        old.unlink(missing_ok=True)


def runs(schedule_id: str, limit: int = KEEP_RUNS) -> list[dict]:
    """The kept results, newest first. A run writes only these, never ``schedules.json``,
    so it can't undo an edit made while it ran."""
    result = []
    for path in sorted(runs_folder(schedule_id).glob("2*.json"), reverse=True):
        if len(result) >= limit:
            break
        try:
            result.append(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            continue
    return result


def last_run(schedule_id: str) -> dict:
    """The newest result without its full ``lumi run`` summary, or {} before the first run."""
    newest = runs(schedule_id, limit=1)
    return {k: v for k, v in newest[0].items() if k != "summary"} if newest else {}


def start(schedule_id: str, *, settings: Any = None) -> subprocess.Popen:
    """Run a schedule now in its own process, as the scheduler would; it outlives the app."""
    from .processes import background_process_kwargs

    schedule = get(schedule_id)
    if schedule is None:
        raise ScheduleError("That schedule no longer exists.")
    check_allowed(schedule.mode, settings)
    if running(schedule_id):
        raise ScheduleError("This schedule is already running.")
    # Running from source: the checkout this app runs from, not whichever
    # copy of the package "python -m lumi" would find first.
    folder = None if getattr(sys, "frozen", False) else str(Path(__file__).resolve().parent.parent)
    token = uuid.uuid4().hex
    process = subprocess.Popen([*command(), "schedule", "run", schedule_id], cwd=folder,
                               env={**os.environ, CLAIM_ENV: token}, stdin=subprocess.DEVNULL,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **background_process_kwargs())
    try:
        _claim(schedule_id, pid=process.pid, token=token)
    except Exception:  # it already finished, or claimed the run itself first
        pass
    return process


def overview(settings: Any = None) -> dict[str, Any]:
    """The schedules for Settings, with whether each is running and what schedules them."""
    try:
        check_allowed(settings=settings)
        turned_off = ""
    except ScheduleError as exc:
        turned_off = str(exc)
    return {"scheduler": {"win32": "Task Scheduler", "darwin": "launchd"}.get(sys.platform, "cron"),
            "turned_off": turned_off,
            "items": [{**asdict(s), "when": s.describe(), "running": running(s.id), "last_run": last_run(s.id)}
                      for s in load()]}


# ── Registering with the operating system ───────────────────────────────────


def command() -> list[str]:
    """How the scheduler starts Lumi: the installed app, or Python with this checkout."""
    if getattr(sys, "frozen", False):
        return [sys.executable]
    return [sys.executable, "-m", "lumi"]


def _windowless(args: list[str]) -> list[str]:
    """On Windows, pythonw.exe for Python, so a run from source doesn't open a console window."""
    candidate = Path(args[0]).with_name("pythonw.exe")
    if args[0].lower().endswith("python.exe") and candidate.exists():
        return [str(candidate), *args[1:]]
    return args


class Registrar:
    def register(self, schedule: Schedule) -> None:
        raise NotImplementedError

    def unregister(self, schedule_id: str) -> None:
        raise NotImplementedError


def _run(args: list[str], **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True, timeout=30,
                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), **kwargs)


class WindowsTasks(Registrar):
    """Task Scheduler, for this user only."""

    def register(self, schedule: Schedule) -> None:
        action = subprocess.list2cmdline([*_windowless(command()), "schedule", "run", schedule.id])
        args = ["schtasks", "/Create", "/TN", f"Lumi\\{schedule.id}", "/TR", action, "/ST", schedule.time, "/F"]
        if schedule.days:
            args += ["/SC", "WEEKLY", "/D", ",".join(d.upper() for d in schedule.days)]
        else:
            args += ["/SC", "DAILY"]
        result = _run(args)
        if result.returncode != 0:
            raise ScheduleError(f"Task Scheduler refused the schedule: {(result.stderr or result.stdout).strip()}")

    def unregister(self, schedule_id: str) -> None:
        _run(["schtasks", "/Delete", "/TN", f"Lumi\\{schedule_id}", "/F"])


class LaunchAgents(Registrar):
    """A LaunchAgent in the user's Library, loaded with launchctl."""

    def _plist(self, schedule_id: str) -> Path:
        return Path.home() / "Library" / "LaunchAgents" / f"ai.lumi.schedule.{schedule_id}.plist"

    def register(self, schedule: Schedule) -> None:
        hour, minute = (int(part) for part in schedule.time.split(":"))
        # launchd counts weekdays from Sunday (0).
        weekdays = [(DAYS.index(d) + 1) % 7 for d in schedule.days]
        intervals = ([{"Hour": hour, "Minute": minute, "Weekday": w} for w in weekdays]
                     or [{"Hour": hour, "Minute": minute}])
        path = self._plist(schedule.id)
        path.parent.mkdir(parents=True, exist_ok=True)
        folder = runs_folder(schedule.id)
        folder.mkdir(parents=True, exist_ok=True)
        path.write_bytes(plistlib.dumps({
            "Label": f"ai.lumi.schedule.{schedule.id}",
            "ProgramArguments": [*command(), "schedule", "run", schedule.id],
            "StartCalendarInterval": intervals,
            "StandardOutPath": str(folder / "launchd.log"),
            "StandardErrorPath": str(folder / "launchd.log"),
        }))
        result = _run(["launchctl", "bootstrap", f"gui/{os.getuid()}", str(path)])
        if result.returncode != 0 and "already" not in (result.stderr or ""):
            raise ScheduleError(f"launchctl refused the schedule: {(result.stderr or result.stdout).strip()}")

    def unregister(self, schedule_id: str) -> None:
        path = self._plist(schedule_id)
        if path.exists():
            _run(["launchctl", "bootout", f"gui/{os.getuid()}", str(path)])
            path.unlink(missing_ok=True)


class Crontab(Registrar):
    """A line per schedule in this user's crontab, found again by its marker."""

    def _lines(self) -> list[str]:
        result = _run(["crontab", "-l"])
        return result.stdout.splitlines() if result.returncode == 0 else []

    def _install(self, lines: list[str]) -> None:
        result = _run(["crontab", "-"], input="\n".join(lines) + "\n")
        if result.returncode != 0:
            raise ScheduleError(f"crontab refused the schedule: {(result.stderr or result.stdout).strip()}")

    def register(self, schedule: Schedule) -> None:
        hour, minute = (int(part) for part in schedule.time.split(":"))
        days = ",".join(str((DAYS.index(d) + 1) % 7) for d in schedule.days) or "*"
        action = " ".join(shlex.quote(part) for part in [*command(), "schedule", "run", schedule.id])
        lines = [line for line in self._lines() if not line.endswith(f"{MARKER}{schedule.id}")]
        self._install(lines + [f"{minute} {hour} * * {days} {action} {MARKER}{schedule.id}"])

    def unregister(self, schedule_id: str) -> None:
        lines = self._lines()
        kept = [line for line in lines if not line.endswith(f"{MARKER}{schedule_id}")]
        if kept != lines:
            self._install(kept)


_registrar: Registrar | None = None


def registrar() -> Registrar:
    if _registrar is not None:
        return _registrar
    if sys.platform == "win32":
        return WindowsTasks()
    if sys.platform == "darwin":
        return LaunchAgents()
    return Crontab()


def set_registrar_for_tests(value: Registrar | None) -> None:
    global _registrar
    _registrar = value


# ── lumi schedule ───────────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(prog="lumi schedule", description="Run a saved task at set times.")
    sub = parser.add_subparsers(dest="action", required=True)
    sub.add_parser("list", help="the schedules and their last runs")
    add = sub.add_parser("add", help="add a schedule")
    add.add_argument("prompt")
    add.add_argument("--name", required=True)
    add.add_argument("--project", default=".")
    add.add_argument("--at", required=True, metavar="HH:MM", help="local time")
    add.add_argument("--days", default="", help="mon,tue,... (default: every day)")
    add.add_argument("--mode", choices=MODES, default="ask")
    add.add_argument("--max-minutes", type=int, default=60, help="stop a run after this long (default: 60)")
    add.add_argument("--provider", default="")
    add.add_argument("--model", default="")
    for name in ("remove", "run", "enable", "disable"):
        sub.add_parser(name).add_argument("id")
    args = parser.parse_args(argv)
    try:
        if args.action == "list":
            for s in load():
                last = "running" if running(s.id) else last_run(s.id).get("status", "never run")
                print(f"{s.id}  {s.name}  {s.describe()}  {'on' if s.enabled else 'off'}  last: {last}")
        elif args.action == "add":
            schedule = save({"name": args.name, "prompt": args.prompt, "project": args.project, "time": args.at,
                             "days": args.days, "mode": args.mode, "provider": args.provider,
                             "model": args.model, "max_minutes": args.max_minutes})
            print(f"Added {schedule.id}: {schedule.name}, {schedule.describe()}.")
        elif args.action == "remove":
            remove(args.id)
        elif args.action in ("enable", "disable"):
            set_enabled(args.id, args.action == "enable")
        else:
            return run(args.id)
    except ScheduleError as exc:
        print(f"lumi schedule: {exc}", file=sys.stderr)
        return 2
    return 0
