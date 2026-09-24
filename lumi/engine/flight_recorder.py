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
    """Append-only trajectory recorder separate from display-event replay."""

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
        self._save_manifest()

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

    def record(self, event: dict[str, Any], *, agent_id: str = "") -> dict[str, Any]:
        with self._lock:
            self._sequence += 1
            enriched = {
                "sequence": self._sequence,
                "timestamp": time.time(),
                "run_id": self.run_id,
                "agent_id": agent_id,
                **event,
            }
            enriched["fingerprint"] = self.event_fingerprint(enriched)
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
        if not self._events_path.exists():
            return []
        values = []
        for line in self._events_path.read_text(encoding="utf-8").splitlines():
            try:
                values.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return values

    def export_otel(self, destination: str | Path | None = None) -> dict[str, Any]:
        """Export a dependency-free OTLP-compatible JSON envelope.

        This intentionally emits JSON rather than importing an SDK. Operators
        can POST it to a collector or translate it with their existing stack.
        """
        trace_id = hashlib.sha256(self.run_id.encode()).hexdigest()[:32]
        spans = []
        for event in self.events():
            timestamp_ns = int(float(event.get("timestamp") or 0) * 1_000_000_000)
            spans.append({
                "traceId": trace_id,
                "spanId": hashlib.sha256(
                    f"{self.run_id}:{event.get('sequence')}".encode()
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
        payload = {
            "resourceSpans": [{
                "resource": {"attributes": [
                    {"key": "service.name", "value": {"stringValue": "resonant-client"}},
                    {"key": "resonant.run_id", "value": {"stringValue": self.run_id}},
                ]},
                "scopeSpans": [{"scope": {"name": "resonant.flight-recorder"}, "spans": spans}],
            }]
        }
        if destination:
            Path(destination).write_text(json.dumps(payload, indent=2), encoding="utf-8")
        return payload

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
        root = project_state_dir(project_path) / "traces"
        candidate = (root / run_id).resolve()
        if root.resolve() not in candidate.parents:
            raise KeyError(f"Invalid run id: {run_id}")
        if not (candidate / "manifest.json").is_file():
            raise KeyError(f"Unknown run: {run_id}")
        return cls.load(candidate)

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
                "total_elapsed", "fingerprint",
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
