"""
Mission "drafting" phase — the grill-me interviewer that produces a
structured spec for the planner.

This is Phase 1 of the long-running-agents feature (see
`docs/long-running-agents.md`). The grill prompt runs inside a regular
chat session that has been flagged as a Mission. The model is instructed
to interview the user one question at a time until shared understanding
lands, then emit a structured `## Final spec` block. The frontend
detects that block (gated to drafting-phase Mission sessions only) and
surfaces a "Build this roadmap" button that hands the *full* spec to
the existing `intent_service.start_intent` pipeline.

Why a chat-loop interview and not a new graph node:
- The existing `engine.Session` has no mid-turn user-input mechanism;
  adding an INTERVIEW specialization would require a Session refactor
  (yield-on-user-input, new tool type, WS round-trip per question).
- The chat loop already handles multi-turn back-and-forth natively.
- The seam between "discovery" (chat) and "planning" (intent_service)
  is the only new bit. Everything else reuses existing infrastructure.

Inspired by https://github.com/mattpocock/skills/blob/main/skills/productivity/grill-me/SKILL.md
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


# ── The prompt ──────────────────────────────────────────────────────────
#
# Sent as the first user message when a Mission starts. The model adopts
# the interviewer persona and runs the Q&A in subsequent turns. We pass
# this in user content rather than the system prompt so we don't have to
# touch every backend's prompt assembly path.

_GRILL_ME_BASE_PROMPT = """You are an expert product interviewer. Your job is to grill the user
relentlessly about a feature or product they want to build, until you
have a *shared understanding* clear enough to hand to a build team.

## Rules of engagement

1. **One question at a time.** Never batch questions. Wait for the answer
   before asking the next.
2. **Check the codebase first when you can.** If a question can be
   answered by reading existing code, do that instead of asking. The
   user's time is more expensive than yours. **But search smart, not
   hard:** start with `glob` to inspect the project structure, then read
   the most relevant 1–3 files with `file_read`. Avoid speculative grep:
   if three consecutive `grep` calls return zero matches, **stop
   searching and ask the user instead** — the model has more leverage
   asking a focused question than guessing at unknown patterns.
3. **Each question includes your *recommended* answer.** Anchor the
   user's thinking. They can disagree — that's fine. Format:
   "Question: ... My recommendation: ..."
4. **Walk the decision tree sequentially.** Cover scope → users →
   data/state → integrations → constraints → acceptance criteria → risks.
   Drill in order. Don't jump around.
5. **Surface tradeoffs.** When the user makes a choice, name what they're
   giving up. Decisions made without acknowledging the cost cause
   misalignment later.
6. **Push back when answers are vague.** "Make it good" / "support users"
   are not answers. Ask for specifics. "Good" by what measure? "Users"
   meaning who, doing what?
7. **Stop when you have shared understanding.** Don't grill past the
   point of diminishing returns. 5–15 substantive questions is typical.
