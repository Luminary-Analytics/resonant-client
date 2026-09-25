"""Second-person approvals in Lumi Cloud (lumi_cloud/approvals.py there), for engine/second_approval.py.

A request goes to the organization this computer is enrolled in, or else the
signed-in person's first organization; Lumi Cloud emails its approvers and
lists the request on its Approvals page. The session then polls for the
answer. Without a sign-in there's no one to ask, and the command doesn't run.
"""

from __future__ import annotations

from typing import Any


class ApprovalRequester:
    def __init__(self, cloud: Any) -> None:
        self.cloud = cloud

    def _organization(self) -> tuple[str, str]:
        status = self.cloud.status()
        if not status.get("signed_in"):
            raise RuntimeError("Lumi isn't signed in to Lumi Cloud")
        device = status.get("device") or {}
        if device.get("organization_id"):
            return str(device["organization_id"]), str(device.get("organization_name") or "")
        organizations = (status.get("account") or {}).get("organizations") or []
        if not organizations:
            raise RuntimeError("you aren't in a Lumi Cloud organization")
        return str(organizations[0].get("id") or ""), str(organizations[0].get("name") or "")

    def request(self, payload: dict) -> dict:
        organization, name = self._organization()
        answer = self.cloud.account_call("POST", "/api/v1/approvals", json={**payload, "organization_id": organization})
        return {"id": answer.get("id"), "organization": name or answer.get("organization") or "",
                "approvers": answer.get("approvers") or []}

    def status(self, request_id: str) -> dict:
        return self.cloud.account_call("GET", f"/api/v1/approvals/{request_id}")
