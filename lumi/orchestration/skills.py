"""
Skill library — reusable verified procedures.

Voyager-inspired: when a plan-graph completes successfully, distill it into a
named, reusable skill. Future intents that match an existing skill skip the
from-scratch decomposition and load the skill's pre-built subtree instead.

Storage layout (out of repo, mirrors Claude Code's projects/ pattern):

    ~/.resonant/skills/
        global/<skill-id>/
            skill.json          # metadata
            procedure.md        # human-readable steps
            verification.md     # success criteria
            examples/           # past plan-graphs that used this
        project/<project-hash>/<skill-id>/   # project-local
        stack/<stack-sig>/<skill-id>/        # stack-keyed

Override the parent dir with `RESONANT_STATE_HOME` (used by tests).

Similarity matching uses token overlap (no external embedding deps required).
A future upgrade can plug in real embeddings without changing the API.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


SKILL_SCOPES = ("global", "project", "stack")
DEFAULT_SCOPE = "global"


# ── Data model ──────────────────────────────────────────────────────────


@dataclass
class Skill:
    """One reusable procedure.

    v0.6.0a1 — `created_by` provenance + `pinned` durability fields. The
    curator only touches `created_by="agent"` skills; `bundled` (shipped
    with the package) and `user` (manually authored) are off-limits. Pinned
    skills are exempt from auto-deprecation regardless of provenance.
    """
    id: str                                  # slug (kebab-case)
    name: str                                # human-readable display name
    description: str                         # one-sentence summary
    scope: str = DEFAULT_SCOPE               # global | project | stack
    triggers: list[str] = field(default_factory=list)   # phrases / situations
    prerequisites: list[str] = field(default_factory=list)  # other skill ids
    success_count: int = 0
    fail_count: int = 0
    last_used_at: float = 0.0
    version: str = "1.0.0"
    tokens: list[str] = field(default_factory=list)     # for similarity scoring
    procedure_steps: list[dict] = field(default_factory=list)  # mirrors original PlanNodes
    # v0.6.0a1 — provenance + pinning.
    # `created_by`: who/what wrote this skill — gates the curator.
    #   - "bundled": shipped with the package (off-limits to curator)
    #   - "agent":   auto-extracted from a successful plan-graph or
    #                autonomous mission iter (curator-touchable)
    #   - "user":    manually authored via CLI / GUI (off-limits)
    # Default "agent" is back-compat with pre-v0.6 saves: those came
    # from the auto-extractor anyway.
    created_by: str = "agent"
    # `pinned`: user-marked durability. Pinned skills are exempt from
    # auto-deprecation by `is_deprecated` regardless of fail_rate or
    # unused_days. Curator also leaves them alone except for factual
    # patches.
    pinned: bool = False

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "Skill":
        allowed = {f.name for f in cls.__dataclass_fields__.values()}
        return cls(**{k: v for k, v in data.items() if k in allowed})

    def fail_rate(self) -> float:
        total = self.success_count + self.fail_count
        return self.fail_count / total if total else 0.0

    def is_deprecated(
        self,
        *,
        unused_days: float = 90.0,
        max_fail_rate: float = 0.5,
        min_uses_for_fail_rate: int = 10,
    ) -> bool:
        """Auto-deprecation rules — kept simple and honest about thresholds.

        v0.6.0a1 — pinned skills are exempt from auto-deprecation. The
        user explicitly signaled they want this skill kept alive
        regardless of usage stats.
        """
        if self.pinned:
            return False
        if self.last_used_at and (time.time() - self.last_used_at) > unused_days * 86400:
            return True
        total = self.success_count + self.fail_count
        if total >= min_uses_for_fail_rate and self.fail_rate() > max_fail_rate:
            return True
        return False

    def is_curator_touchable(self) -> bool:
        """v0.6.0a1 — provenance gate for the curator.

        The curator (skill_curator.py, added in v0.6.0a3) consolidates
        and archives skills. It must NEVER touch bundled or user-authored
        skills — only agent-extracted ones, with two further exemptions:
          - pinned skills: even agent-created, the user wants them kept
          - sticky skills: high-success-count skills the curator should
            consolidate but not archive (handled by curator, not here)
        """
        return self.created_by == "agent" and not self.pinned


# ── Storage helpers ─────────────────────────────────────────────────────


def _state_home() -> Path:
    return Path(os.environ.get("RESONANT_STATE_HOME") or (Path.home() / ".resonant"))


def _skills_root() -> Path:
    root = _state_home() / "skills"
    for scope in SKILL_SCOPES:
        (root / scope).mkdir(parents=True, exist_ok=True)
    (root / "_deprecated").mkdir(parents=True, exist_ok=True)
    return root


def _project_hash(project_path: str | Path) -> str:
    return hashlib.sha1(str(project_path).encode("utf-8", errors="replace")).hexdigest()[:12]


def skill_dir(
    skill_id: str,
    *,
    scope: str = DEFAULT_SCOPE,
    project_path: Optional[str | Path] = None,
    stack_sig: Optional[str] = None,
) -> Path:
    """Resolve the directory for a skill given its scope."""
    if scope not in SKILL_SCOPES:
        raise ValueError(f"Unknown scope {scope!r}; expected {SKILL_SCOPES}")
    base = _skills_root() / scope
    if scope == "project":
        if not project_path:
            raise ValueError("project scope requires project_path")
        base = base / _project_hash(project_path)
    elif scope == "stack":
        if not stack_sig:
            raise ValueError("stack scope requires stack_sig")
        base = base / stack_sig
    return base / skill_id


# ── Read / write ────────────────────────────────────────────────────────


def save_skill(
    skill: Skill,
    *,
    procedure_md: str = "",
    verification_md: str = "",
    project_path: Optional[str | Path] = None,
    stack_sig: Optional[str] = None,
) -> Path:
    """Persist a skill to disk. Returns the skill directory."""
    target = skill_dir(
        skill.id, scope=skill.scope,
        project_path=project_path, stack_sig=stack_sig,
    )
    target.mkdir(parents=True, exist_ok=True)
    (target / "skill.json").write_text(
        json.dumps(skill.to_dict(), indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    if procedure_md:
        (target / "procedure.md").write_text(procedure_md, encoding="utf-8")
    if verification_md:
        (target / "verification.md").write_text(verification_md, encoding="utf-8")
    (target / "examples").mkdir(exist_ok=True)
    return target


def load_skill(
    skill_id: str,
    *,
    scope: str = DEFAULT_SCOPE,
    project_path: Optional[str | Path] = None,
    stack_sig: Optional[str] = None,
) -> Optional[Skill]:
    target = skill_dir(skill_id, scope=scope, project_path=project_path, stack_sig=stack_sig)
    skill_path = target / "skill.json"
    if not skill_path.is_file():
        return None
    try:
        data = json.loads(skill_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Failed to read skill %s: %s", skill_path, exc)
        return None
    return Skill.from_dict(data)


def list_skills(
    *,
    scope: Optional[str] = None,
    project_path: Optional[str | Path] = None,
    stack_sig: Optional[str] = None,
    include_deprecated: bool = False,
) -> list[Skill]:
    """Enumerate skills. Filter by scope (or list across all if omitted)."""
    root = _skills_root()
    scopes = (scope,) if scope else SKILL_SCOPES
    out: list[Skill] = []
    for s in scopes:
        base = root / s
        if not base.is_dir():
            continue
        if s == "project":
            if not project_path:
                continue
            base = base / _project_hash(project_path)
            if not base.is_dir():
                continue
        elif s == "stack":
            if not stack_sig:
                continue
            base = base / stack_sig
            if not base.is_dir():
                continue
        for child in base.iterdir():
            if not child.is_dir():
                continue
            skill_path = child / "skill.json"
            if not skill_path.is_file():
                continue
            try:
                data = json.loads(skill_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            skill = Skill.from_dict(data)
            if not include_deprecated and skill.is_deprecated():
                continue
            out.append(skill)
    return out


def deprecate_skill(skill: Skill, *, project_path: Optional[str | Path] = None,
                    stack_sig: Optional[str] = None) -> Optional[Path]:
    """Move a skill folder into `_deprecated/`. Returns the new location.

    v0.6.0a1 — kept for back-compat (auto-deprecation of stale/failing
    skills). Curator-driven archival uses `archive_skill` below — separate
    semantics + log path so the two pipelines don't overwrite each other's
    timestamps.
    """
    src = skill_dir(skill.id, scope=skill.scope, project_path=project_path, stack_sig=stack_sig)
    if not src.exists():
        return None
    dest_root = _skills_root() / "_deprecated" / skill.scope
    dest_root.mkdir(parents=True, exist_ok=True)
    dest = dest_root / f"{int(time.time())}__{skill.id}"
    src.rename(dest)
    return dest


def archive_skill(
    skill: Skill,
    *,
    project_path: Optional[str | Path] = None,
    stack_sig: Optional[str] = None,
    reason: str = "",
) -> Optional[Path]:
    """v0.6.0a1 — curator-driven archival. Distinct from `deprecate_skill`:

    - Destination is `_skills_root()/_archive/<scope>/<ts>__<id>/` (NOT
      `_deprecated/`); separating the directories lets the user audit
      what the curator did vs what auto-deprecation did.
    - Refuses to archive non-curator-touchable skills (bundled / user /
      pinned) — defense in depth in case a misconfigured curator tries.
    - Writes a `_archive_reason.txt` alongside so `git blame`-equivalent
      forensics work later.

    Returns the destination path on success, None if the source doesn't
    exist or the skill isn't archivable.
    """
    if not skill.is_curator_touchable():
        logger.warning(
            "Refusing to archive non-touchable skill %s (created_by=%s, pinned=%s)",
            skill.id, skill.created_by, skill.pinned,
        )
        return None
    src = skill_dir(skill.id, scope=skill.scope, project_path=project_path, stack_sig=stack_sig)
    if not src.exists():
        return None
    dest_root = _skills_root() / "_archive" / skill.scope
    dest_root.mkdir(parents=True, exist_ok=True)
    dest = dest_root / f"{int(time.time())}__{skill.id}"
    src.rename(dest)
    if reason:
        try:
            (dest / "_archive_reason.txt").write_text(
                reason.strip() + "\n", encoding="utf-8",
            )
        except OSError as exc:
            logger.debug("Failed to write archive reason for %s: %s", skill.id, exc)
    return dest


def list_archived_skills(
    *,
    scope: Optional[str] = None,
    project_path: Optional[str | Path] = None,
) -> list[dict]:
    """v0.6.2a4 — enumerate skills in the _archive folder.

    Each `archive_skill` move lands at `_skills_root()/_archive/<scope>/
    <ts>__<id>/` (timestamp prefix, no project sub-hash — global vs
    project archives are distinguished by `<scope>` only). This walks
    those directories and returns one row per archived skill.

    Returns dicts (not Skill objects) because we want to expose the
    archive-only metadata: `archived_at` timestamp, `reason` from the
    sidecar, `archive_dir` path, plus the loaded `skill` for display.

    Most-recent first, sorted by `archived_at` descending. The
    `project_path` arg is purely for symmetry with the live-skill
    listing API; archive entries don't filter by project hash because
    the archive ts naming makes the dir flat.
    """
    out: list[dict] = []
    archive_root = _skills_root() / "_archive"
    if not archive_root.exists():
        return out
    scopes_to_check = [scope] if scope else list(SKILL_SCOPES)
    for scope_name in scopes_to_check:
        sd = archive_root / scope_name
        if not sd.exists():
            continue
        for entry in sorted(sd.iterdir(), reverse=True):
            if not entry.is_dir():
                continue
            name = entry.name
            ts_str, sep, sid = name.partition("__")
            if not sep or not sid:
                # Wrong shape — skip silently. Some test fixtures or
                # older curator runs may have left non-conforming dirs.
                continue
            try:
                ts = int(ts_str)
            except ValueError:
                continue
            skill_file = entry / "skill.json"
            if not skill_file.is_file():
                continue
            try:
                data = json.loads(skill_file.read_text(encoding="utf-8"))
                skill = Skill.from_dict(data)
            except (OSError, json.JSONDecodeError) as exc:
                logger.warning("Skipping malformed archive %s: %s", entry, exc)
                continue
            reason = ""
            rfile = entry / "_archive_reason.txt"
            if rfile.exists():
                try:
                    reason = rfile.read_text(
                        encoding="utf-8", errors="replace",
                    ).strip()
                except OSError:
                    pass
            out.append({
                "skill": skill,
                "archived_at": ts,
                "archive_dir": entry,
                "reason": reason,
                "scope": scope_name,
            })
    out.sort(key=lambda e: -e["archived_at"])
    return out


def restore_skill(
    skill_id: str,
    *,
    project_path: Optional[str | Path] = None,
    force: bool = False,
) -> Optional[Path]:
    """v0.6.2a4 — restore the most-recent archive of `skill_id`.

    Inverse of `archive_skill`. Looks up the most recent archive entry
    matching the id (across scopes), then `os.rename`s the archive dir
    back to its original `skill_dir(...)` location. Drops the
    `_archive_reason.txt` sidecar so the restored skill looks identical
    to one that never went through archive.

    Returns the destination Path on success.

    Returns None if:
    - No archive of that id exists
    - A live skill with the same id already exists at the destination
      (caller can pass `force=True` to overwrite)

    NOTE: this is a destructive ops if `force=True` — the existing live
    skill is removed before the archive is moved into place. The CLI
    surface should confirm with the user before passing force=True.
    """
    import shutil
    entries = [
        e for e in list_archived_skills(project_path=project_path)
        if e["skill"].id == skill_id
    ]
    if not entries:
        return None
    entry = entries[0]  # most recent — list is ts-desc
    skill: Skill = entry["skill"]
    archive_dir: Path = entry["archive_dir"]

    project_kw = project_path if skill.scope == "project" else None
    stack_kw = None  # restore doesn't currently support stack-scope skills
    dest = skill_dir(
        skill.id, scope=skill.scope,
        project_path=project_kw, stack_sig=stack_kw,
    )
    if dest.exists():
        if not force:
            logger.warning(
                "Refusing to restore %s: live skill exists at %s "
                "(pass force=True to overwrite)",
                skill.id, dest,
            )
            return None
        shutil.rmtree(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    archive_dir.rename(dest)
    reason_file = dest / "_archive_reason.txt"
    if reason_file.exists():
        try:
            reason_file.unlink()
        except OSError as exc:
            logger.debug("Failed to drop _archive_reason on restore: %s", exc)
    return dest


def list_skills_filtered(
    *,
    scope: Optional[str] = None,
    project_path: Optional[str | Path] = None,
    stack_sig: Optional[str] = None,
    created_by: Optional[str] = None,
    pinned: Optional[bool] = None,
    include_deprecated: bool = False,
) -> list[Skill]:
    """v0.6.0a1 — provenance-aware enumeration.

    Wraps `list_skills` with filters on the new `created_by` and
    `pinned` fields. Used by the curator (`created_by="agent"`,
    pinned=False) and by CLI listings (e.g. `resonant skill list
    --pinned`).
    """
    skills = list_skills(
        scope=scope, project_path=project_path, stack_sig=stack_sig,
        include_deprecated=include_deprecated,
    )
    if created_by is not None:
        skills = [s for s in skills if s.created_by == created_by]
    if pinned is not None:
        skills = [s for s in skills if s.pinned == pinned]
    return skills


def promote_skill(
    skill_id: str,
    *,
    project_path: str | Path,
    keep_project_copy: bool = False,
) -> Optional[Skill]:
    """v0.6.1a3 — elevate a project-scoped skill to global scope.

    The skill is COPIED to the global scope (with scope="global");
    by default the project copy is left in place so the user can
    decide whether to archive it. With `keep_project_copy=False` the
    project-scope copy is archived (NOT deleted) after the global
    copy lands.

    Refuses to promote `created_by="bundled"` skills — they're
    already global and protected. Returns the new global-scope
    Skill on success, None if the source skill doesn't exist or
    the global-scope id collides with an existing skill (use
    `force` semantics... no, refuse and let the user resolve).
    """
    src = load_skill(skill_id, scope="project", project_path=project_path)
    if src is None:
        return None
    if src.created_by == "bundled":
        logger.warning(
            "promote_skill: refusing to promote bundled skill %s "
            "(bundled skills are already global)", skill_id,
        )
        return None
    # Collision check: refuse to overwrite a global skill with the
    # same id. The user can pin the global version + archive the
    # project version manually if they want a "force" path.
    existing_global = load_skill(skill_id, scope="global")
    if existing_global is not None:
        logger.warning(
            "promote_skill: refusing to promote %s — global skill "
            "with same id already exists. Archive or rename the "
            "global copy first.",
            skill_id,
        )
        return None

    # Copy to global. Read the procedure / verification sidecars from
    # the project source so the global copy carries the same body.
    project_dir_path = skill_dir(skill_id, scope="project", project_path=project_path)
    procedure_md = ""
    verification_md = ""
    proc_path = project_dir_path / "procedure.md"
    ver_path = project_dir_path / "verification.md"
    if proc_path.is_file():
        procedure_md = proc_path.read_text(encoding="utf-8")
    if ver_path.is_file():
        verification_md = ver_path.read_text(encoding="utf-8")

    # The global copy keeps the same provenance + pinned flag — it's
    # the same skill, just promoted. Reset the success/fail counts
    # so cross-project usage tracking starts fresh? No — preserve
    # them; they're informative even if scope changes.
    promoted = Skill(
        id=src.id,
        name=src.name,
        description=src.description,
        scope="global",
        triggers=list(src.triggers),
        prerequisites=list(src.prerequisites),
        success_count=src.success_count,
        fail_count=src.fail_count,
        last_used_at=src.last_used_at,
        version=src.version,
        tokens=list(src.tokens),
        procedure_steps=list(src.procedure_steps),
        created_by=src.created_by,
        pinned=src.pinned,
    )
    save_skill(promoted, procedure_md=procedure_md, verification_md=verification_md)

    if not keep_project_copy:
        # Archive the project copy. Use deprecate_skill (auto-archive
        # path, project-scoped) since the user-driven promotion is
        # closer to that path's semantics than to the curator's
        # archive_skill (which only touches agent-created).
        try:
            deprecate_skill(src, project_path=project_path)
        except Exception:
            logger.warning(
                "promote_skill: failed to archive project copy of %s "
                "after promotion; both copies now exist on disk",
                skill_id, exc_info=True,
            )

    return promoted


def demote_skill(
    skill_id: str,
    *,
    target_project_path: str | Path,
    keep_global_copy: bool = False,
) -> Optional[Skill]:
    """v0.6.1a3 — opposite of `promote_skill`. Moves a global skill
    to project scope.

    Useful when a skill turns out to be project-specific and was
    elevated by mistake. Refuses bundled skills (their provenance
    is non-negotiable). Returns the new project-scope Skill on
    success.
    """
    src = load_skill(skill_id, scope="global")
    if src is None:
        return None
    if src.created_by == "bundled":
        logger.warning(
            "demote_skill: refusing to demote bundled skill %s "
            "(bundled provenance is non-negotiable)", skill_id,
        )
        return None
    # Collision check.
    existing_project = load_skill(
        skill_id, scope="project", project_path=target_project_path,
    )
    if existing_project is not None:
        logger.warning(
            "demote_skill: refusing to demote %s — project skill "
            "with same id already exists in %s. Archive or rename first.",
            skill_id, target_project_path,
        )
        return None

    global_dir = skill_dir(skill_id, scope="global")
    procedure_md = ""
    verification_md = ""
    proc_path = global_dir / "procedure.md"
    ver_path = global_dir / "verification.md"
    if proc_path.is_file():
        procedure_md = proc_path.read_text(encoding="utf-8")
    if ver_path.is_file():
        verification_md = ver_path.read_text(encoding="utf-8")

    demoted = Skill(
        id=src.id,
        name=src.name,
        description=src.description,
        scope="project",
        triggers=list(src.triggers),
        prerequisites=list(src.prerequisites),
        success_count=src.success_count,
        fail_count=src.fail_count,
        last_used_at=src.last_used_at,
        version=src.version,
        tokens=list(src.tokens),
        procedure_steps=list(src.procedure_steps),
        created_by=src.created_by,
        pinned=src.pinned,
    )
    save_skill(
        demoted, procedure_md=procedure_md,
        verification_md=verification_md,
        project_path=target_project_path,
    )

    if not keep_global_copy:
        try:
            deprecate_skill(src)  # global scope; no project_path
        except Exception:
            logger.warning(
                "demote_skill: failed to archive global copy of %s "
                "after demotion; both copies now exist on disk",
                skill_id, exc_info=True,
            )

    return demoted


def set_pinned(
    skill_id: str,
    pinned: bool,
    *,
    scope: str = DEFAULT_SCOPE,
    project_path: Optional[str | Path] = None,
    stack_sig: Optional[str] = None,
) -> Optional[Skill]:
    """v0.6.0a1 — pin/unpin a skill.

    Loads, mutates, saves. Returns the updated Skill (or None if the
    skill doesn't exist). The `procedure.md` and `verification.md`
    sidecar files aren't rewritten — only `skill.json` updates.
    """
    skill = load_skill(
        skill_id, scope=scope, project_path=project_path, stack_sig=stack_sig,
    )
    if skill is None:
        return None
    skill.pinned = bool(pinned)
    save_skill(
        skill, project_path=project_path, stack_sig=stack_sig,
    )
    return skill


# ── Similarity matching (token overlap) ─────────────────────────────────


_TOKEN_RE = re.compile(r"[A-Za-z0-9]{2,}")


def tokenize(text: str) -> list[str]:
    """Lowercase token list, used for triggers and similarity scoring."""
    return [m.group(0).lower() for m in _TOKEN_RE.finditer(text or "")]


def similarity(a: list[str], b: list[str]) -> float:
    """Jaccard similarity over token sets. 0.0–1.0."""
    if not a or not b:
        return 0.0
    set_a, set_b = set(a), set(b)
    if not set_a or not set_b:
        return 0.0
    intersection = len(set_a & set_b)
    union = len(set_a | set_b)
    return intersection / union if union else 0.0


@dataclass
class SkillMatch:
    skill: Skill
    score: float


def find_matching_skills(
    query: str,
    *,
    scope: Optional[str] = None,
    project_path: Optional[str | Path] = None,
    stack_sig: Optional[str] = None,
    top_k: int = 3,
    high_threshold: float = 0.85,
    partial_threshold: float = 0.6,
) -> list[SkillMatch]:
    """Score candidate skills for a query intent.

    Returns up to top_k matches sorted by score descending. Caller decides
    what to do with each based on the threshold tiers (high / partial / none).
    """
    query_tokens = tokenize(query)
    if not query_tokens:
        return []

    matches: list[SkillMatch] = []
    for skill in list_skills(scope=scope, project_path=project_path, stack_sig=stack_sig):
        # Score against both the skill's tokens AND its triggers (max wins).
        score_tokens = similarity(query_tokens, skill.tokens)
        score_triggers = max(
            (similarity(query_tokens, tokenize(t)) for t in skill.triggers),
            default=0.0,
        )
        score = max(score_tokens, score_triggers)
        if score >= partial_threshold:
            matches.append(SkillMatch(skill=skill, score=score))

    matches.sort(key=lambda m: m.score, reverse=True)
    return matches[:top_k]


def classify_match(score: float, *, high: float = 0.85, partial: float = 0.6) -> str:
    """Bucket a similarity score into the discovery-flow tier."""
    if score >= high:
        return "high"
    if score >= partial:
        return "partial"
    return "none"


# ── Usage tracking ──────────────────────────────────────────────────────


def record_skill_use(
    skill: Skill,
    *,
    success: bool,
    project_path: Optional[str | Path] = None,
    stack_sig: Optional[str] = None,
) -> None:
    """Bump success/fail counts and update last_used_at."""
    if success:
        skill.success_count += 1
    else:
        skill.fail_count += 1
    skill.last_used_at = time.time()
    save_skill(
        skill,
        project_path=project_path,
        stack_sig=stack_sig,
    )


def mark_skill_surfaced(
    skill: Skill,
    *,
    project_path: Optional[str | Path] = None,
    stack_sig: Optional[str] = None,
) -> None:
    """v0.6.3a2 — touch `last_used_at` WITHOUT bumping success/fail.

    Distinct from `record_skill_use`: this records that a skill was
    *surfaced* into a planner's context (deemed relevant by the
    matcher), not that it produced a measured good/bad outcome.

    Why the distinction matters: the curator auto-deprecates skills
    unused for 90 days (`Skill.is_deprecated`). Without a surface
    signal, every agent-created skill rots out of the library 90 days
    after extraction even if it's been surfaced into planner context
    every day — the read side of the self-improvement loop would
    quietly collapse. But surfacing is NOT a quality signal: a skill
    can be surfaced and ignored. So we bump only the staleness clock,
    never the success/fail counts (those need real attribution).
    """
    skill.last_used_at = time.time()
    save_skill(
        skill,
        project_path=project_path,
        stack_sig=stack_sig,
    )
