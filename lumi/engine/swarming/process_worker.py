"""Owned native writer process and bounded, non-replaying host RPC transport."""

from __future__ import annotations

from collections.abc import Callable, Iterator, Sequence
import json
import math
import os
from pathlib import Path
import queue
import signal
import site
import subprocess
import sys
import sysconfig
import threading
import time
from typing import Any
import uuid

import psutil

from ...processes import background_process_kwargs, close_windows_job, windows_kill_job
from ...events import EngineEvent
from ..execution_guard import ExecutionGuardError
from .processes import job_name

PROTOCOL_VERSION = 1
MAX_FRAME_BYTES = 4 * 1024 * 1024
_EVENT_NAMES = frozenset(event.value for event in EngineEvent)


def _source_command(entrypoint: str = "--swarm-worker") -> list[str]:
    # An inherited PYTHONPATH, user site, or startup .pth/sitecustomize hook
    # must not run before this child receives private provider configuration.
    # -S avoids startup hooks; explicit dependency directories also support
    # source development in a venv without relying on editable-install hooks.
    if entrypoint not in {"--swarm-worker", "--swarm-effect"}:
        raise ValueError("Unknown private process entrypoint")
    source = str(Path(__file__).resolve().parents[3])
    dependencies = sorted({sysconfig.get_path("purelib"), sysconfig.get_path("platlib"),
                           site.getusersitepackages()})
    bootstrap = ("import sys,runpy;"
                 f"sys.path[:0]={[source, *dependencies]!r};"
                 "runpy.run_module('lumi',run_name='__main__')")
    return [sys.executable, "-I", "-S", "-c", bootstrap, entrypoint]


