"""
Autonomous Mission roadmap — pure data layer.

v0.5.0a1 (Phase 2 of long-running-agents). See
`docs/long-running-agents-phase-2.md` for the full design.

This module defines the on-disk format for an autonomous mission's
roadmap and provides parser / writer / mutation helpers. It is
deliberately model-free, threading-free, and WS-free: every function
here takes a string-or-path in and returns a string-or-`Roadmap`-or-
`bool` out. The autonomous loop daemon (a later alpha) handles
threading and event emission; the REFLECT specialist (also later)
handles model interaction. THIS module is the source of truth for
the markdown shape and the in-memory shape — nothing else touches
the on-disk file directly.

Design principle ("measure twice, cut once" — see the design doc):
  * The roadmap.md file is the single source of truth for what's
    been done, what's pending, and what acceptance criteria gate
    convergence. Multiple processes / threads may read it; only
    REFLECT writes it (and a brief file lock during the write
    serializes against user hand-edits).
  * Acceptance criteria are TYPED (`[bash]` / `[chrome]` /
    `[vision]` / `[manual]`). The type tag is part of the criterion
    text in the markdown; the parser extracts it. REFLECT routes
    each criterion to the matching validation strategy.
  * Tier IDs are immutable. `T1.3` is `T1.3` for the lifetime of
    the mission. Items can move between tiers but their IDs stay.
  * Convergence = every non-`[manual]` acceptance criterion has
    `passed=True`. The model can't fake this — REFLECT must
    record evidence, and the runner verifies the check actually
    ran.

The on-disk format and section headers are documented in §6.2 of
the design doc; the parser regex is anchored on the same
conventions described there.
"""

from __future__ import annotations

import os
import re
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Iterator, Optional


# ── Type tags for acceptance criteria ──────────────────────────────────


# Order matters for some downstream UX (we render `[bash]` first because
# it's cheapest, `[manual]` last because it doesn't gate convergence).
CRITERION_TYPES: tuple[str, ...] = ("bash", "chrome", "vision", "manual")


# ── Dataclasses ────────────────────────────────────────────────────────


@dataclass
class AcceptanceCriterion:
    """One acceptance-criteria bullet.

    The `text` is the human-readable prose AFTER the type tag. The
    type tag itself is the `type` field. `passed` is None until
    REFLECT runs the check; True/False after. `evidence` is whatever
    REFLECT captured (bash output, screenshot path, vision verdict)
    and is what the runner verifies to defend against fabrication.
    """
    type: str          # one of CRITERION_TYPES
    text: str          # everything AFTER `[type]` in the markdown
    passed: Optional[bool] = None
    evidence: str = ""

    def __post_init__(self) -> None:
        if self.type not in CRITERION_TYPES:
            raise ValueError(
                f"Unknown criterion type {self.type!r}; "
                f"expected one of {CRITERION_TYPES}"
            )

    @property
    def is_blocking(self) -> bool:
        """True if this criterion must pass for convergence.
        `[manual]` items are advisory — listed in the handoff but
        excluded from the `verdict=satisfied` check.
        """
        return self.type != "manual"

    @property
    def is_pending(self) -> bool:
        return self.passed is None

    @property
    def is_satisfied(self) -> bool:
        """True if this criterion no longer blocks convergence —
        either it passed or it's `[manual]` (excluded)."""
        return (not self.is_blocking) or (self.passed is True)


@dataclass
class RoadmapItem:
    """One row in the roadmap's tier list.

    `id` is the user-visible tier ID like `T1.3`. Once assigned, it
    never changes — even if the item moves between tiers, the ID
    stays. Reusing an ID for a different item is forbidden.
    """
    id: str               # e.g. "T1.3"
    tier: int             # parsed from the ID (the `1` in T1.3)
    title: str            # short imperative; the bold prefix in markdown
    description: str = ""
    checked: bool = False
    commit_sha: str = ""  # filled by REFLECT when item ships
    note: str = ""        # one-line completion note from REFLECT

    @classmethod
    def from_id(cls, id: str, title: str, description: str = "") -> "RoadmapItem":
        """Construct, parsing the tier number out of the ID."""
        match = re.match(r"^T(\d+)\.\d+$", id)
        if not match:
            raise ValueError(f"Invalid tier ID {id!r}; expected `T<tier>.<num>`")
        return cls(id=id, tier=int(match.group(1)), title=title, description=description)


