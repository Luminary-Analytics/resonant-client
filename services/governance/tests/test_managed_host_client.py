"""Private native host client uses actual mTLS and current PostgreSQL authority."""
# ruff: noqa: F811 -- shared pytest certificate fixture.

from dataclasses import replace
import uuid

import pytest

from lumi.engine.swarming.managed_client import HostChannelClient, HostChannelConfig, HostChannelError
from sonn_governance.hosts import HostGovernance
from sonn_governance.managed_resources import ManagedResources
from sonn_governance.models import CommandEnvelope
from sonn_governance.monitoring import RunMonitoring
from test_host_http import actual_host, certificates  # noqa: F401
from test_store import Tenant


def uid():
    return str(uuid.uuid4())


def configuration(url, certificates, certificate):
    return HostChannelConfig(url, certificates.ca.certificate_file, certificate.certificate_file,
        certificate.private_key_file, certificate.fingerprint, profile="test", allow_test_loopback=True)


def test_native_host_clients_share_request_cap_and_replay_never_dispatches(database_config, certificates):
    tenant = Tenant(database_config)
    tenant.member(tenant.admin, tenant=["membership_admin", "policy_admin", "host_admin", "metadata_read"])
    tenant.store.command(tenant.admin, tenant.command(request_limit=2))
    hosts = HostGovernance(tenant.store)
    pending = [hosts.owner_command(tenant.admin, CommandEnvelope(1, uid(), tenant.id, tenant.project, 0, "enroll_host", {
        "host_id": uid(), "certificate_sha256": certificate.fingerprint})) for certificate in (certificates.first, certificates.second)]
    with actual_host(hosts, certificates, monitoring=RunMonitoring(hosts), resources=ManagedResources(hosts)) as (url, _):
        first = HostChannelClient(configuration(url, certificates, certificates.first))
        second = HostChannelClient(configuration(url, certificates, certificates.second))
        active = first.activate(pending[0]["challenge"])
        second.activate(pending[1]["challenge"])
        leases = [client.lease(uid(), 1) for client in (first, second)]
        requests = [uid(), uid()]
        for client, lease, request in zip((first, second), leases, requests):
            binding = client.register(command_id=uid(), lease_id=lease["lease_id"], local_run_id=uid(), session_id=uid())
            worker_id = uid()
            client.reserve_worker(lease_id=lease["lease_id"], binding_id=binding["binding_id"], worker_id=worker_id, kind="worker")
            assert client.reserve(lease["lease_id"], request)["state"] == "reserved"
            client.bind_request(lease_id=lease["lease_id"], worker_id=worker_id, request_id=request,
                purpose="primary", model={"provider": "ollama", "model": "fixture"}, input_sha256="a" * 64)
            assert client.start(request)["dispatch_permitted"] is True
            assert client.start(request)["dispatch_permitted"] is False
        with pytest.raises(HostChannelError) as denied:
            second.reserve(leases[1]["lease_id"], uid())
        assert denied.value.status == 409 and denied.value.delivery_unknown is False
        with pytest.raises(HostChannelError) as foreign:
            second.start(requests[0])
        assert foreign.value.status == 403
        hosts.owner_command(tenant.admin, CommandEnvelope(1, uid(), tenant.id, tenant.project, active["revision"],
            "revoke_host", {"host_id": active["host_id"]}))
        with pytest.raises(HostChannelError):
            first.lease(uid(), 1)
        assert first.settle(requests[0], "uncertain")["held_units"] == 1
        assert second.settle(requests[1], "completed")["held_units"] == 0


def test_transport_lost_reply_is_generic_unknown_and_never_retried(certificates, caplog):
    calls = []
    class Governance:
        def start(self, principal, request_id):
            calls.append(request_id)
            raise RuntimeError("synthetic-private-provider-secret")
    with actual_host(Governance(), certificates) as (url, _):
        client = HostChannelClient(configuration(url, certificates, certificates.first))
        with pytest.raises(HostChannelError, match="^Managed host operation unavailable$") as error:
            client.start(uid())
        assert error.value.delivery_unknown is True and len(calls) == 1
    assert "synthetic-private" not in caplog.text


def test_certificate_pin_and_wrong_ca_fail_before_authorized_operation(certificates):
    class Governance:
        def activate(self, *args, **kwargs):
            pytest.fail("Unauthenticated client reached operation")
    with actual_host(Governance(), certificates) as (url, _):
        config = configuration(url, certificates, certificates.first)
        assert config.private_key_file not in repr(config)
        with pytest.raises(HostChannelError) as error:
            HostChannelClient(replace(config, certificate_sha256="0" * 64))
        assert error.value.delivery_unknown is False
        wrong = HostChannelClient(configuration(url, certificates, certificates.foreign))
        with pytest.raises(HostChannelError):
            wrong.activate("fixture")


def test_managed_configuration_cannot_follow_untrusted_endpoints(certificates):
    base = configuration("https://127.0.0.1:19991", certificates, certificates.first)
    for changes in ({"endpoint": "http://127.0.0.1:19991"}, {"endpoint": "https://private@127.0.0.1"},
                    {"endpoint": "https://127.0.0.1/path"}, {"endpoint": "https://127.0.0.1?secret=private"},
                    {"profile": "production"}, {"allow_test_loopback": False}):
        with pytest.raises(ValueError):
            replace(base, **changes)
