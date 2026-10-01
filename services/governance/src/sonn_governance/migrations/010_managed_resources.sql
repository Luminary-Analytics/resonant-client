ALTER TABLE sonn_governance.policy_leases ADD COLUMN runner_protocol integer NOT NULL DEFAULT 1 CHECK(runner_protocol IN (1,2));
ALTER TABLE sonn_governance.audit DROP CONSTRAINT audit_operation_check;
ALTER TABLE sonn_governance.audit ADD CHECK(operation IN
    ('bootstrap','set_policy','set_membership','query_metadata','query_policy','query_audit',
     'enroll_host','activate_host','revoke_host','issue_lease','reserve_request','start_request','settle_request','query_hosts',
     'publish_content','read_content','inspect_content','hold_content','release_content_hold','delete_content',
     'request_grant','approve_grant','reject_grant','revoke_grant','query_grants',
     'register_run','ingest_run','query_runs','request_control','poll_controls','observe_control',
     'scim_create','scim_read','scim_list','scim_update','scim_delete','scim_map','register_provisioner','revoke_provisioner',
     'reserve_worker','observe_worker','bind_request','authorize_tool','observe_tool'));
CREATE TABLE sonn_governance.worker_slots (
    tenant_id uuid NOT NULL,
    project_id uuid NOT NULL,
    worker_id uuid NOT NULL,
    binding_id uuid NOT NULL,
    host_id uuid NOT NULL,
    host_generation bigint NOT NULL,
    kind text NOT NULL CHECK(kind IN ('worker','coordinator')),
    state text NOT NULL CHECK(state IN ('held','uncertain','stopped','never_started')),
    reserved_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    observed_at timestamptz,
    PRIMARY KEY(tenant_id,worker_id),
    FOREIGN KEY(tenant_id,project_id,host_id) REFERENCES sonn_governance.hosts(tenant_id,project_id,host_id),
    FOREIGN KEY(tenant_id,binding_id) REFERENCES sonn_governance.managed_runs
);
CREATE TABLE sonn_governance.request_bindings (
    tenant_id uuid NOT NULL,
    request_id uuid NOT NULL,
    worker_id uuid NOT NULL,
    purpose text NOT NULL CHECK(purpose IN ('primary','planning','compression')),
    model jsonb NOT NULL CHECK(jsonb_typeof(model)='object'),
    input_sha256 text NOT NULL CHECK(input_sha256 ~ '^[a-f0-9]{64}$'),
    PRIMARY KEY(tenant_id,request_id),
    FOREIGN KEY(tenant_id,request_id) REFERENCES sonn_governance.host_requests,
    FOREIGN KEY(tenant_id,worker_id) REFERENCES sonn_governance.worker_slots
);
CREATE TABLE sonn_governance.tool_admissions (
    tenant_id uuid NOT NULL,
    action_id uuid NOT NULL,
    worker_id uuid NOT NULL,
    request_id uuid NOT NULL,
    tool_name text NOT NULL,
    arguments_sha256 text NOT NULL CHECK(arguments_sha256 ~ '^[a-f0-9]{64}$'),
    state text NOT NULL CHECK(state IN ('admitted','uncertain','completed')),
    admitted_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    observed_at timestamptz,
    PRIMARY KEY(tenant_id,action_id),
    FOREIGN KEY(tenant_id,worker_id) REFERENCES sonn_governance.worker_slots,
    FOREIGN KEY(tenant_id,request_id) REFERENCES sonn_governance.request_bindings
);
CREATE TABLE sonn_governance.resource_receipts (
    tenant_id uuid NOT NULL,
    host_id uuid NOT NULL,
    namespace text NOT NULL,
    command_id uuid NOT NULL,
    sha256 text NOT NULL,
    result jsonb NOT NULL,
    audit_id bigint NOT NULL,
    PRIMARY KEY(tenant_id,host_id,namespace,command_id),
    FOREIGN KEY(tenant_id,host_id) REFERENCES sonn_governance.hosts,
    FOREIGN KEY(tenant_id,audit_id) REFERENCES sonn_governance.audit
);
DO $$
DECLARE table_name text;
BEGIN
    FOREACH table_name IN ARRAY ARRAY['worker_slots','request_bindings','tool_admissions','resource_receipts'] LOOP
        EXECUTE format('ALTER TABLE sonn_governance.%I ENABLE ROW LEVEL SECURITY', table_name);
        EXECUTE format('ALTER TABLE sonn_governance.%I FORCE ROW LEVEL SECURITY', table_name);
        EXECUTE format('CREATE POLICY tenant_isolation ON sonn_governance.%I '
            'USING (tenant_id=nullif(current_setting(''sonn.tenant_id'',true),'''')::uuid) '
            'WITH CHECK (tenant_id=nullif(current_setting(''sonn.tenant_id'',true),'''')::uuid)', table_name);
    END LOOP;
END $$;
CREATE FUNCTION sonn_governance.check_resource_archive() RETURNS trigger
    LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,sonn_governance AS $$
BEGIN
    IF NEW.operation IN ('reserve_worker','bind_request','authorize_tool') AND EXISTS(
       SELECT 1 FROM sonn_governance.archive_requirements r WHERE r.tenant_id=NEW.tenant_id AND
         (r.archive_id IS NULL OR r.source_id IS NULL OR EXISTS(
           SELECT 1 FROM sonn_governance.audit_outbox o LEFT JOIN sonn_governance.archive_deliveries d
             ON d.tenant_id=o.tenant_id AND d.audit_id=o.audit_id AND d.archive_id=r.archive_id AND d.source_id=r.source_id AND d.sha256=o.sha256
           WHERE o.tenant_id=r.tenant_id AND d.audit_id IS NULL AND o.queued_at<clock_timestamp()-(r.max_delay_seconds * interval '1 second')))) THEN
        RAISE EXCEPTION 'required resource audit archival is unavailable' USING ERRCODE='55000';
    END IF;
    RETURN NEW;
END $$;
REVOKE ALL ON FUNCTION sonn_governance.check_resource_archive() FROM PUBLIC;
CREATE TRIGGER audit_resource_archive_gate BEFORE INSERT ON sonn_governance.audit FOR EACH ROW EXECUTE FUNCTION sonn_governance.check_resource_archive();
UPDATE sonn_governance.schema_version SET version=10 WHERE singleton;
