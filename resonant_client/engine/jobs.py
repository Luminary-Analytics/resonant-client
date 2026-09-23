"""Bounded project jobs that survive tool calls and browser reconnects.

Jobs own their process trees until completion, cancellation, deadline or client
exit. Application checkpoints belong in project files; restarting the client
does not adopt arbitrary PIDs or silently replay a command.
"""
from __future__ import annotations

import atexit
from collections import deque
import os
from pathlib import Path
import signal
import subprocess
import threading
import time
import uuid

from resonant_client.processes import background_process_kwargs, close_windows_job, windows_kill_job


class JobManager:
    def __init__(self):
        self._items = {}
        self._lock = threading.RLock()

    @staticmethod
    def _root(project):
        return os.path.normcase(str(Path(project).resolve(strict=True)))

    def start(self, project, argv, *, timeout=1200, cancel_event=None):
        root = self._root(project)
        if not isinstance(argv, list) or not argv or any(not isinstance(v, str) or '\0' in v for v in argv):
            raise ValueError('command must be a non-empty array of program and arguments')
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 1 <= timeout <= 1200:
            raise ValueError('Job timeout must be between 1 and 1200 seconds')
        if cancel_event is not None and cancel_event.is_set():
            raise ValueError('Turn was cancelled; no job started')
        with self._lock:
            active = [i for i in self._items.values() if i['state'] == 'running']
            for item in active:
                if item['project'] == root:
                    if item['command'] == argv:
                        return self.status(root, item['id'])
                    raise ValueError('This project already has a running job; inspect or cancel it first')
            if len(active) >= 8:
                raise ValueError('Managed job limit reached (8); finish or cancel a job first')
            process = subprocess.Popen(argv, cwd=root, stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                **background_process_kwargs(new_process_group=True))
            try:
                job = windows_kill_job(process)
            except OSError:
                process.kill()
                process.wait()
                process.stdout.close()
                raise
            handle = uuid.uuid4().hex[:12]
            item = dict(id=handle, project=root, command=list(argv), process=process,
                        job=job, logs=deque(maxlen=64), state='running', exit_code=None,
                        started_at=time.time(), deadline=time.monotonic() + timeout,
                        timeout=timeout, finished_at=None)
            self._items[handle] = item
            # Retain bounded recent history, never discard a live process handle.
            finished = [key for key, old in self._items.items() if old['state'] != 'running']
            for key in finished[:-64]:
                del self._items[key]

        def drain():
            try:
                while chunk := process.stdout.read1(1024):
                    with self._lock:
                        item['logs'].append(chunk.decode('utf-8', errors='replace'))
            finally:
                process.stdout.close()

        def watch():
            while True:
                with self._lock:
                    if item['state'] != 'running':
                        return
                    code = process.poll()
                    if code is not None:
                        self._finish(item, 'completed' if code == 0 else 'failed')
                        return
                    if time.monotonic() >= item['deadline']:
                        self._finish(item, 'timed_out')
                        return
                time.sleep(.1)

        threading.Thread(target=drain, daemon=True).start()
        threading.Thread(target=watch, daemon=True).start()
        return self.status(root, handle)

    def _finish(self, item, state):
        process = item['process']
        if item.get('job'):
            close_windows_job(item.pop('job'))
        elif os.name != 'nt':
            # The group may still contain descendants after its leader exits.
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        elif process.poll() is None:
            process.kill()
        process.wait(timeout=5)
        item.update(state=state, exit_code=process.returncode, finished_at=time.time())

    def status(self, project, handle):
        root = self._root(project)
        with self._lock:
            item = self._items.get(handle)
            if item is None or item['project'] != root:
                raise ValueError('Job does not belong to this project or this client process')
            return {k: item[k] for k in ('id', 'project', 'command', 'state', 'exit_code',
                    'started_at', 'finished_at', 'timeout')} | {
                        'logs': ''.join(item['logs'])[-16384:],
                        'elapsed_seconds': round((item['finished_at'] or time.time()) - item['started_at'], 2)}

    def list(self, project):
        root = self._root(project)
        with self._lock:
            return [self.status(root, k) for k, i in self._items.items() if i['project'] == root]

    def cancel(self, project, handle):
        self.status(project, handle)
        with self._lock:
            item = self._items[handle]
            if item['state'] == 'running':
                self._finish(item, 'cancelled')
        return self.status(project, handle)

    def close(self):
        with self._lock:
            for item in self._items.values():
                if item['state'] == 'running':
                    self._finish(item, 'cancelled')


jobs = JobManager()
atexit.register(jobs.close)
