"""Tests for the specialist registry and prompt assembly."""

from __future__ import annotations

import pytest

from resonant_client.orchestration import (
    NodeSpecialization,
    SPECIALISTS,
    assemble_system_prompt,
    filter_tools_for_specialist,
    get_specialist,
)


def test_every_node_specialization_has_a_profile():
    for spec in NodeSpecialization.ALL:
        assert spec in SPECIALISTS, f"{spec} missing from SPECIALISTS registry"


def test_get_specialist_raises_on_unknown():
    with pytest.raises(KeyError):
        get_specialist("bogus")


def test_explore_cannot_edit_files():
    profile = get_specialist(NodeSpecialization.EXPLORE)
    assert "file_write" not in profile.tool_allowlist
    assert "file_edit" not in profile.tool_allowlist
    assert "bash" not in profile.tool_allowlist
    assert "file_read" in profile.tool_allowlist


def test_verify_can_run_bash_for_tests_but_not_edit():
    profile = get_specialist(NodeSpecialization.VERIFY)
    assert "bash" in profile.tool_allowlist
    assert "file_write" not in profile.tool_allowlist
    assert "file_edit" not in profile.tool_allowlist


def test_research_cannot_edit_or_run_shell():
    profile = get_specialist(NodeSpecialization.RESEARCH)
    assert "file_write" not in profile.tool_allowlist
    assert "bash" not in profile.tool_allowlist
    # But CAN browse
    assert "browser_navigate" in profile.tool_allowlist


def test_implement_has_full_edit_powers():
    profile = get_specialist(NodeSpecialization.IMPLEMENT)
    for tool in ("file_write", "file_edit", "file_read", "bash"):
        assert tool in profile.tool_allowlist


def test_plan_specialist_is_read_only():
    """Planner decomposes — never edits."""
    profile = get_specialist(NodeSpecialization.PLAN)
    assert "file_write" not in profile.tool_allowlist
    assert "file_edit" not in profile.tool_allowlist
    assert "bash" not in profile.tool_allowlist


def test_confidence_threshold_is_per_specialization():
    """Different specializations have different confidence thresholds."""
    assert get_specialist(NodeSpecialization.VERIFY).confidence_threshold > \
           get_specialist(NodeSpecialization.IMPLEMENT).confidence_threshold


def test_max_steps_reasonable():
    # implement gets the most steps, explore the fewest
    impl = get_specialist(NodeSpecialization.IMPLEMENT).max_steps
    expl = get_specialist(NodeSpecialization.EXPLORE).max_steps
    assert impl >= expl


# ── Prompt assembly ─────────────────────────────────────────────────────


def test_assemble_system_prompt_includes_all_layers():
    prompt = assemble_system_prompt(
        specialization=NodeSpecialization.IMPLEMENT,
        node_goal="add dark mode toggle",
        intent="ship dark mode",
        project_conventions="# Use Tailwind\n",
        extra_context="prerequisite found prefers-color-scheme works",
    )
    assert "PROJECT CONVENTIONS" in prompt
    assert "Use Tailwind" in prompt
    assert "SPECIALIZATION: IMPLEMENT" in prompt
    assert "Goal:   add dark mode toggle" in prompt
    assert "Intent: ship dark mode" in prompt
    assert "CONTEXT FROM PRIOR NODES" in prompt
    assert "prerequisite found" in prompt


def test_prompt_omits_empty_sections():
    prompt = assemble_system_prompt(
        specialization=NodeSpecialization.EXPLORE,
        node_goal="read settings.py",
        intent="understand the codebase",
    )
    assert "PROJECT CONVENTIONS" not in prompt
    assert "CONTEXT FROM PRIOR NODES" not in prompt
    assert "SPECIALIZATION: EXPLORE" in prompt


# ── Tool filtering ──────────────────────────────────────────────────────


def test_filter_tools_drops_disallowed():
    fake_tools = [
        {"function": {"name": "file_read", "description": "..."}},
        {"function": {"name": "file_write", "description": "..."}},
        {"function": {"name": "bash", "description": "..."}},
    ]
    explore_tools = filter_tools_for_specialist(NodeSpecialization.EXPLORE, fake_tools)
    names = {t["function"]["name"] for t in explore_tools}
    assert names == {"file_read"}

    impl_tools = filter_tools_for_specialist(NodeSpecialization.IMPLEMENT, fake_tools)
    impl_names = {t["function"]["name"] for t in impl_tools}
    assert impl_names == {"file_read", "file_write", "bash"}


def test_filter_handles_malformed_tool_entries():
    fake_tools = [
        {"function": {"name": "file_read"}},
        {"no_function_key": True},
        {"function": {}},  # no name
    ]
    out = filter_tools_for_specialist(NodeSpecialization.EXPLORE, fake_tools)
    assert len(out) == 1
    assert out[0]["function"]["name"] == "file_read"
