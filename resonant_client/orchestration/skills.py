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
    """One reusable procedure."""
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
        """Auto-deprecation rules — kept simple and honest about thresholds."""
        if self.last_used_at and (time.time() - self.last_used_at) > unused_days * 86400:
            return True
        total = self.success_count + self.fail_count
        if total >= min_uses_for_fail_rate and self.fail_rate() > max_fail_rate:
            return True
        return False


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
    """Move a skill folder into `_deprecated/`. Returns the new location."""
    src = skill_dir(skill.id, scope=skill.scope, project_path=project_path, stack_sig=stack_sig)
    if not src.exists():
        return None
    dest_root = _skills_root() / "_deprecated" / skill.scope
    dest_root.mkdir(parents=True, exist_ok=True)
    dest = dest_root / f"{int(time.time())}__{skill.id}"
    src.rename(dest)
    return dest


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
