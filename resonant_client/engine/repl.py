"""
Persistent REPL processes for the Resonant Engine.

Spawns a long-lived `python` or `node` interpreter, sends code via stdin,
captures stdout/stderr until a per-call sentinel marker appears.

State persists across calls (the REPL is a single process), so the agent can
iterate on a snippet incrementally without paying a `bash` cold-start cost
each turn.

Safety:
- Hard per-call timeout (default 30s) prevents infinite loops from hanging the agent.
- Concurrent-REPL cap (default 4) prevents process leakage.
- Each REPL is killed on `stop_repl()` or process exit.
"""

from __future__ import annotations

import os
import queue
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Optional

from resonant_client.processes import background_process_kwargs

from .tools import ToolResult


MAX_CONCURRENT_REPLS = 4
DEFAULT_EVAL_TIMEOUT = 30.0


# ── ReplProcess ─────────────────────────────────────────────────────────


class ReplProcess:
    """A long-lived interpreter subprocess with stdin-driven evaluation."""

    def __init__(self, lang: str, cwd: Path):
        if lang not in ("python", "node"):
            raise ValueError(f"unsupported REPL language: {lang!r}")
        self.lang = lang
        self.cwd = cwd
        self.repl_id = uuid.uuid4().hex[:12]
        self.proc: Optional[subprocess.Popen] = None
        self.created_at = time.time()
        self.last_used_at = self.created_at
        self._stdout_q: "queue.Queue[Optional[str]]" = queue.Queue()
        self._stderr_q: "queue.Queue[Optional[str]]" = queue.Queue()
        self._eval_lock = threading.Lock()  # serialize evals on one REPL

    # ── Lifecycle ──

    def start(self) -> None:
        if self.lang == "python":
            # -u: unbuffered, -i: interactive after running -c.
            # Suppress prompts so they don't pollute captured stdout.
            cmd = [
                sys.executable, "-u", "-i", "-c",
                "import sys; sys.ps1 = ''; sys.ps2 = ''",
            ]
        else:  # node
            cmd = ["node", "--interactive"]

        try:
            self.proc = subprocess.Popen(
                cmd,
                cwd=str(self.cwd),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=1,  # line-buffered
                text=True,
                encoding="utf-8",
                errors="replace",
                shell=False,
                **background_process_kwargs(),
            )
        except FileNotFoundError as e:
            raise RuntimeError(f"{self.lang} executable not found: {e}") from e

        # Start pipe-reader threads. They push lines onto queues; the main
        # thread blocks on the queue when waiting for a sentinel.
        threading.Thread(
            target=self._pump_pipe, args=(self.proc.stdout, self._stdout_q),
            daemon=True, name=f"repl-{self.repl_id}-stdout",
        ).start()
        threading.Thread(
            target=self._pump_pipe, args=(self.proc.stderr, self._stderr_q),
            daemon=True, name=f"repl-{self.repl_id}-stderr",
        ).start()

        # Drain the startup banner. Node prints "Welcome to Node.js ..." plus
        # a prompt; Python prints a banner before the -c runs. Wait briefly
        # for it to appear, then drain.
        time.sleep(0.4)
        self._drain(self._stdout_q)
        self._drain(self._stderr_q)

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            try:
                if self.proc.stdin and not self.proc.stdin.closed:
                    self.proc.stdin.close()
            except Exception:
                pass
            try:
                self.proc.terminate()
                self.proc.wait(timeout=3.0)
            except subprocess.TimeoutExpired:
                self.proc.kill()
            except Exception:
                pass
        self.proc = None

    @property
    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    # ── Eval ──

    def eval(self, code: str, timeout: float = DEFAULT_EVAL_TIMEOUT) -> dict:
        if not self.alive:
            return {"error": "REPL has exited", "stdout": "", "stderr": ""}

        # Serialize concurrent evals on the same REPL — each must finish (or
        # time out) before the next can start, otherwise sentinel matching
        # would cross-talk.
        with self._eval_lock:
            self.last_used_at = time.time()

            nonce = uuid.uuid4().hex[:10]
            sentinel = f"<<<RESONANT-REPL-DONE-{nonce}>>>"

            if self.lang == "python":
                # Use repr so embedded quotes don't break the print.
                payload = code.rstrip() + f"\nprint({sentinel!r})\n"
            else:  # node
                # console.log accepts a JS string literal; escape backslashes/quotes.
                escaped = sentinel.replace("\\", "\\\\").replace("'", "\\'")
                payload = code.rstrip() + f"\nconsole.log('{escaped}');\n"

            try:
                self.proc.stdin.write(payload)
                self.proc.stdin.flush()
            except (BrokenPipeError, OSError, AttributeError) as e:
                return {"error": f"REPL stdin closed: {e}", "stdout": "", "stderr": ""}

            stdout_chunks: list[str] = []
            deadline = time.time() + max(0.1, timeout)
            found = False

            while time.time() < deadline:
                remaining = max(0.05, deadline - time.time())
                try:
                    line = self._stdout_q.get(timeout=min(remaining, 0.2))
                except queue.Empty:
                    continue
                if line is None:
                    # Pipe closed (REPL crashed or exited)
                    stderr = self._drain(self._stderr_q)
                    return {
                        "error": "REPL stdout closed unexpectedly",
                        "stdout": "".join(stdout_chunks),
                        "stderr": stderr,
                    }
                if sentinel in line:
                    idx = line.find(sentinel)
                    if idx > 0:
                        stdout_chunks.append(line[:idx])
                    found = True
                    break
                stdout_chunks.append(line)

            stderr = self._drain(self._stderr_q)

            if not found:
                return {
                    "error": f"REPL eval timed out after {timeout:.1f}s",
                    "stdout": "".join(stdout_chunks),
                    "stderr": stderr,
                }

            return {
                "error": None,
                "stdout": "".join(stdout_chunks),
                "stderr": stderr,
            }

    # ── Internals ──

    @staticmethod
    def _pump_pipe(pipe, q: "queue.Queue[Optional[str]]") -> None:
        """Read lines off `pipe` into `q`. Push None when EOF."""
        try:
            for line in iter(pipe.readline, ""):
                q.put(line)
        except Exception:
            pass
        finally:
            q.put(None)

    @staticmethod
    def _drain(q: "queue.Queue[Optional[str]]") -> str:
        """Non-blocking drain of any pending lines."""
        out: list[str] = []
        while True:
            try:
                line = q.get_nowait()
            except queue.Empty:
                break
            if line is None:
                break
            out.append(line)
        return "".join(out)


