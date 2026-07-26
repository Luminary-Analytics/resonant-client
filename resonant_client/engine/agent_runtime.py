"""Persistent runtime registry for primary agents and delegated workers.

The registry is deliberately independent from the GUI and the model backend.
It is the durable control plane: a child agent can finish, fail, pause, or be
cancelled while its transcript and handoff remain inspectable after restart.
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable

from .artifacts import ArtifactStore, project_state_dir


class AgentStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    WAITING = "waiting"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    STUCK = "stuck"

    @property
    def terminal(self) -> bool:
        return self in {
            AgentStatus.COMPLETED,
            AgentStatus.FAILED,
            AgentStatus.CANCELLED,
            AgentStatus.STUCK,
        }


@dataclass(slots=True)
class AgentHandoff:
    outcome: str
    summary: str
    evidence: list[str] = field(default_factory=list)
    changed_files: list[str] = field(default_factory=list)
    validation: list[str] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)
    recommended_next_action: str = ""
    artifacts: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_context(self) -> str:
        lines = [f"Outcome: {self.outcome}", f"Summary: {self.summary}"]
        for label, values in (
            ("Evidence", self.evidence),
            ("Changed files", self.changed_files),
            ("Validation", self.validation),
            ("Blockers", self.blockers),
            ("Artifacts", self.artifacts),
        ):
            if values:
                lines.append(f"{label}:\n" + "\n".join(f"- {value}" for value in values))
        if self.recommended_next_action:
            lines.append(f"Recommended next action: {self.recommended_next_action}")
        return "\n".join(lines)


@dataclass(slots=True)
class AgentRecord:
    id: str
    project_path: str
    agent_type: str
    prompt: str
    status: str
    created_at: float
    updated_at: float
    parent_id: str = ""
    model: str = ""
    role: str = "subagent"
    workspace: str = ""
    policy: str = ""
    max_steps: int | None = None
    current_action: str = ""
    steps: int = 0
    token_usage: dict[str, int] = field(default_factory=dict)
    error: str = ""
    handoff: dict[str, Any] | None = None
    transcript_path: str = ""
    control: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AgentRecord":
        fields = cls.__dataclass_fields__
        return cls(**{key: value for key, value in data.items() if key in fields})


@dataclass
class _LiveControl:
    cancel_event: threading.Event = field(default_factory=threading.Event)
    pause_event: threading.Event = field(default_factory=threading.Event)
    steer: Callable[[str], None] | None = None


class AgentRegistry:
    """Thread-safe persistent registry and control surface for agent runs."""

    def __init__(
        self,
        project_path: str | Path,
        *,
        root: str | Path | None = None,
        on_event: Callable[[dict[str, Any]], None] | None = None,
        artifact_store: ArtifactStore | None = None,
    ):
        self.project_path = Path(project_path).expanduser().resolve()
        self.root = Path(root) if root else project_state_dir(self.project_path) / "agents"
        self.root.mkdir(parents=True, exist_ok=True)
        self._on_event = on_event or (lambda event: None)
        self.artifacts = artifact_store or ArtifactStore(self.project_path)
        self._lock = threading.RLock()
        self._records: dict[str, AgentRecord] = {}
        self._live: dict[str, _LiveControl] = {}
        self._load()

    def create(
        self,
        *,
        agent_type: str,
        prompt: str,
        parent_id: str = "",
        model: str = "",
        role: str = "subagent",
        workspace: str = "",
        policy: str = "",
        max_steps: int | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> AgentRecord:
        now = time.time()
        agent_id = f"agt_{uuid.uuid4().hex[:12]}"
        transcript_path = self.root / f"{agent_id}.jsonl"
        record = AgentRecord(
            id=agent_id,
            project_path=str(self.project_path),
            agent_type=agent_type,
            prompt=prompt,
            status=AgentStatus.QUEUED.value,
            created_at=now,
            updated_at=now,
            parent_id=parent_id,
            model=model,
            role=role,
            workspace=workspace or str(self.project_path),
            policy=policy,
            max_steps=max_steps,
            transcript_path=str(transcript_path),
            metadata=dict(metadata or {}),
        )
        with self._lock:
            self._records[agent_id] = record
            self._live[agent_id] = _LiveControl()
            self._save(record)
        self._emit("agent.created", record)
        return record

    def attach_control(
        self,
        agent_id: str,
        *,
        cancel_event: threading.Event | None = None,
        pause_event: threading.Event | None = None,
        steer: Callable[[str], None] | None = None,
    ) -> None:
        with self._lock:
            control = self._live.setdefault(agent_id, _LiveControl())
            if cancel_event is not None:
                control.cancel_event = cancel_event
            if pause_event is not None:
                control.pause_event = pause_event
            if steer is not None:
                control.steer = steer

    def transition(
        self,
        agent_id: str,
        status: AgentStatus | str,
        *,
        current_action: str | None = None,
        error: str | None = None,
    ) -> AgentRecord:
        value = status.value if isinstance(status, AgentStatus) else str(status)
        with self._lock:
            record = self._require(agent_id)
            record.status = value
            record.updated_at = time.time()
            if current_action is not None:
                record.current_action = current_action
            if error is not None:
                record.error = error
            self._save(record)
        self._emit("agent.updated", record)
        return record

    def append_event(self, agent_id: str, event: dict[str, Any]) -> None:
        enriched = {"timestamp": time.time(), "agent_id": agent_id, **event}
        with self._lock:
            record = self._require(agent_id)
            with Path(record.transcript_path).open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(enriched, ensure_ascii=False, default=str) + "\n")
            record.updated_at = enriched["timestamp"]
            event_name = str(event.get("event") or "")
            if event_name == "step.end":
                record.steps += 1
            if event_name in {"tool.call", "step.start", "status"}:
                record.current_action = str(
                    event.get("name") or event.get("phase") or event.get("message") or ""
                )[:240]
            usage = event.get("usage") or {}
            if isinstance(usage, dict):
                for key in ("input_tokens", "output_tokens", "total_tokens"):
                    if usage.get(key) is not None:
                        record.token_usage[key] = int(usage[key] or 0)
            self._save(record)

    def complete(self, agent_id: str, handoff: AgentHandoff) -> AgentRecord:
        with self._lock:
            record = self._require(agent_id)
            record.handoff = handoff.to_dict()
            record.status = AgentStatus.COMPLETED.value
            record.current_action = ""
            record.updated_at = time.time()
            self._save(record)
        self._emit("agent.completed", record)
        return record

    def fail(self, agent_id: str, error: str, *, stuck: bool = False) -> AgentRecord:
        return self.transition(
            agent_id,
            AgentStatus.STUCK if stuck else AgentStatus.FAILED,
            error=error,
            current_action="",
        )

    def request_cancel(self, agent_id: str) -> AgentRecord:
        with self._lock:
            record = self._require(agent_id)
            control = self._live.setdefault(agent_id, _LiveControl())
            control.cancel_event.set()
            record.control["cancel_requested"] = True
            self._save(record)
        if not AgentStatus(record.status).terminal:
            return self.transition(agent_id, AgentStatus.CANCELLED, current_action="")
        return record

    def request_pause(self, agent_id: str) -> AgentRecord:
        with self._lock:
            control = self._live.setdefault(agent_id, _LiveControl())
            control.pause_event.set()
            record = self._require(agent_id)
            record.control["pause_requested"] = True
            self._save(record)
        return self.transition(agent_id, AgentStatus.PAUSED)

    def resume(self, agent_id: str) -> AgentRecord:
        """Un-pause a live agent.

        This only clears a pause on a worker whose thread is still alive. It
        deliberately refuses terminal records: flipping a `stuck` agent to
        RUNNING would present a ghost worker that no thread backs — the exact
        state `_load()` exists to clean up after a restart — and the UI would
        then wait forever on a run that can never emit another event.

        Use `restart_assignment()` to re-dispatch interrupted work.
        """
        with self._lock:
            record = self._require(agent_id)
            if AgentStatus(record.status).terminal:
                raise ValueError(
                    f"Agent {agent_id} is {record.status} and has no live thread to resume. "
                    f"Re-dispatch it with restart_assignment() instead."
                )
            control = self._live.setdefault(agent_id, _LiveControl())
            control.pause_event.clear()
            record.control.pop("pause_requested", None)
            self._save(record)
        return self.transition(agent_id, AgentStatus.RUNNING)

    def restart_assignment(self, agent_id: str) -> dict[str, Any]:
        """Return everything needed to re-dispatch an interrupted agent.

        A worker's thread cannot survive a process restart, but its assignment
        can: the record already persists the prompt, agent type, workspace,
        policy, model, and step budget. This exposes that as a dispatch payload
        so an interrupted run can be re-issued rather than mourned.

        `completed_steps` and `transcript_path` are included so the caller can
        tell the re-dispatched worker what its predecessor already did — a
        restart should not silently redo finished work.

        Raises ValueError for agents that completed successfully; there is
        nothing to restart.
        """
        with self._lock:
            record = self._require(agent_id)
            if record.status == AgentStatus.COMPLETED.value:
                raise ValueError(f"Agent {agent_id} completed; there is nothing to restart.")
            return {
                "source_agent_id": record.id,
                "agent_type": record.agent_type,
                "prompt": record.prompt,
                "project_path": record.project_path,
                "workspace": record.workspace,
                "policy": record.policy,
                "model": record.model,
                "role": record.role,
                "parent_id": record.parent_id,
                "max_steps": record.max_steps,
                "completed_steps": record.steps,
                "transcript_path": record.transcript_path,
                "interrupted_reason": record.error,
                "metadata": dict(record.metadata),
            }

    def adopt_restart(self, agent_id: str) -> AgentRecord:
        """Create a fresh agent seeded from an interrupted one.

        A new record (and a new transcript) rather than reusing the old id, so
        the interrupted run stays inspectable as evidence instead of being
        overwritten by its own retry. The two are linked through
        `metadata.resumed_from`.
        """
        assignment = self.restart_assignment(agent_id)
        record = self.create(
            agent_type=assignment["agent_type"],
            prompt=assignment["prompt"],
            parent_id=assignment["parent_id"],
            model=assignment["model"],
            role=assignment["role"],
            workspace=assignment["workspace"],
            policy=assignment["policy"],
            max_steps=assignment["max_steps"],
            metadata={
                **assignment["metadata"],
                "resumed_from": assignment["source_agent_id"],
                "resumed_after_steps": assignment["completed_steps"],
            },
        )
        return record

    def steer(self, agent_id: str, message: str) -> AgentRecord:
        with self._lock:
            control = self._live.setdefault(agent_id, _LiveControl())
            record = self._require(agent_id)
            if control.steer is None:
                record.control.setdefault("queued_steering", []).append(message)
            else:
                control.steer(message)
            self._save(record)
        self._emit("agent.steered", record, message=message)
        return record

    def get(self, agent_id: str) -> AgentRecord | None:
        with self._lock:
            return self._records.get(agent_id)

    def list(self, *, parent_id: str | None = None) -> list[AgentRecord]:
        with self._lock:
            records = list(self._records.values())
        if parent_id is not None:
            records = [record for record in records if record.parent_id == parent_id]
        return sorted(records, key=lambda record: record.created_at, reverse=True)

    def transcript(self, agent_id: str) -> list[dict[str, Any]]:
        record = self._require(agent_id)
        path = Path(record.transcript_path)
        if not path.exists():
            return []
        events = []
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return events

    def _load(self) -> None:
        for path in self.root.glob("*.json"):
            try:
                record = AgentRecord.from_dict(json.loads(path.read_text(encoding="utf-8")))
            except (OSError, TypeError, ValueError, json.JSONDecodeError):
                continue
            # Live threads cannot survive a process restart. Preserve the
            # transcript, but make the interrupted state explicit instead of
            # presenting a permanently-running ghost worker in the UI.
            if record.status in {
                AgentStatus.QUEUED.value,
                AgentStatus.RUNNING.value,
                AgentStatus.WAITING.value,
                AgentStatus.PAUSED.value,
            }:
                record.status = AgentStatus.STUCK.value
                record.error = record.error or "Runtime restarted before this agent completed"
                record.current_action = ""
                record.updated_at = time.time()
            self._records[record.id] = record
            self._live[record.id] = _LiveControl()
            self._save(record)

    def _save(self, record: AgentRecord) -> None:
        path = self.root / f"{record.id}.json"
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(record.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)

    def _require(self, agent_id: str) -> AgentRecord:
        record = self._records.get(agent_id)
        if record is None:
            raise KeyError(f"Unknown agent: {agent_id}")
        return record

    def _emit(self, event: str, record: AgentRecord, **extra: Any) -> None:
        self._on_event({"event": event, "agent": record.to_dict(), **extra})