@dataclass
class IterationLogEntry:
    """One line in the `## Iteration log` section.

    Captured by the autonomous loop daemon at the end of each
    iteration. `kind` distinguishes regular item-shipped iterations
    from REFLECT passes (which don't ship code but DO mutate the
    roadmap).
    """
    iter_num: int
    timestamp_iso: str
    duration_label: str   # human-friendly "14m" or "2h 3m"
    kind: str             # "shipped" | "reflect" | "blocked" | "skipped"
    item_id: str = ""
    commit_sha: str = ""
    note: str = ""


@dataclass
class Roadmap:
    """The full parsed roadmap.

    The on-disk markdown is the source of truth; this in-memory
    structure is regenerated from disk on every read and serialized
    back on every write. We don't try to preserve un-tracked
    sections — a user-added `## Notes` section, for example, is
    stripped on the next REFLECT pass. Document this in the
    rigorous-grill prompt: don't add untracked sections.
    """
    feature: str = ""              # the H1 title, minus "Autonomous Mission: "
    intent_id: str = ""
    started_iso: str = ""
    time_budget_label: str = ""    # "4h" / "Full auto" / "1h"
    status: str = "running"        # "running" | "paused" | "complete" | "failed"

    goal_spec_block: str = ""      # raw markdown from `## Goal (from grill spec)`

    items: list[RoadmapItem] = field(default_factory=list)
    acceptance_criteria: list[AcceptanceCriterion] = field(default_factory=list)
    iteration_log: list[IterationLogEntry] = field(default_factory=list)
    blocked_notes: list[str] = field(default_factory=list)
    reflection_summary: str = ""   # latest REFLECT verbatim summary

    # ── Convenience queries (read-only) ──────────────────────────

    def next_unchecked_item(self) -> Optional[RoadmapItem]:
        """First item in tier-then-id order whose `checked` is False.
        Returns None when every item is done — the autonomous loop
        treats that as "trigger a full REFLECT pass" rather than
        "stop the mission" (REFLECT may add new items)."""
        for item in self._sorted_items():
            if not item.checked:
                return item
        return None

    def items_by_tier(self) -> dict[int, list[RoadmapItem]]:
        out: dict[int, list[RoadmapItem]] = {}
        for item in self._sorted_items():
            out.setdefault(item.tier, []).append(item)
        return out

    def acceptance_summary(self) -> tuple[int, int]:
        """`(passed_count, total_blocking_count)` — drives the chat-header
        "3/7 met" indicator and the convergence check."""
        blocking = [c for c in self.acceptance_criteria if c.is_blocking]
        passed = sum(1 for c in blocking if c.passed is True)
        return passed, len(blocking)

    def is_converged(self) -> bool:
        """True iff every non-`[manual]` acceptance criterion has
        `passed=True`. This is the canonical convergence check —
        REFLECT's `verdict=satisfied` is gated on this."""
        return all(c.is_satisfied for c in self.acceptance_criteria)

    def has_any_acceptance_criteria(self) -> bool:
        """An autonomous mission with NO acceptance criteria is a
        misconfiguration — the rigorous grill is supposed to require
        at least 4 binary criteria. The loop daemon checks this and
        refuses to declare convergence if the list is empty (otherwise
        an empty list trivially "converges")."""
        return any(c.is_blocking for c in self.acceptance_criteria)

    # ── Internal sort order ─────────────────────────────────────

    def _sorted_items(self) -> list[RoadmapItem]:
        # Primary: tier ascending. Secondary: numeric suffix ascending.
        # Defensive against mismatched parses — items missing a parseable
        # suffix sort to the end of their tier.
        def key(item: RoadmapItem) -> tuple[int, int]:
            match = re.match(r"^T\d+\.(\d+)$", item.id)
            suffix = int(match.group(1)) if match else 10_000
            return (item.tier, suffix)
        return sorted(self.items, key=key)


# ── Markdown parser ────────────────────────────────────────────────────