# ── Registry ────────────────────────────────────────────────────────────


_REPLS: dict[str, ReplProcess] = {}
_REPLS_LOCK = threading.Lock()


def _gc_dead_repls_locked() -> None:
    """Caller must hold _REPLS_LOCK. Drops processes that exited."""
    for rid in list(_REPLS.keys()):
        r = _REPLS[rid]
        if not r.alive:
            r.stop()
            del _REPLS[rid]


def start_repl(lang: str, cwd: Optional[str] = None) -> dict:
    with _REPLS_LOCK:
        _gc_dead_repls_locked()
        if len(_REPLS) >= MAX_CONCURRENT_REPLS:
            return {
                "error": f"too many active REPLs (limit: {MAX_CONCURRENT_REPLS}); stop one first",
                "active_ids": list(_REPLS.keys()),
            }

        cwd_path = Path(cwd or os.getcwd())
        if not cwd_path.is_dir():
            return {"error": f"cwd is not a directory: {cwd_path}"}

        try:
            repl = ReplProcess(lang, cwd_path)
            repl.start()
        except (RuntimeError, ValueError) as e:
            return {"error": str(e)}

        _REPLS[repl.repl_id] = repl
        return {"repl_id": repl.repl_id, "lang": lang, "cwd": str(cwd_path)}


def eval_repl(repl_id: str, code: str, timeout: float = DEFAULT_EVAL_TIMEOUT) -> dict:
    with _REPLS_LOCK:
        repl = _REPLS.get(repl_id)
    if not repl:
        return {"error": f"REPL '{repl_id}' not found", "stdout": "", "stderr": ""}
    return repl.eval(code, timeout=timeout)


