"""Bounded SCIM 2 provisioning profile over authoritative PostgreSQL state.

Credentials pin a tenant and issuer. Explicit immutable subject mapping is
separate from username and externalId. Group roles come only from human-owned
server mappings; IdP-supplied roles and sensitive permission mappings are denied.
"""

from datetime import datetime, timezone
import hashlib
import hmac
import json
import re
import secrets
import unicodedata
from uuid import uuid4

import psycopg
from psycopg.errors import UniqueViolation
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from .models import (
    AccessDenied, Conflict, GovernanceError, InvalidRequest, Principal, ProvisioningPrincipal,
    bounded_text, digest, identifier, membership_document,
)

USER_SCHEMA = "urn:ietf:params:scim:schemas:core:2.0:User"
GROUP_SCHEMA = "urn:ietf:params:scim:schemas:core:2.0:Group"
IDENTITY_SCHEMA = "urn:sonn:params:scim:schemas:extension:identity:2.0:User"
PATCH_SCHEMA = "urn:ietf:params:scim:api:messages:2.0:PatchOp"
LIST_SCHEMA = "urn:ietf:params:scim:api:messages:2.0:ListResponse"
MAPPING_PERMISSIONS = frozenset({"metadata_read", "control_execute", "host_admin", "audit_read"})


class ScimError(GovernanceError):
    """Safe SCIM status and error type; never includes request content."""

    def __init__(self, status: int, scim_type: str | None, detail: str):
        super().__init__(detail)
        self.status = status
        self.scim_type = scim_type
        self.detail = detail


def _object(value, allowed):
    if not isinstance(value, dict):
        raise ScimError(400, "invalidSyntax", "An object is required")
    names = {name.casefold(): name for name in allowed}
    result = {}
    for key, item in value.items():
        if not isinstance(key, str) or key.casefold() not in names or names[key.casefold()] in result:
            raise ScimError(400, "invalidValue", "Unsupported or duplicate attribute")
        result[names[key.casefold()]] = item
    return result


def _text(value, limit=256):
    try:
        return bounded_text(value, limit, "attribute")
    except InvalidRequest as exc:
        raise ScimError(400, "invalidValue", "Invalid attribute") from exc


def _id(value):
    try:
        return identifier(value)
    except InvalidRequest as exc:
        raise ScimError(400, "invalidValue", "Invalid resource identifier") from exc


def _kind(resource_type):
    if resource_type not in {"Users", "Groups"}:
        raise ScimError(404, None, "Resource is unavailable")
    return "scim_users" if resource_type == "Users" else "scim_groups"


def _members(value):
    if not isinstance(value, list) or len(value) > 256:
        raise ScimError(400, "invalidValue", "Invalid members")
    members = []
    for item in value:
        member = _object(item, {"value", "type"})
        if set(member) - {"value", "type"} or "value" not in member or member.get("type", "User") != "User":
            raise ScimError(400, "invalidValue", "Only user members are supported")
        members.append(_id(member["value"]))
    if len(set(members)) != len(members):
        raise ScimError(400, "invalidValue", "Duplicate member")
    return sorted(members)


def _document(resource_type, value):
    if resource_type == "Users":
        result = _object(value, {"schemas", "externalId", "userName", "active", IDENTITY_SCHEMA, "id", "meta", "groups"})
        for name in ("id", "meta", "groups"):
            result.pop(name, None)  # SCIM read-only attributes never select authority.
        if set(result) - {"active"} != {"schemas", "externalId", "userName", IDENTITY_SCHEMA}:
            raise ScimError(400, "invalidValue", "Required user attributes are missing")
        if (not isinstance(result["schemas"], list) or len(result["schemas"]) != 2
                or any(not isinstance(item, str) for item in result["schemas"])
                or set(result["schemas"]) != {USER_SCHEMA, IDENTITY_SCHEMA}):
            raise ScimError(400, "invalidValue", "Unsupported user schemas")
        identity = _object(result[IDENTITY_SCHEMA], {"subject"})
        if set(identity) != {"subject"}:
            raise ScimError(400, "invalidValue", "Explicit subject binding is required")
        _text(identity["subject"], 1024)
        result[IDENTITY_SCHEMA] = identity
        _text(result["userName"])
        if any(unicodedata.category(char).startswith("C") for char in result["userName"]):
            raise ScimError(400, "invalidValue", "Invalid username")
        result.setdefault("active", True)
        if type(result["active"]) is not bool:
            raise ScimError(400, "invalidValue", "Invalid active state")
    else:
        result = _object(value, {"schemas", "externalId", "displayName", "members", "id", "meta"})
        result.pop("id", None)
        result.pop("meta", None)
        if set(result) - {"members"} != {"schemas", "externalId", "displayName"} or result["schemas"] != [GROUP_SCHEMA]:
            raise ScimError(400, "invalidValue", "Required group attributes are missing")
        _text(result["displayName"])
        result["members"] = [{"value": member, "type": "User"} for member in _members(result.get("members", []))]
    _text(result["externalId"])
    return result


