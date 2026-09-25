"""Scheduled tasks (lumi/schedules.py): nothing here registers a real OS task."""

from __future__ import annotations

import asyncio
import json
import os
import plistlib
import subprocess
from types import SimpleNamespace

import pytest

from lumi import schedules
from lumi.gui import ws_commands


class FakeRegistrar(schedules.Registrar):
    def __init__(self):
        self.calls = []

    def register(self, schedule):
        self.calls.append(("register", schedule.id, schedule.time, tuple(schedule.days)))

    def unregister(self, schedule_id):
        self.calls.append(("unregister", schedule_id))


@pytest.fixture(autouse=True)
def registrar():
    fake = FakeRegistrar()
    schedules.set_registrar_for_tests(fake)
    yield fake
    schedules.set_registrar_for_tests(None)


@pytest.fixture
def project(tmp_path):
    folder = tmp_path / "project"
    folder.mkdir()
    return str(folder)


def _raw(project, **extra):
    return {"name": "Nightly check", "prompt": "Check the dependencies.", "project": project, "time": "02:30",
            **extra}


def test_a_schedule_is_checked_before_it_is_saved(project):
    with pytest.raises(schedules.ScheduleError, match="name"):
        schedules.clean(_raw(project, name="  "))
    with pytest.raises(schedules.ScheduleError, match="task"):
        schedules.clean(_raw(project, prompt=""))
    with pytest.raises(schedules.ScheduleError, match="isn't a folder"):
        schedules.clean(_raw(project + "-missing"))
    for time in ("2:30", "24:00", "02:60", "noon"):
        with pytest.raises(schedules.ScheduleError, match="HH:MM"):
            schedules.clean(_raw(project, time=time))
    with pytest.raises(schedules.ScheduleError, match="Days"):
        schedules.clean(_raw(project, days=["mon", "funday"]))
    with pytest.raises(schedules.ScheduleError, match="auto-edit"):
        schedules.clean(_raw(project, mode="full-auto"))
    for minutes in (0, 721, "soon"):
        with pytest.raises(schedules.ScheduleError, match="minutes"):
            schedules.clean(_raw(project, max_minutes=minutes))

    schedule = schedules.clean(_raw(project, days="Wed, mon wed", mode="auto-edit", max_minutes="90"))
    assert (schedule.days, schedule.mode, schedule.max_minutes) == (["mon", "wed"], "auto-edit", 90)
    assert schedule.describe() == "Mon, Wed at 02:30"
    every_day = schedules.clean(_raw(project, days=list(schedules.DAYS)))
    assert every_day.days == [] and every_day.describe() == "every day at 02:30"


def test_saving_registers_pausing_unregisters_and_removing_deletes_the_results(project, registrar):
    schedule = schedules.save(_raw(project, days=["fri"]))
    assert registrar.calls == [("unregister", schedule.id), ("register", schedule.id, "02:30", ("fri",))]
    assert [s.id for s in schedules.load()] == [schedule.id]

    registrar.calls.clear()
    schedules.set_enabled(schedule.id, False)
    assert registrar.calls == [("unregister", schedule.id)]
    assert schedules.get(schedule.id).enabled is False
    schedules.set_enabled(schedule.id, True)
    assert registrar.calls[-1][0] == "register"

    # A change keeps the id and creation time.
    changed = schedules.save(_raw(project, time="23:15"), schedule.id)
    assert (changed.id, changed.created_at, changed.time) == (schedule.id, schedule.created_at, "23:15")
    with pytest.raises(schedules.ScheduleError, match="no longer exists"):
        schedules.save(_raw(project), "gone")

    folder = schedules.runs_folder(schedule.id)
    folder.mkdir(parents=True)
    (folder / "20260101T000000Z.json").write_text("{}", encoding="utf-8")
    schedules.remove(schedule.id)
    assert registrar.calls[-1] == ("unregister", schedule.id)
    assert schedules.load() == [] and not folder.exists()
    with pytest.raises(schedules.ScheduleError):
        schedules.remove(schedule.id)


def test_a_refused_registration_saves_nothing(project):
    class Refusing(FakeRegistrar):
        def register(self, schedule):
            raise schedules.ScheduleError("Task Scheduler refused the schedule: access denied")

    schedules.set_registrar_for_tests(Refusing())
    with pytest.raises(schedules.ScheduleError, match="access denied"):
        schedules.save(_raw(project))
    assert schedules.load() == []


