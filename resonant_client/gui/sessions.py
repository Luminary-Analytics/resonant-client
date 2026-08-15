"""
Resonant Client GUI — Session & Project Manager

Manages persistent agentic-coding sessions organized by project folder.
Sessions are stored as JSON files under ~/.resonant/projects/<hash>/sessions/.
"""

import hashlib
import json
import logging
import os
import re
import threading
import time
import uuid
from collections import OrderedDict
from pathlib import Path
from typing import Optional

from .session_ledger import SESSION_LEDGER_VERSION, SessionEventLedger

logger = logging.getLogger(__name__)

# Sidebar refreshes are frequent and a project can accumulate hundreds of
# session files.  Keep parsed metadata in-process, keyed by the file identity
# that matters for freshness.  Session histories remain on disk; this cache is
# deliberately small and stores only the lightweight summary dictionaries.
_SUMMARY_CACHE_MAX = 2048
_summary_cache: OrderedDict[str, tuple[int, int, Optional[dict]]] = OrderedDict()
_summary_cache_lock = threading.Lock()

_SUMMARY_STRING_PATTERNS = {
    key: re.compile(rf'"{key}"\s*:\s*"([^"\\]*(?:\\.[^"\\]*)*)"')
    for key in (
        "id", "title", "backend_type", "model", "session_role", "orchestration_mode",
        "director_run_id",
    )
}
_SUMMARY_NUMBER_PATTERNS = {
    key: re.compile(rf'"{key}"\s*:\s*([\d.]+)')
    for key in ("created_at", "updated_at", "message_count")
}
_PINNED_PATTERN = re.compile(r'"pinned"\s*:\s*(true|false)')
_MISSION_STATE_PATTERN = re.compile(
    r'"mission_state"\s*:\s*(\{[^{}]*\})', re.DOTALL
)
_MISSION_PHASE_PATTERN = re.compile(r'"phase"\s*:\s*"([^"\\]*(?:\\.[^"\\]*)*)"')
_MISSION_SEED_PATTERN = re.compile(
    r'"seed_feature"\s*:\s*"([^"\\]*(?:\\.[^"\\]*)*)"'
)

PLAYGROUND_PROJECT_NAME = "Playground"
_SESSION_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


def is_valid_session_id(value: str) -> bool:
    """Whether a session id is safe to use as one storage filename stem."""
    return bool(_SESSION_ID_PATTERN.fullmatch(str(value or "")))

def _resonant_dir() -> Path:
    """Resolve ~/.resonant at call time, never at import time.

    Tests monkeypatch Path.home() after this module is already
    imported; an import-time constant kept pointing at the real user
    home, so every test run that touched set_project() prepended
    pytest tmp paths to the user's real ~/.resonant/recent_projects.json.
    """
    return Path.home() / ".resonant"


def _projects_dir() -> Path:
    return _resonant_dir() / "projects"


def _is_pytest_temp_path(path: str) -> bool:
    """True for pytest tmp-dir paths (…/Temp/pytest-of-<user>/pytest-NNN/…).

    These are junk left in recent_projects.json by test runs that
    weren't isolated from the real user home: never write them, and
    scrub any that already made it in.
    """
    normalized = (path or "").replace("\\", "/").lower()
    return "pytest-of-" in normalized or "/temp/pytest-" in normalized


# v0.3.3 — directories where the bundled exe must never treat the
# current working directory as a "project." When the Start Menu
# shortcut launches resonant.exe, Windows sets cwd to the install
# location; ProjectManager() used to take that as the project path,
# producing permission-denied storms when the agent tried to write to
# `C:\Program Files\Resonant Client`. We detect the install/system
# locations and fall back to a writable workspace instead.
_UNSAFE_CWD_PREFIXES_WIN = (
    "c:\\program files",
    "c:\\program files (x86)",
    "c:\\windows",
    "c:\\programdata",
)
_UNSAFE_CWD_PREFIXES_POSIX = (
    "/applications/",
    "/usr/",
    "/bin/",
    "/sbin/",
    "/opt/",
    "/system/",
)


def _is_unsafe_cwd(path: str) -> bool:
    """True if `path` looks like an OS / app-install location that the
    user clearly didn't pick as a project. Case-insensitive on Windows.
    """
    if not path:
        return True
    norm = os.path.normpath(path)
    if os.name == "nt":
        norm = norm.lower()
        return any(norm.startswith(prefix) for prefix in _UNSAFE_CWD_PREFIXES_WIN)
    return any(norm.startswith(prefix) for prefix in _UNSAFE_CWD_PREFIXES_POSIX)


