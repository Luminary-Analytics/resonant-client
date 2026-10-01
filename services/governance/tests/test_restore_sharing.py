"""Actual encrypted sharing backup/tail reconciliation in isolated databases."""
# ruff: noqa: F811 -- imported fixtures build isolated source/archive/target DBs.

import json
import time

import psycopg
from psycopg.rows import dict_row
import pytest

from sonn_governance.content import ContentKeys
from sonn_governance.hosts import HostGovernance
from sonn_governance.managed_collaboration import ManagedCollaboration
from sonn_governance.managed_resources import ManagedResources
from sonn_governance.models import Conflict, Principal
from sonn_governance.monitoring import RunMonitoring
from sonn_governance.restore_sharing import retention_records
from sonn_governance.sharing_retention import SharingRetention
from test_archive import archive_fixture  # noqa: F401
from test_managed_collaboration import Fixture, policy
from test_migrations import isolated_database  # noqa: F401
from test_restore import apply, lane, pg_tool, restore, seal, snapshot  # noqa: F401
from test_store import uid


@pytest.fixture
def shared_lane(lane):
    # Reuse the archived tenant and its first policy revision, rather than
    # silently creating sharing outside the archive's configured tenant scope.
    f = Fixture.__new__(Fixture)
    f.tenant = lane.t
    f.hosts = HostGovernance(lane.t.store, minimum_runner_protocol=2)
    f.monitor, f.resources = RunMonitoring(f.hosts), ManagedResources(f.hosts)
    f.sharing = ManagedCollaboration(f.hosts, ContentKeys({"test": b"k" * 32}, "test"))
    f.sharing.set_policy(lane.t.admin, tenant_id=lane.t.id, project_id=lane.t.project,
                         command_id=uid(), expected_revision=0, policy=policy())
    f.a, f.b = f.person(), f.person()
    custodian = Principal("https://issuer.example", uid(), time.time() + 3600)
    lane.t.member(custodian, projects={lane.t.project: ["retention_admin"]})
    retention = SharingRetention(lane.t.store)
    lane.sharing = f
    lane.sharing_ids = {}
    for key in ("deleted", "held", "released_deleted", "kept"):
        grant = f.grant()
        lane.sharing_ids[key] = {"terms": grant, "message": f.send(grant)}

    def retain(key, operation, revision=1):
        for kind, identity in lane.sharing_ids[key].items():
            retention.retain(custodian, lane.t.id, kind, identity, command_id=uid(),
                expected_revision=revision, operation=operation,
                reason="legal_review" if operation == "hold_sharing_content" else None)

    retain("released_deleted", "hold_sharing_content")
    pg_tool("pg_dump", lane.f.owner, "--format=custom", "--schema=sonn_governance", "--file", str(lane.dump))
    retain("deleted", "delete_sharing_content")
    retain("held", "hold_sharing_content")
    retain("released_deleted", "release_sharing_content_hold", 2)
    retain("released_deleted", "delete_sharing_content", 3)
    grant = f.grant()
    lane.sharing_ids["absent"] = {"terms": grant, "message": f.send(grant)}
    retain("absent", "delete_sharing_content")
    while lane.f.relay.drain(lane.t.id)["delivered"]:
        pass
    return lane


def sharing_snapshot(lane):
    with psycopg.connect(lane.target, row_factory=dict_row) as connection:
        return [connection.execute(f"SELECT * FROM sonn_governance.{table} ORDER BY {key}").fetchall()
                for table, key in (("sharing_grants", "grant_id"), ("sharing_messages", "message_id"))]


