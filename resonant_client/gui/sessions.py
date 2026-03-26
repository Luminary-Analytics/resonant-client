"""
Resonant Client GUI — Session & Project Manager

Manages persistent sessions organized by project folder.
Sessions are stored as JSON files under ~/.resonant/projects/<hash>/sessions/.
"""

import hashlib
import json
import logging
import os
import time
import uuid
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# Storage root
_RESONANT_DIR = Path.home() / ".resonant"
_PROJECTS_DIR = _RESONANT_DIR / "projects"


def _project_hash(project_path: str) -> str:
    """Create a short hash for a project path."""
    normalized = os.path.normpath(project_path).replace("\\", "/").lower()
    return hashlib.sha256(normalized.encode()).hexdigest()[:12]


def _project_dir(project_path: str) -> Path:
    """Get the storage directory for a project."""
    h = _project_hash(project_path)
    return _PROJECTS_DIR / h


def _sessions_dir(project_path: str) -> Path:
    """Get the sessions directory for a project."""
    return _project_dir(project_path) / "sessions"


def _read_session_summary(filepath: Path) -> Optional[dict]:
    """Read only metadata fields from a session file without parsing full history.

    Session files can be large (100KB+) due to conversation_history and
    display_events arrays.  This reads the first ~2KB to extract just the
    metadata fields needed for the sidebar, falling back to full parse only
    if the fast path fails.
    """
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            # Read a small prefix — metadata fields are at the top of the JSON
            prefix = f.read(4096)

        # Try to extract fields from the prefix using simple string scanning
        # JSON keys are at the top level: "id", "title", "backend_type", "model",
        # "created_at", "updated_at", "message_count"
        import re
        summary = {}
        for key in ("id", "title", "backend_type", "model", "session_mode", "session_role", "chat_group"):
            m = re.search(rf'"{key}"\s*:\s*"([^"]*)"', prefix)
            if m:
                summary[key] = m.group(1)
        for key in ("created_at", "updated_at", "message_count"):
            m = re.search(rf'"{key}"\s*:\s*([\d.]+)', prefix)
            if m:
                val = m.group(1)
                summary[key] = float(val) if "." in val else int(val)

        if "id" in summary and "updated_at" in summary:
            return summary
    except Exception:
        pass

    # Fallback: full parse
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            data = json.load(f)
        record = SessionRecord.from_dict(data)
        return record.to_summary()
    except Exception:
        return None


