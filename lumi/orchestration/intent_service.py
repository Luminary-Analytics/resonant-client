"""
IntentService — owns active intents, drives `GraphWalker` in worker threads.

One service instance per AppState. Each `start_intent(text)` returns immediately
with an intent_id; the actual work happens on a background thread. Events are
forwarded through an `on_event` callback (the GUI wires this to a WebSocket
emitter). Every state change persists to disk so the user can reload mid-run.

Cancellation: each active intent owns a `threading.Event`. `cancel()` sets it
and emits `intent.cancelling`. The running specialist's Session shares the
event, so it makes no further model request or tool call; the walker starts no
new node, abandons those that never ran (`plan.stopped`), and the worker then
emits `intent.cancelled`, once, when the plan has stopped.

Pause: each active intent owns a `pause_event`. `pause()` sets it; the walker
starts no new node while the flag is up (the specialist already running
finishes first). `resume()` clears it. Cancel, pause and resume apply only
while the intent's worker runs: a finished intent reports False.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from ..policy import full_auto_refusal
from .audit import (
    log_decision,
    log_floor_violation,
    log_plan_change,
    log_tool_call,
)
from .persistence import (
    list_snapshots,
    load_graph,
    purge_old_snapshots,
    restore_snapshot,
    save_graph,
    snapshot_graph,
)
from .plan_graph import (
    NodeSpecialization,
    PlanGraph,
    PlanNode,
    new_node_id,
)
from .runner import LocalSpecialistRunner
from .skill_extraction import extract_skill, is_extraction_candidate
from .walker import GraphWalker, WalkerEvent

logger = logging.getLogger(__name__)


# ── Active intent bookkeeping ──────────────────────────────────────────


@dataclass
class _ActiveIntent:
    intent_id: str
    graph: PlanGraph
    walker: GraphWalker
    cancel_event: threading.Event
    pause_event: threading.Event
    thread: threading.Thread
    started_at: float = field(default_factory=time.time)
    completed_at: float = 0.0
    status: str = "running"  # "running" | "paused" | "completed" | "cancelled" | "failed"


# ── The service ─────────────────────────────────────────────────────────


class IntentService:
    """Manage the lifecycle of all active intents for one project."""

    def __init__(
        self,
        *,
        project_path: str,
        backend: Any,
        all_tools: list[dict],
        project_instructions: str = "",
        settings: Any = None,
        on_event: Optional[Callable[[dict], None]] = None,
        specialist_backend_resolver: Optional[Callable[[str], Any]] = None,
        mcp_manager: Any = None,
        hook_runner_for: Optional[Callable[[str], Any]] = None,
    ):
        self.project_path = project_path
        self.backend = backend
        self.all_tools = list(all_tools or [])
        self.project_instructions = project_instructions or ""
        self.settings = settings
        self.on_event = on_event or (lambda ev: None)
        # v0.5.8a1 — per-specialist backend routing. See
        # LocalSpecialistRunner.__init__ for the resolver contract.
        self.specialist_backend_resolver = specialist_backend_resolver
        self.mcp_manager = mcp_manager
        # The person's hooks for each specialist (LocalSpecialistRunner._hook_runner).
        self.hook_runner_for = hook_runner_for
        self._active: dict[str, _ActiveIntent] = {}
        self._lock = threading.Lock()

    # ── Lifecycle ──────────────────────────────────────────────────

    def start_intent(
        self, text: str, *, planner_specialization: Optional[str] = None,
    ) -> str:
        """Bootstrap a fresh plan-graph from `text` and start a worker thread.

        The initial graph holds one root `plan` node whose goal is the user's
        text. The walker runs it; the planner specialist returns subgoals; the
        walker expands them. No two-phase awkwardness.

        v0.5.1a2 — `planner_specialization` overrides the default
        `NodeSpecialization.PLAN` for the root node. Used by the
        autonomous-mission daemon to route pro-tier sub-missions to
        `PLAN_DEEP` (research-first) instead. None falls through to
        `PLAN` for backwards compatibility with the regular Mission
        flow.

        Raises ValueError for empty text, an unknown specialization, or an
        organization policy that doesn't allow Full-auto (lumi/policy.py).
        """
        text = (text or "").strip()
        if not text:
            raise ValueError("intent text cannot be empty")
        spec = planner_specialization or NodeSpecialization.PLAN
        if spec not in NodeSpecialization.ALL:
            raise ValueError(
                f"unknown planner specialization {spec!r}; "
                f"expected one of {sorted(NodeSpecialization.ALL)}"
            )
        # Specialists run in Full-auto; refused before anything is saved or
        # started when the organization's policy doesn't allow it.
        refusal = full_auto_refusal()
        if refusal:
            raise ValueError(refusal)
        graph = PlanGraph.new(text)
        root = PlanNode(
            id=new_node_id(),
            intent_id=graph.intent_id,
            goal=text,
            specialization=spec,
        )
        graph.add_node(root)
        save_graph(graph, self.project_path)
        log_decision(
            self.project_path, graph.intent_id,
            summary="intent started", text=text, root_node_id=root.id,
        )

        cancel_event = threading.Event()
        pause_event = threading.Event()

        runner = LocalSpecialistRunner(
            backend=self.backend,
            project_path=self.project_path,
            all_tools=self.all_tools,
            project_instructions=self.project_instructions,
            settings=self.settings,
            cancel_event=cancel_event,
            on_session_event=lambda ev: self._forward_session_event(graph.intent_id, ev),
            audit_logger=self._make_audit_logger(graph.intent_id),
            specialist_backend_resolver=self.specialist_backend_resolver,
            mcp_manager=self.mcp_manager,
            hook_runner_for=self.hook_runner_for,
        )
        walker = GraphWalker(
            runner=runner,
            on_event=lambda ev: self._on_walker_event(graph, ev),
            cancel_event=cancel_event,
            pause_event=pause_event,
        )

        thread = threading.Thread(
            target=self._run_walker,
            args=(graph, walker, cancel_event),
            name=f"intent-{graph.intent_id[:8]}",
            daemon=True,
        )
        active = _ActiveIntent(
            intent_id=graph.intent_id,
            graph=graph,
            walker=walker,
            cancel_event=cancel_event,
            pause_event=pause_event,
            thread=thread,
        )
        with self._lock:
            self._active[graph.intent_id] = active
        thread.start()

        # Snapshot to UI immediately so the viz populates before specialists run.
        self._emit({
            "event": "plan.snapshot",
            "intent_id": graph.intent_id,
            "snapshot": graph.to_dict(),
        })
        self._emit({
            "event": "intent.started",
            "intent_id": graph.intent_id,
            "text": text,
        })
        return graph.intent_id

    def cancel(self, intent_id: str) -> bool:
        active = self._get_running(intent_id)
        if not active:
            return False
        active.cancel_event.set()
        active.status = "cancelled"
        log_decision(self.project_path, intent_id, summary="intent cancel requested")
        # Not yet `intent.cancelled`: the running step is still ending. The
        # worker sends that when the walk is over.
        self._emit({"event": "intent.cancelling", "intent_id": intent_id})
        return True

    def pause(self, intent_id: str) -> bool:
        active = self._get_running(intent_id)
        if not active:
            return False
        active.pause_event.set()
        active.status = "paused"
        log_decision(self.project_path, intent_id, summary="intent paused")
        self._emit({"event": "intent.paused", "intent_id": intent_id})
        return True

    def resume(self, intent_id: str) -> bool:
        active = self._get_running(intent_id)
        if not active:
            return False
        active.pause_event.clear()
        active.status = "running"
        log_decision(self.project_path, intent_id, summary="intent resumed")
        self._emit({"event": "intent.resumed", "intent_id": intent_id})
        return True

    def list_active(self) -> list[str]:
        with self._lock:
            return list(self._active.keys())

    def adopt_running(self, previous: Optional["IntentService"]) -> None:
        """Keep a replaced service's running intents reachable from this one.

        The app builds a new service when its backend, project or tools
        change. An intent started before that keeps running under the service
        that started it, and its events still flow from there; without this,
        its Stop, Pause and Resume would report it finished while it carried
        on.
        """
        if previous is None or previous is self:
            return
        with previous._lock:
            running = {
                intent_id: active
                for intent_id, active in previous._active.items()
                if active.thread.is_alive()
            }
        with self._lock:
            for intent_id, active in running.items():
                self._active.setdefault(intent_id, active)

    def get_graph(self, intent_id: str) -> Optional[PlanGraph]:
        active = self._get(intent_id)
        if active:
            return active.graph
        # Try loading from disk
        return load_graph(intent_id, self.project_path)

    # ── Snapshots / restore ────────────────────────────────────────

    def list_snapshots(self, intent_id: str) -> list[dict]:
        return list_snapshots(self.project_path, intent_id=intent_id)

    def restore_snapshot(self, intent_id: str, ts_ms: int) -> bool:
        """Restore a graph from an old snapshot. The intent must not be running.

        Cancel first if it is — the user is choosing to throw away progress.
        """
        active = self._get(intent_id)
        if active and active.thread.is_alive():
            return False
        snap = restore_snapshot(self.project_path, ts_ms=ts_ms, intent_id=intent_id)
        if not snap:
            return False
        save_graph(snap, self.project_path)
        log_decision(
            self.project_path, intent_id,
            summary="snapshot restored", ts_ms=ts_ms,
        )
        self._emit({
            "event": "plan.snapshot",
            "intent_id": intent_id,
            "snapshot": snap.to_dict(),
        })
        return True

    def purge_old_snapshots(self, *, retention_days: float = 30.0) -> int:
        return purge_old_snapshots(self.project_path, retention_days=retention_days)

    # ── Internals ──────────────────────────────────────────────────

    def _get(self, intent_id: str) -> Optional[_ActiveIntent]:
        with self._lock:
            return self._active.get(intent_id)

    def _get_running(self, intent_id: str) -> Optional[_ActiveIntent]:
        # Finished intents stay in `_active` for get_graph; pausing or
        # cancelling one would only relabel it and announce a change that
        # never happens. `completed_at` is set before the final event goes
        # out, while the thread is still alive: a stop accepted after that
        # would never be followed by `intent.cancelled`.
        active = self._get(intent_id)
        if active and active.thread.is_alive() and not active.completed_at:
            return active
        return None

    def _emit(self, payload: dict) -> None:
        try:
            self.on_event(payload)
        except Exception:
            logger.exception("on_event handler raised; swallowing to keep worker alive")

    def _forward_session_event(self, intent_id: str, event: dict) -> None:
        """Wrap an engine-session event with the intent_id and ship to the GUI."""
        out = dict(event)
        out["intent_id"] = intent_id
        # The tag keeps a specialist's session out of the conversation's own
        # turn: the GUI draws it under the plan's card, as a step of the
        # plan (app.js, "Plan activity").
        out["_source"] = "intent"
        self._emit(out)

    def _make_audit_logger(self, intent_id: str) -> Callable[..., None]:
        project_path = self.project_path

        def log(*, kind: str = "tool_call", **payload) -> None:
            try:
                if kind == "tool_call":
                    log_tool_call(
                        project_path, intent_id,
                        tool_name=payload.get("tool_name", ""),
                        args=payload.get("args") or {},
                        result_summary=payload.get("result_summary", ""),
                        is_error=payload.get("is_error", False),
                        duration_ms=payload.get("duration_ms"),
                    )
                elif kind == "floor_violation":
                    log_floor_violation(
                        project_path, intent_id,
                        rule=payload.get("rule", ""),
                        reason=payload.get("reason", ""),
                        tool_name=payload.get("tool_name", ""),
                    )
                else:
                    log_decision(project_path, intent_id, summary=payload.get("summary", kind), **{
                        k: v for k, v in payload.items() if k != "summary"
                    })
            except Exception:
                logger.debug("audit log failed", exc_info=True)
        return log

    def _on_walker_event(self, graph: PlanGraph, event: WalkerEvent) -> None:
        """Persist + audit + forward each walker event to the GUI."""
        # Persistence: snapshot before plan rewrites, save on any state change.
        try:
            if event.kind == "plan.rewrite":
                snapshot_graph(graph, self.project_path)
            save_graph(graph, self.project_path)
        except Exception:
            logger.warning("Failed to persist graph for intent %s", graph.intent_id, exc_info=True)

        # Audit log
        try:
            if event.kind == "node.start":
                log_decision(
                    self.project_path, graph.intent_id,
                    summary="dispatched specialist",
                    node_id=event.node_id,
                    specialization=event.payload.get("specialization", ""),
                )
            elif event.kind == "node.done":
                log_plan_change(
                    self.project_path, graph.intent_id,
                    node_id=event.node_id or "",
                    change=f"status:{event.payload.get('status', '')}",
                    confidence=event.payload.get("confidence"),
                    summary=event.payload.get("summary", "")[:200],
                )
            elif event.kind == "plan.rewrite":
                log_plan_change(
                    self.project_path, graph.intent_id,
                    node_id=event.node_id or "",
                    change="rewrite",
                    added=event.payload.get("added", []),
                    removed=event.payload.get("removed", []),
                    reason=event.payload.get("reason", ""),
                )
            elif event.kind == "plan.complete":
                log_decision(
                    self.project_path, graph.intent_id,
                    summary="plan complete",
                    all_done=event.payload.get("all_done"),
                    node_count=event.payload.get("node_count"),
                )
            elif event.kind == "plan.stopped":
                log_decision(
                    self.project_path, graph.intent_id,
                    summary="plan stopped",
                    abandoned=len(event.payload.get("abandoned") or []),
                )
        except Exception:
            logger.debug("audit log raised", exc_info=True)

        # Forward to GUI
        self._emit({
            "event": "plan.event",
            "intent_id": graph.intent_id,
            "event_payload": {
                "kind": event.kind,
                "node_id": event.node_id,
                "payload": event.payload,
                "ts": event.ts,
            },
        })

    def _run_walker(
        self,
        graph: PlanGraph,
        walker: GraphWalker,
        cancel_event: threading.Event,
    ) -> None:
        """Worker-thread entry point. The walker honors pause before each node."""
        try:
            walker.run(graph)
        except Exception as exc:
            logger.exception("Walker crashed for intent %s", graph.intent_id)
            with self._lock:
                if graph.intent_id in self._active:
                    self._active[graph.intent_id].status = "failed"
                    self._active[graph.intent_id].completed_at = time.time()
            self._emit({
                "event": "intent.failed",
                "intent_id": graph.intent_id,
                "error": str(exc),
            })
            return

        # Walker returned normally — mark done, snapshot final state.
        try:
            save_graph(graph, self.project_path)
            snapshot_graph(graph, self.project_path)
        except Exception:
            logger.debug("final persist raised", exc_info=True)

        # Auto-extract a skill if this graph qualifies. Best-effort. A plan
        # the person stopped isn't a procedure to repeat, even if every step
        # that ran had finished.
        skill_id = None
        try:
            if not cancel_event.is_set() and is_extraction_candidate(graph):
                skill = extract_skill(graph)
                if skill:
                    skill_id = skill.id
                    log_decision(
                        self.project_path, graph.intent_id,
                        summary="skill auto-extracted",
                        skill_id=skill.id, scope=skill.scope,
                    )
        except Exception:
            logger.debug("skill extraction raised", exc_info=True)

        with self._lock:
            if graph.intent_id in self._active:
                active = self._active[graph.intent_id]
                if cancel_event.is_set():
                    active.status = "cancelled"
                else:
                    active.status = "completed"
                active.completed_at = time.time()

        self._emit({
            "event": "intent.complete" if not cancel_event.is_set() else "intent.cancelled",
            "intent_id": graph.intent_id,
            "extracted_skill_id": skill_id,
        })
