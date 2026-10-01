-- Explicit encrypted cross-member disclosure; no automatic session discovery.
ALTER TABLE sonn_governance.audit DROP CONSTRAINT audit_operation_check;
ALTER TABLE sonn_governance.audit ADD CHECK(operation IN
    ('bootstrap','set_policy','set_membership','query_metadata','query_policy','query_audit',
     'enroll_host','activate_host','revoke_host','issue_lease','reserve_request','start_request','settle_request','query_hosts',
     'publish_content','read_content','inspect_content','hold_content','release_content_hold','delete_content',
     'request_grant','approve_grant','reject_grant','revoke_grant','query_grants',
     'register_run','ingest_run','query_runs','request_control','poll_controls','observe_control',
     'scim_create','scim_read','scim_list','scim_update','scim_delete','scim_map','register_provisioner','revoke_provisioner',
     'reserve_worker','observe_worker','bind_request','authorize_tool','observe_tool','authorize_effect','observe_effect',
     'sharing_policy','sharing_offer','sharing_approve','sharing_revoke','sharing_send','sharing_deliver',
     'sharing_accept','sharing_inspect','sharing_terms'));

CREATE TABLE sonn_governance.sharing_policies (
    tenant_id uuid NOT NULL,
    project_id uuid NOT NULL,
    revision bigint NOT NULL CHECK(revision>0),
    document jsonb NOT NULL,
    sha256 text NOT NULL CHECK(sha256 ~ '^[a-f0-9]{64}$'),
    PRIMARY KEY(tenant_id,project_id,revision),
    FOREIGN KEY(tenant_id,project_id) REFERENCES sonn_governance.projects
);
CREATE TABLE sonn_governance.sharing_grants (
    tenant_id uuid NOT NULL,
    grant_id uuid NOT NULL,
    origin_binding uuid NOT NULL,
    receiver_binding uuid NOT NULL,
    captured jsonb NOT NULL,
    limits jsonb NOT NULL,
    terms_sha256 text NOT NULL CHECK(terms_sha256 ~ '^[a-f0-9]{64}$'),
    key_id text NOT NULL,
    nonce bytea NOT NULL,
    ciphertext bytea NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    expires_at timestamptz NOT NULL,
    approved_at timestamptz,
    revoked_at timestamptz,
    PRIMARY KEY(tenant_id,grant_id),
    FOREIGN KEY(tenant_id,origin_binding) REFERENCES sonn_governance.managed_runs,
    FOREIGN KEY(tenant_id,receiver_binding) REFERENCES sonn_governance.managed_runs,
    CHECK(origin_binding<>receiver_binding)
);
CREATE TABLE sonn_governance.sharing_messages (
    tenant_id uuid NOT NULL,
    message_id uuid NOT NULL,
    grant_id uuid NOT NULL,
    parent_id uuid,
    kind text NOT NULL,
    data_class text NOT NULL,
    body_sha256 text NOT NULL CHECK(body_sha256 ~ '^[a-f0-9]{64}$'),
    byte_size integer NOT NULL CHECK(byte_size BETWEEN 1 AND 8192),
    lineage jsonb NOT NULL CHECK(jsonb_typeof(lineage)='array'),
    binding_path jsonb NOT NULL CHECK(jsonb_typeof(binding_path)='array'),
    key_id text NOT NULL,
    nonce bytea NOT NULL,
    ciphertext bytea NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    delivered_at timestamptz,
    PRIMARY KEY(tenant_id,message_id),
    FOREIGN KEY(tenant_id,grant_id) REFERENCES sonn_governance.sharing_grants,
    FOREIGN KEY(tenant_id,parent_id) REFERENCES sonn_governance.sharing_messages
);
CREATE TABLE sonn_governance.sharing_acceptances (
    tenant_id uuid NOT NULL,
    message_id uuid NOT NULL,
    receiver_binding uuid NOT NULL,
    worker_id uuid NOT NULL,
    request_limit integer NOT NULL CHECK(request_limit BETWEEN 1 AND 1000),
    contract_sha256 text NOT NULL CHECK(contract_sha256 ~ '^[a-f0-9]{64}$'),
    accepted_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY(tenant_id,message_id),
    UNIQUE(tenant_id,worker_id),
    FOREIGN KEY(tenant_id,message_id) REFERENCES sonn_governance.sharing_messages,
    FOREIGN KEY(tenant_id,receiver_binding) REFERENCES sonn_governance.managed_runs,
    FOREIGN KEY(tenant_id,worker_id) REFERENCES sonn_governance.worker_slots
);
CREATE TABLE sonn_governance.sharing_receipts (
    tenant_id uuid NOT NULL,
    actor_id text NOT NULL,
    command_id uuid NOT NULL,
    sha256 text NOT NULL CHECK(sha256 ~ '^[a-f0-9]{64}$'),
    result jsonb NOT NULL,
    audit_id bigint NOT NULL,
    PRIMARY KEY(tenant_id,actor_id,command_id),
    FOREIGN KEY(tenant_id,audit_id) REFERENCES sonn_governance.audit
);
CREATE INDEX sharing_grant_origin ON sonn_governance.sharing_grants(tenant_id,origin_binding,grant_id);
CREATE INDEX sharing_grant_receiver ON sonn_governance.sharing_grants(tenant_id,receiver_binding,grant_id);
CREATE INDEX sharing_message_grant ON sonn_governance.sharing_messages(tenant_id,grant_id,message_id);
CREATE INDEX sharing_message_lineage ON sonn_governance.sharing_messages USING gin(lineage);
DO $$
DECLARE table_name text;
BEGIN
    FOREACH table_name IN ARRAY ARRAY['sharing_policies','sharing_grants','sharing_messages','sharing_acceptances','sharing_receipts'] LOOP
        EXECUTE format('ALTER TABLE sonn_governance.%I ENABLE ROW LEVEL SECURITY', table_name);
        EXECUTE format('ALTER TABLE sonn_governance.%I FORCE ROW LEVEL SECURITY', table_name);
        EXECUTE format('CREATE POLICY tenant_isolation ON sonn_governance.%I '
            'USING (tenant_id=nullif(current_setting(''sonn.tenant_id'',true),'''')::uuid) '
            'WITH CHECK (tenant_id=nullif(current_setting(''sonn.tenant_id'',true),'''')::uuid)', table_name);
    END LOOP;
END $$;
CREATE OR REPLACE FUNCTION sonn_governance.check_resource_archive() RETURNS trigger
    LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,sonn_governance AS $$
BEGIN
    IF NEW.operation IN ('reserve_worker','bind_request','authorize_tool','authorize_effect',
       'sharing_offer','sharing_approve','sharing_send','sharing_deliver','sharing_accept','sharing_terms') AND EXISTS(
       SELECT 1 FROM sonn_governance.archive_requirements r WHERE r.tenant_id=NEW.tenant_id AND
         (r.archive_id IS NULL OR r.source_id IS NULL OR EXISTS(
           SELECT 1 FROM sonn_governance.audit_outbox o LEFT JOIN sonn_governance.archive_deliveries d
             ON d.tenant_id=o.tenant_id AND d.audit_id=o.audit_id AND d.archive_id=r.archive_id AND d.source_id=r.source_id AND d.sha256=o.sha256
           WHERE o.tenant_id=r.tenant_id AND d.audit_id IS NULL AND
             o.queued_at<clock_timestamp()-(r.max_delay_seconds * interval '1 second')))) THEN
        RAISE EXCEPTION 'archive delivery required' USING ERRCODE='55000';
    END IF;
    RETURN NEW;
END $$;
UPDATE sonn_governance.schema_version SET version=13 WHERE singleton;
