"""GitHub pull request tools (lumi/engine/github_tools.py) against a mocked API."""

from __future__ import annotations

import json
import subprocess
import time

import httpx
import pytest

from lumi.engine import github_tools
from lumi.engine.github_tools import Repo, repo_for

TOKEN = "ghp_" + "t" * 36
SHA = "a" * 40


def git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


@pytest.fixture
def repo(tmp_path, monkeypatch):
    git(tmp_path, "init", "-q", "-b", "feature/pagination")
    git(tmp_path, "-c", "user.email=t@example.com", "-c", "user.name=T", "commit", "-q", "--allow-empty", "-m", "start")
    git(tmp_path, "remote", "add", "origin", "https://github.com/acme/widgets.git")
    monkeypatch.setenv("GITHUB_TOKEN", TOKEN)
    monkeypatch.delenv("GITHUB_API_URL", raising=False)
    monkeypatch.delenv("GITHUB_SERVER_URL", raising=False)
    github_tools.configure(None)
    yield tmp_path
    github_tools.set_transport_for_tests(None)


class FakeGitHub:
    def __init__(self):
        self.requests: list[httpx.Request] = []
        self.pr = {"number": 7, "node_id": "PR_7", "title": "Fix pagination", "state": "open", "draft": True,
                   "html_url": "https://github.com/acme/widgets/pull/7", "body": "Fixes the last page.",
                   "base": {"ref": "main"}, "head": {"ref": "feature/pagination", "sha": SHA}}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path, method = request.url.path, request.method
        if request.url.host == "logs.example":
            assert "authorization" not in request.headers  # the token stays with GitHub
            return httpx.Response(200, text="\n".join([*(f"step {i}" for i in range(300)),
                                                       "E   AssertionError: page 3 missing", "FAILED tests/test_page.py"]))
        assert request.headers["authorization"] == f"Bearer {TOKEN}"
        routes = {
            ("GET", "/repos/acme/widgets/pulls"): [self.pr],
            ("GET", "/repos/acme/widgets/pulls/7"): self.pr,
            ("GET", "/repos/acme/widgets/pulls/7/reviews"): [{"user": {"login": "alice"}, "state": "CHANGES_REQUESTED"}],
            ("GET", "/repos/acme/widgets/pulls/7/comments"): [
                {"id": 987, "path": "src/page.py", "line": 42, "position": 5, "user": {"login": "alice"},
                 "body": "Off by one on the last page."},
                {"id": 988, "path": "src/page.py", "line": 42, "position": None, "in_reply_to_id": 987,
                 "user": {"login": "bob"}, "body": "Agreed."}],
            ("GET", "/repos/acme/widgets/issues/7/comments"): [{"id": 555, "user": {"login": "carol"}, "body": "Ping?"}],
            ("GET", f"/repos/acme/widgets/commits/{SHA}/check-runs"): {"check_runs": [
                {"id": 123, "name": "pytest", "status": "completed", "conclusion": "failure"},
                {"id": 124, "name": "lint", "status": "completed", "conclusion": "success"},
                {"id": 125, "name": "build", "status": "in_progress", "conclusion": None}]},
            ("GET", "/repos/acme/widgets"): {"default_branch": "main"},
            ("POST", "/repos/acme/widgets/pulls"): {**self.pr, "number": 8, "html_url": "https://github.com/acme/widgets/pull/8"},
            ("POST", "/repos/acme/widgets/issues/7/comments"): {"id": 600, "html_url": "https://github.com/acme/widgets/pull/7#c600"},
            ("POST", "/repos/acme/widgets/pulls/7/comments/987/replies"): {"id": 601, "html_url": "https://x/r601"},
            ("PATCH", "/repos/acme/widgets/pulls/7"): {**self.pr, "title": "Fix pagination (v2)"},
            ("POST", "/graphql"): {"data": {"markPullRequestReadyForReview": {"pullRequest": {"isDraft": False}}}},
        }
        if (method, path) == ("GET", "/repos/acme/widgets/actions/jobs/123/logs"):
            return httpx.Response(302, headers={"location": "https://logs.example/job/123.txt"})
        if (method, path) in routes:
            return httpx.Response(200, json=routes[(method, path)])
        return httpx.Response(404, json={"message": "Not Found"})

    def sent(self, method, path):
        return [json.loads(r.content) for r in self.requests if r.method == method and r.url.path == path]


@pytest.fixture
def github(repo):
    fake = FakeGitHub()
    github_tools.set_transport_for_tests(httpx.MockTransport(fake))
    return fake


def call(name, args):
    handler = getattr(github_tools, f"exec_{name}")
    return handler(args, time.time())


