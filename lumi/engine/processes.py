"""
Process management tools for the Resonant Engine.

Provides:
- list_processes(name_filter)  → compact text table for the agent
- kill_process(pid|name)       → terminate with grace, then kill

Hard guardrails (refuse unsafe targets):
- pid below SYSTEM_PID_FLOOR (1000 on Windows, 100 on Unix)
- self (the running Python process)
- a few critical names
"""

from __future__ import annotations

import os
import sys
import time
from typing import Optional

try:
    import psutil
    _HAS_PSUTIL = True
except ImportError:
    _HAS_PSUTIL = False

from .tools import ToolResult


SYSTEM_PID_FLOOR = 1000 if sys.platform == "win32" else 100
NEVER_KILL_NAMES = frozenset({
    # Windows critical
    "system", "csrss.exe", "winlogon.exe", "services.exe", "lsass.exe",
    "wininit.exe", "smss.exe", "svchost.exe", "explorer.exe",
    # Unix critical
    "init", "systemd", "launchd", "kernel_task",
})


def _err(msg: str) -> dict:
    return {"error": msg, "processes": []}


def list_processes(name_filter: Optional[str] = None, *, limit: int = 100) -> dict:
    """
    Returns a list of running processes.

    {"processes": [{pid, name, cpu_percent, memory_mb, cmdline_short}, ...]}
    Filtered by case-insensitive substring match on `name` or `cmdline` if
    `name_filter` is given. Capped at `limit` entries.
    """
    if not _HAS_PSUTIL:
        return _err("psutil not installed")

    needle = (name_filter or "").lower().strip()
    rows: list[dict] = []
    for p in psutil.process_iter(["pid", "name", "cmdline", "memory_info"]):
        try:
            info = p.info
            name = (info.get("name") or "").lower()
            cmdline_list = info.get("cmdline") or []
            cmdline = " ".join(cmdline_list)
            if needle and (needle not in name and needle.lower() not in cmdline.lower()):
                continue
            mem = info.get("memory_info")
            mem_mb = round(mem.rss / 1024 / 1024, 1) if mem else 0.0
            rows.append({
                "pid": info.get("pid"),
                "name": info.get("name") or "",
                "memory_mb": mem_mb,
                "cmdline_short": (cmdline[:120] + "…") if len(cmdline) > 120 else cmdline,
            })
        except (psutil.NoSuchProcess, psutil.AccessDenied, Exception):
            continue

    rows.sort(key=lambda r: ((r.get("name") or "").lower(), r.get("pid") or 0))
    return {"processes": rows[: max(1, int(limit))]}


def kill_process(target: int | str) -> dict:
    """
    Terminate a process by pid (int) or exact name (str, case-insensitive).
    Refuses system PIDs, self, and known-critical names.

    Returns {"killed": [{pid, name}], "skipped": [{pid, name, reason}]}.
    """
    if not _HAS_PSUTIL:
        return {"error": "psutil not installed", "killed": [], "skipped": []}

    self_pid = os.getpid()
    candidates: list = []

    if isinstance(target, int) or (isinstance(target, str) and target.isdigit()):
        pid = int(target)
        try:
            candidates.append(psutil.Process(pid))
        except psutil.NoSuchProcess:
            return {"error": f"no process with pid {pid}", "killed": [], "skipped": []}
    elif isinstance(target, str):
        name = target.strip().lower()
        if not name:
            return {"error": "name is required", "killed": [], "skipped": []}
        for p in psutil.process_iter(["pid", "name"]):
            try:
                if (p.info.get("name") or "").lower() == name:
                    candidates.append(p)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        if not candidates:
            return {"error": f"no process named {target!r}", "killed": [], "skipped": []}
    else:
        return {"error": f"target must be pid (int) or name (str), got {type(target).__name__}", "killed": [], "skipped": []}

    killed: list[dict] = []
    skipped: list[dict] = []
    for p in candidates:
        try:
            pid = p.pid
            name = p.name() or ""
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue

        # Guardrails
        if pid < SYSTEM_PID_FLOOR:
            skipped.append({"pid": pid, "name": name, "reason": f"pid below floor ({SYSTEM_PID_FLOOR})"})
            continue
        if pid == self_pid:
            skipped.append({"pid": pid, "name": name, "reason": "self"})
            continue
        if name.lower() in NEVER_KILL_NAMES:
            skipped.append({"pid": pid, "name": name, "reason": "system-critical name"})
            continue

        try:
            p.terminate()
            try:
                p.wait(timeout=3.0)
            except psutil.TimeoutExpired:
                p.kill()
            killed.append({"pid": pid, "name": name})
        except (psutil.NoSuchProcess, psutil.AccessDenied) as e:
            skipped.append({"pid": pid, "name": name, "reason": str(e)})

    return {"killed": killed, "skipped": skipped}


# ── Exec wrappers ──────────────────────────────────────────────────────


def exec_process_list(args: dict, start: float) -> ToolResult:
    name_filter = args.get("name_filter")
    limit = int(args.get("limit", 100))
    data = list_processes(name_filter=name_filter, limit=limit)

    if data.get("error"):
        return ToolResult(data["error"], is_error=True, elapsed=time.time() - start, metadata=data)

    rows = data["processes"]
    if not rows:
        text = "No processes match." if name_filter else "No processes."
    else:
        lines = ["PID      MEM(MB)   NAME                          CMD"]
        for r in rows[:80]:
            lines.append(
                f"{(r['pid'] or 0):<8} {r['memory_mb']:<9} {(r['name'] or '')[:30]:<30}  {r['cmdline_short']}"
            )
        if len(rows) > 80:
            lines.append(f"…({len(rows) - 80} more)")
        text = "\n".join(lines)

    return ToolResult(text, elapsed=time.time() - start, metadata=data)


def exec_process_kill(args: dict, start: float) -> ToolResult:
    pid = args.get("pid")
    name = args.get("name")
    if (pid is None) == (name is None):  # XOR — exactly one
        return ToolResult(
            "Exactly one of `pid` or `name` is required.",
            is_error=True, elapsed=time.time() - start,
        )

    target = int(pid) if pid is not None else str(name)
    data = kill_process(target)
    if data.get("error"):
        return ToolResult(data["error"], is_error=True, elapsed=time.time() - start, metadata=data)

    killed_lines = [f"  ✓ killed pid={k['pid']} name={k['name']}" for k in data["killed"]]
    skipped_lines = [f"  ✗ skipped pid={s['pid']} name={s['name']} ({s['reason']})" for s in data["skipped"]]
    text = "\n".join(killed_lines + skipped_lines) or "No matching processes."
    return ToolResult(text, elapsed=time.time() - start, metadata=data)
