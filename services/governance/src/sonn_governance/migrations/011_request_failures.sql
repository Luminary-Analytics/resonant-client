-- A confirmed failed invocation consumes one request without becoming success.
ALTER TABLE sonn_governance.host_requests DROP CONSTRAINT host_requests_state_check;
ALTER TABLE sonn_governance.host_requests DROP CONSTRAINT host_requests_check;
ALTER TABLE sonn_governance.host_requests ADD CONSTRAINT host_requests_state_check
    CHECK(state IN ('reserved','started','uncertain','completed','failed','never_started'));
ALTER TABLE sonn_governance.host_requests ADD CONSTRAINT host_requests_check
    CHECK((state IN ('completed','failed') AND started AND consumed=1)
        OR (state='never_started' AND NOT started AND consumed=0)
        OR (state IN ('reserved','started','uncertain') AND consumed IS NULL));
UPDATE sonn_governance.schema_version SET version=11 WHERE singleton;
