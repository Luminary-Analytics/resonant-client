CREATE TABLE sonn_governance.audit_outbox (
    tenant_id uuid NOT NULL,
    audit_id bigint NOT NULL,
    payload bytea NOT NULL CHECK(octet_length(payload) BETWEEN 1 AND 16384),
    sha256 text NOT NULL CHECK(sha256 ~ '^[a-f0-9]{64}$'),
    queued_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY(tenant_id,audit_id),
    FOREIGN KEY(tenant_id,audit_id) REFERENCES sonn_governance.audit
);
CREATE TABLE sonn_governance.archive_deliveries (
    tenant_id uuid NOT NULL,
    audit_id bigint NOT NULL,
    archive_id uuid NOT NULL,
    receipt_id uuid NOT NULL,
    sha256 text NOT NULL,
    delivered_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY(tenant_id,audit_id),
    FOREIGN KEY(tenant_id,audit_id) REFERENCES sonn_governance.audit_outbox
);
CREATE TABLE sonn_governance.archive_requirements (
    tenant_id uuid PRIMARY KEY REFERENCES sonn_governance.tenants,
    max_delay_seconds integer NOT NULL CHECK(max_delay_seconds BETWEEN 30 AND 86400)
);
CREATE FUNCTION sonn_governance.capture_audit() RETURNS trigger
    LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,sonn_governance AS $$
DECLARE payload_bytes bytea;
BEGIN
    IF NEW.operation IN ('issue_lease','reserve_request','start_request','register_run','publish_content','read_content')
       AND EXISTS(SELECT 1 FROM sonn_governance.archive_requirements r
           JOIN sonn_governance.audit_outbox o USING(tenant_id)
           LEFT JOIN sonn_governance.archive_deliveries d USING(tenant_id,audit_id)
           WHERE r.tenant_id=NEW.tenant_id AND d.audit_id IS NULL
             AND o.queued_at<clock_timestamp()-(r.max_delay_seconds * interval '1 second')) THEN
        RAISE EXCEPTION 'required audit archival is unavailable' USING ERRCODE='55000';
    END IF;
    payload_bytes := convert_to(jsonb_build_object(
        'version',1,'tenant_id',NEW.tenant_id,'audit_id',NEW.audit_id,'actor_id',NEW.actor_id,
        'operation',NEW.operation,'project_id',NEW.project_id,'resource_revision',NEW.resource_revision,
        'decision_id',NEW.decision_id,'semantics_sha256',NEW.semantics_sha256,'occurred_at',NEW.occurred_at)::text,'UTF8');
    INSERT INTO sonn_governance.audit_outbox(tenant_id,audit_id,payload,sha256)
        VALUES(NEW.tenant_id,NEW.audit_id,payload_bytes,encode(sha256(payload_bytes),'hex'));
    RETURN NEW;
END $$;
REVOKE ALL ON FUNCTION sonn_governance.capture_audit() FROM PUBLIC;
CREATE TRIGGER audit_capture AFTER INSERT ON sonn_governance.audit FOR EACH ROW EXECUTE FUNCTION sonn_governance.capture_audit();

-- Migration-owner-only historical copy under the existing exclusive migration
-- transaction. FORCE is restored before commit; runtime never receives bypass.
ALTER TABLE sonn_governance.audit NO FORCE ROW LEVEL SECURITY;
INSERT INTO sonn_governance.audit_outbox(tenant_id,audit_id,payload,sha256)
SELECT tenant_id,audit_id,payload,encode(sha256(payload),'hex') FROM (
    SELECT tenant_id,audit_id,convert_to(jsonb_build_object(
        'version',1,'tenant_id',tenant_id,'audit_id',audit_id,'actor_id',actor_id,
        'operation',operation,'project_id',project_id,'resource_revision',resource_revision,
        'decision_id',decision_id,'semantics_sha256',semantics_sha256,'occurred_at',occurred_at)::text,'UTF8') AS payload
    FROM sonn_governance.audit
) existing;
ALTER TABLE sonn_governance.audit FORCE ROW LEVEL SECURITY;
DO $$
DECLARE table_name text;
BEGIN
    FOREACH table_name IN ARRAY ARRAY['audit_outbox','archive_deliveries','archive_requirements'] LOOP
        EXECUTE format('ALTER TABLE sonn_governance.%I ENABLE ROW LEVEL SECURITY', table_name);
        EXECUTE format('ALTER TABLE sonn_governance.%I FORCE ROW LEVEL SECURITY', table_name);
        EXECUTE format('CREATE POLICY tenant_isolation ON sonn_governance.%I '
            'USING (tenant_id=nullif(current_setting(''sonn.tenant_id'',true),'''')::uuid) '
            'WITH CHECK (tenant_id=nullif(current_setting(''sonn.tenant_id'',true),'''')::uuid)', table_name);
    END LOOP;
END $$;
UPDATE sonn_governance.schema_version SET version=8 WHERE singleton;
