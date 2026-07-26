"""Tests for the process tools — engine/processes.py.

`kill_process` was previously untested; its safety guardrails (refuse
low/system PIDs, self, critical names) are exactly the kind of thing
that must be pinned. psutil is mocked so no real processes are touched.
"""
from __future__ import annotations

import os
import time

from unittest.mock import MagicMock, patch

import pytest

from resonant_client.engine import processes as proc

# These tests patch `proc.psutil`, which only exists when psutil imported
# successfully. psutil ships with the `desktop` extra (it backs process_list /
# process_kill), but a core-only `pip install -e .` has no psutil and every
# test here failed with a bare AttributeError instead of saying why.
# processes.py itself degrades gracefully in that case; the suite should too.
pytestmark = pytest.mark.skipif(
    not hasattr(proc, "psutil"),
    reason="psutil not installed — install the `desktop` extra to exercise the process tools",
)
from resonant_client.engine.processes import (
    NEVER_KILL_NAMES,
    SYSTEM_PID_FLOOR,
    exec_process_kill,
    kill_process,
)


def _fake_proc(pid, name):
    p = MagicMock()
    p.pid = pid
    p.name.return_value = name
    p.wait.return_value = None
    return p


class TestKillGuardrails:
    def test_refuses_pid_below_floor(self):
        fake = _fake_proc(SYSTEM_PID_FLOOR - 1, "something.exe")
        with patch.object(proc.psutil, "Process", return_value=fake):
            out = kill_process(SYSTEM_PID_FLOOR - 1)
        assert out["killed"] == []
        assert out["skipped"][0]["reason"].startswith("pid below floor")
        fake.terminate.assert_not_called()

    def test_refuses_self(self):
        me = os.getpid()
        fake = _fake_proc(me, "python.exe")
        with patch.object(proc.psutil, "Process", return_value=fake):
            out = kill_process(me)
        assert out["killed"] == []
        assert out["skipped"][0]["reason"] == "self"
        fake.terminate.assert_not_called()

    def test_refuses_critical_name(self):
        crit = sorted(NEVER_KILL_NAMES)[0]
        fake = _fake_proc(4321, crit)
        with patch.object(proc.psutil, "Process", return_value=fake):
            out = kill_process(4321)
        assert out["killed"] == []
        assert out["skipped"][0]["reason"] == "system-critical name"
        fake.terminate.assert_not_called()

    def test_kills_a_safe_target(self):
        fake = _fake_proc(4321, "notepad.exe")
        with patch.object(proc.psutil, "Process", return_value=fake):
            out = kill_process(4321)
        assert out["killed"] == [{"pid": 4321, "name": "notepad.exe"}]
        assert out["skipped"] == []
        fake.terminate.assert_called_once()

    def test_force_kills_on_terminate_timeout(self):
        fake = _fake_proc(4321, "notepad.exe")
        fake.wait.side_effect = proc.psutil.TimeoutExpired(3.0)
        with patch.object(proc.psutil, "Process", return_value=fake):
            out = kill_process(4321)
        assert out["killed"] == [{"pid": 4321, "name": "notepad.exe"}]
        fake.kill.assert_called_once()

    def test_unknown_pid_errors(self):
        with patch.object(proc.psutil, "Process",
                          side_effect=proc.psutil.NoSuchProcess(9999)):
            out = kill_process(9999)
        assert "no process with pid 9999" in out["error"]


class TestKillByName:
    def test_kills_matching_name(self):
        fake = _fake_proc(4321, "myapp.exe")
        fake.info = {"pid": 4321, "name": "myapp.exe"}
        with patch.object(proc.psutil, "process_iter", return_value=[fake]):
            out = kill_process("MyApp.exe")  # case-insensitive
        assert out["killed"] == [{"pid": 4321, "name": "myapp.exe"}]

    def test_name_not_found_errors(self):
        with patch.object(proc.psutil, "process_iter", return_value=[]):
            out = kill_process("nonexistent-xyz")
        assert "no process named" in out["error"]


class TestExecProcessKill:
    def test_requires_exactly_one_of_pid_or_name(self):
        both = exec_process_kill({"pid": 4321, "name": "x"}, time.time())
        assert both.is_error and "Exactly one" in both.output
        neither = exec_process_kill({}, time.time())
        assert neither.is_error and "Exactly one" in neither.output

    def test_pid_path_reports_killed(self):
        fake = _fake_proc(4321, "notepad.exe")
        with patch.object(proc.psutil, "Process", return_value=fake):
            r = exec_process_kill({"pid": 4321}, time.time())
        assert not r.is_error
        assert "killed pid=4321" in r.output


class TestPsutilMissing:
    def test_kill_without_psutil_reports_cleanly(self, monkeypatch):
        monkeypatch.setattr(proc, "_HAS_PSUTIL", False)
        out = kill_process(4321)
        assert out["error"] == "psutil not installed"
        assert out["killed"] == []
