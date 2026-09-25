"""Pull request tools on GitLab, Bitbucket and Azure DevOps (lumi/engine/code_hosts.py), mocked APIs."""

from __future__ import annotations

import base64
import json
import subprocess
import time
from urllib.parse import unquote

import httpx
import pytest

from lumi.engine import code_hosts, github_tools
from lumi.engine.code_hosts import Remote, parse_remote

SHA = "b" * 40


def git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


def call(name, args):
    return getattr(github_tools, f"exec_{name}")(args, time.time())


@pytest.fixture
def repo(tmp_path, monkeypatch):
    git(tmp_path, "init", "-q", "-b", "feature/pagination")
    git(tmp_path, "-c", "user.email=t@example.com", "-c", "user.name=T", "commit", "-q", "--allow-empty", "-m", "start")
    for name in ("GITLAB_TOKEN", "BITBUCKET_TOKEN", "AZURE_DEVOPS_TOKEN", "AZURE_DEVOPS_EXT_PAT", "SYSTEM_ACCESSTOKEN",
                 "CI_SERVER_HOST", "CI_API_V4_URL", "LUMI_GITLAB_HOSTS"):
        monkeypatch.delenv(name, raising=False)
    github_tools.configure(None)
    yield tmp_path
    code_hosts.set_transport_for_tests(None)


class Fake:
    """A host API answering from routes keyed by (method, path); records what it was sent."""

    def __init__(self, routes: dict, *, text_routes: dict | None = None, check_auth=None):
        self.routes, self.text_routes, self.check_auth = routes, text_routes or {}, check_auth
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.check_auth:
            self.check_auth(request)
        key = (request.method, unquote(request.url.raw_path.decode().split("?")[0]))
        if key in self.text_routes:
            return httpx.Response(200, text=self.text_routes[key])
        if key in self.routes:
            value = self.routes[key]
            return httpx.Response(200, json=value(request) if callable(value) else value)
        return httpx.Response(404, json={"message": f"404 {key}"})

    def sent(self, method, path):
        return [json.loads(r.content) for r in self.requests
                if r.method == method and unquote(r.url.raw_path.decode().split("?")[0]) == path]


def install(fake):
    code_hosts.set_transport_for_tests(httpx.MockTransport(fake))
    return fake


@pytest.mark.parametrize("url, expected", [
    ("https://gitlab.com/acme/tools/widgets.git", Remote("gitlab", "gitlab.com", ("acme/tools/widgets",))),
    ("git@gitlab.example.com:acme/widgets.git", Remote("gitlab", "gitlab.example.com", ("acme/widgets",))),
    ("https://x:secret@bitbucket.org/acme/widgets.git", Remote("bitbucket", "bitbucket.org", ("acme", "widgets"))),
    ("git@bitbucket.org:acme/widgets.git", Remote("bitbucket", "bitbucket.org", ("acme", "widgets"))),
    ("https://acme@dev.azure.com/acme/My%20Project/_git/widgets", Remote("azure", "dev.azure.com",
                                                                         ("acme", "My Project", "widgets"))),
    ("git@ssh.dev.azure.com:v3/acme/Proj/widgets", Remote("azure", "ssh.dev.azure.com", ("acme", "Proj", "widgets"))),
    ("https://acme.visualstudio.com/DefaultCollection/Proj/_git/widgets",
     Remote("azure", "acme.visualstudio.com", ("acme", "Proj", "widgets"))),
    ("https://github.com/acme/widgets.git", None),
    ("https://git.example.org/acme/widgets.git", None),
])
def test_remotes(url, expected):
    assert parse_remote(url) == expected


def test_a_self_managed_gitlab_can_be_named(monkeypatch):
    assert parse_remote("https://code.acme.internal/team/app.git") is None
    monkeypatch.setenv("LUMI_GITLAB_HOSTS", "code.acme.internal, other.host")
    assert parse_remote("https://code.acme.internal/team/app.git").kind == "gitlab"


# ── GitLab ──────────────────────────────────────────────────────────────────

