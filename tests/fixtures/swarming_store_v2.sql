-- Pinned pre-collaboration schema 2, captured before migration implementation.
PRAGMA application_id=1398227282;
PRAGMA user_version=2;
CREATE TABLE runs (
        id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, owner_id TEXT NOT NULL,
        project_id TEXT NOT NULL, session_id TEXT NOT NULL,
        supervisor_id TEXT NOT NULL, epoch INTEGER NOT NULL CHECK(epoch > 0),
        protocol_version INTEGER NOT NULL, objective TEXT NOT NULL,
        state TEXT NOT NULL CHECK(state IN ('running','pausing','paused','stopping','cancelled','failed','completed','recovery_required')),
        request_limit INTEGER NOT NULL CHECK(request_limit >= 0),
        event_sequence INTEGER NOT NULL DEFAULT 0,
        managed INTEGER NOT NULL DEFAULT 0, revision INTEGER NOT NULL DEFAULT 0,
        lease_until REAL NOT NULL DEFAULT 0, lease_seconds REAL NOT NULL DEFAULT 30,
        policy_json TEXT NOT NULL DEFAULT '{}', stop_requested INTEGER NOT NULL DEFAULT 0,
        worker_limit INTEGER NOT NULL DEFAULT 2 CHECK(worker_limit BETWEEN 1 AND 4));

CREATE TABLE work_items (
        id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(id),
        objective TEXT NOT NULL, state TEXT NOT NULL
        CHECK(state IN ('pending','ready','leased','running','submitted','accepted','failed','cancelled','uncertain')),
        specification TEXT NOT NULL DEFAULT '{}', revision INTEGER NOT NULL DEFAULT 1,
        UNIQUE(run_id,id));

CREATE TABLE attempts (
        id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(id),
        work_item_id TEXT, worker_id TEXT NOT NULL, epoch INTEGER NOT NULL,
        state TEXT NOT NULL CHECK(state IN ('leased','running','submitted','completed','failed','cancelled','uncertain')),
        grant_json TEXT NOT NULL DEFAULT '{}', process_state TEXT NOT NULL DEFAULT 'pending'
        CHECK(process_state IN ('pending','running','stopped','unknown')),
        kind TEXT NOT NULL DEFAULT 'worker' CHECK(kind IN ('worker','coordinator')),
        pause_requested INTEGER NOT NULL DEFAULT 0 CHECK(pause_requested IN (0,1)),
        cancel_requested INTEGER NOT NULL DEFAULT 0 CHECK(cancel_requested IN (0,1)),
        CHECK((kind='worker' AND work_item_id IS NOT NULL) OR (kind='coordinator' AND work_item_id IS NULL)),
        CHECK(state!='completed' OR kind='coordinator'),
        FOREIGN KEY(run_id,work_item_id) REFERENCES work_items(run_id,id),
        UNIQUE(run_id,id));

CREATE UNIQUE INDEX active_work_claim ON attempts(work_item_id)
        WHERE state IN ('leased','running','uncertain');

CREATE UNIQUE INDEX active_worker_claim ON attempts(run_id,worker_id)
        WHERE state IN ('leased','running','uncertain');

CREATE UNIQUE INDEX active_coordinator_claim ON attempts(run_id)
        WHERE kind='coordinator' AND state IN ('leased','running','uncertain');

CREATE TABLE reservations (
        id TEXT PRIMARY KEY, attempt_id TEXT NOT NULL UNIQUE REFERENCES attempts(id),
        amount INTEGER NOT NULL CHECK(amount > 0), used INTEGER,
        state TEXT NOT NULL CHECK(state IN ('reserved','settled','uncertain')),
        CHECK((state='settled' AND used >= 0 AND used <= amount) OR
              (state IN ('reserved','uncertain') AND used IS NULL)));

CREATE TABLE dispatches (
        id TEXT PRIMARY KEY, attempt_id TEXT NOT NULL UNIQUE REFERENCES attempts(id),
        state TEXT NOT NULL CHECK(state IN ('pending','started','finished','uncertain')));

