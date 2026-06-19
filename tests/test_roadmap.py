"""
Tests for `gui/roadmap.py` — the v0.5.0a1 pure data layer for
Autonomous Mission roadmaps.

Three test groups:
  * Dataclass invariants (AcceptanceCriterion type validation,
    convergence / pending properties, RoadmapItem ID parsing)
  * Markdown round-trip (parse → render → parse produces the same
    structure; edge cases in the format)
  * Mutation helpers (mark_item_complete, update_criterion, add_item,
    append_iteration_log) + file lock behavior

The parser is intentionally forgiving (skip-the-bad-line); the writer
is the canonical formatter. Tests pin both — the writer's output IS
the spec for what the markdown looks like.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from resonant_client.gui.roadmap import (
    AcceptanceCriterion,
    CRITERION_TYPES,
    IterationLogEntry,
    Roadmap,
    RoadmapItem,
    add_item,
    append_iteration_log,
    default_path,
    file_lock,
    clear_inflight,
    load,
    mark_item_complete,
    parse,
    read_inflight,
    render,
    save,
    update_criterion,
    write_inflight,
)


# ── AcceptanceCriterion ────────────────────────────────────────────────


class TestAcceptanceCriterionDataclass:
    def test_valid_types_round_trip(self):
        for t in CRITERION_TYPES:
            c = AcceptanceCriterion(type=t, text="x")
            assert c.type == t

    def test_unknown_type_raises(self):
        with pytest.raises(ValueError):
            AcceptanceCriterion(type="bogus", text="x")

    def test_pending_when_passed_is_none(self):
        c = AcceptanceCriterion(type="bash", text="x")
        assert c.is_pending is True
        assert c.is_satisfied is False

    def test_passed_satisfies(self):
        c = AcceptanceCriterion(type="bash", text="x", passed=True)
        assert c.is_pending is False
        assert c.is_satisfied is True

    def test_failed_does_not_satisfy(self):
        c = AcceptanceCriterion(type="bash", text="x", passed=False)
        assert c.is_satisfied is False

    def test_manual_is_not_blocking(self):
        # Per design: `[manual]` is excluded from convergence even
        # when pending. The handoff lists them but they don't gate
        # `verdict=satisfied`.
        c = AcceptanceCriterion(type="manual", text="x")
        assert c.is_blocking is False
        assert c.is_satisfied is True  # vacuously — not blocking

    def test_bash_chrome_vision_are_blocking(self):
        for t in ("bash", "chrome", "vision"):
            assert AcceptanceCriterion(type=t, text="x").is_blocking is True


# ── RoadmapItem ────────────────────────────────────────────────────────


class TestRoadmapItem:
    def test_from_id_parses_tier(self):
        item = RoadmapItem.from_id("T1.3", title="Title")
        assert item.tier == 1
        assert item.id == "T1.3"
        assert item.title == "Title"

    def test_from_id_higher_tier(self):
        assert RoadmapItem.from_id("T17.99", title="x").tier == 17

    def test_invalid_id_raises(self):
        with pytest.raises(ValueError):
            RoadmapItem.from_id("X1.3", title="x")
        with pytest.raises(ValueError):
            RoadmapItem.from_id("T1", title="x")  # missing suffix
        with pytest.raises(ValueError):
            RoadmapItem.from_id("T1.3.5", title="x")  # too many segments


# ── Roadmap convenience queries ────────────────────────────────────────


def _populated_roadmap() -> Roadmap:
    """Builds a small roadmap for the convenience-query tests."""
    rm = Roadmap(feature="Test feature", intent_id="abc123", time_budget_label="4h")
    rm.items = [
        RoadmapItem.from_id("T1.1", title="First"),
        RoadmapItem.from_id("T1.2", title="Second"),
        RoadmapItem.from_id("T2.1", title="Third"),
    ]
    rm.items[0].checked = True
    rm.acceptance_criteria = [
        AcceptanceCriterion(type="bash", text="build passes", passed=True),
        AcceptanceCriterion(type="chrome", text="counter increments"),
        AcceptanceCriterion(type="manual", text="logo looks ok"),
    ]
    return rm


class TestRoadmapQueries:
    def test_next_unchecked_item_picks_lowest_tier_then_lowest_suffix(self):
        rm = _populated_roadmap()
        nxt = rm.next_unchecked_item()
        assert nxt is not None
        assert nxt.id == "T1.2"

    def test_next_unchecked_item_returns_none_when_all_done(self):
        rm = _populated_roadmap()
        for item in rm.items:
            item.checked = True
        assert rm.next_unchecked_item() is None

    def test_items_by_tier_groups_correctly(self):
        rm = _populated_roadmap()
        groups = rm.items_by_tier()
        assert set(groups.keys()) == {1, 2}
        assert {item.id for item in groups[1]} == {"T1.1", "T1.2"}
        assert {item.id for item in groups[2]} == {"T2.1"}

    def test_acceptance_summary_counts_only_blocking(self):
        rm = _populated_roadmap()
        passed, total = rm.acceptance_summary()
        # Two blocking criteria (bash, chrome); manual is excluded.
        # One passed (bash), one pending (chrome).
        assert (passed, total) == (1, 2)

    def test_is_converged_false_when_blocking_pending(self):
        rm = _populated_roadmap()
        assert rm.is_converged() is False

    def test_is_converged_true_when_all_blocking_pass(self):
        rm = _populated_roadmap()
        rm.acceptance_criteria[1].passed = True  # chrome
        assert rm.is_converged() is True
        # Manual stays pending — that's fine, doesn't block convergence.

    def test_has_any_acceptance_criteria_excludes_manual_only(self):
        # An autonomous mission with ONLY [manual] criteria is a
        # misconfiguration — the rigorous grill should reject this.
        rm = Roadmap()
        rm.acceptance_criteria = [
            AcceptanceCriterion(type="manual", text="x"),
            AcceptanceCriterion(type="manual", text="y"),
        ]
        assert rm.has_any_acceptance_criteria() is False


# ── Markdown parser ────────────────────────────────────────────────────


_FULL_ROADMAP = """\
# Autonomous Mission: Build a counter webpage

