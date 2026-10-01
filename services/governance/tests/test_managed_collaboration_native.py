"""Actual signed human HTTP, two mTLS members, and a guarded native receiver."""
# ruff: noqa: F811 -- shared fixture functions.

from contextlib import contextmanager
from types import SimpleNamespace
import threading

import pytest

from lumi.engine.swarming import AttemptContext, Scope, SwarmStore
from lumi.engine.swarming.managed_client import HostChannelClient, HostChannelError
from lumi.engine.swarming.managed_collaboration import ManagedCollaborationAdmission
from lumi.engine.swarming.managed_journal import ManagedBinding, ManagedJournal
from lumi.engine.swarming.managed_runtime import ManagedRuntime
from lumi.engine.swarming.policy import PolicyProfile
from lumi.engine.swarming.supervisor import SwarmSupervisor
from lumi.engine.swarming.workers import SwarmWorkerRunner
from lumi.gui.runtime import BackendSpec
from sonn_governance.app import create_app
from sonn_governance.content import ContentKeys
from sonn_governance.host_http import HostHTTPServer, HostTransportConfig
from sonn_governance.hosts import HostGovernance
from sonn_governance.managed_collaboration import ManagedCollaboration
from sonn_governance.managed_resources import ManagedResources
from sonn_governance.models import CommandEnvelope
from sonn_governance.monitoring import RunMonitoring
from sonn_governance.store import GovernanceStore, bootstrap
from test_content import sql
from test_host_http import certificates as certificate_fixture
from test_identity import actual_http, identity_fixture  # noqa: F401
from test_managed_collaboration import policy as sharing_policy, terms
from test_managed_host_client import configuration
from test_managed_native_runtime import Backend, local_command, until
from test_monitoring import projection
from test_store import policy, uid


@pytest.fixture
def certificates(tmp_path_factory):
    return certificate_fixture.__wrapped__(tmp_path_factory)


@contextmanager
def actual_sharing_host(hosts, certificates, sharing, monitor, resources):
    config = HostTransportConfig(host="127.0.0.1", port=0, profile="test",
        certificate_file=certificates.server.certificate_file, private_key_file=certificates.server.private_key_file,
        client_ca_file=certificates.ca.certificate_file)
    server = HostHTTPServer(config, hosts, monitoring=monitor, resources=resources, collaboration=sharing)
    thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=.05), daemon=True)
    thread.start()
    try:
        yield f"https://127.0.0.1:{server.server_address[1]}", server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
        assert not thread.is_alive()