CREATE TABLE events (
        run_id TEXT NOT NULL REFERENCES runs(id), sequence INTEGER NOT NULL,
        epoch INTEGER NOT NULL, kind TEXT NOT NULL, payload TEXT NOT NULL, occurred_at REAL, run_state TEXT,
        PRIMARY KEY(run_id,sequence));

CREATE TABLE commands (
        run_id TEXT NOT NULL REFERENCES runs(id), actor TEXT NOT NULL, key TEXT NOT NULL,
        payload TEXT NOT NULL, result TEXT NOT NULL, PRIMARY KEY(run_id,actor,key));

CREATE TABLE messages (
        id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(id), sequence INTEGER NOT NULL,
        sender_attempt_id TEXT NOT NULL, recipient_attempt_id TEXT NOT NULL,
        epoch INTEGER NOT NULL, kind TEXT NOT NULL, body TEXT NOT NULL, reply_to TEXT,
        FOREIGN KEY(run_id,sender_attempt_id) REFERENCES attempts(run_id,id),
        FOREIGN KEY(run_id,recipient_attempt_id) REFERENCES attempts(run_id,id),
        FOREIGN KEY(reply_to) REFERENCES messages(id), UNIQUE(run_id,sequence));

CREATE INDEX recipient_messages ON messages(recipient_attempt_id,sequence);

CREATE TABLE receipts (
        message_id TEXT NOT NULL REFERENCES messages(id),
        recipient_attempt_id TEXT NOT NULL REFERENCES attempts(id), epoch INTEGER NOT NULL,
        stage TEXT NOT NULL CHECK(stage IN ('runtime','context')),
        model_request_id TEXT, input_revision INTEGER,
        PRIMARY KEY(message_id,stage),
        CHECK((stage='runtime' AND model_request_id IS NULL AND input_revision IS NULL) OR
              (stage='context' AND model_request_id IS NOT NULL AND input_revision > 0)));

CREATE TABLE work_dependencies (
        run_id TEXT NOT NULL, work_item_id TEXT NOT NULL, dependency_id TEXT NOT NULL,
        PRIMARY KEY(run_id,work_item_id,dependency_id),
        FOREIGN KEY(run_id,work_item_id) REFERENCES work_items(run_id,id),
        FOREIGN KEY(run_id,dependency_id) REFERENCES work_items(run_id,id));

CREATE TABLE model_requests (
        id TEXT PRIMARY KEY, attempt_id TEXT NOT NULL REFERENCES attempts(id),
        epoch INTEGER NOT NULL, purpose TEXT NOT NULL CHECK(purpose IN ('main','auxiliary')),
        state TEXT NOT NULL CHECK(state IN ('reserved','started','completed','failed','not_started','uncertain')),
        used INTEGER CHECK(used IN (0,1)),
        CHECK((state IN ('completed','failed','not_started') AND used IS NOT NULL)
            OR (state IN ('reserved','started','uncertain') AND used IS NULL)));

CREATE TABLE submissions (
        attempt_id TEXT PRIMARY KEY REFERENCES attempts(id), candidate_revision TEXT NOT NULL,
        handoff TEXT NOT NULL, work_revision INTEGER NOT NULL);

CREATE TABLE check_receipts (
        id TEXT PRIMARY KEY, attempt_id TEXT NOT NULL REFERENCES attempts(id),
        criterion_id TEXT NOT NULL, candidate_revision TEXT NOT NULL,
        executor_id TEXT NOT NULL, check_name TEXT NOT NULL, exit_code INTEGER NOT NULL,
        evidence TEXT NOT NULL);

CREATE TABLE artifact_refs (
        id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(id),
        attempt_id TEXT NOT NULL REFERENCES attempts(id), epoch INTEGER NOT NULL,
        origin TEXT NOT NULL, model_request_id TEXT, tool_call_id TEXT,
        sha256 TEXT NOT NULL, size INTEGER NOT NULL CHECK(size>=0), kind TEXT NOT NULL,
        media_type TEXT NOT NULL, label TEXT NOT NULL, UNIQUE(run_id,id));

