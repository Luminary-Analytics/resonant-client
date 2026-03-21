"""
Scheduled Tasks for Resonant Client.

Runs prompts on a schedule (cron-like or interval-based).
Uses TaskRunner for actual execution.
"""

import json
import logging
import re
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Optional

from .task_runner import TaskRunner

logger = logging.getLogger(__name__)


def _parse_interval(schedule: str) -> Optional[int]:
    """Parse 'every:Nm' or 'every:Nh' to seconds. Returns None if not interval format."""
    m = re.match(r"every:(\d+)(m|h|s)", schedule.strip().lower())
    if not m:
        return None
    val = int(m.group(1))
    unit = m.group(2)
    if unit == "s":
        return val
    if unit == "m":
        return val * 60
    if unit == "h":
        return val * 3600
    return None


@dataclass
class ScheduledTask:
    """A recurring scheduled task."""
    id: str
    name: str
    prompt: str
    schedule: str  # "every:5m", "every:1h", etc.
    backend_type: str
    model: str
    project_path: str = ""
    enabled: bool = True
    last_run: str = ""
    next_run: str = ""
    run_count: int = 0
    created_at: str = ""

    def __post_init__(self):
        if not self.created_at:
            self.created_at = datetime.now().isoformat()
        if not self.next_run and self.enabled:
            self._compute_next_run()

    def _compute_next_run(self):
        """Compute next run time based on schedule."""
        interval = _parse_interval(self.schedule)
        if interval:
            base = datetime.fromisoformat(self.last_run) if self.last_run else datetime.now()
            self.next_run = (base + timedelta(seconds=interval)).isoformat()

    def is_due(self) -> bool:
        """Check if this task should run now."""
        if not self.enabled or not self.next_run:
            return False
        try:
            return datetime.now() >= datetime.fromisoformat(self.next_run)
        except ValueError:
            return False

    def mark_run(self):
        """Mark that this task just ran."""
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
            "project_path": self.project_path,
            "enabled": self.enabled,
            "last_run": self.last_run,
            "next_run": self.next_run,
            "run_count": self.run_count,
            "created_at": self.created_at,
        }


class Scheduler:
    """Manages scheduled tasks with a daemon thread."""

    CHECK_INTERVAL = 30  # seconds between checks

    def __init__(
        self,
        task_runner: TaskRunner,
        backend_factory: Optional[Callable] = None,
        persist_path: str | Path | None = None,
    ):
        self._runner = task_runner
        self._backend_factory = backend_factory
        self._schedules: dict[str, ScheduledTask] = {}
        self._lock = threading.Lock()
        self._persist_path = Path(persist_path) if persist_path else Path.home() / ".resonant" / "schedules.json"
        self._daemon: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._on_trigger: Optional[Callable] = None  # callback(scheduled_task) when triggered
        self._load()

    def set_backend_factory(self, factory: Callable):
        """Set the backend factory for creating backends when tasks run."""
        self._backend_factory = factory

    def set_on_trigger(self, callback: Callable):
        """Set callback when a scheduled task triggers."""
        self._on_trigger = callback

    def start(self):
        """Start the scheduler daemon thread."""
        if self._daemon and self._daemon.is_alive():
            return
        self._stop_event.clear()
        self._daemon = threading.Thread(target=self._daemon_loop, daemon=True)
        self._daemon.start()
        logger.info("Scheduler daemon started")

    def stop(self):
        """Stop the scheduler daemon."""
        self._stop_event.set()
        if self._daemon:
            self._daemon.join(timeout=5)

    def _daemon_loop(self):
        """Main loop — check for due tasks every CHECK_INTERVAL seconds."""
        while not self._stop_event.is_set():
            try:
                self._check_and_run()
            except Exception as e:
                logger.error(f"Scheduler check error: {e}")
            self._stop_event.wait(timeout=self.CHECK_INTERVAL)

    def _check_and_run(self):
        """Check for due tasks and submit them."""
        with self._lock:
            due_tasks = [t for t in self._schedules.values() if t.is_due()]

        for task in due_tasks:
            if not self._backend_factory:
                logger.warning(f"No backend factory for scheduled task {task.id}")
                continue

            def make_factory(bt=task.backend_type, m=task.model):
                def factory():
                    from ..backends import create_backend
                    # Simplified — real implementation would use stored config
                    return create_backend(bt, model=m)
                return factory

            logger.info(f"Triggering scheduled task: {task.name} ({task.id})")
            self._runner.submit(
                name=f"[scheduled] {task.name}",
                prompt=task.prompt,
                backend_factory=make_factory(),
                backend_type=task.backend_type,
                model=task.model,
                project_path=task.project_path,
            )

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
    ) -> ScheduledTask:
        """Add a new scheduled task."""
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
            for key in ("name", "prompt", "schedule", "backend_type", "model", "enabled"):
                if key in kwargs:
                    setattr(task, key, kwargs[key])
            if "schedule" in kwargs:
                task._compute_next_run()
            self._save()
            return True

    def list_schedules(self) -> list[dict]:
        with self._lock:
            return [t.to_dict() for t in sorted(
                self._schedules.values(),
                key=lambda t: t.created_at,
                reverse=True,
            )]

    def _save(self):
        try:
            self._persist_path.parent.mkdir(parents=True, exist_ok=True)
            data = {tid: t.to_dict() for tid, t in self._schedules.items()}
            self._persist_path.write_text(
                json.dumps(data, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
        except OSError as e:
            logger.error(f"Failed to save schedules: {e}")

    def _load(self):
        if not self._persist_path.exists():
            return
        try:
            data = json.loads(self._persist_path.read_text(encoding="utf-8"))
            for tid, td in data.items():
                self._schedules[tid] = ScheduledTask(
                    id=td["id"],
                    name=td.get("name", ""),
                    prompt=td.get("prompt", ""),
                    schedule=td.get("schedule", ""),
                    backend_type=td.get("backend_type", ""),
                    model=td.get("model", ""),
                    project_path=td.get("project_path", ""),
                    enabled=td.get("enabled", True),
                    last_run=td.get("last_run", ""),
                    next_run=td.get("next_run", ""),
                    run_count=td.get("run_count", 0),
                    created_at=td.get("created_at", ""),
                )
        except Exception as e:
            logger.warning(f"Failed to load schedules: {e}")