def test_the_number_of_schedules_is_limited(project, monkeypatch):
    monkeypatch.setattr(schedules, "MAX_SCHEDULES", 2)
    schedules.save(_raw(project))
    schedules.save(_raw(project))
    with pytest.raises(schedules.ScheduleError, match="up to 2"):
        schedules.save(_raw(project))


def test_a_run_is_lumi_run_with_the_schedules_settings(project, monkeypatch):
    schedule = schedules.save(_raw(project, mode="auto-edit", provider="anthropic", model="claude-x",
                                   max_minutes=5))
    seen = {}

    def fake_main(argv, *, stdin, stdout, stderr):
        seen["argv"] = argv
        seen["running"] = schedules.running(schedule.id)
        stdout.write(json.dumps({"status": "completed", "outcome": "answered", "text": "All current.",
                                 "changed_files": ["requirements.txt"], "errors": [],
                                 "usage": {"cost_usd": 0.0123}}))
        return 0

    from lumi import headless

    monkeypatch.setattr(headless, "main", fake_main)
    assert schedules.run(schedule.id) == 0
    assert seen["argv"] == ["--project", project, "--mode", "auto-edit", "--output", "json", "--timeout", "300",
                            "--provider", "anthropic", "--model", "claude-x", "Check the dependencies."]
    assert seen["running"] is True
    assert schedules.running(schedule.id) is False  # the claim goes with the run

    last = schedules.last_run(schedule.id)
    assert last["status"] == "completed" and last["answer"] == "All current." and last["cost_usd"] == 0.0123
    assert last["changed_files"] == ["requirements.txt"] and last["error"] == ""
    assert last["exit_code"] == 0 and "summary" not in last
    assert schedules.runs(schedule.id)[0]["summary"]["outcome"] == "answered"
    # A run records results only; the schedule itself is untouched.
    assert schedules.load()[0] == schedule
    overview = schedules.overview()
    assert overview["items"][0]["last_run"]["status"] == "completed"
    assert overview["items"][0]["when"] == "every day at 02:30" and overview["items"][0]["running"] is False


