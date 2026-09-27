"""Private gated argv executor; all durable authority stays in the parent."""

from __future__ import annotations

import base64
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time

from ...processes import background_process_kwargs
from .process_worker import PROTOCOL_VERSION, encode_frame, read_frame, stdio_pipes


def main() -> int:
    """Wait for owned-process admission before launching any trusted argv."""
    reader, writer = stdio_pipes()
    sys.stdout = sys.stderr = open(os.devnull, "w", encoding="utf-8")
    write_lock = threading.Lock()
    stop_output = threading.Event()
    def send(value):
        with write_lock:
            remaining = memoryview(encode_frame({"version": PROTOCOL_VERSION, **value}))
            while remaining:
                count = writer.write(remaining)
                if not count:
                    raise BrokenPipeError("Effect host closed")
                remaining = remaining[count:]
            writer.flush()
    try:
        send({"kind": "ready"})
        frame = read_frame(reader)
        if not frame or set(frame) != {"version", "kind", "payload"} or frame["kind"] != "init":
            return 2
        initial = frame["payload"]
        if type(initial) is not dict or set(initial) != {"argv", "cwd", "environment"}:
            return 2
        argv, cwd, environment = initial["argv"], initial["cwd"], initial["environment"]
        if (type(argv) is not list or not argv or any(type(value) is not str or not value or "\0" in value for value in argv)
                or type(cwd) is not str or not Path(cwd).is_absolute() or not Path(cwd).is_dir()
                or type(environment) is not dict or any(type(key) is not str or type(value) is not str
                    or "\0" in key or "\0" in value or "=" in key for key, value in environment.items())):
            return 2
        # This target inherits the parent's owned process group/job. It never
        # inherits the private protocol input, and cannot forge output frames.
        process = subprocess.Popen(argv, cwd=cwd, env=environment, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0,
            **background_process_kwargs(new_process_group=False))
        def control():
            try:
                while True:
                    message = read_frame(reader)
                    if message is None or message.get("cancel") is True:
                        break
                    if (set(message) != {"version", "kind", "cancel", "paused"}
                            or message["kind"] != "control" or type(message["cancel"]) is not bool
                            or type(message["paused"]) is not bool):
                        break
            except (OSError, ValueError):
                pass
            # POSIX parent death does not itself close the inherited group.
            # This gate is its leader; never signal an unrelated shared group.
            if os.name != "nt" and os.getpgrp() == os.getpid():
                os.killpg(os.getpid(), signal.SIGKILL)
            elif process.poll() is None:
                process.kill()
        threading.Thread(target=control, daemon=True, name="swarm-effect-parent").start()
        def drain(name, pipe):
            try:
                while True:
                    chunk = pipe.read(8192)
                    if not chunk:
                        return
                    with write_lock:
                        if stop_output.is_set():
                            return
                        remaining = memoryview(encode_frame({"version": PROTOCOL_VERSION, "kind": "event",
                            "event": {"event": "status", "stream": name, "data": base64.b64encode(chunk).decode("ascii")}}))
                        while remaining:
                            count = writer.write(remaining)
                            if not count:
                                return
                            remaining = remaining[count:]
                        writer.flush()
            except (OSError, ValueError):
                return
        readers = [threading.Thread(target=drain, args=(name, pipe), daemon=True)
                   for name, pipe in (("stdout", process.stdout), ("stderr", process.stderr))]
        for thread in readers:
            thread.start()
        code = process.wait()
        # Forwarding the last output waits on the host reading it, which can
        # take seconds on a busy machine; 1 s could report complete output as
        # incomplete. A descendant holding a pipe open still leaves it so.
        deadline = time.monotonic() + 10
        for thread in readers:
            thread.join(timeout=max(0, deadline - time.monotonic()))
        with write_lock:
            stop_output.set()
        complete = all(not thread.is_alive() for thread in readers)
        send({"kind": "event", "event": {"event": "status", "exit_code": code, "output_complete": complete}})
        send({"kind": "closed"})
        return 0
    except BaseException:
        # Configured argv and exception strings can contain credentials.
        return 2
    finally:
        writer.close()
        # The daemon control reader can still hold stdin's read lock. Process
        # exit owns this descriptor, as for the native worker child.
