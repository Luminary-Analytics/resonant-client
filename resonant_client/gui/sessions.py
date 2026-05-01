"""
Resonant Client GUI — Session & Project Manager

Manages persistent agentic-coding sessions organized by project folder.
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
            prefix = f.read(4096)

        import re
        summary = {}
        for key in ("id", "title", "backend_type", "model", "session_role"):
            m = re.search(rf'"{key}"\s*:\s*"([^"]*)"', prefix)
            if m:
                summary[key] = m.group(1)
        for key in ("created_at", "updated_at", "message_count"):
            m = re.search(rf'"{key}"\s*:\s*([\d.]+)', prefix)
            if m:
                val = m.group(1)
                summary[key] = float(val) if "." in val else int(val)

        # Mission detection — sidebar needs to know whether to render this
        # session under "Sessions" or "Missions" and which phase badge to
        # show. We match a small subset of fields in the predictable
        # indent-2 JSON layout. If mission_state is present but malformed,
        # we fall through to the full-parse path so we never silently
        # mis-classify a mission as a regular session.
        if '"mission_state"' in prefix:
            ms_match = re.search(r'"mission_state"\s*:\s*(\{[^{}]*\})', prefix, re.DOTALL)
            if ms_match:
                ms_block = ms_match.group(1)
                phase_m = re.search(r'"phase"\s*:\s*"([^"]+)"', ms_block)
                seed_m = re.search(r'"seed_feature"\s*:\s*"((?:\\"|[^"])*)"', ms_block)
                if phase_m:
                    summary["mission_state"] = {
                        "phase": phase_m.group(1),
                        "seed_feature": (seed_m.group(1) if seed_m else "").replace('\\"', '"'),
                    }
            elif '"mission_state": null' not in prefix:
                # Mission state is present but didn't fit our small-dict
                # regex. Fall through to full parse rather than guess.
                summary.pop("id", None)

        if "id" in summary and "updated_at" in summary:
            return summary
    except Exception:
        pass

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
        session_role: str = "generator",
        thinking_mode: str = "",
        mission_state: Optional[dict] = None,
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
        self.session_role = session_role or "generator"
        self.thinking_mode = thinking_mode or ""
        # Mission state — None for regular chat sessions. A dict with at
        # least {"phase": str, "seed_feature": str} when this session was
        # started in Mission mode. Phases:
        #   "drafting"            — grilling the user, no spec emitted yet
        #   "planning_dispatched" — Build Roadmap clicked, intent_service running
        #   "completed"           — orchestration finished, deliverables ready
        #   "exited"              — user clicked Exit Mission before completion
        # Optional fields:
        #   "spec_markdown"  — full spec block once emitted
        #   "refined_intent" — extracted refined-intent paragraph
        #   "intent_id"      — UUID of the dispatched intent (planning_dispatched+)
        #   "started_at"     — epoch float
        self.mission_state: Optional[dict] = mission_state

    def to_dict(self) -> dict:
        # Field order matters: small metadata fields go FIRST so the
        # 4KB fast-path summary scanner in `_read_session_summary` can
        # find them without touching the (potentially multi-MB)
        # conversation_history / display_events arrays. Active mission
        # sessions in particular need mission_state to be discoverable
        # by the sidebar without forcing a full file load on every
        # session list refresh.
        return {
            "id": self.id,
            "title": self.title,
            "project_path": self.project_path,
            "backend_type": self.backend_type,
            "model": self.model,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "message_count": self.message_count,
            "session_role": self.session_role,
            "thinking_mode": self.thinking_mode,
            "mission_state": self.mission_state,
            "conversation_history": self.conversation_history,
            "display_events": self.display_events,
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
            "session_role": self.session_role,
            "thinking_mode": self.thinking_mode,
            # Sidebar needs to know if this session is a Mission so it can
            # render it in the Missions group with the right phase badge.
            "mission_state": self.mission_state,
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
            session_role=data.get("session_role", "generator"),
            thinking_mode=data.get("thinking_mode", ""),
            mission_state=data.get("mission_state"),
        )

    # ── Mission helpers ────────────────────────────────────────────────
    @property
    def is_mission(self) -> bool:
        return bool(self.mission_state)

    @property
    def mission_phase(self) -> str:
        return (self.mission_state or {}).get("phase", "")

    def start_mission(self, seed_feature: str) -> None:
        """Mark this session as a Mission in the drafting phase."""
        self.mission_state = {
            "phase": "drafting",
            "seed_feature": (seed_feature or "").strip(),
            "started_at": time.time(),
        }

    def advance_mission_phase(self, phase: str, **fields) -> None:
        """Move the mission to a new phase, optionally setting additional
        state fields (e.g. spec_markdown, refined_intent, intent_id)."""
        if not self.mission_state:
            return
        self.mission_state["phase"] = phase
        for key, value in fields.items():
            self.mission_state[key] = value

    def exit_mission(self) -> None:
        """Mark the mission as exited (user-cancelled before completion)."""
        if self.mission_state:
            self.mission_state["phase"] = "exited"
            self.mission_state["exited_at"] = time.time()

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

        norm = self.project_path.replace("\\", "/")
        recents = [r for r in recents if r.get("path", "").replace("\\", "/") != norm]
        recents.insert(0, {
            "path": self.project_path,
            "name": os.path.basename(self.project_path),
            "last_used": time.time(),
        })
        recents = recents[:20]

        try:
            _RESONANT_DIR.mkdir(parents=True, exist_ok=True)
            with open(recents_file, "w", encoding="utf-8") as f:
                json.dump(recents, f, indent=2)
        except Exception as e:
            logger.error(f"Failed to save recent projects: {e}")

    def get_recent_projects(self, *, limit: int = 10) -> list:
        """Get list of recent project paths.

        Filters out:
        - pytest temp dirs (paths containing 'pytest-of-' or '\\Temp\\pytest-')
        - paths that no longer exist on disk
        - duplicates (case-insensitive normalized path)

        Capped at `limit` entries.
        """
        recents_file = _RESONANT_DIR / "recent_projects.json"
        raw: list = []
        try:
            if recents_file.exists():
                with open(recents_file, "r", encoding="utf-8") as f:
                    raw = json.load(f) or []
        except Exception:
            return []

        if not isinstance(raw, list):
            return []

        cleaned: list = []
        seen: set[str] = set()
        for entry in raw:
            if not isinstance(entry, dict):
                continue
            path = (entry.get("path") or "").strip()
            if not path:
                continue
            normalized = path.replace("\\", "/").lower()
            # Filter out pytest test fixture dirs
            if "pytest-of-" in normalized or "/temp/pytest-" in normalized:
                continue
            # Filter out paths that no longer exist
            if not os.path.isdir(path):
                continue
            # De-dup
            if normalized in seen:
                continue
            seen.add(normalized)
            cleaned.append(entry)
            if len(cleaned) >= limit:
                break
        return cleaned

    def clear_recent_projects(self) -> None:
        """Wipe the recent-projects history (keeps the current project)."""
        recents_file = _RESONANT_DIR / "recent_projects.json"
        try:
            recents_file.unlink(missing_ok=True)
        except Exception:
            pass

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

        for s in self.list_sessions():
            s["project_name"] = os.path.basename(self.project_path)
            s["project_path"] = self.project_path
            all_sessions.append(s)
        seen_projects.add(os.path.normpath(self.project_path).replace("\\", "/").lower())

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

        all_sessions.sort(key=lambda s: s.get("updated_at", 0), reverse=True)
        return all_sessions

    def create_session(
        self,
        backend_type: str = "",
        model: str = "",
        session_role: str = "generator",
    ) -> SessionRecord:
        """Create a new session for the current project."""
        record = SessionRecord(
            project_path=self.project_path,
            backend_type=backend_type,
            model=model,
            title="New session",
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

        if engine_session and hasattr(engine_session, "conversation_history"):
            self.current_session.conversation_history = engine_session.conversation_history
            self.current_session.message_count = sum(
                1 for m in engine_session.conversation_history
                if m.get("role") == "user"
            )

        if display_events:
            self.current_session.display_events.extend(display_events)

        self.current_session.save()

    def update_session_title(self, first_message: str):
        """Auto-title the session from the first user message."""
        if self.current_session and self.current_session.title == "New session":
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

    def fork_session(self, source_id: str, fork_at_user_index: int) -> Optional[SessionRecord]:
        """
        Create a new session that branches from `source_id` at the
        (fork_at_user_index)-th user message (0-indexed, inclusive).

        Slices conversation_history at the boundary right after the kept
        user message's response (i.e. just before the next user message),
        and slices display_events proportionally.

        Returns the new SessionRecord, or None if source not found.
        """
        source = self.load_session(source_id)
        if source is None:
            return None

        history = source.conversation_history or []
        # Find indices of user messages
        user_idxs = [i for i, m in enumerate(history) if isinstance(m, dict) and m.get("role") == "user"]
        if not user_idxs:
            cutoff = len(history)
        elif fork_at_user_index < 0:
            cutoff = 0
        elif fork_at_user_index >= len(user_idxs) - 1:
            # Past the last user msg → keep everything
            cutoff = len(history)
        else:
            # Cutoff = index of the (N+1)th user msg → drop it and everything after
            cutoff = user_idxs[fork_at_user_index + 1]

        sliced_history = history[:cutoff]

        # Slice display_events proportionally — best-effort. Replay tolerates incomplete tails.
        evts = source.display_events or []
        if cutoff >= len(history) or not history:
            sliced_events = list(evts)
        else:
            ratio = cutoff / max(1, len(history))
            sliced_events = evts[: max(1, int(round(len(evts) * ratio)))]

        title = source.title or "Untitled"
        if not title.startswith("Fork: "):
            title = f"Fork: {title}"
        msg_count = sum(1 for m in sliced_history if isinstance(m, dict) and m.get("role") == "user")

        new_record = SessionRecord(
            project_path=self.project_path,
            backend_type=source.backend_type,
            model=source.model,
            session_role=source.session_role,
            title=title,
            conversation_history=sliced_history,
            display_events=sliced_events,
            message_count=msg_count,
            thinking_mode=getattr(source, "thinking_mode", "") or "",
        )
        new_record.save()
        self.current_session = new_record
        return new_record
