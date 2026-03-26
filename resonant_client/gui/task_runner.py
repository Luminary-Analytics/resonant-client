"""
Background Task Runner for Resonant Client.

Executes prompts in background threads using the same session wiring as chat.
Shared by both Dispatch (one-off) and Scheduled Tasks (recurring).
"""

from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)


class TaskStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass
class BackgroundTask:
    """A single background task."""

    id: str
    name: str
    prompt: str
    backend_type: str
    model: str
    project_path: str
    session_mode: str = "code"
    session_role: str = "generator"
    backend_spec: dict[str, Any] = field(default_factory=dict)
    status: TaskStatus = TaskStatus.PENDING
    created_at: str = ""
    started_at: str = ""
    completed_at: str = ""
    result: str = ""
    error: str = ""
    display_events: list = field(default_factory=list)
    steps: int = 0
    elapsed: float = 0.0
    cancel_event: threading.Event = field(default_factory=threading.Event, repr=False, compare=False)
    session_factory: Optional[Callable[["BackgroundTask"], Any]] = field(default=None, repr=False, compare=False)
    session: Any = field(default=None, repr=False, compare=False)

    def __post_init__(self):
        if not self.created_at:
            self.created_at = datetime.now().isoformat()

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "prompt": self.prompt,
            "backend_type": self.backend_type,
            "model": self.model,
            "project_path": self.project_path,
            "session_mode": self.session_mode,
            "session_role": self.session_role,
            "backend_spec": self.backend_spec,
            "status": self.status.value,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "result": self.result[:500] if self.result else "",
            "error": self.error,
            "steps": self.steps,
            "elapsed": round(self.elapsed, 2),
        }

    def to_full_dict(self) -> dict:
        data = self.to_dict()
        data["result"] = self.result
        data["display_events"] = self.display_events
        return data