def stdio_pipes():
    """Duplicate inherited private pipes, including frozen windowless Windows.

    The protocol then lives only on the private (non-inheritable) duplicates,
    and standard input and output point at the null device, so nothing this
    process starts later inherits the host channel.
    """
    if os.name != "nt":
        streams = os.fdopen(os.dup(0), "rb", buffering=0), os.fdopen(os.dup(1), "wb", buffering=0)
        _detach_standard_streams()
        return streams
    import ctypes
    import msvcrt
    from ctypes import wintypes
    api = ctypes.WinDLL("kernel32", use_last_error=True)
    api.GetStdHandle.argtypes = [wintypes.DWORD]
    api.GetStdHandle.restype = wintypes.HANDLE
    api.GetCurrentProcess.restype = wintypes.HANDLE
    api.DuplicateHandle.argtypes = [wintypes.HANDLE, wintypes.HANDLE, wintypes.HANDLE,
                                   ctypes.POINTER(wintypes.HANDLE), wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    process = api.GetCurrentProcess()
    streams = []
    for number, flag, mode in ((-10, os.O_RDONLY, "rb"), (-11, os.O_WRONLY, "wb")):
        handle = wintypes.HANDLE()
        if not api.DuplicateHandle(process, api.GetStdHandle(number & 0xffffffff), process,
                                   ctypes.byref(handle), 0, False, 2):
            raise ctypes.WinError(ctypes.get_last_error())
        descriptor = msvcrt.open_osfhandle(handle.value, flag | os.O_BINARY)
        streams.append(os.fdopen(descriptor, mode, buffering=0))
    _detach_standard_streams()
    return tuple(streams)


def _detach_standard_streams() -> None:
    """Point standard input and output at the null device.

    Code a worker runs (git queries, language servers, a tool's command) may
    start children without choosing their streams. Inheriting the host channel
    would let a child read or write protocol bytes, and on Windows a child that
    only queries the pipe blocks behind this process's pending read.
    """
    for number, flags in ((0, os.O_RDONLY), (1, os.O_WRONLY)):
        null = os.open(os.devnull, flags)
        try:
            os.dup2(null, number)
        finally:
            os.close(null)
    if os.name == "nt":
        import ctypes
        import msvcrt
        from ctypes import wintypes
        api = ctypes.WinDLL("kernel32", use_last_error=True)
        api.SetStdHandle.argtypes = [wintypes.DWORD, wintypes.HANDLE]
        # subprocess passes GetStdHandle's values to children that inherit.
        for std, number in ((-10, 0), (-11, 1)):
            if not api.SetStdHandle(std & 0xffffffff, msvcrt.get_osfhandle(number)):
                raise ctypes.WinError(ctypes.get_last_error())


def encode_frame(value: dict[str, Any]) -> bytes:
    """Bound all private control traffic; never log raw protocol frames."""
    encoded = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode() + b"\n"
    if len(encoded) > MAX_FRAME_BYTES:
        raise ValueError("Worker protocol frame exceeds its size limit")
    return encoded


def read_frame(stream) -> dict[str, Any] | None:
    raw = stream.readline(MAX_FRAME_BYTES + 1)
    if not raw:
        return None
    if len(raw) > MAX_FRAME_BYTES or not raw.endswith(b"\n"):
        raise ValueError("Worker protocol frame is incomplete or oversized")
    def finite_number(text):
        value = float(text)
        if not math.isfinite(value):
            raise ValueError("Worker protocol numbers must be finite")
        return value
    value = json.loads(raw.decode("utf-8"), parse_constant=finite_number, parse_float=finite_number)
    if type(value) is not dict or type(value.get("version")) is not int or value["version"] != PROTOCOL_VERSION:
        raise ValueError("Worker protocol version or envelope is invalid")
    return value


class ManagedWorkerProcess:
    """One captured process tree; JSON RPC grants no new supervisor authority.

    A trusted test/host may inject an exact executable argv. Production uses
    the bundled entry point; neither model nor UI supplies an executable.
    """

    def __init__(self, *, command: Sequence[str] | None = None, cancel_grace: float = 1.0) -> None:
        if type(cancel_grace) not in (int, float) or not math.isfinite(cancel_grace) or not 0 <= cancel_grace <= 5:
            raise ValueError("Worker cancellation grace must be between zero and five seconds")
        self.command = list(command) if command is not None else (
            [sys.executable, "--swarm-worker"] if getattr(sys, "frozen", False)
            else _source_command())
        self.cancel_grace = cancel_grace
        self.process: subprocess.Popen | None = None
        self._job = None
        self._incoming: queue.Queue = queue.Queue(maxsize=256)
        self._outgoing: queue.Queue = queue.Queue(maxsize=32)
        self._read_thread: threading.Thread | None = None
        self._write_thread: threading.Thread | None = None
        self._closed = False
        self.cleanup_confirmed = False
        self.exit_code: int | None = None
        self.launch_token = uuid.uuid4().hex
        self.created_at: float | None = None

    @property
    def pid(self) -> int | None:
        return self.process.pid if self.process else None

    @property
    def alive(self) -> bool:
        return self.process is not None and self.process.poll() is None

    def _read(self) -> None:
        try:
            while not self._closed:
                frame = read_frame(self.process.stdout)
                self._receive(("frame", frame))
                if frame is None:
                    return
        except BaseException as exc:
            self._receive(("fault", type(exc).__name__))

    def _receive(self, value) -> None:
        # A full event queue must not leave a transport thread blocked after
        # its consumer closes the generator or rejects a malicious frame.
        while not self._closed:
            try:
                self._incoming.put(value, timeout=.05)
                return
            except queue.Full:
                continue

    def _write(self) -> None:
        try:
            while not self._closed:
                try:
                    value = self._outgoing.get(timeout=.05)
                except queue.Empty:
                    continue
                if value is None:
                    return
                self.process.stdin.write(value)
                self.process.stdin.flush()
        except BaseException as exc:
            self._receive(("fault", type(exc).__name__))

    def _send(self, **value) -> None:
        try:
            self._outgoing.put_nowait(encode_frame({"version": PROTOCOL_VERSION, **value}))
        except queue.Full as exc:
            raise ExecutionGuardError("Worker protocol input queue is stalled") from exc

    def _spawn(self) -> None:
        if self.process is not None:
            raise ExecutionGuardError("A worker process cannot be launched twice")
        self.process = subprocess.Popen(self.command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, cwd=str(Path(__file__).resolve().parents[3]), bufsize=65536,
            env={key: value for key, value in os.environ.items() if not key.upper().startswith("PYTHON")},
            **background_process_kwargs(new_process_group=True))
        try:
            # Child waits for init, so no backend or tool can run before its
            # process tree is owned. A job-assignment failure never falls back.
            self._job = windows_kill_job(self.process, name=job_name(self.launch_token))
            self.created_at = psutil.Process(self.process.pid).create_time()
        except BaseException:
            self.process.kill()
            self.process.wait(timeout=5)
            raise
        self._read_thread = threading.Thread(target=self._read, daemon=True, name="swarm-process-read")
        self._write_thread = threading.Thread(target=self._write, daemon=True, name="swarm-process-write")
        self._read_thread.start()
        self._write_thread.start()

    def _terminate_tree(self) -> None:
        if self.process is None:
            return
        if self._job:
            import ctypes
            from ctypes import wintypes
            api, handle = self._job
            api.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
            if not api.TerminateJobObject(handle, 1):
                raise ctypes.WinError(ctypes.get_last_error())
        elif os.name != "nt":
            try:
                os.killpg(self.process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        elif self.process.poll() is None:
            self.process.kill()

    def _tree_empty(self) -> bool:
        if self._job:
            import ctypes
            from ctypes import wintypes
            class Accounting(ctypes.Structure):
                _fields_ = [("times", ctypes.c_longlong * 4), ("page_faults", wintypes.DWORD),
                            ("total", wintypes.DWORD), ("active", wintypes.DWORD), ("terminated", wintypes.DWORD)]
            api, handle = self._job
            api.QueryInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int,
                                                       ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p]
            accounting = Accounting()
            if not api.QueryInformationJobObject(handle, 1, ctypes.byref(accounting), ctypes.sizeof(accounting), None):
                raise ctypes.WinError(ctypes.get_last_error())
            return accounting.active == 0
        if os.name == "nt":
            return self.process.poll() is not None
        import psutil
        for process in psutil.process_iter(["pid", "status"]):
            try:
                if os.getpgid(process.pid) == self.process.pid and process.info["status"] != psutil.STATUS_ZOMBIE:
                    return False
            except (ProcessLookupError, psutil.NoSuchProcess):
                continue
            except (PermissionError, psutil.AccessDenied):
                return False
        return True

    def run(
        self, initial: dict[str, Any], *, rpc: dict[str, Callable],
        cancel_event: threading.Event, pause_event: threading.Event,
        on_started: Callable[[ManagedWorkerProcess], None] | None = None,
    ) -> Iterator[dict[str, Any]]:
        """Forward incremental events; return only after observed process cleanup."""
        initialized = False
        child_closed = False
        saw_eof = False
        eof_since = None
        exit_since = None
        next_rpc = 1
        pending_rpc = False
        responses: queue.Queue = queue.Queue(maxsize=1)
        control = None
        cancel_since = None
        started = time.monotonic()
        try:
            self._spawn()
            if on_started is not None:
                on_started(self)
            while True:
                current = (cancel_event.is_set(), pause_event.is_set())
                if initialized and current != control:
                    self._send(kind="control", cancel=current[0], paused=current[1])
                    control = current
                if current[0]:
                    cancel_since = cancel_since or time.monotonic()
                    if time.monotonic() - cancel_since >= self.cancel_grace:
                        self._terminate_tree()
                if not initialized and time.monotonic() - started > 15:
                    raise ExecutionGuardError("Worker process did not complete its startup handshake")
                try:
                    response = responses.get_nowait()
                except queue.Empty:
                    response = None
                if response is not None:
                    pending_rpc = False
                    self._send(kind="response", **response)
                try:
                    kind, value = self._incoming.get(timeout=.05)
                except queue.Empty:
                    if self.process.poll() is not None:
                        exit_since = exit_since or time.monotonic()
                        if saw_eof:
                            break
                        # A descendant may retain stdout after the root exits.
                        self._terminate_tree()
                        if time.monotonic() - exit_since > 3:
                            raise ExecutionGuardError("Worker output closure was not observed after process exit")
                    if saw_eof and eof_since is not None and time.monotonic() - eof_since > 1:
                        self._terminate_tree()
                    continue
                if kind == "fault":
                    raise ExecutionGuardError("Worker protocol transport failed")
                if value is None:
                    saw_eof = True
                    eof_since = time.monotonic()
                    if self.process.poll() is not None:
                        break
                    # EOF alone is not termination; request owned cleanup.
                    if not child_closed:
                        self._terminate_tree()
                    continue
                frame_kind = value.get("kind")
                if not initialized:
                    if value != {"version": PROTOCOL_VERSION, "kind": "ready"}:
                        raise ExecutionGuardError("Worker startup handshake was invalid")
                    self._send(kind="init", payload=initial)
                    initialized = True
                    continue
                if child_closed:
                    raise ExecutionGuardError("Worker sent traffic after its closing observation")
                if frame_kind == "event" and set(value) == {"version", "kind", "event"}:
                    event = value["event"]
                    if type(event) is not dict or event.get("event") not in _EVENT_NAMES:
                        raise ExecutionGuardError("Worker sent an unsupported engine event")
                    yield event
                elif frame_kind == "rpc" and set(value) == {"version", "kind", "id", "operation", "args", "kwargs"}:
                    identity = value["id"]
                    if (type(identity) is not int or identity != next_rpc or pending_rpc
                            or value["operation"] not in rpc or type(value["args"]) is not list
                            or type(value["kwargs"]) is not dict):
                        raise ExecutionGuardError("Worker RPC identity or operation is invalid")
                    next_rpc += 1
                    pending_rpc = True
                    def invoke(frame=value):
                        try:
                            result = rpc[frame["operation"]](*frame["args"], **frame["kwargs"])
                            response = {"id": frame["id"], "ok": True, "result": result}
                            encode_frame({"version": PROTOCOL_VERSION, "kind": "response", **response})
                        except BaseException as exc:
                            response = {"id": frame["id"], "ok": False,
                                        "error": f"Worker operation denied ({type(exc).__name__})"}
                        responses.put(response)
                    threading.Thread(target=invoke, daemon=True, name="swarm-process-rpc").start()
                elif value == {"version": PROTOCOL_VERSION, "kind": "closed"} and not pending_rpc:
                    child_closed = True
                else:
                    raise ExecutionGuardError("Worker protocol message is invalid")
            self.exit_code = self.process.wait(timeout=5)
            if not child_closed or self.exit_code != 0:
                raise ExecutionGuardError("Worker process ended without a clean completed protocol; reconcile in-flight work")
        finally:
            self.close()

    def close(self) -> None:
        """Confirm the owned tree is empty; never infer cleanup from pipe EOF."""
        if self._closed:
            if not self.cleanup_confirmed:
                raise ExecutionGuardError("Worker process cleanup remains unconfirmed")
            return
        self._closed = True
        try:
            if self.process is not None:
                self._terminate_tree()
                self.exit_code = self.process.wait(timeout=5)
                deadline = time.monotonic() + 3
                while not self._tree_empty():
                    if time.monotonic() >= deadline:
                        raise ExecutionGuardError("Owned worker process tree did not terminate")
                    time.sleep(.02)
            self.cleanup_confirmed = True
        finally:
            close_windows_job(self._job)
            self._job = None
            try:
                self._outgoing.put_nowait(None)
            except queue.Full:
                pass
            for thread in (self._read_thread, self._write_thread):
                if thread is not None:
                    thread.join(timeout=.5)
            if self.process is not None:
                for stream in (self.process.stdin, self.process.stdout):
                    if stream is not None:
                        stream.close()
