"""Project-owned development servers with explicit lifetime and bounded output."""
from __future__ import annotations

import atexit
from collections import deque
import os
from pathlib import Path
import signal
import socket
import subprocess
import threading
import time
import uuid
from urllib.parse import urlsplit
import urllib.request

from resonant_client.processes import background_process_kwargs, windows_kill_job, close_windows_job


class PreviewManager:
    def __init__(self):
        self._items = {}
        self._lock = threading.RLock()

    def start(self, project, argv, url, *, timeout=15, cancel_event=None):
        root = str(Path(project).resolve(strict=True))
        parsed = urlsplit(url)
        if parsed.scheme != 'http' or parsed.hostname not in ('127.0.0.1', 'localhost') or not parsed.port:
            raise ValueError('Use an http://127.0.0.1:PORT readiness URL.')
        if not isinstance(argv, list) or not argv or any(not isinstance(v, str) for v in argv):
            raise ValueError('command must be a non-empty array of program and arguments')
        with self._lock:
            for item in self._items.values():
                if item['project'] == root and item['command'] == argv and item['url'] == url and item['process'].poll() is None:
                    return self.status(root, item['id'])
            if sum(i['process'].poll() is None for i in self._items.values()) >= 8:
                raise ValueError('Stop an existing preview before starting another (limit 8).')
            with socket.socket() as probe:
                if probe.connect_ex(('127.0.0.1', parsed.port)) == 0:
                    raise ValueError('Preview port is already in use; choose another port.')
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
            item = dict(id=handle, project=root, command=list(argv), url=url,
                        process=process, job=job, logs=deque(maxlen=64), ready=False, started_at=time.time())
            self._items[handle] = item
        def drain():
            try:
                while chunk := process.stdout.read1(1024):
                    with self._lock:
                        item['logs'].append(chunk.decode('utf-8', errors='replace'))
            finally:
                process.stdout.close()
        threading.Thread(target=drain, daemon=True).start()
        deadline = time.monotonic() + min(60, max(1, float(timeout)))
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        while process.poll() is None and time.monotonic() < deadline:
            if cancel_event is not None and cancel_event.is_set():
                return self.stop(root, handle)
            try:
                with opener.open(url, timeout=.5) as response:
                    if 200 <= response.status < 400:
                        item['ready'] = True
                        item['ready_at'] = time.time()
                        break
            except Exception:
                pass
            if cancel_event is not None:
                cancel_event.wait(.1)
            else:
                time.sleep(.1)
        if not item['ready']:
            self.stop(root, handle)
        return self.status(root, handle)

    def status(self, project, handle):
        with self._lock:
            item = self._items.get(handle)
            if item is None or os.path.normcase(item['project']) != os.path.normcase(str(Path(project).resolve())):
                raise ValueError('Preview does not belong to this project')
            code = item['process'].poll()
            return {k: item[k] for k in ('id', 'project', 'command', 'url', 'started_at')} | {
                'state': 'stopped' if code is not None else ('ready' if item['ready'] else 'starting'),
                'exit_code': code, 'ready_at': item.get('ready_at'), 'logs': ''.join(item['logs'])[-16384:]}

    def list(self, project):
        with self._lock:
            return [self.status(project, key) for key, item in self._items.items()
                    if os.path.normcase(item['project']) == os.path.normcase(str(Path(project).resolve()))]

    def stop(self, project, handle):
        self.status(project, handle)  # Check ownership before accessing the process.
        with self._lock:
            item = self._items[handle]
            process = item['process']
            if item.get('job'):
                close_windows_job(item.pop('job'))
                process.wait(timeout=3)
            if process.poll() is None:
                if os.name == 'nt':
                    subprocess.run(['taskkill', '/PID', str(process.pid), '/T', '/F'],
                                   capture_output=True, timeout=10, **background_process_kwargs())
                else:
                    os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    if os.name != 'nt':
                        os.killpg(process.pid, signal.SIGKILL)
                    else:
                        process.kill()
                    process.wait(timeout=3)
        return self.status(project, handle)

    def close(self):
        for item in list(self._items.values()):
            try:
                self.stop(item['project'], item['id'])
            except (OSError, subprocess.SubprocessError):
                pass


previews = PreviewManager()
atexit.register(previews.close)