_HEADER_RE = re.compile(r"^# Autonomous Mission:\s*(.*)\s*$", re.MULTILINE)
_INTENT_RE = re.compile(r"^\*\*Intent ID:\*\*\s*(.+?)\s*$", re.MULTILINE)
_STARTED_RE = re.compile(r"^\*\*Started:\*\*\s*(.+?)\s*$", re.MULTILINE)
_BUDGET_RE = re.compile(r"^\*\*Time budget:\*\*\s*(.+?)\s*$", re.MULTILINE)
_STATUS_RE = re.compile(r"^\*\*Status:\*\*\s*(\w+)", re.MULTILINE)

# Tier section header: `### Tier 1 — initial decomposition` etc.
# The em-dash and trailing label are optional; only the tier number is required.
# NOTE: `(?:—|-)` not `[—-]` — the latter is parsed as a Unicode range
# (U+2014 down to U+002D) which silently misbehaves.
_TIER_HEADER_RE = re.compile(r"^### Tier (\d+)(?:\s*(?:—|-)\s*(.+))?\s*$", re.MULTILINE)

# Item line: `- [x] **T1.3 — Title.** Description... *(shipped at `sha`: note)*`
# Captures: checkbox state, ID, title, description-with-optional-completion-suffix.
# IMPORTANT: trailing capture uses `[^\n]*` (not `.*?\s*$`) to prevent the
# greedy `\s*` from chomping past the line boundary into the next item.
# Subtle bug: with MULTILINE + greedy `\s*`, the engine can find a "match"
# that spans two lines because `\s*` matches `\n`. Using `[^\n]*` forbids
# crossing the boundary.
_ITEM_LINE_RE = re.compile(
    r"^-[ \t]*\[([ x])\][ \t]*\*\*(T\d+\.\d+)[ \t]*(?:—|-)[ \t]*(.+?)\.\*\*([^\n]*)$",
    re.MULTILINE,
)

# Completion suffix inside an item description:
# `*(shipped at `<sha>`: <note>)*` or `*(shipped at <sha>: <note>)*`
_COMPLETION_SUFFIX_RE = re.compile(
    r"\*\(shipped at\s*`?([0-9a-f]{6,40})`?(?:\s*:\s*(.*?))?\)\*",
    re.IGNORECASE,
)

# Acceptance criterion line:
# `- [x] \`[type]\` <text>` or `- [ ] \`[type]\` <text>`
# The type tag is in single backticks to make it regex-friendly and to
# render distinctly in markdown.
_CRITERION_LINE_RE = re.compile(
    r"^-\s*\[([ x])\]\s*`\[(bash|chrome|vision|manual)\]`\s*(.+?)\s*$",
    re.MULTILINE,
)

# Iteration log line — captures iter num, timestamp, duration, the
# rest is a free-form note that we don't structure-parse.
_ITER_LINE_RE = re.compile(
    r"^-\s*\*\*Iter\s+(\d+)\*\*\s*\(([^,]+),\s*([^)]+)\)\s*[—-]\s*(.+?)\s*$",
    re.MULTILINE,
)


def _section_block(markdown: str, header: str) -> str:
    """Extract the body of a `## <header>` section — everything from
    the header line until the next `## ` (or end of file). Returns ""
    when the header isn't present.
    """
    pattern = re.compile(
        rf"^##\s+{re.escape(header)}\s*$(.*?)(?=^##\s+|\Z)",
        re.MULTILINE | re.DOTALL,
    )
    match = pattern.search(markdown)
    return match.group(1).strip() if match else ""


def parse(markdown: str) -> Roadmap:
    """Parse a roadmap.md string into a `Roadmap`.

    Forgiving parser — missing sections produce empty fields, not
    exceptions. Malformed item / criterion lines are silently skipped
    (the writer is the source of canonical formatting; if the user
    hand-edits in a way the parser can't read, the daemon falls back
    to the last known good in-memory state and emits a warning).
    """
    rm = Roadmap()

    if m := _HEADER_RE.search(markdown):
        rm.feature = m.group(1).strip()
    if m := _INTENT_RE.search(markdown):
        rm.intent_id = m.group(1).strip()
    if m := _STARTED_RE.search(markdown):
        rm.started_iso = m.group(1).strip()
    if m := _BUDGET_RE.search(markdown):
        rm.time_budget_label = m.group(1).strip()
    if m := _STATUS_RE.search(markdown):
        rm.status = m.group(1).strip()

    rm.goal_spec_block = _section_block(markdown, "Goal (from grill spec)")

    # Parse items in the `## Roadmap` section.
    roadmap_block = _section_block(markdown, "Roadmap")
    if roadmap_block:
        rm.items = list(_iter_items(roadmap_block))

    # Acceptance criteria can live either as a top-level section
    # `## Acceptance criteria` or embedded inside the goal-spec block
    # (both forms appear in the design doc). Try the dedicated section
    # first, then fall back to scanning the goal block.
    accept_block = _section_block(markdown, "Acceptance criteria")
    if not accept_block:
        accept_block = rm.goal_spec_block
    rm.acceptance_criteria = list(_iter_criteria(accept_block))

    iter_block = _section_block(markdown, "Iteration log")
    if iter_block:
        rm.iteration_log = list(_iter_log(iter_block))

    blocked_block = _section_block(markdown, "Blocked / needs human decision")
    if blocked_block:
        rm.blocked_notes = [
            line.strip("- ").strip()
            for line in blocked_block.splitlines()
            if line.strip().startswith("-")
        ]

    rm.reflection_summary = _section_block(markdown, "Reflection summary (latest)")
    return rm


