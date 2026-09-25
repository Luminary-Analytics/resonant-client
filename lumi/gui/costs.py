"""
Cost tracking for Lumi.
Tracks token usage per session/day; prices come from lumi/pricing.py.

A call without a known price adds its tokens and counts as unpriced: it is
never valued at $0. The per-call records behind these totals are
lumi/usage.py's.
"""

import json
import logging
import threading
from datetime import date
from pathlib import Path

from ..paths import state_home

logger = logging.getLogger(__name__)


def _empty() -> dict:
    return {"input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0, "unpriced_calls": 0,
            "subscription_calls": 0}


class CostTracker:
    """Tracks token usage and costs per session and daily."""

    def __init__(self, path: str | Path | None = None):
        self._path = Path(path) if path else state_home() / "costs.json"
        self._lock = threading.Lock()
        self._daily: dict = {}  # { "2025-03-20": { "input_tokens": N, "output_tokens": N, "cost_usd": X, "unpriced_calls": N } }
        self._session = _empty()
        self._load()

    def record_usage(
        self,
        model: str,
        input_tokens: int,
        output_tokens: int,
        cached_tokens: int = 0,
        actual_cost: float | None = None,
        *,
        provider: str = "",
    ) -> float | None:
        """Price and record one call; returns its cost in USD, or None when unpriced."""
        from ..pricing import cost as price_call

        priced = price_call(provider, model, input_tokens=input_tokens, output_tokens=output_tokens,
                            cached_tokens=cached_tokens, reported_cost=actual_cost)
        self.add(input_tokens, output_tokens, priced["cost_usd"], source=priced["price_source"])
        return priced["cost_usd"]

    def add(self, input_tokens: int, output_tokens: int, cost_usd: float | None, *, source: str = "") -> None:
        """Add one already-priced call (``None``: no per-call price; ``source`` says why)."""
        input_tokens, output_tokens = int(input_tokens or 0), int(output_tokens or 0)
        today = date.today().isoformat()
        with self._lock:
            day = self._daily.setdefault(today, _empty())
            for bucket in (self._session, day):
                bucket["input_tokens"] = int(bucket.get("input_tokens", 0)) + input_tokens
                bucket["output_tokens"] = int(bucket.get("output_tokens", 0)) + output_tokens
                if cost_usd is None:
                    counter = "subscription_calls" if source == "subscription" else "unpriced_calls"
                    bucket[counter] = int(bucket.get(counter, 0)) + 1
                else:
                    bucket["cost_usd"] = float(bucket.get("cost_usd", 0.0)) + float(cost_usd)
            self._save_locked()

    def get_session_cost(self) -> dict:
        """Return current session cost info."""
        with self._lock:
            return {**self._session, "cost_usd": round(self._session["cost_usd"], 4)}

    def get_daily_cost(self, day: str | None = None) -> dict:
        """Return cost info for a specific day (default: today)."""
        day = day or date.today().isoformat()
        with self._lock:
            return {**_empty(), **self._daily.get(day, {})}

    def reset_session(self) -> None:
        """Reset session counters (called on new session)."""
        with self._lock:
            self._session = _empty()

    def get_all_costs(self) -> dict:
        """Return full cost data for display."""
        with self._lock:
            daily = {
                key: {**_empty(), **value, "cost_usd": round(float(value.get("cost_usd", 0.0)), 4)}
                for key, value in self._daily.items()
            }
            total = {
                "input_tokens": sum(int(value.get("input_tokens", 0)) for value in self._daily.values()),
                "output_tokens": sum(int(value.get("output_tokens", 0)) for value in self._daily.values()),
                "cost_usd": round(sum(float(value.get("cost_usd", 0.0)) for value in self._daily.values()), 4),
                "unpriced_calls": sum(int(value.get("unpriced_calls", 0)) for value in self._daily.values()),
                "subscription_calls": sum(int(value.get("subscription_calls", 0)) for value in self._daily.values()),
            }
            return {
                "session": {**self._session, "cost_usd": round(self._session["cost_usd"], 4)},
                "today": daily.get(date.today().isoformat(), _empty()),
                "total": total,
                "daily": daily,
            }

    def _load(self) -> None:
        with self._lock:
            if self._path.exists():
                try:
                    self._daily = json.loads(self._path.read_text(encoding="utf-8"))
                except (json.JSONDecodeError, OSError):
                    self._daily = {}

    def _save_locked(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(
                json.dumps(self._daily, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
        except OSError as e:
            logger.error(f"Failed to save costs: {e}")
