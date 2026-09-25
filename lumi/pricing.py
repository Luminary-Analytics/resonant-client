"""What a model call costs: a dated price list, organization and user
overrides, and the cost a provider reports.

Prices are USD per million tokens. A call's cost is resolved in this order:

1. what the provider reported for the call (OpenRouter);
2. an organization policy's ``pricing.prices`` (lumi/policy.py);
3. the user's ``cost_tracking.price_overrides``;
4. local models (Ollama, EXO, LM Studio): free;
5. subscriptions (Codex, Claude Code, Ollama cloud models): not priced per
   call, recorded as ``subscription``;
6. the bundled list below;
7. otherwise **unpriced**: the cost is ``None``, never $0.

Override and policy patterns are ``fnmatch`` globs over ``provider:model`` or
over the model alone (any provider), compared in lower case.
"""

from __future__ import annotations

import math
import threading
from dataclasses import dataclass
from fnmatch import fnmatchcase
from typing import Any

# The bundled list was checked against the providers' published price pages
# on this date: platform.claude.com/docs/en/about-claude/pricing and
# developers.openai.com/api/docs/pricing. Standard tier, global routing, no
# batch or fast-mode pricing.
CATALOG_AS_OF = "2026-09-25"


@dataclass(frozen=True)
class Price:
    """USD per million tokens. Cache prices default to the input price."""

    input: float
    output: float
    cached_input: float | None = None
    cache_write: float | None = None

    def to_dict(self) -> dict:
        data = {"input": self.input, "output": self.output}
        if self.cached_input is not None:
            data["cached_input"] = self.cached_input
        if self.cache_write is not None:
            data["cache_write"] = self.cache_write
        return data


def _claude(input_: float, output: float, cached: float, write: float) -> Price:
    return Price(input_, output, cached, write)


def _openai(input_: float, cached: float | None, output: float) -> Price:
    return Price(input_, output, cached)


# First match wins, so longer names come before their prefixes.
CATALOG: tuple[tuple[str, Price], ...] = (
    # Anthropic
    ("claude-fable-5-1*", _claude(10, 50, 0.25, 12.5)),
    ("claude-mythos-5-1*", _claude(10, 50, 0.25, 12.5)),
    ("claude-fable-5*", _claude(10, 50, 1, 12.5)),
    ("claude-mythos-5*", _claude(10, 50, 1, 12.5)),
    ("claude-opus-5-5*", _claude(4, 20, 0.20, 5)),
    ("claude-opus-5*", _claude(5, 25, 0.50, 6.25)),
    ("claude-opus-4-8*", _claude(5, 25, 0.50, 6.25)),
    ("claude-opus-4-7*", _claude(5, 25, 0.50, 6.25)),
    ("claude-opus-4-6*", _claude(5, 25, 0.50, 6.25)),
    ("claude-opus-4-5*", _claude(5, 25, 0.50, 6.25)),
    ("claude-opus-4-1*", _claude(15, 75, 1.50, 18.75)),
    ("claude-opus-4-0*", _claude(15, 75, 1.50, 18.75)),
    ("claude-opus-4-2025*", _claude(15, 75, 1.50, 18.75)),
    ("claude-sonnet-5*", _claude(2, 10, 0.20, 2.5)),
    ("claude-sonnet-4-6*", _claude(3, 15, 0.30, 3.75)),
    ("claude-sonnet-4-5*", _claude(3, 15, 0.30, 3.75)),
    ("claude-sonnet-4-0*", _claude(3, 15, 0.30, 3.75)),
    ("claude-sonnet-4-2025*", _claude(3, 15, 0.30, 3.75)),
    ("claude-haiku-4-5*", _claude(1, 5, 0.10, 1.25)),
    ("claude-3-5-haiku*", _claude(0.80, 4, 0.08, 1)),
    # OpenAI
    ("gpt-6-astra*", _openai(10, 1, 50)),
    ("gpt-6-sol*", _openai(2, 0.20, 10)),
    ("gpt-6-luna*", _openai(0.10, 0.01, 0.50)),
    ("gpt-5.6-sol*", _openai(4, 0.40, 20)),
    ("gpt-5.6-terra*", _openai(2, 0.20, 12)),
    ("gpt-5.6-luna*", _openai(0.20, 0.02, 1.20)),
    ("gpt-5.5-pro*", _openai(30, None, 180)),
    ("gpt-5.5", _openai(5, 0.50, 30)),
    ("gpt-5.5-20*", _openai(5, 0.50, 30)),
    ("gpt-5.4-pro*", _openai(30, None, 180)),
    ("gpt-5.4-mini*", _openai(0.75, 0.075, 4.50)),
    ("gpt-5.4-nano*", _openai(0.20, 0.02, 1.25)),
    ("gpt-5.4", _openai(2.50, 0.25, 15)),
    ("gpt-5.4-20*", _openai(2.50, 0.25, 15)),
    ("gpt-5.2", _openai(1.75, 0.175, 14)),
    ("gpt-5.2-20*", _openai(1.75, 0.175, 14)),
    ("gpt-5.1", _openai(1.25, 0.125, 10)),
    ("gpt-5.1-20*", _openai(1.25, 0.125, 10)),
    ("gpt-5-mini*", _openai(0.25, 0.025, 2)),
    ("gpt-5-nano*", _openai(0.05, 0.005, 0.40)),
    ("gpt-5", _openai(1.25, 0.125, 10)),
    ("gpt-5-20*", _openai(1.25, 0.125, 10)),
    ("gpt-4.1-mini*", _openai(0.40, 0.10, 1.60)),
    ("gpt-4.1-nano*", _openai(0.10, 0.025, 0.40)),
    ("gpt-4.1", _openai(2, 0.50, 8)),
    ("gpt-4.1-20*", _openai(2, 0.50, 8)),
    ("gpt-4o-mini*", _openai(0.15, 0.075, 0.60)),
    ("gpt-4o", _openai(2.50, 1.25, 10)),
    ("gpt-4o-20*", _openai(2.50, 1.25, 10)),
    ("o3-mini*", _openai(1.10, 0.55, 4.40)),
    ("o3", _openai(2, 0.50, 8)),
    ("o3-20*", _openai(2, 0.50, 8)),
    ("o4-mini*", _openai(1.10, 0.275, 4.40)),
    # Moonshot, carried over from Lumi's earlier price table (not rechecked).
    ("kimi-k3*", Price(3, 15, 0.30)),
)

