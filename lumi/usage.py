"""Usage records: one line per model call, with its tokens, cost and context.

Records are JSON lines in ``~/.lumi/usage/YYYY-MM.jsonl`` (UTC months)::

    {"v": 1, "id": "…", "ts": "2026-09-25T04:00:00.123Z", "user": "alice",
     "project": "C:/src/app", "session": "<conversation id>", "agent": "",
     "purpose": "turn", "provider": "anthropic", "model": "claude-sonnet-5",
     "input_tokens": 12000, "cached_tokens": 9000, "cache_write_tokens": 0,
     "output_tokens": 800, "reasoning_tokens": 0,
     "cost_usd": 0.0122, "computed_cost_usd": 0.0122, "reported_cost_usd": null,
     "price_source": "catalog", "elapsed": 3.2}

``purpose`` says why the call was made: ``turn`` (the agent loop),
``subagent`` (a delegated worker), ``title`` or ``compression``.
``input_tokens`` counts every prompt token, cached ones included. ``cost_usd``
is ``None`` for unpriced models (lumi/pricing.py), never a guessed $0.

``cost_tracking.enabled`` turns recording off. The GUI, terminal UI and
gateway share the files through an OS lock (lumi/file_lock.py).
"""

from __future__ import annotations

import getpass
import json
import logging
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

from .file_lock import exclusive

logger = logging.getLogger(__name__)


def _first(stats: dict, *keys: str) -> int:
    for key in keys:
        try:
            value = int(stats.get(key) or 0)
        except (TypeError, ValueError):
            continue
        if value > 0:
            return value
    return 0


def token_counts(stats: dict) -> dict:
    """Token counts from one call's stats, whichever names the provider uses.

    ``input_tokens`` includes cached and cache-write tokens. Anthropic's raw
    usage (from Claude Code) counts them separately, so they are added back.
    """
    cached = _first(stats, "cached_tokens", "cache_read_tokens", "cache_read_input_tokens", "cached_input_tokens")
    writes = _first(stats, "cache_write_tokens", "cache_creation_input_tokens")
    input_tokens = _first(stats, "input_tokens", "prompt_eval_count", "tokens_in", "prompt_tokens")
    if "cache_read_input_tokens" in stats or "cache_creation_input_tokens" in stats:
        input_tokens += cached + writes
    return {
        "input_tokens": input_tokens,
        "cached_tokens": min(cached, input_tokens),
        "cache_write_tokens": min(writes, max(0, input_tokens - cached)),
        "output_tokens": _first(stats, "output_tokens", "eval_count", "tokens_out", "completion_tokens"),
        "reasoning_tokens": _first(stats, "reasoning_tokens"),
    }


def current_user() -> str:
    try:
        return getpass.getuser()
    except Exception:
        return ""


