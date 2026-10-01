"""Owner history and paged content preserve scope and immutable provenance."""
from dataclasses import replace
import hashlib
from pathlib import Path

import pytest

from lumi.engine.swarming import Scope, SwarmStore
from lumi.engine.swarming.artifacts import SwarmArtifacts
from lumi.engine.swarming.inspection import SwarmInspection
from lumi.engine.swarming.models import ScopeDenied


@pytest.fixture
def evidence(tmp_path):
    store = SwarmStore(tmp_path / "swarm.sqlite3")
    scope = Scope.personal("owner", "project", "session")
    authority = store.create_run(scope, supervisor_id="supervisor", objective="Inspect exact evidence", request_limit=4)
    store.add_work_item(authority, work_item_id="item", objective="Inspect")
    context = store.claim(authority, work_item_id="item", worker_id="reader", requests=2, command_id="claim").context
    artifacts = SwarmArtifacts(store)
    return store, authority, context, artifacts, SwarmInspection(store)


@pytest.mark.parametrize("field", ("tenant_id", "owner_id", "project_id", "session_id"))
def test_each_history_and_content_request_checks_complete_captured_scope(evidence, field):
    _, authority, context, artifacts, inspector = evidence
    ref = artifacts.publish_text(context, "Private evidence")
    foreign = replace(authority.scope, **{field: "foreign"})
    assert inspector.history(foreign) == {"items": [], "next_before_run_id": None}
    with pytest.raises(ScopeDenied):
        inspector.history(foreign, before_run_id=authority.run_id)
    with pytest.raises(ScopeDenied):
        inspector.read_artifact(foreign, authority.run_id, ref.id)


def test_history_cursor_is_stable_bounded_and_never_leaks_authority(evidence):
    store, authority, _, _, inspector = evidence
    ids = [authority.run_id]
    for number in range(5):
        ids.append(store.create_run(authority.scope, supervisor_id=f"private-supervisor-{number}",
            objective="x" * 301, request_limit=2).run_id)
    first = inspector.history(authority.scope, limit=2)
    assert [row["run_id"] for row in first["items"]] == ids[-2:][::-1]
    assert first["next_before_run_id"] == ids[-2]
    store.create_run(authority.scope, supervisor_id="newest", objective="Newly arrived", request_limit=1)
    second = inspector.history(authority.scope, before_run_id=first["next_before_run_id"], limit=2)
    last = inspector.history(authority.scope, before_run_id=second["next_before_run_id"], limit=2)
    assert [row["run_id"] for row in first["items"] + second["items"] + last["items"]] == ids[::-1]
    assert last["next_before_run_id"] is None
    assert first["items"][0]["objective_truncated"] and len(first["items"][0]["objective"]) == 300
    assert "supervisor" not in repr(first) and "lease" not in repr(first) and "path" not in repr(first)


def test_every_unicode_page_reassembles_exact_text_after_owner_stop(evidence):
    store, authority, context, artifacts, inspector = evidence
    content = "a" * 65535 + "🪷é\r\n<script>not executable</script>" + "end\n" * 7000
    ref = artifacts.publish_text(context, content)
    store.stop(authority, command_id="stop")
    pieces, offset = [], 0
    while True:
        page = inspector.read_artifact(authority.scope, authority.run_id, ref.id, offset=offset, limit=16000)
        assert page["verified_sha256"] == hashlib.sha256(content.encode()).hexdigest()
        assert page["total_characters"] == len(content) and len(page["text"]) <= 16000
        assert set(page["artifact"]) == set(ref.to_dict())
        pieces.append(page["text"])
        if page["next_offset"] is None:
            break
        offset = page["next_offset"]
    assert "".join(pieces) == content
    assert inspector.read_artifact(authority.scope, authority.run_id, ref.id, offset=len(content))["text"] == ""
    with pytest.raises(ValueError, match="after"):
        inspector.read_artifact(authority.scope, authority.run_id, ref.id, offset=len(content) + 1)


def test_corruption_after_requested_page_prevents_any_disclosure(evidence):
    _, authority, context, artifacts, inspector = evidence
    ref = artifacts.publish_text(context, "First page\n" + "z" * 70000)
    blob = artifacts._blob_path(ref.sha256)
    blob.write_bytes(blob.read_bytes()[:-1] + b"X")
    with pytest.raises(ValueError, match="complete content"):
        inspector.read_artifact(authority.scope, authority.run_id, ref.id, limit=5)


def test_invalid_utf8_and_missing_bytes_fail_without_private_storage_paths(evidence):
    _, authority, context, artifacts, inspector = evidence
    ref = artifacts.publish_bytes(context, b"readable\xff", kind="text")
    with pytest.raises(ValueError, match="UTF-8"):
        inspector.read_artifact(authority.scope, authority.run_id, ref.id)
    artifacts._blob_path(ref.sha256).unlink()
    with pytest.raises(ValueError) as error:
        inspector.read_artifact(authority.scope, authority.run_id, ref.id)
    assert str(artifacts.root) not in str(error.value)


def test_nontext_is_verified_but_never_claimed_as_text_or_visual_analysis(evidence):
    _, authority, context, artifacts, inspector = evidence
    ref = artifacts.publish_bytes(context, b"fake-image-bytes", kind="image", media_type="image/png")
    page = inspector.read_artifact(authority.scope, authority.run_id, ref.id)
    assert page["format"] == "unsupported" and page["text"] is None
    assert page["total_characters"] is None and page["next_offset"] is None
    assert page["verified_sha256"] == ref.sha256 and "No text preview or visual interpretation" in page["message"]


def test_scope_is_rechecked_after_blob_io(evidence, monkeypatch):
    store, authority, context, artifacts, inspector = evidence
    ref = artifacts.publish_text(context, "Owner could lose this scope during I/O")
    real_open = Path.open

    def revoke_before_read(path, *args, **kwargs):
        if path == artifacts._blob_path(ref.sha256):
            with store._connection(write=True) as connection:
                connection.execute("UPDATE runs SET owner_id='other' WHERE id=?", (authority.run_id,))
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", revoke_before_read)
    with pytest.raises(ScopeDenied):
        inspector.read_artifact(authority.scope, authority.run_id, ref.id)


@pytest.mark.parametrize("limit", (0, 51, True, 1.5, "2"))
def test_history_limit_is_bounded_and_typed(evidence, limit):
    _, authority, _, _, inspector = evidence
    with pytest.raises(ValueError):
        inspector.history(authority.scope, limit=limit)


@pytest.mark.parametrize("values", ({"offset": -1}, {"offset": True}, {"limit": 16001}, {"limit": 0}, {"limit": "3"}))
def test_artifact_pages_are_bounded_and_typed(evidence, values):
    _, authority, context, artifacts, inspector = evidence
    ref = artifacts.publish_text(context, "Evidence")
    with pytest.raises(ValueError):
        inspector.read_artifact(authority.scope, authority.run_id, ref.id, **values)
