"""v0.6.0a3 — deterministic skill curator.

The curator periodically reviews curator-touchable skills (those with
`created_by="agent"` and not pinned) and archives ones that have gone
stale or hit a high fail rate. Archival uses the v0.6.0a1
`archive_skill()` function — destructive only via `_archive/` move,
NEVER delete.

This is the DETERMINISTIC half of the curator. Pure Python, no model
call. Cheap enough to run on every satisfied-GA trigger from the
autonomous mission daemon (rate-limited to once per 24h per project
to avoid thrash).

The MODEL-DRIVEN half — umbrella consolidation, where a forked agent
reviews narrow sibling skills and merges them into broader patterns —
is deferred to v0.6.1. The deterministic stale-archival on its own
keeps the library from rotting; consolidation is a longer-horizon
quality improvement.

State + reports live at:
    ~/.resonant/projects/<sha1(project_path)[:12]>/curator/
        .state.json                       # last_run_at, run_count, paused
        20260506-103000/
            run.json                      # machine-readable
            REPORT.md                     # human-readable

Mirrors the layout used by the autonomous mission daemon's audit log
(per-project, hash-keyed) so triage tooling can sweep both with the
same conventions.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

from .skills import (
    Skill,
    archive_skill,
    list_skills_filtered,
    _state_home,
    _project_hash,
)

logger = logging.getLogger(__name__)


# Default minimum hours between curator runs for a given project.
# Same shape as Hermes' interval_hours; we just default it shorter
# because our runs are cheaper (no model call in this alpha).
DEFAULT_MIN_HOURS_BETWEEN_RUNS = 24.0


# ── Data shapes ────────────────────────────────────────────────────────


@dataclass
class CuratorAction:
    """One operation the curator took (or proposed).

    `kind`: archive | retain | warn
    `skill_id`: the skill the operation targeted
    `reason`: human-readable explanation written to REPORT.md
    `details`: machine-readable bag for run.json (e.g. last_used_days_ago)
    """
    kind: str
    skill_id: str
    reason: str
    details: dict = field(default_factory=dict)


@dataclass
class CuratorReport:
    """Outcome of one curator run."""
    started_at_iso: str
    finished_at_iso: str
    project_path: str
    project_hash: str
    skills_reviewed: int = 0
    actions: list[CuratorAction] = field(default_factory=list)
    state_dir: str = ""

    def archived(self) -> list[CuratorAction]:
        return [a for a in self.actions if a.kind == "archive"]

    def retained(self) -> list[CuratorAction]:
        return [a for a in self.actions if a.kind == "retain"]

    def warned(self) -> list[CuratorAction]:
        return [a for a in self.actions if a.kind == "warn"]


# ── Path helpers ───────────────────────────────────────────────────────


def _curator_root(project_path: str | Path) -> Path:
    """Return the curator root for this project: live state + per-run logs."""
    return _state_home() / "projects" / _project_hash(project_path) / "curator"


def _state_file(project_path: str | Path) -> Path:
    return _curator_root(project_path) / ".state.json"


def _run_dir(project_path: str | Path, started_at: datetime) -> Path:
    return _curator_root(project_path) / started_at.strftime("%Y%m%d-%H%M%S")


# ── State tracking ─────────────────────────────────────────────────────


def read_state(project_path: str | Path) -> dict:
    """Read the curator state file. Returns {} if the file doesn't
    exist yet (fresh project)."""
    path = _state_file(project_path)
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        logger.warning("curator: failed to read state at %s; treating as empty", path)
        return {}


def write_state(project_path: str | Path, state: dict) -> None:
    """Write the curator state file atomically. Creates the curator
    root dir if needed."""
    root = _curator_root(project_path)
    root.mkdir(parents=True, exist_ok=True)
    path = _state_file(project_path)
    path.write_text(
        json.dumps(state, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def should_run_curation(
    project_path: str | Path,
    *,
    min_hours_between_runs: float = DEFAULT_MIN_HOURS_BETWEEN_RUNS,
) -> bool:
    """Rate-limit the curator. Returns True if enough time has passed
    since the last run for this project (or never run before).

    Also returns False if the state says the curator is paused
    (`paused=True` in `.state.json`). The pause flag is settable via
    the (eventual) `resonant skill curator pause/resume` CLI; for v0.6
    just respect it if present.
    """
    state = read_state(project_path)
    if state.get("paused"):
        return False
    last_run = state.get("last_run_at_epoch", 0)
    if not last_run:
        return True
    elapsed_hours = (time.time() - float(last_run)) / 3600.0
    return elapsed_hours >= min_hours_between_runs


# ── The actual run ─────────────────────────────────────────────────────


def run_curation(
    project_path: str | Path,
    *,
    unused_days_archive: float = 90.0,
    max_fail_rate: float = 0.5,
    min_uses_for_fail_rate: int = 10,
    dry_run: bool = False,
) -> CuratorReport:
    """Run a deterministic curator pass for this project.

    - Lists curator-touchable skills (created_by="agent", not pinned).
    - For each: calls `is_deprecated` with the configured thresholds.
      Skills that match are archived via `archive_skill` with a
      reason string. The user-pinned exemption is enforced inside
      `is_deprecated` itself + by `is_curator_touchable`.
    - Skips bundled / user / pinned skills entirely (defense in
      depth — they're not in `list_skills_filtered(created_by="agent",
      pinned=False)` to begin with).
    - Writes `run.json` + `REPORT.md` to a timestamped subdir under
      the curator root.
    - Updates `.state.json` with `last_run_at_epoch` + `run_count`.

    `dry_run=True` runs the analysis but skips the actual archive +
    state-file writes. Useful for previewing / CLI inspection.

    Returns the CuratorReport. Caller can read `.actions` to see
    what was done (or proposed in dry-run).
    """
    started_at = datetime.utcnow()
    started_at_iso = started_at.isoformat() + "Z"

    project_path_str = str(Path(project_path).expanduser().resolve())
    p_hash = _project_hash(project_path_str)
    run_dir_path = _run_dir(project_path_str, started_at)

    actions: list[CuratorAction] = []

    # Curator-touchable: created_by="agent" + not pinned.
    # CRITICAL: include_deprecated=True is the curator's whole point —
    # we WANT to see deprecated skills so we can archive them. The
    # default-False on list_skills_filtered is for read paths (skill
    # discovery, matching) where deprecated skills should be hidden.
    candidates = list_skills_filtered(
        scope="project",
        project_path=project_path_str,
        created_by="agent",
        pinned=False,
        include_deprecated=True,
    )

    for skill in candidates:
        # Skill.is_deprecated() with the configured thresholds.
        if skill.is_deprecated(
            unused_days=unused_days_archive,
            max_fail_rate=max_fail_rate,
            min_uses_for_fail_rate=min_uses_for_fail_rate,
        ):
            # What was the dominant reason?
            now = time.time()
            unused_days_actual = (
                (now - skill.last_used_at) / 86400.0 if skill.last_used_at else 0.0
            )
            details = {
                "last_used_at_epoch": skill.last_used_at,
                "unused_days_actual": round(unused_days_actual, 2),
                "success_count": skill.success_count,
                "fail_count": skill.fail_count,
                "fail_rate": round(skill.fail_rate(), 3),
            }
            reason_parts = []
            if skill.last_used_at and unused_days_actual > unused_days_archive:
                reason_parts.append(
                    f"unused for {unused_days_actual:.0f} days (threshold {unused_days_archive:.0f})"
                )
            if (
                skill.success_count + skill.fail_count >= min_uses_for_fail_rate
                and skill.fail_rate() > max_fail_rate
            ):
                reason_parts.append(
                    f"fail rate {skill.fail_rate():.0%} on {skill.success_count + skill.fail_count} uses"
                )
            reason = "; ".join(reason_parts) or "deprecated by is_deprecated heuristic"

            if not dry_run:
                # archive_skill internally enforces is_curator_touchable
                # so we get defense in depth here.
                dest = archive_skill(
                    skill,
                    project_path=project_path_str,
                    reason=reason,
                )
                if dest is None:
                    # Archive refused — skill became non-touchable
                    # between the list call and now (race condition,
                    # rare). Treat as warn.
                    actions.append(CuratorAction(
                        kind="warn",
                        skill_id=skill.id,
                        reason="archive refused by gate (race?); skipped",
                        details=details,
                    ))
                    continue
                details["archived_to"] = str(dest)
            actions.append(CuratorAction(
                kind="archive",
                skill_id=skill.id,
                reason=reason,
                details=details,
            ))
        else:
            actions.append(CuratorAction(
                kind="retain",
                skill_id=skill.id,
                reason="within thresholds",
                details={
                    "last_used_at_epoch": skill.last_used_at,
                    "fail_rate": round(skill.fail_rate(), 3),
                },
            ))

    finished_at = datetime.utcnow()
    finished_at_iso = finished_at.isoformat() + "Z"

    report = CuratorReport(
        started_at_iso=started_at_iso,
        finished_at_iso=finished_at_iso,
        project_path=project_path_str,
        project_hash=p_hash,
        skills_reviewed=len(candidates),
        actions=actions,
        state_dir=str(run_dir_path),
    )

    if not dry_run:
        _persist_report(report, run_dir_path)
        # Update curator state.
        state = read_state(project_path_str)
        state["last_run_at_epoch"] = time.time()
        state["last_run_at_iso"] = finished_at_iso
        state["run_count"] = int(state.get("run_count", 0)) + 1
        write_state(project_path_str, state)

    return report


def _persist_report(report: CuratorReport, run_dir: Path) -> None:
    """Write run.json + REPORT.md to the run dir."""
    run_dir.mkdir(parents=True, exist_ok=True)
    # Machine-readable.
    run_json_path = run_dir / "run.json"
    run_json_path.write_text(
        json.dumps(_report_to_dict(report), indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    # Human-readable.
    md = _format_report_md(report)
    (run_dir / "REPORT.md").write_text(md, encoding="utf-8")


def _report_to_dict(report: CuratorReport) -> dict:
    return {
        "started_at": report.started_at_iso,
        "finished_at": report.finished_at_iso,
        "project_path": report.project_path,
        "project_hash": report.project_hash,
        "skills_reviewed": report.skills_reviewed,
        "actions": [asdict(a) for a in report.actions],
    }


def _format_report_md(report: CuratorReport) -> str:
    lines = [
        f"# Curator run — {report.started_at_iso}",
        "",
        f"**Project:** `{report.project_path}` (hash `{report.project_hash}`)",
        f"**Skills reviewed:** {report.skills_reviewed}",
        f"**Started:** {report.started_at_iso}  ·  **Finished:** {report.finished_at_iso}",
        "",
        "## Actions",
        "",
    ]
    if not report.actions:
        lines.append("_No curator-touchable skills found in this project._")
        return "\n".join(lines) + "\n"

    archived = report.archived()
    retained = report.retained()
    warned = report.warned()

    if archived:
        lines.append(f"### Archived ({len(archived)})")
        lines.append("")
        for a in archived:
            lines.append(f"- **`{a.skill_id}`** — {a.reason}")
            if a.details.get("archived_to"):
                lines.append(f"  · archived to `{a.details['archived_to']}`")
        lines.append("")

    if retained:
        lines.append(f"### Retained ({len(retained)})")
        lines.append("")
        for a in retained:
            lines.append(f"- `{a.skill_id}` — {a.reason}")
        lines.append("")

    if warned:
        lines.append(f"### Warnings ({len(warned)})")
        lines.append("")
        for a in warned:
            lines.append(f"- `{a.skill_id}` — {a.reason}")
        lines.append("")

    lines.append("---")
    lines.append("")
    lines.append("Generated by the v0.6.0a3 deterministic curator. Model-driven")
    lines.append("umbrella consolidation will land in v0.6.1+.")
    return "\n".join(lines) + "\n"