def _iter_items(block: str) -> Iterator[RoadmapItem]:
    """Yield items from a `## Roadmap` section body. Skips tier
    headers — they're recovered from the item ID."""
    for match in _ITEM_LINE_RE.finditer(block):
        checked = match.group(1) == "x"
        item_id = match.group(2)
        title = match.group(3).strip()
        rest = match.group(4).strip()

        # Pull the completion-suffix out of `rest` if present; the
        # remaining text is the description.
        commit_sha = ""
        note = ""
        completion = _COMPLETION_SUFFIX_RE.search(rest)
        if completion:
            commit_sha = completion.group(1).strip()
            note = (completion.group(2) or "").strip()
            description = _COMPLETION_SUFFIX_RE.sub("", rest).strip()
        else:
            description = rest

        try:
            item = RoadmapItem.from_id(item_id, title=title, description=description)
        except ValueError:
            continue
        item.checked = checked
        item.commit_sha = commit_sha
        item.note = note
        yield item


def _iter_criteria(block: str) -> Iterator[AcceptanceCriterion]:
    """Yield acceptance criteria from a section body."""
    for match in _CRITERION_LINE_RE.finditer(block):
        checked = match.group(1) == "x"
        ctype = match.group(2)
        text = match.group(3).strip()
        # Heuristic: a checked criterion has `passed=True` UNLESS its
        # text indicates failure (rare — we only set `passed=False`
        # via the writer when REFLECT records a real failure with
        # evidence; the markdown round-trip preserves this via a
        # `[FAIL]` prefix on the text — see write_criterion below).
        if text.startswith("[FAIL]"):
            yield AcceptanceCriterion(type=ctype, text=text[6:].strip(), passed=False)
        elif checked:
            yield AcceptanceCriterion(type=ctype, text=text, passed=True)
        else:
            yield AcceptanceCriterion(type=ctype, text=text, passed=None)


def _iter_log(block: str) -> Iterator[IterationLogEntry]:
    """Yield iteration log entries. Free-form note in the trailing
    capture group — we don't sub-parse it."""
    for match in _ITER_LINE_RE.finditer(block):
        iter_num = int(match.group(1))
        timestamp = match.group(2).strip()
        duration = match.group(3).strip()
        note = match.group(4).strip()
        # The kind / item_id / commit_sha are loosely encoded in the
        # note. Keep the raw `note` for round-trip; let the writer
        # regenerate from structured fields when REFLECT updates.
        yield IterationLogEntry(
            iter_num=iter_num,
            timestamp_iso=timestamp,
            duration_label=duration,
            kind="shipped",
            note=note,
        )


# ── Markdown writer ────────────────────────────────────────────────────


