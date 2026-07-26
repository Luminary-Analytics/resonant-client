"""Tests for the PlanGraph data model and disk persistence."""

from __future__ import annotations

import time

import pytest

from resonant_client.orchestration import (
    PlanGraph,
    PlanNode,
    NodeStatus,
    NodeSpecialization,
    save_graph,
    load_graph,
    snapshot_graph,
    list_snapshots,
    restore_snapshot,
    purge_old_snapshots,
    plans_dir,
)
from resonant_client.orchestration.plan_graph import new_node_id


# ── Fixtures ─────────────────────────────────────────────────────────────


@pytest.fixture
def state_home(tmp_path, monkeypatch):
    home = tmp_path / "state"
    home.mkdir()
    monkeypatch.setenv("RESONANT_STATE_HOME", str(home))
    return home


@pytest.fixture
def project_dir(tmp_path):
    p = tmp_path / "proj"
    p.mkdir()
    return p


def _node(graph, *, goal, parent=None, deps=None, spec=NodeSpecialization.IMPLEMENT):
    n = PlanNode(
        id=new_node_id(), intent_id=graph.intent_id,
        goal=goal, specialization=spec,
        parent_id=parent, depends_on=list(deps or []),
    )
    graph.add_node(n)
    return n


# ── PlanNode validation ─────────────────────────────────────────────────


def test_planode_rejects_unknown_specialization():
    with pytest.raises(ValueError):
        PlanNode(id="x", intent_id="i", goal="g", specialization="bogus")


def test_planode_rejects_unknown_status():
    with pytest.raises(ValueError):
        PlanNode(id="x", intent_id="i", goal="g", status="halfway")


def test_planode_rejects_out_of_range_confidence():
    with pytest.raises(ValueError):
        PlanNode(id="x", intent_id="i", goal="g", confidence=1.5)
    with pytest.raises(ValueError):
        PlanNode(id="x", intent_id="i", goal="g", confidence=-0.1)


# ── PlanGraph mutations ─────────────────────────────────────────────────


def test_add_node_rejects_duplicate_id():
    g = PlanGraph.new("ship dark mode")
    nid = new_node_id()
    g.add_node(PlanNode(id=nid, intent_id=g.intent_id, goal="a"))
    with pytest.raises(ValueError):
        g.add_node(PlanNode(id=nid, intent_id=g.intent_id, goal="b"))


def test_add_node_validates_parent_exists():
    g = PlanGraph.new("ship")
    with pytest.raises(ValueError):
        g.add_node(PlanNode(id="a", intent_id=g.intent_id, goal="x", parent_id="missing"))


def test_add_node_validates_dep_exists():
    g = PlanGraph.new("ship")
    with pytest.raises(ValueError):
        g.add_node(PlanNode(id="a", intent_id=g.intent_id, goal="x", depends_on=["missing"]))


def test_intent_id_mismatch_rejected():
    g = PlanGraph.new("ship")
    foreign = PlanNode(id="a", intent_id="other", goal="x")
    with pytest.raises(ValueError):
        g.add_node(foreign)


# ── DAG semantics ───────────────────────────────────────────────────────


def test_next_runnable_honors_dependencies():
    g = PlanGraph.new("intent")
    a = _node(g, goal="research")
    b = _node(g, goal="implement", deps=[a.id])
    c = _node(g, goal="verify", deps=[b.id])

    assert {n.id for n in g.next_runnable()} == {a.id}, "only the depless node is runnable"
    g.mark_running(a.id)
    g.mark_done(a.id)
    assert {n.id for n in g.next_runnable()} == {b.id}
    g.mark_running(b.id)
    g.mark_done(b.id)
    assert {n.id for n in g.next_runnable()} == {c.id}


def test_abandoned_unblocks_dependents_too():
    """A dependency reaching DONE *or* ABANDONED unblocks its dependents."""
    g = PlanGraph.new("intent")
    a = _node(g, goal="risky")
    b = _node(g, goal="follow-up", deps=[a.id])
    g.mark_abandoned(a.id, reason="not worth it")
    runnable_ids = {n.id for n in g.next_runnable()}
    assert b.id in runnable_ids


def test_is_complete_detects_terminal_only():
    g = PlanGraph.new("intent")
    a = _node(g, goal="a")
    b = _node(g, goal="b")
    assert not g.is_complete()
    g.mark_done(a.id)
    assert not g.is_complete()
    g.mark_abandoned(b.id)
    assert g.is_complete()


