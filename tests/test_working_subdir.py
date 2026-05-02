"""
Tests for v0.3.5 plan-graph `working_subdir` propagation (closes Bug #25
architectural gap).

When implement specialist #1 scaffolds files into a project subdirectory
(`web/`, `apps/api/`), implement #2 should inherit that as its effective
working directory — not start back at the project root and re-discover
the layout via filesystem walks. This module covers:

- `_extract_working_subdir` parsing (format, edge cases, security)
- `PlanNode.working_subdir` round-trip through to_dict/from_dict
- Runner inheritance from `depends_on` chain
- Runner recording of newly-declared subdirs from the specialist summary
"""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import pytest

from resonant_client.orchestration import (
    NodeSpecialization,
    NodeStatus,
    PlanGraph,
    PlanNode,
    new_node_id,
)
from resonant_client.orchestration.runner import (
    LocalSpecialistRunner,
    _extract_working_subdir,
)


# ── _extract_working_subdir ──────────────────────────────────────────────


class TestExtractWorkingSubdir:
    def test_basic_declaration(self):
        text = "Did the work.\n\nWorking subdir: web/\n"
        assert _extract_working_subdir(text) == "web"

    def test_nested_path(self):
        text = "scaffolded\nWorking subdir: apps/api/v1\n"
        assert _extract_working_subdir(text) == "apps/api/v1"

    def test_case_insensitive_label(self):
        # Models drift on label casing — accept WORKING SUBDIR, etc.
        text = "WORKING SUBDIR: services/auth\n"
        assert _extract_working_subdir(text) == "services/auth"

    def test_strips_quotes_and_backticks(self):
        for quoted in ('"web/"', "'web/'", "`web/`"):
            text = f"Working subdir: {quoted}"
            assert _extract_working_subdir(text) == "web"

    def test_strips_trailing_slash(self):
        assert _extract_working_subdir("Working subdir: web///") == "web"

    def test_normalizes_backslashes(self):
        # Windows-style paths get coerced to forward slashes for the
        # cross-platform os.path.join later.
        assert _extract_working_subdir("Working subdir: apps\\api") == "apps/api"

    def test_returns_none_on_no_declaration(self):
        text = "Just a regular summary with no declaration."
        assert _extract_working_subdir(text) is None

    def test_returns_none_on_empty_string(self):
        assert _extract_working_subdir("") is None

    def test_rejects_absolute_unix_path(self):
        # Absolute paths bypass the "subdir of project root" trust
        # boundary. Refuse rather than pretend to handle.
        assert _extract_working_subdir("Working subdir: /etc/passwd") is None

    def test_rejects_absolute_windows_path(self):
        assert _extract_working_subdir("Working subdir: C:\\Windows") is None
        assert _extract_working_subdir("Working subdir: D:/foo") is None

    def test_rejects_parent_traversal(self):
        # `..` lets a subdir escape the project root.
        assert _extract_working_subdir("Working subdir: ../escape") is None
        assert _extract_working_subdir("Working subdir: web/../../../etc") is None

    def test_handles_extra_whitespace(self):
        text = "  Working subdir:    web/   \n"
        assert _extract_working_subdir(text) == "web"

    def test_empty_value_returns_none(self):
        assert _extract_working_subdir("Working subdir:   ") is None


# ── PlanNode round-trip ──────────────────────────────────────────────────


class TestPlanNodeRoundTrip:
    def test_default_is_none(self):
        node = PlanNode(id="x", intent_id="y", goal="z")
        assert node.working_subdir is None

    def test_explicit_value_persists_through_dict(self):
        node = PlanNode(
            id="x", intent_id="y", goal="z",
            working_subdir="web/api",
        )
        d = node.to_dict()
        assert d["working_subdir"] == "web/api"
        restored = PlanNode.from_dict(d)
        assert restored.working_subdir == "web/api"

    def test_old_snapshot_without_field_still_loads(self):
        # Defensive — snapshots from pre-v0.3.5 PlanGraph.json files
        # don't have the field. from_dict's defensive filter must
        # cope without exploding.
        old_dict = {
            "id": "x", "intent_id": "y", "goal": "z",
            "specialization": NodeSpecialization.IMPLEMENT,
            "status": NodeStatus.PENDING,
            "confidence": 1.0,
        }
        node = PlanNode.from_dict(old_dict)
        assert node.working_subdir is None


# ── Runner inheritance + recording ───────────────────────────────────────


def _make_runner_with_session(text_done):
    """Build a runner whose Session emits a single text.done with the
    given content, then ends. Returns (runner, fake_run, project_path)."""
    backend = MagicMock()
    fake_tools = [{"function": {"name": n}} for n in ("file_read", "glob", "grep",
                                                       "file_write", "file_edit", "bash")]
    runner = LocalSpecialistRunner(
        backend=backend,
        project_path="/tmp/proj",
        all_tools=fake_tools,
        project_instructions="",
        settings=None,
        on_session_event=lambda ev: None,
    )
    captured: dict = {}

    def fake_run(self, user_msg, on_permission=None, on_choice=None,
                 on_user_input=None, images=None):
        # Snapshot the project_path the session was given so the test
        # can assert on whether inheritance landed.
        captured["project_path"] = self.project_path
        yield {"event": "session.start"}
        yield {"event": "text.done", "text": text_done}
        yield {"event": "session.end"}
    return runner, fake_run, captured


