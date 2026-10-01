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
     'sharing_accept','sharing_inspect','sharing_terms','fence_absent'));
CREATE TABLE sonn_governance.resource_fences (
    tenant_id uuid NOT NULL,
    project_id uuid NOT NULL,
    host_id uuid NOT NULL,
    kind text NOT NULL CHECK(kind IN ('request','worker','tool','effect')),
    resource_id uuid NOT NULL,
    lease_id uuid NOT NULL,
    fence_id uuid NOT NULL UNIQUE,
    audit_id bigint NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY(tenant_id,kind,resource_id),
    FOREIGN KEY(tenant_id,project_id,host_id) REFERENCES sonn_governance.hosts(tenant_id,project_id,host_id),
    FOREIGN KEY(tenant_id,lease_id) REFERENCES sonn_governance.policy_leases,
    FOREIGN KEY(tenant_id,audit_id) REFERENCES sonn_governance.audit
);
ALTER TABLE sonn_governance.resource_fences ENABLE ROW LEVEL SECURITY;
ALTER TABLE sonn_governance.resource_fences FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON sonn_governance.resource_fences
    USING (tenant_id=nullif(current_setting('sonn.tenant_id',true),'')::uuid)
    WITH CHECK (tenant_id=nullif(current_setting('sonn.tenant_id',true),'')::uuid);
UPDATE sonn_governance.schema_version SET version=14 WHERE singleton;