def register_provisioner(owner_dsn, *, tenant_id, issuer, expires_at):
    """Operator-only provisioning credential; return its secret once to operator."""
    identifier(tenant_id)
    bounded_text(issuer, 2048, "issuer")
    if not issuer.startswith("https://"):
        raise InvalidRequest("provisioning issuer must use HTTPS")
    principal_id, secret = str(uuid4()), secrets.token_urlsafe(32)
    token = f"{principal_id}.{secret}"
    with psycopg.connect(owner_dsn, row_factory=dict_row) as connection:
        connection.execute("SELECT set_config('sonn.tenant_id',%s,true)", (tenant_id,))
        tenant = connection.execute("SELECT active FROM sonn_governance.tenants WHERE tenant_id=%s FOR UPDATE", (tenant_id,)).fetchone()
        now = connection.execute("SELECT extract(epoch FROM clock_timestamp()) AS now").fetchone()["now"]
        if not tenant or not tenant["active"] or type(expires_at) not in {int, float} or not float(now) < expires_at <= float(now) + 366 * 86400:
            raise InvalidRequest("invalid provisioning credential lifetime")
        connection.execute("INSERT INTO sonn_governance.scim_credentials VALUES(%s,%s,%s,%s,%s,true)",
                           (principal_id, tenant_id, issuer, hashlib.sha256(token.encode()).hexdigest(),
                            datetime.fromtimestamp(expires_at, tz=timezone.utc)))
        actor = connection.execute("SELECT current_user AS actor").fetchone()["actor"]
        connection.execute("INSERT INTO sonn_governance.audit(tenant_id,actor_id,operation,semantics_sha256) "
                           "VALUES(%s,%s,'register_provisioner',%s)",
                           (tenant_id, digest({"operator_database_role": actor}), digest({"credential_id": principal_id, "issuer": issuer})))
    return {"credential_id": principal_id, "tenant_id": tenant_id, "token": token}


def revoke_provisioner(owner_dsn, *, credential_id):
    """Operator revocation serializes with all admitted provisioning requests."""
    identifier(credential_id)
    with psycopg.connect(owner_dsn, row_factory=dict_row) as connection:
        row = connection.execute("SELECT tenant_id FROM sonn_governance.scim_credentials WHERE credential_id=%s", (credential_id,)).fetchone()
        if not row:
            raise AccessDenied("resource is unavailable")
        connection.execute("SELECT set_config('sonn.tenant_id',%s,true)", (str(row["tenant_id"]),))
        connection.execute("SELECT 1 FROM sonn_governance.tenants WHERE tenant_id=%s FOR UPDATE", (row["tenant_id"],))
        connection.execute("UPDATE sonn_governance.scim_credentials SET active=false WHERE credential_id=%s", (credential_id,))
        actor = connection.execute("SELECT current_user AS actor").fetchone()["actor"]
        connection.execute("INSERT INTO sonn_governance.audit(tenant_id,actor_id,operation,semantics_sha256) "
                           "VALUES(%s,%s,'revoke_provisioner',%s)",
                           (row["tenant_id"], digest({"operator_database_role": actor}), digest({"credential_id": credential_id})))


