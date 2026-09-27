ALTER TABLE sonn_governance.audit DROP CONSTRAINT audit_operation_check;
ALTER TABLE sonn_governance.audit ADD CHECK(operation IN
    ('bootstrap','set_policy','set_membership','query_metadata','query_policy','query_audit',
     'enroll_host','activate_host','revoke_host','issue_lease','reserve_request','start_request',
     'settle_request','query_hosts','publish_content','read_content','inspect_content',
     'hold_content','release_content_hold','delete_content',
     'request_grant','approve_grant','reject_grant','revoke_grant','query_grants',
     'register_run','ingest_run','query_runs','request_control','poll_controls','observe_control'));

CREATE TABLE sonn_governance.managed_runs (
    tenant_id uuid NOT NULL,
    project_id uuid NOT NULL,
    binding_id uuid NOT NULL,
    host_id uuid NOT NULL,
    host_generation bigint NOT NULL,
    owner_actor text NOT NULL,
    local_run_id uuid NOT NULL,
    session_id uuid NOT NULL,
    revision bigint NOT NULL DEFAULT 1 CHECK(revision>0),
    sequence bigint NOT NULL DEFAULT 0 CHECK(sequence>=0),
    projection jsonb CHECK(jsonb_typeof(projection)='object'),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    last_contact_at timestamptz,
    PRIMARY KEY(tenant_id,binding_id),
    UNIQUE(tenant_id,host_id,local_run_id),
    FOREIGN KEY(tenant_id,project_id) REFERENCES sonn_governance.projects,
    FOREIGN KEY(tenant_id,host_id) REFERENCES sonn_governance.hosts,
    FOREIGN KEY(tenant_id,owner_actor) REFERENCES sonn_governance.memberships
);
CREATE TABLE sonn_governance.run_events (
    tenant_id uuid NOT NULL,
    binding_id uuid NOT NULL,
    sequence bigint NOT NULL CHECK(sequence>0),
    sha256 text NOT NULL CHECK(sha256 ~ '^[a-f0-9]{64}$'),
    projection jsonb NOT NULL CHECK(jsonb_typeof(projection)='object'),
    received_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    quarantined boolean NOT NULL,
    PRIMARY KEY(tenant_id,binding_id,sequence),
    FOREIGN KEY(tenant_id,binding_id) REFERENCES sonn_governance.managed_runs
);
CREATE TABLE sonn_governance.remote_controls (
    tenant_id uuid NOT NULL,
    binding_id uuid NOT NULL,
    control_id uuid NOT NULL,
    actor_id text NOT NULL,
    operation text NOT NULL CHECK(operation IN ('pause','stop')),
    expected_epoch bigint NOT NULL CHECK(expected_epoch>0),
    expected_local_revision bigint NOT NULL CHECK(expected_local_revision>=0),
    policy_revision bigint NOT NULL CHECK(policy_revision>0),
    authorization_revision bigint NOT NULL CHECK(authorization_revision>=0),
    requested_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    expires_at timestamptz NOT NULL,
    received_at timestamptz,
    outcome text CHECK(outcome IN ('applied','denied','uncertain')),
    outcome_at timestamptz,
    reported_processes_stopped boolean NOT NULL DEFAULT false,
    PRIMARY KEY(tenant_id,control_id),
    FOREIGN KEY(tenant_id,binding_id) REFERENCES sonn_governance.managed_runs,
    FOREIGN KEY(tenant_id,actor_id) REFERENCES sonn_governance.memberships,
    CHECK((outcome IS NULL AND outcome_at IS NULL) OR (outcome IS NOT NULL AND outcome_at IS NOT NULL AND received_at IS NOT NULL)),
    CHECK(NOT reported_processes_stopped OR (operation='stop' AND outcome='applied'))
);
CREATE TABLE sonn_governance.monitor_receipts (
    tenant_id uuid NOT NULL,
    actor_id text NOT NULL,
    command_id uuid NOT NULL,
    sha256 text NOT NULL,
    result jsonb NOT NULL,
    audit_id bigint NOT NULL,
    PRIMARY KEY(tenant_id,actor_id,command_id),
    FOREIGN KEY(tenant_id,audit_id) REFERENCES sonn_governance.audit
);
DO $$
DECLARE table_name text;
BEGIN
    FOREACH table_name IN ARRAY ARRAY['managed_runs','run_events','remote_controls','monitor_receipts'] LOOP
        EXECUTE format('ALTER TABLE sonn_governance.%I ENABLE ROW LEVEL SECURITY', table_name);
        EXECUTE format('ALTER TABLE sonn_governance.%I FORCE ROW LEVEL SECURITY', table_name);
        EXECUTE format('CREATE POLICY tenant_isolation ON sonn_governance.%I '
            'USING (tenant_id=nullif(current_setting(''sonn.tenant_id'',true),'''')::uuid) '
            'WITH CHECK (tenant_id=nullif(current_setting(''sonn.tenant_id'',true),'''')::uuid)', table_name);
    END LOOP;
END $$;
UPDATE sonn_governance.schema_version SET version=6 WHERE singleton;
