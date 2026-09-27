"""An explicit desktop team reaches real managed TLS authority and retains scope."""
# ruff: noqa: F811 -- shared certificate fixture.

from dataclasses import replace
import hashlib
import os

import pytest

from lumi.engine.swarming import Scope
from lumi.engine.swarming.managed_client import HostChannelClient
from lumi.engine.swarming.managed_desktop import ManagedDesktop, ManagedDesktopConfig
from lumi.engine.swarming.models import Conflict, ScopeDenied
from lumi.engine.swarming.service import CapturedSession, SwarmRuntime, _READ_TOOLS
from lumi.gui.runtime import BackendSpec
from lumi.gui.settings import SettingsManager
from sonn_governance.hosts import HostGovernance
from sonn_governance.managed_resources import ManagedResources
from sonn_governance.models import CommandEnvelope
from sonn_governance.monitoring import RunMonitoring
from test_host_http import actual_host, certificates as certificate_fixture
from test_managed_host_client import configuration
from test_managed_native_runtime import Backend, until
from test_store import Tenant, uid


@pytest.fixture
def certificates(tmp_path_factory):
    return certificate_fixture.__wrapped__(tmp_path_factory)


def test_explicit_managed_team_never_converts_personal_scope_and_reports_only_metadata(database_config, certificates, tmp_path):
    tenant = Tenant(database_config)
    tenant.member(tenant.admin, tenant=["membership_admin", "policy_admin", "host_admin", "metadata_read", "control_execute"])
    tenant.store.command(tenant.admin, tenant.command(allowed_models=[{"provider": "ollama", "model": "chosen"}],
        allowed_tools=sorted(_READ_TOOLS), request_limit=10))
    hosts = HostGovernance(tenant.store, minimum_runner_protocol=2)
    monitoring = RunMonitoring(hosts)
    pending = hosts.owner_command(tenant.admin, CommandEnvelope(1, uid(), tenant.id, tenant.project, 0, "enroll_host",
        {"host_id": uid(), "certificate_sha256": certificates.first.fingerprint}))
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "fact.txt").write_text("Private desktop fixture evidence")
    project_id = hashlib.sha256(os.path.normcase(str(workspace.resolve())).encode()).hexdigest()
    personal = CapturedSession(Scope.personal("fixture-local-owner", project_id, "saved-session"), str(workspace), BackendSpec("ollama", "chosen"))
    settings = SettingsManager(tmp_path / "settings.json")
    settings.set("swarming", None, {"version": 1, "enabled": True})
    with actual_host(hosts, certificates, monitoring=monitoring, resources=ManagedResources(hosts)) as (url, _):
        transport = configuration(url, certificates, certificates.first)
        active = HostChannelClient(transport).activate(pending["challenge"])
        managed = ManagedDesktop(ManagedDesktopConfig(transport, tenant.id, tenant.project, active["host_id"], 1,
            tenant.admin.actor_id, personal.scope.owner_id, str(workspace), 1))
        service = SwarmRuntime(settings, backend_factory=lambda _: Backend(), state_root=lambda _: tmp_path / "state", managed_desktop=managed)
        try:
            view = service.operate(personal, {"action": "view", "request_id": uid()})
            assert view["execution_mode"] == "personal" and view["managed"]["available"] and view["run"] is None
            assert monitoring.inspect(tenant.admin, tenant.id, tenant.project)["runs"] == []
            capture = service.execution_capture(personal, "managed")
            assert capture.scope.tenant_id == tenant.id and capture.scope.owner_id == tenant.admin.actor_id
            assert personal.scope.tenant_id.startswith("personal:")
            message = {"action": "start", "request_id": uid(), "execution_mode": "managed", "objective": "Private local objective",
                       "tasks": [{"objective": "Read local fact", "read_roots": ["fact.txt"]}], "request_limit": 4, "max_workers": 1}
            with pytest.raises(ScopeDenied):
                service.operate(personal, message)
            started = service.operate(capture, message)
            run_id = started["run"]["run"]["id"]
            runner = service._runners[run_id][1]
            until(lambda: bool(runner.inspect_all()) and all(not item["alive"] for item in runner.inspect_all()))
            snapshot = service._store(capture).snapshot(capture.scope, run_id)
            assert len(snapshot["submissions"]) == 1
            assert len(snapshot["model_requests"]) == 2 and len(snapshot["action_receipts"]) == 1
            with pytest.raises(ScopeDenied):
                service.operate(personal, {"action": "view", "request_id": uid(), "run_id": run_id})
            assert service.operate(personal, {"action": "history", "request_id": uid()})["history"]["items"] == []
            def reported():
                runs = monitoring.inspect(tenant.admin, tenant.id, tenant.project)["runs"]
                return runs and runs[0]["projection"] and runs[0]["projection"]["counts"]["requests_known"] == 2
            until(reported)
            remote = monitoring.inspect(tenant.admin, tenant.id, tenant.project)
            assert "Private local objective" not in str(remote) and "Private desktop fixture evidence" not in str(remote)
            assert personal.scope.session_id not in str(remote) and str(workspace) not in str(remote)
            # The same request cannot mutate the captured mode or restart workers.
            with pytest.raises(ScopeDenied):
                service.operate(capture, {**message, "execution_mode": "personal"})
            again = service.operate(capture, message)
            assert again["run"]["run"]["id"] == run_id and len(runner.inspect_all()) == 1
            view = service.operate(capture, {"action": "view", "request_id": uid(), "run_id": run_id})
            assert view["managed"]["effective_policy"]["authenticated"]
            assert certificates.first.private_key_file not in str(view)
        finally:
            service.close()


def test_unconfigured_client_cannot_fall_back_to_personal_for_managed_intent(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    service = SwarmRuntime(SettingsManager(tmp_path / "settings.json"), backend_factory=lambda _: None, state_root=lambda _: tmp_path / "state")
    personal = CapturedSession(Scope.personal("owner", "project", "session"), str(workspace), BackendSpec("ollama", "chosen"))
    try:
        with pytest.raises(Conflict, match="operator configuration"):
            service.execution_capture(personal, "managed")
        with pytest.raises(ScopeDenied):
            service.operate(replace(personal, scope=replace(personal.scope, owner_id="forged")), {"request_id": uid()})
        assert not (tmp_path / "state").exists()
    finally:
        service.close()
