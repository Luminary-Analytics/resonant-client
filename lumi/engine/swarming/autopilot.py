"""Let a team's orchestrator run it in rounds, under the owner's start-time grant.

Starting a coordinator team with ``autonomy`` records the owner's choice in the
team's captured setup. From then on this host loop takes the owner steps the
orchestrator (the team's coordinator model) would otherwise wait on:

- it accepts each orchestrator plan. The supervisor still validates the plan
  exactly as for an owner decision: declared scopes, criteria and allowance;
- it accepts each read result under the grant (``accept_under_grant``), so the
  next round can use it. The receipt says so and never claims owner review;
- it retries a failed task, or an orchestrator turn without a usable plan, once;
- with ``apply`` in the grant, once a round's writers have finished it combines
  their changes, runs every declared check on the combined result, applies it
  to the project when all of them pass, and accepts the writers under the grant
  (``accept_writer_under_grant``). A failing check sends the writers back once,
  with its output. Conflicting changes, a step without a known outcome or a
  project checkout that changed hand the team back to the owner;
- when a round settles, it asks the orchestrator to plan again from the
  findings and from workers' messages to it;
- after the last round, it asks for one closing turn that may not start work:
  the orchestrator's final report, after which the team completes.

The orchestrator can also finish early by proposing no more work. Without
``apply``, writers' changes wait for the owner to check and apply them. Every
step goes through the durable commands and integration operations the owner's
own controls use, so the owner can pause, steer or stop the team at any time.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from typing import Any

from .models import AdmissionClosed, Conflict, RevisionConflict

logger = logging.getLogger(__name__)

_TERMINAL = frozenset({"completed", "cancelled", "failed"})
_IN_FLIGHT = frozenset({"pending", "ready", "leased", "running", "submitted"})
_RUNNING = frozenset({"ready", "leased", "running"})
MAX_ROUNDS = 8
PLAN_EVIDENCE = ("Accepted by the team's orchestrator under the owner's autonomy grant: plans within this team's "
                 "scopes, criteria and request allowance run without a separate approval.")
CLOSING_EVIDENCE = "The team used its rounds; the orchestrator's closing turn may not start more work."
RESULT_EVIDENCE = ("Accepted under the owner's autonomy grant so the orchestrator's next round can use it; "
                   "the owner has not reviewed it.")
RETRY_EVIDENCE = "Retried once automatically under the owner's autonomy grant."
APPLY_EVIDENCE = ("Applied under the owner's autonomy grant: every declared check passed on these exact combined "
                  "changes. The owner has not reviewed them.")
WRITER_EVIDENCE = ("Accepted under the owner's autonomy grant: the exact combined changes passed every declared "
                   "check and were applied. The owner has not reviewed them.")
# A failed task waits this long before its one retry: a provider that refused a
# request for its rate limit usually needs a moment.
RETRY_DELAY_SECONDS = 15.0
# What the owner is told when a step on the writers' changes keeps failing.
_STEP_FAILED = {
    "prepare_candidate": "The writers' changes couldn't be combined. Inspect them and continue the team yourself.",
    "run_check": "A check couldn't run on the combined changes. Inspect it and continue the team yourself.",
    "apply_candidate": ("The checked changes couldn't be applied: the project checkout has uncommitted changes or "
                        "moved since the team started. Commit or set aside your work, then apply them yourself."),
}


class _Refused(Exception):
    """An organization policy now refuses team work on this computer."""


class TeamAutopilot:
    """One orchestrator loop for one captured team run.

    ``rounds`` counts rounds of work, the first plan included; the closing
    turn that writes the final report comes after them. ``apply`` is the
    owner's grant to apply writers' changes that pass every declared check.
    """

    TICK = 0.4
    # Consecutive refused steps (about ten seconds) before the owner is told.
    REFUSALS = 25

    def __init__(self, runtime: Any, run_id: str, *, rounds: int, apply: bool = False) -> None:
        if type(rounds) is not int or not 1 <= rounds <= MAX_ROUNDS:
            raise ValueError(f"Choose one to {MAX_ROUNDS} orchestrator rounds")
        if type(apply) is not bool:
            raise TypeError("The grant to apply checked changes must be true or false")
        self.runtime, self.run_id, self.rounds, self.apply = runtime, run_id, rounds, apply
        self.round = 1
        self.closing = False
        self.phase = "planning"
        self.detail = "The orchestrator is planning the first round."
        self.final_report: str | None = None
        self._retried: set[str] = set()
        self._failed_at: dict[str, float] = {}
        self._operations: dict[tuple, int] = {}
        self._requests = 0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # ── lifecycle ─────────────────────────────────────────────────────────

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._loop, daemon=True, name=f"swarm-autopilot-{self.run_id[:12]}")
            self._thread.start()

    def close(self) -> None:
        self._stop.set()
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=2)

    def inspect(self) -> dict[str, Any]:
        return {"enabled": True, "round": self.round, "rounds": self.rounds, "apply": self.apply,
                "closing": self.closing, "phase": self.phase, "detail": self.detail,
                "final_report": self.final_report, "active": bool(self._thread and self._thread.is_alive())}

    def _set(self, phase: str, detail: str) -> None:
        self.phase, self.detail = phase, detail

    def _hand_back(self, detail: str) -> bool:
        self._set("needs_owner", detail)
        return True

    def _loop(self) -> None:
        refused = 0
        while not self._stop.wait(self.TICK):
            try:
                if not self.step():
                    return
                refused = 0
            except (Conflict, RevisionConflict, AdmissionClosed) as exc:
                # The owner or a worker changed the team; look again. A step
                # the runtime keeps refusing is the owner's to resolve.
                refused += 1
                if refused >= self.REFUSALS:
                    self._set("needs_owner", f"The team keeps refusing the orchestrator's next step ({exc}). "
                                             "Review the team and continue it yourself.")
                continue
            except Exception as exc:  # noqa: BLE001 - the owner must see that the loop ended
                logger.exception("Team orchestrator loop stopped")
                self._set("needs_owner", f"The orchestrator loop stopped ({type(exc).__name__}). "
                                         "Review the team and continue it yourself.")
                return

    # ── one pass ──────────────────────────────────────────────────────────

    def step(self) -> bool:
        """Take at most one owner step; False once the loop has nothing left to do."""
        try:
            return self._step()
        except _Refused as exc:
            return self._hand_back(str(exc))

    def _step(self) -> bool:
        pair = self.runtime._runners.get(self.run_id)
        if pair is None:
            self._set("stopped", "This team no longer has its execution host.")
            return False
        capture, runner = pair
        snapshot = runner.store.snapshot(capture.scope, self.run_id)
        state = snapshot["run"]["state"]
        if state in _TERMINAL:
            if self.phase != "finished":
                self._set("stopped", f"The team is {state}.")
            return False
        if state != "running":
            self._set("paused", "The team is paused. Resume it to let the orchestrator continue.")
            return True
        scheduler = self.runtime._schedulers.get(self.run_id)
        error = scheduler.inspect()["error"] if scheduler is not None else ""
        if error:
            # Nothing would start the tasks it was about to dispatch.
            return self._hand_back(f"The team stopped starting workers ({error}). Inspect it and continue it yourself.")
        if any(row["kind"] == "coordinator" and (row["state"] in {"leased", "running", "uncertain"}
               or row["process_state"] != "stopped") for row in snapshot["attempts"]):
            self._set("planning", "The orchestrator is writing its final report." if self.closing
                      else f"Round {self.round}: the orchestrator is planning.")
            return True
        for handled in (self._decide, self._failed_orchestrator, self._accept_results, self._retry_failed):
            if handled(capture, runner, snapshot):
                return True
        work = snapshot["work_items"]
        writers = [row for row in work if row["state"] == "submitted" and json.loads(row["specification"])["write_roots"]]
        if writers and not self.apply:
            return self._hand_back("Writers submitted changes. Check and apply them, then accept them to continue.")
        # Wait for the round's running workers, so their changes combine once.
        if writers and not any(row["state"] in _RUNNING for row in work):
            return self._integrate(capture, runner, snapshot, writers)
        if any(row["state"] in _IN_FLIGHT for row in work):
            running = sum(row["state"] in {"leased", "running"} for row in work)
            self._set("working", f"Round {self.round}: {running} of {len(work)} tasks running.")
            return True
        if any(row["state"] == "uncertain" for row in work) or any(
                row["kind"] == "worker" and row["state"] == "uncertain" for row in snapshot["attempts"]):
            return self._hand_back("A worker's model request ended without a known outcome (for example, the "
                                   "connection dropped mid-answer). Reconcile it or stop the team.")
        if any(row["state"] != "accepted" for row in work):
            return self._hand_back("A task failed after its automatic retry. Retry, change or stop the team.")
        return self._settle(capture, runner, snapshot)

    @staticmethod
    def _refusal(action: str) -> None:
        from .service import policy_refusal  # service.py imports this module
        refusal = policy_refusal(action)
        if refusal:
            raise _Refused(refusal)

    def _command(self, runner, kind: str, payload: dict[str, Any]):
        self._refusal(kind)
        with self.runtime._lock:
            return self.runtime._command(runner.supervisor, runner.authority, kind, payload)

    def _decide(self, capture, runner, snapshot) -> bool:
        pending = [row for row in snapshot["coordinator_proposals"] if row["state"] == "pending"]
        if not pending:
            return False
        proposal = pending[0]
        plan = json.loads(proposal["payload_json"])["plan"]
        # The closing turn may not start work; its summary is the report either way.
        accept = not (self.closing and plan["work_items"])
        self._command(runner, "decide_proposal", {"proposal_id": proposal["id"], "sha256": proposal["sha256"],
                                                  "accept": accept, "evidence": PLAN_EVIDENCE if accept else CLOSING_EVIDENCE})
        if accept and plan["work_items"]:
            scheduler = self.runtime._schedulers.get(self.run_id)
            if scheduler is None:
                raise Conflict("The admitted plan requires this team's dispatch owner")
            scheduler.start()
            self._set("working", f"Round {self.round}: {len(plan['work_items'])} tasks planned. {plan['summary'][:300]}")
        else:
            self.final_report = plan["summary"]
            self._set("finishing", "The orchestrator wrote its final report.")
        return True

    def _failed_orchestrator(self, capture, runner, snapshot) -> bool:
        """An orchestrator turn that ended without a usable plan gets one more try."""
        coordinators = [row for row in snapshot["attempts"] if row["kind"] == "coordinator"]
        latest = coordinators[-1] if coordinators else None
        if (latest is None or latest["state"] not in {"failed", "cancelled"}
                or any(row["attempt_id"] == latest["id"] for row in snapshot["coordinator_proposals"])):
            return False
        if self.final_report is not None:
            return False
        # This failed turn was itself the retry of the one before it: stop asking
        # the model and hand over to the owner.
        previous = coordinators[-2] if len(coordinators) > 1 else None
        if previous is not None and previous["id"] in self._retried:
            if self.closing:
                # The work is done and accepted; only the summary is missing.
                self.final_report = "The orchestrator did not write a final report. See the accepted findings."
                return False
            return self._hand_back("The orchestrator could not produce a usable plan twice. Plan the next step yourself.")
        if latest["id"] in self._retried:
            return True  # Its retry is being prepared.
        if self._request_plan(capture, runner, snapshot, note="The orchestrator is trying its turn again."):
            # The next turn is this one's retry.
            self._retried.add(latest["id"])
        return True

    def _accept_results(self, capture, runner, snapshot) -> bool:
        work = {row["id"]: row for row in snapshot["work_items"]}
        submissions = {row["attempt_id"]: row for row in snapshot["submissions"]}
        for attempt in snapshot["attempts"]:
            item = work.get(attempt["work_item_id"])
            if (attempt["kind"] != "worker" or attempt["state"] != "submitted" or attempt["process_state"] != "stopped"
                    or item is None or item["state"] != "submitted" or attempt["id"] not in submissions):
                continue
            specification = json.loads(item["specification"])
            if specification["write_roots"] or specification["criteria"] != ["owner_review"]:
                continue
            self._command(runner, "accept_under_grant", {
                "attempt_id": attempt["id"], "attempt_epoch": attempt["epoch"],
                "candidate_revision": submissions[attempt["id"]]["candidate_revision"], "evidence": RESULT_EVIDENCE})
            self._set("working", f"Round {self.round}: accepted a finding for the orchestrator's next turn.")
            return True
        return False

    def _retry_failed(self, capture, runner, snapshot) -> bool:
        for item in snapshot["work_items"]:
            if item["state"] not in {"failed", "cancelled"} or item["id"] in self._retried:
                continue
            if any(row["work_item_id"] == item["id"] and row["process_state"] != "stopped" for row in snapshot["attempts"]):
                continue  # A retry needs every earlier attempt stopped and accounted for.
            failed_at = self._failed_at.setdefault(item["id"], time.monotonic())
            if time.monotonic() - failed_at < RETRY_DELAY_SECONDS:
                self._set("working", f"Round {self.round}: a task failed; retrying it shortly.")
                return True
            self._command(runner, "retry", {"work_item_id": item["id"], "evidence": RETRY_EVIDENCE})
            self._retried.add(item["id"])
            scheduler = self.runtime._schedulers.get(self.run_id)
            if scheduler is not None:
                scheduler.start()
            self._set("working", f"Round {self.round}: retrying a failed task once.")
            return True
        return False

    # ── writers' changes, under the grant to apply them ───────────────────

    def _integrate(self, capture, runner, snapshot, writers) -> bool:
        """Combine, check, apply and accept the round's submitted writers: one step per pass."""
        operations = snapshot["integration_operations"]
        active = [row for row in operations if row["state"] in {"queued", "running"}]
        if active:
            self._set("integrating", {"prepare_candidate": "Combining the writers' changes.",
                                      "run_check": "Running a declared check on the combined changes.",
                                      "apply": "Applying the checked changes to the project."
                                      }.get(active[-1]["kind"], "Working on the writers' changes."))
            return True
        if any(row["state"] == "uncertain" for row in operations):
            return self._hand_back("A step on the writers' changes ended without a known outcome. "
                                   "Reconcile it or stop the team.")
        items = {row["id"] for row in writers}
        attempts = [row for row in snapshot["attempts"] if row["kind"] == "worker"
                    and row["work_item_id"] in items and row["state"] == "submitted"]
        settled = {row["attempt_id"] for row in snapshot["reservations"] if row["state"] == "settled"}
        worktrees = {row["attempt_id"]: row for row in snapshot["writer_worktrees"]}
        if any(row["process_state"] != "stopped" or row["id"] not in settled
               or worktrees.get(row["id"], {}).get("state") != "ready" for row in attempts):
            self._set("integrating", "Waiting for the writers to finish.")
            return True
        ids = sorted(worktrees[row["id"]]["id"] for row in attempts)
        def members(row):
            return {item["id"] for item in json.loads(row["manifest_json"])["writers"]}
        live = [row for row in snapshot["integration_candidates"] if row["state"] != "superseded"]
        # Writers already in an applied change are accepted under it: after the
        # first of them is accepted, the rest are no longer the whole change.
        applied = next((row for row in reversed(live) if row["state"] == "applied" and members(row) & set(ids)), None)
        if applied is not None:
            return self._accept_writers(runner, snapshot, applied)
        candidate = next((row for row in reversed(live) if sorted(members(row)) == ids), None)
        if candidate is None:
            return self._operate(capture, snapshot, "prepare_candidate", tuple(ids),
                                 "Combining the writers' changes.", writer_ids=ids)
        if candidate["state"] == "conflict":
            return self._hand_back("The writers' changes conflict with each other, so they can't be combined. "
                                   "Send one back to redo its change, or stop the team.")
        if candidate["state"] not in {"ready", "failed", "verified"}:
            return self._hand_back(f"The combined changes need your inspection (state: {candidate['state']}).")
        checks = json.loads(candidate["manifest_json"])["checks"]
        latest = {row["check_key"]: row for row in snapshot["integration_checks"] if row["candidate_id"] == candidate["id"]}
        for check in checks:
            receipt = latest.get(check["key"])
            if receipt is not None and receipt["state"] in {"failed", "timed_out"}:
                return self._send_back(runner, snapshot, candidate, check["key"], receipt)
        for check in checks:
            receipt = latest.get(check["key"])
            if receipt is None or receipt["state"] == "cancelled":
                return self._operate(capture, snapshot, "run_check", (candidate["id"], check["key"]),
                                     f"Running the check {check['key']} on the combined changes.",
                                     candidate_id=candidate["id"], check_key=check["key"])
            if receipt["state"] != "passed":
                return self._hand_back(f"The check {check['key']} needs your inspection (state: {receipt['state']}).")
        if candidate["state"] != "verified":
            return self._hand_back("Every check passed, but the combined changes aren't marked verified. Inspect them.")
        # A failed application is never retried: the checkout is the owner's.
        return self._operate(capture, snapshot, "apply_candidate", (candidate["id"],),
                             "Applying the checked changes to the project.", limit=1,
                             candidate_id=candidate["id"], expected_base=candidate["base_revision"],
                             target_revision=candidate["result_revision"], evidence=APPLY_EVIDENCE)

    def _operate(self, capture, snapshot, action: str, target: tuple, note: str, *, limit: int = 2,
                 **payload: Any) -> bool:
        """Submit one integration operation through the owner's own validated path, at most ``limit`` times."""
        key = (action, target)
        if self._operations.get(key, 0) >= limit:
            return self._hand_back(_STEP_FAILED[action])
        self._refusal(action)
        self._requests += 1
        self.runtime.operate(capture, {"action": action, "request_id": f"autopilot_{self.run_id}_{self._requests}",
                                       "run_id": self.run_id, "expected_revision": snapshot["run"]["revision"],
                                       **payload})
        self._operations[key] = self._operations.get(key, 0) + 1
        self._set("integrating", note)
        return True

    def _send_back(self, runner, snapshot, candidate, key: str, receipt: dict[str, Any]) -> bool:
        """A declared check failed on the combined changes: reject its writers so each is retried once."""
        attempts = {row["id"]: row for row in snapshot["attempts"]}
        code = "" if receipt["exit_code"] is None else f", exit code {receipt['exit_code']}"
        evidence = (f"The combined changes failed the declared check {key} ({receipt['state'].replace('_', ' ')}{code}). "
                    "Sent back under the owner's autonomy grant to fix, with the check's output.")
        for member in json.loads(candidate["manifest_json"])["writers"]:
            attempt = attempts.get(member["attempt_id"])
            if attempt is None or attempt["state"] != "submitted":
                continue
            self._command(runner, "reject", {"attempt_id": attempt["id"], "attempt_epoch": attempt["epoch"],
                                             "evidence": evidence})
            # Unlike a refused request, a failed check needs no pause before the retry.
            self._failed_at[attempt["work_item_id"]] = time.monotonic() - RETRY_DELAY_SECONDS
            self._set("working", f"Round {self.round}: the check {key} failed; sending the writers back to fix it.")
            return True
        return self._hand_back(f"The check {key} failed on the combined changes. Inspect them.")

    def _accept_writers(self, runner, snapshot, candidate) -> bool:
        """The checked changes are applied: accept each of their writers under the grant."""
        attempts = {row["id"]: row for row in snapshot["attempts"]}
        work = {row["id"]: row for row in snapshot["work_items"]}
        for member in json.loads(candidate["manifest_json"])["writers"]:
            attempt = attempts.get(member["attempt_id"])
            if attempt is None or work[attempt["work_item_id"]]["state"] != "submitted":
                continue
            self._command(runner, "accept_writer_under_grant", {
                "attempt_id": attempt["id"], "attempt_epoch": attempt["epoch"], "candidate_id": candidate["id"],
                "evidence": WRITER_EVIDENCE})
            self._set("integrating", f"Round {self.round}: the checked changes were applied; accepting the writers.")
            return True
        return self._hand_back("The applied changes don't cover every submitted writer. Inspect them.")

    # ── rounds ────────────────────────────────────────────────────────────

    def _settle(self, capture, runner, snapshot) -> bool:
        """Every task is accepted: plan the next round, write the report, or complete."""
        if self.final_report is not None:
            self._command(runner, "complete", {})
            scheduler = self.runtime._schedulers.get(self.run_id)
            if scheduler is not None:
                scheduler.close()
            self._set("finished", "The orchestrator finished the objective.")
            return False
        if self.round < self.rounds:
            if self._request_plan(capture, runner, snapshot,
                                  note=f"Round {self.round + 1}: the orchestrator is planning from the findings."):
                self.round += 1
            return True
        self.closing = True
        self._request_plan(capture, runner, snapshot, note="The orchestrator is writing its final report.")
        return True

    def _request_plan(self, capture, runner, snapshot, *, note: str) -> bool:
        """Start the next orchestrator turn; False (and the owner told why) when it can't run."""
        planning = self.runtime._planning_view(runner.store, self.run_id, snapshot)
        if not planning["available"]:
            self._set("needs_owner", planning["reason"] or "The orchestrator cannot plan again.")
            return False
        self._refusal("request_plan")
        self._requests += 1
        message = {"command": "swarm", "action": "request_plan", "project": capture.workspace,
                   "session_id": capture.scope.session_id, "run_id": self.run_id,
                   "request_id": f"autopilot_{self.run_id}_{self._requests}",
                   "expected_revision": snapshot["run"]["revision"],
                   "coordinator_requests": planning["default_requests"], "read_roots": planning["read_roots"]}
        self.runtime._request_plan(capture, message, closing=self.closing)
        self._set("planning", note)
        return True
