"""v0.6.0a4 — skill loader / matcher / prompt injection.

Wraps `find_matching_skills` (from skills.py — already does the
token-overlap matching) and adds the pieces that make skill
discovery useful at autonomous-mission dispatch time:

- `match_skills_for_query(query, project_path, *, max_skills=8)`:
  matches via the existing tokenizer + ALWAYS includes pinned
  skills (project + global scope) regardless of token overlap.
  The user pinned them — they stay in scope.
- `format_skills_for_prompt(matches)`: produces the markdown block
  to inject into PLAN_DEEP's user message. Mirrors the bundled
  agent-callable-tool surface ("View with: skill_view <id>") so
  the agent knows how to dig in.

The actual call sites — autonomous mission daemon's PLAN_DEEP
context build, the chat session's plan-mode prompt — wire to this
module via a small hook + injection point. Those sites land
alongside this in v0.6.0a4 (or as a small GA patch if the
integration is more involved than expected).

Design intent: this module is the THIN matching layer. The heavy
lifting (token similarity, ranking) already lives in skills.py.
What this adds is the policy + presentation logic: "always include
pinned, cap at N, format for the prompt."
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .skills import (
    Skill,
    SkillMatch,
    find_matching_skills,
    list_skills_filtered,
)

logger = logging.getLogger(__name__)


# Default max skills to surface in PLAN_DEEP context. Each skill row
# is ~3 lines; 8 skills = ~24 lines = ~250 tokens. Bigger lists hurt
# prompt-cache efficiency without commensurate matching benefit.
DEFAULT_MAX_SKILLS = 8


# Minimum match score to include a non-pinned skill. Below this,
# the keyword overlap is too weak to be useful — including it would
# just add noise to the prompt.
DEFAULT_MIN_MATCH_SCORE = 0.05


@dataclass
class LoadedSkill:
    """A skill ready for prompt injection. Wraps SkillMatch with a
    boolean for whether it was included due to pinning vs match
    score; useful for telemetry + tests."""
    skill: Skill
    score: float
    via_pin: bool


def match_skills_for_query(
    query: str,
    *,
    project_path: Optional[str | Path] = None,
    max_skills: int = DEFAULT_MAX_SKILLS,
    min_score: float = DEFAULT_MIN_MATCH_SCORE,
) -> list[LoadedSkill]:
    """Discover skills relevant to `query` for this project.

    The matching pipeline:

    1. Pinned skills are added FIRST — both project-scoped (if
       project_path is given) and global-scoped pinned skills go in
       regardless of token overlap. The user said these matter; they
       always make it through.
    2. Then token-overlap matches via `find_matching_skills`, in
       descending score order, skipping anything already included
       via pin and anything below `min_score`.
    3. Total list is capped at `max_skills`.

    `query`: usually the spec markdown (full text from the rigorous-
    grill output) or a feature description. Whatever's most semantically
    rich at the call site.

    `project_path`: when given, project-scoped skills are searched
    in addition to global-scoped. When None, only global skills are
    considered (e.g. for chat-mode use where the project boundary
    isn't known).

    Returns list of `LoadedSkill` ordered by: pinned-first (in alpha
    order by id), then matches by score descending. Up to `max_skills`
    total.
    """
    if max_skills <= 0:
        return []

    seen_ids: set[str] = set()
    out: list[LoadedSkill] = []

    # 1. Pinned skills — global scope.
    pinned_global = list_skills_filtered(
        scope="global", pinned=True, include_deprecated=False,
    )
    # 1b. Pinned skills — project scope (if any).
    pinned_project: list[Skill] = []
    if project_path is not None:
        pinned_project = list_skills_filtered(
            scope="project", project_path=project_path,
            pinned=True, include_deprecated=False,
        )

    # Sort each pinned cohort alphabetically for stable ordering.
    for skill in sorted(pinned_global + pinned_project, key=lambda s: s.id):
        if skill.id in seen_ids:
            continue
        seen_ids.add(skill.id)
        out.append(LoadedSkill(skill=skill, score=1.0, via_pin=True))
        if len(out) >= max_skills:
            return out

    # 2. Token-overlap matches.
    # Search both global and project scopes; merge the results then
    # de-dupe by skill id (project-scoped wins on ties since it's
    # more specific).
    #
    # IMPORTANT: find_matching_skills has its own internal filter at
    # `partial_threshold` (default 0.6 — chosen for the discovery-flow
    # tier classifier). We pass `partial_threshold=min_score` so the
    # loader's threshold (default 0.05) is the actual gate. The
    # original 0.6 was tuned for "is this a strong-enough match to
    # auto-substitute the skill"; we need a much looser threshold for
    # "is this worth surfacing in the planner's prompt."
    matches: list[SkillMatch] = []
    for scope in ("project", "global"):
        if scope == "project" and project_path is None:
            continue
        scope_matches = find_matching_skills(
            query,
            scope=scope,
            project_path=project_path if scope == "project" else None,
            top_k=max_skills * 2,  # over-fetch to allow filtering
            partial_threshold=min_score,
        )
        matches.extend(scope_matches)

    # Sort all collected matches by score descending; for ties, keep
    # the first one (de-dupe handles multi-scope dupes).
    matches.sort(key=lambda m: -m.score)

    for match in matches:
        if len(out) >= max_skills:
            break
        if match.skill.id in seen_ids:
            continue
        if match.score < min_score:
            continue
        seen_ids.add(match.skill.id)
        out.append(LoadedSkill(skill=match.skill, score=match.score, via_pin=False))

    return out


# ── Prompt formatting ──────────────────────────────────────────────────


SKILLS_PROMPT_HEADER = """\
## Relevant skills from prior missions in this project

The following skills were extracted from past successful missions
or shipped as bundled references. Reference them BEFORE planning:
"""


SKILLS_PROMPT_FOOTER = """\
Skills are nudges, not commands. If a skill is wrong for this
mission, note WHY in your spec — the curator will see it on next
consolidation. View a skill's full body with `skill_view <id>`.
"""


def format_skills_for_prompt(loaded: list[LoadedSkill]) -> str:
    """Render the loaded skills into a markdown block ready for
    prompt injection.

    Layout matches the v0.5.7a5 grill exemplar style: numbered list,
    each entry shows the skill description + a one-line "view via"
    handle. Pinned-via-user entries get a 📌 marker so the agent
    sees they were intentionally curated.

    Returns an empty string when `loaded` is empty — callers can
    test `if block:` to decide whether to inject anything.
    """
    if not loaded:
        return ""
    lines = [SKILLS_PROMPT_HEADER, ""]
    for idx, ls in enumerate(loaded, start=1):
        marker = "📌 " if ls.via_pin else ""
        score_note = "(pinned)" if ls.via_pin else f"(match score {ls.score:.2f})"
        lines.append(f"{idx}. {marker}**`{ls.skill.id}`** — {ls.skill.description}")
        lines.append(f"   {score_note}")
        lines.append(f"   View with: `skill_view {ls.skill.id}`")
        lines.append("")
    lines.append(SKILLS_PROMPT_FOOTER)
    return "\n".join(lines).rstrip() + "\n"


def loaded_skill_ids(loaded: list[LoadedSkill]) -> list[str]:
    """Convenience: just the ids in injection order. Useful for
    telemetry + asserting test expectations without coupling to
    the formatting."""
    return [ls.skill.id for ls in loaded]


@dataclass
class SkillContext:
    """v0.6.3a2 — the result of a one-shot skill lookup for a dispatch.

    `block` is the formatted markdown ready to append to a planner's
    goal text (empty string when nothing matched — callers test
    `if ctx.block:`). `skill_ids` is the injection-order id list for
    telemetry / the iter-card chip. `loaded` keeps the full objects
    so the caller can mark them surfaced.
    """
    block: str
    skill_ids: list[str]
    loaded: list[LoadedSkill]


def build_skill_context(
    query: str,
    *,
    project_path: Optional[str | Path] = None,
    max_skills: int = DEFAULT_MAX_SKILLS,
    min_score: float = DEFAULT_MIN_MATCH_SCORE,
) -> SkillContext:
    """v0.6.3a2 — match + format in one call, for runtime wiring.

    This is the single entry point the autonomous-mission daemon's
    `dispatch_item` hook uses: it matches skills against the roadmap
    item's goal text, formats them into an injectable block, and
    returns both the block and the id list. The daemon appends
    `block` to the planner's goal and uses `skill_ids` for the
    `skill_context_loaded` telemetry event.

    Best-effort by contract: any matcher failure is logged and
    returns an EMPTY context rather than raising — a skill-lookup
    failure must never break mission dispatch.
    """
    try:
        loaded = match_skills_for_query(
            query,
            project_path=project_path,
            max_skills=max_skills,
            min_score=min_score,
        )
    except Exception:
        logger.warning(
            "build_skill_context: matcher raised; returning empty context",
            exc_info=True,
        )
        return SkillContext(block="", skill_ids=[], loaded=[])
    return SkillContext(
        block=format_skills_for_prompt(loaded),
        skill_ids=loaded_skill_ids(loaded),
        loaded=loaded,
    )
