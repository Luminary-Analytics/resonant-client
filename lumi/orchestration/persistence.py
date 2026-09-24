"""
Disk-persistence for plan-graphs.

Layout:
    ~/.resonant/projects/<sha1(project_path)[:12]>/plans/
        current/<intent-id>.json    # live plan-graph for an intent
        snapshots/<ts>__<intent-id>.json  # history (for rollback)

Override the parent dir with `RESONANT_STATE_HOME` (used by tests).
Snapshots auto-purge after `retention_days` (default 30).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from pathlib import Path
from typing import Optional

from .plan_graph import PlanGraph

logger = logging.getLogger(__name__)


def _state_home() -> Path:
    return Path(os.environ.get("RESONANT_STATE_HOME") or (Path.home() / ".resonant"))


def _project_hash(project_path: str | Path) -> str:
    return hashlib.sha1(str(project_path).encode("utf-8", errors="replace")).hexdigest()[:12]


def plans_dir(project_path: str | Path) -> Path:
    """Return `<state_home>/projects/<hash>/plans/`. Created on demand."""
    root = _state_home() / "projects" / _project_hash(project_path) / "plans"
    root.mkdir(parents=True, exist_ok=True)
    (root / "current").mkdir(exist_ok=True)
    (root / "snapshots").mkdir(exist_ok=True)
    return root


# ── Save / load current graph ────────────────────────────────────────────


def save_graph(graph: PlanGraph, project_path: str | Path) -> Path:
    """Write the live graph to `current/<intent-id>.json`. Returns the path."""
    target = plans_dir(project_path) / "current" / f"{graph.intent_id}.json"
    payload = json.dumps(graph.to_dict(), indent=2, ensure_ascii=False)
    target.write_text(payload + "\n", encoding="utf-8")
    return target


def load_graph(intent_id: str, project_path: str | Path) -> Optional[PlanGraph]:
    """Load a live graph by intent id. Returns None if missing / unreadable."""
    target = plans_dir(project_path) / "current" / f"{intent_id}.json"
    if not target.is_file():
        return None
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Failed to read plan graph %s: %s", target, exc)
        return None
    return PlanGraph.from_dict(data)


# ── Snapshots ────────────────────────────────────────────────────────────


def snapshot_graph(graph: PlanGraph, project_path: str | Path) -> Path:
    """Append a timestamped snapshot. Returns the snapshot path."""
    ts = int(time.time() * 1000)  # ms — readable + sortable + collision-safe
    name = f"{ts}__{graph.intent_id}.json"
    target = plans_dir(project_path) / "snapshots" / name
    payload = json.dumps(graph.to_dict(), indent=2, ensure_ascii=False)
    target.write_text(payload + "\n", encoding="utf-8")
    return target


def list_snapshots(
    project_path: str | Path,
    *,
    intent_id: Optional[str] = None,
) -> list[dict]:
    """Return snapshot metadata sorted newest-first.

    Each entry: {ts_ms, ts_iso, intent_id, path, node_count, intent}.
    Filter to one intent with `intent_id`.
    """
    snap_dir = plans_dir(project_path) / "snapshots"
    out: list[dict] = []
    for child in snap_dir.iterdir():
        if child.suffix != ".json" or "__" not in child.stem:
            continue
        try:
            ts_str, snap_intent = child.stem.split("__", 1)
            ts_ms = int(ts_str)
        except ValueError:
            continue
        if intent_id and snap_intent != intent_id:
            continue
        try:
            data = json.loads(child.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        out.append({
            "ts_ms": ts_ms,
            "ts_iso": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(ts_ms / 1000)),
            "intent_id": snap_intent,
            "intent": data.get("intent", ""),
            "node_count": len(data.get("nodes", []) or []),
            "path": str(child),
        })
    out.sort(key=lambda e: e["ts_ms"], reverse=True)
    return out


def restore_snapshot(
    project_path: str | Path,
    *,
    ts_ms: int,
    intent_id: str,
) -> Optional[PlanGraph]:
    """Load a snapshot by (ts_ms, intent_id). Returns None if missing."""
    target = plans_dir(project_path) / "snapshots" / f"{ts_ms}__{intent_id}.json"
    if not target.is_file():
        return None
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Failed to read snapshot %s: %s", target, exc)
        return None
    return PlanGraph.from_dict(data)


def purge_old_snapshots(
    project_path: str | Path,
    *,
    retention_days: float = 30.0,
) -> int:
    """Delete snapshots older than `retention_days`. Returns count purged."""
    cutoff_ms = int((time.time() - retention_days * 86400) * 1000)
    snap_dir = plans_dir(project_path) / "snapshots"
    purged = 0
    for child in snap_dir.iterdir():
        if child.suffix != ".json" or "__" not in child.stem:
            continue
        try:
            ts_ms = int(child.stem.split("__", 1)[0])
        except ValueError:
            continue
        if ts_ms < cutoff_ms:
            try:
                child.unlink()
                purged += 1
            except OSError as exc:
                logger.warning("Failed to purge %s: %s", child, exc)
    if purged:
        logger.info("Purged %d snapshot(s) older than %sd from %s", purged, retention_days, snap_dir)
    return purged
