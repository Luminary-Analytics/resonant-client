"""Explicit managed desktop setup, protected files, and captured observer tests."""

from __future__ import annotations

from dataclasses import replace
import getpass
import hashlib
import json
import os
from types import SimpleNamespace
import subprocess
import threading
from uuid import uuid4

import pytest

from lumi.engine.swarming.managed_desktop import (
    ManagedDesktop, ManagedSetupError, _canonical_path, _protected_file, load_configuration, metadata_projection,
)
from lumi.engine.swarming.models import RunAuthority, Scope, ScopeDenied


class RuntimeFixture:
    def __init__(self, journal, client, *, policy_revision):
        self.journal, self.client, self.binding = journal, client, journal.binding
        self.calls = []
        self.contact = threading.Event()
        self.allow_contact = threading.Event()
        self.allow_contact.set()
        self.fail = False
        self.policy = {"authenticated": False, "valid": False, "policy": None, "policy_revision": None}

    def register(self):
        self.calls.append("register")
        self.contact.set()
        self.allow_contact.wait(5)
        if self.fail:
            raise RuntimeError("SECRET endpoint/key.pem PRIVATE PROVIDER ERROR")
        self.journal.bind_remote(str(uuid4()))
        self.policy = {"authenticated": True, "valid": True, "policy": {"request_limit": 10}, "policy_revision": 2}

    def report(self, key, projection):
        self.calls.append(("report", projection))
        self.journal.enqueue_report(key, projection)

    def poll_controls(self, runner):
        self.calls.append("poll")

    def flush(self, **kwargs):
        self.calls.append("flush")
        if self.fail:
            raise RuntimeError("PRIVATE KEY PATH")
        return {"delivered": 0, "unavailable": False}

    def policy_view(self):
        return self.policy


@pytest.fixture
def setup(tmp_path):
    if os.name == "nt":
        # Only this newly allocated fixture directory is changed. The app never
        # silently rewrites operator ACLs; it rejects broadly readable material.
        result = subprocess.run(["icacls", str(tmp_path), "/inheritance:r", "/grant:r",
                                 f"{getpass.getuser()}:(OI)(CI)(F)", "*S-1-5-18:(OI)(CI)(F)", "*S-1-5-32-544:(OI)(CI)(F)"],
                                capture_output=True, timeout=10, check=False)
        assert result.returncode == 0
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    protected = tmp_path / "operator"
    protected.mkdir()
    transport = {"endpoint": "https://governance.example", "certificate_sha256": "a" * 64}
    for field in ("ca_file", "certificate_file", "private_key_file"):
        source = protected / f"{field}.pem"
        source.write_text("PRIVATE TLS fixture", encoding="utf-8")
        source.chmod(0o600)
        transport[field] = str(source)
    document = {"version": 1, "transport": transport, "tenant_id": str(uuid4()), "project_id": str(uuid4()),
                "host_id": str(uuid4()), "host_generation": 1, "owner_id": "f" * 64, "local_owner_id": "local:fixture",
                "workspace": str(workspace), "policy_revision": 2}
    path = protected / "managed.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    path.chmod(0o600)
    config = load_configuration(str(path))
    desktop = ManagedDesktop(config, client_factory=lambda _: SimpleNamespace(), runtime_factory=RuntimeFixture)
    personal = Scope.personal("local:fixture", hashlib.sha256(_canonical_path(workspace).encode()).hexdigest(), "session")
    scope = desktop.scope(personal, str(workspace))
    authority = RunAuthority(scope, "new-run", "supervisor", 1)
    return desktop, authority, path, document, workspace, tmp_path / "state", personal


def snapshot(authority, *, state="running"):
    return {"run": {"id": authority.run_id, "epoch": authority.epoch, "revision": 3, "state": state,
                    **dict(zip(("tenant_id", "owner_id", "project_id", "session_id"), authority.scope.values(), strict=True))},
            "attempts": [{"kind": "worker", "process_state": "running"}], "model_requests": [{"state": "completed"}],
            "remaining_requests": 3, "integration_checks": [], "messages": [{"body": "PRIVATE PROMPT"}],
            "action_receipts": [{"arguments": "PRIVATE TOOL ARGS", "state": "completed"}]}


def test_configuration_and_attachment_are_network_free_and_personal_scope_unchanged(setup):
    desktop, authority, _, _, workspace, state_root, personal = setup
    assert authority.scope.tenant_id != personal.tenant_id
    assert personal.tenant_id == "personal:local:fixture"
    attachment = desktop.attach(authority, str(workspace), state_root)
    assert attachment.runtime.calls == []
    assert desktop.attach(authority, str(workspace), state_root) is attachment
    assert attachment.view()["effective_policy"]["policy"] is None
    assert desktop.configured_view()["effective_policy"] is None
    assert attachment.view()["connection"] == "not_contacted"
    encoded = json.dumps(attachment.view())
    assert "pem" not in encoded and "PRIVATE" not in encoded and str(workspace) not in encoded
    assert not (workspace / "swarm").exists()


