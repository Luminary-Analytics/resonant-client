"""
Cost tracking for Resonant Client.
Tracks token usage per session/day with model-specific pricing.
"""

import json
import logging
import threading
from datetime import date
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Pricing per million tokens (USD)
MODEL_PRICING: dict[str, dict[str, float]] = {
    # Claude models
    "claude-opus-4-20250514":       {"input": 15.0,  "output": 75.0},
    "claude-sonnet-4-20250514":     {"input": 3.0,   "output": 15.0},
    "claude-haiku-4-20250514":      {"input": 0.80,  "output": 4.0},
    "opus":                          {"input": 15.0,  "output": 75.0},
    "sonnet":                        {"input": 3.0,   "output": 15.0},
    "haiku":                         {"input": 0.80,  "output": 4.0},
    # OpenAI models
    "gpt-4o":                        {"input": 2.50,  "output": 10.0},
    "gpt-4o-mini":                   {"input": 0.15,  "output": 0.60},
    "gpt-4.1":                       {"input": 2.0,   "output": 8.0},
    "gpt-4.1-mini":                  {"input": 0.40,  "output": 1.60},
    "gpt-4.1-nano":                  {"input": 0.10,  "output": 0.40},
    "gpt-5.4":                       {"input": 5.0,   "output": 20.0},
    "o3":                            {"input": 10.0,  "output": 40.0},
    "o4-mini":                       {"input": 1.10,  "output": 4.40},
    # Local models (free)
    "local":                         {"input": 0.0,   "output": 0.0},
}


def _match_pricing(model: str) -> dict[str, float]:
    """Find best matching pricing for a model name."""
    model_lower = model.lower()
    # Exact match
    if model_lower in MODEL_PRICING:
        return MODEL_PRICING[model_lower]
    # Substring match
    for key, pricing in MODEL_PRICING.items():
        if key in model_lower or model_lower in key:
            return pricing
    # Ollama / LM Studio → free
    return MODEL_PRICING["local"]


class CostTracker:
    """Tracks token usage and costs per session and daily."""

    def __init__(self, path: str | Path | None = None):
        self._path = Path(path) if path else Path.home() / ".resonant" / "costs.json"
        self._lock = threading.Lock()
        self._daily: dict = {}  # { "2025-03-20": { "input_tokens": N, "output_tokens": N, "cost_usd": X } }
        self._session_input = 0
        self._session_output = 0
        self._session_cost = 0.0
        self._load()

    def record_usage(self, model: str, input_tokens: int, output_tokens: int) -> float:
        """Record token usage, returns cost in USD for this call."""
        pricing = _match_pricing(model)
        cost = (input_tokens * pricing["input"] + output_tokens * pricing["output"]) / 1_000_000
        today = date.today().isoformat()

        with self._lock:
            self._session_input += input_tokens
            self._session_output += output_tokens
            self._session_cost += cost

            if today not in self._daily:
                self._daily[today] = {"input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0}
            self._daily[today]["input_tokens"] += input_tokens
            self._daily[today]["output_tokens"] += output_tokens
            self._daily[today]["cost_usd"] += cost
            self._save_locked()

        return cost

    def get_session_cost(self) -> dict:
        """Return current session cost info."""
        with self._lock:
            return {
                "input_tokens": self._session_input,
                "output_tokens": self._session_output,
                "cost_usd": round(self._session_cost, 4),
            }

    def get_daily_cost(self, day: str | None = None) -> dict:
        """Return cost info for a specific day (default: today)."""
        day = day or date.today().isoformat()
        with self._lock:
            return self._daily.get(day, {"input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0})

    def _get_daily_cost_unlocked(self, day: str | None = None) -> dict:
        """Return cost info without acquiring lock (for internal use when lock is already held)."""
        day = day or date.today().isoformat()
        return self._daily.get(day, {"input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0})

    def reset_session(self) -> None:
        """Reset session counters (called on new session)."""
        with self._lock:
            self._session_input = 0
            self._session_output = 0
            self._session_cost = 0.0

    def get_all_costs(self) -> dict:
        """Return full cost data for display."""
        with self._lock:
            return {
                "session": {
                    "input_tokens": self._session_input,
                    "output_tokens": self._session_output,
                    "cost_usd": round(self._session_cost, 4),
                },
                "today": self._get_daily_cost_unlocked(),
                "daily": {k: {**v, "cost_usd": round(v["cost_usd"], 4)} for k, v in self._daily.items()},
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