CREATE TABLE artifact_grants (
        artifact_id TEXT NOT NULL REFERENCES artifact_refs(id),
        recipient_attempt_id TEXT NOT NULL REFERENCES attempts(id),
        revoked INTEGER NOT NULL DEFAULT 0 CHECK(revoked IN (0,1)),
        PRIMARY KEY(artifact_id,recipient_attempt_id));

CREATE TABLE request_inputs (
        request_id TEXT PRIMARY KEY REFERENCES model_requests(id), input_sha256 TEXT NOT NULL,
        purpose TEXT NOT NULL, input_artifact_id TEXT REFERENCES artifact_refs(id),
        usage_json TEXT, observation_error TEXT NOT NULL DEFAULT '',
        observation_outcome TEXT CHECK(observation_outcome IN ('completed','uncertain')));

CREATE TABLE owner_directives (
        id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(id),
        attempt_id TEXT NOT NULL REFERENCES attempts(id), epoch INTEGER NOT NULL,
        owner_id TEXT NOT NULL, text TEXT NOT NULL, sha256 TEXT NOT NULL);

CREATE TABLE owner_directive_receipts (
        directive_id TEXT PRIMARY KEY REFERENCES owner_directives(id),
        attempt_id TEXT NOT NULL REFERENCES attempts(id), epoch INTEGER NOT NULL,
        request_id TEXT NOT NULL REFERENCES request_inputs(request_id),
        input_sha256 TEXT NOT NULL, input_revision INTEGER NOT NULL CHECK(input_revision>0));

CREATE TABLE action_receipts (
        id TEXT PRIMARY KEY, attempt_id TEXT NOT NULL REFERENCES attempts(id), epoch INTEGER NOT NULL,
        request_id TEXT NOT NULL REFERENCES model_requests(id), call_id TEXT NOT NULL,
        tool_name TEXT NOT NULL, arguments_sha256 TEXT NOT NULL,
        state TEXT NOT NULL CHECK(state IN ('admitted','completed','uncertain')),
        output_artifact_id TEXT REFERENCES artifact_refs(id), is_error INTEGER,
        metadata_json TEXT, UNIQUE(request_id,call_id));

CREATE TABLE writer_worktrees (
        id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(id),
        attempt_id TEXT NOT NULL UNIQUE REFERENCES attempts(id), epoch INTEGER NOT NULL,
        repo_key TEXT NOT NULL, path TEXT NOT NULL, base_revision TEXT NOT NULL,
        result_revision TEXT NOT NULL DEFAULT '', state TEXT NOT NULL,
        manifest_json TEXT NOT NULL,
        process_protocol INTEGER NOT NULL DEFAULT 0 CHECK(process_protocol IN (0,1)));

CREATE TABLE integration_candidates (
        id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(id), epoch INTEGER NOT NULL,
        repo_key TEXT NOT NULL, path TEXT NOT NULL, base_revision TEXT NOT NULL,
        result_revision TEXT NOT NULL DEFAULT '', state TEXT NOT NULL,
        manifest_json TEXT NOT NULL,
        process_protocol INTEGER NOT NULL DEFAULT 0 CHECK(process_protocol IN (0,1)));

CREATE TABLE integration_checks (
        id TEXT PRIMARY KEY, candidate_id TEXT NOT NULL REFERENCES integration_candidates(id),
        check_key TEXT NOT NULL, candidate_revision TEXT NOT NULL, argv_json TEXT NOT NULL,
        state TEXT NOT NULL, exit_code INTEGER, output TEXT NOT NULL DEFAULT '',
        job_id TEXT NOT NULL DEFAULT '',
        process_protocol INTEGER NOT NULL DEFAULT 0 CHECK(process_protocol IN (0,1)));

CREATE TABLE integration_applications (
        id TEXT PRIMARY KEY, candidate_id TEXT NOT NULL REFERENCES integration_candidates(id),
        expected_base TEXT NOT NULL, target_revision TEXT NOT NULL,
        state TEXT NOT NULL, observed_revision TEXT NOT NULL DEFAULT '',
        approval_json TEXT NOT NULL,
        process_protocol INTEGER NOT NULL DEFAULT 0 CHECK(process_protocol IN (0,1)));

