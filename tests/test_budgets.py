"""Budgets (lumi/budgets.py): alerts, approval and stops as priced spend grows,
from Settings and from an organization policy."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from lumi import audit, budgets, usage
from lumi import policy as lumi_policy
from lumi.audit import AuditLog
from lumi.budgets import Rule, parse_rules
from lumi.policy import PolicyError, parse
from lumi.usage import UsageLedger, current_user
from tests.streaming_stub import StreamingBackend, done, text_delta, tool_call

MODEL = "claude-haiku-4-5"  # $1 per million input tokens


class _Settings:
    def __init__(self, data=None):
        self.data = data or {}

    def get(self, section, key=None, default=None):
        values = self.data.get(section, {})
        return values if key is None else values.get(key, default)


@pytest.fixture
def ledger(tmp_path):
    instance = UsageLedger(tmp_path / "usage")
    usage.set_for_tests(instance)
    log = AuditLog(tmp_path / "audit")
    audit.set_for_tests(log)
    instance.audit = log
    return instance


def spend(ledger, dollars, *, project=""):
    ledger.record(provider="anthropic", model=MODEL, purpose="turn", project=project,
                  stats={"input_tokens": int(dollars * 1_000_000)})


def policy(*rules, organization="Acme"):
    lumi_policy.set_for_tests(parse({"schema": "lumi.policy/v1", "organization": organization,
                                     "budgets": list(rules)}, source="test"))


def session(tmp_path, *scripts, model=MODEL, name="anthropic"):
    from lumi.engine.session import Session

    backend = StreamingBackend(name=name, model=model, scripts=list(scripts))
    instance = Session(backend, auto_approve=True)
    instance.project_path = str(tmp_path)
    return instance, backend


def costly_call(dollars=1.0, *, call=None):
    events = [call] if call else [text_delta("Done.")]
    return events + [done(model=MODEL, stats={"input_tokens": int(dollars * 1_000_000)})]


def audit_types(ledger):
    return [json.loads(line)["type"] for path in ledger.audit._files()
            for line in path.read_text(encoding="utf-8").splitlines()]


class TestRules:
    def test_parsing(self):
        [rule] = parse_rules([{"scope": "project", "match": "*/pay*", "period": "month", "warn_usd": 5,
                               "block_usd": "10"}], owner="Acme")
        assert (rule.scope, rule.period, rule.block_usd, rule.owner) == ("project", "month", 10.0, "Acme")
        for bad, message in [
            ([{"scope": "team", "warn_usd": 1}], "scope"),
            ([{"scope": "user", "period": "week", "warn_usd": 1}], "period"),
            ([{"scope": "user", "warn_usd": -1}], "negative"),
            ([{"scope": "user", "warn_usd": "lots"}], "amount"),
            ([{"scope": "user"}], "needs"),
            ({"scope": "user"}, "list"),
        ]:
            with pytest.raises(ValueError, match=message):
                parse_rules(bad)

    def test_a_policy_carries_its_budgets(self):
        policy({"scope": "user", "period": "month", "warn_usd": 200})
        assert budgets.rules()[0].owner == "Acme"
        with pytest.raises(PolicyError, match="budgets"):
            parse({"schema": "lumi.policy/v1", "budgets": [{"scope": "user"}]}, source="t")

    def test_settings_become_rules(self):
        budgets.configure(_Settings({"cost_tracking": {"budget_alert_usd": 2, "daily_limit_usd": 5,
                                                       "turn_limit_usd": 1, "enabled": True}}))
        assert [(r.scope, r.warn_usd, r.approve_usd, r.block_usd) for r in budgets.rules()] == [
            ("user", 2.0, None, None), ("user", None, 5.0, None), ("turn", None, None, 1.0)]
        budgets.configure(_Settings({"cost_tracking": {"budget_alert_usd": None, "daily_limit_usd": 0}}))
        assert budgets.rules() == ()


class TestSpend:
    def test_levels_by_scope(self, ledger, tmp_path):
        spend(ledger, 3, project="C:/src/payments-api")
        spend(ledger, 1, project="C:/src/website")
        ledger.record(provider="sonn", model="sonn-auto", purpose="turn", stats={"input_tokens": 10})  # unpriced
        rules = parse_rules([
            {"scope": "user", "warn_usd": 3, "approve_usd": 4.5, "block_usd": 10},
            {"scope": "project", "match": "*/payments*", "block_usd": 3},
            {"scope": "turn", "block_usd": 0.5},
        ])
        verdicts = {v.rule.scope: v for v in budgets.evaluate("C:/src/payments-api", turn_spend=0.25, applicable=rules)}
        assert verdicts["user"].level == "warn" and verdicts["user"].spent == pytest.approx(4)
        assert verdicts["project"].level == "block" and "this project" in verdicts["project"].message
        assert "turn" not in verdicts
        # The payments budget doesn't apply elsewhere; another account's spend isn't mine.
        assert {v.rule.scope for v in budgets.evaluate("C:/src/website", applicable=rules)} == {"user"}
        assert budgets.evaluate("C:/src/website", applicable=rules, user="someone-else") == []

    def test_status_for_settings(self, ledger):
        spend(ledger, 2)
        policy({"scope": "user", "period": "month", "warn_usd": 5, "block_usd": 8})
        [row] = budgets.status("")
        assert (row["owner"], row["spent_usd"], row["block_usd"]) == ("Acme", 2.0, 8.0)


class TestTurns:
    def test_an_alert_shows_once_per_period(self, ledger, tmp_path):
        spend(ledger, 2)
        budgets.configure(_Settings({"cost_tracking": {"budget_alert_usd": 1}}))
        first, _ = session(tmp_path, costly_call(0.01))
        events = list(first.run("Go"))
        warnings = [e for e in events if e.get("kind") == "budget_warning"]
        assert len(warnings) == 1 and "past your $1.00 alert" in warnings[0]["message"]
        second, _ = session(tmp_path, costly_call(0.01))
        assert not [e for e in second.run("Go") if e.get("kind") == "budget_warning"]
        assert "budget.warning" in audit_types(ledger)

    def test_past_a_limit_the_person_decides(self, ledger, tmp_path):
        spend(ledger, 5)
        budgets.configure(_Settings({"cost_tracking": {"daily_limit_usd": 4}}))
        asked = []

        def answer(reply):
            def on_user_input(question, options):
                asked.append((question, options))
                return reply
            return on_user_input

        continuing, backend = session(tmp_path, costly_call(0.01, call=tool_call("file_read", {"path": "x"})),
                                      costly_call(0.01))
        events = list(continuing.run("Go", on_user_input=answer("Continue")))
        assert backend.stream_count == 2 and len(asked) == 1  # asked once, not before every request
        assert asked[0][1] == ["Continue", "Stop"] and "past your $4.00 limit" in asked[0][0]
        assert not [e for e in events if e.get("code") == "budget_exceeded"]

        budgets.reset()
        budgets.configure(_Settings({"cost_tracking": {"daily_limit_usd": 4}}))
        stopping, backend = session(tmp_path, costly_call(0.01))
        events = list(stopping.run("Go", on_user_input=answer("Stop")))
        assert backend.stream_count == 0
        assert [e["message"] for e in events if e.get("code") == "budget_exceeded"][0].startswith("Stopped at your request")

        unattended, backend = session(tmp_path, costly_call(0.01))
        events = list(unattended.run("Go"))  # the gateway can't ask
        assert backend.stream_count == 0 and "can't ask" in next(e for e in events if e.get("code") == "budget_exceeded")["message"]
        decisions = [json.loads(line)["data"]["decision"] for path in ledger.audit._files()
                     for line in path.read_text(encoding="utf-8").splitlines() if '"budget.approval"' in line]
        assert decisions == ["approved", "declined", "unavailable"]

    def test_a_spent_budget_refuses_the_turn(self, ledger, tmp_path):
        spend(ledger, 10)
        policy({"scope": "user", "period": "month", "block_usd": 8})
        refused, backend = session(tmp_path, costly_call())
        events = list(refused.run("Go"))
        assert backend.stream_count == 0
        error = next(e for e in events if e.get("code") == "budget_exceeded")
        assert "Acme's $8.00 budget" in error["message"] and "administrator" in error["message"]

    def test_a_turn_stops_at_its_cap(self, ledger, tmp_path):
        budgets.configure(_Settings({"cost_tracking": {"turn_limit_usd": 1.5}}))
        capped, backend = session(tmp_path, costly_call(1, call=tool_call("file_read", {"path": "x"})),
                                  costly_call(1, call=tool_call("file_read", {"path": "x"}, call_id="c2")),
                                  costly_call(1))
        events = list(capped.run("Go"))
        assert backend.stream_count == 2  # the third request would start past the cap
        error = next(e for e in events if e.get("code") == "budget_exceeded")
        assert "This turn has spent $2.00" in error["message"] and "send Continue" in error["message"]
        # A new turn has a fresh allowance.
        again, backend = session(tmp_path, costly_call(0.5))
        list(again.run("Continue"))
        assert backend.stream_count == 1
        assert "budget.block" in audit_types(ledger)

    def test_a_budget_can_require_priced_models(self, ledger, tmp_path):
        policy({"scope": "user", "period": "month", "block_usd": 100, "block_unpriced": True})
        unpriced, backend = session(tmp_path, costly_call(), name="sonn", model="sonn-auto")
        events = list(unpriced.run("Go"))
        assert backend.stream_count == 0
        assert "needs a price for sonn-auto" in next(e for e in events if e.get("code") == "budget_exceeded")["message"]
        local, backend = session(tmp_path, [text_delta("ok"), done()], name="ollama", model="qwen3:32b")
        list(local.run("Go"))
        assert backend.stream_count == 1  # local models are priced ($0)


class TestSettings:
    def test_limits_are_validated_and_budgets_listed(self, ledger, tmp_path):
        from lumi.gui import ws_commands
        from lumi.gui.costs import CostTracker
        from lumi.gui.settings import SettingsManager

        settings = SettingsManager(tmp_path / "settings.json")

        class _WS:
            def __init__(self):
                self.sent = []

            async def send_json(self, payload):
                self.sent.append(payload)

        def command(name, **msg):
            def update_setting_value(section, key, value, *, clear_secret=False):
                settings.set(section, key, value)
                return settings.get_masked()

            state = SimpleNamespace(settings=settings, update_setting_value=update_setting_value,
                                    costs=CostTracker(tmp_path / "costs.json"),
                                    get_init_data=lambda refresh_only=False: {"event": "init"})
            ctx = ws_commands.CommandContext(ws=_WS(), state=state, msg={"command": name, **msg},
                                             runs=SimpleNamespace(busy=False))
            asyncio.run(ws_commands.HANDLERS[name](ctx))
            return ctx.ws.sent

        command("update_settings", section="cost_tracking", key="turn_limit_usd", value=2.5)
        assert settings.get("cost_tracking", "turn_limit_usd") == 2.5
        command("update_settings", section="cost_tracking", key="turn_limit_usd", value=None)
        assert settings.get("cost_tracking", "turn_limit_usd") is None
        sent = command("update_settings", section="cost_tracking", key="daily_limit_usd", value="ten")
        assert sent[0]["source"] == "settings" and "amount in USD" in sent[0]["message"]

        policy({"scope": "turn", "block_usd": 3})
        [costs] = command("get_costs")
        assert costs["data"]["budgets"][0]["block_usd"] == 3.0


def test_rule_ids_distinguish_owners():
    assert Rule(scope="user", warn_usd=1).id != Rule(scope="user", warn_usd=1, owner="Acme").id
    assert current_user()
