-- Preserve causal accounting while permitting explicit active-payload deletion.
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
     'sharing_accept','sharing_inspect','sharing_terms','fence_absent',
     'inspect_sharing_terms_retention','hold_sharing_terms','release_sharing_terms_hold','delete_sharing_terms',
     'inspect_sharing_message_retention','hold_sharing_message','release_sharing_message_hold','delete_sharing_message'));
ALTER TABLE sonn_governance.sharing_grants
    ADD COLUMN retention_revision bigint NOT NULL DEFAULT 1 CHECK(retention_revision>0),
    ADD COLUMN held boolean NOT NULL DEFAULT false,
    ADD COLUMN hold_actor text,
    ADD COLUMN hold_reason text CHECK(hold_reason IN ('owner_request','security_review','legal_review')),
    ADD COLUMN deleted_at timestamptz,
    ALTER COLUMN terms_sha256 DROP NOT NULL,
    ALTER COLUMN key_id DROP NOT NULL,
    ALTER COLUMN nonce DROP NOT NULL,
    ALTER COLUMN ciphertext DROP NOT NULL,
    ADD CHECK ((held AND hold_actor IS NOT NULL AND hold_reason IS NOT NULL AND deleted_at IS NULL)
        OR (NOT held AND hold_actor IS NULL AND hold_reason IS NULL)),
    ADD CHECK ((deleted_at IS NULL AND terms_sha256 IS NOT NULL AND key_id IS NOT NULL AND nonce IS NOT NULL AND ciphertext IS NOT NULL)
        OR (deleted_at IS NOT NULL AND terms_sha256 IS NULL AND key_id IS NULL AND nonce IS NULL AND ciphertext IS NULL));
ALTER TABLE sonn_governance.sharing_messages
    ADD COLUMN retention_revision bigint NOT NULL DEFAULT 1 CHECK(retention_revision>0),
    ADD COLUMN held boolean NOT NULL DEFAULT false,
    ADD COLUMN hold_actor text,
    ADD COLUMN hold_reason text CHECK(hold_reason IN ('owner_request','security_review','legal_review')),
    ADD COLUMN deleted_at timestamptz,
    ALTER COLUMN body_sha256 DROP NOT NULL,
    ALTER COLUMN key_id DROP NOT NULL,
    ALTER COLUMN nonce DROP NOT NULL,
    ALTER COLUMN ciphertext DROP NOT NULL,
    ADD CHECK ((held AND hold_actor IS NOT NULL AND hold_reason IS NOT NULL AND deleted_at IS NULL)
        OR (NOT held AND hold_actor IS NULL AND hold_reason IS NULL)),
    ADD CHECK ((deleted_at IS NULL AND body_sha256 IS NOT NULL AND key_id IS NOT NULL AND nonce IS NOT NULL AND ciphertext IS NOT NULL)
        OR (deleted_at IS NOT NULL AND body_sha256 IS NULL AND key_id IS NULL AND nonce IS NULL AND ciphertext IS NULL));
CREATE TABLE sonn_governance.sharing_retention_receipts (
    tenant_id uuid NOT NULL,
    actor_id text NOT NULL,
    command_id uuid NOT NULL,
    semantics_sha256 text NOT NULL CHECK(semantics_sha256 ~ '^[a-f0-9]{64}$'),
    result jsonb NOT NULL,
    audit_id bigint NOT NULL,
    PRIMARY KEY(tenant_id,actor_id,command_id),
    FOREIGN KEY(tenant_id,audit_id) REFERENCES sonn_governance.audit
);
ALTER TABLE sonn_governance.sharing_retention_receipts ENABLE ROW LEVEL SECURITY;
ALTER TABLE sonn_governance.sharing_retention_receipts FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON sonn_governance.sharing_retention_receipts
    USING (tenant_id=nullif(current_setting('sonn.tenant_id',true),'')::uuid)
    WITH CHECK (tenant_id=nullif(current_setting('sonn.tenant_id',true),'')::uuid);
UPDATE sonn_governance.schema_version SET version=15 WHERE singleton;