LOCAL_PROVIDERS = frozenset({"ollama", "exo", "lmstudio"})
SUBSCRIPTION_PROVIDERS = frozenset({"codex", "claude-code"})

_lock = threading.Lock()
_user_overrides: tuple[tuple[str, Price], ...] = ()


def _parse_price(value: Any) -> Price | None:
    if not isinstance(value, dict):
        return None
    try:
        numbers = {key: float(value[key]) for key in ("input", "output")}
        for key in ("cached_input", "cache_write"):
            if value.get(key) is not None:
                numbers[key] = float(value[key])
    except (KeyError, TypeError, ValueError):
        return None
    if not all(math.isfinite(n) and n >= 0 for n in numbers.values()):
        return None
    return Price(numbers["input"], numbers["output"], numbers.get("cached_input"), numbers.get("cache_write"))


def parse_prices(data: Any) -> tuple[tuple[str, Price], ...]:
    """``{"pattern": {"input": .., "output": ..}}`` as ordered (pattern, Price) pairs.

    Invalid entries raise ``ValueError`` naming the pattern.
    """
    if not isinstance(data, dict):
        raise ValueError("Prices must map a model pattern to its prices.")
    parsed = []
    for pattern, value in data.items():
        price = _parse_price(value)
        if not isinstance(pattern, str) or not pattern.strip() or price is None:
            raise ValueError(f"The price for {pattern!r} needs non-negative input and output numbers.")
        parsed.append((pattern.strip().lower(), price))
    return tuple(parsed)


def configure(settings: Any) -> None:
    """Load the user's price overrides (``cost_tracking.price_overrides``)."""
    global _user_overrides
    raw = settings.get("cost_tracking", "price_overrides", {}) if settings is not None else {}
    try:
        overrides = parse_prices(raw or {})
    except ValueError:
        overrides = ()
    with _lock:
        _user_overrides = overrides


def reset() -> None:
    """No user overrides (tests)."""
    global _user_overrides
    with _lock:
        _user_overrides = ()


def _matches(pattern: str, provider: str, model: str) -> bool:
    if ":" in pattern and not pattern.startswith(("*", "?")):
        head, _, tail = pattern.partition(":")
        if fnmatchcase(provider, head) and fnmatchcase(model, tail):
            return True
    return fnmatchcase(f"{provider}:{model}", pattern) or fnmatchcase(model, pattern)


def _first(prices: tuple[tuple[str, Price], ...], provider: str, model: str) -> tuple[str, Price] | None:
    for pattern, price in prices:
        if _matches(pattern, provider, model):
            return pattern, price
    return None


