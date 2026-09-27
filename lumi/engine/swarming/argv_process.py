"""A trusted argv gate with bounded output and the native owned-tree protocol."""

from __future__ import annotations

import base64
from dataclasses import dataclass
import math
import os
from pathlib import Path
import sys
import threading
import time

from ..execution_guard import ExecutionGuardError
from .process_worker import ManagedWorkerProcess, _source_command

_NAMED_JOB_SUPPORT = os.name == "nt"


def effect_support() -> dict[str, str | bool]:
    """Arbitrary check descendants require actual OS job containment."""
    return {"supported": _NAMED_JOB_SUPPORT, "reason": "" if _NAMED_JOB_SUPPORT else
            "Supervised writers and integration checks require Windows named-job process containment; this host is unsupported"}


@dataclass(frozen=True)
class ArgvResult:
    """Observed command output is separate from gate-process termination."""

    exit_code: int | None
    stdout: bytes
    stderr: bytes
    output_truncated: bool
    output_complete: bool
    timed_out: bool
    cancelled: bool
    interruption: BaseException | None = None


class ManagedArgvProcess(ManagedWorkerProcess):
    """Launch a trusted local check/Git command only after host ownership commit."""

    def __init__(self, *, command=None):
        super().__init__(command=command if command is not None else (
            [sys.executable, "--swarm-effect"] if getattr(sys, "frozen", False) else _source_command("--swarm-effect")),
            cancel_grace=.1)
        self.result: ArgvResult | None = None

    def execute(self, argv, cwd, *, environment=None, timeout_seconds=60,
                max_output_bytes=8 * 1024 * 1024, on_started=None, cancel_check=None) -> ArgvResult:
        """Observe the exact command; timeout/cancellation never assert success."""
        if not effect_support()["supported"]:
            raise ExecutionGuardError(effect_support()["reason"])
        if (type(timeout_seconds) not in (int, float) or not math.isfinite(timeout_seconds)
                or not 0 < timeout_seconds <= 1200 or type(max_output_bytes) is not int
                or not 1 <= max_output_bytes <= 16 * 1024 * 1024):
            raise ValueError("Invalid managed argv limits")
        if not Path(cwd).is_absolute():
            raise ValueError("Managed argv requires a captured absolute cwd")
        initial = {"argv": list(argv), "cwd": str(cwd), "environment": dict(os.environ if environment is None else environment)}
        cancel, finished = threading.Event(), threading.Event()
        interruptions = []
        timed_out = False
        output = {"stdout": bytearray(), "stderr": bytearray()}
        truncated = False
        result = None
        deadline = time.monotonic() + timeout_seconds
        def monitor():
            nonlocal timed_out
            while not finished.wait(.025):
                if time.monotonic() >= deadline:
                    timed_out = True
                    cancel.set()
                    return
                if cancel_check is not None:
                    try:
                        cancel_check()
                    except BaseException as exc:
                        interruptions.append(exc)
                        cancel.set()
                        return
        watcher = threading.Thread(target=monitor, daemon=True, name="swarm-effect-control")
        watcher.start()
        def started(process):
            if cancel.is_set():
                raise ExecutionGuardError("Effect admission closed before executable input")
            if on_started is not None:
                on_started(process)
            if cancel.is_set():
                raise ExecutionGuardError("Effect admission closed before executable input")
        try:
            try:
                for event in super().run(initial, rpc={}, cancel_event=cancel, pause_event=threading.Event(), on_started=started):
                    if result is not None or event.get("event") != "status":
                        raise ExecutionGuardError("Effect gate sent an invalid result sequence")
                    if set(event) == {"event", "stream", "data"} and event["stream"] in output and type(event["data"]) is str:
                        chunk = base64.b64decode(event["data"], validate=True)
                        if len(chunk) > 8192:
                            raise ExecutionGuardError("Effect output chunk exceeds its fixed bound")
                        target = output[event["stream"]]
                        available = max_output_bytes - len(target)
                        target.extend(chunk[:available])
                        truncated = truncated or len(chunk) > available
                    elif (set(event) == {"event", "exit_code", "output_complete"}
                          and type(event["exit_code"]) is int and type(event["output_complete"]) is bool):
                        result = event
                    else:
                        raise ExecutionGuardError("Effect gate observation is malformed")
            except ExecutionGuardError:
                if not cancel.is_set() or not self.cleanup_confirmed:
                    raise
            if result is None and not cancel.is_set():
                raise ExecutionGuardError("Effect command completion was not observed")
            self.result = ArgvResult(result["exit_code"] if result else None, bytes(output["stdout"]), bytes(output["stderr"]),
                truncated, bool(result and result["output_complete"]), timed_out, cancel.is_set(),
                interruptions[0] if interruptions else None)
            return self.result
        except BaseException:
            # Already received bytes remain observations even when the final
            # protocol or cleanup fails. They can never establish completion.
            self.result = ArgvResult(result["exit_code"] if result else None, bytes(output["stdout"]), bytes(output["stderr"]),
                truncated, False, timed_out, cancel.is_set(), interruptions[0] if interruptions else None)
            raise
        finally:
            finished.set()
            watcher.join(timeout=.2)
