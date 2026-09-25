"""Budgets: warn, then ask, then stop, as priced spend grows.

A budget is a rule over the usage records (lumi/usage.py)::

    {"scope": "user", "period": "month", "warn_usd": 200, "approve_usd": 300, "block_usd": 400}
    {"scope": "project", "match": "*/payments*", "period": "day", "block_usd": 50}
    {"scope": "turn", "block_usd": 5, "block_unpriced": true}

* ``user`` budgets count everything this machine's account spent in the period;
* ``project`` budgets count the projects whose folder matches ``match``;
* ``turn`` budgets count one turn.

Periods are UTC days or months. Spend is the priced cost: an unpriced call
can't count, so ``block_unpriced`` refuses unpriced models while that budget
applies. Past ``warn_usd`` the turn shows a warning once per period; past
``approve_usd`` it asks the person to continue (once per period, or once per
turn for turn budgets) and stops without an answer; at ``block_usd`` it stops.

Rules come from an organization policy's ``budgets`` (lumi/policy.py) and
from Settings: ``cost_tracking.budget_alert_usd`` warns daily,
``cost_tracking.daily_limit_usd`` asks before going past it, and
``cost_tracking.turn_limit_usd`` stops a turn. Budgets across a team's
machines need Lumi Cloud; these are enforced on this machine.
"""

from __future__ import annotations

import math
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from fnmatch import fnmatchcase
from typing import Any, Iterable

SCOPES = ("user", "project", "turn")
PERIODS = ("day", "month")
LEVELS = ("ok", "warn", "approve", "block")


@dataclass(frozen=True)
class Rule:
    scope: str
    period: str = "day"
    match: str = "*"
    warn_usd: float | None = None
    approve_usd: float | None = None
    block_usd: float | None = None
    block_unpriced: bool = False
    owner: str = ""  # the organization; empty for the person's own settings

    @property
    def id(self) -> str:
        return "|".join(str(part) for part in (self.owner, self.scope, self.period, self.match,
                                               self.warn_usd, self.approve_usd, self.block_usd))

    def applies_to(self, project: str) -> bool:
        if self.scope != "project":
            return True
        path = str(project or "").replace("\\", "/")
        return bool(path) and fnmatchcase(path.lower(), self.match.replace("\\", "/").lower())

    def to_dict(self) -> dict:
        return {"scope": self.scope, "period": self.period, "match": self.match, "warn_usd": self.warn_usd,
                "approve_usd": self.approve_usd, "block_usd": self.block_usd,
                "block_unpriced": self.block_unpriced, "owner": self.owner}


def _amount(value: Any, where: str) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise ValueError(f"{where} must be an amount in USD.")
    try:
        amount = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{where} must be an amount in USD.") from None
    if not math.isfinite(amount) or amount < 0:
        raise ValueError(f"{where} can't be negative.")
    return amount


def parse_rules(data: Any, *, owner: str = "") -> tuple[Rule, ...]:
    """Validate a list of budget rules; raises ``ValueError`` saying what to fix."""
    if data is None:
        return ()
    if not isinstance(data, list):
        raise ValueError("Budgets must be a list of rules.")
    rules = []
    for index, raw in enumerate(data, start=1):
        where = f"Budget {index}"
        if not isinstance(raw, dict):
            raise ValueError(f"{where} must be an object.")
        scope = str(raw.get("scope") or "")
        if scope not in SCOPES:
            raise ValueError(f"{where}: scope must be one of {', '.join(SCOPES)}.")
        period = str(raw.get("period") or "day")
        if period not in PERIODS:
            raise ValueError(f"{where}: period must be day or month.")
        rule = Rule(
            scope=scope,
            period=period,
            match=str(raw.get("match") or "*"),
            warn_usd=_amount(raw.get("warn_usd"), f"{where}: warn_usd"),
            approve_usd=_amount(raw.get("approve_usd"), f"{where}: approve_usd"),
            block_usd=_amount(raw.get("block_usd"), f"{where}: block_usd"),
            block_unpriced=raw.get("block_unpriced") is True,
            owner=owner,
        )
        if rule.warn_usd is None and rule.approve_usd is None and rule.block_usd is None and not rule.block_unpriced:
            raise ValueError(f"{where} needs warn_usd, approve_usd, block_usd or block_unpriced.")
        rules.append(rule)
    return tuple(rules)


# ── Rules in effect ─────────────────────────────────────────────────────────

_lock = threading.Lock()
_settings_rules: tuple[Rule, ...] = ()
# Warnings shown and approvals given, per rule and period (or turn).
_warned: set[tuple[str, str]] = set()
_approved: set[tuple[str, str]] = set()


def configure(settings: Any) -> None:
    """Turn the person's cost settings into rules."""
    global _settings_rules

    def amount(key: str) -> float | None:
        try:
            value = _amount(settings.get("cost_tracking", key, None), key) if settings is not None else None
        except ValueError:
            return None
        return value if value else None

    rules = []
    if amount("budget_alert_usd") is not None:
        rules.append(Rule(scope="user", period="day", warn_usd=amount("budget_alert_usd")))
    if amount("daily_limit_usd") is not None:
        rules.append(Rule(scope="user", period="day", approve_usd=amount("daily_limit_usd")))
    if amount("turn_limit_usd") is not None:
        rules.append(Rule(scope="turn", block_usd=amount("turn_limit_usd")))
    with _lock:
        _settings_rules = tuple(rules)


