"""A second person approves risky commands before they run (organization policy, ``approvals``).

An organization's policy can list commands (``fnmatch`` patterns over the
whole command text, such as ``git push --force*`` or ``terraform apply*``)
that someone else in the organization approves before they run. After the
person using Lumi allows such a command (or their permission mode does),
the session:

1. asks Lumi Cloud (``set_requester``, lumi/approvals.py), which emails the
   organization's approvers and lists the request on its Approvals page;
2. waits, cancellably, for someone other than the requester to approve or
   deny it, up to the policy's ``wait_minutes``;
3. runs the command only if it was approved. A denial, no answer in time, a
   stop, or no Lumi Cloud to ask, and it doesn't run.

The command text sent to Lumi Cloud has saved keys and secret patterns
removed first.
"""

from __future__ import annotations

import fnmatch
import threading
import time
from dataclasses import dataclass, field
from typing import Any

SHELL_TOOLS = ("bash", "check_run")
ARGV_TOOLS = ("job_start", "preview_start")
_requester: Any = None


def set_requester(requester: Any) -> None:
    """Who asks Lumi Cloud: ``request(payload) -> {id, status, organization, approvers}`` and
    ``status(id) -> {status, approver}``; None when there's no Lumi Cloud to ask."""
    global _requester
    _requester = requester


def _command(tool: str, args: dict) -> str:
    command = args.get("command") if isinstance(args, dict) else None
    if tool in SHELL_TOOLS and isinstance(command, str):
        return " ".join(command.split())
    if tool in ARGV_TOOLS and isinstance(command, list):
        return " ".join(str(word) for word in command)
    return ""


def needed(tool: str, args: dict) -> str:
    """The policy pattern this call's command matches, or "" when it needs no second person."""
    from ..policy import current

    policy = current()
    patterns = getattr(policy, "approval_commands", ()) if policy else ()
    command = _command(tool, args) if patterns else ""
    return next((pattern for pattern in patterns if command and fnmatch.fnmatchcase(command, pattern)), "")


@dataclass
class Request:
    """One request to Lumi Cloud and what became of it."""

    pattern: str
    command: str
    state: str = "waiting"  # waiting, approved, denied, expired, cancelled or unavailable
    id: str = ""
    organization: str = ""
    approvers: list = field(default_factory=list)
    approver: str = ""
    wait_minutes: int = 30
    message: str = ""

    def event(self) -> dict:
        return {"state": self.state, "pattern": self.pattern, "command": self.command, "request_id": self.id,
                "organization": self.organization, "approvers": list(self.approvers), "approver": self.approver,
                "wait_minutes": self.wait_minutes, "message": self.message}


def ask(tool: str, args: dict, pattern: str, *, project: str = "") -> Request:
    """Send the request; ``state`` is "waiting", or "unavailable" when Lumi Cloud can't be asked."""
    from .. import secret_scan
    from ..policy import current

    policy = current()
    command = secret_scan.redact_text(_command(tool, args), patterns=True)[0][:2000]
    request = Request(pattern=pattern, command=command,
                      wait_minutes=int(getattr(policy, "approval_wait_minutes", 30) or 30))
    requester = _requester
    if requester is None:
        request.state = "unavailable"
        request.message = (f"This command needs a second person's approval ({pattern}), and Lumi isn't signed in to "
                           "your organization's Lumi Cloud to ask, so it didn't run. Tell the person.")
        return request
    try:
        answer = requester.request({"tool": tool, "command": command, "pattern": pattern, "project": project,
                                    "wait_minutes": request.wait_minutes})
    except Exception as exc:  # noqa: BLE001 - no answer means no approval
        request.state = "unavailable"
        request.message = f"Asking for approval failed ({exc}), so the command didn't run. Tell the person."
        return request
    request.id = str(answer.get("id") or "")
    request.organization = str(answer.get("organization") or "your organization")
    request.approvers = [str(name) for name in answer.get("approvers") or []][:10]
    if not request.id:
        request.state = "unavailable"
        request.message = "Lumi Cloud didn't take the approval request, so the command didn't run. Tell the person."
    return request


def await_decision(request: Request, *, cancel_event: threading.Event | None = None, poll: float = 3.0,
                   clock=time.monotonic, sleep=time.sleep) -> Request:
    """Wait for an approver's answer, a stop, or the time limit; sets ``state`` and ``message``."""
    if request.state != "waiting":
        return request
    deadline = clock() + request.wait_minutes * 60
    while True:
        if cancel_event is not None and cancel_event.is_set():
            request.state, request.message = "cancelled", "Stopped while waiting for approval; the command didn't run."
            return request
        try:
            answer = _requester.status(request.id) if _requester is not None else {"status": "unavailable"}
        except Exception:  # noqa: BLE001 - keep waiting through a dropped connection
            answer = {"status": "waiting"}
        status = str(answer.get("status") or "waiting")
        request.approver = str(answer.get("approver") or "")
        if status == "approved":
            request.state, request.message = "approved", ""
            return request
        if status == "denied":
            who = request.approver or "An approver"
            request.state = "denied"
            request.message = (f"{who} in {request.organization} denied running this ({request.pattern}). "
                               "Don't retry it; tell the person.")
            return request
        if status in ("expired", "unavailable") or clock() >= deadline:
            request.state = "expired"
            request.message = (f"Nobody in {request.organization} approved this within {request.wait_minutes} "
                               "minutes, so it didn't run. Tell the person.")
            return request
        sleep(poll)
