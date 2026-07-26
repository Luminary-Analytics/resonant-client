"""The contract between HarnessPrompts and the application that hosts it.

These 95 methods used to live on `gui.AppState`, so exercising any of them
meant constructing the whole GUI application object. The point of the
extraction is that they now depend on a small, stated surface — this file pins
that surface, because a dependency that is not tested will quietly grow back.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from resonant_client.gui.app import AppState
from resonant_client.harness.prompts import HarnessPrompts

# Everything HarnessPrompts is allowed to reach for on its host. Adding to this
# list is a real design decision, not a detail — it widens the coupling the
# extraction exists to bound.
ALLOWED_APP_SURFACE = {
    "HARNESS_ROLE_MAX_TOKENS",
    "SESSION_MAX_TOKENS",
    "available_backends",
    "backend",
    "backend_spec",
    "build_backend_spec",
    "build_session",
    "detect_backends",
    "harness_service",
    "normalize_session_mode",
    "normalize_session_role",
    "project",
    "settings",
}


def _stub_app(project_path: str, *, harness_service=None) -> SimpleNamespace:
    """A host object providing exactly the documented surface, nothing more."""
    return SimpleNamespace(
        HARNESS_ROLE_MAX_TOKENS={"planner": None, "generator": None, "evaluator": None},
        SESSION_MAX_TOKENS=None,
        available_backends={},
        backend=None,
        backend_spec=None,
        build_backend_spec=lambda *a, **k: None,
        build_session=lambda *a, **k: None,
        detect_backends=lambda *a, **k: None,
        harness_service=harness_service,
        normalize_session_mode=AppState.normalize_session_mode,
        normalize_session_role=AppState.normalize_session_role,
        project=SimpleNamespace(project_path=project_path),
        settings=SimpleNamespace(get=lambda *a, **k: ""),
    )


def test_the_dependency_on_the_host_stays_within_the_stated_surface():
    """A source-level guard on the seam itself.

    If a method reaches through `self._app` for something new, this fails and
    the addition has to be justified rather than absorbed silently.
    """
    import re
    import resonant_client.harness.prompts as module

    text = Path(module.__file__).read_text(encoding="utf-8")
    reached = {m.group(1) for m in re.finditer(r"self\._app\.([A-Za-z_]\w*)", text)}
    unexpected = reached - ALLOWED_APP_SURFACE

    assert not unexpected, (
        f"HarnessPrompts reached for {sorted(unexpected)} on its host. Either the "
        f"host surface genuinely needs to grow (update ALLOWED_APP_SURFACE and say "
        f"why) or that logic belongs on AppState."
    )


def test_prompts_work_against_a_stub_host(tmp_path: Path):
    """The payoff: a harness summary without constructing a GUI application."""
    service = SimpleNamespace(
        get_summary=lambda target: {"root": str(target), "spec_path": "spec.md"},
    )
    prompts = HarnessPrompts(_stub_app(str(tmp_path), harness_service=service))

    summary = prompts.get_harness_summary()

    assert summary["root"] == str(tmp_path)
    assert summary["spec_path"] == "spec.md"


def test_pure_text_helpers_need_no_host_at_all():
    """Most of the cluster is text munging that never touches the host."""
    prompts = HarnessPrompts(None)

    truncated = prompts._truncate_text("abcdef", max_chars=3)
    assert truncated.startswith("ab") and len(truncated) <= 3
    # Drops blanks; deliberately does not deduplicate.
    assert prompts._normalize_string_list(["a", "", "b", "a"]) == ["a", "b", "a"]
    assert prompts._strip_list_marker("- item") == "item"


def test_app_state_still_exposes_the_compatibility_surface():
    """settings.py, the WS registry, and the provider tests call these on
    AppState; the extraction must not have moved them out from under callers."""
    for name in ("harness_enabled", "get_harness_summary", "select_harness_backend"):
        assert callable(getattr(AppState, name)), name


def test_harness_prompts_is_lazy_and_cached():
    state = AppState.__new__(AppState)

    assert state._harness_prompts is None
    first = state.harness_prompts
    assert first is state.harness_prompts
    assert isinstance(first, HarnessPrompts)


def test_the_extracted_cluster_is_no_longer_on_app_state():
    """Guards against a partial move leaving two copies to drift apart."""
    for name in (
        "build_harness_generator_patch_prompt",
        "infer_generator_structured_payload",
        "precheck_harness_evaluator_payload",
        "_normalize_string_list",
    ):
        assert not hasattr(AppState, name), f"{name} should live on HarnessPrompts now"
        assert hasattr(HarnessPrompts, name), name


@pytest.mark.parametrize("name", sorted(ALLOWED_APP_SURFACE))
def test_a_real_app_state_provides_the_whole_surface(name):
    """The stub above is only meaningful if AppState really offers these.

    A fully constructed AppState, not `__new__`: most of the surface is set in
    `__init__`, so skipping it would test nothing. conftest redirects the test
    home, so construction has no effect on real user state.
    """
    state = AppState()
    assert hasattr(state, name), f"AppState is missing {name}"