def reset() -> None:
    """No rules, warnings or approvals (tests)."""
    global _settings_rules
    with _lock:
        _settings_rules = ()
        _warned.clear()
        _approved.clear()


def rules() -> tuple[Rule, ...]:
    """The organization's rules first, then the person's."""
    from .policy import current as current_policy

    policy = current_policy()
    with _lock:
        own = _settings_rules
    return tuple(policy.budgets if policy else ()) + own


# ── Spend ───────────────────────────────────────────────────────────────────


def period_key(period: str, now: datetime | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    return now.strftime("%Y-%m") if period == "month" else now.strftime("%Y-%m-%d")


def _spent(rule: Rule, project: str, turn_spend: float, rows: list[dict], user: str) -> float:
    if rule.scope == "turn":
        return turn_spend
    key = period_key(rule.period)
    total = 0.0
    for row in rows:
        if not str(row.get("ts", "")).startswith(key):
            continue
        if rule.scope == "user" and row.get("user") != user:
            continue
        if rule.scope == "project" and not rule.applies_to(str(row.get("project") or "")):
            continue
        cost = row.get("cost_usd")
        if isinstance(cost, (int, float)) and not isinstance(cost, bool):
            total += float(cost)
    return total


@dataclass(frozen=True)
class Verdict:
    level: str
    rule: Rule
    spent: float
    threshold: float
    message: str

    def key(self, turn: str) -> tuple[str, str]:
        return (self.rule.id, turn if self.rule.scope == "turn" else period_key(self.rule.period))


def _money(value: float) -> str:
    return f"${value:,.4f}" if 0 < value < 0.01 else f"${value:,.2f}"


def _whose(rule: Rule) -> str:
    return f"{rule.owner}'s" if rule.owner else "your"


def _what(rule: Rule, project: str) -> str:
    if rule.scope == "turn":
        return "This turn has spent"
    period = "Today's" if rule.period == "day" else "This month's"
    if rule.scope == "project":
        return f"{period} spend in this project is"
    return f"{period} spend is"


def evaluate(project: str = "", *, turn_spend: float = 0.0, rows: Iterable[dict] | None = None,
             user: str | None = None, applicable: Iterable[Rule] | None = None) -> list[Verdict]:
    """The most severe level each applicable rule has reached (rules at ``ok`` are left out)."""
    from .usage import current_user, ledger

    rule_list = [rule for rule in (rules() if applicable is None else applicable) if rule.applies_to(project)]
    if not rule_list:
        return []
    # Day and month periods both fall within this month's records.
    rows = list(ledger().records(since=period_key("month")) if rows is None else rows)
    user = current_user() if user is None else user
    verdicts = []
    for rule in rule_list:
        spent = _spent(rule, project, turn_spend, rows, user)
        what = _what(rule, project)
        if rule.block_usd is not None and spent >= rule.block_usd:
            verdicts.append(Verdict("block", rule, spent, rule.block_usd,
                                    f"{what} {_money(spent)}, which reaches {_whose(rule)} {_money(rule.block_usd)} budget."))
        elif rule.approve_usd is not None and spent >= rule.approve_usd:
            verdicts.append(Verdict("approve", rule, spent, rule.approve_usd,
                                    f"{what} {_money(spent)}, past {_whose(rule)} {_money(rule.approve_usd)} limit."))
        elif rule.warn_usd is not None and spent >= rule.warn_usd:
            verdicts.append(Verdict("warn", rule, spent, rule.warn_usd,
                                    f"{what} {_money(spent)}, past {_whose(rule)} {_money(rule.warn_usd)} alert."))
    return verdicts


def unpriced_refusal(project: str, provider: str, model: str) -> str:
    """Why an unpriced model can't run under a budget that requires prices, or ''."""
    from .pricing import resolve

    price, source, _ = resolve(provider, model)
    if price is not None or source == "subscription":
        return ""
    for rule in rules():
        if rule.block_unpriced and rule.applies_to(project):
            who = rule.owner or "Your"
            return (f"{who} budget needs a price for {model or 'this model'} on {provider or 'this provider'}, "
                    "and Lumi doesn't have one. Choose a priced model, or add its price under Usage & cost.")
    return ""


def warned(verdict: Verdict, turn: str) -> bool:
    """True the first time a warning is seen in its period; later calls return False."""
    key = verdict.key(turn)
    with _lock:
        if key in _warned:
            return False
        _warned.add(key)
        return True


def approved(verdict: Verdict, turn: str) -> bool:
    with _lock:
        return verdict.key(turn) in _approved


def approve(verdict: Verdict, turn: str) -> None:
    with _lock:
        _approved.add(verdict.key(turn))


def status(project: str = "") -> list[dict]:
    """Every rule with this period's spend, for Settings."""
    from .usage import current_user, ledger

    rule_list = list(rules())
    if not rule_list:
        return []
    rows = ledger().records(since=period_key("month"))
    user = current_user()
    result = []
    for rule in rule_list:
        spent = 0.0 if rule.scope == "turn" else _spent(rule, project, 0.0, rows, user)
        result.append({**rule.to_dict(), "spent_usd": round(spent, 6),
                       "applies": rule.applies_to(project), "period_key": period_key(rule.period)})
    return result
