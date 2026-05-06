"""Tests for v0.6.1a1 — autonomous_factory wires skills hooks.

The v0.6.0 GA shipped the hook plumbing in DaemonHooks but no
production wiring. v0.6.1a1 connects `extract_skill_hook` and
`queue_curation_hook` to the actual extractor + curator inside
`build_autonomous_mission_hooks`.

These tests verify the wiring shape:
- Default flags → hooks are non-None and callable.
- Disabled flags → hooks are None.
- Calling the extract hook spawns a daemon thread (doesn't block).
- Calling the queue_curation hook spawns a daemon thread (doesn't block).
- Rate-limit check guards curator spawn.
- Bad kwargs to the extract hook are logged + skipped (no raise).

We don't drive the actual extractor / curator end-to-end here —
those modules already have unit tests. This file is the
factory-plumbing test.
"""
from __future__ import annotations

import threading
import time
from unittest.mock import patch

import pytest

from resonant_client.gui.autonomous_factory import (
    DispatchTracker,
    build_autonomous_mission_hooks,
)


# ── Stubs for IntentService / backend so the factory builds without I/O


class _StubIntentService:
    def start_intent(self, *a, **kw):
        return "intent-1"

    def cancel(self, intent_id):
        pass


class _StubBackend:
    name = "ollama"
    model = "deepseek-v4-flash:cloud"
    base_url = "http://test"
    api_key = None
    tool_mode = "native"

    def stream(self, **kw):
        # Never called in these tests; the factory just holds the ref.
        if False:
            yield


@pytest.fixture
def state_home(tmp_path, monkeypatch):
    home = tmp_path / "state-home"
    home.mkdir()
    monkeypatch.setenv("RESONANT_STATE_HOME", str(home))
    return home


@pytest.fixture
def project_dir(tmp_path):
    project = tmp_path / "fakeproj"
    project.mkdir()
    return project


def _build(project_path, **kwargs):
    """Helper: build hooks with sane defaults for whatever's not under test."""
    defaults = dict(
        intent_service=_StubIntentService(),
        dispatch_tracker=DispatchTracker(),
        project_path=str(project_path),
        backend=_StubBackend(),
        project_instructions="",
        settings=None,
        roadmap_path=str(project_path / "roadmap.md"),
        daemon_stop_event=threading.Event(),
    )
    defaults.update(kwargs)
    return build_autonomous_mission_hooks(**defaults)


# ── Default wiring ────────────────────────────────────────────────────


class TestDefaultWiring:
    def test_extract_hook_wired_by_default(self, state_home, project_dir):
        hooks = _build(project_dir)
        assert hooks.extract_skill_hook is not None
        assert callable(hooks.extract_skill_hook)

    def test_curator_hook_wired_by_default(self, state_home, project_dir):
        hooks = _build(project_dir)
        assert hooks.queue_curation_hook is not None
        assert callable(hooks.queue_curation_hook)


# ── Disable flags ─────────────────────────────────────────────────────


class TestDisableFlags:
    def test_extract_hook_None_when_disabled(self, state_home, project_dir):
        hooks = _build(project_dir, enable_skill_extraction=False)
        assert hooks.extract_skill_hook is None

    def test_curator_hook_None_when_disabled(self, state_home, project_dir):
        hooks = _build(project_dir, enable_skill_curator=False)
        assert hooks.queue_curation_hook is None

    def test_both_disabled_no_hooks(self, state_home, project_dir):
        hooks = _build(
            project_dir,
            enable_skill_extraction=False,
            enable_skill_curator=False,
        )
        assert hooks.extract_skill_hook is None
        assert hooks.queue_curation_hook is None


# ── Extract hook fires in background thread ───────────────────────────