**Intent ID:** intent-abc123
**Started:** 2026-05-02T08:14:03Z
**Time budget:** 4h
**Status:** running

## Goal (from grill spec)

Build a tiny webpage with a counter button that increments on click.

## Roadmap

### Tier 1 — initial decomposition

- [x] **T1.1 — Scaffold Vite project.** Set up package.json + tsconfig + vite.config. *(shipped at `abc123f`: scaffold OK)*
- [ ] **T1.2 — Add counter button.** Implement the click handler in `src/main.ts`.
- [ ] **T1.3 — Style the button.** Use Tailwind utility classes.

### Tier 2 — discovered during iteration

- [ ] **T2.1 — Add unit tests.** Vitest suite covering the increment logic. *(added in iteration 4)*

## Acceptance criteria

*(must all be true at convergence)*

- [x] `[bash]` `npm run build` exits 0
- [ ] `[chrome]` Click `#counter`; count increments from 0 to 1
- [ ] `[vision]` The button is centered horizontally on the page
- [ ] `[manual]` The font feels modern and readable

## Iteration log

- **Iter 1** (2026-05-02T08:18:11Z, 14m) — picked T1.1, shipped at `abc123f`. Notes: clean.

## Blocked / needs human decision

- T1.4 needs the user to choose between Tailwind and styled-components

## Reflection summary (latest)

