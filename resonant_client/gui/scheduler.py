"""
Scheduled Tasks for Resonant Client.

Runs prompts on an interval schedule and uses TaskRunner for execution.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Optional

from .task_runner import TaskRunner

logger = logging.getLogger(__name__)


def _parse_interval(schedule: str) -> Optional[int]:
    """Parse 'every:Nm' or 'every:Nh' to seconds."""
    match = re.match(r"every:(\d+)(m|h|s)", schedule.strip().lower())
    if not match:
        return None
    value = int(match.group(1))
    unit = match.group(2)
    if unit == "s":
        return value
    if unit == "m":
        return value * 60
    if unit == "h":
        return value * 3600
    return None


@dataclass
class ScheduledTask:
    """A recurring scheduled task."""

    id: str
    name: str
    prompt: str
    schedule: str
    backend_type: str
    model: str
    task_kind: str = "session"
    max_loops: int = 6
    session_mode: str = "code"
    session_role: str = "generator"
    backend_spec: dict = None
    project_path: str = ""
    enabled: bool = True
    last_run: str = ""
    next_run: str = ""
    run_count: int = 0
    created_at: str = ""

    def __post_init__(self):
        self.backend_spec = dict(self.backend_spec or {})
        self.task_kind = self.task_kind or "session"
        self.max_loops = max(1, int(self.max_loops or 6))
        if not self.created_at:
            self.created_at = datetime.now().isoformat()
        if not self.next_run and self.enabled:
            self._compute_next_run()

    def _compute_next_run(self):
        interval = _parse_interval(self.schedule)
        if interval:
            base = datetime.fromisoformat(self.last_run) if self.last_run else datetime.now()
            self.next_run = (base + timedelta(seconds=interval)).isoformat()

    def is_due(self) -> bool:
        if not self.enabled or not self.next_run:
            return False
        try:
            return datetime.now() >= datetime.fromisoformat(self.next_run)
        except ValueError:
            return False

    def mark_run(self):
        self.last_run = datetime.now().isoformat()
        self.run_count += 1
        self._compute_next_run()

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "prompt": self.prompt,
            "schedule": self.schedule,
            "backend_type": self.backend_type,
            "model": self.model,
            "task_kind": self.task_kind,
            "max_loops": self.max_loops,
            "session_mode": self.session_mode,
            "session_role": self.session_role,
            "backend_spec": self.backend_spec,
            "project_path": self.project_path,
            "enabled": self.enabled,
            "last_run": self.last_run,
            "next_run": self.next_run,
            "run_count": self.run_count,
            "created_at": self.created_at,
        }


class Scheduler:
    """Manages scheduled tasks with a daemon thread."""

    CHECK_INTERVAL = 30

    def __init__(
        self,
        task_runner: TaskRunner,
        backend_factory: Optional[Callable[[ScheduledTask], Callable]] = None,
        persist_path: str | Path | None = None,
    ):
        self._runner = task_runner
        self._backend_factory = backend_factory
        self._special_executor: Optional[Callable[[ScheduledTask], Any]] = None
        self._schedules: dict[str, ScheduledTask] = {}
        self._lock = threading.Lock()
        self._persist_path = Path(persist_path) if persist_path else Path.home() / ".resonant" / "schedules.json"
        self._daemon: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._on_trigger: Optional[Callable] = None
        self._load()

    def set_backend_factory(self, factory: Callable[[ScheduledTask], Callable]):
        """Set the task session factory builder for scheduled runs."""
        self._backend_factory = factory

    def set_special_executor(self, executor: Callable[[ScheduledTask], Any]):
        """Set executor for non-session scheduled task kinds."""
        self._special_executor = executor

    def set_on_trigger(self, callback: Callable):
        self._on_trigger = callback

    def start(self):
        if self._daemon and self._daemon.is_alive():
            return
        self._stop_event.clear()
        self._daemon = threading.Thread(target=self._daemon_loop, daemon=True)
        self._daemon.start()
        logger.info("Scheduler daemon started")

    def stop(self):
        self._stop_event.set()
        if self._daemon:
            self._daemon.join(timeout=5)

    def _daemon_loop(self):
        while not self._stop_event.is_set():
            try:
                self._check_and_run()
            except Exception as exc:
                logger.error(f"Scheduler check error: {exc}")
            self._stop_event.wait(timeout=self.CHECK_INTERVAL)

    def _check_and_run(self):
        with self._lock:
            due_tasks = [task for task in self._schedules.values() if task.is_due()]

        for task in due_tasks:
            try:
                if task.task_kind == "harness_cycle":
                    if not self._special_executor:
                        logger.warning(f"No special executor for scheduled task {task.id}")
                        continue
                    logger.info(f"Triggering scheduled harness cycle: {task.name} ({task.id})")
                    self._special_executor(task)
                else:
                    if not self._backend_factory:
                        logger.warning(f"No backend factory for scheduled task {task.id}")
                        continue

                    session_factory = self._backend_factory(task)
                    if not session_factory:
                        logger.warning(f"No session factory for scheduled task {task.id}")
                        continue

                    logger.info(f"Triggering scheduled task: {task.name} ({task.id})")
                    self._runner.submit(
                        name=f"[scheduled] {task.name}",
                        prompt=task.prompt,
                        session_factory=session_factory,
                        backend_type=task.backend_type,
                        model=task.model,
                        project_path=task.project_path,
                        session_mode=task.session_mode,
                        session_role=task.session_role,
                        backend_spec=task.backend_spec,
                    )
            except Exception as exc:
                logger.error(f"Failed to trigger scheduled task {task.id}: {exc}")
                continue

            task.mark_run()
            self._save()

            if self._on_trigger:
                try:
                    self._on_trigger(task)
                except Exception:
                    pass

    def add(
        self,
        name: str,
        prompt: str,
        schedule: str,
        backend_type: str,
        model: str,
        project_path: str = "",
        task_kind: str = "session",
        max_loops: int = 6,
        session_mode: str = "code",
        session_role: str = "generator",
        backend_spec: Optional[dict] = None,
    ) -> ScheduledTask:
        interval = _parse_interval(schedule)
        if interval is None:
            raise ValueError(f"Invalid schedule format: {schedule}. Use 'every:Nm', 'every:Nh', or 'every:Ns'")

        task = ScheduledTask(
            id=uuid.uuid4().hex[:12],
            name=name or prompt[:50],
            prompt=prompt,
            schedule=schedule,
            backend_type=backend_type,
            model=model,
            task_kind=task_kind,
            max_loops=max(1, int(max_loops)),
            session_mode=session_mode,
            session_role=session_role,
            backend_spec=backend_spec or {},
            project_path=project_path,
        )

        with self._lock:
            self._schedules[task.id] = task
        self._save()
        return task

    def remove(self, task_id: str) -> bool:
        with self._lock:
            if task_id in self._schedules:
                del self._schedules[task_id]
                self._save()
                return True
        return False

    def enable(self, task_id: str) -> bool:
        with self._lock:
            task = self._schedules.get(task_id)
            if task:
                task.enabled = True
                task._compute_next_run()
                self._save()
                return True
        return False

    def disable(self, task_id: str) -> bool:
        with self._lock:
            task = self._schedules.get(task_id)
            if task:
                task.enabled = False
                self._save()
                return True
        return False

    def update(self, task_id: str, **kwargs) -> bool:
        with self._lock:
            task = self._schedules.get(task_id)
            if not task:
                return False
            for key in (
                "name",
                "prompt",
                "schedule",
                "backend_type",
                "model",
                "task_kind",
                "max_loops",
                "session_mode",
                "session_role",
                "enabled",
                "backend_spec",
            ):
                if key in kwargs:
                    value = kwargs[key]
                    if key == "max_loops":
                        value = max(1, int(value or 6))
                    elif key == "task_kind":
                        value = value or "session"
                    setattr(task, key, value)
            if "schedule" in kwargs:
                task._compute_next_run()
            self._save()
            return True

    def list_schedules(self) -> list[dict]:
        with self._lock:
            return [
                task.to_dict()
                for task in sorted(self._schedules.values(), key=lambda item: item.created_at, reverse=True)
            ]

    def _save(self):
        try:
            self._persist_path.parent.mkdir(parents=True, exist_ok=True)
            data = {task_id: task.to_dict() for task_id, task in self._schedules.items()}
            self._persist_path.write_text(
                json.dumps(data, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
        except OSError as exc:
            logger.error(f"Failed to save schedules: {exc}")

    def _load(self):
        if not self._persist_path.exists():
            return
        try:
            data = json.loads(self._persist_path.read_text(encoding="utf-8"))
            for task_id, task_data in data.items():
                self._schedules[task_id] = ScheduledTask(
                    id=task_data["id"],
                    name=task_data.get("name", ""),
                    prompt=task_data.get("prompt", ""),
                    schedule=task_data.get("schedule", ""),
                    backend_type=task_data.get("backend_type", ""),
                    model=task_data.get("model", ""),
                    task_kind=task_data.get("task_kind", "session"),
                    max_loops=task_data.get("max_loops", 6),
                    session_mode=task_data.get("session_mode", "code"),
                    session_role=task_data.get("session_role", "generator"),
                    backend_spec=task_data.get("backend_spec", {}),
                    project_path=task_data.get("project_path", ""),
                    enabled=task_data.get("enabled", True),
                    last_run=task_data.get("last_run", ""),
                    next_run=task_data.get("next_run", ""),
                    run_count=task_data.get("run_count", 0),
                    created_at=task_data.get("created_at", ""),
                )
        except Exception as exc:
            logger.warning(f"Failed to load schedules: {exc}")
