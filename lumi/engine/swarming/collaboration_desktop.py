"""Personal collaboration UI adapter over captured desktop run owners.

No peer discovery, transcript copying or credential serialization. The empty
preparation mode keeps a lease but launches no provider; accepted work uses the
receiver's immutable model and its own remaining request allowance.
"""
from __future__ import annotations

import copy
from dataclasses import replace
import hashlib
import uuid

from .collaboration import CollaborationTerms, SwarmCollaboration
from .models import AttemptContext, Conflict, ScopeDenied, require_id
from .policy import PolicyProfile, normalize_scopes
from .recovery import record_run_host
from .supervisor import SwarmSupervisor
from .workers import SwarmWorkerRunner

TOOLS = frozenset({"file_read", "glob", "grep", "artifact_read", "swarm_status", "swarm_send", "swarm_receive", "swarm_submit"})
FIELDS = {"receiver_session_id", "receiver_run_id", "terms", "grant_id", "terms_sha256", "message_id",
          "kind", "data_class", "body", "artifact_ids", "parent_id", "read_roots", "requests",
          "before_grant_id", "before_message_id"}
ACTIONS = {"collaboration_prepare", "collaboration_offer", "collaboration_approve", "collaboration_send",
           "collaboration_deliver", "collaboration_accept_work", "collaboration_revoke", "collaboration_inspect"}


def _personal(scope):
    if scope.tenant_id != f"personal:{scope.owner_id}":
        raise ScopeDenied("This collaboration preview supports personal conversations only")


def navigation_idle(snapshot):
    """Unknown effects retain workspace ownership even after a thread exited."""
    if any(row["process_state"] != "stopped" or row["state"] in {"leased", "running", "uncertain"}
           for row in snapshot["attempts"]):
        return False
    if any(row["state"] in {"reserved", "started", "uncertain"} for row in snapshot["model_requests"]):
        return False
    if any(row["state"] != "completed" for row in snapshot["action_receipts"]):
        return False
    if any(row["state"] != "settled" for row in snapshot["reservations"]):
        return False
    return not any(row["state"] in {"queued", "running", "uncertain"} for row in snapshot.get("integration_operations", []))


def prepare(runtime, capture, store, message, *, managed=False):
    """Create one explicitly budgeted, read-only idle team in this conversation."""
    if not managed:
        _personal(capture.scope)
    elif runtime._execution_mode(capture) != "managed" or message.get("execution_mode") != "managed":
        raise ScopeDenied("Select managed execution explicitly before preparing shared work")
    if runtime.settings.get("swarming", "version", 1) != 1 or runtime.settings.get("swarming", "enabled", False) is not True:
        raise Conflict("Enable the team preview before preparing collaboration")
    spec = capture.backend_spec
    connections = runtime.team_model(spec)
    objective, allowance = message.get("objective"), message.get("request_limit")
    if type(objective) is not str or not objective.strip() or len(objective.encode()) > 8192:
        raise ValueError("Describe a collaboration objective of at most 8 KiB")
    if type(allowance) is not int or not 1 <= allowance <= 1000:
        raise ValueError("Choose one to 1000 total model requests")
    run_id = "collaboration_" + hashlib.sha256(message["request_id"].encode()).hexdigest()
    setup = {"mode": "managed_collaboration" if managed else "personal_collaboration", "objective": objective, "request_limit": allowance,
             "model": {"provider": spec.backend_type, "model": spec.model}}
    if managed:
        setup["execution_mode"] = "managed"
    existing = runtime._runners.get(run_id)
    if existing:
        runtime.captured_run(run_id, capture.workspace, capture.scope.session_id)
        if existing[0].scope != capture.scope:
            raise ScopeDenied("Prepared collaboration belongs to another captured owner")
        with store._connection() as connection:
            if runtime.store_setup(connection, store, run_id, message["request_id"], setup) is None:
                raise Conflict("Inspect the retained preparation before retrying")
        return run_id
    runtime._refresh_ownership()
    if runtime._storage_uncertain or runtime._discovery_errors or runtime._active - runtime._collaboration_idle:
        raise Conflict("Stop or reconcile active work before preparing another conversation")
    if len(runtime._active) >= 8:
        raise Conflict("Stop an idle collaboration team before preparing another (maximum eight)")
    latest = runtime._latest(store, capture.scope)
    if latest and store.snapshot(capture.scope, latest)["run"]["state"] not in {"completed", "cancelled", "failed"}:
        raise Conflict("This conversation already has a retained active team")
    spec = copy.deepcopy(spec)
    spec.api_key = spec.resolve_api_key(runtime.settings)
    capture = replace(capture, backend_spec=spec)
    supervisor = SwarmSupervisor(store)
    authority = supervisor.create(capture.scope, supervisor_id=uuid.uuid4().hex, objective=objective,
        request_limit=allowance, policy=PolicyProfile(1, TOOLS, frozenset({spec.backend_type}), max_workers=2),
        run_id=run_id, lease_seconds=60)
    runtime._active.add(run_id)
    runner = None
    try:
        record_run_host(store, authority)
        attachment = runtime._managed_desktop.attach(authority, capture.workspace, runtime._state_root(capture.workspace)) if managed else None
        if attachment is not None:
            runtime._managed_attachments[run_id] = attachment
            attachment.effects(store)
        runner = SwarmWorkerRunner(supervisor, authority, capture.workspace, backend_factory=runtime._factory,
            project_instructions=capture.instructions, managed_readers=runtime._managed_readers,
            exclusions=runtime.exclusions_for(capture.workspace), connections=connections,
            managed_runtime=attachment.runtime if attachment is not None else None,
            # Budgets, usage and audit (organization.py); a policy refuses shared work.
            governance=runtime._governance(capture, run_id, setup))
        runtime._runners[run_id] = (capture, runner)
        if attachment is not None:
            from .managed_collaboration_desktop import ManagedSharing
            runtime._managed_sharing[run_id] = ManagedSharing(runtime, capture, store, runner, attachment)
        with store._connection(write=True) as connection:
            store._remember(connection, run_id, "desktop-setup", message["request_id"], setup, {"run_id": run_id})
        runner.keep_owned()
        runtime._collaboration_runs.add(run_id)
        runtime._collaboration_idle.add(run_id)
        if attachment is not None:
            attachment.start_pump(runner, lambda: store.snapshot(authority.scope, authority.run_id))
    except BaseException:
        if runner:
            runner.stop()
        else:
            runtime._command(supervisor, authority, "stop", {})
        raise
    return run_id