> Last updated by REFLECT on 2026-05-02T09:14:15Z (iter 5).
> 1 of 4 items shipped, 3 remaining. Verdict: continuing.
"""


class TestParseFullRoadmap:
    def test_header_fields_parsed(self):
        rm = parse(_FULL_ROADMAP)
        assert rm.feature == "Build a counter webpage"
        assert rm.intent_id == "intent-abc123"
        assert rm.started_iso == "2026-05-02T08:14:03Z"
        assert rm.time_budget_label == "4h"
        assert rm.status == "running"

    def test_goal_block_extracted(self):
        rm = parse(_FULL_ROADMAP)
        assert "counter button" in rm.goal_spec_block

    def test_items_parsed_with_status(self):
        rm = parse(_FULL_ROADMAP)
        assert len(rm.items) == 4
        ids = {item.id for item in rm.items}
        assert ids == {"T1.1", "T1.2", "T1.3", "T2.1"}
        # T1.1 is checked with a commit ref + note
        t11 = next(item for item in rm.items if item.id == "T1.1")
        assert t11.checked is True
        assert t11.commit_sha == "abc123f"
        assert "scaffold OK" in t11.note

    def test_unchecked_items_have_empty_completion_fields(self):
        rm = parse(_FULL_ROADMAP)
        t12 = next(item for item in rm.items if item.id == "T1.2")
        assert t12.checked is False
        assert t12.commit_sha == ""
        assert t12.note == ""
        assert "click handler" in t12.description

    def test_tiers_parsed_from_ids(self):
        rm = parse(_FULL_ROADMAP)
        groups = rm.items_by_tier()
        assert set(groups.keys()) == {1, 2}

    def test_acceptance_criteria_parsed_with_types(self):
        rm = parse(_FULL_ROADMAP)
        assert len(rm.acceptance_criteria) == 4
        types = [c.type for c in rm.acceptance_criteria]
        assert types == ["bash", "chrome", "vision", "manual"]

    def test_passed_criterion_recovered(self):
        rm = parse(_FULL_ROADMAP)
        bash_c = rm.acceptance_criteria[0]
        assert bash_c.passed is True

    def test_pending_criteria_recovered_as_none(self):
        rm = parse(_FULL_ROADMAP)
        chrome_c = rm.acceptance_criteria[1]
        assert chrome_c.passed is None

    def test_iteration_log_parsed(self):
        rm = parse(_FULL_ROADMAP)
        assert len(rm.iteration_log) == 1
        entry = rm.iteration_log[0]
        assert entry.iter_num == 1
        assert "T1.1" in entry.note

    def test_blocked_notes_parsed(self):
        rm = parse(_FULL_ROADMAP)
        assert len(rm.blocked_notes) == 1
        assert "Tailwind" in rm.blocked_notes[0]


class TestParseEdgeCases:
    def test_empty_string_returns_empty_roadmap(self):
        rm = parse("")
        assert rm.feature == ""
        assert rm.items == []
        assert rm.acceptance_criteria == []

    def test_missing_sections_silently_ignored(self):
        # A roadmap that has just the header + items, no acceptance
        # section, no iteration log — must parse without raising.
        text = """\
# Autonomous Mission: x

**Intent ID:** y
**Time budget:** 4h
**Status:** running

## Roadmap

### Tier 1

- [ ] **T1.1 — Do the thing.** Description here.
"""
        rm = parse(text)
        assert rm.feature == "x"
        assert len(rm.items) == 1
        assert rm.acceptance_criteria == []

    def test_item_title_period_optional(self):
        # v0.5.0a9 — REFLECT routinely emits items without a trailing
        # period (`**T2.1 — Fix something**` not `... something.**`).
        # The parser must accept both forms; the canonical writer
        # always emits the period for readability. Found in v0.5.0
        # GA smoke run #5.
        text = """\
# Autonomous Mission: x

## Roadmap

### Tier 1

- [ ] **T1.1 — With period.** Description.
- [ ] **T1.2 — Without period** Description.
- [x] **T1.3 — Also no period** Already done.
"""
        rm = parse(text)
        ids = {item.id for item in rm.items}
        assert ids == {"T1.1", "T1.2", "T1.3"}
        # And titles stripped consistently for both forms
        titles = {item.id: item.title for item in rm.items}
        assert "With period" in titles["T1.1"]
        assert "Without period" in titles["T1.2"]
        assert "Also no period" in titles["T1.3"]

    def test_malformed_item_line_skipped(self):
        # An item line that doesn't match the regex (e.g. missing the
        # `T<n>.<n>` ID) is skipped silently, not an error. This is
        # the design — the writer is canonical, parser is forgiving.
        text = """\
# Autonomous Mission: x

## Roadmap

### Tier 1

