ALTER TABLE sonn_governance.hosts ADD COLUMN enrolled_by text;
ALTER TABLE sonn_governance.hosts ADD FOREIGN KEY(tenant_id,enrolled_by)
    REFERENCES sonn_governance.memberships(tenant_id,actor_id);
-- Recover only a retained exact enrollment decision. A legacy row without this
-- proof stays NULL and cannot acquire new authority; no owner is guessed.
-- The migration owner can update all tenants inside this exclusive DDL
-- transaction. Runtime connections never observe the temporary owner exemption.
ALTER TABLE sonn_governance.hosts NO FORCE ROW LEVEL SECURITY;
ALTER TABLE sonn_governance.command_receipts NO FORCE ROW LEVEL SECURITY;
UPDATE sonn_governance.hosts h SET enrolled_by = (
    SELECT c.actor_id FROM sonn_governance.command_receipts c
    WHERE c.tenant_id=h.tenant_id AND c.result->>'host_id'=h.host_id::text
      AND c.result->>'state'='pending' AND c.result ? 'challenge'
    ORDER BY c.audit_id LIMIT 1
);
ALTER TABLE sonn_governance.hosts FORCE ROW LEVEL SECURITY;
ALTER TABLE sonn_governance.command_receipts FORCE ROW LEVEL SECURITY;
UPDATE sonn_governance.schema_version SET version=4 WHERE singleton;
