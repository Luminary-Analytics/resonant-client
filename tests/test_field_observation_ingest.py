"""Tests for v0.6.2a5 — field-observation ingestion as user skills.

User-authored field-observations docs become Skills with
`created_by="user"` (curator-exempt) and `pinned=True` (auto-dep-
exempt). Re-running is idempotent unless `force=True`.

Tests cover:
- Single-file ingest produces a Skill with the right provenance + body
- Multi-file dir ingest skips planning docs (NEXT-RUN, TODO, etc.)
- Idempotent: re-run is a no-op without --force
- --force overwrites
- --dry-run returns the parsed Skill but doesn't persist
- CLI: subcommand parses correctly, exit codes match contract
"""
from __future__ import annotations

import textwrap

import pytest

from resonant_client.orchestration.field_observation_ingest import (
    _parse_field_observation_md,
    ingest_field_observation_dir,
    ingest_field_observation_file,
)
from resonant_client.orchestration.skill_cli import build_parser, main
from resonant_client.orchestration.skills import load_skill, list_skills_filtered


# ── Fixtures ──────────────────────────────────────────────────────────


@pytest.fixture
def state_home(tmp_path, monkeypatch):
    home = tmp_path / "state-home"
    home.mkdir()
    monkeypatch.setenv("RESONANT_STATE_HOME", str(home))
    return home


@pytest.fixture
def field_obs_dir(tmp_path):
    """A directory with two field-obs docs + one planning doc that should be skipped."""
    d = tmp_path / "field-obs"
    d.mkdir()

    (d / "2026-05-06-self-improvement-loop.md").write_text(textwrap.dedent("""\
        # Self-improvement loop field run

        First field run of the v0.6.0+v0.6.1 loop. Three findings emerged.

        ## F1: trivial mission bypass

        Trivial missions bypass the formal iter loop, so extractor + curator
        hooks never fire.

        ## F2: rate-limit UX
        ...
    """), encoding="utf-8")

    (d / "2026-05-03-resonant-linux-bridge.md").write_text(textwrap.dedent("""\
        # Linux bridge field run

        > Vision-scope test of the autonomous-mission flow.

        The grill agent navigated 12 questions to refine the spec.
    """), encoding="utf-8")

    # Planning doc — should be skipped by the dir-walk heuristic.
    (d / "NEXT-RUN-PREP.md").write_text(textwrap.dedent("""\
        # Next run prep

        TODO list before the next field run.
    """), encoding="utf-8")

    return d


# ── _parse_field_observation_md ───────────────────────────────────────


class TestParser:
    def test_extracts_h1_as_name(self):
        text = "# My field observation\n\nSome content here.\n"
        name, desc, body = _parse_field_observation_md(text)
        assert name == "My field observation"

    def test_extracts_first_paragraph_as_description(self):
        text = (
            "# Observation\n\n"
            "First real paragraph with the summary.\n\n"
            "Second paragraph after.\n"
        )
        name, desc, body = _parse_field_observation_md(text)
        assert "First real paragraph" in desc
        assert "Second paragraph" not in desc

    def test_skips_blockquote_marker_in_description(self):
        text = (
            "# Observation\n\n"
            "> This is the kicker quote.\n"
        )
        name, desc, body = _parse_field_observation_md(text)
        assert "This is the kicker quote" in desc

    def test_skips_field_obs_preamble_lines(self):
        # The **Date:** / **Project path:** lines should not become the description.
        text = (
            "# Observation\n\n"
            "**Date:** 2026-05-06\n"
            "**Project path:** /tmp/foo\n\n"
            "The actual summary paragraph that should win.\n"
        )
        name, desc, body = _parse_field_observation_md(text)
        assert "actual summary paragraph" in desc
        assert "Date" not in desc
        assert "Project path" not in desc

    def test_body_is_full_text_unchanged(self):
        text = "# Title\n\nSome content.\n\n## Section\n\nMore.\n"
        name, desc, body = _parse_field_observation_md(text)
        assert body == text

    def test_no_h1_falls_back_to_default_name(self):
        text = "Just some prose without a heading.\n"
        name, desc, body = _parse_field_observation_md(text)
        assert name == "Field observation"
        assert "Just some prose" in desc


# ── ingest_field_observation_file ─────────────────────────────────────


