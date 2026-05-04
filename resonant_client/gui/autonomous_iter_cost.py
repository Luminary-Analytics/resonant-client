"""v0.5.9a2 — per-iteration cost + model attribution tracker.

Closes the "what did this iter actually cost?" observability gap
that became material once v0.5.8a1 shipped per-specialist Ollama
model routing. With pro pinned for REFLECT/PLAN_DEEP and flash for
IMPLEMENT/EXPLORE, a single iter can mix cost profiles — and the
flat session-level cost number doesn't surface that.

Architecture: a thread-safe accumulator that lives on AppState and
is updated from two layers:

  1. The chat-stream `status` event handler in app.py records each
     model's token usage as it streams.
  2. The autonomous-event forwarder calls
     `on_iteration_started(intent_id, iter)` to open a bucket, then
     `on_iteration_finalized(intent_id, iter) -> dict` to close it
     and read the breakdown.

The bucket is keyed by (intent_id, iter_count). Multiple
status events for the same iter accumulate; closing the bucket
returns a structured summary the GUI can attach to the iter card.

Buckets are evicted on close to keep memory bounded — a long
running mission with 100+ iters won't accumulate stale buckets.

Tests: pure-Python unit tests in test_autonomous_iter_cost.py.
"""
from __future__ import annotations

import logging
import threading
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class IterCostSnapshot:
    """One iter's accumulated cost + model usage. Per-model rollup
    makes the v0.5.8a1 routing visible: pro/flash split shows up as
    distinct entries."""
    iter_count: int = 0
    intent_id: str = ""
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float = 0.0
    # model id (e.g. "deepseek-v4-pro:cloud") → {tokens_in, tokens_out,
    # cost_usd, calls}
    by_model: dict[str, dict] = field(default_factory=dict)
    started_at_epoch: float = 0.0
    finalized_at_epoch: float = 0.0

    def to_payload(self) -> dict:
        """Serialize for WS emission. Sorted-by-cost models so the
        most expensive runner shows first in the UI."""
        models = []
        for model, stats in self.by_model.items():
            models.append({
                "model": model,
                "tokens_in": stats.get("tokens_in", 0),
                "tokens_out": stats.get("tokens_out", 0),
                "cost_usd": round(stats.get("cost_usd", 0.0), 6),
                "calls": stats.get("calls", 0),
            })
        models.sort(key=lambda m: -m["cost_usd"])
        return {
            "iter_count": self.iter_count,
            "intent_id": self.intent_id,
            "tokens_in": self.tokens_in,
            "tokens_out": self.tokens_out,
            "cost_usd": round(self.cost_usd, 6),
            "by_model": models,
            "duration_seconds": round(
                max(0.0, self.finalized_at_epoch - self.started_at_epoch), 3,
            ),
        }


