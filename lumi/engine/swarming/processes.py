"""Host-generated process identity and conservative restart observations."""

from __future__ import annotations

import math
import os
import time
import uuid

import psutil

from ...paths import state_home
from .models import AttemptContext, Conflict, RunAuthority, ScopeDenied, require_id
from .store import SwarmStore


def host_identity() -> str:
    """Load one local installation identity without exposing machine metadata."""
    path = state_home() / "swarm-host-id"
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        # A concurrent local startup may still be completing its short write.
        for _ in range(20):
            identity = path.read_text(encoding="utf-8").strip()
            if identity:
                require_id(identity)
                return identity
            time.sleep(.01)
        raise Conflict("Local process host identity is incomplete; inspect its retained file")
    identity = "host-" + uuid.uuid4().hex
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        stream.write(identity)
        stream.flush()
        os.fsync(stream.fileno())
    return identity


def job_name(launch_token: str) -> str:
    if type(launch_token) is not str or len(launch_token) != 32 or any(char not in "0123456789abcdef" for char in launch_token):
        raise ValueError("Process launch identity must be a generated hexadecimal token")
    return "Local\\SONN.Swarm." + launch_token


def windows_job_active(name: str) -> int | None:
    """Query an existing named job; None means no such owned job remains."""
    import ctypes
    from ctypes import wintypes
    api = ctypes.WinDLL("kernel32", use_last_error=True)
    api.OpenJobObjectW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
    api.OpenJobObjectW.restype = wintypes.HANDLE
    api.CloseHandle.argtypes = [wintypes.HANDLE]
    api.QueryInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p,
                                               wintypes.DWORD, ctypes.c_void_p]
    handle = api.OpenJobObjectW(4, False, name)
    if not handle:
        error = ctypes.get_last_error()
        if error == 2:
            return None
        raise ctypes.WinError(error)
    class Accounting(ctypes.Structure):
        _fields_ = [("times", ctypes.c_longlong * 4), ("page_faults", wintypes.DWORD),
                    ("total", wintypes.DWORD), ("active", wintypes.DWORD), ("terminated", wintypes.DWORD)]
    try:
        result = Accounting()
        if not api.QueryInformationJobObject(handle, 1, ctypes.byref(result), ctypes.sizeof(result), None):
            raise ctypes.WinError(ctypes.get_last_error())
        return result.active
    finally:
        api.CloseHandle(handle)


def _os_observation(record: dict) -> str:
    """Distinguish PID reuse and inaccessible processes without guessing exit."""
    try:
        process = psutil.Process(record["pid"])
        same = abs(process.create_time() - record["created_at"]) < .001
        root_alive = same and process.status() != psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        root_alive = False
    except (psutil.AccessDenied, OSError):
        return "unknown"
    try:
        if os.name == "nt":
            active = windows_job_active(job_name(record["launch_token"]))
            if active:
                return "running"
            return "running" if root_alive else "stopped"
        for member in psutil.process_iter(["pid", "status"]):
            try:
                if os.getpgid(member.pid) == record["pid"] and member.info["status"] != psutil.STATUS_ZOMBIE:
                    return "running"
            except (ProcessLookupError, psutil.NoSuchProcess):
                continue
        return "running" if root_alive else "stopped"
    except (OSError, psutil.AccessDenied):
        return "unknown"


