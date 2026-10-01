"""Crash an isolated fixture host after durable admission, before any provider call.

The browser exercises recovery against its real saved process identity. This is
an injected interruption fixture, never evidence about paid provider accounting.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sys
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from lumi.engine.swarming import AttemptContext, Command, Scope, SwarmStore, SwarmSupervisor
from lumi.engine.swarming.execution import SwarmExecutionGuard
from lumi.engine.swarming.policy import AssignmentGrant, PolicyProfile
from lumi.engine.swarming.recovery import record_run_host


def main() -> None:
    data = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    store = SwarmStore(data["store_path"])
    supervisor = SwarmSupervisor(store)
    scope = Scope(**data["scope"])
    authority = supervisor.create(scope, supervisor_id="fixture-original-host", run_id="fixture-crashed-team",
        objective="Recover interrupted CSV investigations", request_limit=20, lease_seconds=1,
        policy=PolicyProfile(1, frozenset({"file_read"}), frozenset({"ollama"}), max_workers=2))
    record_run_host(store, authority)

    def command(kind, **payload):
        return supervisor.handle(Command(uuid.uuid4().hex, authority.run_id,
            store.snapshot(scope, authority.run_id)["run"]["revision"], authority.epoch, kind, payload), authority)

    with store._connection(write=True) as connection:
        store._remember(connection, authority.run_id, "desktop-setup", "fixture-setup",
            {"model": {"provider": "ollama", "model": "fixture-native"}, "worker_requests": 4}, {"run_id": authority.run_id})
    command("plan", work_items=[{"id": f"recovery-work-{index}", "objective": f"Recovered CSV investigation {index}",
        "role": "explore", "tools": ["file_read"], "read_roots": ["."], "criteria": ["owner_review"]} for index in (1, 2, 3)])
    for index in (1, 2):
        assignment = command("assign", work_item_id=f"recovery-work-{index}", worker_id=f"fixture-worker-{index}",
            requests=4, model={"provider": "ollama", "model": "fixture-native"}).result
        context = AttemptContext(scope, authority.run_id, assignment["attempt_id"], assignment["worker_id"], authority.epoch)
        command("worker_started", attempt_id=context.attempt_id, attempt_epoch=context.epoch)
        if index == 1:
            command("reserve_request", attempt_id=context.attempt_id, attempt_epoch=context.epoch,
                request_id="fixture-unstarted-request", purpose="main")
            continue  # Crash leaves this reservation without any dispatch receipt.
        grant = AssignmentGrant.from_dict(json.loads(store.snapshot(scope, authority.run_id)["attempts"][-1]["grant_json"]))
        guard = SwarmExecutionGuard(supervisor, authority, context, data["workspace"], grant)
        request_id = guard.begin_request(purpose="primary", inputs={"_model_selection": {"provider": "ollama", "model": "fixture-native"},
            "conversation_history": [{"role": "user", "input_origin": "generated", "content": "Injected recovery fixture; no provider is invoked."}]})
        guard.end_request(request_id, outcome="completed", usage=None, error="")
        arguments = {"path": "fact.txt"}
        digest = hashlib.sha256(json.dumps(arguments, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
        guard.begin_tool(request_id, "fixture-admitted-read", "file_read", arguments, digest)
    Path(data["trace_path"]).write_text("Fixture stopped before any provider invocation and before the admitted file read.\n", encoding="utf-8")
    os._exit(0)  # Deliberately omit all worker/request/action cleanup observations.


if __name__ == "__main__":
    main()