class ScimStore:
    """Supported Users/Groups profile; every operation rechecks current credential."""

    def __init__(self, store):
        self.store = store

    def authenticate(self, authorization):
        if not isinstance(authorization, str) or re.fullmatch(r"Bearer [a-f0-9-]{36}\.[A-Za-z0-9_-]{43}", authorization) is None:
            raise AccessDenied("provisioning authentication required")
        token = authorization[7:]
        credential_id = token.split(".", 1)[0]
        try:
            identifier(credential_id)
        except InvalidRequest as exc:
            raise AccessDenied("provisioning authentication required") from exc
        with self.store._connection() as connection:
            row = connection.execute("SELECT *,clock_timestamp() AS now FROM sonn_governance.scim_credentials WHERE credential_id=%s", (credential_id,)).fetchone()
            expected = row["token_sha256"] if row else "0" * 64
            valid = hmac.compare_digest(hashlib.sha256(token.encode()).hexdigest(), expected)
            if not valid or not row or not row["active"] or row["expires_at"] <= row["now"]:
                raise AccessDenied("provisioning authentication required")
            return ProvisioningPrincipal(credential_id, str(row["tenant_id"]), row["expires_at"].timestamp())

    @staticmethod
    def _authority(connection, principal, *, write=False):
        if not isinstance(principal, ProvisioningPrincipal):
            raise AccessDenied("resource is unavailable")
        connection.execute("SELECT set_config('sonn.tenant_id',%s,true)", (principal.tenant_id,))
        lock = "FOR UPDATE" if write else "FOR SHARE"
        tenant = connection.execute(f"SELECT * FROM sonn_governance.tenants WHERE tenant_id=%s {lock}", (principal.tenant_id,)).fetchone()
        # Operator revocation takes this same tenant lock before changing the
        # credential. No row lock requiring credential UPDATE privilege is needed.
        credential = connection.execute("SELECT * FROM sonn_governance.scim_credentials WHERE credential_id=%s AND tenant_id=%s",
                                        (principal.credential_id, principal.tenant_id)).fetchone()
        now = connection.execute("SELECT clock_timestamp() AS now").fetchone()["now"]
        if (not tenant or not tenant["active"] or not credential or not credential["active"]
                or credential["expires_at"] <= now or principal.expires_at <= now.timestamp()):
            raise AccessDenied("resource is unavailable")
        return credential, tenant

    def _finish(self, connection, principal, operation, *, resource_id=None):
        self.store._audit(connection, principal, principal.tenant_id, operation=operation,
                          semantics_sha256=digest({"resource_id": resource_id}) if resource_id else None)
        self._authority(connection, principal)

    @staticmethod
    def _row(connection, principal, credential, resource_type, resource_id):
        _id(resource_id)
        table = _kind(resource_type)
        row = connection.execute(f"SELECT * FROM sonn_governance.{table} WHERE tenant_id=%s AND issuer=%s AND resource_id=%s AND NOT deleted",
                                 (principal.tenant_id, credential["issuer"], resource_id)).fetchone()
        if not row:
            raise ScimError(404, None, "Resource is unavailable")
        return row

    @staticmethod
    def _representation(connection, resource_type, row):
        result = {"schemas": [USER_SCHEMA, IDENTITY_SCHEMA] if resource_type == "Users" else [GROUP_SCHEMA],
                  "id": str(row["resource_id"]), "externalId": row["external_id"],
                  "meta": {"resourceType": "User" if resource_type == "Users" else "Group",
                           "version": f'W/"{row["revision"]}"', "created": row["created_at"].isoformat(),
                           "lastModified": row["modified_at"].isoformat(),
                           "location": f'/scim/v2/{resource_type}/{row["resource_id"]}'}}
        if resource_type == "Users":
            result.update({"userName": row["user_name"], "active": row["active"], IDENTITY_SCHEMA: {"subject": row["subject"]}})
        else:
            result["displayName"] = row["display_name"]
            members = connection.execute("SELECT m.user_id FROM sonn_governance.scim_members m "
                                         "JOIN sonn_governance.scim_users u ON u.tenant_id=m.tenant_id AND u.resource_id=m.user_id "
                                         "WHERE m.tenant_id=%s AND m.group_id=%s AND NOT u.deleted ORDER BY m.user_id",
                                         (row["tenant_id"], row["resource_id"])).fetchall()
            result["members"] = [{"value": str(member["user_id"]), "type": "User"} for member in members]
        return result

    @staticmethod
    def _sync(connection, tenant_id, revision):
        # Provenance is preserved: SCIM changes never delete manual grants, and
        # local suspension never disappears when the IdP reactivates a user.
        connection.execute("DELETE FROM sonn_governance.scim_tenant_grants WHERE tenant_id=%s", (tenant_id,))
        connection.execute("DELETE FROM sonn_governance.scim_project_grants WHERE tenant_id=%s", (tenant_id,))
        connection.execute("UPDATE sonn_governance.memberships m SET provisioned_active=(u.active AND NOT u.deleted),revision=%s "
                           "FROM sonn_governance.scim_users u WHERE m.tenant_id=%s AND u.tenant_id=m.tenant_id AND u.actor_id=m.actor_id",
                           (revision, tenant_id))
        connection.execute("INSERT INTO sonn_governance.scim_tenant_grants SELECT r.tenant_id,u.actor_id,r.group_id,r.permission "
                           "FROM sonn_governance.scim_tenant_rules r JOIN sonn_governance.scim_groups g ON g.tenant_id=r.tenant_id AND g.resource_id=r.group_id "
                           "JOIN sonn_governance.scim_members m ON m.tenant_id=r.tenant_id AND m.group_id=r.group_id "
                           "JOIN sonn_governance.scim_users u ON u.tenant_id=m.tenant_id AND u.resource_id=m.user_id "
                           "WHERE r.tenant_id=%s AND u.active AND NOT u.deleted AND NOT g.deleted", (tenant_id,))
        connection.execute("INSERT INTO sonn_governance.scim_project_grants SELECT r.tenant_id,u.actor_id,r.group_id,r.project_id,r.permission "
                           "FROM sonn_governance.scim_project_rules r JOIN sonn_governance.scim_groups g ON g.tenant_id=r.tenant_id AND g.resource_id=r.group_id "
                           "JOIN sonn_governance.scim_members m ON m.tenant_id=r.tenant_id AND m.group_id=r.group_id "
                           "JOIN sonn_governance.scim_users u ON u.tenant_id=m.tenant_id AND u.resource_id=m.user_id "
                           "WHERE r.tenant_id=%s AND u.active AND NOT u.deleted AND NOT g.deleted", (tenant_id,))
        connection.execute("UPDATE sonn_governance.tenants SET authorization_revision=%s WHERE tenant_id=%s", (revision, tenant_id))

    @staticmethod
    def _set_members(connection, principal, credential, resource_id, members):
        ids = _members(members)
        for user_id in ids:
            if not connection.execute("SELECT 1 FROM sonn_governance.scim_users WHERE tenant_id=%s AND issuer=%s AND resource_id=%s AND NOT deleted",
                                      (principal.tenant_id, credential["issuer"], user_id)).fetchone():
                raise ScimError(400, "invalidValue", "Member is unavailable")
        connection.execute("DELETE FROM sonn_governance.scim_members WHERE tenant_id=%s AND group_id=%s", (principal.tenant_id, resource_id))
        for user_id in ids:
            connection.execute("INSERT INTO sonn_governance.scim_members VALUES(%s,%s,%s)", (principal.tenant_id, resource_id, user_id))

    def create(self, principal, resource_type, document):
        _kind(resource_type)
        value = _document(resource_type, document)
        try:
            with self.store._connection() as connection:
                credential, tenant = self._authority(connection, principal, write=True)
                table = _kind(resource_type)
                maximum = 10000 if resource_type == "Users" else 1000
                total = connection.execute(f"SELECT count(*) AS total FROM sonn_governance.{table} WHERE tenant_id=%s",
                                           (principal.tenant_id,)).fetchone()["total"]
                if total >= maximum:
                    raise ScimError(413, "tooMany", "Provisioning resource limit reached")
                resource_id = str(uuid4())
                if resource_type == "Users":
                    subject = value[IDENTITY_SCHEMA]["subject"]
                    actor = Principal(credential["issuer"], subject, principal.expires_at).actor_id
                    connection.execute("INSERT INTO sonn_governance.memberships(tenant_id,actor_id,active,revision,provisioned_active) "
                                       "VALUES(%s,%s,true,%s,%s) ON CONFLICT(tenant_id,actor_id) DO NOTHING",
                                       (principal.tenant_id, actor, tenant["authorization_revision"] + 1, value["active"]))
                    connection.execute("INSERT INTO sonn_governance.scim_users(tenant_id,resource_id,issuer,external_id,subject,actor_id,user_name,user_name_key,active) "
                                       "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                                       (principal.tenant_id, resource_id, credential["issuer"], value["externalId"], subject, actor,
                                        value["userName"], unicodedata.normalize("NFKC", value["userName"]).casefold(), value["active"]))
                else:
                    connection.execute("INSERT INTO sonn_governance.scim_groups(tenant_id,resource_id,issuer,external_id,display_name) VALUES(%s,%s,%s,%s,%s)",
                                       (principal.tenant_id, resource_id, credential["issuer"], value["externalId"], value["displayName"]))
                    self._set_members(connection, principal, credential, resource_id, value["members"])
                self._sync(connection, principal.tenant_id, tenant["authorization_revision"] + 1)
                result = self._representation(connection, resource_type, self._row(connection, principal, credential, resource_type, resource_id))
                self._finish(connection, principal, "scim_create", resource_id=resource_id)
                return result
        except UniqueViolation as exc:
            raise ScimError(409, "uniqueness", "Resource identity already exists") from exc

    def get(self, principal, resource_type, resource_id):
        with self.store._connection() as connection:
            credential, _ = self._authority(connection, principal)
            result = self._representation(connection, resource_type, self._row(connection, principal, credential, resource_type, resource_id))
            self._finish(connection, principal, "scim_read", resource_id=resource_id)
            return result

    def list(self, principal, resource_type, start_index=1, count=100, filter=None):
        table = _kind(resource_type)
        if type(start_index) is not int or not 1 <= start_index <= 100000 or type(count) is not int or not 0 <= count <= 100:
            raise ScimError(400, "invalidValue", "Invalid pagination")
        field, value = None, None
        if filter is not None:
            if not isinstance(filter, str) or len(filter) > 512:
                raise ScimError(400, "invalidFilter", "Unsupported filter")
            match = re.fullmatch(r'(externalId|userName)\s+eq\s+("(?:\\.|[^"\\])*")', filter, re.IGNORECASE)
            if not match or (match.group(1).casefold() == "username" and resource_type != "Users"):
                raise ScimError(400, "invalidFilter", "Unsupported filter")
            field = "external_id" if match.group(1).casefold() == "externalid" else "user_name_key"
            try:
                value = _text(json.loads(match.group(2)))
            except (ValueError, TypeError) as exc:
                raise ScimError(400, "invalidFilter", "Invalid filter") from exc
            if field == "user_name_key":
                value = unicodedata.normalize("NFKC", value).casefold()
        suffix = f" AND {field}=%s" if field else ""
        with self.store._connection() as connection:
            credential, _ = self._authority(connection, principal)
            params = [principal.tenant_id, credential["issuer"]] + ([value] if field else [])
            where = f"FROM sonn_governance.{table} WHERE tenant_id=%s AND issuer=%s AND NOT deleted{suffix}"
            total = connection.execute(f"SELECT count(*) AS total {where}", params).fetchone()["total"]
            rows = connection.execute(f"SELECT * {where} ORDER BY resource_id LIMIT %s OFFSET %s", [*params, count, start_index - 1]).fetchall()
            result = {"schemas": [LIST_SCHEMA], "totalResults": total, "startIndex": start_index, "itemsPerPage": len(rows),
                      "Resources": [self._representation(connection, resource_type, row) for row in rows]}
            self._finish(connection, principal, "scim_list")
            return result

    @staticmethod
    def _version(row, if_match):
        if if_match is None:
            raise ScimError(428, None, "If-Match is required")
        if not isinstance(if_match, str) or if_match != f'W/"{row["revision"]}"':
            raise ScimError(412, None, "Resource version changed")

    @staticmethod
    def _patch_document(resource_type, original, patch):
        patch = _object(patch, {"schemas", "Operations"})
        if (set(patch) != {"schemas", "Operations"} or patch["schemas"] != [PATCH_SCHEMA]
                or not isinstance(patch["Operations"], list) or not 1 <= len(patch["Operations"]) <= 20):
            raise ScimError(400, "invalidSyntax", "Invalid PATCH envelope")
        result = dict(original)
        writable = {"active": "active", "username": "userName"} if resource_type == "Users" else {"displayname": "displayName", "members": "members"}
        for operation in patch["Operations"]:
            op = _object(operation, {"op", "path", "value"})
            if not isinstance(op.get("op"), str) or op["op"].casefold() not in {"add", "replace", "remove"}:
                raise ScimError(400, "invalidSyntax", "Unsupported PATCH operation")
            action = op["op"].casefold()
            path = op.get("path")
            if action == "remove":
                if "value" in op or resource_type != "Groups" or not isinstance(path, str):
                    raise ScimError(400, "invalidPath", "Unsupported removal")
                if path.casefold() == "members":
                    result["members"] = []
                    continue
                match = re.fullmatch(r'members\[value eq "([a-f0-9-]{36})"\]', path, re.IGNORECASE)
                if not match:
                    raise ScimError(400, "invalidPath", "Unsupported member filter")
                selected = _id(match.group(1))
                if selected not in {item["value"] for item in result["members"]}:
                    raise ScimError(400, "noTarget", "Member is unavailable")
                result["members"] = [item for item in result["members"] if item["value"] != selected]
                continue
            if "value" not in op:
                raise ScimError(400, "invalidSyntax", "PATCH value is required")
            if path is None:
                changes = _object(op["value"], set(writable.values()))
                if not changes:
                    raise ScimError(400, "invalidValue", "No writable attributes")
            elif isinstance(path, str) and path.casefold() in writable:
                changes = {writable[path.casefold()]: op["value"]}
            else:
                raise ScimError(400, "mutability", "Attribute is not writable")
            for key, value in changes.items():
                if key == "members" and action == "add":
                    ids = set(_members(result["members"])) | set(_members(value))
                    result["members"] = [{"value": member, "type": "User"} for member in sorted(ids)]
                else:
                    result[key] = value
        return _document(resource_type, result)

    def _change(self, principal, resource_type, resource_id, document, if_match, *, patch=False, delete=False):
        table = _kind(resource_type)
        try:
            with self.store._connection() as connection:
                credential, tenant = self._authority(connection, principal, write=True)
                row = self._row(connection, principal, credential, resource_type, resource_id)
                self._version(row, if_match)
                if delete:
                    if resource_type == "Users":
                        # Removing a returned member changes the group resource
                        # representation, so its ETag must change in this commit.
                        connection.execute("UPDATE sonn_governance.scim_groups g SET revision=revision+1,modified_at=clock_timestamp() "
                                           "WHERE tenant_id=%s AND EXISTS(SELECT 1 FROM sonn_governance.scim_members m "
                                           "WHERE m.tenant_id=g.tenant_id AND m.group_id=g.resource_id AND m.user_id=%s)",
                                           (principal.tenant_id, resource_id))
                        connection.execute("DELETE FROM sonn_governance.scim_members WHERE tenant_id=%s AND user_id=%s",
                                           (principal.tenant_id, resource_id))
                    connection.execute(f"UPDATE sonn_governance.{table} SET deleted=true,revision=revision+1,modified_at=clock_timestamp() "
                                       "WHERE tenant_id=%s AND resource_id=%s", (principal.tenant_id, resource_id))
                else:
                    value = (self._patch_document(resource_type, self._representation(connection, resource_type, row), document)
                             if patch else _document(resource_type, document))
                    if value["externalId"] != row["external_id"]:
                        raise ScimError(400, "mutability", "External identity is immutable")
                    if resource_type == "Users":
                        if value[IDENTITY_SCHEMA]["subject"] != row["subject"]:
                            raise ScimError(400, "mutability", "Subject binding is immutable")
                        connection.execute("UPDATE sonn_governance.scim_users SET user_name=%s,user_name_key=%s,active=%s,revision=revision+1,"
                                           "modified_at=clock_timestamp() WHERE tenant_id=%s AND resource_id=%s",
                                           (value["userName"], unicodedata.normalize("NFKC", value["userName"]).casefold(), value["active"],
                                            principal.tenant_id, resource_id))
                    else:
                        connection.execute("UPDATE sonn_governance.scim_groups SET display_name=%s,revision=revision+1,modified_at=clock_timestamp() "
                                           "WHERE tenant_id=%s AND resource_id=%s", (value["displayName"], principal.tenant_id, resource_id))
                        self._set_members(connection, principal, credential, resource_id, value["members"])
                self._sync(connection, principal.tenant_id, tenant["authorization_revision"] + 1)
                result = None if delete else self._representation(connection, resource_type, self._row(connection, principal, credential, resource_type, resource_id))
                self._finish(connection, principal, "scim_delete" if delete else "scim_update", resource_id=resource_id)
                return result
        except UniqueViolation as exc:
            raise ScimError(409, "uniqueness", "Resource identity already exists") from exc

    def replace(self, principal, resource_type, resource_id, document, if_match):
        return self._change(principal, resource_type, resource_id, document, if_match)

    def patch(self, principal, resource_type, resource_id, document, if_match):
        return self._change(principal, resource_type, resource_id, document, if_match, patch=True)

    def delete(self, principal, resource_type, resource_id, if_match):
        return self._change(principal, resource_type, resource_id, None, if_match, delete=True)

    def map_group(self, principal, command):
        """Human-controlled explicit non-sensitive role mapping; no IdP roles."""
        semantics = command.semantics()
        value = semantics["payload"]
        if (command.operation != "map_scim_group" or command.project_id is not None
                or set(value) != {"group_id", "tenant_permissions", "project_permissions"}):
            raise InvalidRequest("invalid group mapping")
        identifier(value["group_id"])
        membership_document({"actor_id": "0" * 64, "active": True,
                             "tenant_permissions": value["tenant_permissions"], "project_permissions": value["project_permissions"]})
        if (set(value["tenant_permissions"]) - MAPPING_PERMISSIONS
                or any(set(permissions) - MAPPING_PERMISSIONS for permissions in value["project_permissions"].values())):
            raise InvalidRequest("this provisioning profile cannot grant sensitive or administrative permissions")
        with self.store._connection() as connection:
            tenant = self.store._authorize(connection, principal, command.tenant_id, None, "membership_admin", edit_membership=True)
            group = connection.execute("SELECT 1 FROM sonn_governance.scim_groups WHERE tenant_id=%s AND resource_id=%s AND NOT deleted",
                                       (command.tenant_id, value["group_id"])).fetchone()
            if not group:
                raise AccessDenied("resource is unavailable")
            for project in value["project_permissions"]:
                if not connection.execute("SELECT 1 FROM sonn_governance.projects WHERE tenant_id=%s AND project_id=%s AND active",
                                          (command.tenant_id, project)).fetchone():
                    raise AccessDenied("resource is unavailable")
            key = f"{command.tenant_id}:{principal.actor_id}:{command.command_id}"
            number = int.from_bytes(hashlib.sha256(key.encode()).digest()[:8], "big", signed=True)
            connection.execute("SELECT pg_advisory_xact_lock(%s)", (number,))
            previous = connection.execute("SELECT semantics_sha256,result FROM sonn_governance.command_receipts WHERE tenant_id=%s AND actor_id=%s "
                                          "AND namespace='governance.v1' AND command_id=%s", (command.tenant_id, principal.actor_id, command.command_id)).fetchone()
            if previous:
                if previous["semantics_sha256"] != digest(semantics):
                    raise Conflict("command identity has different semantics")
                self.store._identity(connection, principal)
                return previous["result"]
            if tenant["authorization_revision"] != command.expected_revision:
                raise Conflict("authorization revision changed")
            for table in ("scim_tenant_grants", "scim_project_grants", "scim_tenant_rules", "scim_project_rules"):
                connection.execute(f"DELETE FROM sonn_governance.{table} WHERE tenant_id=%s AND group_id=%s", (command.tenant_id, value["group_id"]))
            for permission in value["tenant_permissions"]:
                connection.execute("INSERT INTO sonn_governance.scim_tenant_rules VALUES(%s,%s,%s)", (command.tenant_id, value["group_id"], permission))
            for project, permissions in value["project_permissions"].items():
                for permission in permissions:
                    connection.execute("INSERT INTO sonn_governance.scim_project_rules VALUES(%s,%s,%s,%s)", (command.tenant_id, value["group_id"], project, permission))
            revision = tenant["authorization_revision"] + 1
            self._sync(connection, command.tenant_id, revision)
            audit = self.store._audit(connection, principal, command.tenant_id, operation="scim_map", revision=revision, semantics_sha256=digest(semantics))
            result = {"group_id": value["group_id"], "revision": revision, "audit_id": audit}
            connection.execute("INSERT INTO sonn_governance.command_receipts VALUES(%s,%s,'governance.v1',%s,%s,%s,%s)",
                               (command.tenant_id, principal.actor_id, command.command_id, digest(semantics), Jsonb(result), audit))
            self.store._identity(connection, principal)
            return result
