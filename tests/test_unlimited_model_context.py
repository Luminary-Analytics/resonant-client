"""Contracts for provider-native generation length and full context windows."""

from pathlib import Path

from resonant_client.engine.compression import model_context_budget
from resonant_client.gui.app import AppState


BACKENDS_SOURCE = Path(__file__).parents[1] / "resonant_client" / "backends.py"


def test_model_requests_never_set_num_predict():
    source = BACKENDS_SOURCE.read_text(encoding="utf-8")
    assert "num_predict" not in source


def test_all_interactive_and_harness_roles_have_no_generation_cap():
    assert AppState.SESSION_MAX_TOKENS is None
    assert set(AppState.HARNESS_ROLE_MAX_TOKENS.values()) == {None}


def test_cloud_models_reserve_output_headroom_inside_advertised_windows():
    assert model_context_budget("glm-5.2:cloud") == 874_496
    assert model_context_budget("deepseek-v4-pro:cloud") == 917_504
    assert model_context_budget("deepseek-v4-flash:cloud") == 917_504
