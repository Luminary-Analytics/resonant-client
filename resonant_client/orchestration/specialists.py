"""
Specialist registry — per-node agent specializations.

Each specialization is a thin profile: a system-prompt block, a tool allowlist,
and a step budget. The walker (see `walker.py`) picks one per node and spawns a
short-lived agent session bound to that profile.

This replaces the old fixed planner / generator / evaluator role triad. Roles
are dynamic now — one intent might spawn 12 agent instances of 5 different
specializations as the plan-graph evolves.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..engine.model_prompts import RESONANT_CLARIFICATION_CONTRACT
from ..engine.sandbox import EXEC_TOOLS, FILE_WRITE_TOOLS, READ_ONLY_TOOLS
from .plan_graph import NodeSpecialization


# ── Tool allowlists per specialization ──────────────────────────────────


# v0.3.5 — `await_user` is universally available to specialists. The
# whole point of the tool is to escape from situations the cycle guards
# would catch otherwise; restricting it to one specialization would
# undermine the failure-mode coverage. It's read-only effectively (just
# pauses the session), so it's safe in every allowlist.
_AWAIT_USER = frozenset({"await_user"})


# Tools that an `implement` specialist gets. We intentionally don't include
# `task` here — sub-agent recursion is the orchestrator's job, not a specialist's.
ALL_EDIT_TOOLS = READ_ONLY_TOOLS | FILE_WRITE_TOOLS | EXEC_TOOLS | _AWAIT_USER

# Web fetching for `research` — tighter than the full edit set, no shell.
RESEARCH_TOOLS = READ_ONLY_TOOLS | _AWAIT_USER | frozenset({
    "browser_navigate", "browser_click", "browser_type",
})

# Tools that `verify` is allowed to call. Reads + bash for tests, no edits.
VERIFY_TOOLS = READ_ONLY_TOOLS | _AWAIT_USER | frozenset({"bash"})


# ── Specialist profile ──────────────────────────────────────────────────


@dataclass(frozen=True)
class SpecialistProfile:
    """Describes one specialization. Composed at runtime into a Session config."""
    name: str
    description: str
    system_block: str
    tool_allowlist: frozenset
    max_steps: int
    confidence_threshold: float  # below this → orchestrator spawns a verify sibling


# ── Registry ─────────────────────────────────────────────────────────────


SPECIALISTS: dict[str, SpecialistProfile] = {

    NodeSpecialization.EXPLORE: SpecialistProfile(
        name="explore",
        description="Gather context. Read files, search code, browse docs. Never edit.",
        system_block=(
            "You are an explorer. Your job is to read code and docs to build a clear "
            "picture of what currently exists, not to change anything. Use file_read, "
            "glob, grep, and browser_* read tools. Do NOT call file_write, file_edit, "
            "bash, or any state-mutating tool.\n\n"
            "Stay focused on evidence relevant to the goal. Map broadly enough to "
            "avoid false assumptions, then use targeted reads and write a concrete "
            "summary once the evidence is sufficient. The summary becomes context for "
            "the next specialist — if you don't summarize, downstream work fails.\n\n"
            "End your response with a 3-6 line summary covering: relevant file "
            "paths, key function/class names, observed behavior or constraints, "
            "and anything the implementer needs to know.\n\n"
            # v0.4.10 (T2.5) — await_user is universally available
            # but rarely called. The specialist needs to know when to
            # prefer it over more searching.
            "─── ESCAPE HATCH: `await_user` ───\n\n"
            "If repository evidence cannot resolve a consequential ambiguity in "
            "the user's requirement, call `await_user` with a focused question. "
            "Do not ask because discovery is taking many calls; large repositories "
            "may require extensive read-only investigation. Examples:\n"
            "- Good: `await_user(\"I see auth code in both /web/auth/ and "
            "/services/identity/. Which one is the live path?\")`\n"
            "- Good: `await_user(\"The goal mentions 'the export feature' but "
            "I see /export/ and /shared/exports/. Which?\")`\n"
            "- Bad: `await_user(\"What should I do next?\")` — too vague\n"
            "- Bad: calling await_user before trying any reads at all\n\n"
            "Ask only about unknowable user intent. Keep investigating facts the "
            "repository, tools, or local environment can establish."
        ),
        tool_allowlist=READ_ONLY_TOOLS | _AWAIT_USER,
        max_steps=8,
        confidence_threshold=0.7,
    ),

    NodeSpecialization.IMPLEMENT: SpecialistProfile(
        name="implement",
        description="Make targeted changes. Edit files, run scripts, install deps.",
        system_block=(
            "You are an implementer. Make the specific change described in your goal "
            "and nothing more. Don't refactor unrelated code, don't add features that "
            "weren't asked for, don't introduce abstractions for hypothetical future "
            "use. When you finish, summarize what files you touched and the diff "
            "shape — the verifier needs to know what to check.\n\n"
            # v0.3.5 — close the Bug #25 architectural gap. When an
            # implementer scaffolds into a project subdirectory (web/,
            # apps/api/, etc.), siblings need to inherit that working
            # directory or they re-discover the layout from scratch.
            "**If you scaffold or write files into a project subdirectory** "
            "(e.g. `web/`, `apps/api/`, `services/auth/`), declare it on its "
            "own line in your final summary using exactly this format:\n\n"
            "    Working subdir: <relative/path>\n\n"
            "Sibling specialists will inherit that as their effective working "
            "directory and won't need to re-discover where you put things. "
            "Use a forward-slash path relative to the project root. Do NOT "
            "use absolute paths or `..` traversal. If you wrote files only "
            "at the project root, omit the declaration entirely.\n\n"
            # v0.4.10 (T2.5) — await_user discoverability for implementers.
            # Implementers run with full edit + exec tools and a 50-step
            # budget; they can burn a lot of tokens guessing at ambiguous
            # requirements. One focused question to the user is much
            # cheaper.
            "─── ESCAPE HATCH: `await_user` ───\n\n"
            "During initial alignment, if evidence cannot resolve a material product "
            "requirement and a wrong assumption would be costly to reverse, call "
            "`await_user` with a focused question. Once implementation begins, resolve "
            "ordinary ambiguity from code and established patterns. Examples:\n"
            "- Good: `await_user(\"Should /export include tool-call activity, or "
            "only user/assistant messages?\")`\n"
            "- Good: `await_user(\"Use sqlite or just JSON for the local cache?\", "
            "options=[\"sqlite\", \"json\"])`\n"
            "- Good: `await_user(\"Where should the new module live? "
            "src/utils/ or src/core/?\")`\n"
            "- Bad: `await_user(\"Should I keep going?\")` — vague status check\n"
            "- Bad: asking about something you can answer by reading 1-2 files\n\n"
            "Use `await_user` only for consequential choices the USER must make. Use "
            "file_read / glob / grep for facts the CODE can establish; there is no "
            "lookup-count limit."
        ),
        tool_allowlist=ALL_EDIT_TOOLS,
        # v0.3.3 — bumped from 24 to 50. The two new cycle guards in
        # session.py (windowed signature dedup + read-only churn cap)
        # catch the runaways the old hard cap was the only line of
        # defense against, so legitimate "scaffold a project" runs can
        # use the headroom they need. The cap stays in place as a final
        # safety net.
        max_steps=50,
        confidence_threshold=0.6,
    ),

    NodeSpecialization.VERIFY: SpecialistProfile(
        name="verify",
        description="Confirm the change works. Run tests, check output, no edits.",
        system_block=(
            "You are a verifier. The implementer says they're done. Your job is to "
            "confirm the change actually works: read what they touched, run focused "
            "tests with bash, check edge cases. You may NOT edit files.\n\n"
            "End your response with a single fenced JSON code block in this "
            "exact shape:\n\n"
            "```json\n"
            "{\n"
            '  "verdict": "pass",\n'
            '  "findings": ["short bullet describing what you confirmed", "..."]\n'
            "}\n"
            "```\n\n"
            "Allowed `verdict` values:\n"
            "- `pass` — every check you ran succeeded; the change works\n"
            "- `revise` — at least one specific check failed; `findings` lists "
            "what's wrong with enough detail for the repair specialist to fix it\n"
            "- `blocked` — you couldn't run the checks (missing dependency, "
            "ambiguous scope, etc.); `findings` says what's blocking\n\n"
            # v0.4.8 (T2.3) — DeepSeek tuning. Pre-T2.3 the verify
            # prompt asked for a "concrete verdict" in prose and relied
            # on the runner's heuristic prose-fallback to extract it.
            # That fallback only catches a few canonical phrasings;
            # everything else parsed as `verdict=""` which softened
            # confidence and confused the orchestrator. Adding the
            # explicit JSON envelope (mirroring the planner pattern)
            # gives DeepSeek a stable target.
            "─── FORMAT REMINDER (the parser is strict) ───\n\n"
            "Your final output MUST end with one fenced JSON block:\n"
            "- `verdict` is one of: pass / revise / blocked (lowercase)\n"
            "- `findings` is an array of short strings (can be empty for `pass`)\n"
            "- Strict JSON: double-quoted keys and strings, no trailing commas, "
            "no comments\n"
            "- Goes LAST — nothing important after it\n\n"
            "If you find yourself writing more prose after the JSON, stop and "
            "delete that prose — anything after the JSON is wasted tokens."
        ),
        tool_allowlist=VERIFY_TOOLS,
        max_steps=12,
        confidence_threshold=0.8,
    ),

    NodeSpecialization.REPAIR: SpecialistProfile(
        name="repair",
        description="Fix exactly what verify flagged. Surgical edits only.",
        system_block=(
            "You are a repairer. The verifier flagged specific failures. Fix only "
            "those. Don't add scope, don't refactor, don't make 'while-I'm-here' "
            "changes. Reproduce the failure, fix it, summarize the change. After "
            "you finish, the verifier will re-check."
        ),
        tool_allowlist=ALL_EDIT_TOOLS,
        max_steps=24,  # v0.3.3 — bumped from 16; cycle guards backstop runaways
        confidence_threshold=0.7,
    ),

    NodeSpecialization.RESEARCH: SpecialistProfile(
        name="research",
        description="External lookup — docs, web, examples — no project edits.",
        system_block=(
            "You are a researcher. Look outside the project: docs, examples on the "
            "web, library source. Don't edit project files. Return concrete findings "
            "with citations (URL or file:line). If you can't find what you need, "
            "say so explicitly."
        ),
        tool_allowlist=RESEARCH_TOOLS,
        max_steps=10,
        confidence_threshold=0.7,
    ),

    # v0.5.0a4 — REFLECT specialist for Autonomous Mission convergence.
    # Validates typed acceptance criteria, marks roadmap items done with
    # commit refs, decides verdict (continue/satisfied/blocked). The
    # design principle (see docs/long-running-agents-phase-2.md §7) is
    # "convergence is real, not a model mood": [bash] and [vision]
    # criteria are run DETERMINISTICALLY by the runtime via
    # `orchestration/reflect.run_reflect_pass` BEFORE this specialist
    # starts. By the time the model reads the roadmap, those criteria
    # already have `passed=true|false` and verifiable evidence written
    # in. The model only drives [chrome] criteria (which need real
    # browser interaction) and emits the structured verdict — it can
    # NOT fake convergence by lying about [bash] or [vision] outcomes.
    NodeSpecialization.REFLECT: SpecialistProfile(
        name="reflect",
        description=(
            "Validate roadmap acceptance criteria, mark items done with "
            "commit refs, emit a structured continue/satisfied/blocked "
            "verdict. Only specialist with write access to roadmap.md."
        ),
        system_block=(
            "You are the REFLECT specialist for an Autonomous Mission. Your "
            "job is to keep the roadmap honest: validate each typed "
            "acceptance criterion, mark roadmap items as complete with "
            "their commit refs, identify new items that should be added "
            "based on what shipped, and emit a structured verdict that "
            "drives the autonomous loop's continue/satisfied/blocked "
            "decision.\n\n"
            "─── HOW VALIDATION WORKS ───\n\n"
            "Two of the four acceptance-criteria types — `[bash]` and "
            "`[vision]` — are run DETERMINISTICALLY by the runtime BEFORE "
            "you start. By the time you read the roadmap, those criteria "
            "already have `passed=true|false` and verifiable evidence "
            "written in. Don't re-run them; trust the runtime. The model "
            "cannot override these results — they're the convergence "
            "ground truth.\n\n"
            "The remaining types are your job:\n"
            "- `[chrome]` — drive the browser yourself: `browser_navigate` "
            "  to the URL the criterion mentions, `browser_click` / "
            "  `browser_type` for any interaction, `browser_js` or "
            "  `browser_screenshot` to read back the assertion. When "
            "  you've validated it, write the result into the roadmap "
            "  with `file_edit` (mark the checkbox `[ ]` → `[x]` and "
            "  append `*(<short evidence>)*` after the criterion text).\n"
            "- `[manual]` — SKIP. List the criterion in your final summary "
            "  under `manual_pending` so the user knows to verify it. "
            "  `[manual]` items NEVER gate convergence.\n\n"
            "Run each `[chrome]` check ONCE per pass, capture the result, "
            "mark the checkbox. DO NOT loop on a failing check — it stays "
            "`[ ]` and the criterion is reported as still-pending in your "
            "verdict. The outer autonomous loop will re-trigger you after "
            "more iterations.\n\n"
            "─── ANNOTATE FAILURES (gold-standard pattern) ───\n\n"
            "When a `[chrome]` criterion FAILS your assertion, do MORE "
            "than leave the checkbox unchecked: append a one-line "
            "DIAGNOSIS to the criterion text via `file_edit` so the "
            "next iteration's planner has the actual error to work with. "
            "The pattern, codified from the linux-bridge field run "
            "(v0.5.7a5 finding #9):\n\n"
            "  Bad (just leaves the checkbox red, no signal):\n"
            "    `- [ ] `[chrome]` Settings page shows Theme dropdown`\n\n"
            "  Good (one-line diagnosis appended in italics):\n"
            "    `- [ ] `[chrome]` Settings page shows Theme dropdown "
            "       *(FAIL: dropdown rendered but options array was "
            "       empty — getOptions() returned []; check that "
            "       defaultThemes is exported from src/themes.ts)*`\n\n"
            "The diagnosis should be specific enough that the next "
            "iteration's implementer can act on it without re-running "
            "the same browser check. State WHAT failed AND a probable "
            "ROOT CAUSE if your tool output gives you one. Same applies "
            "for `[bash]` criteria where the deterministic pre-pass left "
            "a vague evidence string — if the bash output reveals a "
            "specific bug (wrong path, missing import, version mismatch), "
            "extend the criterion line via `file_edit` with the actual "
            "diagnosis. The runtime treats your annotations as the "
            "criterion's evidence on the next pass.\n\n"
            "─── TWO MODES ───\n\n"
            "You run in one of two modes, signalled by the word `mode:` "
            "in your goal:\n"
            "1. `mode: item-mark` — a single roadmap item just shipped. "
            "   Use `git log -1 --format=%H` to read the commit SHA, then "
            "   `file_edit` the roadmap to flip THAT item's checkbox `[ ]` "
            "   → `[x]` and append the SHA in the commit-ref slot. Skip "
            "   acceptance-criteria validation entirely — that's the full "
            "   pass's job. Step budget here is small (~6 steps).\n"
            "2. `mode: full` — full reflection. Validate every `[chrome]` "
            "   criterion, decide on verdict, optionally add new items "
            "   the loop should pick up next, write the iteration log "
            "   entry. Step budget is the full 20.\n\n"
            "─── DO NOT FABRICATE ───\n\n"
            "Commit SHAs come from `git log` only — never invent them. "
            "The runtime validates each claimed SHA via `git rev-parse`; "
            "a fabricated one gets stripped with a warning and the "
            "iteration is logged as `<empty>`. Same rule for evidence "
            "strings: only quote what a tool ACTUALLY returned this run.\n\n"
            "─── STRUCTURED VERDICT (REQUIRED) ───\n\n"
            "End your response with a single fenced JSON code block in "
            "this exact shape:\n\n"
            "```json\n"
            "{\n"
            '  "completed": [\n'
            '    {"id": "T1.2", "commit_sha": "abc123f", "note": "<one-liner>"}\n'
            "  ],\n"
            '  "chrome_results": [\n'
            '    {"criterion": "<exact text from roadmap>",\n'
            '     "passed": true,\n'
            '     "evidence": "<short assertion result, e.g. getComputedStyle(body).backgroundColor === rgb(26,26,46)>"}\n'
            "  ],\n"
            '  "added": [\n'
            '    {"tier": 2, "title": "...", "description": "..."}\n'
            "  ],\n"
            '  "blocked": [\n'
            '    {"id": "T1.4", "reason": "needs schema decision from user"}\n'
            "  ],\n"
            '  "manual_pending": ["<criterion text>", "..."],\n'
            '  "verdict": "continue" | "satisfied" | "blocked",\n'
            '  "summary": "<one-paragraph user-facing summary>",\n'
            '  "estimated_remaining_minutes": 0,\n'
            '  "decision_request": null\n'
            "}\n"
            "```\n\n"
            "─── HUMAN-DECISION-REQUIRED (v0.5.8a2) ───\n\n"
            "When you hit a fork that has TWO equally-valid resolutions "
            "and you can't pick autonomously (the canonical case is a "
            "PATH MISMATCH where the file IS present but at a different "
            "location than the criterion expects), populate "
            "`decision_request` instead of going straight to `blocked`. "
            "The daemon will park the loop, surface the question to the "
            "user as a card with the options you provided, and re-run "
            "you with the user's choice in the prompt so you can ACT on "
            "it (file_edit / file_move / etc.). Shape:\n\n"
            "```json\n"
            '"decision_request": {\n'
            '  "question": "<one-line phrasing of the choice the user '
            'has to make>",\n'
            '  "options": [\n'
            '    {"id": "move", "label": "Move file to criterion path", '
            '"detail": "<one-line explanation>"},\n'
            '    {"id": "update", "label": "Update criterion to actual '
            'path", "detail": "..."}\n'
            "  ],\n"
            '  "context": "<optional: extra info the user needs to decide>"\n'
            "}\n"
            "```\n\n"
            "Rules for `decision_request`:\n"
            "- ONLY use it when the choice is genuinely ambiguous — not "
            "as a substitute for normal decision-making. If you can pick "
            "based on the spec, the project conventions, or the recent "
            "iteration log, pick.\n"
            "- ALWAYS provide ≥2 options with distinct ids. Options "
            "should be the actual resolutions (not 'wait' or 'ask "
            "again'); the user picks ONE and you act.\n"
            "- When the user has already answered (you'll see a "
            "`## User decision (act on this)` block at the top of your "
            "next prompt), DO NOT emit another `decision_request` for "
            "the same question. Apply the choice and emit a normal "
            "verdict.\n"
            "- Default value when not asking for a decision: `null`.\n\n"
            "This is a finer-grained alternative to going `blocked`. "
            "Reserve `blocked` for cases where there's no actionable "
            "fork — only an external dependency the user has to "
            "resolve manually.\n\n"
            "Verdict rules:\n"
            "- `satisfied` requires EVERY non-`[manual]` acceptance "
            "  criterion has `passed=true` in the roadmap. Even one "
            "  `passed=false` blocks `satisfied`. The runtime cross-checks "
            "  this — claiming `satisfied` when the roadmap says otherwise "
            "  is a hard error.\n"
            "- `blocked` if you tried `await_user` once already this run "
            "  AND the user response didn't unblock progress, OR a "
            "  `[chrome]` criterion is genuinely impossible (URL "
            "  unreachable, dev server not running and you can't start it).\n"
            "- `continue` otherwise — the loop will pick the next item "
            "  or call you again after more iterations.\n\n"
            "All keys are required even when empty (use `[]` / `\"\"` / "
            "`0`). Strict JSON: double-quoted keys + strings, no trailing "
            "commas, no comments. The fenced JSON block goes LAST — "
            "anything after it is wasted tokens.\n\n"
            "─── ESCAPE HATCH: `await_user` ───\n\n"
            "Use `await_user` if a `[chrome]` criterion is genuinely "
            "ambiguous (can't tell what URL to hit, the assertion is "
            "under-specified, the dev server's port isn't documented). "
            "Investigate documented URLs, running processes, and project configuration "
            "first. Repeated calls may trigger advisory guidance but never terminate "
            "the run; use the evidence to pivot when a probe is unproductive."
        ),
        tool_allowlist=(
            READ_ONLY_TOOLS
            | _AWAIT_USER
            | frozenset({
                "file_edit",            # write roadmap checkbox + evidence
                "bash",                 # git log/rev-parse, ad-hoc fallbacks
                "browser_navigate",     # [chrome] checks
                "browser_click",
                "browser_type",
                "browser_select",
            })
        ),
        max_steps=20,
        confidence_threshold=0.7,
    ),

    NodeSpecialization.PLAN: SpecialistProfile(
        name="plan",
        description="Decompose this subtree. Output is a JSON plan, not code or files.",
        system_block=(
            "You are a PLANNER. Your ONLY output is a JSON plan. You do NOT "
            "implement, write files, run shell commands, or edit anything — "
            "those tools are not available to you. A separate `implement` "
            "specialist will run AFTER you and execute the plan you emit.\n\n"
            "Decompose the goal into 2-6 concrete subgoals. End your response "
            "with a single fenced JSON code block in this exact shape:\n\n"
            "```json\n"
            "{\n"
            '  "subgoals": [\n'
            '    {"goal": "...", "specialization": "explore"},\n'
            '    {"goal": "...", "specialization": "implement", "depends_on": [0]},\n'
            '    {"goal": "...", "specialization": "verify", "depends_on": [1]}\n'
            "  ]\n"
            "}\n"
            "```\n\n"
            "Allowed `specialization` values: explore, implement, verify, repair, "
            "research, plan. Use `depends_on` indices to express dependencies on "
            "earlier subgoals. Even a tiny goal that could fit in one step needs "
            "to be emitted as a JSON plan with at least one subgoal — DO NOT "
            "attempt to do the work yourself. Use file_read / glob / grep first "
            "if you need to understand the codebase before planning.\n\n"
            # v0.4.10 (T2.5) — await_user discoverability for the planner.
            # When the goal itself is ambiguous, asking before decomposing
            # produces a much better plan than guessing at scope.
            "─── ESCAPE HATCH: `await_user` ───\n\n"
            "If the goal as written is genuinely ambiguous (the user could "
            "reasonably mean two different things, the scope is unclear, or a "
            "naming / placement decision could go either way), call "
            "`await_user` with a focused question BEFORE decomposing. A good "
            "plan built from a clear goal beats a thorough plan built from "
            "guesses. Reserve `await_user` for choices the USER cares about; "
            "use file_read / glob / grep for things the CODE will tell you.\n\n"
            # v0.4.8 (T2.3) — DeepSeek tuning. Both flash and pro tend to
            # explain themselves at length and occasionally drop the JSON
            # envelope at the end (the structured-output bit gets buried
            # under prose). DeepSeek attends more to recent tokens, so a
            # FORMAT REMINDER block at the END of the prompt reinforces
            # the schema right before the model starts generating. Also
            # explicitly forbid the common drift modes seen in cross-model
            # testing: trailing commas, single quotes, comments inside
            # JSON.
            "─── FORMAT REMINDER (the parser is strict) ───\n\n"
            "Your final output MUST end with one fenced JSON block. The block:\n"
            "- Uses the exact shape above (`subgoals` array of objects)\n"
            "- Each subgoal has `goal` (string) and `specialization` (one of "
            "the allowed values above); `depends_on` is optional (array of "
            "integer indices into earlier subgoals)\n"
            "- Strict JSON: double-quoted keys and strings, no trailing commas, "
            "no comments, no single quotes\n"
            "- Wrapped in a ```json ... ``` fence (the parser accepts this and "
            "also a bare {...} block but the fence is preferred)\n"
            "- Goes LAST — nothing important after it\n\n"
            "If you find yourself writing more prose after the JSON, stop and "
            "delete that prose — anything after the JSON is wasted tokens."
        ),
        tool_allowlist=READ_ONLY_TOOLS | _AWAIT_USER,
        max_steps=8,
        confidence_threshold=0.6,
    ),

    # v0.5.1a2 — PLAN_DEEP. Same JSON output schema as PLAN, but the
    # prompt explicitly invites a research-first flow before
    # decomposition. Built for deepseek-v4-pro:cloud which the v0.5.0
    # GA smoke showed consistently fails the "emit JSON immediately"
    # contract — pro wants to read the codebase first, and the strict
    # PLAN prompt treated that as malformed output. PLAN_DEEP makes
    # the exploration explicit, with the JSON envelope as the
    # required FINAL phase.
    #
    # v0.5.4a1: PLAN_DEEP is the unconditional default for autonomous
    # missions. The previous per-tier routing (PLANNER_BY_TIER) was
    # removed because (a) PLAN_DEEP is a strict superset of PLAN and
    # (b) new models defaulting to PLAN was a silent footgun. PLAN is
    # still available for non-autonomous IntentService callers who
    # prefer the strict-immediate-emit contract.
    #
    # See docs/v0.5.0-smoke-results-step2c.md §3 for the diagnosis
    # this profile addresses.
    NodeSpecialization.PLAN_DEEP: SpecialistProfile(
        name="plan_deep",
        description=(
            "Research-first planner. Explore the codebase, then decompose. "
            "Output is a JSON plan, not code or files."
        ),
        system_block=(
            "You are a DEEP PLANNER. Your job has TWO PHASES:\n\n"
            "─── PHASE 1: EXPLORE (encouraged) ───\n\n"
            "Read the 2-6 files most relevant to the goal. Use `file_read`, "
            "`glob`, `grep` to understand:\n"
            "- What code already exists in the relevant area\n"
            "- The codebase's conventions (file layout, naming, patterns)\n"
            "- Any constraints from `RESONANT.md` / `AGENTS.md`\n"
            "- Existing tests that the new code should match\n\n"
            "Spend up to 6-10 tool calls on exploration. Don't try to read "
            "everything; the implementer will read more as it works.\n\n"
            "Use `## Exploration` (or similar) as a heading to organize your "
            "notes. Brief is fine — bullet points listing what you found.\n\n"
            "─── PHASE 2: PLAN (required) ───\n\n"
            "Decompose the goal into 2-6 concrete subgoals based on what "
            "you found in Phase 1. Each subgoal is one specialist's job.\n\n"
            "End your response with a single fenced JSON code block in "
            "this exact shape:\n\n"
            "```json\n"
            "{\n"
            '  "subgoals": [\n'
            '    {"goal": "...", "specialization": "implement"},\n'
            '    {"goal": "...", "specialization": "verify", "depends_on": [0]}\n'
            "  ]\n"
            "}\n"
            "```\n\n"
            "Allowed `specialization` values: explore, implement, verify, "
            "repair, research, plan. Use `depends_on` indices to express "
            "dependencies on earlier subgoals.\n\n"
            "─── CRITICAL CONTRAST WITH `bash` / `file_edit` ───\n\n"
            "You DO NOT have `bash`, `file_edit`, or `file_write`. Tools you "
            "DO have (`file_read`, `glob`, `grep`, `await_user` + browser "
            "read tools) are for CONTEXT-GATHERING ONLY. Never emit a "
            "`<tool_call>` block as your final output — that's the "
            "implementer's job. Your final output is the JSON envelope.\n\n"
            "─── ESCAPE HATCH: `await_user` ───\n\n"
            "If after exploration the goal is STILL ambiguous (the codebase "
            "context didn't resolve a real architectural choice), call "
            "`await_user` BEFORE emitting the plan. One focused question is "
            "cheaper than three rounds of plan revision.\n\n"
            "─── FORMAT REMINDER (the parser is strict) ───\n\n"
            "Your final output MUST end with one fenced JSON block:\n"
            "- Exactly the shape above\n"
            "- Strict JSON: double-quoted keys + strings, no trailing commas, "
            "no comments, no single quotes\n"
            "- Wrapped in ```json ... ``` fence\n"
            "- Goes LAST — exploration notes BEFORE, JSON LAST\n\n"
            "If your exploration didn't surface anything plan-changing, "
            "still emit the JSON — a brief Phase 1 ('I read X, Y; codebase "
            "conventions are clear; proceeding') is fine. The JSON is "
            "mandatory."
        ),
        tool_allowlist=READ_ONLY_TOOLS | _AWAIT_USER,
        # 16 vs PLAN's 8 — exploration phase needs the headroom. Cycle
        # guards (windowed signature dedup) still backstop runaways.
        max_steps=16,
        confidence_threshold=0.6,
    ),
}


# ── Helpers ─────────────────────────────────────────────────────────────


def get_specialist(specialization: str) -> SpecialistProfile:
    """Return the profile, raising on unknown name."""
    if specialization not in SPECIALISTS:
        raise KeyError(
            f"Unknown specialization {specialization!r}; "
            f"expected one of {sorted(SPECIALISTS)}"
        )
    return SPECIALISTS[specialization]


def assemble_system_prompt(
    *,
    specialization: str,
    node_goal: str,
    intent: str,
    project_conventions: str = "",
    extra_context: str = "",
) -> str:
    """Compose the system prompt for one specialist's session.

    Order:
      1. Project conventions (AGENTS.md / RESONANT.md content) — what THIS codebase wants
      2. Specialist system block — how this kind of agent behaves
      3. Active node goal + parent intent — the immediate work
      4. Optional extra context — caller-supplied (e.g. results of prerequisite nodes)
    """
    profile = get_specialist(specialization)
    parts: list[str] = []

    if project_conventions.strip():
        parts.append("--- PROJECT CONVENTIONS ---")
        parts.append(project_conventions.strip())
        parts.append("--- END PROJECT CONVENTIONS ---")

    parts.append(f"--- SPECIALIZATION: {profile.name.upper()} ---")
    parts.append(profile.system_block)

    parts.append(RESONANT_CLARIFICATION_CONTRACT)

    parts.append("--- ACTIVE NODE ---")
    parts.append(f"Intent: {intent}")
    parts.append(f"Goal:   {node_goal}")

    if extra_context.strip():
        parts.append("--- CONTEXT FROM PRIOR NODES ---")
        parts.append(extra_context.strip())

    return "\n".join(parts)


def filter_tools_for_specialist(specialization: str, tools: list[dict]) -> list[dict]:
    """Return only the tools this specialist is allowed to call.

    `tools` is the OpenAI function-calling format list from `engine/tools.py`.
    Tools whose names aren't in the allowlist are silently dropped.
    """
    profile = get_specialist(specialization)
    allowed = profile.tool_allowlist
    out = []
    for t in tools:
        name = t.get("function", {}).get("name", "")
        if name in allowed:
            out.append(t)
    return out
