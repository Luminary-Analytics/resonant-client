"""Team work under an organization policy: models, modes, checks, budgets, usage and audit.

The Team preview predates Lumi's organization controls, so while a policy
applied it refused all new team work (``service.policy_refusal``). A personal
team now runs under a policy, following these rules; sharing with another
conversation and organization-managed teams stay refused (``unsupported_refusal``).

- **Models.** The team's model passes ``Policy.model_allowed`` (zero-retention
  rules included) when the team starts, before each participant starts and
  before each model request, so a policy that arrives mid-run stops new work.
- **Modes** (``permissions.allowed_modes``). A read-only team only reads and
  reports, which every mode allows. Writers change files in their worktrees
  without asking, as Auto-edit does, so a team with writers needs ``auto-edit``
  or ``bypass``. An orchestrator allowed to apply checked changes changes the
  checkout and runs checks with nobody asked, as a mission does, so it needs
  ``bypass`` (Full-auto; see ``policy.full_auto_refusal``).
- **Checks.** The owner's declared checks are commands: they pass the
  guardrails and the irreversibility floor as the agent's commands do, and the
  organization's shell rules (a ``deny``, or a ``prompt`` nobody is there to
  answer, refuses them). A check that needs a second person's approval
  (``approvals``) is refused, and while the shell sandbox is on a team with
  writers is refused: its checks can't run in the sandbox yet.
- **Budgets and usage.** Each model request is checked against the budgets
  (``budgets.py``) before its allowance is reserved, counting the whole team run
  as one turn, and recorded in the usage records (``usage.py``) with purpose
  ``team`` (``team_planning`` or ``team_compression`` for auxiliary requests),
  the owner's project and conversation, and agent ``team:<run>:<worker>``.
- **Audit.** The team's start, stop and completion, each participant's start
  and end, integration steps, decisions and refusals go to the audit log
  (``audit.py``): metadata, with content only through ``audit.content`` and
  paths only through ``audit.name``.

Every check reads the policy, settings and budgets in force when it runs.
"""

from __future__ import annotations

import copy
import logging
from typing import Any, Iterable

logger = logging.getLogger(__name__)

# The usage records' purpose for a participant's main requests (usage.py).
PURPOSE = "team"
# Whose rules a team runs under. A personal team follows this module; a team
# shared with another conversation and an organization-managed team don't yet.
PERSONAL, SHARING, MANAGED = "personal", "sharing", "managed"


class _Settings:
    """``settings.get(section, key, default)``, policy locks applied; defaults for a plain mapping (tests)."""

    def __init__(self, settings: Any) -> None:
        self._settings = settings

    def get(self, section: str, key: str | None = None, default: Any = None) -> Any:
        getter = getattr(self._settings, "get", None)
        if not callable(getter):
            return default
        try:
            return getter(section, key, default)
        except TypeError:
            # A plain mapping of settings has no section lookup.
            return default


def unsupported_refusal() -> str:
    """Why shared and organization-managed team work can't run here, or '' without a policy."""
    from ...policy import current

    policy = current()
    if policy is None:
        return ""
    return (f"{policy.organization}'s policy applies on this computer, and the Team preview doesn't follow it "
            "for sharing with another conversation or for organization-managed teams yet, so it can't start "
            "or change that team work here.")


def model_refusal(provider: str, model: str) -> str:
    """Why the organization's policy refuses the team's model (zero-retention rules included), or ''."""
    from ...policy import current

    policy = current()
    if policy is None or policy.model_allowed(str(provider or ""), str(model or "")):
        return ""
    return (f"{policy.organization}'s policy doesn't allow {model or 'this model'} on {provider or 'this provider'}, "
            "so the team can't use it. Choose another model for this conversation.")


def mode_refusal(*, writers: bool, applies: bool) -> str:
    """Why the policy's permission modes refuse this kind of team, or ''."""
    from ...policy import current

    policy = current()
    if policy is None:
        return ""
    if applies and not policy.mode_allowed("bypass"):
        return (f"{policy.organization}'s policy doesn't allow Full-auto, which an orchestrator that applies "
                "checked changes needs: it changes your checkout and runs checks without asking. Start the team "
                "without applying changes.")
    if writers and not (policy.mode_allowed("auto-edit") or policy.mode_allowed("bypass")):
        return (f"{policy.organization}'s policy allows neither Auto-edit nor Full-auto, which a team with "
                "writers needs: its writers change files without asking. Start a read-only team.")
    # A read-only team only reads and reports. Every mode allows that, and a
    # policy's allowed_modes always names at least one (policy.parse).
    return ""