GL = "/api/v4/projects/acme/tools/widgets"  # the project path, url-encoded on the wire


def gitlab_fake():
    mr = {"iid": 12, "title": "Draft: Fix pagination", "state": "opened", "draft": True, "sha": SHA,
          "web_url": "https://gitlab.com/acme/tools/widgets/-/merge_requests/12", "description": "Fixes the last page.",
          "target_branch": "main", "source_branch": "feature/pagination", "head_pipeline": {"id": 77}}

    def token(request):
        assert request.headers["private-token"] == "glpat-test"
        assert "project" not in request.url.raw_path.decode() or "%2F" in request.url.raw_path.decode()

    routes = {
        ("GET", f"{GL}/merge_requests"): [mr],
        ("GET", f"{GL}/merge_requests/12"): mr,
        ("GET", f"{GL}/merge_requests/12/approvals"): {"approved_by": [{"user": {"username": "alice"}}]},
        ("GET", f"{GL}/merge_requests/12/discussions"): [
            {"id": "d1a", "notes": [
                {"id": 1, "body": "Off by one on the last page.", "author": {"username": "alice"},
                 "position": {"new_path": "src/page.py", "new_line": 42}},
                {"id": 2, "body": "Agreed.", "author": {"username": "bob"}}]},
            {"id": "sys", "notes": [{"id": 3, "system": True, "body": "added 1 commit"}]}],
        ("GET", f"{GL}/pipelines/77/jobs"): [{"id": 501, "name": "pytest", "status": "failed"},
                                             {"id": 502, "name": "lint", "status": "success"},
                                             {"id": 503, "name": "build", "status": "running"}],
        ("GET", GL): {"default_branch": "main"},
        ("POST", f"{GL}/merge_requests"): lambda r: {"iid": 13, "web_url": "https://gitlab.com/x/-/merge_requests/13"},
        ("POST", f"{GL}/merge_requests/12/notes"): {"id": 900},
        ("POST", f"{GL}/merge_requests/12/discussions/d1a/notes"): {"id": 901},
        ("PUT", f"{GL}/merge_requests/12"): lambda r: {**mr, **json.loads(r.content)},
    }
    text = {("GET", f"{GL}/jobs/501/trace"): "\n".join([*(f"step {i}" for i in range(300)),
                                                        "E   AssertionError: page 3 missing"])}
    return Fake(routes, text_routes=text, check_auth=token)


def test_gitlab_merge_requests(repo, monkeypatch):
    git(repo, "remote", "add", "origin", "https://gitlab.com/acme/tools/widgets.git")
    fake = install(gitlab_fake())
    missing = call("github_pr_view", {"cwd": str(repo)})
    assert missing.is_error and "No GitLab token" in missing.output
    monkeypatch.setenv("GITLAB_TOKEN", "glpat-test")

    view = call("github_pr_view", {"cwd": str(repo)})
    assert not view.is_error, view.output
    text = view.output
    assert "MR !12 'Draft: Fix pagination' (opened, draft)" in text and "approved by alice" in text
    assert "✗ pytest (failed, job 501)" in text and "… build (running, job 503)" in text
    assert "[id d1a] src/page.py:42 @alice: Off by one" in text and "  - @bob: Agreed." in text
    assert "added 1 commit" not in text  # system notes
    assert view.metadata["failing_checks"] == ["pytest"] and view.metadata["host"] == "gitlab"

    log = call("github_check_log", {"cwd": str(repo), "job_id": "501", "lines": 20})
    assert "page 3 missing" in log.output and "Last 20 of 301 lines" in log.output

    created = call("github_pr_create", {"cwd": str(repo), "title": "Fix pagination", "draft": True, "push": False})
    assert "Opened MR !13" in created.output
    assert fake.sent("POST", f"{GL}/merge_requests")[0] == {
        "source_branch": "feature/pagination", "target_branch": "main", "title": "Draft: Fix pagination",
        "description": ""}

    assert "Commented on MR !12" in call("github_pr_comment", {"cwd": str(repo), "body": "Fixed."}).output
    call("github_pr_comment", {"cwd": str(repo), "body": "Done.", "reply_to": "d1a"})
    assert fake.sent("POST", f"{GL}/merge_requests/12/discussions/d1a/notes") == [{"body": "Done."}]

    ready = call("github_pr_update", {"cwd": str(repo), "ready": True})
    assert "Updated MR !12" in ready.output
    assert fake.sent("PUT", f"{GL}/merge_requests/12") == [{"title": "Fix pagination"}]  # the draft prefix removed
    nothing = call("github_pr_update", {"cwd": str(repo), "title": "Draft: Fix pagination"})
    assert nothing.is_error and "Nothing to change" in nothing.output