class SessionRecord:
    """A serializable session record (metadata + conversation history).

    Stores two parallel histories:
    - conversation_history: backend-formatted messages for engine context restore
    - display_events: EngineEvent dicts for UI replay when resuming a session
    """

    def __init__(
        self,
        session_id: str = "",
        title: str = "",
        project_path: str = "",
        backend_type: str = "",
        model: str = "",
        created_at: float = 0.0,
        updated_at: float = 0.0,
        conversation_history: list = None,
        display_events: list = None,
        message_count: int = 0,
        session_mode: str = "code",
        session_role: str = "generator",
        chat_group: str = "",
    ):
        self.id = session_id or str(uuid.uuid4())[:8]
        self.title = title
        self.project_path = project_path
        self.backend_type = backend_type
        self.model = model
        self.created_at = created_at or time.time()
        self.updated_at = updated_at or time.time()
        self.conversation_history = conversation_history or []
        self.display_events = display_events or []
        self.message_count = message_count
        self.session_mode = session_mode or "code"
        self.session_role = session_role or ("chat" if self.session_mode == "chat" else "generator")
        self.chat_group = chat_group

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "title": self.title,
            "project_path": self.project_path,
            "backend_type": self.backend_type,
            "model": self.model,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "conversation_history": self.conversation_history,
            "display_events": self.display_events,
            "message_count": self.message_count,
            "session_mode": self.session_mode,
            "session_role": self.session_role,
            "chat_group": self.chat_group,
        }

    def to_summary(self) -> dict:
        """Lightweight summary for the sidebar (no conversation history)."""
        return {
            "id": self.id,
            "title": self.title,
            "backend_type": self.backend_type,
            "model": self.model,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "message_count": self.message_count,
            "session_mode": self.session_mode,
            "session_role": self.session_role,
            "chat_group": self.chat_group,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "SessionRecord":
        return cls(
            session_id=data.get("id", ""),
            title=data.get("title", ""),
            project_path=data.get("project_path", ""),
            backend_type=data.get("backend_type", ""),
            model=data.get("model", ""),
            created_at=data.get("created_at", 0.0),
            updated_at=data.get("updated_at", 0.0),
            conversation_history=data.get("conversation_history", []),
            display_events=data.get("display_events", []),
            message_count=data.get("message_count", 0),
            session_mode=data.get("session_mode", "code"),
            session_role=data.get(
                "session_role",
                "chat" if data.get("session_mode", "code") == "chat" else "generator",
            ),
            chat_group=data.get("chat_group", ""),
        )

    def save(self):
        """Persist to disk."""
        d = _sessions_dir(self.project_path)
        d.mkdir(parents=True, exist_ok=True)
        filepath = d / f"{self.id}.json"
        try:
            with open(filepath, "w", encoding="utf-8") as f:
                json.dump(self.to_dict(), f, indent=2, ensure_ascii=False)
        except Exception as e:
            logger.error(f"Failed to save session {self.id}: {e}")

    def delete(self):
        """Remove from disk."""
        filepath = _sessions_dir(self.project_path) / f"{self.id}.json"
        try:
            filepath.unlink(missing_ok=True)
        except Exception as e:
            logger.error(f"Failed to delete session {self.id}: {e}")


class ProjectManager:
    """Manages sessions for a given project folder."""

    def __init__(self, project_path: str = ""):
        self.project_path = project_path or os.getcwd()
        self.current_session: Optional[SessionRecord] = None
        # Track recent projects
        self._ensure_storage()

    def _ensure_storage(self):
        _sessions_dir(self.project_path).mkdir(parents=True, exist_ok=True)

    def set_project(self, project_path: str):
        """Switch to a different project folder."""
        self.project_path = os.path.normpath(project_path)
        self.current_session = None
        self._ensure_storage()
        self._save_recent_project()

    def _save_recent_project(self):
        """Track this project in the recent projects list."""
        recents_file = _RESONANT_DIR / "recent_projects.json"
        recents = []
        try:
            if recents_file.exists():
                with open(recents_file, "r", encoding="utf-8") as f:
                    recents = json.load(f)
        except Exception:
            pass

        # Normalize
        norm = self.project_path.replace("\\", "/")

        # Remove if already exists, then prepend
        recents = [r for r in recents if r.get("path", "").replace("\\", "/") != norm]
        recents.insert(0, {
            "path": self.project_path,
            "name": os.path.basename(self.project_path),
            "last_used": time.time(),
        })

        # Keep last 20
        recents = recents[:20]

        try:
            _RESONANT_DIR.mkdir(parents=True, exist_ok=True)
            with open(recents_file, "w", encoding="utf-8") as f:
                json.dump(recents, f, indent=2)
        except Exception as e:
            logger.error(f"Failed to save recent projects: {e}")

    def get_recent_projects(self) -> list:
        """Get list of recent project paths."""
        recents_file = _RESONANT_DIR / "recent_projects.json"
        try:
            if recents_file.exists():
                with open(recents_file, "r", encoding="utf-8") as f:
                    return json.load(f)
        except Exception:
            pass
        return []

    def list_sessions(self) -> list[dict]:
        """List all sessions for the current project (summaries only)."""
        d = _sessions_dir(self.project_path)
        sessions = []
        if not d.exists():
            return sessions

        for filepath in sorted(d.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
            summary = _read_session_summary(filepath)
            if summary:
                sessions.append(summary)

        return sessions

    def list_all_sessions(self) -> list[dict]:
        """List sessions across ALL recent projects, sorted by time.

        Each summary includes a 'project_name' and 'project_path' field.
        """
        all_sessions = []
        seen_projects = set()

        # Current project first
        for s in self.list_sessions():
            s["project_name"] = os.path.basename(self.project_path)
            s["project_path"] = self.project_path
            all_sessions.append(s)
        seen_projects.add(os.path.normpath(self.project_path).replace("\\", "/").lower())

        # Then other recent projects
        for proj in self.get_recent_projects():
            path = proj.get("path", "") if isinstance(proj, dict) else str(proj)
            norm = os.path.normpath(path).replace("\\", "/").lower()
            if norm in seen_projects or not path:
                continue
            seen_projects.add(norm)

            d = _sessions_dir(path)
            if not d.exists():
                continue
            for filepath in sorted(d.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
                summary = _read_session_summary(filepath)
                if summary:
                    summary["project_name"] = os.path.basename(path)
                    summary["project_path"] = path
                    all_sessions.append(summary)

        # Sort all by updated_at descending
        all_sessions.sort(key=lambda s: s.get("updated_at", 0), reverse=True)
        return all_sessions

    def create_session(
        self,
        backend_type: str = "",
        model: str = "",
        session_mode: str = "code",
        session_role: str = "generator",
    ) -> SessionRecord:
        """Create a new session for the current project."""
        record = SessionRecord(
            project_path=self.project_path,
            backend_type=backend_type,
            model=model,
            title="New session",
            session_mode=session_mode,
            session_role=session_role,
        )
        record.save()
        self.current_session = record
        return record

    def load_session(self, session_id: str) -> Optional[SessionRecord]:
        """Load a session by ID."""
        filepath = _sessions_dir(self.project_path) / f"{session_id}.json"
        if not filepath.exists():
            return None

        try:
            with open(filepath, "r", encoding="utf-8") as f:
                data = json.load(f)
            record = SessionRecord.from_dict(data)
            self.current_session = record
            return record
        except Exception as e:
            logger.error(f"Failed to load session {session_id}: {e}")
            return None

    def save_current_session(self, engine_session=None, display_events=None):
        """Save the current session's state."""
        if not self.current_session:
            return

        self.current_session.updated_at = time.time()

        # Sync conversation history from the engine session
        if engine_session and hasattr(engine_session, "conversation_history"):
            self.current_session.conversation_history = engine_session.conversation_history
            # Count user messages
            self.current_session.message_count = sum(
                1 for m in engine_session.conversation_history
                if m.get("role") == "user"
            )

        # Append display events for UI replay
        if display_events:
            self.current_session.display_events.extend(display_events)

        self.current_session.save()

    def update_session_title(self, first_message: str):
        """Auto-title the session from the first user message."""
        if self.current_session and self.current_session.title == "New session":
            # Use first 60 chars of first message as title
            title = first_message.strip()
            if len(title) > 60:
                title = title[:57] + "..."
            self.current_session.title = title

    def delete_session(self, session_id: str):
        """Delete a session by ID."""
        filepath = _sessions_dir(self.project_path) / f"{session_id}.json"
        if filepath.exists():
            filepath.unlink()
        if self.current_session and self.current_session.id == session_id:
            self.current_session = None

    # ── Chat Groups ────────────────────────────────────────────────

    _GROUPS_FILE = _RESONANT_DIR / "chat_groups.json"

    def list_chat_groups(self) -> list[str]:
        """Return ordered list of chat group names."""
        try:
            if self._GROUPS_FILE.exists():
                with open(self._GROUPS_FILE, "r", encoding="utf-8") as f:
                    groups = json.load(f)
                if isinstance(groups, list):
                    return groups
        except Exception:
            pass
        return []

    def _save_groups(self, groups: list[str]):
        self._GROUPS_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(self._GROUPS_FILE, "w", encoding="utf-8") as f:
            json.dump(groups, f, indent=2, ensure_ascii=False)

    def create_chat_group(self, name: str) -> list[str]:
        """Create a new chat group. Returns updated list."""
        groups = self.list_chat_groups()
        if name and name not in groups:
            groups.append(name)
            self._save_groups(groups)
        return groups

    def rename_chat_group(self, old_name: str, new_name: str) -> list[str]:
        """Rename a chat group and update all sessions referencing it."""
        groups = self.list_chat_groups()
        if old_name in groups and new_name:
            groups = [new_name if g == old_name else g for g in groups]
            self._save_groups(groups)
            # Update sessions across all projects
            self._update_group_in_sessions(old_name, new_name)
        return groups

    def delete_chat_group(self, name: str) -> list[str]:
        """Delete a chat group and ungroup its sessions."""
        groups = self.list_chat_groups()
        if name in groups:
            groups.remove(name)
            self._save_groups(groups)
            # Ungroup affected sessions
            self._update_group_in_sessions(name, "")
        return groups

    def set_session_group(self, session_id: str, group: str):
        """Assign a session to a chat group (or ungroup with empty string)."""
        # Search across all project session directories
        for proj in self.get_recent_projects():
            path = proj.get("path", "") if isinstance(proj, dict) else str(proj)
            if not path:
                continue
            filepath = _sessions_dir(path) / f"{session_id}.json"
            if filepath.exists():
                try:
                    with open(filepath, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    data["chat_group"] = group
                    with open(filepath, "w", encoding="utf-8") as f:
                        json.dump(data, f, indent=2, ensure_ascii=False)
                except Exception:
                    pass
                return
        # Also check current project
        filepath = _sessions_dir(self.project_path) / f"{session_id}.json"
        if filepath.exists():
            try:
                with open(filepath, "r", encoding="utf-8") as f:
                    data = json.load(f)
                data["chat_group"] = group
                with open(filepath, "w", encoding="utf-8") as f:
                    json.dump(data, f, indent=2, ensure_ascii=False)
            except Exception:
                pass

    def _update_group_in_sessions(self, old_group: str, new_group: str):
        """Update chat_group in all session files across all projects."""
        seen = set()
        paths = [self.project_path]
        for proj in self.get_recent_projects():
            p = proj.get("path", "") if isinstance(proj, dict) else str(proj)
            if p:
                paths.append(p)
        for path in paths:
            norm = os.path.normpath(path).replace("\\", "/").lower()
            if norm in seen:
                continue
            seen.add(norm)
            d = _sessions_dir(path)
            if not d.exists():
                continue
            for filepath in d.glob("*.json"):
                try:
                    with open(filepath, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    if data.get("chat_group") == old_group:
                        data["chat_group"] = new_group
                        with open(filepath, "w", encoding="utf-8") as f:
                            json.dump(data, f, indent=2, ensure_ascii=False)
                except Exception:
                    pass
