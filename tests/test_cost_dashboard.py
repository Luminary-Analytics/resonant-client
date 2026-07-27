from datetime import date
from pathlib import Path

import pytest

from resonant_client.gui.costs import CostTracker


ROOT = Path(__file__).parents[1]
APP_JS = ROOT / "resonant_client" / "gui" / "static" / "app.js"
APP_PY = ROOT / "resonant_client" / "gui" / "app.py"
STYLES_CSS = APP_JS.with_name("styles.css")


def frontend_source() -> str:
    """All frontend scripts. ResonantApp is split across mixin files, so
    reading app.js alone would fail whenever a method moves between them."""
    return "\n".join(
        path.read_text(encoding="utf-8")
        for path in sorted(APP_JS.parent.glob("*.js"))
    )


def test_cost_tracker_returns_persisted_total_and_current_session(tmp_path):
    tracker = CostTracker(tmp_path / "costs.json")

    tracker.record_usage("gpt-4o-mini", 1_000_000, 500_000)
    payload = tracker.get_all_costs()

    assert payload["session"] == {
        "input_tokens": 1_000_000,
        "output_tokens": 500_000,
        "cost_usd": pytest.approx(0.45),
    }
    assert payload["today"] == payload["daily"][date.today().isoformat()]
    assert payload["total"] == {
        "input_tokens": 1_000_000,
        "output_tokens": 500_000,
        "cost_usd": pytest.approx(0.45),
    }

    reloaded = CostTracker(tmp_path / "costs.json").get_all_costs()
    assert reloaded["session"]["input_tokens"] == 0
    assert reloaded["total"] == payload["total"]


def test_settings_exposes_usage_cost_dashboard_contract():
    source = frontend_source()
    styles = STYLES_CSS.read_text(encoding="utf-8")

    assert "this.send({ command: 'get_costs' });" in source
    assert "this.costData = event.data || null;" in source
    assert "id: 'cost_tracking', title: 'Usage & Cost'" in source
    assert "_renderCostDashboard(data)" in source
    assert "Tracked total" in source
    assert "Recent daily usage" in source
    assert "Local and Ollama-hosted models still report tokens" in source
    assert ".cost-stat-grid" in styles
    assert ".cost-history-row" in styles
    assert ".cost-budget-track" in styles


def test_session_history_is_replayed_before_runtime_rebuild():
    # Reads the registered handler rather than a line range in app.py. The
    # command has moved files once already; what matters is the ordering
    # inside whatever function actually serves it.
    import inspect

    from resonant_client.gui import ws_commands

    body = inspect.getsource(ws_commands.HANDLERS["switch_session"])

    assert body.index('"event": "session_loaded"') < body.index("state.create_backend(")
    assert '"runtime_pending": bool(record.backend_type)' in body
    assert '"event": "status_msg"' in body
    assert "Session loaded. Its runtime is unavailable" in body
