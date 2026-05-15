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
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from ..gui.roadmap import AcceptanceCriterion


# ── The prompt ──────────────────────────────────────────────────────────
#
# Sent as the first user message when a Mission starts. The model adopts
# the interviewer persona and runs the Q&A in subsequent turns. We pass
# this in user content rather than the system prompt so we don't have to
# touch every backend's prompt assembly path.

_GRILL_ME_BASE_PROMPT = """You are an expert product interviewer. Your job is to grill the user
relentlessly about a feature or product they want to build, until you
have a *shared understanding* clear enough to hand to a build team.

## Rule 0 — you are an INTERVIEWER, never an IMPLEMENTER

This rule has precedence over everything below. Read it twice.

You have read-only codebase tools (`glob`, `file_read`, `grep`) for
RESEARCH only. You must **never** use `file_edit`, `file_write`,
`bash`, or any other mutating tool to do the work yourself — not even
when the task looks small enough that doing it directly would obviously
be faster. The build team (an autonomous loop) does the work. You do
not. Your one and only deliverable is the `## Final spec` block.

If the user's request turns out to be a **concrete one-shot task**
rather than an open-ended feature — e.g. "append this line to file X",
"rename function Y to Z", "bump the version string" — do NOT refuse,
and do NOT silently do it yourself. Both of those dead-end the mission:
self-executing leaves the mission stuck in the drafting phase forever,
and the build/verify/skill-extraction loop never runs.

Instead: ask one or two quick clarifying questions if anything is
genuinely ambiguous, then emit a **minimal** `## Final spec`. A small
concrete task legitimately needs only 1–3 acceptance criteria — pin the
exact expected end-state (file contents, command output, etc.) and let
the loop execute + verify it. A one-line task still goes through the
loop; that is how it gets verified, recorded, and observed by the
self-improvement system.

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


# ── Rigorous-mode addendum (Autonomous Mission) ────────────────────────
#
# Appended to the base prompt when `autonomous=True` is passed to
# `format_grill_first_message`. The autonomous loop runs unattended for
# hours against this spec, so the cost of a vague spec is much higher
# than in an interactive session. We trade extra grilling time up front
# for a roadmap with binary, type-tagged acceptance criteria and a
# committed time budget. "Measure twice, cut once."

_RIGOROUS_GRILL_ADDITIONS = """

## Autonomous Mission addendum (rigorous mode)

This is an **Autonomous Mission**: when the spec is accepted, the model
will run unattended for hours against this spec. The user is going to
bed / lunch / a meeting. If the spec is wrong, the model will *converge
on the wrong thing* — and discover that fact only when the user wakes
up. Measure twice, cut once.

So: grill harder than you would for an interactive session.

### Extra rules for rigorous mode

R1. **Question count is 10–25, not 5–15.** Don't stop at "I think I get
    it" — autonomous runs are expensive to redo. If you find yourself
    ready to emit a spec after 6 questions, that's a signal to dig in
    harder on edge cases, failure modes, and what "done" actually means.
    **At least 3 of your questions must probe edge cases or failure
    modes** (not just happy-path scoping). Examples:
    - "What should happen when the input file is empty?"
    - "What's the expected error message when the user passes an
      invalid argument?"
    - "How should this handle UTF-8 vs Windows-1252 encoded inputs?"

R2. **Acceptance criteria must be binary, type-tagged, AND test
    BEHAVIOR (not just existence).** Each criterion MUST be:
    - **Binary**: pass or fail, nothing in between. "It feels snappy"
      is not binary. "p95 search latency under 200ms when measured by
      `bash` against the loaded fixture" is binary.
    - **Type-tagged**: prefix every criterion with one of these tags
      so the runtime knows how to validate it:
      - `[bash]` — runnable shell assertion (exit code or output
        comparison). Wrap the command itself in inline backticks.
        Examples:
          - `` - `[bash]` `pytest tests/test_foo.py` exits 0 ``
          - `` - `[bash]` `wc -l < src/foo.py` output < 200 ``
      - `[chrome]` — visible/clickable behaviour in a browser, checked
        via Chrome MCP. Example:
          - `` - `[chrome]` Settings page shows a "Vision model" dropdown with at least one option ``
      - `[vision]` — visual property a human would eyeball, checked via
        a vision model screenshot pass. Example:
          - `` - `[vision]` After toggling dark mode, the page background renders near-black, not just an inverted icon ``
      - `[manual]` — only when no automation is feasible. Use sparingly;
        prefer the others. The runtime will skip these and surface them
        to the user at the end.

    **Critical: prefer criteria that test BEHAVIOR over criteria that
    test EXISTENCE.** Bad: `` `[bash]` `python wc.py foo.txt` exits 0 ``
    (only proves the script runs without crashing). Good:
    `` `[bash]` `python wc.py foo.txt` output == "2 6 30 foo.txt"  ``
    (proves the OUTPUT is correct given a known input). The first kind
    is satisfied by a script that prints "hello" — the second isn't.