# ── Bitbucket ───────────────────────────────────────────────────────────────

BB = "/2.0/repositories/acme/widgets"
PIPE, STEP = "{p-1}", "{s-1}"


def bitbucket_fake(expected_auth):
    pr = {"id": 5, "title": "Fix pagination", "state": "OPEN", "draft": True, "description": "Fixes the last page.",
          "links": {"html": {"href": "https://bitbucket.org/acme/widgets/pull-requests/5"}},
          "source": {"branch": {"name": "feature/pagination"}, "commit": {"hash": SHA}},
          "destination": {"branch": {"name": "main"}},
          "participants": [{"user": {"nickname": "alice"}, "approved": True},
                           {"user": {"nickname": "bob"}, "approved": False, "state": "changes_requested"}]}

    def auth(request):
        assert request.headers["authorization"] == expected_auth

    routes = {
        ("GET", f"{BB}/pullrequests"): {"values": [pr]},
        ("GET", f"{BB}/pullrequests/5"): pr,
        ("GET", f"{BB}/pullrequests/5/comments"): {"values": [
            {"id": 31, "content": {"raw": "Off by one."}, "user": {"nickname": "alice"},
             "inline": {"path": "src/page.py", "to": 42}},
            {"id": 32, "content": {"raw": "Agreed."}, "user": {"nickname": "bob"}, "parent": {"id": 31}},
            {"id": 33, "deleted": True, "content": {"raw": ""}, "user": {}}]},
        ("GET", f"{BB}/pipelines/"): {"values": [
            {"uuid": "{p-0}", "target": {"ref_name": "other"}},
            {"uuid": PIPE, "target": {"ref_name": "feature/pagination"}}]},
        ("GET", f"{BB}/pipelines/{PIPE}/steps/"): {"values": [
            {"uuid": STEP, "name": "Test", "state": {"name": "COMPLETED", "result": {"name": "FAILED"}}},
            {"uuid": "{s-2}", "name": "Deploy", "state": {"name": "IN_PROGRESS"}}]},
        ("GET", BB): {"mainbranch": {"name": "main"}},
        ("POST", f"{BB}/pullrequests"): {"id": 6, "links": {"html": {"href": "https://bitbucket.org/acme/widgets/pull-requests/6"}}},
        ("POST", f"{BB}/pullrequests/5/comments"): {"id": 40, "links": {"html": {"href": "https://bb/c40"}}},
        ("PUT", f"{BB}/pullrequests/5"): lambda r: {**pr, **json.loads(r.content)},
    }
    text = {("GET", f"{BB}/pipelines/{PIPE}/steps/{STEP}/log"): "npm test\nError: page 3 missing\n"}
    return Fake(routes, text_routes=text, check_auth=auth)