def check_refusal(checks: Iterable[dict], *, project: str, settings: Any) -> str:
    """Why one of the owner's declared checks can't run as a command, or ''.

    A check runs a program with arguments. It passes what the agent's
    commands pass: the guardrails (each word too, as ``sh -c`` carries a whole
    command), the irreversibility floor, and the review gate's and the
    organization's shell rules, as a ``check_run`` and as a ``bash`` command.
    A rule that asks a person refuses it, since the team runs its checks
    without an approval prompt, and so does a command a second person must
    approve (``approvals``): the team can't wait for one yet.
    """
    from ...orchestration.autonomy import check_floor
    from ...policy import current
    from .. import guardrails, review_gate, second_approval
    from ..policies import ExecutionPolicy, PolicyAction, with_organization_rules

    checks = [check for check in checks if isinstance(check, dict)]
    if not checks:
        return ""
    policy = current()
    organization = policy.organization if policy else "Your organization"
    rules = with_organization_rules(ExecutionPolicy(guardrails.policy_rules() + review_gate.policy_rules()))
    view = _Settings(settings)
    for check in checks:
        if not isinstance(check.get("argv"), (list, tuple)):
            continue  # Not a command; the team's setup refuses it (service._writer_configuration).
        argv = [str(word) for word in check["argv"]]
        command = " ".join(argv)
        name = f"The check {check.get('key')}" if check.get("key") else "A check"
        reason = guardrails.blocked_argv(argv)
        if reason:
            return f"{name} can't run: {guardrails.refusal(reason)}"
        violation = check_floor(tool_name="check_run", args={"command": command}, project_path=project,
                                settings=view)
        if violation is not None:
            return f"{name} can't run: {violation.reason.rstrip('.')}."
        for tool in ("check_run", "bash"):
            rule = rules.first_match(tool, {"command": command})
            if rule is not None and rule.action == PolicyAction.DENY.value:
                return f"{name} can't run: {(rule.reason or f'{organization} refuses it').rstrip('.')}."
            if rule is not None and rule.action == PolicyAction.PROMPT.value:
                why = f" ({rule.reason.rstrip('.')})" if rule.reason else ""
                return (f"{name} can't run: {organization}'s shell rules ask a person before it runs{why}, and a "
                        "team runs its checks without an approval prompt.")
        pattern = second_approval.needed("check_run", {"command": command})
        if pattern:
            return (f"{name} needs a second person's approval under {organization}'s policy ({pattern}), which a "
                    "team can't wait for yet. Leave it out of the team's checks.")
    return ""


def sandbox_refusal(settings: Any) -> str:
    """Why a team with writers can't run while the shell sandbox is on, or ''.

    Commands must run in the sandbox or not at all (engine/os_sandbox.py).
    A team's checks don't run in it yet, and writers need checks.
    """
    if _Settings(settings).get("security", "shell_sandbox", "off") != "project":
        return ""
    return ("The shell sandbox is on (Settings > Privacy & security, or your organization's policy), and a team's "
            "checks can't run in it yet, so a team with writers can't run. Start a read-only team.")


def _cost(row: dict) -> float:
    cost = row.get("cost_usd")
    return float(cost) if isinstance(cost, (int, float)) and not isinstance(cost, bool) else 0.0


