"""The organization's review queue in Lumi Cloud (lumi_cloud/reviews.py there), for engine/review_gate.py.

When agent changes need review, a pull request the agent opens is reported
to the queue of the organization this computer is enrolled in, or else the
signed-in person's first organization. ``github_pr_view`` reports its state
as reviews come in. Nothing is reported while the person isn't signed in.
"""

from __future__ import annotations

from typing import Any


class ReviewQueue:
    def __init__(self, cloud: Any) -> None:
        self.cloud = cloud

    def _organization(self) -> str:
        status = self.cloud.status()
        if not status.get("signed_in"):
            return ""
        device = status.get("device") or {}
        if device.get("organization_id"):
            return str(device["organization_id"])
        organizations = (status.get("account") or {}).get("organizations") or []
        return str(organizations[0].get("id") or "") if organizations else ""

    def register(self, pr: dict) -> str:
        """Add an agent pull request to the queue; its id there, or "" when there's no organization."""
        organization = self._organization()
        if not organization:
            return ""
        answer = self.cloud.account_call("POST", "/api/v1/reviews", json={**pr, "organization_id": organization})
        return str(answer.get("id") or "")

    def update(self, review_id: str, status: str) -> None:
        self.cloud.account_call("POST", f"/api/v1/reviews/{review_id}", json={"status": status})