class AutonomousIterCostTracker:
    """Per-iteration cost accumulator. Thread-safe.

    Lifecycle per iteration:
      open  : on_iteration_started(intent_id, iter_count, started_at)
      add   : record_status(intent_id, model, tokens_in, tokens_out, cost_usd)
              (called for every chat-stream status event during the iter)
      close : on_iteration_finalized(intent_id, iter_count, finalized_at)
              returns the IterCostSnapshot, evicts the bucket

    Status events that arrive between iter close and next open (e.g.
    REFLECT pass running between iter-N-complete and iter-N+1-start)
    are still attributed to whichever iter is OPEN — the daemon only
    has one open iter at a time per mission. If no iter is open they're
    dropped (REFLECT-during-no-iter case is rare; first-iter-not-yet-
    started case is invisible UX-wise).
    """

    def __init__(self):
        self._lock = threading.Lock()
        # (intent_id, iter_count) → IterCostSnapshot. Buckets evicted
        # on finalize; a misbehaving caller that never finalizes leaks
        # one bucket per dropped iter. We accept that — the leak is
        # tiny (~200 bytes per iter) and most missions have <100 iters.
        self._buckets: dict[tuple[str, int], IterCostSnapshot] = {}
        # Per intent_id, the iter_count of the currently-open bucket.
        # status events route to this iter even though they arrive
        # asynchronously. One open bucket per intent.
        self._open_iter: dict[str, int] = {}

    def on_iteration_started(
        self, intent_id: str, iter_count: int, started_at: float = 0.0,
    ) -> None:
        """Open a fresh bucket for this iter. Idempotent —
        re-opening the same (intent_id, iter) is a no-op (the
        existing bucket keeps accumulating). Calling with a NEW
        iter_count for the same intent closes the previous bucket
        implicitly (defensive — daemon should always call finalize
        first, but we don't want a dropped finalize to leak)."""
        if not intent_id:
            return
        key = (intent_id, iter_count)
        with self._lock:
            prev_open = self._open_iter.get(intent_id)
            if prev_open is not None and prev_open != iter_count:
                # Defensive: the previous iter never got finalized.
                # Leave its bucket in place (caller may finalize
                # later) but stop routing to it.
                logger.debug(
                    "iter %s for intent %s never finalized; opening "
                    "iter %s anyway", prev_open, intent_id, iter_count,
                )
            if key not in self._buckets:
                self._buckets[key] = IterCostSnapshot(
                    iter_count=iter_count,
                    intent_id=intent_id,
                    started_at_epoch=started_at,
                )
            self._open_iter[intent_id] = iter_count

    def record_status(
        self,
        intent_id: str,
        model: str,
        tokens_in: int,
        tokens_out: int,
        cost_usd: float,
    ) -> None:
        """Add token + cost numbers to the currently-open bucket
        for this intent. No-op when no bucket is open OR when the
        token counts are zero (status events for warmup / heartbeat
        also flow through this channel)."""
        if not intent_id or not model:
            return
        if tokens_in <= 0 and tokens_out <= 0 and cost_usd <= 0:
            return
        with self._lock:
            iter_count = self._open_iter.get(intent_id)
            if iter_count is None:
                # No iter open. We could route to a "pre-iter"
                # bucket, but for now just drop — the only caller in
                # this state is REFLECT-runs-on-empty-roadmap, which
                # is rare AND already accounted for via the daemon's
                # own reflecting-phase tracking.
                return
            key = (intent_id, iter_count)
            bucket = self._buckets.get(key)
            if bucket is None:
                # Defensive: open_iter says a bucket exists but it
                # doesn't. Re-create.
                bucket = IterCostSnapshot(
                    iter_count=iter_count, intent_id=intent_id,
                )
                self._buckets[key] = bucket
            bucket.tokens_in += int(max(0, tokens_in))
            bucket.tokens_out += int(max(0, tokens_out))
            bucket.cost_usd += float(max(0.0, cost_usd))
            entry = bucket.by_model.setdefault(model, {
                "tokens_in": 0, "tokens_out": 0,
                "cost_usd": 0.0, "calls": 0,
            })
            entry["tokens_in"] += int(max(0, tokens_in))
            entry["tokens_out"] += int(max(0, tokens_out))
            entry["cost_usd"] += float(max(0.0, cost_usd))
            entry["calls"] += 1

    def on_iteration_finalized(
        self, intent_id: str, iter_count: int, finalized_at: float = 0.0,
    ) -> Optional[IterCostSnapshot]:
        """Close + return the bucket. Subsequent record_status calls
        for this (intent, iter) won't accumulate (the open_iter
        pointer is cleared). Returns None if no bucket was open
        (caller's responsibility to handle the missing-data case)."""
        if not intent_id:
            return None
        key = (intent_id, iter_count)
        with self._lock:
            bucket = self._buckets.pop(key, None)
            if self._open_iter.get(intent_id) == iter_count:
                self._open_iter.pop(intent_id, None)
        if bucket is None:
            return None
        bucket.finalized_at_epoch = finalized_at
        return bucket

    def reset_intent(self, intent_id: str) -> None:
        """Drop all buckets + open-iter state for an intent. Called
        when a mission ends so subsequent missions with the same
        intent_id (rare) don't see stale data."""
        if not intent_id:
            return
        with self._lock:
            self._open_iter.pop(intent_id, None)
            keys_to_drop = [
                k for k in self._buckets if k[0] == intent_id
            ]
            for k in keys_to_drop:
                self._buckets.pop(k, None)

    def open_iter_for(self, intent_id: str) -> Optional[int]:
        """Read the currently-open iter for an intent. Used by tests."""
        with self._lock:
            return self._open_iter.get(intent_id)

    def bucket_count(self) -> int:
        """Number of live buckets. Used by tests + memory-leak watchdog."""
        with self._lock:
            return len(self._buckets)