def test_configured_scope_cannot_be_borrowed_by_other_local_owner_project_or_session(setup):
    desktop, authority, _, _, workspace, state_root, personal = setup
    with pytest.raises(ScopeDenied):
        desktop.scope(replace(personal, owner_id="other"), str(workspace))
    with pytest.raises(ScopeDenied):
        desktop.scope(replace(personal, project_id="other"), str(workspace))
    with pytest.raises(ScopeDenied):
        desktop.scope(authority.scope, str(workspace))
    with pytest.raises(ScopeDenied):
        desktop.attach(replace(authority, scope=personal), str(workspace), state_root)
    desktop.attach(authority, str(workspace), state_root)
    with pytest.raises(ScopeDenied, match="another captured session"):
        desktop.attach(replace(authority, scope=replace(authority.scope, session_id="other")), str(workspace), state_root)


def test_config_and_key_files_inside_workspace_cannot_enter_model_scope(setup):
    _, _, path, document, workspace, _, _ = setup
    unsafe = workspace / "managed.json"
    unsafe.write_text(path.read_text(), encoding="utf-8")
    unsafe.chmod(0o600)
    with pytest.raises(ManagedSetupError, match="outside"):
        load_configuration(str(unsafe))
    key = workspace / "private.pem"
    key.write_text("PRIVATE KEY", encoding="utf-8")
    key.chmod(0o600)
    document["transport"]["private_key_file"] = str(key)
    path.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(ManagedSetupError, match="outside"):
        load_configuration(str(path))


def test_config_rejects_unknown_fields_relative_paths_and_secrets_in_errors(setup):
    _, _, path, document, _, _, _ = setup
    for value in ({**document, "api_key": "SECRET"}, {**document, "version": True},
                  {**document, "transport": {**document["transport"], "endpoint": "https://user:SECRET@host"}}):
        path.write_text(json.dumps(value), encoding="utf-8")
        with pytest.raises(ManagedSetupError) as failure:
            load_configuration(str(path))
        assert "SECRET" not in str(failure.value) and str(path) not in str(failure.value)
    with pytest.raises(ManagedSetupError):
        load_configuration("managed.json")


@pytest.mark.skipif(os.name != "nt", reason="Windows native ACL fixture")
def test_windows_acl_rejects_other_users_reading_operator_private_material(setup):
    _, _, path, _, _, _, _ = setup
    _protected_file(str(path))
    changed = subprocess.run(["icacls", str(path), "/grant", "*S-1-1-0:(R)"], capture_output=True, timeout=10, check=False)
    assert changed.returncode == 0
    with pytest.raises(ManagedSetupError, match="restricted"):
        _protected_file(str(path))


@pytest.mark.skipif(os.name == "nt", reason="POSIX native file mode fixture")
def test_posix_permissions_reject_other_users_reading_operator_private_material(setup):
    _, _, path, _, _, _, _ = setup
    path.chmod(0o644)
    with pytest.raises(ManagedSetupError, match="restricted"):
        _protected_file(str(path))


def test_reopening_prior_managed_state_requires_explicit_recovery_without_network(setup):
    desktop, authority, _, _, workspace, state_root, _ = setup
    original = desktop.attach(authority, str(workspace), state_root)
    assert desktop.close()
    reopened = ManagedDesktop(desktop.config, client_factory=lambda _: SimpleNamespace(), runtime_factory=RuntimeFixture)
    with pytest.raises(ManagedSetupError, match="explicit recovery"):
        reopened.attach(authority, str(workspace), state_root)
    assert original.runtime.calls == []


def test_observer_projects_metadata_only_and_close_waits_honestly_for_admitted_network(setup):
    desktop, authority, _, _, workspace, state_root, _ = setup
    attachment = desktop.attach(authority, str(workspace), state_root)
    attachment.runtime.allow_contact.clear()
    runner = SimpleNamespace(authority=authority)
    attachment.start_pump(runner, lambda: snapshot(authority), interval_seconds=.05)
    assert attachment.runtime.contact.wait(2)
    assert attachment.close(timeout=.01) is False
    assert attachment.view()["observer_running"]
    attachment.runtime.allow_contact.set()
    assert attachment.close(timeout=2)
    assert attachment.runtime.calls == ["register"]
    with pytest.raises(ManagedSetupError, match="closed"):
        attachment.start_pump(runner, lambda: snapshot(authority))


