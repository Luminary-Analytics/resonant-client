ALTER TABLE sonn_governance.archive_requirements ADD COLUMN archive_id uuid;
ALTER TABLE sonn_governance.archive_requirements ADD COLUMN source_id uuid;
ALTER TABLE sonn_governance.archive_deliveries ADD COLUMN source_id uuid;
ALTER TABLE sonn_governance.archive_requirements ALTER COLUMN max_delay_seconds DROP NOT NULL;

-- Historical unbound requirements are intentionally not guessed. A custodian
-- must configure exact source/destination identities; unknown old receipts do
-- not satisfy a newly pinned requirement.
CREATE FUNCTION sonn_governance.check_archive_binding() RETURNS trigger
    LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,sonn_governance AS $$
BEGIN
    IF NEW.operation IN ('issue_lease','reserve_request','start_request','register_run','publish_content','read_content')
       AND EXISTS(SELECT 1 FROM sonn_governance.archive_requirements r
           WHERE r.tenant_id=NEW.tenant_id AND
             (r.archive_id IS NULL OR r.source_id IS NULL OR
              EXISTS(SELECT 1 FROM sonn_governance.audit_outbox o
                     LEFT JOIN sonn_governance.archive_deliveries d
                       ON d.tenant_id=o.tenant_id AND d.audit_id=o.audit_id
                      AND d.archive_id=r.archive_id AND d.source_id=r.source_id AND d.sha256=o.sha256
                     WHERE o.tenant_id=r.tenant_id AND d.audit_id IS NULL
                       AND o.queued_at<clock_timestamp()-(r.max_delay_seconds * interval '1 second')))) THEN
        RAISE EXCEPTION 'required audit archival binding is unavailable' USING ERRCODE='55000';
    END IF;
    RETURN NEW;
END $$;
REVOKE ALL ON FUNCTION sonn_governance.check_archive_binding() FROM PUBLIC;
CREATE TRIGGER audit_archive_binding_gate BEFORE INSERT ON sonn_governance.audit FOR EACH ROW EXECUTE FUNCTION sonn_governance.check_archive_binding();
UPDATE sonn_governance.schema_version SET version=9 WHERE singleton;