class UsageLedger:
    """Append-only usage records under one folder, with totals for budgets."""

    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.enabled = True
        self._lock = threading.Lock()
        # Parsed records per month file, with the byte offset read so far.
        self._cache: dict[str, tuple[int, list[dict]]] = {}

    def configure(self, settings: Any) -> None:
        self.enabled = settings is None or settings.get("cost_tracking", "enabled", True) is not False

    def record(self, *, provider: str, model: str, stats: dict, purpose: str,
               session: str = "", project: str = "", agent: str = "", elapsed: float = 0.0,
               priced: bool = True) -> dict | None:
        """Price and append one call's usage; returns the record (None when off).

        ``priced=False`` records a call billed some other way than tokens
        (dictation is billed per minute) as unpriced, never at token rates.
        """
        if not self.enabled or not isinstance(stats, dict):
            return None
        from .pricing import cost

        counts = token_counts(stats)
        if priced:
            price = cost(provider, model, input_tokens=counts["input_tokens"], output_tokens=counts["output_tokens"],
                         cached_tokens=counts["cached_tokens"], cache_write_tokens=counts["cache_write_tokens"],
                         reported_cost=stats.get("cost_usd"))
        else:
            price = {"cost_usd": None, "computed_cost_usd": None, "reported_cost_usd": None,
                     "price_source": "unpriced"}
        ts = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        try:
            elapsed = round(max(0.0, float(elapsed or 0)), 3)
        except (TypeError, ValueError):
            elapsed = 0.0
        record = {
            "v": 1,
            "id": uuid.uuid4().hex,
            "ts": ts,
            "user": current_user(),
            "project": str(project or ""),
            "session": str(session or ""),
            "agent": str(agent or ""),
            "purpose": str(purpose or "turn"),
            "provider": str(provider or ""),
            "model": str(model or ""),
            **counts,
            "cost_usd": price["cost_usd"],
            "computed_cost_usd": price["computed_cost_usd"],
            "reported_cost_usd": price["reported_cost_usd"],
            "price_source": price["price_source"],
            "elapsed": elapsed,
        }
        with self._lock, exclusive(self.root / ".lock"):
            try:
                self.root.mkdir(parents=True, exist_ok=True)
                with open(self.root / f"{ts[:7]}.jsonl", "a", encoding="utf-8", newline="\n") as handle:
                    handle.write(json.dumps(record, sort_keys=True) + "\n")
            except OSError:
                logger.warning("Could not write a usage record", exc_info=True)
                return None
        return record

    # ── Reading ────────────────────────────────────────────────────────────
    def _month(self, month: str) -> list[dict]:
        path = self.root / f"{month}.jsonl"
        with self._lock:
            offset, rows = self._cache.get(month, (0, []))
            try:
                size = path.stat().st_size
            except OSError:
                self._cache.pop(month, None)
                return []
            if size < offset:  # replaced or truncated: read it again
                offset, rows = 0, []
            if size > offset:
                with open(path, "rb") as handle:
                    handle.seek(offset)
                    chunk = handle.read()
                # Only whole lines; a line being written is read next time.
                end = chunk.rfind(b"\n") + 1
                # A new list, so callers holding the previous one never see it change.
                rows = list(rows)
                for line in chunk[:end].decode("utf-8", errors="replace").splitlines():
                    try:
                        row = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(row, dict):
                        rows.append(row)
                offset += end
            self._cache[month] = (offset, rows)
            return rows

    def months(self) -> list[str]:
        return sorted(p.stem for p in self.root.glob("????-??.jsonl")) if self.root.is_dir() else []

    def records(self, *, since: str = "", until: str = "") -> list[dict]:
        """Records with ``since <= ts < until`` (ISO dates or times; empty means open)."""
        months = [m for m in self.months() if (not since or m >= since[:7]) and (not until or m <= until[:7])]
        rows: list[dict] = []
        for month in months:
            rows.extend(r for r in self._month(month)
                        if (not since or str(r.get("ts", "")) >= since) and (not until or str(r.get("ts", "")) < until))
        return rows


def totals(rows: Iterable[dict]) -> dict:
    """Calls, tokens and cost for some records; unpriced calls are counted, not valued."""
    result = {"calls": 0, "input_tokens": 0, "cached_tokens": 0, "output_tokens": 0,
              "cost_usd": 0.0, "unpriced_calls": 0, "subscription_calls": 0}
    for row in rows:
        result["calls"] += 1
        for key in ("input_tokens", "cached_tokens", "output_tokens"):
            result[key] += int(row.get(key) or 0)
        if isinstance(row.get("cost_usd"), (int, float)):
            result["cost_usd"] += float(row["cost_usd"])
        elif row.get("price_source") == "subscription":
            result["subscription_calls"] += 1
        else:
            result["unpriced_calls"] += 1
    result["cost_usd"] = round(result["cost_usd"], 6)
    return result


def breakdown(rows: Iterable[dict], key: str) -> dict[str, dict]:
    """``totals`` grouped by one field (model, purpose, project …)."""
    groups: dict[str, list[dict]] = {}
    for row in rows:
        groups.setdefault(str(row.get(key) or ""), []).append(row)
    return {name: totals(items) for name, items in sorted(groups.items())}


# ── The process-wide ledger ─────────────────────────────────────────────────

_ledger: UsageLedger | None = None
_ledger_lock = threading.Lock()
_listener: Callable[[dict], None] | None = None