def _looks_like_resonant_source(path: str) -> bool:
    """True when a source/dev launch cwd points at Resonant itself.

    The desktop app's project should be the user's workspace, not the
    application repo just because the dev server was launched from there.
    Users can still open this folder explicitly via the project picker.
    """
    try:
        p = Path(path)
        return (
            (p / "resonant_client" / "gui" / "app.py").is_file()
            and (p / "pyproject.toml").is_file()
        )
    except OSError:
        return False


def _read_recent_project_entries() -> list[dict]:
    recents_file = _resonant_dir() / "recent_projects.json"
    try:
        if recents_file.exists():
            with open(recents_file, "r", encoding="utf-8-sig") as f:
                raw = json.load(f) or []
                return raw if isinstance(raw, list) else []
    except Exception:
        pass
    return []


def _playground_project_path() -> str:
    """Return the permanent app-owned Playground project path.

    Prefer a real folder named Playground next to the user's current or
    recent projects; this preserves dogfooding setups like D:/Repos/Playground.
    Fall back to the existing safe Documents workspace for fresh installs.
    """
    override = os.environ.get("RESONANT_PLAYGROUND_PROJECT", "").strip()
    recent_entries = _read_recent_project_entries()

    source_paths: list[str] = []
    if override:
        source_paths.append(os.path.expandvars(os.path.expanduser(override)))
    cwd = os.getcwd()
    if cwd and not _is_unsafe_cwd(cwd):
        source_paths.append(cwd)
    for entry in recent_entries:
        if not isinstance(entry, dict):
            continue
        path = (entry.get("path") or "").strip()
        if path:
            source_paths.append(path)

    def usable(path: str | Path) -> bool:
        raw = os.path.normpath(str(path))
        return (
            bool(raw)
            and not _is_pytest_temp_path(raw)
            and not _is_unsafe_cwd(raw)
            and os.path.isdir(raw)
        )

    for raw in source_paths:
        candidate = Path(raw)
        if candidate.name.lower() == PLAYGROUND_PROJECT_NAME.lower() and usable(candidate):
            return os.path.normpath(str(candidate))

    for raw in source_paths:
        candidate = Path(raw).parent / PLAYGROUND_PROJECT_NAME
        if usable(candidate):
            return os.path.normpath(str(candidate))

    docs = Path.home() / "Documents" / "Resonant Projects"
    try:
        docs.mkdir(parents=True, exist_ok=True)
        return str(docs)
    except OSError:
        pass

    workspace = _resonant_dir() / "workspace"
    try:
        workspace.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    return str(workspace)


def _safe_default_project_path() -> str:
    """Pick a sensible project path for cold launches where the user
    hasn't explicitly chosen one yet.

    Resolution order:
      1. Most-recent project from `~/.resonant/recent_projects.json`
         (filtered to existing dirs).
      2. `~/Documents/Resonant Projects` — created if missing.
      3. `~/.resonant/workspace` — last-resort fallback inside our own
         data dir, always writable.

    NEVER returns `os.getcwd()` when cwd is an OS/install location —
    that was the Bug #25 footgun (writes to `C:\\Program Files\\...`
    silently failing). If cwd is a normal user-writable dir, it still
    wins over (2)/(3) so the existing "launch from terminal in repo
    root" workflow keeps working.
    """
    # Honor cwd when it's user-writable (preserves dev workflow). The
    # Resonant source repo is excluded HERE only — a dev-server launch
    # from the repo shouldn't make Resonant its own project, but a repo
    # the user explicitly opened (and that recents remembers) must still
    # restore on the next launch.
    cwd = os.getcwd()
    cwd_is_resonant_source = _looks_like_resonant_source(cwd)
    if not _is_unsafe_cwd(cwd) and not cwd_is_resonant_source:
        try:
            test_path = os.path.join(cwd, ".resonant_write_probe")
            with open(test_path, "w", encoding="utf-8"):
                pass
            os.unlink(test_path)
            return cwd
        except OSError:
            pass

    # Most-recent project (best signal for repeat users).
    recents_file = _resonant_dir() / "recent_projects.json"
    try:
        if recents_file.exists():
            with open(recents_file, "r", encoding="utf-8-sig") as f:
                raw = json.load(f) or []
            for entry in raw:
                if not isinstance(entry, dict):
                    continue
                path = (entry.get("path") or "").strip()
                if not path:
                    continue
                # Same pytest-fixture filter as get_recent_projects —
                # tests that call set_project leave temp dirs in the live
                # recents file, and adopting one strands sessions under a
                # project hash pytest deletes a few runs later.
                if _is_pytest_temp_path(path):
                    continue
                # When cwd was vetoed as the Resonant source repo (a dev
                # launch from the checkout), don't let the recents loop
                # hand the same checkout straight back. Normal desktop
                # launches (cwd = install dir) restore it fine.
                if cwd_is_resonant_source and _looks_like_resonant_source(path):
                    continue
                if os.path.isdir(path) and not _is_unsafe_cwd(path):
                    return path
    except Exception:
        pass

    # Fresh user — try ~/Documents/Resonant Projects.
    docs = Path.home() / "Documents" / "Resonant Projects"
    try:
        docs.mkdir(parents=True, exist_ok=True)
        return str(docs)
    except OSError:
        pass

    # Last resort — always writable since it's our own data dir.
    workspace = _resonant_dir() / "workspace"
    try:
        workspace.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    return str(workspace)