def stop_repl(repl_id: str) -> dict:
    with _REPLS_LOCK:
        repl = _REPLS.pop(repl_id, None)
    if not repl:
        return {"error": f"REPL '{repl_id}' not found"}
    repl.stop()
    return {"stopped": repl_id}


def list_repls() -> list[dict]:
    with _REPLS_LOCK:
        _gc_dead_repls_locked()
        return [
            {
                "repl_id": r.repl_id,
                "lang": r.lang,
                "cwd": str(r.cwd),
                "alive": r.alive,
                "age_seconds": round(time.time() - r.created_at, 1),
                "idle_seconds": round(time.time() - r.last_used_at, 1),
            }
            for r in _REPLS.values()
        ]


def stop_all_repls() -> int:
    """Used at shutdown; returns count stopped."""
    with _REPLS_LOCK:
        n = len(_REPLS)
        for r in _REPLS.values():
            r.stop()
        _REPLS.clear()
    return n


# ── Exec wrappers (ToolResult shape used by tools.execute_tool) ─────────


def _format_eval_result(repl_id: str, lang: str, data: dict) -> str:
    parts = [f"[{lang}:{repl_id[:8]}]"]
    if data.get("error"):
        parts.append(f"ERROR: {data['error']}")
    out = (data.get("stdout") or "").rstrip()
    err = (data.get("stderr") or "").rstrip()
    if out:
        parts.append(out)
    if err:
        parts.append("--- stderr ---")
        parts.append(err)
    return "\n".join(parts)


def _exec_repl_start(args: dict, start: float, lang: str) -> ToolResult:
    cwd = args.get("cwd")
    data = start_repl(lang, cwd=cwd)
    if data.get("error"):
        return ToolResult(
            f"{lang} REPL start failed: {data['error']}",
            is_error=True,
            elapsed=time.time() - start,
            metadata=data,
        )
    return ToolResult(
        f"Started {lang} REPL [{data['repl_id'][:8]}] in {data['cwd']}",
        elapsed=time.time() - start,
        metadata=data,
    )


def _exec_repl_eval(args: dict, start: float, lang: str) -> ToolResult:
    repl_id = (args.get("repl_id") or "").strip()
    code = args.get("code", "")
    timeout = float(args.get("timeout", DEFAULT_EVAL_TIMEOUT))
    if not repl_id:
        return ToolResult("repl_id is required", is_error=True, elapsed=time.time() - start)
    if not code:
        return ToolResult("code is required", is_error=True, elapsed=time.time() - start)
    data = eval_repl(repl_id, code, timeout=timeout)
    output = _format_eval_result(repl_id, lang, data)
    return ToolResult(
        output,
        is_error=bool(data.get("error")),
        elapsed=time.time() - start,
        metadata=data,
    )


def _exec_repl_stop(args: dict, start: float, lang: str) -> ToolResult:
    repl_id = (args.get("repl_id") or "").strip()
    if not repl_id:
        return ToolResult("repl_id is required", is_error=True, elapsed=time.time() - start)
    data = stop_repl(repl_id)
    if data.get("error"):
        return ToolResult(data["error"], is_error=True, elapsed=time.time() - start, metadata=data)
    return ToolResult(f"Stopped {lang} REPL [{repl_id[:8]}]", elapsed=time.time() - start, metadata=data)


# Public exec_* functions (one per (lang, action) pair).

def exec_repl_python_start(args: dict, start: float) -> ToolResult:
    return _exec_repl_start(args, start, "python")


def exec_repl_python_eval(args: dict, start: float) -> ToolResult:
    return _exec_repl_eval(args, start, "python")


def exec_repl_python_stop(args: dict, start: float) -> ToolResult:
    return _exec_repl_stop(args, start, "python")


def exec_repl_node_start(args: dict, start: float) -> ToolResult:
    return _exec_repl_start(args, start, "node")


def exec_repl_node_eval(args: dict, start: float) -> ToolResult:
    return _exec_repl_eval(args, start, "node")


def exec_repl_node_stop(args: dict, start: float) -> ToolResult:
    return _exec_repl_stop(args, start, "node")