def test_bitbucket_pull_requests(repo, monkeypatch):
    git(repo, "remote", "add", "origin", "git@bitbucket.org:acme/widgets.git")
    monkeypatch.setenv("BITBUCKET_TOKEN", "me:app-password")
    fake = install(bitbucket_fake("Basic " + base64.b64encode(b"me:app-password").decode()))

    view = call("github_pr_view", {"cwd": str(repo)})
    assert not view.is_error, view.output
    text = view.output
    assert "PR #5 'Fix pagination' (open, draft)" in text and "alice approved, bob changes_requested" in text
    assert f"✗ Test (FAILED, job {PIPE}:{STEP})" in text and "… Deploy (IN_PROGRESS" in text
    assert "[id 31] src/page.py:42 @alice: Off by one." in text and "[id 32] @bob (reply to 31): Agreed." in text
    assert "[id 33]" not in text

    log = call("github_check_log", {"cwd": str(repo), "job_id": f"{PIPE}:{STEP}"})
    assert "page 3 missing" in log.output
    assert "Give the job id" in call("github_check_log", {"cwd": str(repo), "job_id": "123"}).output

    call("github_pr_create", {"cwd": str(repo), "title": "Fix pagination", "body": "Fixes it.", "push": False})
    assert fake.sent("POST", f"{BB}/pullrequests")[0] == {
        "title": "Fix pagination", "description": "Fixes it.", "draft": False,
        "source": {"branch": {"name": "feature/pagination"}}, "destination": {"branch": {"name": "main"}}}
    call("github_pr_comment", {"cwd": str(repo), "body": "Done.", "reply_to": "31"})
    assert fake.sent("POST", f"{BB}/pullrequests/5/comments") == [{"content": {"raw": "Done."}, "parent": {"id": 31}}]
    call("github_pr_update", {"cwd": str(repo), "ready": True, "body": "Updated."})
    assert fake.sent("PUT", f"{BB}/pullrequests/5") == [{"description": "Updated.", "draft": False}]


def test_bitbucket_access_tokens_are_bearer(repo, monkeypatch):
    git(repo, "remote", "add", "origin", "https://bitbucket.org/acme/widgets.git")
    monkeypatch.setenv("BITBUCKET_TOKEN", "ATCTT3xFfGN0-token")
    install(bitbucket_fake("Bearer ATCTT3xFfGN0-token"))
    assert not call("github_pr_view", {"cwd": str(repo)}).is_error


# ── Azure DevOps ────────────────────────────────────────────────────────────

AZ = "/acme/My Project/_apis"
AZR = f"{AZ}/git/repositories/widgets"


def azure_fake(expected_auth):
    pr = {"pullRequestId": 41, "title": "Fix pagination", "status": "active", "isDraft": True,
          "description": "Fixes the last page.", "sourceRefName": "refs/heads/feature/pagination",
          "targetRefName": "refs/heads/main", "lastMergeSourceCommit": {"commitId": SHA},
          "reviewers": [{"displayName": "Alice", "vote": 10}, {"displayName": "Bob", "vote": -5},
                        {"displayName": "Carol", "vote": 0}]}

    def auth(request):
        assert request.headers["authorization"] == expected_auth
        assert request.url.params.get("api-version") == "7.1"

    routes = {
        ("GET", f"{AZR}/pullrequests"): {"value": [pr]},
        ("GET", f"{AZR}/pullrequests/41"): pr,
        ("GET", f"{AZR}/pullRequests/41/threads"): {"value": [
            {"id": 7, "status": "active", "threadContext": {"filePath": "/src/page.py", "rightFileStart": {"line": 42}},
             "comments": [{"id": 1, "content": "Off by one.", "author": {"displayName": "Alice"}, "commentType": "text"},
                          {"id": 2, "content": "Agreed.", "author": {"displayName": "Bob"}, "commentType": "text"}]},
            {"id": 8, "comments": [{"id": 1, "content": "Policy updated", "commentType": "system"}]}]},
        ("GET", f"{AZ}/build/builds"): {"value": [
            {"id": 900, "result": "failed", "definition": {"name": "CI"}},
            {"id": 899, "result": "succeeded", "definition": {"name": "CI"}}]},
        ("GET", f"{AZ}/build/builds/900/timeline"): {"records": [
            {"name": "Checkout", "result": "succeeded", "log": {"id": 2}},
            {"name": "Run tests", "result": "failed", "log": {"id": 5}}]},
        ("GET", AZR): {"defaultBranch": "refs/heads/main"},
        ("POST", f"{AZR}/pullrequests"): {"pullRequestId": 42},
        ("POST", f"{AZR}/pullRequests/41/threads"): {"id": 9, "comments": [{"id": 1}]},
        ("POST", f"{AZR}/pullRequests/41/threads/7/comments"): {"id": 3},
        ("PATCH", f"{AZR}/pullrequests/41"): lambda r: {**pr, **json.loads(r.content)},
    }
    text = {("GET", f"{AZ}/build/builds/900/logs/5"): "##[section]Starting: Run tests\n##[error]page 3 missing\n"}
    return Fake(routes, text_routes=text, check_auth=auth)


