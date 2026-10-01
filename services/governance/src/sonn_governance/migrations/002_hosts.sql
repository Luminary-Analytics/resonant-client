ALTER TABLE sonn_governance.tenant_grants DROP CONSTRAINT tenant_grants_permission_check;
ALTER TABLE sonn_governance.tenant_grants ADD CHECK(permission IN
    ('metadata_read','content_read','control_execute','policy_admin','membership_admin','audit_read','host_admin'));
ALTER TABLE sonn_governance.project_grants DROP CONSTRAINT project_grants_permission_check;
ALTER TABLE sonn_governance.project_grants ADD CHECK(permission IN
    ('metadata_read','content_read','control_execute','policy_admin','audit_read','host_admin'));
ALTER TABLE sonn_governance.audit DROP CONSTRAINT audit_operation_check;
ALTER TABLE sonn_governance.audit ADD CHECK(operation IN
    ('bootstrap','set_policy','set_membership','query_metadata','query_policy','query_audit',
     'enroll_host','activate_host','revoke_host','issue_lease','reserve_request','start_request','settle_request','query_hosts'));

CREATE TABLE sonn_governance.hosts (
    tenant_id uuid NOT NULL,
    project_id uuid NOT NULL,
    host_id uuid NOT NULL,
    certificate_sha256 text NOT NULL CHECK(certificate_sha256 ~ '^[a-f0-9]{64}$'),
    state text NOT NULL CHECK(state IN ('pending','active','revoked')),
    revision bigint NOT NULL CHECK(revision > 0),
    generation bigint NOT NULL CHECK(generation > 0),
    challenge_sha256 text NOT NULL CHECK(challenge_sha256 ~ '^[a-f0-9]{64}$'),
    challenge_expires_at timestamptz NOT NULL,
    activation_result jsonb,
    PRIMARY KEY(tenant_id,host_id),
    UNIQUE(tenant_id,project_id,host_id),
    UNIQUE(certificate_sha256),
    FOREIGN KEY(tenant_id,project_id) REFERENCES sonn_governance.projects
);
-- Minimal authentication directory: the verified certificate selects tenant
-- context before RLS is available. No policy/content/secret is stored here.
-- Only the trusted server database role has access; no listing HTTP endpoint.
CREATE TABLE sonn_governance.host_directory (
    certificate_sha256 text PRIMARY KEY,
    tenant_id uuid NOT NULL,
    host_id uuid NOT NULL,
    FOREIGN KEY(tenant_id,host_id) REFERENCES sonn_governance.hosts
);
CREATE TABLE sonn_governance.policy_leases (
    tenant_id uuid NOT NULL,
    project_id uuid NOT NULL,
    lease_id uuid NOT NULL,
    host_id uuid NOT NULL,
    host_generation bigint NOT NULL,
    policy_revision bigint NOT NULL,
    policy_sha256 text NOT NULL,
    document jsonb NOT NULL,
    expires_at timestamptz NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    offline_request_allowance integer NOT NULL DEFAULT 0 CHECK(offline_request_allowance=0),
    PRIMARY KEY(tenant_id,lease_id),
    UNIQUE(tenant_id,project_id,lease_id,host_id),
    FOREIGN KEY(tenant_id,project_id,host_id) REFERENCES sonn_governance.hosts(tenant_id,project_id,host_id),
    FOREIGN KEY(tenant_id,project_id,policy_revision) REFERENCES sonn_governance.policies
);
CREATE TABLE sonn_governance.host_requests (
    tenant_id uuid NOT NULL,
    project_id uuid NOT NULL,
    request_id uuid NOT NULL,
    host_id uuid NOT NULL,
    lease_id uuid NOT NULL,
    state text NOT NULL CHECK(state IN ('reserved','started','uncertain','completed','never_started')),
    started boolean NOT NULL DEFAULT false,
    consumed integer CHECK(consumed IN(0,1)),
    PRIMARY KEY(tenant_id,request_id),
    FOREIGN KEY(tenant_id,project_id,lease_id,host_id)
        REFERENCES sonn_governance.policy_leases(tenant_id,project_id,lease_id,host_id),
    CHECK((state='completed' AND started AND consumed=1)
        OR (state='never_started' AND NOT started AND consumed=0)
        OR (state IN('reserved','started','uncertain') AND consumed IS NULL)),
    CHECK(state!='reserved' OR NOT started),
    CHECK(state!='started' OR started)
);
CREATE TABLE sonn_governance.host_receipts (
    tenant_id uuid NOT NULL,
    host_id uuid NOT NULL,
    namespace text NOT NULL CHECK(namespace IN('lease','reserve','start')),
    command_id uuid NOT NULL,
    semantics_sha256 text NOT NULL,
    result jsonb NOT NULL,
    audit_id bigint NOT NULL,
    PRIMARY KEY(tenant_id,host_id,namespace,command_id),
    FOREIGN KEY(tenant_id,host_id) REFERENCES sonn_governance.hosts,
    FOREIGN KEY(tenant_id,audit_id) REFERENCES sonn_governance.audit
);
DO $$
DECLARE table_name text;
BEGIN
    FOREACH table_name IN ARRAY ARRAY['hosts','policy_leases','host_requests','host_receipts'] LOOP
        EXECUTE format('ALTER TABLE sonn_governance.%I ENABLE ROW LEVEL SECURITY', table_name);
        EXECUTE format('ALTER TABLE sonn_governance.%I FORCE ROW LEVEL SECURITY', table_name);
        EXECUTE format('CREATE POLICY tenant_isolation ON sonn_governance.%I '
            'USING (tenant_id = nullif(current_setting(''sonn.tenant_id'', true), '''')::uuid) '
            'WITH CHECK (tenant_id = nullif(current_setting(''sonn.tenant_id'', true), '''')::uuid)', table_name);
    END LOOP;
END $$;
UPDATE sonn_governance.schema_version SET version=2 WHERE singleton;