def ledger() -> UsageLedger:
    global _ledger
    with _ledger_lock:
        if _ledger is None:
            from .paths import state_home

            _ledger = UsageLedger(state_home() / "usage")
        return _ledger


def set_for_tests(value: UsageLedger | None) -> None:
    global _ledger
    with _ledger_lock:
        _ledger = value


def configure(settings: Any) -> None:
    ledger().configure(settings)


def set_listener(listener: Callable[[dict], None] | None) -> None:
    """Call ``listener(record)`` after each recorded call (the GUI's running totals)."""
    global _listener
    _listener = listener


def record(**fields: Any) -> dict | None:
    """Record one call; never raises."""
    try:
        result = ledger().record(**fields)
    except Exception:
        logger.debug("Usage record failed", exc_info=True)
        return None
    listener = _listener
    if result is not None and listener is not None:
        try:
            listener(result)
        except Exception:
            logger.debug("The usage listener failed", exc_info=True)
    return result


# ── `lumi usage` ────────────────────────────────────────────────────────────

CSV_FIELDS = (
    "ts", "user", "project", "session", "agent", "purpose", "provider", "model",
    "input_tokens", "cached_tokens", "cache_write_tokens", "output_tokens", "reasoning_tokens",
    "cost_usd", "computed_cost_usd", "reported_cost_usd", "price_source", "elapsed", "id",
)


def _money(value: float) -> str:
    return f"${value:,.4f}" if 0 < value < 0.01 else f"${value:,.2f}"


def main(argv: list[str] | None = None) -> int:
    """Print or export the usage records: ``lumi usage [--since] [--until] [--format] [--by]``."""
    import argparse
    import csv
    import sys

    parser = argparse.ArgumentParser(prog="lumi usage", description="Summarize or export Lumi's usage records.")
    parser.add_argument("--since", default="", help="first day, YYYY-MM-DD (default: the first of this month, UTC)")
    parser.add_argument("--until", default="", help="the day after the last, YYYY-MM-DD (default: now)")
    parser.add_argument("--format", choices=("summary", "csv", "jsonl"), default="summary")
    parser.add_argument("--by", choices=("model", "provider", "project", "purpose", "user", "session"),
                        default="model", help="how the summary groups calls")
    args = parser.parse_args(argv)
    since = args.since or datetime.now(timezone.utc).strftime("%Y-%m-01")
    rows = ledger().records(since=since, until=args.until)
    out = sys.stdout
    if args.format == "jsonl":
        for row in rows:
            out.write(json.dumps(row, sort_keys=True) + "\n")
        return 0
    if args.format == "csv":
        writer = csv.DictWriter(out, fieldnames=CSV_FIELDS, extrasaction="ignore", lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
        return 0
    total = totals(rows)
    extra = [f"{total['unpriced_calls']} unpriced"] if total["unpriced_calls"] else []
    extra += [f"{total['subscription_calls']} on a subscription"] if total["subscription_calls"] else []
    out.write(f"Usage since {since}{' until ' + args.until if args.until else ''}: {total['calls']} calls, "
              f"{total['input_tokens']:,} tokens in, {total['output_tokens']:,} out, {_money(total['cost_usd'])}"
              f"{' (' + ', '.join(extra) + ')' if extra else ''}\n")
    groups = breakdown(rows, args.by)
    if groups:
        width = max(len(args.by), *(len(name or "-") for name in groups))
        out.write(f"\n{args.by:<{width}}  {'calls':>7}  {'input':>12}  {'output':>10}  cost\n")
        for name, item in sorted(groups.items(), key=lambda pair: (-pair[1]["cost_usd"], -pair[1]["calls"])):
            priced = item["calls"] - item["unpriced_calls"] - item["subscription_calls"]
            cost = _money(item["cost_usd"]) if priced else ("subscription" if item["subscription_calls"] else "unpriced")
            out.write(f"{name or '-':<{width}}  {item['calls']:>7}  {item['input_tokens']:>12,}  "
                      f"{item['output_tokens']:>10,}  {cost}\n")
    return 0
