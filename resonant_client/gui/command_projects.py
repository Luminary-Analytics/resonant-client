"""
Command Center Project Store — manages orchestrated projects with coordinator agents.
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
class CommandProject:
    """A project managed through the Command Center."""

    id: str = ""
    name: str = ""
    path: str = ""
    strategy: str = ""
    coordinator_task_id: str = ""
    status: str = "idle"  # idle | planning | running | completed | failed
    tasks: list = field(default_factory=list)  # coordinator-generated task breakdown
    agents: list = field(default_factory=list)  # {id, name, role, status, ...}
    activity: list = field(default_factory=list)  # feed messages
    created_at: str = ""
    updated_at: str = ""

    def __post_init__(self):
        if not self.id:
            self.id = uuid.uuid4().hex[:12]
        if not self.created_at:
            self.created_at = datetime.now().isoformat()
        if not self.updated_at:
            self.updated_at = self.created_at

    def to_dict(self) -> dict:
        return asdict(self)

    def add_activity(self, sender_type: str, sender_name: str, content: str, sender_id: str = ""):
        """Add an activity message to the project feed."""
        self.activity.append({
            "id": uuid.uuid4().hex[:8],
            "timestamp": datetime.now().isoformat(),
            "sender_type": sender_type,
            "sender_id": sender_id or sender_type,
            "sender_name": sender_name,
            "content": content,
        })
        # Keep last 200 messages
        if len(self.activity) > 200:
            self.activity = self.activity[-200:]
        self.updated_at = datetime.now().isoformat()


class CommandProjectStore:
    """CRUD store for command projects with file persistence."""

    def __init__(self, persist_dir: str | Path | None = None):
        self._projects: dict[str, CommandProject] = {}
        self._lock = threading.Lock()
        self._persist_dir = Path(persist_dir) if persist_dir else Path.home() / ".resonant" / "command_projects"
        self._persist_dir.mkdir(parents=True, exist_ok=True)
        self._load()

    def _load(self):
        for fp in self._persist_dir.glob("*.json"):
            try:
                data = json.loads(fp.read_text(encoding="utf-8"))
                proj = CommandProject(**{k: v for k, v in data.items() if k in CommandProject.__dataclass_fields__})
                self._projects[proj.id] = proj
            except Exception as exc:
                logger.warning("Failed to load command project %s: %s", fp.name, exc)

    def _persist(self, project: CommandProject):
        try:
            fp = self._persist_dir / f"{project.id}.json"
            fp.write_text(json.dumps(project.to_dict(), indent=2, default=str), encoding="utf-8")
        except Exception as exc:
            logger.warning("Failed to persist command project %s: %s", project.id, exc)

    def list_projects(self) -> list[dict]:
        with self._lock:
            projects = sorted(self._projects.values(), key=lambda p: p.updated_at, reverse=True)
            return [p.to_dict() for p in projects]

    def create_project(self, name: str, path: str, strategy: str) -> CommandProject:
        proj = CommandProject(name=name, path=path, strategy=strategy)
        with self._lock:
            self._projects[proj.id] = proj
        self._persist(proj)
        return proj

    def get_project(self, project_id: str) -> Optional[CommandProject]:
        with self._lock:
            return self._projects.get(project_id)

    def update_project(self, project_id: str, **kwargs) -> Optional[CommandProject]:
        with self._lock:
            proj = self._projects.get(project_id)
            if not proj:
                return None
            for key, value in kwargs.items():
                if hasattr(proj, key) and key not in ("id", "created_at"):
                    setattr(proj, key, value)
            proj.updated_at = datetime.now().isoformat()
        self._persist(proj)
        return proj

    def add_activity(self, project_id: str, sender_type: str, sender_name: str, content: str, sender_id: str = ""):
        with self._lock:
            proj = self._projects.get(project_id)
            if proj:
                proj.add_activity(sender_type, sender_name, content, sender_id)
        if proj:
            self._persist(proj)

    def update_task(self, project_id: str, task_id: str, **kwargs):
        """Update a specific task within a project."""
        with self._lock:
            proj = self._projects.get(project_id)
            if not proj:
                return
            for task in proj.tasks:
                if task.get("id") == task_id:
                    task.update(kwargs)
                    proj.updated_at = datetime.now().isoformat()
                    break
        if proj:
            self._persist(proj)

    def add_agent(self, project_id: str, agent: dict):
        """Add an agent to a project's agent list."""
        with self._lock:
            proj = self._projects.get(project_id)
            if proj:
                proj.agents.append(agent)
                proj.updated_at = datetime.now().isoformat()
        if proj:
            self._persist(proj)