def operate(runtime, capture, store, message):
    """Caller holds the runtime control lock; no automatic replay of dispatch."""
    action, run_id = message["action"], message.get("run_id")
    if action == "collaboration_prepare":
        return prepare(runtime, capture, store, message)
    _personal(capture.scope)
    if action == "collaboration_inspect":
        return run_id  # Read-only scoped projection below also works after Stop.
    pair = runtime._runners.get(run_id)
    if pair is None or pair[0].scope != capture.scope:
        raise Conflict("This conversation needs its current team host before collaboration changes")
    runner = pair[1]
    core = SwarmCollaboration(runner.supervisor)
    args = {"command_id": message["request_id"], "expected_revision": message.get("expected_revision")}
    if action == "collaboration_offer":
        receiver_session_id = message.get("receiver_session_id")
        require_id(receiver_session_id)
        core.offer(runner.authority, **args, receiver_scope=replace(capture.scope, session_id=receiver_session_id),
                   receiver_run_id=message.get("receiver_run_id"), terms=CollaborationTerms.from_dict(message.get("terms")))
    elif action == "collaboration_approve":
        core.accept_grant(runner.authority, **args, grant_id=message.get("grant_id"), terms_sha256=message.get("terms_sha256"))
    elif action == "collaboration_send":
        artifacts = message.get("artifact_ids", [])
        if type(artifacts) is not list:
            raise ValueError("Artifact references must be an explicit list")
        core.send(runner.authority, **args, grant_id=message.get("grant_id"), kind=message.get("kind"),
                  data_class=message.get("data_class"), body=message.get("body", ""), artifact_ids=tuple(artifacts), parent_id=message.get("parent_id"))
    elif action == "collaboration_deliver":
        core.deliver(runner.authority, **args, message_id=message.get("message_id"))
    elif action == "collaboration_revoke":
        core.revoke(runner.authority, **args, grant_id=message.get("grant_id"), evidence=message.get("evidence"))
    elif action == "collaboration_accept_work":
        roots = message.get("read_roots")
        if type(roots) is not list or not roots:
            raise ValueError("Choose explicit readable project folders for accepted work")
        key = hashlib.sha256(message["request_id"].encode()).hexdigest()
        work = {"id": "collab_work_" + key, "objective": message.get("objective"), "role": "explore",
                "read_roots": list(normalize_scopes(tuple(roots))), "write_roots": [],
                "tools": sorted(TOOLS), "criteria": ["owner_review"]}
        # A fresh acceptance may launch only when every other captured workspace
        # owner is idle. Its own normal slot/scope/budget rules are still checked.
        runtime._refresh_ownership()
        if (runtime._active - runtime._collaboration_idle) - {run_id}:
            raise Conflict("Another team owns active work; wait before accepting this assignment")
        # It starts a participant: the organization's rules and the budgets
        # decide first, before the claim is committed (organization.py).
        refusal = runtime.team_dispatch_refusal(run_id)
        if refusal:
            raise Conflict(refusal)
        result = core.accept_work(runner.authority, **args, message_id=message.get("message_id"), work_item=work,
            model={"provider": pair[0].backend_spec.backend_type, "model": pair[0].backend_spec.model},
            requests=message.get("requests"), worker_id="collab_worker_" + key, evidence=message.get("evidence")).result
        attempt = result["claim"]
        runtime._collaboration_idle.discard(run_id)
        # The durable claim can survive a lost launch acknowledgement. Never
        # reconstruct or launch it again on a replay; recovery stays explicit.
        with store._connection(write=True) as connection:
            launched = store._duplicate(connection, run_id, "collaboration-dispatch", message["request_id"], {"attempt_id": attempt["attempt_id"]})
            if launched is None:
                store._remember(connection, run_id, "collaboration-dispatch", message["request_id"],
                                {"attempt_id": attempt["attempt_id"]}, {"launch_attempted": True})
        if launched is None:
            context = AttemptContext(capture.scope, run_id, attempt["attempt_id"], attempt["worker_id"], attempt["epoch"])
            runner.start(context, pair[0].backend_spec)
    return run_id


