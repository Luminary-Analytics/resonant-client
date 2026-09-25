"""Reproducible run manifests, traces, comparisons, and OTLP-style export."""

from __future__ import annotations

import hashlib
import json
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable

from .artifacts import project_state_dir


@dataclass(slots=True)
class RunManifest:
    run_id: str
    project_path: str
    created_at: float
    updated_at: float
    status: str = "initialized"
    backend: str = ""
    model: str = ""
    model_role: str = "primary"
    prompt_sha256: str = ""
    system_sha256: str = ""
    tool_schema_sha256: str = ""
    provider_options: dict[str, Any] = field(default_factory=dict)
    capability_profile: dict[str, Any] = field(default_factory=dict)
    checkpoint_ids: list[str] = field(default_factory=list)
    artifact_ids: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class FlightRecorder:
    """Append-only trajectory recorder separate from display-event replay.

    One recorder serves a session for as long as it lives, across its turns.
    Each top-level turn begins a slice of the trace (``begin_turn``); the
    events recorded until the next one carry its ``turn_id``, a delegated
    worker's included, so a turn's trace can be read and exported by itself.
    """

    def __init__(
        self,
        project_path: str | Path,
        *,
        run_id: str = "",
        root: str | Path | None = None,
        metadata: dict[str, Any] | None = None,
    ):
        self.project_path = Path(project_path).expanduser().resolve()
        self.root = Path(root) if root else project_state_dir(self.project_path) / "traces"
        self.root.mkdir(parents=True, exist_ok=True)
        self.run_id = run_id or f"run_{uuid.uuid4().hex[:12]}"
        self.run_dir = self.root / self.run_id
        self.run_dir.mkdir(parents=True, exist_ok=True)
        now = time.time()
        self.manifest = RunManifest(
            run_id=self.run_id,
            project_path=str(self.project_path),
            created_at=now,
            updated_at=now,
            metadata=dict(metadata or {}),
        )
        self._manifest_path = self.run_dir / "manifest.json"
        self._events_path = self.run_dir / "events.jsonl"
        self._lock = threading.RLock()
        self._sequence = 0
        self.turn_id = ""
        # Events the engine records as it yields them, by identity, so a
        # client that records every event it streams adds them only once.
        self._recorded_once: dict[int, tuple[dict[str, Any], dict[str, Any]]] = {}
        self._save_manifest()

    def begin_turn(self) -> str:
        """Start a turn's slice of the trace and return its id."""
        with self._lock:
            self.turn_id = f"turn_{uuid.uuid4().hex[:12]}"
            self._recorded_once.clear()
            return self.turn_id

    def configure(
        self,
        *,
        backend: str = "",
        model: str = "",
        model_role: str = "primary",
        prompt: str = "",
        system_prompt: str = "",
        tools: Iterable[dict[str, Any]] = (),
        provider_options: dict[str, Any] | None = None,
        capability_profile: dict[str, Any] | None = None,
    ) -> None:
        self.manifest.status = "running"
        self.manifest.backend = backend
        self.manifest.model = model
        self.manifest.model_role = model_role
        self.manifest.prompt_sha256 = self._digest(prompt)
        self.manifest.system_sha256 = self._digest(system_prompt)
        self.manifest.tool_schema_sha256 = self._digest(
            json.dumps(list(tools), sort_keys=True, ensure_ascii=False, default=str)
        )
        self.manifest.provider_options = dict(provider_options or {})
        self.manifest.capability_profile = dict(capability_profile or {})
        self.manifest.updated_at = time.time()
        self._save_manifest()

    def record(
        self, event: dict[str, Any], *, agent_id: str = "", once: bool = False,
    ) -> dict[str, Any]:
        """Append one event.

        ``once`` marks an event the engine records as it yields it: recording
        the same event object again in this turn returns the first record.
        The trace's own fields win over an event's (a checkpoint event carries
        the checkpoint's ``sequence``); an event's ``agent_id`` names its agent.
        """
        with self._lock:
            earlier = self._recorded_once.get(id(event))
            if earlier is not None and earlier[0] is event:
                return earlier[1]
            self._sequence += 1
            enriched = {
                "agent_id": agent_id,
                **event,
                "sequence": self._sequence,
                "timestamp": time.time(),
                "run_id": self.run_id,
            }
            if self.turn_id:
                enriched["turn_id"] = self.turn_id
            enriched["fingerprint"] = self.event_fingerprint(enriched)
            if once:
                self._recorded_once[id(event)] = (event, enriched)
            with self._events_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(enriched, ensure_ascii=False, default=str) + "\n")
            self.manifest.updated_at = enriched["timestamp"]
            if event.get("event") == "checkpoint.created" and event.get("checkpoint_id"):
                self.manifest.checkpoint_ids.append(str(event["checkpoint_id"]))
            if event.get("event") == "artifact.created" and event.get("artifact_id"):
                self.manifest.artifact_ids.append(str(event["artifact_id"]))
            if self._sequence % 10 == 0:
                self._save_manifest()
            return enriched

    def close(self, status: str = "completed", **metadata: Any) -> None:
        self.manifest.status = status
        self.manifest.updated_at = time.time()
        self.manifest.metadata.update(metadata)
        self._save_manifest()

    def events(self) -> list[dict[str, Any]]:
        return self._read_events(self._events_path)

    def turn_events(self, turn_id: str) -> list[dict[str, Any]]:
        """The events of one turn's slice (``begin_turn``), in order."""
        return self._turn_slice(self.events(), turn_id)

    @classmethod
    def read_turn(cls, project_path: str | Path, run_id: str, turn_id: str) -> list[dict[str, Any]]:
        """One turn's events, read without opening the run.

        ``open_run`` rewrites the run's manifest, which a session still
        recording into it may be writing too; reading a turn writes nothing.
        """
        events_path = cls._run_dir(project_path, run_id) / "events.jsonl"
        return cls._turn_slice(cls._read_events(events_path), turn_id)

    def export_otel(
        self, destination: str | Path | None = None, *, turn_id: str = "",
    ) -> dict[str, Any]:
        """Export a dependency-free OTLP-compatible JSON envelope.

        This intentionally emits JSON rather than importing an SDK. Operators
        can POST it to a collector or translate it with their existing stack.
        With ``turn_id``, only that turn's events are exported, as a trace of
        their own.
        """
        events = self.turn_events(turn_id) if turn_id else self.events()
        payload = self.otel_payload(self.run_id, events, turn_id=turn_id)
        if destination:
            Path(destination).write_text(json.dumps(payload, indent=2), encoding="utf-8")
        return payload

    @staticmethod
    def otel_payload(
        run_id: str, events: Iterable[dict[str, Any]], *, turn_id: str = "",
    ) -> dict[str, Any]:
        """The OTLP JSON envelope for these events (``export_otel``)."""
        trace_key = f"{run_id}:{turn_id}" if turn_id else run_id
        trace_id = hashlib.sha256(trace_key.encode()).hexdigest()[:32]
        spans = []
        for event in events:
            timestamp_ns = int(float(event.get("timestamp") or 0) * 1_000_000_000)
            spans.append({
                "traceId": trace_id,
                "spanId": hashlib.sha256(
                    f"{run_id}:{event.get('sequence')}".encode()
                ).hexdigest()[:16],
                "name": str(event.get("event") or "event"),
                "startTimeUnixNano": str(timestamp_ns),
                "endTimeUnixNano": str(timestamp_ns),
                "attributes": [
                    {"key": key, "value": {"stringValue": str(value)}}
                    for key, value in event.items()
                    if key not in {"timestamp", "run_id"} and value is not None
                ],
            })
        resource = [
            {"key": "service.name", "value": {"stringValue": "resonant-client"}},
            {"key": "lumi.run_id", "value": {"stringValue": run_id}},
        ]
        if turn_id:
            resource.append({"key": "lumi.turn_id", "value": {"stringValue": turn_id}})
        return {
            "resourceSpans": [{
                "resource": {"attributes": resource},
                "scopeSpans": [{"scope": {"name": "lumi.flight-recorder"}, "spans": spans}],
            }]
        }

    @staticmethod
    def _read_events(path: Path) -> list[dict[str, Any]]:
        if not path.exists():
            return []
        values = []
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                values.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return values

    @staticmethod
    def _turn_slice(events: list[dict[str, Any]], turn_id: str) -> list[dict[str, Any]]:
        return [event for event in events if turn_id and event.get("turn_id") == turn_id]

    @classmethod
    def list_runs(cls, project_path: str | Path) -> list[dict[str, Any]]:
        root = project_state_dir(project_path) / "traces"
        manifests: list[dict[str, Any]] = []
        if not root.exists():
            return manifests
        for path in root.glob("*/manifest.json"):
            try:
                manifests.append(json.loads(path.read_text(encoding="utf-8")))
            except (OSError, ValueError, json.JSONDecodeError):
                continue
        return sorted(
            manifests,
            key=lambda item: float(item.get("updated_at") or item.get("created_at") or 0),
            reverse=True,
        )

    @classmethod
    def open_run(cls, project_path: str | Path, run_id: str) -> "FlightRecorder":
        return cls.load(cls._run_dir(project_path, run_id))

    @staticmethod
    def _run_dir(project_path: str | Path, run_id: str) -> Path:
        """A run's folder under the project's traces, never outside them."""
        root = project_state_dir(project_path) / "traces"
        candidate = (root / run_id).resolve()
        if root.resolve() not in candidate.parents:
            raise KeyError(f"Invalid run id: {run_id}")
        if not (candidate / "manifest.json").is_file():
            raise KeyError(f"Unknown run: {run_id}")
        return candidate

    @classmethod
    def load(cls, run_dir: str | Path) -> "FlightRecorder":
        directory = Path(run_dir)
        manifest_data = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
        recorder = cls(
            manifest_data["project_path"],
            run_id=manifest_data["run_id"],
            root=directory.parent,
        )
        recorder.manifest = RunManifest(**manifest_data)
        recorder._sequence = len(recorder.events())
        # ``__init__`` creates a new manifest for fresh recorders.  Loading an
        # existing run must immediately restore its persisted lifecycle state
        # instead of leaving the on-disk trace marked as newly initialized.
        recorder._save_manifest()
        return recorder

    @classmethod
    def compare(cls, left: "FlightRecorder", right: "FlightRecorder") -> dict[str, Any]:
        left_events = left.events()
        right_events = right.events()
        common = min(len(left_events), len(right_events))
        divergence = None
        for index in range(common):
            if left_events[index].get("fingerprint") != right_events[index].get("fingerprint"):
                divergence = {
                    "index": index,
                    "left": left_events[index],
                    "right": right_events[index],
                }
                break
        if divergence is None and len(left_events) != len(right_events):
            divergence = {
                "index": common,
                "left": left_events[common] if common < len(left_events) else None,
                "right": right_events[common] if common < len(right_events) else None,
            }
        return {
            "left_run_id": left.run_id,
            "right_run_id": right.run_id,
            "left_events": len(left_events),
            "right_events": len(right_events),
            "first_causal_divergence": divergence,
            "same_trajectory": divergence is None,
        }

    @staticmethod
    def event_fingerprint(event: dict[str, Any]) -> str:
        stable = {
            key: value
            for key, value in event.items()
            if key not in {
                "timestamp", "sequence", "run_id", "elapsed", "elapsed_seconds",
                "total_elapsed", "fingerprint", "turn_id", "trace",
            }
        }
        return hashlib.sha256(
            json.dumps(stable, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")
        ).hexdigest()[:24]

    @staticmethod
    def _digest(value: str) -> str:
        return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()

    def _save_manifest(self) -> None:
        with self._lock:
            temp = self._manifest_path.with_suffix(".json.tmp")
            temp.write_text(
                json.dumps(self.manifest.to_dict(), indent=2, ensure_ascii=False, default=str),
                encoding="utf-8",
            )
            temp.replace(self._manifest_path)