CREATE TABLE integration_processes (
        id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(id), epoch INTEGER NOT NULL,
        effect_kind TEXT NOT NULL CHECK(effect_kind IN ('writer','candidate','check','application')),
        effect_id TEXT NOT NULL, argv_sha256 TEXT NOT NULL, cwd TEXT NOT NULL,
        host_id TEXT NOT NULL, pid INTEGER CHECK(pid>0), created_at REAL, launch_token TEXT,
        state TEXT NOT NULL CHECK(state IN ('intent','owned','invoked','stopped','unknown','not_started')),
        invoked INTEGER NOT NULL DEFAULT 0 CHECK(invoked IN (0,1)), exit_code INTEGER,
        CHECK((pid IS NULL AND created_at IS NULL AND launch_token IS NULL) OR
              (pid IS NOT NULL AND created_at IS NOT NULL AND launch_token IS NOT NULL)));

CREATE TABLE integration_operations (
        id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(id),
        supervisor_id TEXT NOT NULL, epoch INTEGER NOT NULL, command_id TEXT NOT NULL,
        expected_revision INTEGER NOT NULL, kind TEXT NOT NULL, payload_json TEXT NOT NULL,
        effect_id TEXT NOT NULL, approval_expires_at REAL,
        state TEXT NOT NULL CHECK(state IN ('queued','running','completed','failed','cancelled','uncertain')),
        result_json TEXT NOT NULL DEFAULT '{}', error TEXT NOT NULL DEFAULT '',
        UNIQUE(run_id,supervisor_id,command_id));

CREATE TABLE writer_acceptances (
        attempt_id TEXT PRIMARY KEY REFERENCES attempts(id),
        writer_id TEXT NOT NULL REFERENCES writer_worktrees(id),
        candidate_id TEXT NOT NULL REFERENCES integration_candidates(id),
        application_id TEXT NOT NULL REFERENCES integration_applications(id),
        work_revision INTEGER NOT NULL, writer_revision TEXT NOT NULL,
        candidate_revision TEXT NOT NULL, checks_json TEXT NOT NULL,
        evidence TEXT NOT NULL, owner_id TEXT NOT NULL, epoch INTEGER NOT NULL);

CREATE TABLE process_observations (
        attempt_id TEXT PRIMARY KEY REFERENCES attempts(id), run_id TEXT NOT NULL REFERENCES runs(id),
        epoch INTEGER NOT NULL, host_id TEXT NOT NULL, pid INTEGER NOT NULL CHECK(pid>0),
        created_at REAL NOT NULL, launch_token TEXT NOT NULL,
        state TEXT NOT NULL CHECK(state IN ('started','stopped','unknown')), exit_code INTEGER);

CREATE TABLE coordinator_proposals (
        id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(id),
        attempt_id TEXT NOT NULL REFERENCES attempts(id), request_id TEXT NOT NULL REFERENCES model_requests(id),
        epoch INTEGER NOT NULL, input_revision INTEGER NOT NULL, sha256 TEXT NOT NULL,
        payload_json TEXT NOT NULL, state TEXT NOT NULL CHECK(state IN ('pending','accepted','rejected')),
        decision_evidence TEXT NOT NULL DEFAULT '');

CREATE TABLE coordinator_inputs (
        request_id TEXT PRIMARY KEY REFERENCES model_requests(id),
        attempt_id TEXT NOT NULL REFERENCES attempts(id), input_sha256 TEXT NOT NULL,
        prompt_sha256 TEXT NOT NULL, graph_sha256 TEXT NOT NULL,
        policy_digest TEXT NOT NULL, criteria_json TEXT NOT NULL);

CREATE TABLE run_hosts (
        run_id TEXT NOT NULL REFERENCES runs(id), epoch INTEGER NOT NULL CHECK(epoch>0),
        host_id TEXT NOT NULL, pid INTEGER NOT NULL CHECK(pid>0), created_at REAL NOT NULL,
        PRIMARY KEY(run_id,epoch));