def test_actual_restore_removes_sharing_payloads_retains_accounting_and_unknown_holds(shared_lane):
    lane = shared_lane
    saved = seal(lane)
    assert "Selected private task" not in json.dumps(saved) and "legal_review" not in json.dumps(saved)
    restore(lane)
    original = sharing_snapshot(lane)
    result = apply(lane, saved)
    assert result["sharing_objects"] == 10 and result["sharing_deleted"] == 6
    assert result["sharing_absent_objects"] == 2 and result["sharing_held_with_reason_unresolved"] == 2
    rows = sharing_snapshot(lane)
    for index, (kind, identity, fingerprint) in enumerate((("terms", "grant_id", "terms_sha256"), ("message", "message_id", "body_sha256"))):
        old_by_id = {str(row[identity]): row for row in original[index]}
        by_id = {str(row[identity]): row for row in rows[index]}
        for key in ("deleted", "released_deleted"):
            row = by_id[lane.sharing_ids[key][kind]]
            assert all(row[field] is None for field in ("ciphertext", "nonce", "key_id", fingerprint))
            assert row["deleted_at"] and not row["held"]
            if kind == "message":
                assert row["byte_size"] == old_by_id[str(row[identity])]["byte_size"]
                assert row["lineage"] == old_by_id[str(row[identity])]["lineage"]
            else:
                # Retention deletion is not a fabricated historical revocation.
                assert row["revoked_at"] == old_by_id[str(row[identity])]["revoked_at"]
        held = by_id[lane.sharing_ids["held"][kind]]
        assert held["ciphertext"] and held["hold_actor"] is None and held["hold_reason"] is None
        assert by_id[lane.sharing_ids["kept"][kind]] == old_by_id[lane.sharing_ids["kept"][kind]]
    with psycopg.connect(lane.target, row_factory=dict_row) as connection:
        proofs = connection.execute("SELECT kind,resource_id,state,evidence FROM sonn_restore.sharing_retention").fetchall()
        assert len(proofs) == 10
        assert sum(proof["state"] == "held" for proof in proofs) == 2
        assert all("reason" not in proof["evidence"] for proof in proofs)
    before = snapshot(lane), sharing_snapshot(lane)
    assert apply(lane, saved) == result and (snapshot(lane), sharing_snapshot(lane)) == before
    for table, key, kind in (("sharing_grants", "grant_id", "terms"), ("sharing_messages", "message_id", "message")):
        with psycopg.connect(lane.target) as connection:
            with pytest.raises(psycopg.errors.RaiseException, match="quarantined"):
                connection.execute(f"DELETE FROM sonn_governance.{table} WHERE {key}=%s", (lane.sharing_ids["held"][kind],))
    with pytest.raises(psycopg.OperationalError):
        psycopg.connect(lane.target_app)


@pytest.mark.parametrize("damage", ["revision", "hold"])
def test_altered_sharing_backup_rolls_back_all_retention_and_sequence(shared_lane, damage):
    lane = shared_lane
    saved = seal(lane)
    restore(lane)
    with psycopg.connect(lane.target) as connection:
        assignment = "retention_revision=retention_revision+1" if damage == "revision" else "held=true,hold_actor='fixture',hold_reason='legal_review'"
        connection.execute(f"UPDATE sonn_governance.sharing_messages SET {assignment} WHERE message_id=%s", (lane.sharing_ids["held"]["message"],))
    before = snapshot(lane), sharing_snapshot(lane)
    with pytest.raises(Conflict, match="sharing payload"):
        apply(lane, saved)
    assert (snapshot(lane), sharing_snapshot(lane)) == before


def test_interrupted_sharing_apply_rolls_back_payload_deletion_and_receipts(shared_lane, monkeypatch):
    import sonn_governance.restore as module
    lane = shared_lane
    saved = seal(lane)
    restore(lane)
    original = module._archive
    calls = 0

    def interrupt(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("fixture interrupted after sharing deletion")
        return original(*args, **kwargs)

    monkeypatch.setattr(module, "_archive", interrupt)
    before = snapshot(lane), sharing_snapshot(lane)
    with pytest.raises(RuntimeError, match="after sharing deletion"):
        apply(lane, saved)
    assert (snapshot(lane), sharing_snapshot(lane)) == before
    with psycopg.connect(lane.target) as connection:
        assert connection.execute("SELECT count(*) FROM sonn_restore.sharing_retention").fetchone()[0] == 0
    monkeypatch.undo()
    assert apply(lane, saved)["sharing_deleted"] == 6


def test_historical_unbound_sharing_creation_is_not_guessed_from_semantic_hash():
    event = {"operation": "sharing_offer", "project_id": uid(), "decision_id": None,
             "resource_revision": None, "semantics_sha256": "a" * 64}
    with pytest.raises(Conflict, match="exact object binding"):
        retention_records([{"payload": json.dumps(event).encode()}])