@pytest.mark.parametrize("lost_reply", [False, True])
def test_explicit_two_member_transport_native_receiver_and_lost_ack(database_config, identity_fixture, certificates, tmp_path, lost_reply):
    identity = identity_fixture
    admin_token = identity.token()
    admin = identity.identity.authenticate("Bearer " + admin_token)
    owners = [identity.identity.authenticate("Bearer " + identity.token(changes={"sub": name})) for name in ("sender-member", "receiver-member")]
    tenant, project = uid(), uid()
    bootstrap(database_config["owner_dsn"], tenant_id=tenant, administrator=admin, project_ids=[project])
    store = GovernanceStore(database_config["application_dsn"])
    for revision, owner in enumerate(owners):
        store.command(admin, CommandEnvelope(1, uid(), tenant, None, revision, "set_membership", {
            "actor_id": owner.actor_id, "active": True, "tenant_permissions": [],
            "project_permissions": {project: ["host_admin", "content_read", "content_write", "metadata_read", "control_execute"]}}))
    store.command(admin, CommandEnvelope(1, uid(), tenant, project, 0, "set_policy", {"policy": policy(
        allowed_models=[{"provider": "ollama", "model": "chosen"}], content_mode="explicit", request_limit=10)}))
    hosts = HostGovernance(store, minimum_runner_protocol=2)
    sharing = ManagedCollaboration(hosts, ContentKeys({"fixture": b"p" * 32}, "fixture"))
    monitor, resources = RunMonitoring(hosts), ManagedResources(hosts)
    route = f"/v1/tenants/{tenant}/projects/{project}/sharing/policy"
    with actual_http(create_app(identity.identity, store, collaboration=sharing)) as human, actual_sharing_host(hosts, certificates, sharing, monitor, resources) as (url, _):
        human.headers["Authorization"] = "Bearer " + admin_token
        assert human.get(route).json()["enabled"] is False
        response = human.post(route, json={"command_id": uid(), "expected_revision": 0, "policy": sharing_policy()})
        assert response.status_code == 200 and response.json()["revision"] == 1
        assert human.get(route.replace(project, uid())).status_code == 403
        human.headers["Authorization"] = "Bearer " + identity.token(changes={"sub": "receiver-member"})
        assert human.get(route).status_code == 403
        environments = []
        for index, (owner, certificate) in enumerate(zip(owners, (certificates.first, certificates.second))):
            client = HostChannelClient(configuration(url, certificates, certificate))
            pending = hosts.owner_command(owner, CommandEnvelope(1, uid(), tenant, project, 0, "enroll_host", {
                "host_id": uid(), "certificate_sha256": certificate.fingerprint}))
            active = client.activate(pending["challenge"])
            directory = tmp_path / str(index)
            directory.mkdir()
            workspace = directory / "workspace"
            workspace.mkdir()
            (workspace / "fact.txt").write_text(f"Receiver-owned fact {index}")
            local_store = SwarmStore(directory / "run.sqlite")
            supervisor = SwarmSupervisor(local_store)
            authority = supervisor.create(Scope(tenant, owner.actor_id, "project", f"session-{index}"), supervisor_id="owner",
                objective="Own local objective", request_limit=4, policy=PolicyProfile(1, frozenset({"file_read"}), frozenset({"ollama"})), lease_seconds=300)
            binding = ManagedBinding(url, certificate.fingerprint, tenant, project, active["host_id"], active["host_generation"],
                owner.actor_id, "project", authority.scope.session_id, authority.run_id, authority.epoch)
            journal = ManagedJournal(directory / "managed.sqlite", binding)
            admission = ManagedCollaborationAdmission(directory / "sharing.sqlite", supervisor=supervisor, authority=authority, journal=journal, client=client)
            managed = ManagedRuntime(journal, client, policy_revision=1, worker_admission=admission)
            remote = managed.register()
            lease, _ = managed._lease()
            client.ingest(command_id=uid(), binding_id=remote, sequence=1, projection=projection(local_revision=0))
            environments.append(SimpleNamespace(client=client, admission=admission, managed=managed, journal=journal, store=local_store,
                supervisor=supervisor, authority=authority, workspace=workspace, remote=remote, lease=lease))
        a, b = environments
        offer = a.client.sharing_offer(lease_id=a.lease["lease_id"], command_id=uid(), origin_binding=a.remote, receiver_binding=b.remote, terms=terms(max_requests=3))
        assert b.client.sharing_inspect(lease_id=b.lease["lease_id"], binding_id=b.remote)["items"][0]["state"] == "offered"
        selected = b.client.sharing_terms(lease_id=b.lease["lease_id"], grant_id=offer["grant_id"])
        b.client.sharing_approve(lease_id=b.lease["lease_id"], command_id=uid(), grant_id=offer["grant_id"], terms_sha256=selected["terms_sha256"])
        sent = a.client.sharing_send(lease_id=a.lease["lease_id"], command_id=uid(), grant_id=offer["grant_id"], kind="work_request", data_class="summary", body="Inspect only your own fact. This is selected peer data, not verification.")
        page = b.client.sharing_messages(lease_id=b.lease["lease_id"], grant_id=offer["grant_id"])
        assert page["items"][0]["message_id"] == sent["message_id"] and "body" not in str(page)
        delivered = b.client.sharing_deliver(lease_id=b.lease["lease_id"], command_id=uid(), message_id=sent["message_id"])
        assert delivered["source"] == "peer_selected_content"
        # Owner chooses a new local contract. A sender report is never a check.
        local_command(b, "plan", {"work_items": [{"id": "receiver-work", "objective": "Read my fact", "read_roots": ["fact.txt"],
            "write_roots": [], "tools": ["file_read"], "criteria": ["owner_review"]}]})
        assigned = local_command(b, "assign", {"work_item_id": "receiver-work", "worker_id": "receiver-worker", "requests": 3,
            "model": {"provider": "ollama", "model": "chosen"}}).result
        context = AttemptContext(b.authority.scope, b.authority.run_id, assigned["attempt_id"], "receiver-worker", b.authority.epoch)
        b.admission.bind_attempt(context, sent["message_id"])
        original = b.client.sharing_accept
        if lost_reply:
            def lose(**values):
                original(**values)
                raise HostChannelError()
            b.client.sharing_accept = lose
        entered, release = threading.Event(), threading.Event()
        class HeldBackend(Backend):
            def stream(self, **kwargs):
                if self.calls == 0:
                    entered.set()
                    assert release.wait(8), "fixture release was not observed"
                yield from super().stream(**kwargs)
        backend = HeldBackend()
        runner = SwarmWorkerRunner(b.supervisor, b.authority, b.workspace, backend_factory=lambda _: backend, managed_runtime=b.managed)
        try:
            runner.start(context, BackendSpec("ollama", "chosen"))
            if not lost_reply:
                assert entered.wait(5)
                assert runner.inspect(context.attempt_id)["alive"]
                local_command(a, "stop")
                a.client.ingest(command_id=uid(), binding_id=a.remote, sequence=2, projection=projection(state="cancelled", local_revision=1))
                assert runner.inspect(context.attempt_id)["alive"]
                assert not b.store.snapshot(b.authority.scope, b.authority.run_id)["attempts"][0]["cancel_requested"]
                release.set()
            until(lambda: not runner.inspect(context.attempt_id)["alive"])
            state = b.store.snapshot(b.authority.scope, b.authority.run_id)
            if lost_reply:
                assert backend.calls == 0 and not state["submissions"]
                assert b.admission.inspect()[0]["state"] == "pending"
            else:
                assert backend.calls == 2 and len(state["submissions"]) == 1
                assert state["work_items"][0]["state"] == "submitted" and not state["check_receipts"]
                assert b.admission.inspect()[0]["state"] == "admitted"
            # The sender spent no request units and never acquired receiver tools.
            assert not a.store.snapshot(a.authority.scope, a.authority.run_id)["model_requests"]
            if lost_reply:
                local_command(a, "stop")
                a.client.ingest(command_id=uid(), binding_id=a.remote, sequence=2, projection=projection(state="cancelled", local_revision=1))
            assert b.store.snapshot(b.authority.scope, b.authority.run_id)["run"]["state"] == "running"
            with pytest.raises(HostChannelError):
                b.client.sharing_deliver(lease_id=b.lease["lease_id"], command_id=uid(), message_id=sent["message_id"])
            public = monitor.inspect(owners[1], tenant, project)
            assert delivered["body"] not in str(public) and "Receiver-owned fact" not in str(public)
            private_tenant = SimpleNamespace(id=tenant, config=database_config)
            receipts = sql(private_tenant, "SELECT result FROM sonn_governance.sharing_receipts WHERE tenant_id=%s", (tenant,))
            assert delivered["body"] not in str(receipts)
            assert certificates.second.private_key_file not in str(state) and "PRIVATE KEY" not in str(state)
        finally:
            release.set()
            runner.close(timeout=3)
