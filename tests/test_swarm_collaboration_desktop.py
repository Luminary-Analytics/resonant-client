"""Real SQLite/native Session integration; scripted provider, no live accounts."""
from dataclasses import replace
import threading
import time

import pytest

from lumi.engine.swarming.collaboration import CollaborationTerms
from lumi.engine.swarming.models import Conflict, ScopeDenied
from lumi.engine.swarming.service import CapturedSession, SwarmRuntime
from lumi.engine.swarming import Scope
from lumi.gui.runtime import BackendSpec
from lumi.gui.settings import SettingsManager
from tests.streaming_stub import StreamingBackend, done, text_delta, tool_call


@pytest.fixture
def pair(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    (project / "fact.txt").write_text("Receiver-owned evidence", encoding="utf-8")
    instances = []
    def factory(spec):
        backend = StreamingBackend(scripts=[[tool_call("file_read", {"path": "fact.txt"}), done()],
                                           [text_delta("Read receiver evidence; owner review required."), done()]])
        backend.name, backend.model = spec.backend_type, spec.model
        instances.append(backend)
        return backend
    runtime = SwarmRuntime(SettingsManager(tmp_path / "settings.json"), backend_factory=factory,
                           state_root=lambda _: tmp_path / "state")
    a = CapturedSession(Scope.personal("owner", "project", "session-a"), str(project), BackendSpec("ollama", "explicit-a"))
    b = replace(a, scope=replace(a.scope, session_id="session-b"), backend_spec=BackendSpec("ollama", "explicit-b"))
    runtime.operate(a, {"action": "configure", "enabled": True, "request_id": "enable"})
    yield runtime, a, b, instances
    runtime.close()


def prepare(runtime, capture, key):
    return runtime.operate(capture, {"action": "collaboration_prepare", "request_id": key,
                                     "objective": "Explicit receiver investigations", "request_limit": 6})["run"]["run"]["id"]


def send(runtime, capture, run_id, action, key, **fields):
    revision = runtime.operate(capture, {"action": "view", "run_id": run_id, "request_id": "refresh"})["run"]["run"]["revision"]
    return runtime.operate(capture, {"action": action, "run_id": run_id, "request_id": key,
                                     "expected_revision": revision, **fields})


def grant(runtime, a, b, first, second, key="offer", max_messages=20):
    terms = CollaborationTerms("Inspect shared finding", frozenset({"finding", "work_request"}),
        frozenset({"summary"}), time.time() + 600, frozenset({first, second}), max_messages=max_messages).to_dict()
    offered = send(runtime, a, first, "collaboration_offer", key,
                   receiver_session_id=b.scope.session_id, receiver_run_id=second, terms=terms)
    grant_id = offered["collaboration"]["grants"][0]["grant_id"]
    viewed = runtime.operate(b, {"action": "collaboration_inspect", "run_id": second, "request_id": "inspect", "grant_id": grant_id})
    detail = viewed["collaboration"]["detail"]
    send(runtime, b, second, "collaboration_approve", "approve-" + key, grant_id=grant_id, terms_sha256=detail["terms_sha256"])
    return grant_id


def test_two_idle_teams_no_provider_and_independent_saved_models(pair):
    runtime, a, b, instances = pair
    first, second = prepare(runtime, a, "a"), prepare(runtime, b, "b")
    assert first != second and runtime.busy and not runtime.navigation_busy and instances == []
    assert runtime.captured_run(first, a.workspace, a.scope.session_id).backend_spec.model == "explicit-a"
    assert runtime.captured_run(second, b.workspace, b.scope.session_id).backend_spec.model == "explicit-b"
    assert prepare(runtime, a, "a") == first  # Exact lost-ack retry creates nothing.
    with pytest.raises(Conflict):
        prepare(runtime, a, "another")
    send(runtime, a, first, "stop", "stop-a")
    assert runtime.operate(b, {"action": "view", "run_id": second, "request_id": "b-view"})["run"]["run"]["state"] == "running"


def test_explicit_delivery_own_assignment_and_stop_isolation(pair):
    runtime, a, b, instances = pair
    first, second = prepare(runtime, a, "a"), prepare(runtime, b, "b")
    grant_id = grant(runtime, a, b, first, second)
    send(runtime, a, first, "collaboration_send", "message", grant_id=grant_id, kind="work_request", data_class="summary", body="Untrusted peer suggestion")
    view = runtime.operate(b, {"action": "collaboration_inspect", "run_id": second, "request_id": "pending", "grant_id": grant_id})
    pending = view["collaboration"]["detail"]["messages"][0]
    assert "body" not in pending and "Untrusted peer suggestion" not in str(view)
    delivered = send(runtime, b, second, "collaboration_deliver", "deliver", grant_id=grant_id, message_id=pending["id"])
    assert delivered["collaboration"]["detail"]["messages"][0]["body"] == "Untrusted peer suggestion"
    gate, entered = threading.Event(), threading.Event()
    original = runtime._factory
    def held(spec):
        backend = original(spec)
        stream = backend.stream
        def blocked(**kwargs):
            entered.set()
            assert gate.wait(4)
            yield from stream(**kwargs)
        backend.stream = blocked
        return backend
    runtime._runners[second][1]._factory = held
    try:
        accepted = send(runtime, b, second, "collaboration_accept_work", "accept", grant_id=grant_id, message_id=pending["id"],
            objective="Read fact.txt for my own task", read_roots=["fact.txt"], requests=3, evidence="I reviewed the proposal and choose this bounded investigation")
        assert entered.wait(3) and runtime.navigation_busy
        assert len(accepted["run"]["attempts"]) == 1 and accepted["run"]["reservations"][0]["amount"] == 3
        replay = send(runtime, b, second, "collaboration_accept_work", "accept", grant_id=grant_id, message_id=pending["id"],
            objective="Read fact.txt for my own task", read_roots=["fact.txt"], requests=3, evidence="I reviewed the proposal and choose this bounded investigation")
        assert len(replay["run"]["attempts"]) == 1 and len(instances) == 1
        with pytest.raises(Conflict, match="active"):
            prepare(runtime, replace(a, scope=replace(a.scope, session_id="third")), "third")
        send(runtime, a, first, "stop", "origin-stop")
        assert runtime._runners[second][1].inspect_all()[0]["alive"]
    finally:
        gate.set()
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and (runtime._runners[second][1].inspect_all()[0]["alive"] or runtime.navigation_busy):
        threading.Event().wait(.02)
    snapshot = runtime.operate(b, {"action": "view", "request_id": "finished", "run_id": second})["run"]
    assert snapshot["work_items"][0]["state"] == "submitted" and not runtime.navigation_busy
    assert instances[0].model == "explicit-b"
    assert "Untrusted peer suggestion" not in str(instances[0].stream_calls)


def test_history_scope_revocation_and_retained_delivery(pair):
    runtime, a, b, _ = pair
    first, second = prepare(runtime, a, "a"), prepare(runtime, b, "b")
    grant_id = grant(runtime, a, b, first, second)
    send(runtime, a, first, "collaboration_send", "message", grant_id=grant_id, kind="finding", data_class="summary", body="Explicitly selected fact")
    viewed = runtime.operate(b, {"action": "collaboration_inspect", "run_id": second, "request_id": "inspect", "grant_id": grant_id})
    message_id = viewed["collaboration"]["detail"]["messages"][0]["id"]
    send(runtime, b, second, "collaboration_deliver", "deliver", message_id=message_id)
    send(runtime, a, first, "collaboration_revoke", "revoke", grant_id=grant_id, evidence="Close future disclosure")
    send(runtime, b, second, "stop", "stop")
    history = runtime.operate(b, {"action": "collaboration_inspect", "run_id": second, "request_id": "historical", "grant_id": grant_id})
    assert history["collaboration"]["detail"]["state"] == "revoked"
    assert history["collaboration"]["detail"]["messages"][0]["body"] == "Explicitly selected fact"
    with pytest.raises(ScopeDenied):
        runtime.operate(b, {"action": "collaboration_inspect", "run_id": first, "request_id": "foreign", "grant_id": grant_id})
    assert "supervisor_id" not in str(history["collaboration"]) and "api_key" not in str(history["collaboration"])


def test_message_history_is_bounded_and_cursor_is_scoped(pair):
    runtime, a, b, _ = pair
    first, second = prepare(runtime, a, "a"), prepare(runtime, b, "b")
    grant_id = grant(runtime, a, b, first, second, max_messages=30)
    for index in range(21):
        send(runtime, a, first, "collaboration_send", f"message-{index}", grant_id=grant_id, kind="finding", data_class="summary", body=f"Private selected fact {index}")
    first_page = runtime.operate(b, {"action": "collaboration_inspect", "run_id": second, "request_id": "page-one", "grant_id": grant_id})["collaboration"]["detail"]
    assert len(first_page["messages"]) == 20 and all("body" not in row for row in first_page["messages"])
    second_page = runtime.operate(b, {"action": "collaboration_inspect", "run_id": second, "request_id": "page-two",
        "grant_id": grant_id, "before_message_id": first_page["next_before_message_id"]})["collaboration"]["detail"]
    assert len(second_page["messages"]) == 1 and second_page["next_before_message_id"] is None
    assert second_page["messages"][0]["id"] not in {row["id"] for row in first_page["messages"]}
    with pytest.raises(ScopeDenied):
        runtime.operate(b, {"action": "collaboration_inspect", "run_id": second, "request_id": "bad-cursor",
                           "grant_id": grant_id, "before_message_id": "foreign"})
    with pytest.raises(ScopeDenied):
        runtime.operate(b, {"action": "collaboration_inspect", "run_id": second, "request_id": "bad-grant", "before_grant_id": "foreign"})


def test_failed_dispatch_is_never_replayed_and_keeps_navigation_closed(pair, monkeypatch):
    runtime, a, b, instances = pair
    first, second = prepare(runtime, a, "a"), prepare(runtime, b, "b")
    grant_id = grant(runtime, a, b, first, second)
    send(runtime, a, first, "collaboration_send", "message", grant_id=grant_id, kind="work_request", data_class="summary", body="Investigate fact")
    proposal = runtime.operate(b, {"action": "collaboration_inspect", "run_id": second, "request_id": "inspect", "grant_id": grant_id})["collaboration"]["detail"]["messages"][0]
    send(runtime, b, second, "collaboration_deliver", "deliver", message_id=proposal["id"])
    launches = []
    def ambiguous(*args, **kwargs):
        launches.append(args)
        raise OSError("Launch acknowledgement unavailable")
    monkeypatch.setattr(runtime._runners[second][1], "start", ambiguous)
    fields = {"grant_id": grant_id, "message_id": proposal["id"], "objective": "Inspect fact independently",
              "read_roots": ["fact.txt"], "requests": 3, "evidence": "Choose my own bounded task"}
    with pytest.raises(OSError, match="acknowledgement"):
        send(runtime, b, second, "collaboration_accept_work", "ambiguous", **fields)
    result = send(runtime, b, second, "collaboration_accept_work", "ambiguous", **fields)
    assert len(launches) == 1 and not instances
    assert len(result["run"]["attempts"]) == 1 and runtime.navigation_busy
    assert result["run"]["reservations"][0]["state"] == "reserved"


def test_expired_orphaned_preparation_does_not_authorize_idle_navigation(pair):
    runtime, a, b, _ = pair
    first = prepare(runtime, a, "a")
    from lumi.engine.swarming.collaboration_desktop import navigation_idle
    snapshot = runtime.operate(a, {"action": "view", "run_id": first, "request_id": "view"})["run"]
    assert navigation_idle(snapshot)
    snapshot["reservations"] = [{"state": "uncertain"}]
    assert not navigation_idle(snapshot)
    # An unowned retained run cannot be treated as one of this host's empty
    # collaboration teams, even when its graph contains no attempts.
    other = SwarmRuntime(runtime.settings, backend_factory=runtime._factory, state_root=runtime._state_root)
    try:
        with pytest.raises(Conflict, match="active"):
            prepare(other, b, "unowned")
    finally:
        other.close()