def _catalog_model(model: str) -> str:
    # OpenRouter and similar gateways name models "vendor/model".
    return model.rsplit("/", 1)[-1]


def resolve(provider: str, model: str) -> tuple[Price | None, str, str]:
    """The price for one provider's model: (price or None, source, matching pattern)."""
    provider = str(provider or "").strip().lower()
    model = str(model or "").strip().lower()
    from .policy import current as current_policy

    policy = current_policy()
    if policy is not None and policy.prices:
        found = _first(policy.prices, provider, model)
        if found:
            return found[1], "organization", found[0]
    with _lock:
        overrides = _user_overrides
    found = _first(overrides, provider, model)
    if found:
        return found[1], "override", found[0]
    if provider in LOCAL_PROVIDERS:
        if model.endswith((":cloud", "-cloud")):
            return None, "subscription", ""
        return Price(0, 0), "local", ""
    if provider in SUBSCRIPTION_PROVIDERS:
        return None, "subscription", ""
    found = _first(CATALOG, provider, _catalog_model(model))
    if found:
        return found[1], "catalog", found[0]
    return None, "unpriced", ""


def _count(value: Any) -> int:
    try:
        number = int(value or 0)
    except (TypeError, ValueError):
        return 0
    return max(0, number)


def cost(provider: str, model: str, *, input_tokens: int = 0, output_tokens: int = 0,
         cached_tokens: int = 0, cache_write_tokens: int = 0, reported_cost: Any = None) -> dict:
    """What one call cost.

    ``input_tokens`` counts every prompt token, cached and cache writes
    included. Returns ``cost_usd`` (None when unpriced), the ``computed`` and
    ``reported`` costs separately, and where the price came from.
    """
    price, source, pattern = resolve(provider, model)
    total_in = _count(input_tokens)
    cached = min(_count(cached_tokens), total_in)
    writes = min(_count(cache_write_tokens), total_in - cached)
    computed = None
    if price is not None:
        cached_rate = price.input if price.cached_input is None else price.cached_input
        write_rate = price.input if price.cache_write is None else price.cache_write
        computed = (
            (total_in - cached - writes) * price.input
            + cached * cached_rate
            + writes * write_rate
            + _count(output_tokens) * price.output
        ) / 1_000_000
    reported = None
    if isinstance(reported_cost, (int, float)) and not isinstance(reported_cost, bool):
        if math.isfinite(float(reported_cost)) and reported_cost >= 0:
            reported = float(reported_cost)
    return {
        "cost_usd": reported if reported is not None else computed,
        "computed_cost_usd": computed,
        "reported_cost_usd": reported,
        "price_source": "reported" if reported is not None else source,
        "price_pattern": pattern,
    }


def describe_catalog() -> dict:
    """The bundled list and the overrides in effect, for Settings."""
    from .policy import current as current_policy

    policy = current_policy()
    with _lock:
        overrides = _user_overrides
    return {
        "as_of": CATALOG_AS_OF,
        "catalog": [{"pattern": pattern, **price.to_dict()} for pattern, price in CATALOG],
        "overrides": [{"pattern": pattern, **price.to_dict()} for pattern, price in overrides],
        "organization": [{"pattern": pattern, **price.to_dict()} for pattern, price in (policy.prices if policy else ())],
    }


def parse_override_lines(value: Any) -> dict:
    """Settings' price lines as a ``cost_tracking.price_overrides`` dict.

    Each line is ``pattern input output [cached_input] [cache_write]`` in USD
    per million tokens; ``#`` starts a comment. A dict is validated as is.
    """
    if isinstance(value, dict):
        return {pattern: price.to_dict() for pattern, price in parse_prices(value)}
    lines = value.splitlines() if isinstance(value, str) else list(value or [])
    prices: dict[str, dict] = {}
    for number, line in enumerate(lines, start=1):
        text = str(line).split("#", 1)[0].strip()
        if not text:
            continue
        pattern, *numbers = text.split()
        if not 2 <= len(numbers) <= 4:
            raise ValueError(f"Line {number}: write a model pattern, then input and output prices "
                             "(and optionally cached input and cache writes).")
        try:
            values = [float(item) for item in numbers]
        except ValueError:
            raise ValueError(f"Line {number}: prices must be numbers, in USD per million tokens.") from None
        if not all(math.isfinite(item) and item >= 0 for item in values):
            raise ValueError(f"Line {number}: prices can't be negative.")
        keys = ("input", "output", "cached_input", "cache_write")
        prices[pattern.lower()] = dict(zip(keys, values))
    if len(prices) > 200:
        raise ValueError("Set at most 200 prices.")
    return prices
