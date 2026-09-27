"""Let a team's orchestrator run it in rounds, under the owner's start-time grant.

Starting a coordinator team with ``autonomy`` records the owner's choice in the
team's captured setup. From then on this host loop takes the owner steps the
orchestrator (the team's coordinator model) would otherwise wait on:

- it accepts each orchestrator plan. The supervisor still validates the plan
  exactly as for an owner decision: declared scopes, criteria and allowance;
- it accepts each read result under the grant (``accept_under_grant``), so the
  next round can use it. The receipt says so and never claims owner review;
- it retries a failed task, or an orchestrator turn without a usable plan, once;
- when a round settles, it asks the orchestrator to plan again from the
  findings and from workers' messages to it;
- after the last round, it asks for one closing turn that may not start work:
  the orchestrator's final report, after which the team completes.

The orchestrator can also finish early by proposing no more work. Writers'
changes still wait for the owner to check and apply them. Every step goes
through the durable commands the owner's own controls use, so the owner can
pause, steer or stop the team at any time.
"""

from __future__ import annotations

import json
import logging
import threading
from typing import Any

from .models import AdmissionClosed, Conflict, RevisionConflict

logger = logging.getLogger(__name__)

_TERMINAL = frozenset({"completed", "cancelled", "failed"})
_IN_FLIGHT = frozenset({"pending", "ready", "leased", "running", "submitted"})
MAX_ROUNDS = 8
PLAN_EVIDENCE = ("Accepted by the team's orchestrator under the owner's autonomy grant: plans within this team's "
                 "scopes, criteria and request allowance run without a separate approval.")
CLOSING_EVIDENCE = "The team used its rounds; the orchestrator's closing turn may not start more work."
RESULT_EVIDENCE = ("Accepted under the owner's autonomy grant so the orchestrator's next round can use it; "
                   "the owner has not reviewed it.")
RETRY_EVIDENCE = "Retried once automatically under the owner's autonomy grant."


class TeamAutopilot:
    """One orchestrator loop for one captured team run.

    ``rounds`` counts rounds of work, the first plan included; the closing
    turn that writes the final report comes after them.
    """

    TICK = 0.4

    def __init__(self, runtime: Any, run_id: str, *, rounds: int) -> None:
        if type(rounds) is not int or not 1 <= rounds <= MAX_ROUNDS:
            raise ValueError(f"Choose one to {MAX_ROUNDS} orchestrator rounds")
        self.runtime, self.run_id, self.rounds = runtime, run_id, rounds
        self.round = 1
        self.closing = False
        self.phase = "planning"
        self.detail = "The orchestrator is planning the first round."
        self.final_report: str | None = None
        self._retried: set[str] = set()
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
        return {"enabled": True, "round": self.round, "rounds": self.rounds, "closing": self.closing,
                "phase": self.phase, "detail": self.detail, "final_report": self.final_report,
                "active": bool(self._thread and self._thread.is_alive())}

    def _set(self, phase: str, detail: str) -> None:
        self.phase, self.detail = phase, detail

    def _loop(self) -> None:
        while not self._stop.wait(self.TICK):
            try:
                if not self.step():
                    return
            except (Conflict, RevisionConflict, AdmissionClosed):
                continue  # The owner or a worker changed the team; look again.
            except Exception as exc:  # noqa: BLE001 - the owner must see that the loop ended
                logger.exception("Team orchestrator loop stopped")
                self._set("needs_owner", f"The orchestrator loop stopped ({type(exc).__name__}). "
                                         "Review the team and continue it yourself.")
                return

    # ── one pass ──────────────────────────────────────────────────────────

    def step(self) -> bool:
        """Take at most one owner step; False once the loop has nothing left to do."""
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
        if any(row["kind"] == "coordinator" and (row["state"] in {"leased", "running", "uncertain"}
               or row["process_state"] != "stopped") for row in snapshot["attempts"]):
            self._set("planning", "The orchestrator is writing its final report." if self.closing
                      else f"Round {self.round}: the orchestrator is planning.")
            return True
        for handled in (self._decide, self._failed_orchestrator, self._accept_results, self._retry_failed):
            if handled(capture, runner, snapshot):
                return True
        work = snapshot["work_items"]
        if any(row["state"] == "submitted" and json.loads(row["specification"])["write_roots"] for row in work):
            self._set("needs_owner", "Writers submitted changes. Check and apply them, then accept them to continue.")
            return True
        if any(row["state"] in _IN_FLIGHT for row in work):
            running = sum(row["state"] in {"leased", "running"} for row in work)
            self._set("working", f"Round {self.round}: {running} of {len(work)} tasks running.")
            return True
        if any(row["state"] != "accepted" for row in work):
            self._set("needs_owner", "A task failed after its automatic retry. Retry, change or stop the team.")
            return True
        return self._settle(capture, runner, snapshot)

    def _command(self, runner, kind: str, payload: dict[str, Any]):
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
        if latest["id"] in self._retried:
            if self.closing:
                # The work is done and accepted; only the summary is missing.
                self.final_report = "The orchestrator did not write a final report. See the accepted findings."
                return False
            self._set("needs_owner", "The orchestrator could not produce a usable plan twice. Plan the next step yourself.")
            return True
        self._request_plan(capture, runner, snapshot, note="The orchestrator is trying its turn again.")
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
            self._command(runner, "retry", {"work_item_id": item["id"], "evidence": RETRY_EVIDENCE})
            self._retried.add(item["id"])
            scheduler = self.runtime._schedulers.get(self.run_id)
            if scheduler is not None:
                scheduler.start()
            self._set("working", f"Round {self.round}: retrying a failed task once.")
            return True
        return False

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
        self._requests += 1
        message = {"command": "swarm", "action": "request_plan", "project": capture.workspace,
                   "session_id": capture.scope.session_id, "run_id": self.run_id,
                   "request_id": f"autopilot_{self.run_id}_{self._requests}",
                   "expected_revision": snapshot["run"]["revision"],
                   "coordinator_requests": planning["default_requests"], "read_roots": planning["read_roots"]}
        self.runtime._request_plan(capture, message, closing=self.closing)
        self._set("planning", note)
        return True
