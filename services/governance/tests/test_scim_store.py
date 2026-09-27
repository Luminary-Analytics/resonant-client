"""SCIM fixture provisioning uses real PostgreSQL and explicit human mappings."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import json
import threading
import time

import psycopg
import pytest

from sonn_governance.hosts import HostGovernance
from sonn_governance.models import AccessDenied, CommandEnvelope, InvalidRequest, Principal, Query
from sonn_governance.scim import (
    GROUP_SCHEMA, IDENTITY_SCHEMA, PATCH_SCHEMA, USER_SCHEMA, ScimError, ScimStore,
    register_provisioner, revoke_provisioner,
)
from test_store import Tenant, uid
from test_hosts import enroll, lease


@pytest.fixture
def setup(database_config):
    tenant = Tenant(database_config)
    scim = ScimStore(tenant.store)
    credential = register_provisioner(database_config["owner_dsn"], tenant_id=tenant.id,
                                     issuer=tenant.admin.issuer, expires_at=time.time() + 3600)
    principal = scim.authenticate("Bearer " + credential["token"])
    return tenant, scim, principal, credential


def user(*, external=None, subject=None, name=None, active=True):
    return {"schemas": [USER_SCHEMA, IDENTITY_SCHEMA], "externalId": external or uid(),
            "userName": name or (uid() + "@example.invalid"), "active": active,
            IDENTITY_SCHEMA: {"subject": subject or uid()}}


def group(*members):
    return {"schemas": [GROUP_SCHEMA], "externalId": uid(), "displayName": "Fixture group",
            "members": [{"value": member["id"]} for member in members]}


def patch(path, value=None, op="replace"):
    operation = {"op": op, "path": path}
    if op != "remove":
        operation["value"] = value
    return {"schemas": [PATCH_SCHEMA], "Operations": [operation]}


def revision(tenant):
    with psycopg.connect(tenant.config["owner_dsn"]) as connection:
        tenant.revision = connection.execute("SELECT authorization_revision FROM sonn_governance.tenants WHERE tenant_id=%s", (tenant.id,)).fetchone()[0]
    return tenant.revision


def mapping(tenant, scim, resource, permissions=("metadata_read",)):
    command = CommandEnvelope(1, uid(), tenant.id, None, revision(tenant), "map_scim_group", {
        "group_id": resource["id"], "tenant_permissions": [], "project_permissions": {tenant.project: list(permissions)},
    })
    return scim.map_group(tenant.admin, command)


def human(tenant, resource):
    return Principal(tenant.admin.issuer, resource[IDENTITY_SCHEMA]["subject"], time.time() + 3600)


def metadata(tenant, resource):
    return tenant.store.query(human(tenant, resource), Query(tenant.id, tenant.project, "metadata"))


def test_explicit_subject_and_human_mapping_are_required(setup):
    tenant, scim, principal, _ = setup
    person = scim.create(principal, "Users", user(name=tenant.admin.subject, external=tenant.admin.subject))
    with pytest.raises(AccessDenied):
        metadata(tenant, person)
    team = scim.create(principal, "Groups", group(person))
    mapping(tenant, scim, team)
    assert metadata(tenant, person)["project_id"] == tenant.project
    original_actor = human(tenant, person).actor_id
    renamed = scim.patch(principal, "Users", person["id"], patch("userName", "renamed@example.invalid"), person["meta"]["version"])
    assert human(tenant, renamed).actor_id == original_actor
    assert metadata(tenant, renamed)["project_id"] == tenant.project


def test_external_id_subject_and_duplicate_username_are_protected(setup):
    _, scim, principal, _ = setup
    document = user(name="Alice@example.invalid")
    created = scim.create(principal, "Users", document)
    for changed in ({**document, "externalId": uid()},
                    {**document, IDENTITY_SCHEMA: {"subject": uid()}}):
        with pytest.raises(ScimError) as failure:
            scim.replace(principal, "Users", created["id"], changed, created["meta"]["version"])
        assert failure.value.scim_type == "mutability"
    for duplicate in (document, user(name="ALICE@EXAMPLE.INVALID")):
        with pytest.raises(ScimError) as failure:
            scim.create(principal, "Users", duplicate)
        assert failure.value.status == 409
    with pytest.raises(ScimError):
        scim.create(principal, "Users", {**user(), "roles": ["membership_admin"]})


def test_conditional_patch_is_atomic_and_racing_versions_have_one_winner(setup):
    _, scim, principal, _ = setup
    person = scim.create(principal, "Users", user())
    version = person["meta"]["version"]
    document = patch("active", False)
    document["Operations"].append({"op": "replace", "path": "roles", "value": ["content_read"]})
    with pytest.raises(ScimError):
        scim.patch(principal, "Users", person["id"], document, version)
    assert scim.get(principal, "Users", person["id"])["active"] is True
    barrier = threading.Barrier(2)

    def update(name):
        barrier.wait(5)
        try:
            return scim.patch(principal, "Users", person["id"], patch("userName", name), version)
        except ScimError as error:
            assert error.status == 412
            return None

    with ThreadPoolExecutor(max_workers=2) as executor:
        result = list(executor.map(update, ("first", "second")))
    assert sum(item is not None for item in result) == 1
    with pytest.raises(ScimError) as failure:
        scim.patch(principal, "Users", person["id"], patch("active", False), None)
    assert failure.value.status == 428


def test_pagination_filter_and_readonly_fields(setup):
    _, scim, principal, _ = setup
    people = [scim.create(principal, "Users", user(name=name)) for name in ("Zeta", "Alpha", "Émilie")]
    result = scim.list(principal, "Users", start_index=2, count=1)
    assert result["totalResults"] == 3 and result["itemsPerPage"] == 1 and result["startIndex"] == 2
    assert scim.list(principal, "Users", count=0)["Resources"] == []
    found = scim.list(principal, "Users", filter='userName eq "ÉMILIE"')
    assert found["Resources"][0]["id"] == people[2]["id"]
    exact = scim.list(principal, "Users", filter="externalId eq " + json.dumps(people[0]["externalId"]))
    assert exact["totalResults"] == 1
    for invalid in ('userName co "a"', 'active eq true'):
        with pytest.raises(ScimError) as failure:
            scim.list(principal, "Users", filter=invalid)
        assert failure.value.scim_type == "invalidFilter"
    replaced = scim.replace(principal, "Users", people[0]["id"], {**people[0], "id": uid()}, people[0]["meta"]["version"])
    assert replaced["id"] == people[0]["id"]


def test_idp_suspension_and_local_suspension_are_independent(setup):
    tenant, scim, principal, _ = setup
    person = scim.create(principal, "Users", user())
    team = scim.create(principal, "Groups", group(person))
    mapping(tenant, scim, team)
    assert metadata(tenant, person)
    paused = scim.patch(principal, "Users", person["id"], patch("active", False), person["meta"]["version"])
    with pytest.raises(AccessDenied):
        metadata(tenant, paused)
    revision(tenant)
    tenant.member(human(tenant, person), projects={tenant.project: ["metadata_read"]}, active=True)
    with pytest.raises(AccessDenied):
        metadata(tenant, paused)
    tenant.member(human(tenant, person), projects={tenant.project: ["metadata_read"]}, active=False)
    reactivated = scim.patch(principal, "Users", person["id"], patch("active", True), paused["meta"]["version"])
    with pytest.raises(AccessDenied):
        metadata(tenant, reactivated)
    revision(tenant)
    tenant.member(human(tenant, person), projects={tenant.project: ["metadata_read"]}, active=True)
    assert metadata(tenant, reactivated)


def test_group_sources_are_distinct_and_manual_grants_survive_removal(setup):
    tenant, scim, principal, _ = setup
    person = scim.create(principal, "Users", user())
    first, second = [scim.create(principal, "Groups", group(person)) for _ in range(2)]
    mapping(tenant, scim, first)
    mapping(tenant, scim, second)
    scim.patch(principal, "Groups", first["id"], patch(f'members[value eq "{person["id"]}"]', op="remove"), first["meta"]["version"])
    assert metadata(tenant, person)
    scim.delete(principal, "Groups", second["id"], second["meta"]["version"])
    with pytest.raises(AccessDenied):
        metadata(tenant, person)
    revision(tenant)
    tenant.member(human(tenant, person), projects={tenant.project: ["metadata_read"]})
    scim.delete(principal, "Groups", first["id"], 'W/"2"')
    assert metadata(tenant, person)


def test_sensitive_or_admin_group_mapping_is_denied(setup):
    tenant, scim, principal, _ = setup
    person = scim.create(principal, "Users", user(subject=tenant.admin.subject))
    team = scim.create(principal, "Groups", group(person))
    for permission in ("content_read", "content_write", "retention_admin", "membership_admin", "membership_approve", "policy_admin"):
        with pytest.raises(InvalidRequest):
            mapping(tenant, scim, team, [permission])


def test_credential_current_revocation_and_cross_tenant_isolation(setup, database_config):
    tenant, scim, principal, credential = setup
    person = scim.create(principal, "Users", user())
    other = Tenant(database_config)
    second = register_provisioner(database_config["owner_dsn"], tenant_id=other.id,
                                  issuer=other.admin.issuer, expires_at=time.time() + 300)
    foreign = scim.authenticate("Bearer " + second["token"])
    with pytest.raises(ScimError) as failure:
        scim.get(foreign, "Users", person["id"])
    assert failure.value.status == 404
    assert scim.list(foreign, "Users")["totalResults"] == 0
    with pytest.raises(AccessDenied):
        scim.list(replace(principal, tenant_id=other.id), "Users")
    with pytest.raises(AccessDenied):
        scim.list(tenant.admin, "Users")
    revoke_provisioner(database_config["owner_dsn"], credential_id=credential["credential_id"])
    with pytest.raises(AccessDenied):
        scim.authenticate("Bearer " + credential["token"])
    with pytest.raises(AccessDenied):
        scim.get(principal, "Users", person["id"])


def test_same_external_id_in_different_issuer_is_not_same_identity(setup):
    tenant, scim, principal, _ = setup
    document = user()
    first = scim.create(principal, "Users", document)
    credential = register_provisioner(tenant.config["owner_dsn"], tenant_id=tenant.id,
                                      issuer="https://different.example", expires_at=time.time() + 300)
    different = scim.authenticate("Bearer " + credential["token"])
    second = scim.create(different, "Users", document)
    assert first["id"] != second["id"]
    with pytest.raises(ScimError):
        scim.create(principal, "Groups", group(second))


def test_delete_preserves_external_identity_and_denies_online_member(setup):
    tenant, scim, principal, _ = setup
    document = user()
    person = scim.create(principal, "Users", document)
    team = scim.create(principal, "Groups", group(person))
    mapping(tenant, scim, team)
    scim.delete(principal, "Users", person["id"], person["meta"]["version"])
    assert scim.list(principal, "Users")["totalResults"] == 0
    with pytest.raises(ScimError) as failure:
        scim.get(principal, "Users", person["id"])
    assert failure.value.status == 404
    with pytest.raises(ScimError) as failure:
        scim.create(principal, "Users", document)
    assert failure.value.status == 409
    with pytest.raises(AccessDenied):
        metadata(tenant, person)


def test_audit_failure_rolls_back_user_and_membership_projection(setup, monkeypatch):
    tenant, scim, principal, _ = setup
    original = tenant.store._audit

    def fail(*args, **kwargs):
        raise RuntimeError("audit unavailable")

    document = user()
    monkeypatch.setattr(tenant.store, "_audit", fail)
    with pytest.raises(RuntimeError):
        scim.create(principal, "Users", document)
    monkeypatch.setattr(tenant.store, "_audit", original)
    assert scim.list(principal, "Users")["totalResults"] == 0
    assert scim.create(principal, "Users", document)["meta"]["version"] == 'W/"1"'


def test_group_host_role_removal_fences_existing_host_lease(setup):
    tenant, scim, principal, _ = setup
    tenant.store.command(tenant.admin, tenant.command())
    person = scim.create(principal, "Users", user())
    team = scim.create(principal, "Groups", group(person))
    mapping(tenant, scim, team, ["host_admin"])
    original = tenant.admin
    tenant.admin = human(tenant, person)
    hosts = HostGovernance(tenant.store)
    host, _, _ = enroll(tenant, hosts)
    assert lease(hosts, host)
    tenant.admin = original
    scim.patch(principal, "Groups", team["id"], patch("members", op="remove"), team["meta"]["version"])
    with pytest.raises(AccessDenied):
        lease(hosts, host)


def test_user_deletion_changes_affected_group_etag(setup):
    _, scim, principal, _ = setup
    person = scim.create(principal, "Users", user())
    team = scim.create(principal, "Groups", group(person))
    scim.delete(principal, "Users", person["id"], person["meta"]["version"])
    changed = scim.get(principal, "Groups", team["id"])
    assert changed["members"] == [] and changed["meta"]["version"] == 'W/"2"'
    with pytest.raises(ScimError) as failure:
        scim.patch(principal, "Groups", team["id"], patch("displayName", "Changed"), team["meta"]["version"])
    assert failure.value.status == 412


def test_member_append_remove_and_unknown_path_rollback(setup):
    _, scim, principal, _ = setup
    first, second = [scim.create(principal, "Users", user()) for _ in range(2)]
    team = scim.create(principal, "Groups", group(first))
    updated = scim.patch(principal, "Groups", team["id"], patch("members", [{"value": second["id"]}], "add"), team["meta"]["version"])
    assert {row["value"] for row in updated["members"]} == {first["id"], second["id"]}
    removed = scim.patch(principal, "Groups", team["id"], patch(f'members[value eq "{first["id"]}"]', op="remove"), updated["meta"]["version"])
    assert removed["members"] == [{"value": second["id"], "type": "User"}]
    with pytest.raises(ScimError) as failure:
        scim.patch(principal, "Groups", team["id"], patch(f'members[value eq "{first["id"]}"]', op="remove"), removed["meta"]["version"])
    assert failure.value.scim_type == "noTarget"


def test_mapping_replay_rechecks_current_human_authority(setup):
    tenant, scim, principal, _ = setup
    team = scim.create(principal, "Groups", group())
    revision(tenant)
    tenant.member(tenant.reader, tenant=["membership_admin"])
    command = CommandEnvelope(1, uid(), tenant.id, None, tenant.revision, "map_scim_group", {
        "group_id": team["id"], "tenant_permissions": [], "project_permissions": {tenant.project: ["metadata_read"]},
    })
    original = scim.map_group(tenant.reader, command)
    assert scim.map_group(tenant.reader, command) == original
    revision(tenant)
    tenant.member(tenant.reader, tenant=[])
    with pytest.raises(AccessDenied):
        scim.map_group(tenant.reader, command)