R3. **Minimum of 4 binary acceptance criteria covering 4 distinct
    aspects.** A spec with one or two gives the autonomous loop almost
    no signal — it'll declare "done" the moment the easiest one
    passes.

    **Exception (see Rule 0):** a genuine one-shot concrete task —
    "append a line", "rename a symbol", "bump a version" — is not a
    feature and does not need 4 criteria. For those, 1–3 criteria that
    pin the exact end-state are correct and sufficient; do not pad to
    4 and do not loop the user asking for a fourth. This exception is
    ONLY for truly atomic tasks. Anything with branching behavior,
    multiple files, or "and also…" scope is a feature — hit the floor.

    For an actual feature, the 4-criterion floor should map to:
    1. **Happy path with concrete output** — a known input maps to a
       specific expected output. Pin the actual values.
    2. **Error / edge-case behavior** — what happens for
       invalid/missing/empty input? Pin the expected error or behavior.
    3. **Code-quality or constraint check** — `tsc --noEmit`, `! grep
       'TODO'`, line-count cap, no-banned-imports, etc.
    4. **Regression guard or integration check** — runs the broader
       test suite, hits the dev server, validates a [chrome] or
       [vision] surface if applicable.
    If you only have happy-path criteria, the implementer might ship
    something that "works once" but fails on the second input. Don't
    let that happen.

R4. **Probe for concrete output examples.** For any feature that
    produces output (CLI prints, API responses, file writes, UI
    text), ask: "When the user runs/triggers X with INPUT Y, what
    EXACTLY should they see?" Pin both the input AND the output
    verbatim. Vague "it should print the count" → specific "it should
    print `2 4 22 example.txt` for a 2-line file with 4 words and 22
    chars." This is what makes Form-A `output == X` criteria possible.

R5. **Greenfield vs refactor — ask early.** One of your first 3
    questions should establish whether this is:
    - **Greenfield** — new file(s), no existing code to integrate
      with. Implementer can decide structure freely.
    - **Refactor / extension** — modifies existing code. Implementer
      must respect existing conventions, dependencies, tests.
    This affects the autonomous loop's planner-selection downstream
    (refactor work benefits from a research-first planner). Capture
    the answer in the spec under `**Key assumptions:**` as e.g.
    `Greenfield (no existing code touched)` or `Extends
    src/foo/bar.py`.

R6. **Ask for a time budget near the end.** Once scope is roughly
    settled, ask the user how long the autonomous run should be allowed
    to take. Recommend a value based on scope. Format the question
    like:
        Question: How long should the autonomous run be allowed to go?
        My recommendation: 4h. Options: 1h, 4h, 6h, 8h, 12h, 24h, 48h,
        full auto.
    Capture the answer verbatim in the spec under `**Time budget:**`.

R7. **Don't pad the spec.** If you genuinely can't think of a fourth
    binary criterion after grilling, *stop and ask the user* — "I'm
    stuck thinking of a fourth measurable criterion for this. What
    would tell you, definitively, that this is done?" — rather than
    inventing a vague one to hit the count.

R8. **Question style: 1-2 sentences, not paragraphs.** Each question
    must be tight enough that the user can answer in one sitting.
    Long, multi-part questions invite vague multi-part answers. If
    you find yourself wanting to ask three things at once, ask the
    most important ONE thing and follow up with the others on
    subsequent turns.

### Question-format EXEMPLAR

