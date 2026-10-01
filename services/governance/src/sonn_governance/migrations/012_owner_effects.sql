-- Owner Git/check effects are distinct from model-request tool authority.
ALTER TABLE sonn_governance.audit DROP CONSTRAINT audit_operation_check;
ALTER TABLE sonn_governance.audit ADD CHECK(operation IN
    ('bootstrap','set_policy','set_membership','query_metadata','query_policy','query_audit',
     'enroll_host','activate_host','revoke_host','issue_lease','reserve_request','start_request','settle_request','query_hosts',
     'publish_content','read_content','inspect_content','hold_content','release_content_hold','delete_content',
     'request_grant','approve_grant','reject_grant','revoke_grant','query_grants',
     'register_run','ingest_run','query_runs','request_control','poll_controls','observe_control',
     'scim_create','scim_read','scim_list','scim_update','scim_delete','scim_map','register_provisioner','revoke_provisioner',
     'reserve_worker','observe_worker','bind_request','authorize_tool','observe_tool','authorize_effect','observe_effect'));
CREATE TABLE sonn_governance.owner_effects (
    tenant_id uuid NOT NULL,
    project_id uuid NOT NULL,
    effect_id uuid NOT NULL,
    binding_id uuid NOT NULL,
    host_id uuid NOT NULL,
    host_generation bigint NOT NULL,
    kind text NOT NULL CHECK(kind IN ('writer_git','candidate_git','candidate_check','checkout_apply')),
    semantics_sha256 text NOT NULL CHECK(semantics_sha256 ~ '^[a-f0-9]{64}$'),
    state text NOT NULL CHECK(state IN ('admitted','uncertain','completed','failed','never_started')),
    admitted_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    observed_at timestamptz,
    PRIMARY KEY(tenant_id,effect_id),
    FOREIGN KEY(tenant_id,project_id,host_id) REFERENCES sonn_governance.hosts(tenant_id,project_id,host_id),
    FOREIGN KEY(tenant_id,binding_id) REFERENCES sonn_governance.managed_runs
);
ALTER TABLE sonn_governance.owner_effects ENABLE ROW LEVEL SECURITY;
ALTER TABLE sonn_governance.owner_effects FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON sonn_governance.owner_effects
    USING (tenant_id=nullif(current_setting('sonn.tenant_id',true),'')::uuid)
    WITH CHECK (tenant_id=nullif(current_setting('sonn.tenant_id',true),'')::uuid);
CREATE OR REPLACE FUNCTION sonn_governance.check_resource_archive() RETURNS trigger
    LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,sonn_governance AS $$
BEGIN
    IF NEW.operation IN ('reserve_worker','bind_request','authorize_tool','authorize_effect') AND EXISTS(
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
UPDATE sonn_governance.schema_version SET version=12 WHERE singleton;