class TestExtractHookSpawnsThread:
    def test_calling_extract_hook_does_not_block(self, state_home, project_dir):
        hooks = _build(project_dir)
        # Patch extract_skill_from_iter to record the call from
        # whatever thread it ends up on. Sleep briefly to confirm
        # the call happens in a different thread.
        called_on = []

        def slow_extractor(ctx, *, backend):
            called_on.append(threading.current_thread().name)
            time.sleep(0.1)

        with patch(
            "resonant_client.orchestration.skill_mission_extraction.extract_skill_from_iter",
            slow_extractor,
        ):
            t0 = time.time()
            hooks.extract_skill_hook(
                roadmap_item_title="t",
                roadmap_item_description="d",
                iter_count=1,
                intent_id="i",
                project_path=str(project_dir),
                outcome_verdict="satisfied",
                outcome_summary="ok",
            )
            elapsed = time.time() - t0

        # Hook returned in well under the 0.1s the extractor sleeps —
        # confirms it ran in a background thread.
        assert elapsed < 0.05

        # Wait briefly for the thread to actually run.
        deadline = time.time() + 1.0
        while time.time() < deadline and not called_on:
            time.sleep(0.01)
        assert called_on, "extractor never ran in the background thread"
        assert called_on[0].startswith("skill-extractor-")

    def test_extract_hook_swallows_typeerror_from_bad_kwargs(self, state_home, project_dir):
        hooks = _build(project_dir)
        # Hook is forgiving: bad kwargs (e.g. signature drift) get
        # logged + skipped, not raised. Tests pass an unknown kwarg
        # to verify.
        # No exception expected.
        hooks.extract_skill_hook(
            roadmap_item_title="t",
            this_kwarg_does_not_exist=42,
        )


# ── Curator hook fires in background thread ──────────────────────────


class TestCuratorHookSpawnsThread:
    def test_calling_curator_hook_does_not_block(self, state_home, project_dir):
        hooks = _build(project_dir)
        called_on = []

        def slow_curator(project_path, **kwargs):
            called_on.append(threading.current_thread().name)
            time.sleep(0.1)

        with patch(
            "resonant_client.orchestration.skill_curator.run_curation",
            slow_curator,
        ):
            t0 = time.time()
            hooks.queue_curation_hook(str(project_dir))
            elapsed = time.time() - t0

        # Should return immediately.
        assert elapsed < 0.05

        # Background thread runs.
        deadline = time.time() + 1.0
        while time.time() < deadline and not called_on:
            time.sleep(0.01)
        assert called_on, "curator never ran in the background thread"
        assert called_on[0].startswith("skill-curator-")

    def test_curator_hook_respects_rate_limit(self, state_home, project_dir):
        hooks = _build(project_dir)
        run_calls = []

        def fake_run(project_path, **kwargs):
            run_calls.append(project_path)

        # Patch the rate-limit check to return False (just ran recently).
        with patch(
            "resonant_client.orchestration.skill_curator.should_run_curation",
            return_value=False,
        ), patch(
            "resonant_client.orchestration.skill_curator.run_curation",
            fake_run,
        ):
            hooks.queue_curation_hook(str(project_dir))
            time.sleep(0.05)  # let any thread run if it spawned

        # No run_curation call — rate limiter blocked it.
        assert run_calls == []

    def test_curator_hook_swallows_should_run_check_exception(
        self, state_home, project_dir,
    ):
        hooks = _build(project_dir)
        # If should_run_curation raises (corrupt state file etc.),
        # the hook logs + skips rather than crashing the daemon.
        with patch(
            "resonant_client.orchestration.skill_curator.should_run_curation",
            side_effect=RuntimeError("corrupt state"),
        ):
            # Should NOT raise.
            hooks.queue_curation_hook(str(project_dir))


# ── Together with the daemon ──────────────────────────────────────────


class TestHooksReachable:
    """End-to-end shape: hooks survive being passed into a real
    DaemonHooks construction and are accessible via attribute access.
    Defends against accidental dropping of the new fields when
    DaemonHooks is mutated."""

    def test_factory_output_carries_both_hooks(self, state_home, project_dir):
        hooks = _build(project_dir)
        # Both attributes exist on the returned object.
        assert hasattr(hooks, "extract_skill_hook")
        assert hasattr(hooks, "queue_curation_hook")
        # Both have been set to callables (default-enabled).
        assert callable(hooks.extract_skill_hook)
        assert callable(hooks.queue_curation_hook)

    def test_existing_hook_fields_unchanged(self, state_home, project_dir):
        # Sanity: the new wiring didn't accidentally break the
        # existing hook fields.
        hooks = _build(project_dir)
        assert callable(hooks.dispatch_item)
        assert callable(hooks.wait_for_dispatch)
        assert callable(hooks.cancel_dispatch)
        assert callable(hooks.get_commit_sha)
        assert callable(hooks.validate_sha)
        assert callable(hooks.run_full_reflect)
        assert callable(hooks.check_context_factory)