def test_azure_devops_pull_requests(repo, monkeypatch):
    git(repo, "remote", "add", "origin", "https://acme@dev.azure.com/acme/My%20Project/_git/widgets")
    monkeypatch.setenv("AZURE_DEVOPS_TOKEN", "pat-value")
    fake = install(azure_fake("Basic " + base64.b64encode(b":pat-value").decode()))

    view = call("github_pr_view", {"cwd": str(repo)})
    assert not view.is_error, view.output
    text = view.output
    assert "PR #41 'Fix pagination' (active, draft)" in text
    assert "main <- feature/pagination" in text and "Alice approved, Bob waiting for the author" in text
    assert "✗ CI (failed, job 900)" in text and text.count("CI (") == 1  # the newest run of each pipeline
    assert "[id 7] /src/page.py:42 @Alice: Off by one." in text and "  - @Bob: Agreed." in text
    assert "Policy updated" not in text
    assert view.metadata["url"] == "https://dev.azure.com/acme/My%20Project/_git/widgets/pullrequest/41"

    log = call("github_check_log", {"cwd": str(repo), "job_id": "900"})
    assert log.output.startswith("Run tests (failed)") and "page 3 missing" in log.output

    call("github_pr_create", {"cwd": str(repo), "title": "Fix pagination", "draft": True, "push": False})
    assert fake.sent("POST", f"{AZR}/pullrequests")[0] == {
        "sourceRefName": "refs/heads/feature/pagination", "targetRefName": "refs/heads/main",
        "title": "Fix pagination", "description": "", "isDraft": True}
    new_thread = call("github_pr_comment", {"cwd": str(repo), "body": "Fixed."})
    assert "thread 9" in new_thread.output
    call("github_pr_comment", {"cwd": str(repo), "body": "Done.", "reply_to": "7"})
    assert fake.sent("POST", f"{AZR}/pullRequests/41/threads/7/comments") == [
        {"content": "Done.", "parentCommentId": 1, "commentType": 1}]
    call("github_pr_update", {"cwd": str(repo), "ready": True})
    assert fake.sent("PATCH", f"{AZR}/pullrequests/41") == [{"isDraft": False}]


def test_azure_pipelines_use_the_job_token(repo, monkeypatch):
    git(repo, "remote", "add", "origin", "git@ssh.dev.azure.com:v3/acme/My%20Project/widgets")
    monkeypatch.setenv("SYSTEM_ACCESSTOKEN", "job-token")
    install(azure_fake("Bearer job-token"))
    assert not call("github_pr_view", {"cwd": str(repo)}).is_error


def test_settings_tokens_and_errors_are_safe(repo, monkeypatch):
    git(repo, "remote", "add", "origin", "https://gitlab.com/acme/tools/widgets.git")

    class Settings:
        def get(self, section, key=None, default=None):
            return {"gitlab": "glpat-test"}.get(key, default) if section == "api_keys" else default

    github_tools.configure(Settings())
    fake = install(gitlab_fake())
    assert not call("github_pr_view", {"cwd": str(repo)}).is_error
    fake.routes.pop(("GET", f"{GL}/merge_requests"))
    fake.routes[("GET", f"{GL}/merge_requests")] = []
    none = call("github_pr_view", {"cwd": str(repo)})
    assert none.is_error and "no open merge request for feature/pagination" in none.output
    missing = call("github_pr_view", {"cwd": str(repo), "number": 99})
    assert missing.is_error and "returned 404" in missing.output and "glpat-test" not in missing.output