- [ ] **T1.1 — Good.** Valid item.
- [ ] **NotAnId — Bad.** This should be skipped.
- [ ] **T1.2 — Good 2.** Valid.
"""
        rm = parse(text)
        ids = {item.id for item in rm.items}
        assert ids == {"T1.1", "T1.2"}

    def test_failed_criterion_round_trips_via_fail_prefix(self):
        text = """\
# Autonomous Mission: x

## Acceptance criteria

- [ ] `[bash]` [FAIL] `npm test` exits 0
"""
        rm = parse(text)
        assert len(rm.acceptance_criteria) == 1
        c = rm.acceptance_criteria[0]
        assert c.passed is False
        assert c.text == "`npm test` exits 0"

    def test_acceptance_criteria_inside_goal_block(self):
        # Some specs have the acceptance criteria embedded in the goal
        # block rather than a separate section. Parser falls back.
        text = """\
# Autonomous Mission: x

**Status:** running

## Goal (from grill spec)

Some prose describing the goal.

**Acceptance criteria:**
- [ ] `[bash]` `pytest` exits 0
- [ ] `[chrome]` button click works

## Roadmap

### Tier 1

- [ ] **T1.1 — Do it.**
"""
        rm = parse(text)
        assert len(rm.acceptance_criteria) == 2


# ── Round-trip ────────────────────────────────────────────────────────


class TestRoundTrip:
    def test_full_roadmap_round_trips(self):
        # Parse → render → parse should produce the same data on the
        # second pass. Whitespace / formatting may differ between
        # the original and rendered string; semantic content matches.
        original = parse(_FULL_ROADMAP)
        rendered = render(original)
        round_tripped = parse(rendered)

        assert round_tripped.feature == original.feature
        assert round_tripped.intent_id == original.intent_id
        assert round_tripped.time_budget_label == original.time_budget_label
        assert round_tripped.status == original.status

        # Items
        assert len(round_tripped.items) == len(original.items)
        for orig_item, rt_item in zip(
            sorted(original.items, key=lambda i: i.id),
            sorted(round_tripped.items, key=lambda i: i.id),
        ):
            assert orig_item.id == rt_item.id
            assert orig_item.checked == rt_item.checked
            assert orig_item.commit_sha == rt_item.commit_sha
            assert orig_item.title == rt_item.title

        # Acceptance criteria
        assert len(round_tripped.acceptance_criteria) == len(original.acceptance_criteria)
        for orig_c, rt_c in zip(
            original.acceptance_criteria,
            round_tripped.acceptance_criteria,
        ):
            assert orig_c.type == rt_c.type
            assert orig_c.passed == rt_c.passed
            assert orig_c.text == rt_c.text

    def test_render_writes_canonical_section_order(self):
        rm = parse(_FULL_ROADMAP)
        rendered = render(rm)
        # Section headers must appear in the documented canonical order.
        order = [
            "## Goal (from grill spec)",
            "## Roadmap",
            "## Acceptance criteria",
            "## Iteration log",
            "## Blocked / needs human decision",
            "## Reflection summary (latest)",
        ]
        positions = [rendered.find(h) for h in order]
        assert all(p > 0 for p in positions), positions
        assert positions == sorted(positions), "Sections must render in canonical order"

    def test_render_omits_empty_sections(self):
        rm = Roadmap(feature="Minimal", intent_id="x", time_budget_label="1h")
        rm.items = [RoadmapItem.from_id("T1.1", title="Only")]
        rendered = render(rm)
        assert "## Roadmap" in rendered
        assert "## Acceptance criteria" not in rendered
        assert "## Iteration log" not in rendered
        assert "## Blocked / needs human decision" not in rendered

    def test_failed_criterion_renders_with_fail_prefix(self):
        rm = Roadmap(feature="x", intent_id="y", time_budget_label="1h")
        rm.acceptance_criteria = [
            AcceptanceCriterion(type="bash", text="`pytest` exits 0", passed=False),
        ]
        rendered = render(rm)
        assert "[FAIL]" in rendered
        # And it round-trips back to passed=False
        rt = parse(rendered)
        assert rt.acceptance_criteria[0].passed is False


# ── Mutation helpers ──────────────────────────────────────────────────


class TestMarkItemComplete:
    def test_marks_existing_item(self):
        rm = _populated_roadmap()
        ok = mark_item_complete(rm, "T1.2", "deadbeef", note="done")
        assert ok is True
        item = next(i for i in rm.items if i.id == "T1.2")
        assert item.checked is True
        assert item.commit_sha == "deadbeef"
        assert item.note == "done"

    def test_returns_false_for_unknown_id(self):
        rm = _populated_roadmap()
        ok = mark_item_complete(rm, "T9.9", "x", note="")
        assert ok is False

    def test_renders_completion_suffix(self):
        rm = _populated_roadmap()
        mark_item_complete(rm, "T1.2", "abc123", note="works fine")
        rendered = render(rm)
        assert "abc123" in rendered
        assert "works fine" in rendered


class TestUpdateCriterion:
    def test_marks_passing_criterion(self):
        rm = _populated_roadmap()
        ok = update_criterion(rm, "counter increments", passed=True, evidence="DOM ok")
        assert ok is True
        c = rm.acceptance_criteria[1]
        assert c.passed is True
        assert c.evidence == "DOM ok"

    def test_returns_false_for_unmatched_text(self):
        rm = _populated_roadmap()
        ok = update_criterion(rm, "totally different text", passed=True)
        assert ok is False

    def test_marking_failed_renders_with_fail_prefix(self):
        rm = _populated_roadmap()
        update_criterion(rm, "counter increments", passed=False, evidence="click did nothing")
        rendered = render(rm)
        assert "[FAIL]" in rendered


class TestAddItem:
    def test_appends_to_existing_tier(self):
        rm = _populated_roadmap()
        item = add_item(rm, tier=1, title="Fourth", description="Late add")
        # Tier 1 had T1.1, T1.2 → next suffix is 3
        assert item.id == "T1.3"
        assert item.tier == 1
        assert item in rm.items

    def test_starts_at_one_in_new_tier(self):
        rm = _populated_roadmap()
        item = add_item(rm, tier=5, title="First in tier 5")
        assert item.id == "T5.1"

    def test_source_iter_appears_in_description(self):
        rm = _populated_roadmap()
        item = add_item(rm, tier=2, title="x", description="base", source_iter=4)
        assert "iteration 4" in item.description

    def test_source_iter_alone_when_no_description(self):
        rm = _populated_roadmap()
        item = add_item(rm, tier=2, title="x", source_iter=7)
        assert "iteration 7" in item.description

    def test_id_immutability(self):
        # Adding an item, then later adding to same tier, must not
        # reuse the first item's ID even after intermediate work.
        rm = Roadmap(feature="x", intent_id="y", time_budget_label="1h")
        first = add_item(rm, tier=1, title="A")
        assert first.id == "T1.1"
        # Mark it complete (the item is "done" but its ID is still
        # claimed for this tier).
        mark_item_complete(rm, "T1.1", "sha", "note")
        second = add_item(rm, tier=1, title="B")
        assert second.id == "T1.2"  # not "T1.1"


class TestAppendIterationLog:
    def test_appends_with_canonical_format(self):
        rm = _populated_roadmap()
        append_iteration_log(
            rm, iter_num=2, duration_label="14m", note="picked T1.2"
        )
        assert len(rm.iteration_log) == 1
        entry = rm.iteration_log[0]
        assert entry.iter_num == 2
        assert entry.duration_label == "14m"
        assert "T1.2" in entry.note

    def test_renders_in_order(self):
        rm = Roadmap(feature="x", intent_id="y", time_budget_label="1h")
        append_iteration_log(rm, 1, "10m", "first")
        append_iteration_log(rm, 2, "5m", "second")
        rendered = render(rm)
        assert rendered.find("first") < rendered.find("second")


# ── Disk I/O + locking ───────────────────────────────────────────────


class TestDiskIO:
    def test_save_then_load_round_trips(self, tmp_path):
        rm = _populated_roadmap()
        path = tmp_path / "roadmap.md"
        save(rm, path)
        loaded = load(path)
        assert loaded.feature == rm.feature
        assert len(loaded.items) == len(rm.items)
        assert len(loaded.acceptance_criteria) == len(rm.acceptance_criteria)

    def test_load_missing_file_returns_empty_roadmap(self, tmp_path):
        loaded = load(tmp_path / "no-such-file.md")
        assert loaded.feature == ""
        assert loaded.items == []

    def test_save_creates_parent_dirs(self, tmp_path):
        rm = Roadmap(feature="x", intent_id="y", time_budget_label="1h")
        nested = tmp_path / ".resonant" / "deep" / "roadmap.md"
        save(rm, nested)
        assert nested.is_file()

    def test_save_leaves_no_temp_file(self, tmp_path):
        # v0.6.5 — the atomic write goes through a sibling .tmp +
        # os.replace; a successful save must not leave the .tmp behind.
        save(_populated_roadmap(), tmp_path / "roadmap.md")
        assert not (tmp_path / "roadmap.md.tmp").exists()

    def test_save_overwrites_existing_fully(self, tmp_path):
        # Atomic replace means the second save fully supersedes the
        # first — no append, no half-merged corruption.
        path = tmp_path / "roadmap.md"
        save(Roadmap(feature="first", intent_id="i1", time_budget_label="1h"), path)
        save(Roadmap(feature="second", intent_id="i1", time_budget_label="1h"), path)
        assert load(path).feature == "second"


class TestFileLock:
    def test_acquires_and_releases(self, tmp_path):
        path = tmp_path / "roadmap.md"
        path.touch()
        lock_path = tmp_path / "roadmap.md.lock"
        assert not lock_path.exists()
        with file_lock(path):
            assert lock_path.exists()
        assert not lock_path.exists()

    def test_steals_stale_lock(self, tmp_path, monkeypatch):
        # An orphaned lock file older than 60s should be stolen.
        path = tmp_path / "roadmap.md"
        path.touch()
        lock_path = tmp_path / "roadmap.md.lock"
        lock_path.touch()
        # Backdate the lock by 90s
        old_time = time.time() - 90
        os.utime(lock_path, (old_time, old_time))
        # The lock should be acquired (stale lock stolen)
        with file_lock(path):
            assert lock_path.exists()
        assert not lock_path.exists()


class TestDefaultPath:
    def test_lives_in_dot_resonant(self, tmp_path):
        p = default_path(tmp_path, "intent-abc123")
        assert p.parent.name == ".resonant"
        assert p.name == "roadmap-intent-abc123.md"
        assert p.parent.parent == tmp_path


# ── In-flight iteration checkpoint (v0.6.5 crash-safe resume) ─────────


class TestInflightCheckpoint:
    def test_write_then_read_round_trips(self, tmp_path):
        path = tmp_path / "roadmap.md"
        write_inflight(path, item_id="T1.2", iter_num=7, started_at=123.0)
        data = read_inflight(path)
        assert data is not None
        assert data["item_id"] == "T1.2"
        assert data["iter"] == 7

    def test_read_absent_returns_none(self, tmp_path):
        assert read_inflight(tmp_path / "roadmap.md") is None

    def test_clear_removes_checkpoint(self, tmp_path):
        path = tmp_path / "roadmap.md"
        write_inflight(path, item_id="T1.2", iter_num=1, started_at=1.0)
        assert (tmp_path / "roadmap.md.inflight").exists()
        clear_inflight(path)
        assert read_inflight(path) is None
        assert not (tmp_path / "roadmap.md.inflight").exists()

    def test_clear_absent_is_noop(self, tmp_path):
        # Idempotent — clearing a non-existent checkpoint must not raise.
        clear_inflight(tmp_path / "roadmap.md")

    def test_read_corrupt_returns_none(self, tmp_path):
        # A truncated / garbage checkpoint must not raise on read.
        (tmp_path / "roadmap.md.inflight").write_text("{not json", encoding="utf-8")
        assert read_inflight(tmp_path / "roadmap.md") is None

    def test_write_leaves_no_temp_file(self, tmp_path):
        path = tmp_path / "roadmap.md"
        write_inflight(path, item_id="T1.2", iter_num=1, started_at=1.0)
        assert not (tmp_path / "roadmap.md.inflight.tmp").exists()