def view(store, scope, run_id, *, grant_id=None, before_grant_id=None, before_message_id=None):
    """Bounded owner history. Incoming content stays hidden until explicit delivery."""
    _personal(scope)
    core = SwarmCollaboration(SwarmSupervisor(store))
    with store._connection() as connection:
        run = store._run(connection, scope, run_id)
        cursor = 2**63 - 1
        if before_grant_id:
            core._grant(connection, run, before_grant_id, live=False)
            cursor = connection.execute("SELECT rowid FROM collaboration_grants WHERE id=?", (before_grant_id,)).fetchone()[0]
        rows = connection.execute("SELECT rowid,* FROM collaboration_grants WHERE (origin_run_id=? OR receiver_run_id=?) AND rowid<? ORDER BY rowid DESC LIMIT 21",
                                  (run_id, run_id, cursor)).fetchall()
        grants = []
        for row in rows[:20]:
            grant, terms = core._grant(connection, run, row["id"], live=False)
            grants.append({"grant_id": grant["id"], "purpose": terms.purpose, "state": grant["state"], "expires_at": terms.expires_at,
                           "origin_run_id": grant["origin_run_id"], "receiver_run_id": grant["receiver_run_id"]})
        result = {"address": {"session_id": scope.session_id, "run_id": run_id}, "grants": grants,
                  "next_before_grant_id": rows[19]["id"] if len(rows) > 20 else None, "detail": None,
                  "accepted_work_item_ids": [row[0] for row in connection.execute(
                      "SELECT work_item_id FROM collaboration_acceptances WHERE receiver_run_id=? ORDER BY rowid LIMIT 256", (run_id,))]}
        if grant_id:
            grant, terms = core._grant(connection, run, grant_id, live=False)
            message_cursor = 2**63 - 1
            if before_message_id:
                row = connection.execute("SELECT rowid FROM collaboration_messages WHERE id=? AND grant_id=?", (before_message_id, grant_id)).fetchone()
                if row is None:
                    raise ScopeDenied("Message history cursor is unavailable")
                message_cursor = row[0]
            messages = connection.execute("SELECT rowid,* FROM collaboration_messages WHERE grant_id=? AND rowid<? ORDER BY rowid DESC LIMIT 21", (grant_id, message_cursor)).fetchall()
            content = []
            for row in messages[:20]:
                core._verify_message(row)
                item = core._message_view(row, content=row["sender_run_id"] == run_id or row["delivered_at"] is not None)
                accepted = connection.execute("SELECT work_item_id,attempt_id,request_allowance FROM collaboration_acceptances WHERE message_id=?", (row["id"],)).fetchone()
                item["acceptance"] = dict(accepted) if accepted else None
                content.append(item)
            result["detail"] = {"grant_id": grant_id, "terms": terms.to_dict(), "terms_sha256": grant["terms_sha256"],
                "state": grant["state"], "origin_run_id": grant["origin_run_id"], "receiver_run_id": grant["receiver_run_id"],
                "messages": content, "next_before_message_id": messages[19]["id"] if len(messages) > 20 else None}
        return result