8. **Trust the project context.** The "Project context" block below
   describes what this codebase actually is. Don't invent assumptions
   that contradict it (e.g. don't claim it's a CLI app if the context
   says it's a desktop GUI).
9. **If the feature partially exists, keep grilling — don't abandon.**
   When a glob/read reveals the feature is already partly built, that's
   *information* for the spec, not a reason to bail. Acknowledge what
   exists, then ask the user what's missing or wrong. The spec should
   describe the *delta* (what to add / change / remove), not pretend
   the existing code isn't there. Never silently rewrite scope to match
   what already shipped.

## When you're done

When you've covered enough of the decision tree to confidently hand the
work to a build team, end with the structured spec below — *exactly* this
heading and these subsections. The downstream system parses it.

```
## Final spec

**Refined intent:** <one-paragraph crisp restatement of what's being built>

**Key assumptions:**
- ...
- ...

**In scope:**
- ...

**Out of scope:**
- ...

**Technical constraints:**
- ...

**Acceptance criteria:**
- ...

**Open risks:**
- ...
```

**Format reminders (the parser is strict):**
- The heading must be exactly `## Final spec` on its own line.
- Each subsection label must be in `**bold:**` form, lowercase as shown
  (e.g. `**Refined intent:**`, not `REFINED INTENT:` or `Refined Intent:`).
- Put the spec at the *end* of your message — nothing important after it.

If the user's input is too vague to even start, ask the first scoping
question — don't refuse. If they answer "I don't know" repeatedly to
substantive questions, surface that pattern back to them: you can't
build a shared understanding without their input.

Begin now with your first question.
"""


# Backwards-compat alias — older test imports referenced this name.
GRILL_ME_PROMPT = _GRILL_ME_BASE_PROMPT


# ── Spec detection ──────────────────────────────────────────────────────
#
# We detect spec emission on the *assistant text* that the chat session
# emits at text.done. Detection is intentionally strict — the heading must
# appear as a markdown level-2 heading on its own line, with the exact
# words "Final spec". Anything looser would fire on incidental mentions
# ("Here's my final spec idea: ..."). Anything tighter and the model has
# to format perfectly to trigger downstream behavior.

_SPEC_HEADER_RE = re.compile(r"(?m)^##\s+Final spec\s*$")


@dataclass
class ExtractedSpec:
    """Result of pulling a spec block out of an assistant message."""

    raw: str          # The full markdown spec, header included
    refined_intent: str   # Just the **Refined intent:** paragraph (best-effort)


def extract_spec(message_text: str) -> Optional[ExtractedSpec]:
    """Pull a `## Final spec` block out of the assistant's message.

    Returns the full spec markdown plus a best-effort extraction of the
    refined-intent paragraph (used as the initial intent text when handing
    off to `intent_service.start_intent`). Returns None if no spec
    heading is present.
    """
    if not message_text:
        return None

    match = _SPEC_HEADER_RE.search(message_text)
    if not match:
        return None

    # Take everything from the header to end-of-message. If the model emits
    # additional `## ...` sections after the spec we'd lose them, but in
    # practice the spec is the last thing in the message.
    spec_block = message_text[match.start():].strip()

    refined_intent = _extract_refined_intent(spec_block)
    return ExtractedSpec(raw=spec_block, refined_intent=refined_intent)


_REFINED_INTENT_RE = re.compile(
    r"\*\*Refined intent:?\*\*\s*(.+?)(?:\n\n|\n\*\*|$)",
    re.DOTALL | re.IGNORECASE,
)


def _extract_refined_intent(spec_block: str) -> str:
    """Pull just the refined-intent paragraph out of a spec block.

    Falls back to the first non-empty paragraph after the header if the
    model didn't follow the format exactly — we'd rather pass *something*
    to the intent service than refuse the handoff.
    """
    match = _REFINED_INTENT_RE.search(spec_block)
    if match:
        return match.group(1).strip()

    # Fallback: first non-empty line/para after the header.
    lines = spec_block.splitlines()
    after_header = []
    found_header = False
    for line in lines:
        if not found_header:
            if _SPEC_HEADER_RE.match(line):
                found_header = True
            continue
        stripped = line.strip()
        if stripped:
            after_header.append(stripped)
            if len(after_header) >= 3:
                break
    return " ".join(after_header)


def _read_project_context(project_path: Optional[str]) -> str:
    """Pull a short "what this codebase is" blurb from RESONANT.md or
    AGENTS.md so the grilling model doesn't make wrong assumptions about
    deployment target / language / architecture.

    Returns up to ~1500 chars from the first matching file. Empty string
    if neither exists or the project_path is invalid.
    """
    if not project_path:
        return ""
    root = Path(project_path)
    if not root.is_dir():
        return ""
    # Prefer RESONANT.md (this app's convention) then fall back to the
    # broader AGENTS.md / CLAUDE.md conventions used by other agentic
    # tools so we benefit from whatever the user already wrote.
    for filename in ("RESONANT.md", "AGENTS.md", "CLAUDE.md"):
        candidate = root / filename
        if candidate.is_file():
            try:
                text = candidate.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            return text.strip()[:1500]
    return ""


def format_grill_first_message(
    feature_description: str,
    project_path: Optional[str] = None,
) -> str:
    """Build the first user message that kicks off a Mission's grill phase.

    Combines: the interviewer prompt + a project-context block (so the
    model doesn't invent wrong assumptions about what kind of app this
    is — Tier-1 fix #4) + the user's seed description.
    """
    seed = (feature_description or "").strip()
    if not seed:
        seed = "(the user did not provide a description — start by asking what they want to build)"

    context_blurb = _read_project_context(project_path)
    if context_blurb:
        context_block = (
            "\n## Project context\n\n"
            f"This is the codebase you'll be grilling about — verbatim from "
            f"`RESONANT.md` / `AGENTS.md` at the project root:\n\n"
            f"---\n{context_blurb}\n---\n"
        )
    else:
        context_block = (
            "\n## Project context\n\n"
            f"Project path: `{project_path or '(unknown)'}`\n"
            "No RESONANT.md / AGENTS.md was found at the root, so check "
            "with the `glob` tool before assuming what kind of app this is.\n"
        )

    return (
        _GRILL_ME_BASE_PROMPT
        + context_block
        + "\n---\n\nThe feature/product the user wants you to grill them about:\n\n"
        + seed
    )