class TestRemotes:
    @pytest.mark.parametrize("url, expected", [
        ("https://github.com/acme/widgets.git", Repo("github.com", "acme", "widgets")),
        ("https://x-access-token:secret@github.com/acme/widgets", Repo("github.com", "acme", "widgets")),
        ("git@github.com:acme/widgets.git", Repo("github.com", "acme", "widgets")),
        ("ssh://git@github.example.com:2222/acme/widgets.git", Repo("github.example.com", "acme", "widgets")),
    ])
    def test_the_origin_names_the_repository(self, repo, url, expected):
        git(repo, "remote", "set-url", "origin", url)
        assert repo_for(str(repo)) == expected

    def test_enterprise_hosts_and_a_foreign_remote(self, repo, monkeypatch):
        server = Repo("github.example.com", "acme", "widgets")
        assert server.api == "https://github.example.com/api/v3"
        monkeypatch.setenv("GITHUB_API_URL", "https://ghe.internal/api/v3")
        assert server.api == "https://ghe.internal/api/v3"
        git(repo, "remote", "set-url", "origin", "https://user:hunter2@git.example.org/acme/widgets.git")
        result = call("github_pr_view", {"cwd": str(repo)})
        assert result.is_error and "isn't a GitHub repository" in result.output and "hunter2" not in result.output
        # GitLab, Bitbucket and Azure DevOps have their own path (engine/code_hosts.py).
        monkeypatch.delenv("GITLAB_TOKEN", raising=False)
        git(repo, "remote", "set-url", "origin", "https://user:hunter2@gitlab.com/acme/widgets.git")
        result = call("github_pr_view", {"cwd": str(repo)})
        assert result.is_error and "No GitLab token" in result.output and "hunter2" not in result.output


class TestReading:
    def test_a_pull_request_with_reviews_and_checks(self, github, repo):
        result = call("github_pr_view", {"cwd": str(repo)})
        assert not result.is_error, result.output
        text = result.output
        assert "PR #7 'Fix pagination' (open, draft)" in text and "alice changes_requested" in text
        assert "✗ pytest (failure, job 123)" in text and "… build (in_progress, job 125)" in text
        assert "[id 987] src/page.py:42 @alice: Off by one" in text
        assert "[id 988] src/page.py:42 [outdated] @bob (reply to 987)" in text
        assert "[id 555] @carol: Ping?" in text
        assert result.metadata["failing_checks"] == ["pytest"]
        assert TOKEN not in text

    def test_a_failing_jobs_log(self, github, repo):
        result = call("github_check_log", {"cwd": str(repo), "job_id": 123, "lines": 30})
        assert "FAILED tests/test_page.py" in result.output and "Last 30 of 302 lines" in result.output
        assert call("github_check_log", {"cwd": str(repo)}).is_error

    def test_without_a_token(self, github, repo, monkeypatch):
        monkeypatch.delenv("GITHUB_TOKEN")
        monkeypatch.delenv("GH_TOKEN", raising=False)
        result = call("github_pr_view", {"cwd": str(repo)})
        assert result.is_error and "Settings > API keys" in result.output


class TestChanging:
    def test_open_a_pull_request(self, github, repo):
        result = call("github_pr_create", {"cwd": str(repo), "title": "Fix pagination", "body": "Details",
                                           "draft": True, "push": False})
        assert not result.is_error, result.output
        assert result.metadata["number"] == 8
        [sent] = github.sent("POST", "/repos/acme/widgets/pulls")
        assert sent == {"title": "Fix pagination", "body": "Details", "head": "feature/pagination",
                        "base": "main", "draft": True}
        git(repo, "checkout", "-q", "-b", "main")
        result = call("github_pr_create", {"cwd": str(repo), "title": "x", "push": False})
        assert result.is_error and "base branch" in result.output

    def test_comment_and_reply(self, github, repo):
        assert not call("github_pr_comment", {"cwd": str(repo), "body": "Fixed in 1a2b3c."}).is_error
        assert not call("github_pr_comment", {"cwd": str(repo), "body": "Done.", "reply_to": 987}).is_error
        assert github.sent("POST", "/repos/acme/widgets/issues/7/comments") == [{"body": "Fixed in 1a2b3c."}]
        assert github.sent("POST", "/repos/acme/widgets/pulls/7/comments/987/replies") == [{"body": "Done."}]
        assert call("github_pr_comment", {"cwd": str(repo), "body": " "}).is_error

    def test_update_and_mark_ready(self, github, repo):
        result = call("github_pr_update", {"cwd": str(repo), "number": 7, "title": "Fix pagination (v2)", "ready": True})
        assert not result.is_error, result.output
        assert github.sent("PATCH", "/repos/acme/widgets/pulls/7") == [{"title": "Fix pagination (v2)"}]
        [graphql] = github.sent("POST", "/graphql")
        assert graphql["variables"] == {"id": "PR_7"}
        assert call("github_pr_update", {"cwd": str(repo), "number": 7}).is_error


class TestPermissions:
    def test_reading_is_approved_and_changing_asks_in_auto_edit(self, github, repo):
        from lumi.engine.session import Session
        from tests.streaming_stub import StreamingBackend, done, text_delta, tool_call

        backend = StreamingBackend(scripts=[
            [tool_call("github_pr_view", {}, call_id="v1"), done()],
            [tool_call("github_pr_comment", {"body": "Fixed."}, call_id="c1"), done()],
            [text_delta("Done."), done()],
        ])
        session = Session(backend, auto_approve=False)
        session.autonomy_tier = "auto-edit"
        session.project_path = str(repo)
        asked = []
        events = list(session.run("Answer the review", on_permission=lambda name, args: asked.append(name) or False))
        assert asked == ["github_pr_comment"]
        results = {e["call_id"]: e for e in events if e.get("event") == "tool.result"}
        assert not results["v1"]["is_error"] and results["c1"]["denied"]
        assert github.sent("POST", "/repos/acme/widgets/issues/7/comments") == []