class TeamGovernance:
    """One team run's organization rules, spend and audit trail, on the host that runs it.

    The runtime makes one per run (service.py) from the team's captured model
    and configuration, and its runner hands it to each participant's
    execution guard (execution.py). ``kind`` is ``PERSONAL``, ``SHARING`` or
    ``MANAGED``; a policy refuses the last two.
    """

    def __init__(self, settings: Any, *, run_id: str, project: str, session: str = "", provider: str,
                 model: str, kind: str = PERSONAL, writers: bool = False, applies: bool = False,
                 checks: Iterable[dict] = ()) -> None:
        if kind not in {PERSONAL, SHARING, MANAGED}:
            raise ValueError("Unknown team execution kind")
        self.settings = settings
        self.run_id = str(run_id or "")
        self.project = str(project)
        self.session = str(session or "")
        self.provider, self.model = str(provider or ""), str(model or "")
        self.kind = kind
        self.writers, self.applies = writers is True, applies is True
        self.checks = tuple(copy.deepcopy(dict(check)) for check in checks if isinstance(check, dict))

    @classmethod
    def from_setup(cls, settings: Any, setup: dict, *, run_id: str, project: str, session: str,
                   personal: bool) -> TeamGovernance:
        """The rules for a retained team, from its captured desktop setup."""
        model = setup.get("model") if isinstance(setup.get("model"), dict) else {}
        autonomy = setup.get("autonomy") if isinstance(setup.get("autonomy"), dict) else {}
        kind = (MANAGED if not personal or setup.get("execution_mode") == "managed"
                else SHARING if str(setup.get("mode", "")).endswith("_collaboration") else PERSONAL)
        return cls(settings, run_id=run_id, project=project, session=session, provider=model.get("provider", ""),
                   model=model.get("model", ""), kind=kind, writers=bool(setup.get("write_roots")),
                   applies=autonomy.get("apply") is True, checks=setup.get("checks") or ())

    # ── Rules ────────────────────────────────────────────────────────────

    def refusal(self) -> str:
        """Why this team can't go on under the policy and settings in force now, or ''.

        Budgets aren't part of it (``dispatch_refusal``): they stop model
        requests, not the owner's reviews, checks or applications.
        """
        from ...policy import blocked_reason

        reason = blocked_reason()
        if reason:
            return reason
        if self.kind != PERSONAL:
            reason = unsupported_refusal()
            if reason:
                return reason
        return (model_refusal(self.provider, self.model)
                or mode_refusal(writers=self.writers, applies=self.applies)
                or check_refusal(self.checks, project=self.project, settings=self.settings)
                or (sandbox_refusal(self.settings) if self.writers else ""))

    def dispatch_refusal(self) -> str:
        """Why no new participant or model request may start now (rules or budgets), or ''."""
        return self.refusal() or self._budget(record=False)

    def start_refusal(self) -> str:
        """Why this team can't start, recorded in the audit log, or ''."""
        from ... import audit

        reason = self.refusal() or self._budget(record=True)
        if reason:
            self.record("team.refusal", action="start", reason=audit.content(reason))
        return reason

    def request_refusal(self, context: Any, purpose: str) -> str:
        """Before a participant's model request is reserved: why it can't start (audited), or ''.

        The execution guard then raises ``RequestRefused``: a known outcome,
        since nothing is reserved or sent.
        """
        from ... import audit

        reason = self.refusal() or self._budget(record=True, context=context)
        if reason:
            self._audit("team.request_refused", context, purpose=str(purpose), reason=audit.content(reason))
        return reason

    def spent(self, rows: Iterable[dict]) -> float:
        """This run's priced spend in these usage records."""
        prefix = f"team:{self.run_id}:"
        return sum(_cost(row) for row in rows if str(row.get("agent", "")).startswith(prefix))

    def _budget(self, *, record: bool, context: Any = None) -> str:
        """Why the budgets stop this team's next model request, or ''.

        A ``turn`` budget counts the whole team run, and its approvals and
        warnings are kept per run. Nobody can answer a budget's question
        during a team run, so a limit that asks stops it, as in any run that
        can't ask; an approval the person gave in a chat this period counts.
        """
        from ... import budgets, usage

        try:
            reason = budgets.unpriced_refusal(self.project, self.provider, self.model)
            if reason:
                return reason
            rules = [rule for rule in budgets.rules() if rule.applies_to(self.project)]
            if not rules:
                return ""
            rows = usage.ledger().records(since=budgets.period_key("month"))
            verdicts = budgets.evaluate(self.project, turn_spend=self.spent(rows), rows=rows, applicable=rules)
        except Exception:  # noqa: BLE001 - as for a chat turn, a budget that can't be read doesn't stop work
            logger.debug("Team budget check failed", exc_info=True)
            return ""
        for verdict in sorted(verdicts, key=lambda item: budgets.LEVELS.index(item.level), reverse=True):
            details = {"owner": verdict.rule.owner, "scope": verdict.rule.scope, "period": verdict.rule.period,
                       "spent_usd": round(verdict.spent, 6), "threshold_usd": verdict.threshold}
            message = verdict.message
            if verdict.rule.scope == "turn":
                message = message.replace("This turn has spent", "This team has spent", 1)
            if verdict.level == "block":
                if record:
                    self._audit("budget.block", context, **details)
                fix = ("Ask your administrator to raise it." if verdict.rule.owner
                       else "Raise it under Settings > Usage & cost to continue.")
                return f"Stopped: {message} {fix}"
            if verdict.level == "approve" and not budgets.approved(verdict, self.run_id):
                if record:
                    self._audit("budget.approval", context, **details, decision="unavailable")
                return f"Stopped: {message} Continuing needs approval, which a team run can't ask for."
            if verdict.level == "warn" and record and budgets.warned(verdict, self.run_id):
                self._audit("budget.warning", context, **details)
        return ""

    # ── Usage ────────────────────────────────────────────────────────────

    def agent(self, context: Any) -> str:
        """The usage and audit records' agent for one participant."""
        return f"team:{self.run_id}:{context.worker_id}"

    def record_request(self, context: Any, purpose: str, *, stats: Any, elapsed: float) -> None:
        """Record one observed request's usage (usage.py) and its ``model.usage`` audit entry.

        The execution guard calls this for every participant, in-process or
        in its own process; their sessions record nothing themselves.
        """
        from ... import usage

        if not isinstance(stats, dict):
            return  # No usage was observed; an unknown amount is never recorded as zero.
        label = PURPOSE if purpose == "primary" else f"{PURPOSE}_{purpose}"
        elapsed = round(max(0.0, float(elapsed or 0.0)), 3)
        record = usage.record(provider=self.provider, model=self.model, stats=stats, purpose=label,
                              session=self.session, project=self.project, agent=self.agent(context),
                              elapsed=elapsed)
        if record is not None:
            fields = {key: record[key] for key in ("input_tokens", "cached_tokens", "cache_write_tokens",
                                                   "output_tokens", "cost_usd", "price_source")}
        else:
            fields = usage.token_counts(stats)
        self._audit("model.usage", context, provider=self.provider, model=self.model, purpose=label,
                    elapsed=elapsed, **fields)

    # ── Audit ────────────────────────────────────────────────────────────

    def _audit(self, event: str, context: Any = None, **data: Any) -> None:
        from ... import audit

        fields: dict[str, Any] = {"agent": self.agent(context) if context is not None else "", "run": self.run_id}
        if context is not None:
            fields["attempt"] = context.attempt_id
        audit.record(event, session=self.session, project=audit.name(self.project), **fields, **data)

    def record(self, event: str, **data: Any) -> None:
        """One team-level audit record: team.start, .stop, .complete, .decision, .integration or .refusal."""
        self._audit(event, None, **data)

    def participant_started(self, context: Any, *, kind: str) -> None:
        self._audit("team.participant.start", context, kind=kind, provider=self.provider, model=self.model)

    def participant_ended(self, context: Any, *, kind: str, outcome: str, error: str = "") -> None:
        from ... import audit

        extra = {"error": audit.content(error)} if error else {}
        self._audit("team.participant.end", context, kind=kind, provider=self.provider, model=self.model,
                    outcome=str(outcome), **extra)

    def decision(self, decision: str, *, by: str, evidence: Any = None, **data: Any) -> None:
        """A decision on the team's work: the owner's, or the orchestrator's under the owner's grant."""
        from ... import audit

        extra = {"evidence": audit.content(evidence)} if evidence else {}
        event = "team.complete" if decision == "complete" else "team.decision"
        self.record(event, decision=decision, by=by, **{key: value for key, value in data.items()
                                                        if value is not None}, **extra)

    def integration_observed(self, kind: str, state: str, result: dict) -> None:
        """An integration step's outcome (workflow.IntegrationWorkflow): combining, a check or applying."""
        from ... import audit

        data: dict[str, Any] = {"step": str(kind), "outcome": str(state)}
        if result.get("state"):
            data["result"] = str(result["state"])
        candidate = result.get("candidate_id") or (result.get("id") if kind == "prepare_candidate" else None)
        if candidate:
            data["candidate"] = str(candidate)
        if result.get("check_key"):
            data["check"] = audit.name(result["check_key"])
        if isinstance(result.get("exit_code"), int):
            data["exit_code"] = result["exit_code"]
        if result.get("observed_revision"):
            data["revision"] = str(result["observed_revision"])
        self.record("team.integration", **data)
