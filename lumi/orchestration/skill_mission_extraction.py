"""v0.6.0a2 — Skill extraction from autonomous-mission iters.

The existing `skill_extraction.py` module distills skills from
PLAN-GRAPH completion (Voyager-inspired). This module is the
sibling that distills skills from autonomous-mission ITER
completion — a different abstraction (roadmap items + REFLECT
verdicts) that pre-existed plan-graphs as the autonomous coder's
unit of work.

Triggered from `autonomous_loop.py::_run_full_reflect` after the
reflection event is emitted, when `verdict == "satisfied"` and
the daemon's cross-check did NOT override the verdict (i.e. it's
real success, not a model mood).

The extraction is best-effort and non-blocking-on-failure:
- A model call is made via the daemon's existing backend.
- The model decides whether the iter produced anything reusable;
  if not, it returns "(no skill)" and we exit silently.
- If the model returns a skill, we parse + save with
  `created_by="agent"` provenance — making it curator-touchable.

Cost guardrails:
- Heuristic threshold (`should_extract_from_iter`) cuts trivial
  iters before any model call.
- Hard token cap (default 4096) on the extractor's response.
- Best-effort: any exception is logged + swallowed; the daemon
  continues unaffected.

Tests in `tests/test_skill_mission_extraction.py` use the
streaming-stub harness from v0.5.17 to drive the extractor
deterministically.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Optional

from .skills import Skill, save_skill, tokenize

logger = logging.getLogger(__name__)


# Sentinel the extractor model returns when the iter produced
# nothing reusable. The model is explicitly instructed to use this
# exact string in the system prompt below.
NO_SKILL_SENTINEL = "(no skill)"


# Hard cap on extraction-prompt response tokens. The extractor's
# output is a SKILL.md body — usually 200-500 lines. 4096 tokens
# is ~3000 words, plenty for the format. The cap prevents runaway
# generation on a model that ignores the format guidance.
DEFAULT_EXTRACTOR_MAX_TOKENS = 4096


@dataclass
class IterContext:
    """The minimal slice of iter state the extractor needs.

    Decoupled from the full Roadmap / FullReflectOutcome objects so
    tests can construct one without booting the entire mission daemon.
    """
    roadmap_item_title: str
    roadmap_item_description: str
    iter_count: int
    intent_id: str
    project_path: str
    outcome_verdict: str
    outcome_summary: str
    pass_result_bash_passed: int = 0
    pass_result_bash_failed: int = 0
    pass_result_vision_passed: int = 0
    pass_result_vision_failed: int = 0
    decision_request_resolved: bool = False
    verdict_overridden: bool = False


# ── Threshold heuristic ────────────────────────────────────────────────


def should_extract_from_iter(ctx: IterContext) -> bool:
    """Decide whether the iter is worth attempting extraction for.

    Conservative: only verdict-satisfied + not-overridden iters that
    passed at least 2 acceptance criteria. The intent is to skip
    truly trivial iters (single-file scaffolding, one-line fixes)
    while letting non-trivial work through to the model.

    The MODEL has the final say — it can return NO_SKILL_SENTINEL
    even if this returns True. The heuristic is just a cheap pre-
    filter to avoid spending tokens on hopeless candidates.
    """
    if ctx.outcome_verdict != "satisfied":
        return False
    if ctx.verdict_overridden:
        return False
    # Truly trivial — hardly worth a skill.
    total_passed = ctx.pass_result_bash_passed + ctx.pass_result_vision_passed
    if total_passed < 2:
        return False
    return True


# ── Prompt builder ─────────────────────────────────────────────────────


SKILL_EXTRACTOR_SYSTEM_PROMPT = """\
You are the SKILL_EXTRACTOR specialist. The autonomous mission just
completed a roadmap item successfully. Your job: produce a single
agentskills.io-format SKILL.md that captures the REUSABLE pattern
from this iter — not the specific work that was done.

Reusable means: the next mission in this codebase (or a similar
codebase) facing the same shape of problem should be able to
follow this skill and skip your trial-and-error.

