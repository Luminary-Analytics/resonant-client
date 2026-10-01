ALTER TABLE sonn_governance.memberships ADD COLUMN provisioned_active boolean NOT NULL DEFAULT true;
CREATE TABLE sonn_governance.scim_credentials (
    credential_id uuid PRIMARY KEY,
    tenant_id uuid NOT NULL REFERENCES sonn_governance.tenants,
    issuer text NOT NULL,
    token_sha256 text NOT NULL UNIQUE CHECK(token_sha256 ~ '^[a-f0-9]{64}$'),
    expires_at timestamptz NOT NULL,
    active boolean NOT NULL DEFAULT true
);
CREATE TABLE sonn_governance.scim_users (
    tenant_id uuid NOT NULL,
    resource_id uuid NOT NULL,
    issuer text NOT NULL,
    external_id text NOT NULL,
    subject text NOT NULL,
    actor_id text NOT NULL,
    user_name text NOT NULL,
    user_name_key text NOT NULL,
    active boolean NOT NULL,
    deleted boolean NOT NULL DEFAULT false,
    revision bigint NOT NULL DEFAULT 1 CHECK(revision>0),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    modified_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY(tenant_id,resource_id),
    UNIQUE(tenant_id,issuer,external_id),
    UNIQUE(tenant_id,actor_id),
    UNIQUE(tenant_id,issuer,user_name_key),
    FOREIGN KEY(tenant_id,actor_id) REFERENCES sonn_governance.memberships
);
CREATE TABLE sonn_governance.scim_groups (
    tenant_id uuid NOT NULL REFERENCES sonn_governance.tenants,
    resource_id uuid NOT NULL,
    issuer text NOT NULL,
    external_id text NOT NULL,
    display_name text NOT NULL,
    deleted boolean NOT NULL DEFAULT false,
    revision bigint NOT NULL DEFAULT 1 CHECK(revision>0),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    modified_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY(tenant_id,resource_id),
    UNIQUE(tenant_id,issuer,external_id)
);
CREATE TABLE sonn_governance.scim_members (
    tenant_id uuid NOT NULL,
    group_id uuid NOT NULL,
    user_id uuid NOT NULL,
    PRIMARY KEY(tenant_id,group_id,user_id),
    FOREIGN KEY(tenant_id,group_id) REFERENCES sonn_governance.scim_groups(tenant_id,resource_id),
    FOREIGN KEY(tenant_id,user_id) REFERENCES sonn_governance.scim_users(tenant_id,resource_id)
);
CREATE TABLE sonn_governance.scim_tenant_rules (
    tenant_id uuid NOT NULL,
    group_id uuid NOT NULL,
    permission text NOT NULL CHECK(permission IN('metadata_read','control_execute','host_admin','audit_read')),
    PRIMARY KEY(tenant_id,group_id,permission),
    FOREIGN KEY(tenant_id,group_id) REFERENCES sonn_governance.scim_groups(tenant_id,resource_id)
);
CREATE TABLE sonn_governance.scim_project_rules (
    tenant_id uuid NOT NULL,
    group_id uuid NOT NULL,
    project_id uuid NOT NULL,
    permission text NOT NULL CHECK(permission IN('metadata_read','control_execute','host_admin','audit_read')),
    PRIMARY KEY(tenant_id,group_id,project_id,permission),
    FOREIGN KEY(tenant_id,group_id) REFERENCES sonn_governance.scim_groups(tenant_id,resource_id),
    FOREIGN KEY(tenant_id,project_id) REFERENCES sonn_governance.projects
);
CREATE TABLE sonn_governance.scim_tenant_grants (
    tenant_id uuid NOT NULL,
    actor_id text NOT NULL,
    group_id uuid NOT NULL,
    permission text NOT NULL CHECK(permission IN('metadata_read','control_execute','host_admin','audit_read')),
    PRIMARY KEY(tenant_id,actor_id,group_id,permission),
    FOREIGN KEY(tenant_id,actor_id) REFERENCES sonn_governance.memberships,
    FOREIGN KEY(tenant_id,group_id,permission) REFERENCES sonn_governance.scim_tenant_rules
);
CREATE TABLE sonn_governance.scim_project_grants (
    tenant_id uuid NOT NULL,
    actor_id text NOT NULL,
    group_id uuid NOT NULL,
    project_id uuid NOT NULL,
    permission text NOT NULL CHECK(permission IN('metadata_read','control_execute','host_admin','audit_read')),
    PRIMARY KEY(tenant_id,actor_id,group_id,project_id,permission),
    FOREIGN KEY(tenant_id,actor_id) REFERENCES sonn_governance.memberships,
    FOREIGN KEY(tenant_id,group_id,project_id,permission) REFERENCES sonn_governance.scim_project_rules
);
DO $$
DECLARE table_name text;
BEGIN
    FOREACH table_name IN ARRAY ARRAY['scim_users','scim_groups','scim_members','scim_tenant_rules',
        'scim_project_rules','scim_tenant_grants','scim_project_grants'] LOOP
        EXECUTE format('ALTER TABLE sonn_governance.%I ENABLE ROW LEVEL SECURITY',table_name);
        EXECUTE format('ALTER TABLE sonn_governance.%I FORCE ROW LEVEL SECURITY',table_name);
        EXECUTE format('CREATE POLICY tenant_isolation ON sonn_governance.%I '
            'USING(tenant_id=nullif(current_setting(''sonn.tenant_id'',true),'''')::uuid) '
            'WITH CHECK(tenant_id=nullif(current_setting(''sonn.tenant_id'',true),'''')::uuid)',table_name);
    END LOOP;
END $$;
ALTER TABLE sonn_governance.audit DROP CONSTRAINT audit_operation_check;
ALTER TABLE sonn_governance.audit ADD CHECK(operation IN
    ('bootstrap','set_policy','set_membership','query_metadata','query_policy','query_audit',
     'enroll_host','activate_host','revoke_host','issue_lease','reserve_request','start_request',
     'settle_request','query_hosts','publish_content','read_content','inspect_content',
     'hold_content','release_content_hold','delete_content','request_grant','approve_grant',
     'reject_grant','revoke_grant','query_grants','register_run','ingest_run','query_runs',
     'request_control','poll_controls','observe_control','scim_create','scim_read','scim_list',
     'scim_update','scim_delete','scim_map','register_provisioner','revoke_provisioner'));
UPDATE sonn_governance.schema_version SET version=7 WHERE singleton;
