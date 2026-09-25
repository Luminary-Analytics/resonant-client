"""Prices (lumi/pricing.py) and usage records (lumi/usage.py): one record per
model call, priced from the provider's report, the organization, the user or
the bundled list, and never $0 for an unknown model."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from lumi import pricing, usage
from lumi import policy as lumi_policy
from lumi.policy import PolicyError, parse
from lumi.usage import UsageLedger, token_counts
from tests.streaming_stub import StreamingBackend, done, text_delta, tool_call


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
    return instance


class TestPrices:
    def test_the_list_prefers_the_most_specific_model(self):
        assert pricing.resolve("anthropic", "claude-opus-5-5")[2] == "claude-opus-5-5*"
        assert pricing.resolve("anthropic", "claude-opus-5")[0].input == 5
        assert pricing.resolve("anthropic", "claude-opus-4-20250514")[0].input == 15
        assert pricing.resolve("openai", "gpt-5.4-mini-2026-03-01")[0].input == 0.75
        assert pricing.resolve("openai", "gpt-5.4")[0].output == 15
        # A different model that merely starts the same way is not guessed.
        assert pricing.resolve("openai", "gpt-5.3-codex-spark")[:2] == (None, "unpriced")

    def test_cache_reads_and_writes(self):
        # Claude Sonnet 5: $2 in, $10 out, $0.20 cache reads, $2.50 5-minute writes.
        priced = pricing.cost("anthropic", "claude-sonnet-5", input_tokens=1_000_000, cached_tokens=600_000,
                              cache_write_tokens=100_000, output_tokens=100_000)
        assert priced["cost_usd"] == pytest.approx(0.6 + 0.12 + 0.25 + 1.0)
        assert priced["price_source"] == "catalog"

    def test_unknown_local_and_subscription_models(self):
        unknown = pricing.cost("sonn", "sonn-auto", input_tokens=1000, output_tokens=100)
        assert unknown["cost_usd"] is None and unknown["price_source"] == "unpriced"
        assert pricing.cost("ollama", "qwen3:32b", input_tokens=1000)["cost_usd"] == 0
        for provider, model in (("ollama", "glm-5:cloud"), ("codex", "gpt-5.5"), ("claude-code", "opus")):
            priced = pricing.cost(provider, model, input_tokens=1000)
            assert (priced["cost_usd"], priced["price_source"]) == (None, "subscription")

    def test_a_reported_cost_wins_and_the_estimate_is_kept(self):
        priced = pricing.cost("openrouter", "anthropic/claude-sonnet-5", input_tokens=1_000_000,
                              output_tokens=0, reported_cost=1.75)
        assert priced == {**priced, "cost_usd": 1.75, "reported_cost_usd": 1.75,
                          "computed_cost_usd": pytest.approx(2.0), "price_source": "reported"}
        assert pricing.cost("openrouter", "x/y", reported_cost=float("nan"))["cost_usd"] is None

    def test_organization_prices_win_over_yours_and_the_list(self):
        pricing.configure(_Settings({"cost_tracking": {"price_overrides": {
            "anthropic:claude-opus-*": {"input": 4.5, "output": 22}, "sonn:*": {"input": 1, "output": 2}}}}))
        assert pricing.resolve("anthropic", "claude-opus-5-5")[1:] == ("override", "anthropic:claude-opus-*")
        assert pricing.resolve("sonn", "sonn-auto")[0].output == 2
        lumi_policy.set_for_tests(parse({
            "schema": "lumi.policy/v1", "organization": "Acme",
            "pricing": {"prices": {"anthropic:claude-opus-*": {"input": 3.2, "output": 16}}},
        }, source="test"))
        price, source, _ = pricing.resolve("anthropic", "claude-opus-5-5")
        assert (price.input, source) == (3.2, "organization")
        assert pricing.describe_catalog()["organization"][0]["pattern"] == "anthropic:claude-opus-*"

    def test_a_policy_with_bad_prices_is_refused(self):
        with pytest.raises(PolicyError, match="pricing.prices"):
            parse({"schema": "lumi.policy/v1", "pricing": {"prices": {"x": {"input": -1, "output": 1}}}}, source="t")

    def test_price_lines_from_settings(self):
        lines = ["# negotiated", "anthropic:claude-opus-* 3.2 16 0.16", "", "conn-*:llama-* 0.2 0.6"]
        assert pricing.parse_override_lines(lines) == {
            "anthropic:claude-opus-*": {"input": 3.2, "output": 16, "cached_input": 0.16},
            "conn-*:llama-*": {"input": 0.2, "output": 0.6},
        }
        for bad, message in [(["gpt-5 1"], "Line 1"), (["gpt-5 one two"], "numbers"), (["gpt-5 -1 2"], "negative")]:
            with pytest.raises(ValueError, match=message):
                pricing.parse_override_lines(bad)


class TestTokenCounts:
    @pytest.mark.parametrize("stats, expected", [
        # Lumi's Anthropic adapter: input already includes cache reads and writes.
        ({"input_tokens": 1000, "cached_tokens": 600, "cache_write_tokens": 100, "output_tokens": 50},
         (1000, 600, 100, 50)),
        # Claude Code's raw usage counts them separately.
        ({"input_tokens": 300, "cache_read_input_tokens": 600, "cache_creation_input_tokens": 100,
          "output_tokens": 50}, (1000, 600, 100, 50)),
        ({"input_tokens": 900, "cached_input_tokens": 400, "output_tokens": 70}, (900, 400, 0, 70)),  # Codex
        ({"prompt_eval_count": 120, "eval_count": 80}, (120, 0, 0, 80)),  # Ollama
    ])
    def test_each_providers_names(self, stats, expected):
        counts = token_counts(stats)
        assert (counts["input_tokens"], counts["cached_tokens"], counts["cache_write_tokens"],
                counts["output_tokens"]) == expected


class TestLedger:
    def test_records_are_priced_and_summed(self, ledger):
        ledger.record(provider="anthropic", model="claude-haiku-4-5-20251001", purpose="turn", project="p",
                      stats={"input_tokens": 1_000_000, "output_tokens": 0})
        ledger.record(provider="sonn", model="sonn-auto", purpose="turn", project="p",
                      stats={"input_tokens": 10, "output_tokens": 5})
        ledger.record(provider="codex", model="gpt-5.5", purpose="title", stats={"input_tokens": 10})
        rows = ledger.records()
        assert [row["price_source"] for row in rows] == ["catalog", "unpriced", "subscription"]
        assert rows[0]["user"] and len(rows[0]["id"]) == 32
        assert usage.totals(rows) == {"calls": 3, "input_tokens": 1_000_020, "cached_tokens": 0,
                                      "output_tokens": 5, "cost_usd": 1.0, "unpriced_calls": 1,
                                      "subscription_calls": 1}
        assert usage.breakdown(rows, "purpose")["title"]["calls"] == 1

    def test_reading_follows_new_records_and_other_writers(self, ledger, tmp_path):
        ledger.record(provider="ollama", model="qwen3:32b", purpose="turn", stats={"input_tokens": 1})
        assert len(ledger.records()) == 1
        other = UsageLedger(tmp_path / "usage")  # the gateway, say
        other.record(provider="ollama", model="qwen3:32b", purpose="turn", stats={"input_tokens": 2})
        ledger.record(provider="ollama", model="qwen3:32b", purpose="turn", stats={"input_tokens": 3})
        assert [row["input_tokens"] for row in ledger.records()] == [1, 2, 3]
        month = ledger.records()[0]["ts"][:7]
        assert ledger.records(since=month) and not ledger.records(until="2000-01")

    def test_turned_off_and_the_listener(self, ledger):
        seen = []
        usage.set_listener(seen.append)
        usage.record(provider="ollama", model="m", purpose="turn", stats={"input_tokens": 1})
        usage.set_listener(None)
        usage.record(provider="ollama", model="m", purpose="turn", stats={"input_tokens": 1})
        assert len(seen) == 1 and seen[0]["price_source"] == "local"
        ledger.configure(_Settings({"cost_tracking": {"enabled": False}}))
        assert ledger.record(provider="ollama", model="m", purpose="turn", stats={"input_tokens": 1}) is None
        assert len(ledger.records()) == 2


class TestTurns:
    def test_each_model_call_is_recorded_and_priced(self, ledger, tmp_path):
        from lumi.engine.session import Session

        backend = StreamingBackend(name="anthropic", model="claude-sonnet-5", scripts=[
            [tool_call("file_read", {"path": "a.txt"}),
             done(model="claude-sonnet-5", stats={"input_tokens": 2000, "cached_tokens": 1000, "output_tokens": 100})],
            [text_delta("Done."), done(model="claude-sonnet-5", stats={"input_tokens": 3000, "output_tokens": 50})],
        ])
        (tmp_path / "a.txt").write_text("hello", encoding="utf-8")
        session = Session(backend, auto_approve=True)
        session.project_path = str(tmp_path)
        session.audit_session_id = "conv-9"
        events = list(session.run("Read a.txt"))

        rows = ledger.records()
        assert [(row["purpose"], row["session"], row["input_tokens"]) for row in rows] == [
            ("turn", "conv-9", 2000), ("turn", "conv-9", 3000)]
        assert rows[0]["cost_usd"] == pytest.approx((1000 * 2 + 1000 * 0.2 + 100 * 10) / 1e6)
        statuses = [e for e in events if e.get("event") == "status" and e.get("stats")]
        assert statuses[0]["stats"]["cost_usd"] == rows[0]["cost_usd"]
        assert statuses[0]["stats"]["price_source"] == "catalog"

    def test_an_auxiliary_request_is_recorded_with_its_purpose(self, ledger):
        from lumi.engine.request_purpose import auxiliary_stream

        backend = StreamingBackend(name="ollama", model="qwen3:32b",
                                   events=[text_delta("A title"), done(stats={"input_tokens": 40, "output_tokens": 4})])
        events = list(auxiliary_stream(backend, "title", usage_context={"session": "conv-1"},
                                       user_msg="x", conversation_history=[], instructions="", tools=[], max_tokens=32))
        assert events[-1][0] == "done"
        [row] = ledger.records()
        assert (row["purpose"], row["session"], row["cost_usd"], row["price_source"]) == ("title", "conv-1", 0.0, "local")

    def test_a_workers_forwarded_events_are_not_recorded_twice(self, ledger, tmp_path, monkeypatch):
        from lumi import audit
        from lumi.audit import AuditLog
        from lumi.engine.session import Session

        log = AuditLog(tmp_path / "audit")
        audit.set_for_tests(log)
        session = Session(StreamingBackend(events=[done()]))

        def turn(*args, **kwargs):
            # What the parent passes on from a delegated worker's run().
            yield {"event": "tool.call", "name": "grep", "call_id": "w1", "arguments": {}, "_subagent": True}
            yield {"event": "error", "message": "worker failed", "_subagent": True}
            yield {"event": "session.end"}

        monkeypatch.setattr(session, "_run_turn", turn)
        list(session.run("Go"))
        records = [json.loads(line) for path in log._files() for line in path.read_text(encoding="utf-8").splitlines()]
        assert [r["type"] for r in records] == ["turn.start", "turn.end"]
        assert records[-1]["data"]["outcome"] == "completed"


class TestCostsPage:
    def _command(self, state, command, **msg):
        from lumi.gui import ws_commands

        class _WS:
            def __init__(self):
                self.sent = []

            async def send_json(self, payload):
                self.sent.append(payload)

        ctx = ws_commands.CommandContext(ws=_WS(), state=state, msg={"command": command, **msg},
                                         runs=SimpleNamespace(busy=False))
        asyncio.run(ws_commands.HANDLERS[command](ctx))
        return ctx.ws.sent

    def test_costs_include_this_months_calls_and_the_prices(self, ledger, tmp_path):
        from lumi.gui.costs import CostTracker

        ledger.record(provider="sonn", model="sonn-auto", purpose="turn", stats={"input_tokens": 10})
        state = SimpleNamespace(costs=CostTracker(tmp_path / "costs.json"))
        [event] = self._command(state, "get_costs")
        month = event["data"]["month"]
        assert month["unpriced_calls"] == 1 and month["by_model"]["sonn-auto"]["calls"] == 1
        assert event["data"]["pricing"]["as_of"] == pricing.CATALOG_AS_OF

    def test_price_lines_are_validated_and_saved(self, tmp_path):
        from lumi.gui.settings import SettingsManager

        settings = SettingsManager(tmp_path / "settings.json")

        def update_setting_value(section, key, value, *, clear_secret=False):
            settings.set(section, key, value)
            return settings.get_masked()

        state = SimpleNamespace(settings=settings, update_setting_value=update_setting_value,
                                get_init_data=lambda refresh_only=False: {"event": "init"})
        sent = self._command(state, "update_settings", section="cost_tracking", key="price_overrides",
                             value=["sonn:* 1 2"])
        assert sent[0]["event"] == "settings"
        assert settings.get("cost_tracking", "price_overrides") == {"sonn:*": {"input": 1.0, "output": 2.0}}
        sent = self._command(state, "update_settings", section="cost_tracking", key="price_overrides",
                             value=["sonn:* free lunch"])
        assert "numbers" in sent[0]["message"] and sent[0]["source"] == "settings"

    def test_the_cost_tracker_counts_unpriced_calls(self, tmp_path):
        from lumi.gui.costs import CostTracker

        tracker = CostTracker(tmp_path / "costs.json")
        assert tracker.record_usage("sonn-auto", 100, 10, provider="sonn") is None
        assert tracker.record_usage("gpt-5.5", 100, 10, provider="codex") is None
        tracker.add(50, 5, 0.25)
        session = tracker.get_session_cost()
        assert (session["input_tokens"], session["cost_usd"], session["unpriced_calls"],
                session["subscription_calls"]) == (250, 0.25, 1, 1)

    def test_the_app_counts_every_recorded_call(self, ledger, tmp_path, monkeypatch):
        # Titles and compaction are recorded outside a turn's status events;
        # the totals Settings shows must still include them.
        from lumi.engine.request_purpose import auxiliary_stream
        from lumi.gui.app import AppState
        from lumi.gui.costs import CostTracker

        state = SimpleNamespace(costs=CostTracker(tmp_path / "costs.json"))
        usage.set_listener(lambda record: AppState._count_usage(state, record))
        backend = StreamingBackend(name="conn-x", model="claude-haiku-4-5",
                                   events=[done(model="claude-haiku-4-5",
                                                stats={"input_tokens": 1_000_000, "output_tokens": 0})])
        list(auxiliary_stream(backend, "title", user_msg="x", conversation_history=[], instructions="",
                              tools=[], max_tokens=32))
        assert state.costs.get_session_cost()["cost_usd"] == pytest.approx(1.0)


class TestExport:
    def test_summary_csv_and_jsonl(self, ledger, capsys):
        ledger.record(provider="anthropic", model="claude-haiku-4-5", purpose="turn", project="p",
                      stats={"input_tokens": 1_000_000, "output_tokens": 0})
        ledger.record(provider="sonn", model="sonn-auto", purpose="turn", project="p", stats={"input_tokens": 10})
        month = ledger.records()[0]["ts"][:7]

        assert usage.main(["--since", f"{month}-01"]) == 0
        summary = capsys.readouterr().out
        assert "2 calls" in summary and "$1.00" in summary and "1 unpriced" in summary
        assert "claude-haiku-4-5" in summary and "unpriced" in summary.splitlines()[-1]

        usage.main(["--since", f"{month}-01", "--format", "csv"])
        lines = capsys.readouterr().out.splitlines()
        assert lines[0].startswith("ts,user,project") and len(lines) == 3

        usage.main(["--since", f"{month}-01", "--format", "jsonl", "--by", "project"])
        assert [json.loads(line)["model"] for line in capsys.readouterr().out.splitlines()] == [
            "claude-haiku-4-5", "sonn-auto"]