# ── Pruning ─────────────────────────────────────────────────────────────


def test_prune_removes_subtree_and_logs_reasons():
    g = PlanGraph.new("intent")
    root = _node(g, goal="parent")
    child_a = _node(g, goal="child-a", parent=root.id)
    child_b = _node(g, goal="child-b", parent=root.id)
    grandchild = _node(g, goal="grand", parent=child_a.id)
    sibling = _node(g, goal="sibling")  # not in subtree

    removed = g.prune_node(root.id, reason="approach abandoned")
    assert removed == 4  # root + 2 children + 1 grandchild
    assert sibling.id in g.nodes
    # All four removed nodes appear in drop_log with the reason
    dropped_ids = {entry["node_id"] for entry in g.drop_log}
    assert {root.id, child_a.id, child_b.id, grandchild.id} == dropped_ids
    assert all(entry["reason"] == "approach abandoned" for entry in g.drop_log)


def test_prune_cleans_dangling_dependencies():
    g = PlanGraph.new("intent")
    a = _node(g, goal="a")
    b = _node(g, goal="b")
    c = _node(g, goal="c", deps=[a.id, b.id])
    g.prune_node(a.id, reason="redundant")
    assert a.id not in g.nodes
    # c keeps b as a dep but a was cleaned out
    assert g.nodes[c.id].depends_on == [b.id]


# ── Subtree rewriting ───────────────────────────────────────────────────


def test_rewrite_subtree_replaces_branch():
    g = PlanGraph.new("intent")
    root = _node(g, goal="root")
    bad = _node(g, goal="bad approach", parent=root.id)
    bad_child = _node(g, goal="bad detail", parent=bad.id)

    new_a_id = new_node_id()
    new_b_id = new_node_id()
    replacements = [
        PlanNode(id=new_a_id, intent_id=g.intent_id, goal="better approach", parent_id=root.id),
        PlanNode(id=new_b_id, intent_id=g.intent_id, goal="better detail", parent_id=new_a_id),
    ]
    g.rewrite_subtree(bad.id, replacements, reason="took a wrong turn")

    assert bad.id not in g.nodes and bad_child.id not in g.nodes
    assert {new_a_id, new_b_id, root.id} <= set(g.nodes.keys())
    # Drop log captures the rewrite reason
    reasons = {entry["reason"] for entry in g.drop_log}
    assert "took a wrong turn" in reasons


# ── Snapshot / restore ──────────────────────────────────────────────────


def test_snapshot_is_deep_copy():
    g = PlanGraph.new("intent")
    a = _node(g, goal="a")
    snap = g.snapshot()
    g.update_confidence(a.id, 0.3)
    # Original mutates, snapshot doesn't
    assert g.nodes[a.id].confidence == 0.3
    assert snap.nodes[a.id].confidence == 1.0


def test_restore_reinstates_dropped_nodes():
    g = PlanGraph.new("intent")
    a = _node(g, goal="keep me")
    b = _node(g, goal="will be dropped")
    snap = g.snapshot()
    g.prune_node(b.id, reason="user clicked the wrong button")
    assert b.id not in g.nodes
    g.restore(snap)
    assert b.id in g.nodes


def test_restore_rejects_mismatched_intent():
    g1 = PlanGraph.new("a")
    g2 = PlanGraph.new("b")
    with pytest.raises(ValueError):
        g1.restore(g2)


# ── JSON round-trip ─────────────────────────────────────────────────────


def test_to_dict_from_dict_round_trip():
    g = PlanGraph.new("ship dark mode")
    a = _node(g, goal="research", spec=NodeSpecialization.RESEARCH)
    b = _node(g, goal="implement", parent=a.id, spec=NodeSpecialization.IMPLEMENT)
    g.update_confidence(b.id, 0.78)
    g.mark_running(a.id)

    raw = g.to_dict()
    restored = PlanGraph.from_dict(raw)
    assert restored.intent == "ship dark mode"
    assert restored.intent_id == g.intent_id
    assert set(restored.nodes.keys()) == {a.id, b.id}
    assert restored.nodes[b.id].confidence == 0.78
    assert restored.nodes[a.id].status == NodeStatus.RUNNING