DO NOT extract:
- Project-specific code that won't apply elsewhere.
- One-off bug fixes (no underlying pattern).
- Trivial work (file scaffolding, boilerplate, single-line changes).
- Skills that restate something already in AGENTS.md or an existing
  skill (you don't have skill_list yet, so use your judgment).

DO extract:
- Patterns, conventions, framework quirks the agent had to learn
  by trial.
- Workflows that took multiple steps with non-obvious ordering.
- Failure-mode → resolution mappings (e.g. "when bash criterion X
  fails with error Y, the cause is usually Z; do W").

Output format — emit EXACTLY ONE of:

1. The literal string `(no skill)` if nothing reusable emerged.
   This is the right answer most of the time. It is BETTER to
   write zero skills than to pollute the library.

2. A SKILL.md body in this exact format:

```
---
name: <human-readable name>
description: <one-sentence summary, <120 chars>
version: 1.0.0
triggers: [<keyword>, <keyword>, <phrase>]
---

# <Skill title>

## Symptom
<What triggered the need for this skill — be concrete.>

## What to do
<The actual reusable content. Numbered steps, code snippets, or
workflow guidance. Optimize for the next agent encountering this
same shape of problem.>

## When NOT to apply
<One or two situations where this skill would mislead. Important.>
```

No prose outside the skill body. No reasoning, no "I think this
might be useful" hedging. Either emit the sentinel or emit the
skill — nothing else.
"""


def build_extractor_user_prompt(ctx: IterContext) -> str:
    """The user-side message handed to the extractor along with the
    system prompt above. Carries the iter context the model needs
    to judge whether a skill is warranted."""
    return f"""\
The autonomous mission `{ctx.intent_id}` just completed a roadmap
item with `verdict=satisfied`.

## Roadmap item
**{ctx.roadmap_item_title}**

{ctx.roadmap_item_description or "(no description)"}

## Iteration
- Iter count: {ctx.iter_count}
- Project: {ctx.project_path}

## REFLECT outcome
- Verdict: {ctx.outcome_verdict}
- Verdict-overridden: {ctx.verdict_overridden}
- Decision-request was resolved during this iter: {ctx.decision_request_resolved}

### Summary
{ctx.outcome_summary or "(no summary)"}

### Acceptance criteria results
- Bash criteria: {ctx.pass_result_bash_passed} passed, {ctx.pass_result_bash_failed} failed
- Vision criteria: {ctx.pass_result_vision_passed} passed, {ctx.pass_result_vision_failed} failed

---

Decide: did this iter produce a reusable pattern worth a skill?
If yes, emit the SKILL.md body (per the format in your system
prompt). If no, emit exactly `(no skill)`.
"""


# ── Response parser ────────────────────────────────────────────────────


def parse_extractor_response(response_text: str) -> Optional[tuple[dict, str]]:
    """Parse the extractor's response into (frontmatter_dict, body_md).

    Returns None if the response is the no-skill sentinel OR
    malformed. Caller should treat None as "no extraction; move on".

    The format expected matches `bundled_skills/_parse_frontmatter`
    output — same parser shape, but inlined here so this module is
    self-contained (the bundled_skills loader is purely an install-
    time concern; this is a runtime concern).
    """
    text = (response_text or "").strip()
    if not text:
        return None
    if text.lower().startswith(NO_SKILL_SENTINEL.lower()):
        return None
    # Strip a fenced code-block wrapper if the model added one.
    if text.startswith("```"):
        # Remove first line (```...) and the closing fence.
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()

    # Reuse the bundled-skills frontmatter parser via import — same
    # logic, no duplication.
    from .bundled_skills import _parse_frontmatter
    fm, body = _parse_frontmatter(text)
    if not fm or not body.strip():
        # Malformed. Better to skip than save garbage.
        logger.warning(
            "skill_mission_extraction: model response had no frontmatter "
            "or empty body; skipping"
        )
        return None
    return fm, body


# ── End-to-end extraction ──────────────────────────────────────────────


def extract_skill_from_iter(
    ctx: IterContext,
    *,
    backend: Any,
    max_tokens: int = DEFAULT_EXTRACTOR_MAX_TOKENS,
) -> Optional[Skill]:
    """Run the extractor end-to-end.

    1. Threshold check (`should_extract_from_iter`).
    2. If passed: call backend.stream() with the extractor prompts.
    3. Parse the response; if no skill, return None.
    4. Build a `Skill` with `created_by="agent"`, `scope="project"`,
       and persist via `save_skill`.
    5. Return the saved Skill (or None on any failure).

    Best-effort: any exception is logged and swallowed; the caller
    (autonomous mission daemon) keeps running unaffected.

    Backend contract: must support .stream(user_msg=..., conversation_
    history=[], instructions=..., tools=[], max_tokens=...)
    yielding (event_type, data) tuples matching the Session.run()
    protocol. The streaming-stub harness from v0.5.17 satisfies this.
    """
    if not should_extract_from_iter(ctx):
        return None

    try:
        # Collect text from the stream.
        collected: list[str] = []
        for event_type, data in backend.stream(
            user_msg=build_extractor_user_prompt(ctx),
            conversation_history=[],
            instructions=SKILL_EXTRACTOR_SYSTEM_PROMPT,
            tools=[],
            max_tokens=max_tokens,
        ):
            if event_type == "text.delta":
                collected.append(data.get("delta", ""))
            elif event_type == "done":
                break
            elif event_type == "error":
                logger.warning(
                    "skill_mission_extraction: backend emitted error: %s",
                    data.get("message", "unknown"),
                )
                return None

        response_text = "".join(collected).strip()
        parsed = parse_extractor_response(response_text)
        if parsed is None:
            return None

        frontmatter, body = parsed
        skill = _build_skill_from_extraction(ctx, frontmatter, body)
        if skill is None:
            return None
        save_skill(
            skill,
            procedure_md=body,
            project_path=ctx.project_path,
        )
        logger.info(
            "skill_mission_extraction: extracted skill `%s` from iter %d "
            "of mission %s", skill.id, ctx.iter_count, ctx.intent_id,
        )
        return skill
    except Exception as exc:
        # Best-effort — never break the mission daemon over a failed
        # extraction.
        logger.warning(
            "skill_mission_extraction: extraction failed for iter %d: %s",
            ctx.iter_count, exc, exc_info=True,
        )
        return None


def _build_skill_from_extraction(
    ctx: IterContext,
    frontmatter: dict,
    body: str,
) -> Optional[Skill]:
    """Assemble a Skill from the parsed extractor response + iter
    context. Sanity-checks required fields; returns None if the
    frontmatter is missing the bits a usable skill needs."""
    name = (frontmatter.get("name") or "").strip()
    description = (frontmatter.get("description") or "").strip()
    if not name or not description:
        logger.warning(
            "skill_mission_extraction: frontmatter missing name or "
            "description; skipping. Got name=%r description=%r",
            name, description,
        )
        return None

    # Build a kebab-case slug from the name.
    from .skill_extraction import slugify
    skill_id = slugify(name)

    triggers_raw = frontmatter.get("triggers", [])
    if isinstance(triggers_raw, str):
        triggers_raw = [triggers_raw]
    triggers = [str(t).strip() for t in triggers_raw if str(t).strip()]

    # Tokens: name + description + triggers + body sample.
    token_source = " ".join(
        [name, description] + triggers + [body[:1500]]
    )
    tokens = sorted(set(tokenize(token_source)))

    return Skill(
        id=skill_id,
        name=name,
        description=description,
        scope="project",                          # mission-extracted = project-scoped
        triggers=triggers,
        prerequisites=[],
        success_count=1,                          # successful extraction = 1 use
        fail_count=0,
        last_used_at=time.time(),
        version=str(frontmatter.get("version") or "1.0.0"),
        tokens=tokens,
        procedure_steps=[],                       # autonomous-mission iters don't have plan-graph nodes
        created_by="agent",                       # the load-bearing provenance gate
        pinned=False,
    )
