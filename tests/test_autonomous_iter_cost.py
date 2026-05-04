"""Tests for v0.5.9a2 — `AutonomousIterCostTracker`.

Pure unit tests of the cost-accumulation lifecycle. Integration with
the WS event flow (status events from chat-stream + daemon iter
events) is covered indirectly by the smoke harness; these tests
pin the contract.
"""
from __future__ import annotations

import threading

import pytest

from resonant_client.gui.autonomous_iter_cost import (
    AutonomousIterCostTracker,
    IterCostSnapshot,
)


# ── Lifecycle: open → record → finalize ────────────────────────────────


class TestNormalLifecycle:
    def test_finalize_returns_accumulated_snapshot(self):
        t = AutonomousIterCostTracker()
        t.on_iteration_started("i1", 1, started_at=100.0)
        t.record_status("i1", "deepseek-v4-flash:cloud", 1000, 200, 0.0)
        t.record_status("i1", "deepseek-v4-flash:cloud", 500, 100, 0.0)
        snap = t.on_iteration_finalized("i1", 1, finalized_at=130.0)

        assert snap is not None
        assert snap.iter_count == 1
        assert snap.intent_id == "i1"
        assert snap.tokens_in == 1500
        assert snap.tokens_out == 300
        # No cost since flash is local-priced.
        assert snap.cost_usd == 0.0
        # Two calls accumulated under the same model.
        assert snap.by_model == {
            "deepseek-v4-flash:cloud": {
                "tokens_in": 1500,
                "tokens_out": 300,
                "cost_usd": 0.0,
                "calls": 2,
            },
        }

    def test_per_model_attribution(self):
        # The v0.5.8a1 use case: pro for REFLECT, flash for IMPLEMENT
        # within one iter. Both should show up as distinct entries.
        t = AutonomousIterCostTracker()
        t.on_iteration_started("i1", 1)
        t.record_status("i1", "deepseek-v4-flash:cloud", 5000, 800, 0.0)
        t.record_status("i1", "deepseek-v4-pro:cloud", 12000, 2000, 0.05)
        snap = t.on_iteration_finalized("i1", 1)

        assert set(snap.by_model.keys()) == {
            "deepseek-v4-flash:cloud",
            "deepseek-v4-pro:cloud",
        }
        assert snap.by_model["deepseek-v4-pro:cloud"]["cost_usd"] == 0.05
        assert snap.tokens_in == 17000
        assert snap.cost_usd == 0.05

    def test_to_payload_sorts_by_cost_desc(self):
        t = AutonomousIterCostTracker()
        t.on_iteration_started("i1", 1)
        t.record_status("i1", "model-a", 100, 10, 0.10)
        t.record_status("i1", "model-b", 100, 10, 0.50)
        t.record_status("i1", "model-c", 100, 10, 0.01)
        snap = t.on_iteration_finalized("i1", 1)
        payload = snap.to_payload()
        ordered = [m["model"] for m in payload["by_model"]]
        assert ordered == ["model-b", "model-a", "model-c"]

    def test_finalize_evicts_bucket(self):
        t = AutonomousIterCostTracker()
        t.on_iteration_started("i1", 1)
        t.record_status("i1", "m", 10, 10, 0.001)
        assert t.bucket_count() == 1
        t.on_iteration_finalized("i1", 1)
        assert t.bucket_count() == 0
        assert t.open_iter_for("i1") is None


# ── Edge cases ─────────────────────────────────────────────────────────