class ProcessObservations:
    """Trusted host observer; accepts no browser-reported liveness evidence."""

    def __init__(self, store: SwarmStore, *, host_id: str | None = None):
        self.store = store
        self.host_id = host_id or host_identity()
        require_id(self.host_id)

    def started(self, authority: RunAuthority, context: AttemptContext, *, pid: int,
                created_at: float, launch_token: str) -> dict:
        """Commit observed ownership before the parent permits child generation."""
        if type(pid) is not int or pid <= 0 or type(created_at) not in (int, float) or not math.isfinite(created_at):
            raise ValueError("Process identity must contain an observed PID and creation time")
        job_name(launch_token)
        process = psutil.Process(pid)
        if abs(process.create_time() - created_at) >= .001:
            raise Conflict("Process identity changed before launch admission")
        record = {"attempt_id": context.attempt_id, "run_id": context.run_id, "epoch": context.epoch,
                  "host_id": self.host_id, "pid": pid, "created_at": created_at,
                  "launch_token": launch_token, "state": "started", "exit_code": None}
        with self.store._connection(write=True) as connection:
            self.store._admitting(self.store._authority(connection, authority))
            self.store._same_run(authority, context)
            attempt = self.store._attempt(connection, context)
            if attempt["state"] not in {"leased", "running"}:
                raise Conflict("Process launch requires an active attempt")
            prior = connection.execute("SELECT * FROM process_observations WHERE attempt_id=?", (context.attempt_id,)).fetchone()
            if prior is not None:
                if dict(prior) != record:
                    raise Conflict("An attempt cannot acquire a second process identity")
                return record
            connection.execute("INSERT INTO process_observations VALUES(?,?,?,?,?,?,?,?,?)", tuple(record.values()))
            self.store._event(connection, context.run_id, "worker_process_started",
                              {key: value for key, value in record.items() if key != "launch_token"})
        return record

    def stopped(self, context: AttemptContext, process) -> None:
        """Record captured process/tree cleanup even after authority expires."""
        if not process.cleanup_confirmed or process.exit_code is None:
            raise Conflict("Process cleanup has not been observed")
        with self.store._connection(write=True) as connection:
            self.store._run(connection, context.scope, context.run_id)
            attempt = connection.execute("SELECT worker_id,epoch FROM attempts WHERE id=? AND run_id=?",
                                         (context.attempt_id, context.run_id)).fetchone()
            row = connection.execute("SELECT * FROM process_observations WHERE attempt_id=?", (context.attempt_id,)).fetchone()
            if (attempt is None or row is None or attempt["worker_id"] != context.worker_id
                    or row["epoch"] != context.epoch or row["host_id"] != self.host_id
                    or row["pid"] != process.pid or row["launch_token"] != process.launch_token
                    or abs(row["created_at"] - process.created_at) >= .001):
                raise ScopeDenied("Process termination belongs to another captured identity")
            connection.execute("UPDATE process_observations SET state='stopped',exit_code=? WHERE attempt_id=?",
                               (process.exit_code, context.attempt_id))
            self.store._event(connection, context.run_id, "worker_process_stopped",
                              {"attempt_id": context.attempt_id, "epoch": context.epoch, "exit_code": process.exit_code})

    def inspect(self, scope, run_id: str, attempt_id: str) -> dict:
        """Observe local identity; a foreign host or missing record stays unknown."""
        with self.store._connection() as connection:
            self.store._run(connection, scope, run_id)
            row = connection.execute("SELECT * FROM process_observations WHERE attempt_id=? AND run_id=?",
                                     (attempt_id, run_id)).fetchone()
        if row is None:
            return {"attempt_id": attempt_id, "observation": "unknown", "reason": "No durable process identity"}
        record = dict(row)
        observed = _os_observation(record) if record["host_id"] == self.host_id else "unknown"
        return {**{key: value for key, value in record.items() if key != "launch_token"}, "observation": observed}

    def reconcile(self, authority: RunAuthority, attempt_id: str) -> dict:
        """Persist fresh OS observation; request/action accounting is unchanged."""
        observation = self.inspect(authority.scope, authority.run_id, attempt_id)
        with self.store._connection(write=True) as connection:
            self.store._authority(connection, authority)
            row = connection.execute("SELECT * FROM process_observations WHERE attempt_id=? AND run_id=?",
                                     (attempt_id, authority.run_id)).fetchone()
            if row is None or row["host_id"] != self.host_id:
                raise ScopeDenied("This host cannot reconcile the captured process")
            if any(observation.get(key) != row[key] for key in ("pid", "created_at", "epoch", "host_id")):
                raise Conflict("Process identity changed during observation")
            state = "stopped" if observation["observation"] == "stopped" else "unknown"
            connection.execute("UPDATE process_observations SET state=? WHERE attempt_id=?", (state, attempt_id))
            from .supervisor import SwarmSupervisor
            supervisor = SwarmSupervisor(self.store)
            supervisor._refresh_reservation(connection, attempt_id)
            supervisor._control_checkpoint(connection, authority.run_id)
            self.store._event(connection, authority.run_id, "worker_process_reconciled",
                              {"attempt_id": attempt_id, "observation": observation["observation"]})
        return observation
