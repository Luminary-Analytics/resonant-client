"""Tests for model-family prompt profiles and scoped agent roles."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from resonant_client.engine.agents import get_agent_type
from resonant_client.engine.model_prompts import (
    build_model_prompt,
    detect_model_family,
    get_model_prompt_profile,
)
from resonant_client.engine.session import (
    Session,
    get_system_instruction_layers,
    get_system_instructions,
    inspect_system_instructions,
)
from tests.streaming_stub import StreamingBackend


@pytest.mark.parametrize(
    ("model_name", "expected"),
    [
        ("glm-5.2:cloud", "glm"),
        ("mlx-community/GLM-5.3-Flash-4bit", "glm"),
        ("zai/glm-5", "glm"),
        ("deepseek-v4-pro:cloud", "deepseek"),
        ("DeepSeek_R1", "deepseek"),
        ("kimi-k3", "kimi"),
        ("qwen3-coder:30b", "generic"),
        (None, "generic"),
    ],
)
def test_detect_model_family(model_name, expected):
    assert detect_model_family(model_name) == expected


def test_profiles_share_model_neutral_guidance():
    glm = build_model_prompt("glm-5.2:cloud")
    deepseek = build_model_prompt("deepseek-v4-pro:cloud")
    generic = build_model_prompt("qwen3-coder")
    kimi = build_model_prompt("kimi-k3")

    for prompt in (glm, deepseek, generic, kimi):
        assert "You are Resonant, a thoughtful technical collaborator" in prompt
        assert "Read relevant code and project instructions before editing" in prompt
        assert "match\nthe user's tone" in prompt
        assert "Treat tool activity as background" in prompt
        assert "The response must stand on its own" in prompt
        assert "avoid generic praise" in prompt
        assert "Run independent reads together" in prompt
        assert "Never claim an action or check that did not complete" in prompt
        assert "Continue through ordinary\n  tool failures" in prompt
        assert "Delegate only bounded independent work" in prompt
        assert "Use `await_user` only for one consequential requirement" in prompt
        assert "set `recommended_option` to that exact value" in prompt
        assert "reuse settled evidence instead of\nrediscovering it" in prompt
        assert "Use `search_tools` once" in prompt
        # Keep the invariant prompt small enough for fast local-model prefills.
        assert len(prompt) < 3_200
    assert len({glm, deepseek, generic, kimi}) == 1


def test_unknown_role_falls_back_to_primary():
    unknown = build_model_prompt("qwen", role="not-a-role")
    primary = build_model_prompt("qwen", role="primary")
    assert unknown == primary


def test_system_prompt_layers_model_role_and_project_context():
    prompt = get_system_instructions(
        project_instructions="Use repository test command: pytest -q",
        working_directory="/repo",
        model_name="deepseek-v4-pro:cloud",
        prompt_role="subagent",
        role_instructions="Read only src/parser.py",
    )

    assert "Use `search_tools` once" in prompt
    assert "Role: isolated sub-agent" in prompt
    assert "SCOPED ROLE INSTRUCTIONS" in prompt
    assert "Read only src/parser.py" in prompt
    assert "PROJECT INSTRUCTIONS" in prompt
    assert "pytest -q" in prompt


def test_plan_mode_keeps_model_profile_and_disables_tools():
    prompt = get_system_instructions(
        plan_mode=True,
        model_name="glm-5.2:cloud",
    )
    assert "You are Resonant, a thoughtful technical collaborator" in prompt
    assert "CURRENT MODE: PLAN" in prompt
    assert "Do not call tools" in prompt
    assert "RESONANT TOOL NOTES" not in prompt


def test_subagent_profile_is_wired_into_child_session():
    parent = Session(backend=StreamingBackend(), max_steps=1)
    captured = {}

    def fake_run(child, user_msg, **kwargs):
        captured["prompt_role"] = child.prompt_role
        captured["role_instructions"] = child.role_instructions
        captured["user_msg"] = user_msg
        yield {"event": "session.end"}

    with patch.object(Session, "run", fake_run):
        events = list(parent._execute_task(
            fn_args={"prompt": "Map parser call sites", "agent_type": "explore"},
            call_id="task-1",
            fn_args_str='{"agent_type":"explore","prompt":"Map parser call sites"}',
        ))

    assert captured["prompt_role"] == "subagent"
    assert captured["role_instructions"] == get_agent_type("explore").system_prompt
    assert captured["user_msg"] == "Map parser call sites"
    completed = next(event for event in events if event["event"] == "subagent.end")
    assert completed["call_id"] == "task-1"
    assert completed["result"] == "(no output)"


def test_profile_metadata_matches_rendered_family():
    assert get_model_prompt_profile("glm-5.2").display_name == "GLM 5.x"
    assert get_model_prompt_profile("deepseek-v4").family == "deepseek"


def test_prompt_inspector_matches_exact_assembled_prompt():
    kwargs = {
        "project_instructions": "Run pytest -q",
        "working_directory": "/repo",
        "model_name": "glm-5.2:cloud",
        "prompt_role": "primary",
    }
    layers = get_system_instruction_layers(**kwargs)
    inspected = inspect_system_instructions(**kwargs)

    assert inspected["profile"] == "GLM 5.x"
    assert inspected["prompt"] == get_system_instructions(**kwargs)
    assert inspected["prompt"] == "\n\n".join(
        layer["content"] for layer in layers
    )
    assert inspected["estimated_tokens"] > 0
    assert inspected["estimated_tokens"] < 800
    assert len(inspected["sha256"]) == 64
    assert [layer["id"] for layer in inspected["layers"]] == [
        "runtime", "model_profile", "project", "tools"
    ]