class TestEdgeCases:
    def test_record_with_no_open_iter_drops(self):
        # No matching on_iteration_started; status event has nowhere
        # to go. Drop silently — caller can't tell anyway.
        t = AutonomousIterCostTracker()
        t.record_status("i1", "m", 1000, 200, 0.0)
        assert t.bucket_count() == 0

    def test_record_zero_tokens_drops(self):
        t = AutonomousIterCostTracker()
        t.on_iteration_started("i1", 1)
        # Zero-token "heartbeat" status events don't contribute.
        t.record_status("i1", "m", 0, 0, 0.0)
        snap = t.on_iteration_finalized("i1", 1)
        assert snap.tokens_in == 0
        assert snap.tokens_out == 0
        assert snap.by_model == {}

    def test_finalize_without_open_returns_none(self):
        t = AutonomousIterCostTracker()
        snap = t.on_iteration_finalized("i1", 1)
        assert snap is None

    def test_empty_intent_id_is_noop(self):
        t = AutonomousIterCostTracker()
        t.on_iteration_started("", 1)
        t.record_status("", "m", 100, 100, 0.0)
        assert t.bucket_count() == 0
        assert t.on_iteration_finalized("", 1) is None

    def test_negative_tokens_clamped_to_zero(self):
        t = AutonomousIterCostTracker()
        t.on_iteration_started("i1", 1)
        # Pathological case: malformed status event with negative
        # token counts. Clamp to 0 rather than corrupting the bucket.
        t.record_status("i1", "m", -100, -50, -0.10)
        snap = t.on_iteration_finalized("i1", 1)
        assert snap.tokens_in == 0
        assert snap.tokens_out == 0
        assert snap.cost_usd == 0.0

    def test_idempotent_open_keeps_existing_bucket(self):
        # Calling on_iteration_started twice with same (intent, iter)
        # should NOT reset the accumulated data.
        t = AutonomousIterCostTracker()
        t.on_iteration_started("i1", 1)
        t.record_status("i1", "m", 100, 50, 0.001)
        t.on_iteration_started("i1", 1)  # Re-open
        t.record_status("i1", "m", 200, 100, 0.002)
        snap = t.on_iteration_finalized("i1", 1)
        assert snap.tokens_in == 300
        assert snap.tokens_out == 150


class TestImplicitCloseOnNewIter:
    def test_new_iter_routes_status_to_new_bucket(self):
        # If on_iteration_started fires with a NEW iter_count for an
        # intent before the previous one was finalized, subsequent
        # status events should route to the NEW bucket — the old one
        # is left as-is (caller may finalize later) but no longer
        # receives traffic.
        t = AutonomousIterCostTracker()
        t.on_iteration_started("i1", 1)
        t.record_status("i1", "m", 100, 50, 0.001)
        t.on_iteration_started("i1", 2)  # New iter, no finalize on iter 1
        t.record_status("i1", "m", 999, 999, 0.099)

        # iter 1 has only the first 100/50; iter 2 has 999/999.
        snap1 = t.on_iteration_finalized("i1", 1)
        snap2 = t.on_iteration_finalized("i1", 2)
        assert snap1.tokens_in == 100
        assert snap1.tokens_out == 50
        assert snap2.tokens_in == 999
        assert snap2.tokens_out == 999


# ── Multi-intent isolation ─────────────────────────────────────────────


class TestMultiIntentIsolation:
    def test_two_intents_dont_cross_pollinate(self):
        # If two missions are running concurrently (rare per design,
        # but resume + new can race), their cost data must stay
        # separate.
        t = AutonomousIterCostTracker()
        t.on_iteration_started("i1", 1)
        t.on_iteration_started("i2", 1)
        t.record_status("i1", "m", 100, 100, 0.01)
        t.record_status("i2", "m", 200, 200, 0.02)

        snap1 = t.on_iteration_finalized("i1", 1)
        snap2 = t.on_iteration_finalized("i2", 1)
        assert snap1.tokens_in == 100
        assert snap2.tokens_in == 200

    def test_reset_intent_clears_only_that_intent(self):
        t = AutonomousIterCostTracker()
        t.on_iteration_started("i1", 1)
        t.on_iteration_started("i2", 1)
        t.record_status("i1", "m", 100, 100, 0.0)
        t.record_status("i2", "m", 200, 200, 0.0)

        t.reset_intent("i1")

        # i1 cleared, i2 untouched.
        assert t.open_iter_for("i1") is None
        assert t.open_iter_for("i2") == 1
        assert t.on_iteration_finalized("i1", 1) is None
        snap2 = t.on_iteration_finalized("i2", 1)
        assert snap2.tokens_in == 200


# ── Thread safety ──────────────────────────────────────────────────────


class TestThreadSafety:
    def test_concurrent_record_status_no_data_loss(self):
        # 10 threads each contribute 100 status events; final sum
        # must match the deterministic total.
        t = AutonomousIterCostTracker()
        t.on_iteration_started("i1", 1)
        n_threads = 10
        per_thread = 100
        barrier = threading.Barrier(n_threads)

        def worker():
            barrier.wait()
            for _ in range(per_thread):
                t.record_status("i1", "m", 1, 1, 0.0001)

        threads = [
            threading.Thread(target=worker) for _ in range(n_threads)
        ]
        for th in threads:
            th.start()
        for th in threads:
            th.join()

        snap = t.on_iteration_finalized("i1", 1)
        assert snap.tokens_in == n_threads * per_thread
        assert snap.tokens_out == n_threads * per_thread
        # cost_usd is float, allow tiny epsilon. 10 * 100 * 0.0001 = 0.1
        assert abs(snap.cost_usd - 0.1) < 1e-6