class TestRunnerInheritance:
    def test_child_inherits_subdir_from_parent_dep(self):
        # Setup: parent implementer declared `web/`. Child has no
        # subdir set yet. After the runner walks deps, child.working_subdir
        # should be `web/` AND the session should have run with
        # `/tmp/proj/web` as its project_path.
        g = PlanGraph.new("intent")
        parent = PlanNode(
            id=new_node_id(), intent_id=g.intent_id,
            goal="scaffold", specialization=NodeSpecialization.IMPLEMENT,
            status=NodeStatus.DONE,
            working_subdir="web",
            result={"summary": "scaffolded the web app"},
        )
        g.add_node(parent)
        child = PlanNode(
            id=new_node_id(), intent_id=g.intent_id,
            goal="add styles", specialization=NodeSpecialization.IMPLEMENT,
            depends_on=[parent.id],
        )
        g.add_node(child)

        runner, fake_run, captured = _make_runner_with_session("ok")
        with patch("resonant_client.orchestration.runner.Session.run", fake_run):
            runner._run_node(child, g)

        assert child.working_subdir == "web"
        # session.project_path should join the runner root with the subdir
        assert captured["project_path"] == os.path.normpath("/tmp/proj/web")

    def test_explicit_subdir_on_node_takes_precedence(self):
        # If a node already has a subdir set, dep inheritance must not
        # overwrite it (could be set via a separate code path).
        g = PlanGraph.new("intent")
        parent = PlanNode(
            id=new_node_id(), intent_id=g.intent_id,
            goal="x", specialization=NodeSpecialization.IMPLEMENT,
            status=NodeStatus.DONE,
            working_subdir="web",
            result={"summary": "ok"},
        )
        g.add_node(parent)
        child = PlanNode(
            id=new_node_id(), intent_id=g.intent_id,
            goal="y", specialization=NodeSpecialization.IMPLEMENT,
            depends_on=[parent.id],
            working_subdir="apps/api",  # explicit
        )
        g.add_node(child)

        runner, fake_run, captured = _make_runner_with_session("ok")
        with patch("resonant_client.orchestration.runner.Session.run", fake_run):
            runner._run_node(child, g)

        assert child.working_subdir == "apps/api"
        assert captured["project_path"] == os.path.normpath("/tmp/proj/apps/api")

    def test_no_subdir_when_no_deps_have_one(self):
        g = PlanGraph.new("intent")
        node = PlanNode(
            id=new_node_id(), intent_id=g.intent_id,
            goal="z", specialization=NodeSpecialization.IMPLEMENT,
        )
        g.add_node(node)

        runner, fake_run, captured = _make_runner_with_session("ok")
        with patch("resonant_client.orchestration.runner.Session.run", fake_run):
            runner._run_node(node, g)

        assert node.working_subdir is None
        # session uses the bare project_path
        assert captured["project_path"] == "/tmp/proj"


class TestRunnerRecordsDeclaration:
    def test_summary_declaration_lands_on_node(self):
        # Specialist scaffolds a fresh project layout — declares it
        # in the summary. After _run_node returns, the node should
        # carry that declaration so siblings inherit it.
        g = PlanGraph.new("intent")
        node = PlanNode(
            id=new_node_id(), intent_id=g.intent_id,
            goal="scaffold", specialization=NodeSpecialization.IMPLEMENT,
        )
        g.add_node(node)

        summary_text = (
            "Created package.json, vite.config.ts, src/main.ts under a "
            "fresh React scaffold.\n\n"
            "Working subdir: web\n"
        )
        runner, fake_run, _ = _make_runner_with_session(summary_text)
        with patch("resonant_client.orchestration.runner.Session.run", fake_run):
            runner._run_node(node, g)

        assert node.working_subdir == "web"

    def test_refinement_extends_inherited_subdir(self):
        # Parent declared `web/`. Child works inside that and creates
        # a more-specific scaffold at `web/api/`. Child's working_subdir
        # should refine to `web/api`, not regress to `web`.
        g = PlanGraph.new("intent")
        parent = PlanNode(
            id=new_node_id(), intent_id=g.intent_id,
            goal="x", specialization=NodeSpecialization.IMPLEMENT,
            status=NodeStatus.DONE,
            working_subdir="web",
            result={"summary": "ok"},
        )
        g.add_node(parent)
        child = PlanNode(
            id=new_node_id(), intent_id=g.intent_id,
            goal="y", specialization=NodeSpecialization.IMPLEMENT,
            depends_on=[parent.id],
        )
        g.add_node(child)

        # Child declares a refinement.
        runner, fake_run, _ = _make_runner_with_session("ok\nWorking subdir: web/api\n")
        with patch("resonant_client.orchestration.runner.Session.run", fake_run):
            runner._run_node(child, g)

        assert child.working_subdir == "web/api"

    def test_does_not_regress_to_parent_dir(self):
        # Parent declared `web/api`. Child's summary mentions `web` (the
        # broader parent). We must NOT clobber the inheritance with the
        # less-specific path — that's a regression, not a refinement.
        g = PlanGraph.new("intent")
        parent = PlanNode(
            id=new_node_id(), intent_id=g.intent_id,
            goal="x", specialization=NodeSpecialization.IMPLEMENT,
            status=NodeStatus.DONE,
            working_subdir="web/api",
            result={"summary": "ok"},
        )
        g.add_node(parent)
        child = PlanNode(
            id=new_node_id(), intent_id=g.intent_id,
            goal="y", specialization=NodeSpecialization.IMPLEMENT,
            depends_on=[parent.id],
        )
        g.add_node(child)

        runner, fake_run, _ = _make_runner_with_session("ok\nWorking subdir: web\n")
        with patch("resonant_client.orchestration.runner.Session.run", fake_run):
            runner._run_node(child, g)

        # Should preserve the inherited (more-specific) `web/api`.
        assert child.working_subdir == "web/api"
