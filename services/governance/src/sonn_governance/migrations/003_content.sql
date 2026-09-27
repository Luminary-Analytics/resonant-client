ALTER TABLE sonn_governance.tenant_grants DROP CONSTRAINT tenant_grants_permission_check;
ALTER TABLE sonn_governance.tenant_grants ADD CHECK(permission IN
    ('metadata_read','content_read','content_write','retention_admin','control_execute',
     'policy_admin','membership_admin','audit_read','host_admin'));
ALTER TABLE sonn_governance.project_grants DROP CONSTRAINT project_grants_permission_check;
ALTER TABLE sonn_governance.project_grants ADD CHECK(permission IN
    ('metadata_read','content_read','content_write','retention_admin','control_execute',
     'policy_admin','audit_read','host_admin'));
ALTER TABLE sonn_governance.audit DROP CONSTRAINT audit_operation_check;
ALTER TABLE sonn_governance.audit ADD CHECK(operation IN
    ('bootstrap','set_policy','set_membership','query_metadata','query_policy','query_audit',
     'enroll_host','activate_host','revoke_host','issue_lease','reserve_request','start_request',
     'settle_request','query_hosts','publish_content','read_content','inspect_content',
     'hold_content','release_content_hold','delete_content'));

CREATE TABLE sonn_governance.content_objects (
    tenant_id uuid NOT NULL,
    project_id uuid NOT NULL,
    content_id uuid NOT NULL,
    revision bigint NOT NULL DEFAULT 1 CHECK(revision > 0),
    key_id text NOT NULL,
    nonce bytea NOT NULL CHECK(octet_length(nonce)=12),
    ciphertext bytea,
    content_sha256 text CHECK(content_sha256 ~ '^[a-f0-9]{64}$'),
    size_bytes integer CHECK(size_bytes BETWEEN 1 AND 1048576),
    media_type text CHECK(media_type IN ('text/plain','application/json','image/png')),
    created_by text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    expires_at timestamptz NOT NULL,
    held boolean NOT NULL DEFAULT false,
    hold_actor text,
    hold_reason text CHECK(hold_reason IN ('owner_request','security_review','legal_review')),
    deleted_at timestamptz,
    PRIMARY KEY(tenant_id,project_id,content_id),
    UNIQUE(key_id,nonce),
    FOREIGN KEY(tenant_id,project_id) REFERENCES sonn_governance.projects,
    FOREIGN KEY(tenant_id,created_by) REFERENCES sonn_governance.memberships,
    CHECK((deleted_at IS NULL AND ciphertext IS NOT NULL AND content_sha256 IS NOT NULL
           AND size_bytes IS NOT NULL AND media_type IS NOT NULL
           AND octet_length(ciphertext)=size_bytes+16)
       OR (deleted_at IS NOT NULL AND ciphertext IS NULL AND content_sha256 IS NULL
           AND size_bytes IS NULL AND media_type IS NULL AND NOT held)),
    CHECK((held AND hold_actor IS NOT NULL AND hold_reason IS NOT NULL)
       OR (NOT held AND hold_actor IS NULL AND hold_reason IS NULL))
);
CREATE TABLE sonn_governance.content_receipts (
    tenant_id uuid NOT NULL,
    actor_id text NOT NULL,
    command_id uuid NOT NULL,
    semantics_sha256 text NOT NULL,
    result jsonb NOT NULL CHECK(jsonb_typeof(result)='object'),
    audit_id bigint NOT NULL,
    PRIMARY KEY(tenant_id,actor_id,command_id),
    FOREIGN KEY(tenant_id,actor_id) REFERENCES sonn_governance.memberships,
    FOREIGN KEY(tenant_id,audit_id) REFERENCES sonn_governance.audit
);
DO $$
DECLARE table_name text;
BEGIN
    FOREACH table_name IN ARRAY ARRAY['content_objects','content_receipts'] LOOP
        EXECUTE format('ALTER TABLE sonn_governance.%I ENABLE ROW LEVEL SECURITY', table_name);
        EXECUTE format('ALTER TABLE sonn_governance.%I FORCE ROW LEVEL SECURITY', table_name);
        EXECUTE format('CREATE POLICY tenant_isolation ON sonn_governance.%I '
            'USING (tenant_id=nullif(current_setting(''sonn.tenant_id'',true),'''')::uuid) '
            'WITH CHECK (tenant_id=nullif(current_setting(''sonn.tenant_id'',true),'''')::uuid)', table_name);
    END LOOP;
END $$;
UPDATE sonn_governance.schema_version SET version=3 WHERE singleton;