def test_a_failed_run_is_recorded_and_old_results_are_pruned(project, monkeypatch):
    schedule = schedules.save(_raw(project))
    from lumi import headless

    def broken(argv, *, stdin, stdout, stderr):
        raise RuntimeError("provider unreachable")

    monkeypatch.setattr(headless, "main", broken)
    assert schedules.run(schedule.id) == 1
    last = schedules.last_run(schedule.id)
    assert last["status"] == "failed" and "provider unreachable" in last["error"]

    # A run that got as far as a summary reports its own errors.
    def refused(argv, *, stdin, stdout, stderr):
        stdout.write(json.dumps({"status": "budget_exceeded", "outcome": "failed", "text": "",
                                 "errors": [{"message": "The daily budget is used up.", "code": "budget_exceeded"}]}))
        stderr.write("lumi run: stopped")
        return 4

    monkeypatch.setattr(headless, "main", refused)
    assert schedules.run(schedule.id) == 4
    last = schedules.last_run(schedule.id)
    assert (last["status"], last["error"]) == ("budget_exceeded", "The daily budget is used up.")

    folder = schedules.runs_folder(schedule.id)
    for day in range(1, 40):
        (folder / f"2025{day:04d}T000000Z.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(headless, "main", lambda argv, **_: 0)
    schedules.run(schedule.id)
    assert len(list(folder.glob("2*.json"))) == schedules.KEEP_RUNS
    assert schedules.last_run(schedule.id)["status"] == "done"


def test_a_real_unattended_run_without_a_model_records_why(project):
    # headless.main itself, with nothing configured in the isolated home.
    schedule = schedules.save(_raw(project))
    assert schedules.run(schedule.id) == 2
    last = schedules.last_run(schedule.id)
    assert (last["exit_code"], last["status"]) == (2, "failed")
    assert "Choose a provider" in last["error"]


def test_a_schedule_never_runs_twice_at_once(project, monkeypatch):
    import psutil

    schedule = schedules.save(_raw(project))
    folder = schedules.runs_folder(schedule.id)
    folder.mkdir(parents=True)
    me = psutil.Process()
    (folder / "running.json").write_text(json.dumps({"pid": me.pid, "created": me.create_time()}),
                                         encoding="utf-8")
    assert schedules.running(schedule.id) is True
    with pytest.raises(schedules.ScheduleError, match="already running"):
        schedules.run(schedule.id)
    with pytest.raises(schedules.ScheduleError, match="already running"):
        schedules.start(schedule.id)

    # A claim left by a process that has gone is taken over.
    (folder / "running.json").write_text(json.dumps({"pid": me.pid, "created": me.create_time() - 1000}),
                                         encoding="utf-8")
    assert schedules.running(schedule.id) is False
    from lumi import headless

    monkeypatch.setattr(headless, "main", lambda argv, **_: 0)
    assert schedules.run(schedule.id) == 0
    assert not (folder / "running.json").exists()

    # A claim being written this moment counts as running; an old broken one doesn't.
    (folder / "running.json").write_text("", encoding="utf-8")
    assert schedules.running(schedule.id) is True
    old = os.path.getmtime(folder / "running.json") - 60
    os.utime(folder / "running.json", (old, old))
    assert schedules.running(schedule.id) is False


def test_run_now_claims_the_run_for_the_process_it_starts(project, monkeypatch):
    schedule = schedules.save(_raw(project))
    started = {}

    class Process:
        pid = os.getpid()  # a live process, so the claim is checkable

        def wait(self):
            return 0

    def fake_popen(args, **kwargs):
        started.update(args=args, **kwargs)
        return Process()

    monkeypatch.setattr(schedules.subprocess, "Popen", fake_popen)
    schedules.start(schedule.id)
    assert started["args"][-3:] == ["schedule", "run", schedule.id]
    assert started["stdin"] is subprocess.DEVNULL
    token = started["env"][schedules.CLAIM_ENV]
    claim = json.loads((schedules.runs_folder(schedule.id) / "running.json").read_text(encoding="utf-8"))
    assert claim["token"] == token and claim["pid"] == os.getpid()
    # A second click finds it running.
    with pytest.raises(schedules.ScheduleError, match="already running"):
        schedules.start(schedule.id)

    # The started process takes the claim over with the token, and the token
    # doesn't stay in the environment its own commands inherit.
    from lumi import headless

    monkeypatch.setattr(headless, "main", lambda argv, **_: 0)
    monkeypatch.setenv(schedules.CLAIM_ENV, token)
    assert schedules.run(schedule.id) == 0
    assert schedules.CLAIM_ENV not in os.environ
    with pytest.raises(schedules.ScheduleError, match="no longer exists"):
        schedules.start("gone")


def test_scheduled_tasks_can_be_turned_off(project):
    from lumi.paths import state_home

    schedule = schedules.save(_raw(project))
    settings_file = state_home() / "settings.json"
    settings_file.parent.mkdir(parents=True, exist_ok=True)
    settings_file.write_text(json.dumps({"security": {"scheduled_tasks": False}}), encoding="utf-8")

    with pytest.raises(schedules.ScheduleError, match="turned off"):
        schedules.save(_raw(project))
    with pytest.raises(schedules.ScheduleError, match="turned off"):
        schedules.start(schedule.id)
    # Pausing and removing still work.
    assert schedules.set_enabled(schedule.id, False).enabled is False
    with pytest.raises(schedules.ScheduleError, match="turned off"):
        schedules.set_enabled(schedule.id, True)
    # A run the operating system still starts is refused, and the refusal is kept.
    with pytest.raises(schedules.ScheduleError, match="turned off"):
        schedules.run(schedule.id)
    last = schedules.last_run(schedule.id)
    assert (last["status"], last["exit_code"]) == ("refused", 2) and "turned off" in last["error"]
    assert "turned off" in schedules.overview()["turned_off"]
    assert schedules.main(["run", schedule.id]) == 2


def test_the_policy_decides_which_modes_a_schedule_may_use(project, monkeypatch):
    from lumi import policy

    acme = SimpleNamespace(organization="Acme", mode_allowed=lambda mode: mode in ("ask", "auto-edit"))
    monkeypatch.setattr(policy, "current", lambda: acme)
    settings = SimpleNamespace(get=lambda section, key, default=None: default)
    with pytest.raises(schedules.ScheduleError, match="Acme's policy doesn't allow the bypass mode"):
        schedules.save(_raw(project, mode="bypass"), settings=settings)
    assert schedules.save(_raw(project, mode="auto-edit"), settings=settings).mode == "auto-edit"
    assert schedules.overview(settings)["turned_off"] == ""


def test_task_scheduler_entries(monkeypatch):
    calls = []

    def fake_run(args, **kwargs):
        calls.append(args)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(schedules, "_run", fake_run)
    monkeypatch.setattr(schedules, "command", lambda: [r"C:\Program Files\Lumi\Lumi.exe"])
    tasks = schedules.WindowsTasks()
    tasks.register(schedules.Schedule(id="abc", name="n", prompt="p", project=".", time="02:30"))
    tasks.register(schedules.Schedule(id="def", name="n", prompt="p", project=".", time="07:05",
                                      days=["mon", "fri"]))
    tasks.unregister("abc")
    daily, weekly, removed = calls
    assert daily[:4] == ["schtasks", "/Create", "/TN", "Lumi\\abc"]
    assert daily[daily.index("/TR") + 1] == '"C:\\Program Files\\Lumi\\Lumi.exe" schedule run abc'
    assert daily[daily.index("/ST") + 1] == "02:30" and daily[daily.index("/SC") + 1] == "DAILY"
    assert weekly[weekly.index("/SC") + 1] == "WEEKLY" and weekly[weekly.index("/D") + 1] == "MON,FRI"
    assert removed == ["schtasks", "/Delete", "/TN", "Lumi\\abc", "/F"]

    monkeypatch.setattr(schedules, "_run", lambda args, **kwargs: SimpleNamespace(
        returncode=1, stdout="", stderr="ERROR: Access is denied."))
    with pytest.raises(schedules.ScheduleError, match="Access is denied"):
        tasks.register(schedules.Schedule(id="x", name="n", prompt="p", project=".", time="01:00"))


def test_a_run_from_source_uses_pythonw_on_windows(tmp_path):
    python = tmp_path / "python.exe"
    python.write_text("", encoding="utf-8")
    assert schedules._windowless([str(python), "-m", "lumi"]) == [str(python), "-m", "lumi"]
    (tmp_path / "pythonw.exe").write_text("", encoding="utf-8")
    assert schedules._windowless([str(python), "-m", "lumi"]) == [str(tmp_path / "pythonw.exe"), "-m", "lumi"]
    assert schedules._windowless([r"C:\Lumi\Lumi.exe"]) == [r"C:\Lumi\Lumi.exe"]


def test_crontab_lines(monkeypatch):
    table = ["MAILTO=me", "0 1 * * * /usr/bin/backup # lumi-schedule:old"]
    calls = []

    def fake_run(args, **kwargs):
        calls.append(args)
        if args == ["crontab", "-l"]:
            return SimpleNamespace(returncode=0, stdout="\n".join(table) + "\n", stderr="")
        table[:] = kwargs["input"].splitlines()
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(schedules, "_run", fake_run)
    monkeypatch.setattr(schedules, "command", lambda: ["/opt/lumi app/lumi"])
    cron = schedules.Crontab()
    cron.register(schedules.Schedule(id="new", name="n", prompt="p", project=".", time="07:05",
                                     days=["mon", "sun"]))
    assert table == ["MAILTO=me", "0 1 * * * /usr/bin/backup # lumi-schedule:old",
                     "5 7 * * 1,0 '/opt/lumi app/lumi' schedule run new # lumi-schedule:new"]
    cron.register(schedules.Schedule(id="new", name="n", prompt="p", project=".", time="08:00"))
    assert table[-1] == "0 8 * * * '/opt/lumi app/lumi' schedule run new # lumi-schedule:new"
    assert len(table) == 3
    cron.unregister("old")
    assert table == ["MAILTO=me", "0 8 * * * '/opt/lumi app/lumi' schedule run new # lumi-schedule:new"]
    calls.clear()
    cron.unregister("missing")
    assert calls == [["crontab", "-l"]]  # nothing to change, nothing installed


def test_launch_agent_file(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(schedules, "_run", lambda args, **kwargs: calls.append(args) or SimpleNamespace(
        returncode=0, stdout="", stderr=""))
    monkeypatch.setattr(schedules.os, "getuid", lambda: 501, raising=False)
    monkeypatch.setattr(schedules, "command", lambda: ["/Applications/Lumi.app/Contents/MacOS/Lumi"])
    agents = schedules.LaunchAgents()
    agents.register(schedules.Schedule(id="mac", name="n", prompt="p", project=".", time="06:45",
                                       days=["mon", "sun"]))
    path = agents._plist("mac")
    data = plistlib.loads(path.read_bytes())
    assert data["Label"] == "ai.lumi.schedule.mac"
    assert data["ProgramArguments"] == ["/Applications/Lumi.app/Contents/MacOS/Lumi", "schedule", "run", "mac"]
    # launchd counts weekdays from Sunday (0).
    assert data["StartCalendarInterval"] == [{"Hour": 6, "Minute": 45, "Weekday": 1},
                                             {"Hour": 6, "Minute": 45, "Weekday": 0}]
    assert calls == [["launchctl", "bootstrap", "gui/501", str(path)]]
    agents.unregister("mac")
    assert calls[-1] == ["launchctl", "bootout", "gui/501", str(path)] and not path.exists()


def test_the_command_line(project, capsys, monkeypatch):
    assert schedules.main(["add", "Summarize yesterday's commits.", "--name", "Digest", "--project", project,
                           "--at", "08:00", "--days", "mon,tue", "--max-minutes", "15"]) == 0
    schedule = schedules.load()[0]
    assert (schedule.name, schedule.days, schedule.max_minutes) == ("Digest", ["mon", "tue"], 15)
    assert schedules.main(["disable", schedule.id]) == 0
    assert schedules.get(schedule.id).enabled is False
    capsys.readouterr()
    assert schedules.main(["list"]) == 0
    assert f"{schedule.id}  Digest  Mon, Tue at 08:00  off  last: never run" in capsys.readouterr().out
    assert schedules.main(["add", "x", "--name", "Bad", "--project", project, "--at", "8am"]) == 2
    assert "HH:MM" in capsys.readouterr().err
    from lumi import headless

    monkeypatch.setattr(headless, "main", lambda argv, **_: 3)
    assert schedules.main(["run", schedule.id]) == 3
    assert schedules.main(["remove", schedule.id]) == 0 and schedules.load() == []


class _WS:
    def __init__(self):
        self.sent = []

    async def send_json(self, payload):
        self.sent.append(payload)


def _command(name, **msg):
    ctx = ws_commands.CommandContext(ws=_WS(), state=SimpleNamespace(), msg={"command": name, **msg},
                                     runs=SimpleNamespace(busy=False))

    async def go():
        await ws_commands.HANDLERS[name](ctx)
        # Let "Run now" see its process finish.
        await asyncio.gather(*ws_commands._SCHEDULE_RUNS)

    asyncio.run(go())
    return ctx.ws.sent


def test_the_settings_page_commands(project, monkeypatch):
    assert _command("schedules_list")[0] == {"event": "schedules", "data": schedules.overview()}

    sent = _command("schedule_save", schedule=_raw(project, days=["sat"]))
    saved = sent[0]["data"]["saved"]
    assert sent[0]["event"] == "schedules" and sent[0]["data"]["items"][0]["id"] == saved
    assert _command("schedule_save", schedule=_raw(project, time="25:00")) == [
        {"event": "schedule_error", "message": "Enter the time as HH:MM, for example 02:30."}]
    assert _command("schedule_save", schedule="nope")[0]["event"] == "error"

    assert _command("schedule_change", id=saved, action="pause")[0]["data"]["items"][0]["enabled"] is False
    assert _command("schedule_change", id=saved, action="resume")[0]["data"]["items"][0]["enabled"] is True
    assert _command("schedule_change", id=saved, action="explode")[0]["event"] == "schedule_error"

    waited = []

    class Process:
        def wait(self):
            waited.append(True)
            return 0

    monkeypatch.setattr(schedules, "start", lambda schedule_id, **kwargs: Process())
    sent = _command("schedule_change", id=saved, action="run")
    # The list at once, and again when the run's process ends.
    assert [payload["event"] for payload in sent] == ["schedules", "schedules"] and waited == [True]

    assert _command("schedule_change", id=saved, action="remove")[0]["data"]["items"] == []
    assert _command("schedule_change", id=saved, action="remove")[0] == {
        "event": "schedule_error", "message": "That schedule no longer exists."}