def render(rm: Roadmap) -> str:
    """Serialize a `Roadmap` to canonical markdown. The writer is the
    source of canonical formatting; the parser is forgiving.

    Formatting choices:
      * Items render with their completion suffix `*(shipped at `sha`: note)*`
        ONLY when checked + commit_sha non-empty. Otherwise no suffix.
      * Acceptance criteria render the type tag in single backticks
        (`[bash]`, `[chrome]`, etc.) and a `[FAIL] ` prefix on the
        text when `passed=False`. (Round-trip safe via the parser.)
      * Sections always emit in this order: Goal → Roadmap → Iteration
        log → Completed → Blocked → Reflection summary.
    """
    parts: list[str] = []
    parts.append(f"# Autonomous Mission: {rm.feature}\n")
    parts.append("")
    parts.append(f"**Intent ID:** {rm.intent_id}")
    parts.append(f"**Started:** {rm.started_iso}")
    parts.append(f"**Time budget:** {rm.time_budget_label}")
    parts.append(f"**Status:** {rm.status}")
    parts.append("")

    if rm.goal_spec_block:
        parts.append("## Goal (from grill spec)")
        parts.append("")
        parts.append(rm.goal_spec_block)
        parts.append("")

    parts.append("## Roadmap")
    parts.append("")
    by_tier = rm.items_by_tier()
    for tier in sorted(by_tier.keys()):
        parts.append(f"### Tier {tier}")
        parts.append("")
        for item in by_tier[tier]:
            parts.append(_format_item(item))
        parts.append("")

    if rm.acceptance_criteria:
        parts.append("## Acceptance criteria")
        parts.append("")
        parts.append(
            "*(must all be true at convergence; REFLECT validates each one "
            "using its tagged strategy. Model can't fake them — runner "
            "verifies the check ran and the output matched.)*"
        )
        parts.append("")
        for crit in rm.acceptance_criteria:
            parts.append(_format_criterion(crit))
        parts.append("")

    if rm.iteration_log:
        parts.append("## Iteration log")
        parts.append("")
        for entry in rm.iteration_log:
            parts.append(_format_iter_log(entry))
        parts.append("")

    if rm.blocked_notes:
        parts.append("## Blocked / needs human decision")
        parts.append("")
        for note in rm.blocked_notes:
            parts.append(f"- {note}")
        parts.append("")

    if rm.reflection_summary:
        parts.append("## Reflection summary (latest)")
        parts.append("")
        parts.append(rm.reflection_summary)
        parts.append("")

    return "\n".join(parts).rstrip() + "\n"


def _format_item(item: RoadmapItem) -> str:
    box = "[x]" if item.checked else "[ ]"
    suffix = ""
    if item.checked and item.commit_sha:
        if item.note:
            suffix = f" *(shipped at `{item.commit_sha}`: {item.note})*"
        else:
            suffix = f" *(shipped at `{item.commit_sha}`)*"
    desc = f" {item.description}".rstrip() if item.description else ""
    return f"- {box} **{item.id} — {item.title}.**{desc}{suffix}"


def _format_criterion(c: AcceptanceCriterion) -> str:
    box = "[x]" if c.passed is True else "[ ]"
    text = c.text
    if c.passed is False:
        text = f"[FAIL] {text}"
    return f"- {box} `[{c.type}]` {text}"


def _format_iter_log(entry: IterationLogEntry) -> str:
    return (
        f"- **Iter {entry.iter_num}** "
        f"({entry.timestamp_iso}, {entry.duration_label}) — {entry.note}"
    )


# ── Mutation helpers ──────────────────────────────────────────────────


def mark_item_complete(
    rm: Roadmap, item_id: str, commit_sha: str, note: str = ""
) -> bool:
    """Mark `item_id` as complete with the given commit ref. Returns
    True if the item was found and marked; False if the ID doesn't
    exist (REFLECT might pass a stale ID after a hand-edit removed
    the item — the daemon logs and continues).
    """
    for item in rm.items:
        if item.id == item_id:
            item.checked = True
            item.commit_sha = commit_sha
            item.note = note
            return True
    return False


def update_criterion(
    rm: Roadmap,
    text_match: str,
    passed: bool,
    evidence: str = "",
) -> bool:
    """Mark an acceptance criterion as passed/failed.

    Match is by exact `text` equality (criteria don't have IDs in the
    markdown, so we key on the prose). REFLECT supplies the same
    text it read from the file, so this is reliable in normal flow.
    Returns True iff a match was updated.
    """
    for c in rm.acceptance_criteria:
        if c.text == text_match:
            c.passed = passed
            c.evidence = evidence
            return True
    return False