class TestIngestSingleFile:
    def test_writes_skill_with_user_provenance(self, state_home, tmp_path):
        path = tmp_path / "obs.md"
        path.write_text("# Test obs\n\nDescription here.\n", encoding="utf-8")
        r = ingest_field_observation_file(path)
        assert r.written is True
        assert r.skill is not None
        assert r.skill.created_by == "user"
        assert r.skill.pinned is True
        assert r.skill.scope == "global"

    def test_skill_id_from_file_stem(self, state_home, tmp_path):
        path = tmp_path / "my-special-obs.md"
        path.write_text("# Test\n\nBody.\n", encoding="utf-8")
        r = ingest_field_observation_file(path)
        assert r.skill_id == "my-special-obs"

    def test_idempotent_second_call_skipped(self, state_home, tmp_path):
        path = tmp_path / "obs.md"
        path.write_text("# Test\n\nBody.\n", encoding="utf-8")
        first = ingest_field_observation_file(path)
        assert first.written is True
        second = ingest_field_observation_file(path)
        assert second.written is False
        assert "already exists" in second.skipped_reason
        # Same skill returned (unchanged on disk).
        assert second.skill is not None
        assert second.skill.id == first.skill.id

    def test_force_overwrites(self, state_home, tmp_path):
        path = tmp_path / "obs.md"
        path.write_text("# Original\n\nFirst.\n", encoding="utf-8")
        first = ingest_field_observation_file(path)
        # Modify file then force re-ingest.
        path.write_text("# Updated\n\nSecond version.\n", encoding="utf-8")
        second = ingest_field_observation_file(path, force=True)
        assert second.written is True
        assert second.skill is not None
        assert second.skill.name == "Updated"
        # On disk the new name is what's loaded.
        loaded = load_skill(first.skill.id)
        assert loaded is not None
        assert loaded.name == "Updated"

    def test_dry_run_does_not_write(self, state_home, tmp_path):
        path = tmp_path / "obs.md"
        path.write_text("# Dry\n\nNot persisted.\n", encoding="utf-8")
        r = ingest_field_observation_file(path, dry_run=True)
        assert r.written is False
        assert r.dry_run is True
        assert r.skill is not None  # parsed but not saved
        assert load_skill(r.skill_id) is None  # truly not on disk

    def test_missing_file_raises(self, state_home, tmp_path):
        with pytest.raises(FileNotFoundError):
            ingest_field_observation_file(tmp_path / "no-such.md")

    def test_procedure_md_is_full_body(self, state_home, tmp_path):
        body = "# Test\n\nIntro.\n\n## Section\n\nMore content here.\n"
        path = tmp_path / "obs.md"
        path.write_text(body, encoding="utf-8")
        ingest_field_observation_file(path)
        # Check the procedure.md sidecar contains the full body.
        from resonant_client.orchestration.skills import skill_dir
        sd = skill_dir("obs", scope="global")
        procedure = (sd / "procedure.md").read_text(encoding="utf-8")
        assert "## Section" in procedure
        assert "More content here" in procedure


# ── ingest_field_observation_dir ──────────────────────────────────────


class TestIngestDirectory:
    def test_walks_md_files(self, state_home, field_obs_dir):
        results = ingest_field_observation_dir(field_obs_dir)
        # Two field-obs docs ingested; NEXT-RUN-PREP skipped (excluded by heuristic).
        ids = sorted(r.skill_id for r in results)
        assert "2026-05-06-self-improvement-loop" in ids
        assert "2026-05-03-resonant-linux-bridge" in ids
        # Heuristic skipped NEXT-RUN-PREP entirely (not even returned).
        assert all("next-run" not in i.lower() for i in ids)

    def test_all_ingested_skills_have_user_provenance(self, state_home, field_obs_dir):
        ingest_field_observation_dir(field_obs_dir)
        users = list_skills_filtered(created_by="user")
        assert len(users) == 2
        for s in users:
            assert s.pinned is True
            assert s.scope == "global"

    def test_re_run_is_noop(self, state_home, field_obs_dir):
        first = ingest_field_observation_dir(field_obs_dir)
        assert sum(1 for r in first if r.written) == 2
        second = ingest_field_observation_dir(field_obs_dir)
        assert sum(1 for r in second if r.written) == 0
        for r in second:
            assert "already exists" in r.skipped_reason

    def test_missing_directory_raises(self, state_home, tmp_path):
        with pytest.raises(NotADirectoryError):
            ingest_field_observation_dir(tmp_path / "nope")

    def test_planning_docs_skipped(self, state_home, tmp_path):
        d = tmp_path / "obs"
        d.mkdir()
        # Three docs all matching planning markers.
        for stem in ("NEXT-RUN-PREP", "TODO-tasks", "DRAFT-spec"):
            (d / f"{stem}.md").write_text("# x\n\ny\n", encoding="utf-8")
        # One real field-obs.
        (d / "2026-05-06-real.md").write_text("# Real obs\n\nBody.\n", encoding="utf-8")
        results = ingest_field_observation_dir(d)
        assert len(results) == 1
        assert results[0].skill_id == "2026-05-06-real"


# ── CLI: ingest-field-obs ─────────────────────────────────────────────


class TestCliIngestFieldObs:
    def test_subcommand_in_parser(self):
        parser = build_parser()
        args = parser.parse_args(["ingest-field-obs", "/some/path"])
        assert args.command == "ingest-field-obs"
        assert args.path == "/some/path"
        assert args.force is False
        assert args.dry_run is False

    def test_force_flag(self):
        parser = build_parser()
        args = parser.parse_args(["ingest-field-obs", "/p", "--force"])
        assert args.force is True

    def test_dry_run_flag(self):
        parser = build_parser()
        args = parser.parse_args(["ingest-field-obs", "/p", "--dry-run"])
        assert args.dry_run is True

    def test_unknown_path_returns_1(self, state_home, capsys):
        rc = main(["ingest-field-obs", "/no/such/path"])
        captured = capsys.readouterr()
        assert rc == 1
        assert "Path not found" in captured.err

    def test_single_file_ingest(self, state_home, tmp_path, capsys):
        p = tmp_path / "obs.md"
        p.write_text("# CLI test\n\nBody.\n", encoding="utf-8")
        rc = main(["ingest-field-obs", str(p)])
        captured = capsys.readouterr()
        assert rc == 0
        assert "Ingested 1" in captured.out
        assert "obs" in captured.out
        assert load_skill("obs") is not None

    def test_directory_ingest(self, state_home, field_obs_dir, capsys):
        rc = main(["ingest-field-obs", str(field_obs_dir)])
        captured = capsys.readouterr()
        assert rc == 0
        assert "Ingested 2" in captured.out

    def test_dry_run_does_not_persist_via_cli(self, state_home, tmp_path, capsys):
        p = tmp_path / "obs.md"
        p.write_text("# Dry\n\nBody.\n", encoding="utf-8")
        rc = main(["ingest-field-obs", str(p), "--dry-run"])
        captured = capsys.readouterr()
        assert rc == 0
        assert "DRY RUN" in captured.out
        assert load_skill("obs") is None