class TaskRunner:
    """Runs background tasks using a thread pool."""

    def __init__(self, max_concurrent: int = 3, persist_dir: str | Path | None = None):
        self._pool = ThreadPoolExecutor(max_workers=max_concurrent)
        self._tasks: dict[str, BackgroundTask] = {}
        self._lock = threading.Lock()
        self._persist_dir = Path(persist_dir) if persist_dir else Path.home() / ".resonant" / "tasks"
        self._on_complete: Optional[Callable] = None
        self._load_persisted()

    def set_on_complete(self, callback: Callable):
        """Set callback for task completion (called from worker thread)."""
        self._on_complete = callback

    def submit(
        self,
        name: str,
        prompt: str,
        session_factory: Callable[[BackgroundTask], Any],
        backend_type: str = "",
        model: str = "",
        project_path: str = "",
        session_mode: str = "code",
        session_role: str = "generator",
        backend_spec: Optional[dict[str, Any]] = None,
    ) -> BackgroundTask:
        """Submit a new background task. Returns the task object."""
        task = BackgroundTask(
            id=uuid.uuid4().hex[:12],
            name=name or prompt[:50],
            prompt=prompt,
            backend_type=backend_type,
            model=model,
            project_path=project_path,
            session_mode=session_mode,
            session_role=session_role,
            backend_spec=backend_spec or {},
            session_factory=session_factory,
        )

        with self._lock:
            self._tasks[task.id] = task

        self._pool.submit(self._run_task, task)
        return task

    def _run_task(self, task: BackgroundTask):
        """Execute a task in a worker thread."""
        if task.cancel_event.is_set():
            task.status = TaskStatus.CANCELLED
            task.completed_at = datetime.now().isoformat()
            return

        task.status = TaskStatus.RUNNING
        task.started_at = datetime.now().isoformat()
        start = time.time()

        try:
            if not task.session_factory:
                raise ValueError("No session factory configured for background task")

            session = task.session_factory(task)
            task.session = session

            collected_text = []
            steps = 0

            for event in session.run(task.prompt):
                event_type = event.get("event", "")
                task.display_events.append(event)

                if event_type == "text.done":
                    text = event.get("text", "")
                    if text:
                        collected_text.append(text)
                elif event_type == "step.end":
                    steps += 1
                elif event_type == "error":
                    message = event.get("message", "Unknown error")
                    if task.cancel_event.is_set() or message == "Interrupted":
                        task.status = TaskStatus.CANCELLED
                    else:
                        task.error = message

                if task.cancel_event.is_set():
                    session.cancel()

            task.result = "\n\n".join(collected_text) if collected_text else "(no output)"
            task.steps = steps
            task.elapsed = time.time() - start

            if task.cancel_event.is_set():
                task.status = TaskStatus.CANCELLED
                if not task.result or task.result == "(no output)":
                    task.result = "Cancelled"
            elif task.status != TaskStatus.CANCELLED and task.error:
                task.status = TaskStatus.FAILED
            else:
                task.status = TaskStatus.COMPLETED

        except Exception as exc:
            if task.cancel_event.is_set():
                task.status = TaskStatus.CANCELLED
                task.error = ""
            else:
                task.status = TaskStatus.FAILED
                task.error = str(exc)
                logger.error(f"Task {task.id} failed: {exc}")
        finally:
            task.completed_at = datetime.now().isoformat()
            task.session = None
            self._persist_task(task)

            if self._on_complete:
                try:
                    self._on_complete(task)
                except Exception as exc:
                    logger.error(f"Task completion callback error: {exc}")

    def get_task(self, task_id: str) -> Optional[BackgroundTask]:
        with self._lock:
            return self._tasks.get(task_id)

    def list_tasks(self, limit: int = 50) -> list[dict]:
        with self._lock:
            tasks = sorted(
                self._tasks.values(),
                key=lambda task: task.created_at,
                reverse=True,
            )[:limit]
            return [task.to_dict() for task in tasks]

    def cancel(self, task_id: str) -> bool:
        with self._lock:
            task = self._tasks.get(task_id)
            if not task or task.status not in (TaskStatus.PENDING, TaskStatus.RUNNING):
                return False
            task.cancel_event.set()
            if task.session:
                task.session.cancel()
            if task.status == TaskStatus.PENDING:
                task.status = TaskStatus.CANCELLED
                task.completed_at = datetime.now().isoformat()
                self._persist_task(task)
            return True

    def _persist_task(self, task: BackgroundTask):
        """Save task to disk for history."""
        try:
            self._persist_dir.mkdir(parents=True, exist_ok=True)
            path = self._persist_dir / f"{task.id}.json"
            path.write_text(
                json.dumps(task.to_full_dict(), indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
        except OSError as exc:
            logger.error(f"Failed to persist task: {exc}")

    def _load_persisted(self):
        """Load completed tasks from disk."""
        if not self._persist_dir.exists():
            return
        try:
            for path in sorted(self._persist_dir.glob("*.json"), reverse=True)[:50]:
                data = json.loads(path.read_text(encoding="utf-8"))
                task = BackgroundTask(
                    id=data["id"],
                    name=data.get("name", ""),
                    prompt=data.get("prompt", ""),
                    backend_type=data.get("backend_type", ""),
                    model=data.get("model", ""),
                    project_path=data.get("project_path", ""),
                    session_mode=data.get("session_mode", "code"),
                    session_role=data.get("session_role", "generator"),
                    backend_spec=data.get("backend_spec", {}),
                    status=TaskStatus(data.get("status", "completed")),
                    created_at=data.get("created_at", ""),
                    started_at=data.get("started_at", ""),
                    completed_at=data.get("completed_at", ""),
                    result=data.get("result", ""),
                    error=data.get("error", ""),
                    display_events=data.get("display_events", []),
                    steps=data.get("steps", 0),
                    elapsed=data.get("elapsed", 0.0),
                )
                with self._lock:
                    if task.id not in self._tasks:
                        self._tasks[task.id] = task
        except Exception as exc:
            logger.warning(f"Failed to load persisted tasks: {exc}")