def _project_hash(project_path: str) -> str:
    """Create a short hash for a project path."""
    normalized = os.path.normpath(project_path).replace("\\", "/").lower()
    return hashlib.sha256(normalized.encode()).hexdigest()[:12]


def _project_dir(project_path: str) -> Path:
    """Get the storage directory for a project."""
    h = _project_hash(project_path)
    return _projects_dir() / h


def _sessions_dir(project_path: str) -> Path:
    """Get the sessions directory for a project."""
    return _project_dir(project_path) / "sessions"


def _decode_json_string(value: str) -> str:
    """Decode the contents of a JSON string captured without its quotes."""
    try:
        return json.loads(f'"{value}"')
    except (TypeError, ValueError, json.JSONDecodeError):
        return value


def _copy_summary(summary: Optional[dict]) -> Optional[dict]:
    """Return a caller-safe copy without paying for a general deep copy."""
    if summary is None:
        return None
    copied = summary.copy()
    if isinstance(copied.get("mission_state"), dict):
        copied["mission_state"] = copied["mission_state"].copy()
    return copied


def _invalidate_session_summary(filepath: Path) -> None:
    key = os.path.abspath(os.fspath(filepath))
    with _summary_cache_lock:
        _summary_cache.pop(key, None)


def _cache_session_summary(filepath: Path, summary: Optional[dict]) -> None:
    """Prime a freshly written session summary and bound cache growth."""
    try:
        stat = filepath.stat()
    except OSError:
        _invalidate_session_summary(filepath)
        return

    key = os.path.abspath(os.fspath(filepath))
    value = (stat.st_mtime_ns, stat.st_size, _copy_summary(summary))
    with _summary_cache_lock:
        _summary_cache[key] = value
        _summary_cache.move_to_end(key)
        while len(_summary_cache) > _SUMMARY_CACHE_MAX:
            _summary_cache.popitem(last=False)


