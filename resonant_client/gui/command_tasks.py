"""
Command Center Task Board — persistent task management for coordinating agents.
"""

from __future__ import annotations

import json
import logging
import threading
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class CommandTask:
    """A task on the command center task board."""

    id: str = ""
    title: str = ""
    description: str = ""
    status: str = "todo"  # todo | assigned | running | completed | failed
    priority: str = "medium"  # low | medium | high
    assigned_agent_id: str = ""
    created_at: str = ""
    updated_at: str = ""
    completed_at: str = ""
    result_summary: str = ""
    tags: list[str] = field(default_factory=list)

    def __post_init__(self):
        if not self.id:
            self.id = uuid.uuid4().hex[:12]
        if not self.created_at:
            self.created_at = datetime.now().isoformat()
        if not self.updated_at:
            self.updated_at = self.created_at

    def to_dict(self) -> dict:
        return asdict(self)


class CommandTaskStore:
    """CRUD store for command tasks with file persistence."""

    def __init__(self, persist_dir: str | Path | None = None):
        self._tasks: dict[str, CommandTask] = {}
        self._lock = threading.Lock()
        self._persist_dir = Path(persist_dir) if persist_dir else Path.home() / ".resonant" / "command_tasks"
        self._persist_dir.mkdir(parents=True, exist_ok=True)
        self._load()

    def _load(self):
        """Load tasks from disk."""
        for fp in self._persist_dir.glob("*.json"):
            try:
                data = json.loads(fp.read_text(encoding="utf-8"))
                task = CommandTask(**{k: v for k, v in data.items() if k in CommandTask.__dataclass_fields__})
                self._tasks[task.id] = task
            except Exception as exc:
                logger.warning("Failed to load command task %s: %s", fp.name, exc)

    def _persist(self, task: CommandTask):
        """Save a single task to disk."""
        try:
            fp = self._persist_dir / f"{task.id}.json"
            fp.write_text(json.dumps(task.to_dict(), indent=2), encoding="utf-8")
        except Exception as exc:
            logger.warning("Failed to persist command task %s: %s", task.id, exc)

    def _remove_file(self, task_id: str):
        """Remove task file from disk."""
        try:
            fp = self._persist_dir / f"{task_id}.json"
            if fp.exists():
                fp.unlink()
        except Exception as exc:
            logger.warning("Failed to remove command task file %s: %s", task_id, exc)

    def list_tasks(self) -> list[dict]:
        """Return all tasks sorted by priority then creation time."""
        priority_order = {"high": 0, "medium": 1, "low": 2}
        with self._lock:
            tasks = sorted(
                self._tasks.values(),
                key=lambda t: (priority_order.get(t.priority, 9), t.created_at),
            )
            return [t.to_dict() for t in tasks]

    def create_task(
        self,
        title: str,
        description: str = "",
        priority: str = "medium",
        tags: Optional[list[str]] = None,
    ) -> CommandTask:
        """Create a new task."""
        task = CommandTask(
            title=title,
            description=description,
            priority=priority,
            tags=tags or [],
        )
        with self._lock:
            self._tasks[task.id] = task
        self._persist(task)
        return task

    def update_task(self, task_id: str, **kwargs) -> Optional[CommandTask]:
        """Update task fields. Returns updated task or None."""
        with self._lock:
            task = self._tasks.get(task_id)
            if not task:
                return None
            for key, value in kwargs.items():
                if hasattr(task, key) and key not in ("id", "created_at"):
                    setattr(task, key, value)
            task.updated_at = datetime.now().isoformat()
            if task.status == "completed" and not task.completed_at:
                task.completed_at = datetime.now().isoformat()
        self._persist(task)
        return task

    def delete_task(self, task_id: str) -> bool:
        """Delete a task. Returns True if found."""
        with self._lock:
            task = self._tasks.pop(task_id, None)
        if task:
            self._remove_file(task_id)
            return True
        return False

    def get_task(self, task_id: str) -> Optional[CommandTask]:
        """Get a task by ID."""
        with self._lock:
            return self._tasks.get(task_id)