def test_observer_failure_is_safe_and_local_stop_does_not_wait_for_network(setup):
    desktop, authority, _, _, workspace, state_root, _ = setup
    attachment = desktop.attach(authority, str(workspace), state_root)
    attachment.runtime.fail = True
    attachment.runtime.allow_contact.clear()
    stopped = threading.Event()
    runner = SimpleNamespace(authority=authority, stop=stopped.set)
    attachment.start_pump(runner, lambda: snapshot(authority), interval_seconds=.05)
    assert attachment.runtime.contact.wait(2)
    runner.stop()
    assert stopped.is_set()  # Observer has no local runner control lock.
    attachment.runtime.allow_contact.set()
    assert attachment.close(timeout=2)
    view = attachment.view()
    assert view["connection"] == "unavailable"
    assert "PRIVATE" not in json.dumps(view) and "pem" not in json.dumps(view)


def test_observer_never_renews_historical_authority_or_uses_foreign_snapshot(setup):
    desktop, authority, _, _, workspace, state_root, _ = setup
    attachment = desktop.attach(authority, str(workspace), state_root)
    read = threading.Event()
    def old_snapshot():
        read.set()
        return snapshot(authority, state="recovery_required")
    attachment.start_pump(SimpleNamespace(authority=authority), old_snapshot, interval_seconds=.05)
    assert read.wait(2)
    assert attachment.close(timeout=2)
    assert attachment.runtime.calls == []
    with pytest.raises(ScopeDenied):
        attachment.start_pump(SimpleNamespace(authority=replace(authority, epoch=2)), old_snapshot)


def test_projection_uses_counters_and_keeps_unknown_processes_and_effects_visible(setup):
    _, authority, _, _, _, _, _ = setup
    source = snapshot(authority)
    source["model_requests"].append({"state": "uncertain", "prompt": "SECRET"})
    result = metadata_projection(source)
    assert result["counts"]["requests_known"] == 1
    assert result["counts"]["requests_unknown"] == 1
    assert result["counts"]["requests_held"] == 1
    assert result["alert"] == "uncertain_effect"
    assert "PRIVATE" not in json.dumps(result) and "SECRET" not in json.dumps(result)
    with pytest.raises(ManagedSetupError, match="outside"):
        setup[0].attach(authority, str(setup[4]), setup[4] / "state")


def test_existing_history_is_scoped_and_opens_without_renewal_or_fencing(setup):
    desktop, authority, _, _, workspace, state_root, _ = setup
    attachment = desktop.attach(authority, str(workspace), state_root)
    opened = desktop.open_history(authority.scope, authority.run_id, authority.epoch, str(workspace), state_root)
    assert opened.path == attachment.runtime.journal.path
    assert opened.instance_id != attachment.runtime.journal.instance_id
    assert attachment.runtime.calls == []
    assert opened.recovery_summary()["pending_observations"] == 0
    with pytest.raises(ManagedSetupError, match="unavailable"):
        desktop.open_history(authority.scope, "absent-run", authority.epoch, str(workspace), state_root)
    with pytest.raises(ScopeDenied):
        desktop.open_history(replace(authority.scope, owner_id="a" * 64), authority.run_id, authority.epoch, str(workspace), state_root)


def test_effect_coverage_is_cached_without_network_and_cannot_switch_native_store(setup):
    from lumi.engine.swarming.store import SwarmStore
    desktop, authority, _, _, workspace, state_root, _ = setup
    attachment = desktop.attach(authority, str(workspace), state_root)
    store = SwarmStore(state_root / "swarm.sqlite")
    effects = attachment.effects(store)
    assert attachment.effects(store) is effects and effects.path.is_file()
    assert attachment.runtime.calls == [] and attachment.view()["owner_effects"]["resume_ready"]
    with pytest.raises(ScopeDenied):
        attachment.effects(SwarmStore(state_root / "different.sqlite"))
    assert attachment.close()
    with pytest.raises(ManagedSetupError, match="closed"):
        attachment.effects(store)


def test_effect_cleanup_delivery_continues_when_ordinary_authority_contact_fails(setup, monkeypatch):
    from lumi.engine.swarming.store import SwarmStore
    desktop, authority, _, _, workspace, state_root, _ = setup
    attachment = desktop.attach(authority, str(workspace), state_root)
    effects = attachment.effects(SwarmStore(state_root / "swarm.sqlite"))
    observed = threading.Event()
    def flush(**kwargs):
        assert not attachment._lock.locked()
        observed.set()
        return {"delivered": 1, "unavailable": False}
    monkeypatch.setattr(effects, "flush", flush)
    attachment.runtime.fail = True
    attachment.start_pump(SimpleNamespace(authority=authority), lambda: snapshot(authority), interval_seconds=.05)
    assert observed.wait(2)
    assert attachment.close(timeout=2)
    assert attachment.view()["connection"] == "unavailable"
