"""Real private argv gates prove pre-effect ownership and bounded cleanup."""

import os
import sys
import threading
import time

import psutil
import pytest

from lumi.engine.execution_guard import ExecutionGuardError
from lumi.engine.swarming import argv_process
from lumi.engine.swarming.argv_process import ManagedArgvProcess
from lumi.engine.swarming.models import AdmissionClosed


@pytest.mark.skipif(os.name != "nt", reason="Arbitrary effect containment requires Windows named jobs")
def test_gate_records_real_identity_before_target_receives_argv(tmp_path):
    marker = tmp_path / "effect.txt"
    observed = []
    process = ManagedArgvProcess()
    def started(child):
        assert not marker.exists()
        assert psutil.Process(child.pid).create_time() == child.created_at
        observed.append(child.launch_token)
    code = f"from pathlib import Path; Path({str(marker)!r}).write_text('observed'); print('résumé 日本語'); raise SystemExit(7)"
    result = process.execute([sys.executable, "-I", "-S", "-X", "utf8", "-c", code], tmp_path, on_started=started)
    assert observed and marker.read_text() == "observed"
    assert result.exit_code == 7 and "résumé 日本語" in result.stdout.decode("utf-8")
    assert result.output_complete and not result.cancelled and not result.output_truncated
    assert process.cleanup_confirmed and process.exit_code == 0 and not process.alive


@pytest.mark.skipif(os.name != "nt", reason="Arbitrary effect containment requires Windows named jobs")
def test_failed_ownership_callback_never_launches_effect(tmp_path):
    marker = tmp_path / "must-not-run"
    process = ManagedArgvProcess()
    def reject(_):
        raise AdmissionClosed("Fixture owner stopped before invocation")
    with pytest.raises(AdmissionClosed):
        process.execute([sys.executable, "-c", f"from pathlib import Path; Path({str(marker)!r}).touch()"],
                        tmp_path, on_started=reject)
    assert not marker.exists() and process.cleanup_confirmed and not process.alive


@pytest.mark.skipif(os.name != "nt", reason="Arbitrary effect containment requires Windows named jobs")
def test_output_is_capped_while_both_streams_are_drained(tmp_path):
    process = ManagedArgvProcess()
    result = process.execute([sys.executable, "-c",
        "import sys; sys.stdout.buffer.write(b'x'*3000000); sys.stderr.buffer.write(b'y'*3000000)"],
        tmp_path, max_output_bytes=1024)
    assert result.exit_code == 0 and result.output_complete and result.output_truncated
    assert result.stdout == b"x" * 1024 and result.stderr == b"y" * 1024
    assert process.cleanup_confirmed


@pytest.mark.parametrize("cause", ["timeout", "stop"])
@pytest.mark.skipif(os.name != "nt", reason="Arbitrary effect containment requires Windows named jobs")
def test_timeout_and_stop_terminate_descendants_without_claiming_success(tmp_path, cause):
    pid_file = tmp_path / "descendant.pid"
    code = ("import subprocess,sys,time; from pathlib import Path; "
        "child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)']); "
        f"Path({str(pid_file)!r}).write_text(str(child.pid)); time.sleep(60)")
    process = ManagedArgvProcess()
    def check():
        if cause == "stop" and pid_file.exists():
            raise AdmissionClosed("Fixture Stop")
    result = process.execute([sys.executable, "-c", code], tmp_path,
                             timeout_seconds=1 if cause == "timeout" else 10, cancel_check=check)
    assert result.cancelled and result.timed_out == (cause == "timeout")
    assert process.cleanup_confirmed and not process.alive
    assert pid_file.exists()
    pid = int(pid_file.read_text())
    assert not psutil.pid_exists(pid) or psutil.Process(pid).status() == psutil.STATUS_ZOMBIE


@pytest.mark.skipif(os.name != "nt", reason="Arbitrary effect containment requires Windows named jobs")
def test_parent_pipe_eof_stops_effect_on_supported_process_group(tmp_path):
    # Explicit reader EOF is the same private channel signal seen on parent
    # exit; the named Windows job retains owned cleanup.
    pid_file = tmp_path / "target.pid"
    process = ManagedArgvProcess()
    result = []
    failure = []
    def execute():
        try:
            result.append(process.execute([sys.executable, "-c",
                f"import os,time; from pathlib import Path; Path({str(pid_file)!r}).write_text(str(os.getpid())); time.sleep(60)"],
                tmp_path, timeout_seconds=10))
        except BaseException as exc:
            failure.append(exc)
    worker = threading.Thread(target=execute)
    worker.start()
    try:
        deadline = time.monotonic() + 4
        while not pid_file.exists() and time.monotonic() < deadline:
            time.sleep(.02)
        assert pid_file.exists()
        process.process.stdin.close()
        worker.join(timeout=5)
        assert not worker.is_alive() and process.cleanup_confirmed
        assert failure or result[0].exit_code != 0
        pid = int(pid_file.read_text())
        assert not psutil.pid_exists(pid) or psutil.Process(pid).status() == psutil.STATUS_ZOMBIE
    finally:
        if process.alive:
            process.close()
        worker.join(timeout=5)


def test_unsupported_host_denies_before_any_process_launch(tmp_path, monkeypatch):
    monkeypatch.setattr(argv_process, "_NAMED_JOB_SUPPORT", False)
    process = ManagedArgvProcess()
    monkeypatch.setattr(process, "_spawn", lambda: pytest.fail("Unsupported host launched a process"))
    with pytest.raises(ExecutionGuardError, match="Windows named-job"):
        process.execute([sys.executable, "-c", "raise SystemExit(0)"], tmp_path)
    assert process.pid is None
