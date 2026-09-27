ALTER TABLE sonn_governance.tenant_grants DROP CONSTRAINT tenant_grants_permission_check;
ALTER TABLE sonn_governance.tenant_grants ADD CHECK(permission IN
    ('metadata_read','content_read','content_write','retention_admin','control_execute',
     'policy_admin','membership_admin','membership_approve','audit_read','host_admin'));
ALTER TABLE sonn_governance.audit DROP CONSTRAINT audit_operation_check;
ALTER TABLE sonn_governance.audit ADD CHECK(operation IN
    ('bootstrap','set_policy','set_membership','query_metadata','query_policy','query_audit',
     'enroll_host','activate_host','revoke_host','issue_lease','reserve_request','start_request',
     'settle_request','query_hosts','publish_content','read_content','inspect_content',
     'hold_content','release_content_hold','delete_content','request_grant','approve_grant',
     'reject_grant','revoke_grant','query_grants'));
CREATE TABLE sonn_governance.grant_requests (
    tenant_id uuid NOT NULL,
    request_id uuid NOT NULL,
    requester text NOT NULL,
    target_actor text NOT NULL,
    authorization_revision bigint NOT NULL CHECK(authorization_revision>=0),
    payload jsonb NOT NULL CHECK(jsonb_typeof(payload)='object'),
    sha256 text NOT NULL CHECK(sha256 ~ '^[a-f0-9]{64}$'),
    state text NOT NULL CHECK(state IN('pending','approved','rejected','revoked')),
    decided_by text,
    decision_audit_id bigint,
    PRIMARY KEY(tenant_id,request_id),
    FOREIGN KEY(tenant_id,requester) REFERENCES sonn_governance.memberships,
    FOREIGN KEY(tenant_id,target_actor) REFERENCES sonn_governance.memberships,
    FOREIGN KEY(tenant_id,decided_by) REFERENCES sonn_governance.memberships,
    FOREIGN KEY(tenant_id,decision_audit_id) REFERENCES sonn_governance.audit,
    CHECK((state='pending' AND decided_by IS NULL AND decision_audit_id IS NULL)
        OR (state!='pending' AND decided_by IS NOT NULL AND decision_audit_id IS NOT NULL))
);
ALTER TABLE sonn_governance.grant_requests ENABLE ROW LEVEL SECURITY;
ALTER TABLE sonn_governance.grant_requests FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON sonn_governance.grant_requests
    USING(tenant_id=nullif(current_setting('sonn.tenant_id',true),'')::uuid)
    WITH CHECK(tenant_id=nullif(current_setting('sonn.tenant_id',true),'')::uuid);
UPDATE sonn_governance.schema_version SET version=5 WHERE singleton;