Grill turns that produced consistently 5/5-rated answers in the
linux-bridge field run (codified for v0.5.7a5 from finding #12)
follow this 5-beat pattern:

1. **Acknowledge the previous answer in one short line.** Confirms
   you actually read it; sets the bridge for the next question. Not
   a recap — just enough to anchor.
2. **Bridge with motivation.** Explain WHY the next question
   matters — what downstream decision or risk it pins down. Avoids
   the user feeling like they're being interrogated for its own sake.
3. **Frame the question with concrete options (a / b / c).** Closed-
   form choices > open-ended "how do you want to handle X?". The
   user's job becomes "pick one or override", not "draft a design
   from scratch".
4. **Recommend ONE option with rationale.** Pick a default. Tell
   the user why. Saves them from the cognitive load of weighing
   every option from cold; lets them respond fast (often "yeah
   recommendation, ship it") OR override decisively.
5. **Invite override.** End with an explicit "if you'd rather X,
   say so" so the user knows the recommendation is a starting
   point, not a verdict.

Concrete example (a Q3-equivalent from a hypothetical CLI-tool
mission asking about argument parsing):

  Acknowledged — single-binary scope keeps the surface area
  manageable.

  Next question pins down the argument-parsing layer, which
  determines how easy it'll be to add subcommands later.

  How should the CLI handle arguments?
    a. Hand-rolled `sys.argv` parsing (smallest dependency, painful
       to extend)
    b. `argparse` (stdlib, supports subcommands, well-known idioms)
    c. `click` (cleaner ergonomics, third-party dependency)

  Recommendation: **b. argparse** — stdlib so no extra dep, and
  every Python dev recognizes the pattern. We can move to click
  later if subcommands proliferate.

  If you'd rather start with click for the better ergonomics, say
  so and I'll plan the spec around that instead.

A few extra notes on the pattern:

- **Active scope-narrowing is encouraged.** When the user's last
  answer hints at a direction that simplifies the next 3 questions,
  surface that as a synthesis-confirmation rather than a new
  question. Linux-bridge Q5b: after the user confirmed "drop games
  entirely from v0.1", the model offered a synthesized confirmation
  ("So v0.1 is productivity-app-only; we can defer GPU/DXVK/VKD3D
  to v0.2 — confirm?") instead of asking the games sub-questions.
- **Don't hedge your recommendation.** "I'd suggest maybe trying
  option b" is worse than "Recommendation: b. argparse — stdlib so
  no extra dep". Pick one, defend it, invite override. Hedging
  forces the user to pick anyway AND removes useful signal about
  what you actually think.
- **No filler.** Skip "Great question!" / "That's a really
  important consideration." The acknowledge line earns its keep
  by referencing the user's specific previous answer, not by
  praising the question.

### Spec-format additions for rigorous mode

The spec block must include one new subsection, `**Time budget:**`,
after `**Out of scope:**` and before `**Technical constraints:**`:

```
**Out of scope:**
- ...

**Time budget:** <one of: 1h, 4h, 6h, 8h, 12h, 24h, 48h, full auto>

**Technical constraints:**
- ...
```

…and `**Acceptance criteria:**` items use the type-tag format above
(at least 4 entries, each prefixed with one of `[bash]` / `[chrome]` /
`[vision]` / `[manual]` in inline backticks):

```
**Acceptance criteria:**
- `[bash]` `pytest tests/test_settings.py` exits 0
- `[bash]` `grep -c 'data-theme' static/styles.css` output > 5
- `[chrome]` Settings page shows a "Theme" dropdown with Light and Dark options
- `[vision]` After selecting Dark, the page background renders near-black
```

Aim for at least one `[bash]` criterion (cheapest to run repeatedly)
and at least one `[chrome]` or `[vision]` if the change touches
anything visible.
"""

_VISION_UNAVAILABLE_NOTE = """

**Vision model unavailable on this machine.** Do NOT emit any
`[vision]` acceptance criteria — the runtime can't validate them on
this host. Use `[chrome]` for any visible-behaviour check (it can
fall back to DOM-based assertions) or `[manual]` only as a last
resort.
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
    # Rigorous-mode (Autonomous Mission) extras. Empty / [] for
    # standard interactive specs that don't include them.
    time_budget: str = ""
    acceptance_criteria: list[AcceptanceCriterion] = field(default_factory=list)


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
    time_budget = extract_time_budget(spec_block)
    acceptance_criteria = extract_acceptance_criteria(spec_block)
    return ExtractedSpec(
        raw=spec_block,
        refined_intent=refined_intent,
        time_budget=time_budget,
        acceptance_criteria=acceptance_criteria,
    )


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


# ── Rigorous-mode parsers (Autonomous Mission) ─────────────────────────
#
# The rigorous spec format adds `**Time budget:**` and switches
# `**Acceptance criteria:**` to typed, inline-backtick-prefixed entries
# like `` - `[bash]` `pytest ...` exits 0 ``. These regexes pull those
# fields out for the planner / autonomous loop. They are intentionally
# strict — the prompt is explicit about format, and a silent mis-parse
# at hand-off time would let the autonomous loop run with no
# convergence ground truth. Strict regex → empty list → loud failure
# is preferable to fuzzy matching.

_SPEC_CRITERION_RE = re.compile(
    r"^-\s+`\[(bash|chrome|vision|manual)\]`\s+(.+?)\s*$",
    re.MULTILINE,
)


def extract_acceptance_criteria(spec_block: str) -> list[AcceptanceCriterion]:
    """Pull typed acceptance criteria out of the **Acceptance criteria:**
    subsection of a spec block.

    Returns an empty list if the section is missing, or if it exists but
    contains no lines matching the typed-tag format. Callers (e.g. the
    autonomous-loop bootstrap) can treat an empty list as a hard failure.
    """
    # Slice from `**Acceptance criteria:**` to the next bold subsection
    # (or end-of-block) so we don't accidentally pick up criterion-shaped
    # lines from elsewhere in the spec.
    section_match = re.search(
        r"\*\*Acceptance criteria:\*\*\s*\n(.*?)(?=\n\*\*[A-Za-z]|\Z)",
        spec_block,
        re.DOTALL | re.IGNORECASE,
    )
    if not section_match:
        return []
    section = section_match.group(1)

    criteria: list[AcceptanceCriterion] = []
    for type_tag, text in _SPEC_CRITERION_RE.findall(section):
        criteria.append(
            AcceptanceCriterion(type=type_tag, text=text.strip())
        )
    return criteria


_TIME_BUDGET_RE = re.compile(
    r"\*\*Time budget:\*\*\s*(.+?)\s*$",
    re.IGNORECASE | re.MULTILINE,
)


def extract_time_budget(spec_block: str) -> str:
    """Pull the **Time budget:** value out of a rigorous-mode spec block.

    Returns "" when absent (standard interactive specs don't include
    this subsection). Caller decides whether an empty value is OK
    (interactive) or a fatal misformat (autonomous handoff).
    """
    match = _TIME_BUDGET_RE.search(spec_block)
    if not match:
        return ""
    return match.group(1).strip()


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
    *,
    autonomous: bool = False,
    vision_available: bool = True,
) -> str:
    """Build the first user message that kicks off a Mission's grill phase.

    Combines: the interviewer prompt + a project-context block (so the
    model doesn't invent wrong assumptions about what kind of app this
    is — Tier-1 fix #4) + the user's seed description.

    When ``autonomous=True``, the rigorous-mode addendum is appended so
    the model:
    - Asks 10–25 questions instead of 5–15.
    - Demands binary, type-tagged acceptance criteria
      (`[bash]` / `[chrome]` / `[vision]` / `[manual]`).
    - Requires ≥4 binary criteria.
    - Asks the user for a time budget near the end and emits it as
      `**Time budget:**`.

    When ``vision_available=False`` (and autonomous is on), an extra
    note instructs the model not to emit `[vision]` criteria — the
    runtime can't validate them on this host without a vision model.
    The flag is a no-op in standard mode.
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

    prompt = _GRILL_ME_BASE_PROMPT
    if autonomous:
        prompt = prompt + _RIGOROUS_GRILL_ADDITIONS
        if not vision_available:
            prompt = prompt + _VISION_UNAVAILABLE_NOTE

    return (
        prompt
        + context_block
        + "\n---\n\nThe feature/product the user wants you to grill them about:\n\n"
        + seed
    )
