"""Final managed claims serialize with Stop using the supported store journal."""
# ruff: noqa: F811 -- imported temporary local execution fixture.

import threading
from types import SimpleNamespace

import pytest

from lumi.engine.swarming.models import AdmissionClosed
from tests.test_swarm_execution import command, fixture, inputs  # noqa: F401


def test_local_stop_waits_for_atomic_final_managed_claim(fixture):
    guard, _ = fixture
    entered, release, stopped = threading.Event(), threading.Event(), threading.Event()
    failures, claimed = [], []

    def claim(context, request_id):
        entered.set()
        assert release.wait(3)
        claimed.append(request_id)

    guard._managed = SimpleNamespace(prepare_request=lambda *args, **kwargs: None,
        claim_request=claim, abandon_request=lambda *args: None)

    def begin():
        try:
            guard.begin_request(purpose="primary", inputs=inputs())
        except BaseException as exc:
            failures.append(exc)

    def stop():
        try:
            command(guard.supervisor, guard.authority, "stop")
            stopped.set()
        except BaseException as exc:
            failures.append(exc)

    begin_thread = threading.Thread(target=begin)
    stop_thread = threading.Thread(target=stop)
    begin_thread.start()
    try:
        assert entered.wait(3), failures
        stop_thread.start()
        assert not stopped.wait(.1), "Stop committed through a stale read-only admission snapshot"
    finally:
        release.set()
        begin_thread.join(3)
        if stop_thread.ident is not None:
            stop_thread.join(3)
    assert failures == [] and stopped.is_set() and len(claimed) == 1
    with pytest.raises(AdmissionClosed):
        guard.begin_request(purpose="primary", inputs=inputs())
    assert len(claimed) == 1