def test_from_dict_tolerates_unknown_node_fields():
    """Older snapshots with extra fields should still load (filter unknown keys)."""
    g = PlanGraph.new("intent")
    a = _node(g, goal="x")
    raw = g.to_dict()
    raw["nodes"][0]["future_field"] = "ignored"
    restored = PlanGraph.from_dict(raw)
    assert a.id in restored.nodes


# ── Persistence: save / load ────────────────────────────────────────────


def test_save_load_round_trip(state_home, project_dir):
    g = PlanGraph.new("intent")
    a = _node(g, goal="x")
    g.mark_running(a.id, agent_session_id="sess-1")
    save_graph(g, str(project_dir))
    loaded = load_graph(g.intent_id, str(project_dir))
    assert loaded is not None
    assert loaded.nodes[a.id].agent_session_id == "sess-1"
    assert loaded.nodes[a.id].status == NodeStatus.RUNNING


def test_load_returns_none_for_missing(state_home, project_dir):
    assert load_graph("does-not-exist", str(project_dir)) is None


def test_plans_dir_lives_under_state_home(state_home, project_dir):
    pdir = plans_dir(str(project_dir))
    assert state_home in pdir.parents


# ── Persistence: snapshots ──────────────────────────────────────────────


def test_snapshot_then_restore(state_home, project_dir):
    g = PlanGraph.new("intent")
    a = _node(g, goal="keep")
    b = _node(g, goal="drop me")
    snap_path = snapshot_graph(g, str(project_dir))
    assert snap_path.exists()
    # Mutate live graph
    g.prune_node(b.id, reason="oops")
    save_graph(g, str(project_dir))
    # List + restore
    snaps = list_snapshots(str(project_dir), intent_id=g.intent_id)
    assert len(snaps) == 1
    assert snaps[0]["intent_id"] == g.intent_id
    assert snaps[0]["node_count"] == 2  # snapshot was taken before the prune
    restored = restore_snapshot(
        str(project_dir), ts_ms=snaps[0]["ts_ms"], intent_id=g.intent_id,
    )
    assert restored is not None
    assert b.id in restored.nodes


def test_snapshot_filter_by_intent(state_home, project_dir):
    g1 = PlanGraph.new("intent-1")
    _node(g1, goal="x")
    g2 = PlanGraph.new("intent-2")
    _node(g2, goal="y")
    snapshot_graph(g1, str(project_dir))
    snapshot_graph(g2, str(project_dir))

    all_snaps = list_snapshots(str(project_dir))
    assert len(all_snaps) == 2

    just_g1 = list_snapshots(str(project_dir), intent_id=g1.intent_id)
    assert len(just_g1) == 1
    assert just_g1[0]["intent_id"] == g1.intent_id


def test_purge_drops_old_snapshots(state_home, project_dir):
    g = PlanGraph.new("intent")
    _node(g, goal="x")
    snap_path = snapshot_graph(g, str(project_dir))
    # Backdate the snapshot file in-place by renaming with an old timestamp
    old_ts = int((time.time() - 60 * 86400) * 1000)
    aged = snap_path.with_name(f"{old_ts}__{g.intent_id}.json")
    snap_path.rename(aged)

    purged = purge_old_snapshots(str(project_dir), retention_days=30)
    assert purged == 1
    assert not aged.exists()


def test_purge_keeps_recent(state_home, project_dir):
    g = PlanGraph.new("intent")
    _node(g, goal="x")
    snapshot_graph(g, str(project_dir))
    purged = purge_old_snapshots(str(project_dir), retention_days=30)
    assert purged == 0
    assert len(list_snapshots(str(project_dir))) == 1


# ── Misc ────────────────────────────────────────────────────────────────


def test_critical_path_running_excludes_leaves():
    g = PlanGraph.new("intent")
    leaf = _node(g, goal="leaf")  # nothing depends on it
    middle = _node(g, goal="middle")
    leaf_after_middle = _node(g, goal="after", deps=[middle.id])
    g.mark_running(leaf.id)
    g.mark_running(middle.id)

    critical_ids = {n.id for n in g.critical_path_running()}
    assert middle.id in critical_ids
    assert leaf.id not in critical_ids


def test_audit_log_records_status_changes():
    g = PlanGraph.new("intent")
    a = _node(g, goal="x")
    g.mark_running(a.id, agent_session_id="sess")
    g.mark_done(a.id, confidence=0.9)
    kinds = [e["kind"] for e in g.nodes[a.id].audit_log]
    assert kinds == ["status_change", "status_change"]
