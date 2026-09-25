"""Agent changes wait for a named reviewer (lumi/engine/review_gate.py): the deny rules, the pull
request the agent opens, and the review queue it joins."""

from __future__ import annotations

import time

import httpx
import pytest

from lumi.engine import github_tools, review_gate
from lumi.engine.policies import PolicyAction, policy_for_tier, project_execution_policy
from lumi.gui.ws_commands import _socket_setting_value
from tests.test_github_tools import FakeGitHub, repo  # noqa: F401 - the fixture


class _Settings:
    def __init__(self, **review):
        self.data = {"review": {"agent_changes": False, "reviewers": [], **review}}

    def get(self, section, key=None, default=None):
        values = self.data.get(section, {})
        return values if key is None else values.get(key, default)


@pytest.fixture(autouse=True)
def gate():
    yield
    review_gate.configure(None)
    review_gate.set_registrar(None)


class ReviewingGitHub(FakeGitHub):
    """The fake GitHub, also taking review requests."""

    def __call__(self, request):
        if request.method == "POST" and request.url.path.endswith("/requested_reviewers"):
            self.requests.append(request)
            return httpx.Response(201, json={})
        return super().__call__(request)


class Queue:
    def __init__(self):
        self.registered, self.updates = [], []

    def register(self, pr):
        self.registered.append(pr)
        return "rvw_1"

    def update(self, review_id, status):
        self.updates.append((review_id, status))


def test_off_by_default():
    review_gate.configure(_Settings())
    assert review_gate.policy_rules() == [] and review_gate.blocked("gh pr merge 7") == ""
    assert review_gate.pr_body("Fixes it.") == "Fixes it."


@pytest.mark.parametrize("command, reason", [
    ("gh pr merge 7 --squash", "Merging a pull request"),
    ("cd app && gh pr merge", "Merging a pull request"),
    ("glab mr merge 3", "Merging a merge request"),
    ("az repos pr update --id 5 --status completed", "Completing a pull request"),
    ("git push origin main", "Pushing to the default branch"),
    ("git push -u origin HEAD:master", "Pushing to the default branch"),
    ("git push origin refs/heads/trunk", "Pushing to the default branch"),
    ("git push -u origin feature/login", ""),
    ("git push origin main-refactor", ""),
    ("echo gh pr merge", ""),
    ("gh pr view 7", ""),
])
def test_what_is_refused_while_on(command, reason):
    review_gate.configure(_Settings(agent_changes=True, reviewers=["octocat"]))
    assert review_gate.blocked(command) == reason


def test_every_policy_denies_merging_before_anything_can_allow_it(tmp_path, monkeypatch):
    review_gate.configure(_Settings(agent_changes=True, reviewers=["octocat", "acme/platform"]))
    for tier in ("ask", "auto-edit", "full-auto"):
        assert policy_for_tier(tier).evaluate("bash", {"command": "gh pr merge 7"}) == PolicyAction.DENY
    # Not even a trusted repository's allow rule, or an organization's, reaches past it.
    (tmp_path / "lumi-policy.json").write_text(
        '{"rules": [{"tool_pattern": "bash", "action": "allow", "arg_patterns": {"command": "gh pr merge"}}]}',
        encoding="utf-8")
    from lumi import policy

    org = policy.parse({"schema": "lumi.policy/v1", "organization": "Acme", "shell": {"rules": [
        {"tool_pattern": "bash", "action": "allow", "arg_patterns": {"command": "git push"}}]}}, source="test")
    monkeypatch.setattr(policy, "current", lambda: org)
    merged = project_execution_policy("full-auto", str(tmp_path))
    assert merged.evaluate("bash", {"command": "gh pr merge 7"}) == PolicyAction.DENY
    assert merged.evaluate("bash", {"command": "git push origin main"}) == PolicyAction.DENY
    denied = next(rule for rule in merged.rules if rule.action == "deny" and "gh" in str(rule.arg_patterns))
    assert "@octocat, @acme/platform" in denied.reason
    review_gate.configure(_Settings())
    assert policy_for_tier("full-auto").evaluate("bash", {"command": "gh pr merge 7"}) != PolicyAction.DENY


def test_the_agents_pull_request_names_its_reviewers_and_joins_the_queue(repo):  # noqa: F811
    fake = ReviewingGitHub()
    github_tools.set_transport_for_tests(httpx.MockTransport(fake))
    github_tools.configure(_Settings(agent_changes=True, reviewers=["octocat", "acme/platform"]))
    queue = Queue()
    review_gate.set_registrar(queue)

    result = github_tools.exec_github_pr_create({"cwd": str(repo), "title": "Fix pagination", "body": "Fixes it.",
                                                 "push": False}, time.time())
    assert not result.is_error, result.output
    [created] = fake.sent("POST", "/repos/acme/widgets/pulls")
    assert created["body"].endswith("This change was made by an AI agent (Lumi). It needs review by @octocat, "
                                    "@acme/platform before it merges.")
    assert fake.sent("POST", "/repos/acme/widgets/pulls/8/requested_reviewers") == [
        {"reviewers": ["octocat"], "team_reviewers": ["platform"]}]
    assert "Requested review from @octocat, @acme/platform." in result.output
    assert "don't merge it or push to the default branch yourself" in result.output
    assert queue.registered == [{"repository": "github.com/acme/widgets", "number": 8,
                                 "url": "https://github.com/acme/widgets/pull/8", "title": "Fix pagination",
                                 "reviewers": ["octocat", "acme/platform"]}]

    # Viewing it reports its state to the queue (the fake answers with its address and a change request).
    fake.pr["html_url"] = "https://github.com/acme/widgets/pull/8"
    github_tools.exec_github_pr_view({"cwd": str(repo), "number": 7}, time.time())
    assert queue.updates == [("rvw_1", "changes_requested")]


def test_reviewers_setting_is_checked():
    assert _socket_setting_value("review", "reviewers", "@octocat\nacme/platform\n\noctocat") == [
        "octocat", "acme/platform"]
    with pytest.raises(ValueError, match="isn't a GitHub username"):
        _socket_setting_value("review", "reviewers", ["octo cat"])
    with pytest.raises(ValueError, match="on or off"):
        _socket_setting_value("review", "agent_changes", "yes")


def test_an_organization_can_lock_it():
    from lumi import policy

    locked = policy.parse({"schema": "lumi.policy/v1", "organization": "Acme",
                           "settings": {"review.agent_changes": True, "review.reviewers": ["octocat"]}}, source="test")
    assert locked.locked("review", "agent_changes") and locked.value("review", "reviewers") == ["octocat"]
    for bad in ({"review.agent_changes": "yes"}, {"review.reviewers": "octocat"}, {"review.reviewers": [1]}):
        with pytest.raises(policy.PolicyError):
            policy.parse({"schema": "lumi.policy/v1", "organization": "Acme", "settings": bad}, source="test")
