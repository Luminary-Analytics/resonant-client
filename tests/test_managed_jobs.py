"""Real-process lifecycle checks for long workers, independent of Blender output."""
import json
import sys
import threading
import time

import pytest

from resonant_client.engine.jobs import JobManager


@pytest.fixture
def manager():
    item = JobManager()
    yield item
    item.close()


def until(check, timeout=8):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = check()
        if value:
            return value
        time.sleep(.05)
    pytest.fail('Worker condition timed out')


def test_progress_survives_tool_return_and_resume_preserves_checkpoint(manager, tmp_path):
    worker = tmp_path / 'worker.py'
    worker.write_text("""import pathlib,time
p=pathlib.Path('progress.json')
n=int(p.read_text()) if p.exists() else 0
while n<12:
 n+=1
 p.write_text(str(n))
 print('frame',n,flush=True)
 time.sleep(.1)
""", encoding='utf-8')
    argv = [sys.executable, str(worker)]
    t0 = time.monotonic()
    first = manager.start(tmp_path, argv)
    assert time.monotonic() - t0 < 2
    progress = tmp_path / 'progress.json'
    until(lambda: progress.exists() and progress.read_text().strip().isdigit() and int(progress.read_text()) >= 2)
    assert manager.start(tmp_path, argv)['id'] == first['id']
    with pytest.raises(ValueError, match='already has'):
        manager.start(tmp_path, [sys.executable, '-c', 'print(1)'])
    manager.cancel(tmp_path, first['id'])
    saved = progress.read_text()
    time.sleep(.25)
    assert progress.read_text() == saved
    second = manager.start(tmp_path, argv)
    until(lambda: manager.status(tmp_path, second['id'])['state'] == 'completed')
    assert progress.read_text() == '12'
    assert int(saved) >= 2
    assert first['id'] != second['id']


def test_deadline_ownership_and_bounded_logs(manager, tmp_path):
    other = tmp_path / 'other'
    other.mkdir()
    job = manager.start(tmp_path, [sys.executable, '-u', '-c', "import time;print('x'*100000);time.sleep(30)"], timeout=1)
    with pytest.raises(ValueError, match='belong'):
        manager.cancel(other, job['id'])
    until(lambda: manager.status(tmp_path, job['id'])['state'] == 'timed_out')
    assert len(manager.status(tmp_path, job['id'])['logs']) <= 16384
    assert manager.list(other) == []


def test_failure_and_client_exit_stop_owned_tree(manager, tmp_path):
    failed = manager.start(tmp_path, [sys.executable, '-c', 'raise SystemExit(7)'])
    until(lambda: manager.status(tmp_path, failed['id'])['state'] == 'failed')
    assert manager.status(tmp_path, failed['id'])['exit_code'] == 7
    script = tmp_path / 'spawn.py'
    script.write_text("import subprocess,sys,time\np=subprocess.Popen([sys.executable,'-c',\"import pathlib,time; time.sleep(2); pathlib.Path('escaped').write_text('bad')\"])\nprint(p.pid,flush=True)\ntime.sleep(30)\n")
    active = manager.start(tmp_path, [sys.executable, str(script)])
    until(lambda: manager.status(tmp_path, active['id'])['logs'].strip())
    manager.close()
    time.sleep(2.2)
    assert not (tmp_path / 'escaped').exists()
    assert manager.status(tmp_path, active['id'])['state'] == 'cancelled'


def test_cancelled_submission_and_tool_permissions(manager, tmp_path):
    from resonant_client.engine.sandbox import EXEC_TOOLS, READ_ONLY_TOOLS
    from resonant_client.engine.tools import AGENT_TOOLS
    event = threading.Event()
    event.set()
    with pytest.raises(ValueError, match='cancelled'):
        manager.start(tmp_path, [sys.executable, '-c', 'print(1)'], cancel_event=event)
    assert {'job_start', 'job_cancel'} <= EXEC_TOOLS
    assert 'job_status' in READ_ONLY_TOOLS
    schemas = {t['function']['name']: t['function'] for t in AGENT_TOOLS}
    assert schemas['job_start']['parameters']['properties']['timeout']['maximum'] == 1200
    for invalid in (0, 1201, float('nan'), True):
        with pytest.raises(ValueError):
            manager.start(tmp_path, [sys.executable, '-c', 'print(1)'], timeout=invalid)


def test_actual_tool_dispatch(manager, tmp_path, monkeypatch):
    from resonant_client.engine import jobs
    from resonant_client.engine.tools import execute_tool
    monkeypatch.setattr(jobs, 'jobs', manager)
    result = execute_tool('job_start', {'command': [sys.executable, '-c', 'print("ok")']}, project_path=str(tmp_path))
    assert not result.is_error, result.output
    handle = json.loads(result.output)['id']
    until(lambda: manager.status(tmp_path, handle)['state'] == 'completed')
    status = execute_tool('job_status', {'id': handle}, project_path=str(tmp_path))
    assert json.loads(status.output)['exit_code'] == 0
