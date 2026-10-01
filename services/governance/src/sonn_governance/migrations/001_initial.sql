CREATE SCHEMA sonn_governance;
REVOKE ALL ON SCHEMA sonn_governance FROM PUBLIC;

CREATE TABLE sonn_governance.schema_version (
    singleton boolean PRIMARY KEY DEFAULT true CHECK(singleton),
    version integer NOT NULL CHECK(version > 0)
);
INSERT INTO sonn_governance.schema_version(version) VALUES(1);

CREATE TABLE sonn_governance.tenants (
    tenant_id uuid PRIMARY KEY,
    active boolean NOT NULL DEFAULT true,
    authorization_revision bigint NOT NULL DEFAULT 0 CHECK(authorization_revision >= 0)
);
CREATE TABLE sonn_governance.memberships (
    tenant_id uuid NOT NULL REFERENCES sonn_governance.tenants,
    actor_id text NOT NULL CHECK(actor_id ~ '^[a-f0-9]{64}$'),
    active boolean NOT NULL,
    revision bigint NOT NULL CHECK(revision >= 0),
    PRIMARY KEY(tenant_id, actor_id)
);
CREATE TABLE sonn_governance.projects (
    tenant_id uuid NOT NULL REFERENCES sonn_governance.tenants,
    project_id uuid NOT NULL,
    active boolean NOT NULL DEFAULT true,
    policy_revision bigint NOT NULL DEFAULT 0 CHECK(policy_revision >= 0),
    PRIMARY KEY(tenant_id, project_id)
);
CREATE TABLE sonn_governance.tenant_grants (
    tenant_id uuid NOT NULL,
    actor_id text NOT NULL,
    permission text NOT NULL CHECK(permission IN
        ('metadata_read','content_read','control_execute','policy_admin','membership_admin','audit_read')),
    PRIMARY KEY(tenant_id, actor_id, permission),
    FOREIGN KEY(tenant_id, actor_id) REFERENCES sonn_governance.memberships
);
CREATE TABLE sonn_governance.project_grants (
    tenant_id uuid NOT NULL,
    actor_id text NOT NULL,
    project_id uuid NOT NULL,
    permission text NOT NULL CHECK(permission IN
        ('metadata_read','content_read','control_execute','policy_admin','audit_read')),
    PRIMARY KEY(tenant_id, actor_id, project_id, permission),
    FOREIGN KEY(tenant_id, actor_id) REFERENCES sonn_governance.memberships,
    FOREIGN KEY(tenant_id, project_id) REFERENCES sonn_governance.projects
);
CREATE TABLE sonn_governance.policies (
    tenant_id uuid NOT NULL,
    project_id uuid NOT NULL,
    revision bigint NOT NULL CHECK(revision > 0),
    document jsonb NOT NULL CHECK(jsonb_typeof(document) = 'object'),
    sha256 text NOT NULL CHECK(sha256 ~ '^[a-f0-9]{64}$'),
    PRIMARY KEY(tenant_id, project_id, revision),
    FOREIGN KEY(tenant_id, project_id) REFERENCES sonn_governance.projects
);
CREATE TABLE sonn_governance.audit (
    tenant_id uuid NOT NULL REFERENCES sonn_governance.tenants,
    audit_id bigint GENERATED ALWAYS AS IDENTITY,
    actor_id text NOT NULL,
    operation text NOT NULL CHECK(operation IN
        ('bootstrap','set_policy','set_membership','query_metadata','query_policy','query_audit')),
    project_id uuid,
    resource_revision bigint,
    decision_id uuid,
    semantics_sha256 text CHECK(semantics_sha256 ~ '^[a-f0-9]{64}$'),
    occurred_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY(tenant_id, audit_id),
    FOREIGN KEY(tenant_id, project_id) REFERENCES sonn_governance.projects
);
CREATE TABLE sonn_governance.command_receipts (
    tenant_id uuid NOT NULL,
    actor_id text NOT NULL,
    namespace text NOT NULL CHECK(namespace = 'governance.v1'),
    command_id uuid NOT NULL,
    semantics_sha256 text NOT NULL CHECK(semantics_sha256 ~ '^[a-f0-9]{64}$'),
    result jsonb NOT NULL CHECK(jsonb_typeof(result) = 'object'),
    audit_id bigint NOT NULL,
    PRIMARY KEY(tenant_id, actor_id, namespace, command_id),
    FOREIGN KEY(tenant_id, actor_id) REFERENCES sonn_governance.memberships,
    FOREIGN KEY(tenant_id, audit_id) REFERENCES sonn_governance.audit
);

-- Application authorization remains mandatory. This context confines accidental
-- unscoped SQL; it is set transaction-locally by the trusted server, never HTTP.
DO $$
DECLARE table_name text;
BEGIN
    FOREACH table_name IN ARRAY ARRAY['tenants','memberships','projects','tenant_grants',
        'project_grants','policies','audit','command_receipts'] LOOP
        EXECUTE format('ALTER TABLE sonn_governance.%I ENABLE ROW LEVEL SECURITY', table_name);
        EXECUTE format('ALTER TABLE sonn_governance.%I FORCE ROW LEVEL SECURITY', table_name);
        EXECUTE format('CREATE POLICY tenant_isolation ON sonn_governance.%I '
            'USING (tenant_id = nullif(current_setting(''sonn.tenant_id'', true), '''')::uuid) '
            'WITH CHECK (tenant_id = nullif(current_setting(''sonn.tenant_id'', true), '''')::uuid)', table_name);
    END LOOP;
END $$;