def add_item(
    rm: Roadmap,
    tier: int,
    title: str,
    description: str = "",
    *,
    source_iter: Optional[int] = None,
) -> RoadmapItem:
    """Append a new item to the given tier with an auto-assigned ID.

    The ID is `T<tier>.<next>` where `<next>` is one greater than the
    current max suffix in that tier (or 1 if the tier is empty).
    Tier IDs are immutable post-creation, so this allocator is the
    only place IDs come from after the initial spec parse.
    """
    existing_in_tier = [item for item in rm.items if item.tier == tier]
    next_suffix = 1
    if existing_in_tier:
        max_suffix = 0
        for item in existing_in_tier:
            match = re.match(r"^T\d+\.(\d+)$", item.id)
            if match:
                max_suffix = max(max_suffix, int(match.group(1)))
        next_suffix = max_suffix + 1

    full_desc = description
    if source_iter is not None and full_desc:
        full_desc = f"{full_desc} *(added in iteration {source_iter})*"
    elif source_iter is not None:
        full_desc = f"*(added in iteration {source_iter})*"

    new_id = f"T{tier}.{next_suffix}"
    item = RoadmapItem.from_id(new_id, title=title, description=full_desc)
    rm.items.append(item)
    return item


def append_iteration_log(
    rm: Roadmap,
    iter_num: int,
    duration_label: str,
    note: str,
    *,
    item_id: str = "",
    commit_sha: str = "",
    kind: str = "shipped",
) -> None:
    """Append a one-line iteration entry. Caller produces the
    timestamp via `time.gmtime()` so tests can stub it."""
    rm.iteration_log.append(IterationLogEntry(
        iter_num=iter_num,
        timestamp_iso=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        duration_label=duration_label,
        kind=kind,
        item_id=item_id,
        commit_sha=commit_sha,
        note=note,
    ))


# ── Disk I/O with file locking ────────────────────────────────────────
#
# The user can edit roadmap.md while the daemon is running. We use a
# simple advisory `<roadmap>.lock` file as a coordination point:
# REFLECT acquires it before reading-modifying-writing. On Windows the
# atomic file lock primitives differ from POSIX, so we keep the
# implementation simple — create-if-not-exists with a stale-lock
# threshold (60s).


_LOCK_STALE_SECONDS = 60.0


@contextmanager
def file_lock(roadmap_path: Path) -> Iterator[None]:
    """Advisory lock around a roadmap path. Best-effort: uses a
    sibling `.lock` file with create-if-not-exists semantics. If
    a lock file is older than `_LOCK_STALE_SECONDS`, we treat it
    as orphaned (process crashed) and steal it.

    Not a substitute for fcntl/msvcrt locking; sufficient for
    "REFLECT and the user shouldn't write at the same instant."
    """
    lock_path = roadmap_path.with_suffix(roadmap_path.suffix + ".lock")
    waited = 0.0
    while True:
        try:
            fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.close(fd)
            break
        except FileExistsError:
            try:
                age = time.time() - lock_path.stat().st_mtime
            except OSError:
                age = 0
            if age > _LOCK_STALE_SECONDS:
                try:
                    lock_path.unlink()
                except OSError:
                    pass
                continue
            time.sleep(0.05)
            waited += 0.05
            if waited > 5.0:
                # Five seconds of contention is a real problem; surface it.
                raise TimeoutError(
                    f"Could not acquire lock on {lock_path} after 5s. "
                    f"Stale lock? Delete {lock_path} manually if no other "
                    f"process is editing the roadmap."
                )
    try:
        yield
    finally:
        try:
            lock_path.unlink()
        except OSError:
            pass


def load(path: Path | str) -> Roadmap:
    """Read + parse a roadmap.md file. Empty / missing returns an
    empty Roadmap (caller decides whether that's an error)."""
    p = Path(path)
    if not p.is_file():
        return Roadmap()
    text = p.read_text(encoding="utf-8")
    return parse(text)


def save(rm: Roadmap, path: Path | str) -> None:
    """Render + write a Roadmap to disk. Acquires the file lock
    around the write to serialize against user hand-edits."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    rendered = render(rm)
    with file_lock(p):
        p.write_text(rendered, encoding="utf-8")


# ── Default location ──────────────────────────────────────────────────


def default_path(project_path: Path | str, intent_id: str) -> Path:
    """`<project>/.resonant/roadmap-<intent_id>.md` per the design
    doc §6.1. Caller is responsible for creating the `.resonant/`
    dir before saving (save() does this anyway via mkdir(parents=True))."""
    return Path(project_path) / ".resonant" / f"roadmap-{intent_id}.md"