def _parse_session_summary(filepath: Path) -> Optional[dict]:
    """Read only metadata fields from a session file without parsing full history.

    Legacy session files can be large because they embed conversation and
    display arrays. Ledger-backed files are small, but this prefix scan still
    avoids unnecessary JSON parsing during frequent sidebar refreshes.
    """
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            prefix = f.read(4096)

        summary = {}
        for key, pattern in _SUMMARY_STRING_PATTERNS.items():
            m = pattern.search(prefix)
            if m:
                summary[key] = _decode_json_string(m.group(1))
        for key, pattern in _SUMMARY_NUMBER_PATTERNS.items():
            m = pattern.search(prefix)
            if m:
                val = m.group(1)
                summary[key] = float(val) if "." in val else int(val)

        # Mission detection — sidebar needs to know whether to render this
        # session under "Sessions" or "Missions" and which phase badge to
        # show. We match a small subset of fields in the predictable
        # indent-2 JSON layout. If mission_state is present but malformed,
        # we fall through to the full-parse path so we never silently
        # mis-classify a mission as a regular session.
        m = _PINNED_PATTERN.search(prefix)
        if m:
            summary["pinned"] = m.group(1) == "true"

        if '"mission_state"' in prefix:
            ms_match = _MISSION_STATE_PATTERN.search(prefix)
            if ms_match:
                ms_block = ms_match.group(1)
                phase_m = _MISSION_PHASE_PATTERN.search(ms_block)
                seed_m = _MISSION_SEED_PATTERN.search(ms_block)
                if phase_m:
                    summary["mission_state"] = {
                        "phase": _decode_json_string(phase_m.group(1)),
                        "seed_feature": _decode_json_string(seed_m.group(1)) if seed_m else "",
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


def _read_session_summary(
    filepath: Path,
    *,
    file_stat: Optional[os.stat_result] = None,
) -> Optional[dict]:
    """Return cached session metadata when the file has not changed."""
    try:
        stat = file_stat or filepath.stat()
    except OSError:
        _invalidate_session_summary(filepath)
        return None

    key = os.path.abspath(os.fspath(filepath))
    with _summary_cache_lock:
        cached = _summary_cache.get(key)
        if cached and cached[:2] == (stat.st_mtime_ns, stat.st_size):
            _summary_cache.move_to_end(key)
            return _copy_summary(cached[2])

    summary = _parse_session_summary(filepath)
    with _summary_cache_lock:
        _summary_cache[key] = (stat.st_mtime_ns, stat.st_size, _copy_summary(summary))
        _summary_cache.move_to_end(key)
        while len(_summary_cache) > _SUMMARY_CACHE_MAX:
            _summary_cache.popitem(last=False)
    return _copy_summary(summary)


def _session_files_by_mtime(directory: Path) -> list[tuple[Path, os.stat_result]]:
    """Enumerate session files with one stat call each, newest first."""
    files: list[tuple[Path, os.stat_result]] = []
    try:
        candidates = directory.glob("*.json")
        for filepath in candidates:
            try:
                files.append((filepath, filepath.stat()))
            except OSError:
                # A session can be deleted concurrently with a sidebar refresh.
                continue
    except OSError:
        return []
    files.sort(key=lambda item: item[1].st_mtime_ns, reverse=True)
    return files


class SessionRecord:
    """Serializable session metadata backed by one append-only event ledger.

    ``conversation_history`` and ``display_events`` remain the in-memory views
    consumed by the existing engine and renderer. On disk they are projections
    of ``<session-id>.events.jsonl`` rather than parallel arrays in this file.
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
        pinned: bool = False,
        orchestration_mode: str = "single",
        director_config: Optional[dict] = None,
        director_run_id: str = "",
        event_log: str = "",
        session_format_version: int = SESSION_LEDGER_VERSION,
    ):
        self.id = session_id or str(uuid.uuid4())[:8]
        if not is_valid_session_id(self.id):
            raise ValueError(f"Invalid session id: {self.id!r}")
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
        #   v0.5.0a6 — Autonomous Mission phases:
        #   "autonomous_running"  — ∞ Build autonomously clicked,
        #                            AutonomousMissionDaemon iterating
        #   "autonomous_complete" — daemon ended with verdict=satisfied
        #   "autonomous_paused"   — daemon ended for any other reason
        #                            (user_stop / time_budget / blocked /
        #                            check_failed / stuck / misconfigured)
        # Optional fields:
        #   "spec_markdown"           — full spec block once emitted
        #   "refined_intent"          — extracted refined-intent paragraph
        #   "intent_id"               — UUID of the dispatched intent
        #                                (planning_dispatched / autonomous_*)
        #   "started_at"              — epoch float
        #   "autonomous_started_at"   — epoch float at autonomous dispatch
        self.mission_state: Optional[dict] = mission_state
        self.pinned: bool = bool(pinned)
        self.orchestration_mode = (
            "director" if orchestration_mode == "director" else "single"
        )
        self.director_config: dict = dict(director_config or {})
        self.director_run_id = str(director_run_id or "")
        self.session_format_version = int(session_format_version or SESSION_LEDGER_VERSION)
        raw_event_log = str(event_log or "")
        expected_event_log = f"{self.id}.events.jsonl"
        self.event_log = (
            raw_event_log if raw_event_log == expected_event_log else expected_event_log
        )

    @property
    def ledger(self) -> SessionEventLedger:
        return SessionEventLedger(_sessions_dir(self.project_path) / self.event_log)

    def load_ledger(self) -> None:
        """Refresh the two compatibility views from the authoritative ledger."""
        ledger = self.ledger
        if not ledger.path.exists():
            return
        records = ledger.read_records()
        self.conversation_history = ledger.project_conversation(records=records)
        self.display_events = ledger.project_display_events(records=records)

    def append_display_events(self, events: list[dict]) -> list[int]:
        """Durably append live GUI events and update the in-memory projection."""
        clean = [dict(event) for event in events if isinstance(event, dict)]
        if not clean:
            return []
        if not self.ledger.path.exists() and (self.conversation_history or self.display_events):
            self.ledger.seed(self.conversation_history, self.display_events)
        seqs = self.ledger.append_display(clean)
        self.display_events.extend(clean)
        return seqs

    def display_page(
        self, *, before_seq: int | None = None, limit: int = 240
    ) -> dict:
        """Return a serializable tail/older page from the display projection."""
        if not self.ledger.path.exists():
            self.ledger.seed(self.conversation_history, self.display_events)
        return self.ledger.display_page(before_seq=before_seq, limit=limit).to_dict()

    def projections(self) -> dict:
        """Return consistent whole-session read models at one ledger sequence."""
        if not self.ledger.path.exists():
            self.ledger.seed(self.conversation_history, self.display_events)
        return self.ledger.projections()

    def history_snapshot(
        self, *, before_seq: int | None = None, limit: int = 240
    ) -> dict:
        """Return a page and projections from one committed ledger snapshot."""
        if not self.ledger.path.exists():
            self.ledger.seed(self.conversation_history, self.display_events)
        return self.ledger.history_snapshot(before_seq=before_seq, limit=limit)

    def to_dict(self, *, include_histories: bool = True) -> dict:
        # Field order matters: small metadata fields go FIRST so the
        # 4KB fast-path summary scanner in `_read_session_summary` can
        # find them without touching the (potentially multi-MB)
        # conversation_history / display_events arrays. Active mission
        # sessions in particular need mission_state to be discoverable
        # by the sidebar without forcing a full file load on every
        # session list refresh.
        data = {
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
            "pinned": self.pinned,
            "orchestration_mode": self.orchestration_mode,
            "director_config": self.director_config,
            "director_run_id": self.director_run_id,
            "session_format_version": self.session_format_version,
            "event_log": self.event_log,
        }
        # Direct serialization remains compatible for callers and tests. The
        # on-disk metadata path opts out so histories live only in the ledger.
        if include_histories:
            data["conversation_history"] = self.conversation_history
            data["display_events"] = self.display_events
        return data

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
            "pinned": self.pinned,
            "orchestration_mode": self.orchestration_mode,
            "director_run_id": self.director_run_id,
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
            pinned=data.get("pinned", False),
            orchestration_mode=data.get("orchestration_mode", "single"),
            director_config=data.get("director_config") or {},
            director_run_id=data.get("director_run_id", ""),
            event_log=data.get("event_log", ""),
            session_format_version=data.get(
                "session_format_version", SESSION_LEDGER_VERSION
            ),
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
            self.ledger.seed(self.conversation_history, self.display_events)
            event_count = self.ledger.sync(
                self.conversation_history, self.display_events
            )
            payload = self.to_dict(include_histories=False)
            payload["event_count"] = event_count
            temporary = filepath.with_suffix(".json.tmp")
            with open(temporary, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2, ensure_ascii=False)
                f.flush()
                os.fsync(f.fileno())
            os.replace(temporary, filepath)
            _cache_session_summary(filepath, self.to_summary())
        except Exception as e:
            _invalidate_session_summary(filepath)
            logger.error(f"Failed to save session {self.id}: {e}")

    def delete(self):
        """Remove from disk."""
        filepath = _sessions_dir(self.project_path) / f"{self.id}.json"
        try:
            filepath.unlink(missing_ok=True)
            self.ledger.path.unlink(missing_ok=True)
            _invalidate_session_summary(filepath)
        except Exception as e:
            logger.error(f"Failed to delete session {self.id}: {e}")


class ProjectManager:
    """Manages sessions for a given project folder."""

    def __init__(self, project_path: str = ""):
        # v0.3.3 — never silently take os.getcwd() when cwd is an OS or
        # app-install location (Bug #25). _safe_default_project_path
        # falls back through recent-projects → ~/Documents/Resonant
        # Projects → ~/.resonant/workspace.
        self.project_path = project_path or _safe_default_project_path()
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
        self._save_recent_project_path(self.project_path)

    def register_project(self, project_path: str) -> str:
        """Track a project folder without switching the active project."""
        normalized = os.path.normpath(project_path)
        _sessions_dir(normalized).mkdir(parents=True, exist_ok=True)
        self._save_recent_project_path(normalized)
        return normalized

    def rename_project(self, project_path: str, name: str) -> str:
        """Give a tracked project a custom display name.

        Stored with `renamed: True` so `_save_recent_project_path` knows not to
        overwrite it with the folder basename the next time the project is
        opened.
        """
        label = (name or "").strip()
        if not label:
            raise ValueError("Project name cannot be empty.")
        normalized = os.path.normpath(project_path)
        target = normalized.replace("\\", "/").lower()

        # Renaming back to the folder's own name means "stop overriding", not
        # "pin this string forever" — otherwise renaming the folder on disk
        # would leave the sidebar showing the old label with no way back.
        is_default = label == (os.path.basename(normalized) or normalized)

        entries = self._read_recent_projects_raw()
        for entry in entries:
            if (entry.get("path") or "").replace("\\", "/").lower() == target:
                entry["name"] = label
                if is_default:
                    entry.pop("renamed", None)
                else:
                    entry["renamed"] = True
                break
        else:
            raise ValueError(f"Project is not in the sidebar: {normalized}")

        self._write_recent_projects_raw(entries)
        return label

    def forget_project(self, project_path: str) -> bool:
        """Remove a project from the sidebar list.

        Deliberately does NOT touch the folder or its sessions on disk — this
        only stops tracking it. Re-opening the folder brings it back with its
        sessions intact.

        Returns whether an entry was actually removed.
        """
        normalized = os.path.normpath(project_path)
        target = normalized.replace("\\", "/").lower()

        entries = self._read_recent_projects_raw()
        remaining = [
            entry for entry in entries
            if (entry.get("path") or "").replace("\\", "/").lower() != target
        ]
        if len(remaining) == len(entries):
            return False
        self._write_recent_projects_raw(remaining)
        return True

    def _read_recent_projects_raw(self) -> list:
        """Raw recents entries, unfiltered. Callers that display them should
        use `get_recent_projects`, which drops missing and pytest paths."""
        recents_file = _resonant_dir() / "recent_projects.json"
        try:
            if recents_file.exists():
                with open(recents_file, "r", encoding="utf-8-sig") as f:
                    entries = json.load(f)
                    if isinstance(entries, list):
                        return [e for e in entries if isinstance(e, dict)]
        except Exception:
            logger.debug("recent_projects.json unreadable", exc_info=True)
        return []

    def _write_recent_projects_raw(self, entries: list) -> None:
        recents_file = _resonant_dir() / "recent_projects.json"
        try:
            _resonant_dir().mkdir(parents=True, exist_ok=True)
            with open(recents_file, "w", encoding="utf-8") as f:
                json.dump(entries, f, indent=2)
        except Exception as e:
            logger.error(f"Failed to save recent projects: {e}")

    def _save_recent_project_path(self, project_path: str):
        """Track an arbitrary project path in the recent projects list."""
        normalized_project = os.path.normpath(project_path)
        recents_file = _resonant_dir() / "recent_projects.json"
        recents = []
        try:
            if recents_file.exists():
                with open(recents_file, "r", encoding="utf-8-sig") as f:
                    recents = json.load(f)
        except Exception:
            pass
        if not isinstance(recents, list):
            recents = []

        norm = normalized_project.replace("\\", "/")
        # A user-set display name has to survive this rewrite. Opening a
        # project rebuilds its entry, so without carrying `renamed` forward a
        # rename would silently revert the next time the project was selected.
        previous = next(
            (
                r for r in recents
                if isinstance(r, dict)
                and (r.get("path") or "").replace("\\", "/") == norm
            ),
            None,
        )
        # Scrub pytest tmp dirs on every write so files polluted by
        # pre-fix test runs self-heal; get_recent_projects applies the
        # same filter at read time.
        recents = [
            r for r in recents
            if isinstance(r, dict)
            and (r.get("path") or "").replace("\\", "/") != norm
            and not _is_pytest_temp_path(r.get("path") or "")
        ]
        if not _is_pytest_temp_path(normalized_project):
            entry = {
                "path": normalized_project,
                "name": os.path.basename(normalized_project) or normalized_project,
                "last_used": time.time(),
            }
            if previous and previous.get("renamed") and (previous.get("name") or "").strip():
                entry["name"] = previous["name"]
                entry["renamed"] = True
            recents.insert(0, entry)
        recents = recents[:20]

        try:
            _resonant_dir().mkdir(parents=True, exist_ok=True)
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
        recents_file = _resonant_dir() / "recent_projects.json"
        raw: list = []
        try:
            if recents_file.exists():
                with open(recents_file, "r", encoding="utf-8-sig") as f:
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
            if _is_pytest_temp_path(path):
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

    def get_playground_project(self) -> dict:
        path = _playground_project_path()
        return {
            "path": path,
            "name": PLAYGROUND_PROJECT_NAME,
            "permanent": True,
        }

    def clear_recent_projects(self) -> None:
        """Wipe the recent-projects history (keeps the current project)."""
        recents_file = _resonant_dir() / "recent_projects.json"
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

        for filepath, stat in _session_files_by_mtime(d):
            summary = _read_session_summary(filepath, file_stat=stat)
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
            for filepath, stat in _session_files_by_mtime(d):
                summary = _read_session_summary(filepath, file_stat=stat)
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
        orchestration_mode: str = "single",
        director_config: Optional[dict] = None,
    ) -> SessionRecord:
        """Create a new session for the current project."""
        record = SessionRecord(
            project_path=self.project_path,
            backend_type=backend_type,
            model=model,
            title="New session",
            session_role=session_role,
            orchestration_mode=orchestration_mode,
            director_config=director_config,
        )
        record.save()
        self.current_session = record
        return record

    def load_session(
        self, session_id: str, *, activate: bool = True, hydrate: bool = True
    ) -> Optional[SessionRecord]:
        """Load a session by ID."""
        if not is_valid_session_id(session_id):
            return None
        filepath = _sessions_dir(self.project_path) / f"{session_id}.json"
        if not filepath.exists():
            return None

        try:
            with open(filepath, "r", encoding="utf-8") as f:
                data = json.load(f)
            record = SessionRecord.from_dict(data)
            # The selected project owns this metadata file. Never let a stale
            # or hand-edited project_path redirect its ledger elsewhere.
            record.project_path = self.project_path
            if hydrate and record.ledger.path.exists():
                record.load_ledger()
            elif data.get("conversation_history") or data.get("display_events"):
                # One-time migration from the pre-ledger session format.
                record.save()
            if activate:
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
            director_run = getattr(engine_session, "director_run", None)
            if director_run is not None:
                self.current_session.orchestration_mode = "director"
                self.current_session.director_config = director_run.config.to_dict()
                self.current_session.director_run_id = director_run.id

        if display_events:
            # Streaming normally persisted these records already. Legacy and
            # test callers may still provide one complete end-of-turn batch.
            existing = self.current_session.display_events
            count = len(display_events)
            if count > len(existing) or existing[-count:] != display_events:
                self.current_session.append_display_events(display_events)

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
        if not is_valid_session_id(session_id):
            return
        filepath = _sessions_dir(self.project_path) / f"{session_id}.json"
        if filepath.exists():
            filepath.unlink()
        (_sessions_dir(self.project_path) / f"{session_id}.events.jsonl").unlink(
            missing_ok=True
        )
        _invalidate_session_summary(filepath)
        if self.current_session and self.current_session.id == session_id:
            self.current_session = None

    def fork_session(self, source_id: str, fork_at_user_index: int) -> Optional[SessionRecord]:
        """
        Create a new session that branches from `source_id` at the
        (fork_at_user_index)-th user message (0-indexed, inclusive).

        Slices conversation_history at the boundary right after the kept
        user message's response (i.e. just before the next user message),
        and slices display events at the same exact task boundary.

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

        # Display history follows the same exact user-task boundary.
        evts = source.display_events or []
        display_user_idxs = [
            i for i, event in enumerate(evts)
            if isinstance(event, dict) and event.get("event") == "user_message"
        ]
        if not display_user_idxs:
            sliced_events = list(evts) if cutoff >= len(history) else []
        elif fork_at_user_index < 0:
            sliced_events = []
        elif fork_at_user_index >= len(display_user_idxs) - 1:
            sliced_events = list(evts)
        else:
            sliced_events = evts[: display_user_idxs[fork_at_user_index + 1]]

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
            orchestration_mode=source.orchestration_mode,
            director_config=source.director_config,
            # A fork receives the configuration, but starts a new durable run
            # so its task graph cannot mutate the source session's history.
            director_run_id="",
        )
        new_record.save()
        self.current_session = new_record
        return new_record
