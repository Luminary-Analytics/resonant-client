"""Background smoke-evaluation jobs for the GUI dashboard."""

from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Optional

from ..smoke.baseline import diff_against_baseline, load_baseline
from ..smoke.runner import MODELS
from ..smoke.specs import list_spec_names
from ..smoke.variance import run_variance

logger = logging.getLogger(__name__)


class EvaluationManager:
    """Runs one live variance job at a time and persists compact records."""

    def __init__(
        self,
        *,
        storage_dir: str | Path | None = None,
        runner: Callable[..., Any] = run_variance,
        on_event: Optional[Callable[[dict], None]] = None,
    ) -> None:
        self.storage_dir = Path(storage_dir or Path.home() / ".resonant" / "evaluations")
        self._runner = runner
        self._on_event = on_event
        self._lock = threading.RLock()
        self._active_id = ""
        self._records: dict[str, dict] = {}
        self._turn_telemetry: list[dict] = []
        self._load_records()
        self._load_turn_telemetry()

    def capabilities(self) -> dict:
        return {
            "models": [{"label": label, "model": model} for label, model in MODELS.items()],
            "specs": list_spec_names(),
        }

    def snapshot(self) -> dict:
        with self._lock:
            records = sorted(
                (dict(record) for record in self._records.values()),
                key=lambda item: item.get("started_at", 0),
                reverse=True,
            )
            return {
                **self.capabilities(),
                "active_id": self._active_id,
                "records": records[:30],
                "turn_telemetry": list(reversed(self._turn_telemetry[-50:])),
                "turn_summary": self._turn_summary(),
            }

    def record_turn_telemetry(self, payload: dict) -> dict:
        """Persist a redacted interactive-turn health record."""
        allowed = {
            "model",
            "outcome",
            "elapsed_seconds",
            "steps",
            "tool_calls",
            "output_characters",
            "empty_response_attempts",
            "promise_continuations",
            "changed_files",
            "validation_tools",
            "provider_stats",
        }
        record = {key: payload.get(key) for key in allowed if key in payload}
        record["recorded_at"] = time.time()
        with self._lock:
            self._turn_telemetry.append(record)
            self._turn_telemetry = self._turn_telemetry[-500:]
            try:
                self.storage_dir.mkdir(parents=True, exist_ok=True)
                with self._telemetry_path().open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(record, default=str) + "\n")
            except OSError:
                logger.warning("failed to persist turn telemetry", exc_info=True)
        self._emit()
        return dict(record)

    def _turn_summary(self) -> dict:
        records = self._turn_telemetry[-500:]
        by_model: dict[str, dict] = {}
        for record in records:
            model = str(record.get("model") or "unknown")
            bucket = by_model.setdefault(model, {
                "turns": 0,
                "empty_response_turns": 0,
                "promise_continuations": 0,
                "incomplete_or_failed": 0,
                "elapsed_seconds": 0.0,
            })
            bucket["turns"] += 1
            if int(record.get("empty_response_attempts") or 0) > 0:
                bucket["empty_response_turns"] += 1
            bucket["promise_continuations"] += int(record.get("promise_continuations") or 0)
            if record.get("outcome") in {"incomplete", "failed", "changed_unverified"}:
                bucket["incomplete_or_failed"] += 1
            bucket["elapsed_seconds"] += float(record.get("elapsed_seconds") or 0.0)
        for bucket in by_model.values():
            turns = max(1, bucket["turns"])
            bucket["avg_elapsed_seconds"] = round(bucket.pop("elapsed_seconds") / turns, 3)
            bucket["empty_response_rate"] = round(bucket["empty_response_turns"] / turns, 4)
            bucket["incomplete_rate"] = round(bucket["incomplete_or_failed"] / turns, 4)
        return {"turns": len(records), "by_model": by_model}

    def start(
        self,
        *,
        model_label: str,
        spec_name: str,
        n: int,
        project_path: str | Path,
        timeout_minutes: int = 25,
    ) -> dict:
        if model_label not in MODELS:
            raise ValueError(f"Unknown evaluation model {model_label!r}")
        if spec_name not in set(list_spec_names()):
            raise ValueError(f"Unknown evaluation spec {spec_name!r}")
        if n not in {1, 3, 5}:
            raise ValueError("Evaluation runs must be 1, 3, or 5")
        timeout_minutes = max(1, min(int(timeout_minutes), 120))

        with self._lock:
            if self._active_id:
                active = self._records.get(self._active_id, {})
                if active.get("status") in {"queued", "running"}:
                    raise RuntimeError("An evaluation is already running")
            run_id = uuid.uuid4().hex[:12]
            record = {
                "id": run_id,
                "status": "queued",
                "model_label": model_label,
                "model_id": MODELS[model_label],
                "spec_name": spec_name,
                "n": n,
                "completed_runs": 0,
                "started_at": time.time(),
                "finished_at": None,
                "result": None,
                "baseline_diff": None,
                "error": "",
            }
            self._records[run_id] = record
            self._active_id = run_id
            self._save_record(record)

        thread = threading.Thread(
            target=self._run,
            args=(run_id, Path(project_path), timeout_minutes),
            name=f"resonant-eval-{run_id}",
            daemon=True,
        )
        thread.start()
        self._emit()
        return dict(record)

    def _run(self, run_id: str, project_path: Path, timeout_minutes: int) -> None:
        with self._lock:
            record = self._records[run_id]
            record["status"] = "running"
            self._save_record(record)
        self._emit()

        def on_run_complete(index: int, _result: Any) -> None:
            with self._lock:
                record["completed_runs"] = index
                self._save_record(record)
            self._emit()

        try:
            report = self._runner(
                spec_name=record["spec_name"],
                model_label=record["model_label"],
                n=record["n"],
                smoke_timeout_minutes=timeout_minutes,
                on_run_complete=on_run_complete,
            )
            baseline = load_baseline(
                project_path=project_path,
                spec=record["spec_name"],
                model=record["model_label"],
            )
            baseline_diff = (
                diff_against_baseline(current=report, baseline=baseline).to_dict()
                if baseline is not None else None
            )
            with self._lock:
                record["status"] = "passed" if report.convergence_rate >= 1.0 else "failed"
                record["completed_runs"] = record["n"]
                record["result"] = report.to_dict()
                record["baseline_diff"] = baseline_diff
                record["finished_at"] = time.time()
        except Exception as exc:
            logger.exception("evaluation %s failed", run_id)
            with self._lock:
                record["status"] = "error"
                record["error"] = str(exc)
                record["finished_at"] = time.time()
        finally:
            with self._lock:
                if self._active_id == run_id:
                    self._active_id = ""
                self._save_record(record)
            self._emit()

    def _emit(self) -> None:
        if self._on_event is None:
            return
        try:
            self._on_event({"event": "evaluation_dashboard", "data": self.snapshot()})
        except Exception:
            logger.debug("evaluation dashboard callback failed", exc_info=True)

    def _record_path(self, run_id: str) -> Path:
        return self.storage_dir / f"{run_id}.json"

    def _telemetry_path(self) -> Path:
        return self.storage_dir / "turn-telemetry.jsonl"

    def _save_record(self, record: dict) -> None:
        try:
            self.storage_dir.mkdir(parents=True, exist_ok=True)
            path = self._record_path(record["id"])
            pending = path.with_suffix(".json.tmp")
            pending.write_text(json.dumps(record, indent=2), encoding="utf-8")
            pending.replace(path)
        except OSError:
            logger.warning("failed to persist evaluation record", exc_info=True)

    def _load_records(self) -> None:
        if not self.storage_dir.exists():
            return
        for path in self.storage_dir.glob("*.json"):
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
                if record.get("status") in {"queued", "running"}:
                    record["status"] = "interrupted"
                    record["error"] = "Application exited before evaluation completed"
                    record["finished_at"] = record.get("finished_at") or time.time()
                if record.get("id"):
                    self._records[record["id"]] = record
            except (OSError, json.JSONDecodeError):
                logger.debug("skipping invalid evaluation record %s", path)

    def _load_turn_telemetry(self) -> None:
        path = self._telemetry_path()
        if not path.exists():
            return
        try:
            for line in path.read_text(encoding="utf-8").splitlines()[-500:]:
                if line.strip():
                    record = json.loads(line)
                    if isinstance(record, dict):
                        self._turn_telemetry.append(record)
        except (OSError, json.JSONDecodeError):
            logger.debug("skipping invalid turn telemetry", exc_info=True)
