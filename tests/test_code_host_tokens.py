"""GitHub and GitLab tokens go only to trusted hosts (engine/github_tools.token_hosts).

An issue link can name any host, so before this an ``@issue:`` mention or an
``issue_view`` call could send either token to a server someone else chose.
"""

from __future__ import annotations

import subprocess
import time

import httpx
import pytest

from lumi import net
from lumi import policy as lumi_policy
from lumi.engine import code_hosts, github_tools, issue_trackers
from lumi.engine.context_broker import ContextBroker
from lumi.engine.github_tools import GitHubError, check_token_host, token_hosts
from lumi.engine.tools import execute_tool

GITHUB_TOKEN = "ghp_" + "h" * 36
GITLAB_TOKEN = "glpat-" + "h" * 20
ENVIRONMENT = ("GITHUB_TOKEN", "GH_TOKEN", "GITLAB_TOKEN", "GITHUB_API_URL", "GITHUB_SERVER_URL", "LUMI_GITHUB_HOSTS",
               "LUMI_GITLAB_HOSTS", "CI_SERVER_HOST", "CI_API_V4_URL")


class _Settings:
    def __init__(self, **values):
        self.values = values

    def get(self, section, key=None, default=None):
        return self.values.get(f"{section}.{key}", default)


class Recorder:
    """Answers every host and tracker with an empty issue or list, and keeps each request."""

    def __init__(self):
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.path.endswith(("/comments", "/notes", "/pulls", "/merge_requests")):
            return httpx.Response(200, json=[])
        return httpx.Response(200, json={"number": 1, "iid": 1, "title": "Flaky login", "state": "open",
                                         "html_url": "https://github.com/acme/app/issues/1", "web_url": ""})

    @property
    def hosts(self) -> set[str]:
        return {request.url.host for request in self.requests}


@pytest.fixture
def api(monkeypatch):
    for name in ENVIRONMENT:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("GITHUB_TOKEN", GITHUB_TOKEN)
    monkeypatch.setenv("GITLAB_TOKEN", GITLAB_TOKEN)
    recorder = Recorder()
    transport = httpx.MockTransport(recorder)
    for module in (github_tools, code_hosts, issue_trackers):
        module.set_transport_for_tests(transport)
    github_tools.configure(None)
    yield recorder
    github_tools.configure(None)
    for module in (github_tools, code_hosts, issue_trackers):
        module.set_transport_for_tests(None)


def configure(**settings):
    github_tools.configure(_Settings(**settings))  # also code_hosts and issue_trackers


def project(path, origin: str) -> str:
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "remote", "add", "origin", origin], check=True)
    return str(path)


def call(name, args):
    return getattr(github_tools, f"exec_{name}")(args, time.time())


def test_host_lists_hold_host_names():
    assert net.host_names("GitHub.Acme.Corp, https://ghe.example.com:8443/api/v3\nghe.example.com") == [
        "github.acme.corp", "ghe.example.com"]
    assert net.host_names(["10.0.0.5", " code.acme.internal ", ""]) == ["10.0.0.5", "code.acme.internal"]
    for entry, message in (("*.acme.corp", "wildcards"), ("user:secret@ghe.example.com", "isn't a host name"),
                           ("not a host", "isn't a host name"), ("ghe.example.com:port", "isn't a host name")):
        with pytest.raises(ValueError, match=message):
            net.host_names([entry])
    with pytest.raises(ValueError, match="up to 50"):
        net.host_names([f"h{n}.example.com" for n in range(51)])
    with pytest.raises(ValueError, match="List host names"):
        net.host_names(5)
    # Read at use (the environment, a hand-edited file), bad entries are skipped.
    assert net.host_names(["ok.example.com", "*.bad", 5, None], strict=False) == ["ok.example.com"]
    assert net.host_names({"not": "a list"}, strict=False) == []


def test_which_hosts_get_each_token(api, monkeypatch):
    assert token_hosts("github") == (frozenset({"github.com", "api.github.com"}), False)
    assert token_hosts("gitlab") == (frozenset({"gitlab.com"}), False)
    configure(**{"code_hosts.github_hosts": ["ghe.acme.corp"], "code_hosts.gitlab_hosts": ["code.acme.corp"]})
    monkeypatch.setenv("LUMI_GITHUB_HOSTS", "ghe2.acme.corp, *.bad")
    monkeypatch.setenv("GITHUB_SERVER_URL", "https://actions.acme.corp")
    monkeypatch.setenv("GITHUB_API_URL", "https://actions.acme.corp/api/v3")
    monkeypatch.setenv("CI_SERVER_HOST", "ci.acme.corp")
    monkeypatch.setenv("CI_API_V4_URL", "https://ci.acme.corp/api/v4")
    assert token_hosts("github") == (frozenset({"github.com", "api.github.com", "ghe.acme.corp", "ghe2.acme.corp",
                                                "actions.acme.corp"}), False)
    assert token_hosts("gitlab") == (frozenset({"gitlab.com", "code.acme.corp", "ci.acme.corp"}), False)
    check_token_host("github", "ghe2.acme.corp", "api.github.com")
    with pytest.raises(GitHubError, match="evil.example isn't a GitHub host you've listed") as refused:
        check_token_host("github", "github.com", "EVIL.example")
    assert "Settings > Issue trackers > Your code hosts, or to LUMI_GITHUB_HOSTS" in str(refused.value)

    # A policy's list is the whole list: Settings and the environment don't add to it.
    lumi_policy.set_for_tests(lumi_policy.Policy(organization="Acme",
                                                 settings={"code_hosts.github_hosts": ["GHE.acme.corp"]}))
    assert token_hosts("github") == (frozenset({"github.com", "api.github.com", "ghe.acme.corp"}), True)
    with pytest.raises(GitHubError, match="isn't one of the GitHub hosts your organization's policy lists"):
        check_token_host("github", "ghe2.acme.corp")
    assert token_hosts("gitlab")[1] is False  # the GitLab list isn't locked


def test_an_issue_link_elsewhere_sends_no_token(api, tmp_path, monkeypatch):
    broker = ContextBroker(tmp_path)
    [item] = broker.resolve_mentions("Look at @issue:https://evil.example/acme/app/issues/1 please")
    assert "Couldn't read issue" in item.content and "evil.example isn't a GitHub host you've listed" in item.content
    viewed = execute_tool("issue_view", {"issue": "https://evil.example/group/app/-/issues/5"})
    assert viewed.is_error and "evil.example isn't a GitLab host you've listed" in viewed.output
    commented = execute_tool("issue_comment", {"issue": "https://evil.example/acme/app/issues/1", "body": "Fixed."})
    assert commented.is_error and "won't send it your GitHub token" in commented.output
    # A host that only looks like GitHub's, or hides it in sign-in details, is another host.
    for link in ("https://github.com.evil.example/acme/app/issues/1", "https://github.com@evil.example/acme/app/issues/1",
                 "https://api.github.com.evil.example/acme/app/issues/1"):
        assert execute_tool("issue_view", {"issue": link}).is_error
    assert api.requests == []  # refused before any request

    # GitHub Actions sends every request to GITHUB_API_URL; the host a link names must still be trusted.
    monkeypatch.setenv("GITHUB_API_URL", "https://api.github.com")
    assert execute_tool("issue_view", {"issue": "https://evil.example/acme/app/issues/1"}).is_error
    monkeypatch.delenv("GITHUB_API_URL")
    assert api.requests == []

    # The same links on github.com and gitlab.com are read, with the token.
    [ok] = broker.resolve_mentions("@issue:https://github.com/acme/app/issues/1")
    assert "Flaky login" in ok.content
    assert api.hosts == {"api.github.com"}
    assert all(request.headers["authorization"] == f"Bearer {GITHUB_TOKEN}" for request in api.requests)
    api.requests.clear()
    assert not execute_tool("issue_view", {"issue": "https://gitlab.com/group/app/-/issues/5"}).is_error
    assert api.hosts == {"gitlab.com"} and api.requests[0].headers["private-token"] == GITLAB_TOKEN


def test_a_listed_enterprise_host_gets_the_token(api, tmp_path, monkeypatch):
    cwd = project(tmp_path / "ghe", "https://github.acme.corp/acme/widgets.git")
    refused = call("github_pr_view", {"cwd": cwd})
    assert refused.is_error and "github.acme.corp isn't a GitHub host you've listed" in refused.output
    assert GITHUB_TOKEN not in refused.output and api.requests == []

    configure(**{"code_hosts.github_hosts": ["github.acme.corp"]})
    call("github_pr_view", {"cwd": cwd})
    assert api.hosts == {"github.acme.corp"}
    assert api.requests[0].url.path == "/api/v3/repos/acme/widgets/pulls"
    assert api.requests[0].headers["authorization"] == f"Bearer {GITHUB_TOKEN}"

    # A listed host needn't have "github" in its name to be recognized.
    api.requests.clear()
    named = project(tmp_path / "code", "git@code.acme.corp:acme/widgets.git")
    assert call("github_pr_view", {"cwd": named}).is_error and api.requests == []
    monkeypatch.setenv("LUMI_GITHUB_HOSTS", "code.acme.corp")
    call("github_pr_view", {"cwd": named})
    assert api.hosts == {"code.acme.corp"}

    # Under a policy, only its hosts: the setting and the environment above no longer count.
    api.requests.clear()
    lumi_policy.set_for_tests(lumi_policy.Policy(organization="Acme",
                                                 settings={"code_hosts.github_hosts": ["ghe.acme.corp"]}))
    for origin in (cwd, named):
        result = call("github_pr_view", {"cwd": origin})
        assert result.is_error and "your organization's policy lists" in result.output
    assert api.requests == []


def test_self_managed_gitlab_needs_listing(api, tmp_path, monkeypatch):
    cwd = project(tmp_path, "https://gitlab.acme.corp/team/app.git")
    refused = call("github_pr_view", {"cwd": cwd})
    assert refused.is_error and "gitlab.acme.corp isn't a GitLab host you've listed" in refused.output
    assert api.requests == []
    monkeypatch.setenv("LUMI_GITLAB_HOSTS", "gitlab.acme.corp")
    call("github_pr_view", {"cwd": cwd})
    assert api.hosts == {"gitlab.acme.corp"} and api.requests[0].headers["private-token"] == GITLAB_TOKEN

    # GitLab CI's own server is trusted, at the API address CI gives.
    api.requests.clear()
    monkeypatch.delenv("LUMI_GITLAB_HOSTS")
    monkeypatch.setenv("CI_SERVER_HOST", "gitlab.acme.corp")
    monkeypatch.setenv("CI_API_V4_URL", "https://gitlab.acme.corp/api/v4")
    call("github_pr_view", {"cwd": cwd})
    assert api.hosts == {"gitlab.acme.corp"}


def test_a_redirect_leaves_the_gitlab_token_behind(api):
    seen: list[httpx.Request] = []

    def storage(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.host == "gitlab.com":
            return httpx.Response(302, headers={"location": "https://storage.example/trace.txt"})
        return httpx.Response(200, text="step 1\nstep 2")

    code_hosts.set_transport_for_tests(httpx.MockTransport(storage))
    remote = code_hosts.Remote("gitlab", "gitlab.com", ("team/app",))
    text = code_hosts._request(remote, "GET", "https://gitlab.com/api/v4/projects/team%2Fapp/jobs/1/trace", text=True)
    assert text == "step 1\nstep 2"
    assert seen[0].headers["private-token"] == GITLAB_TOKEN
    assert seen[1].url.host == "storage.example" and "private-token" not in seen[1].headers


def test_settings_and_policies_list_host_names():
    from lumi.gui.ws_commands import _socket_setting_value

    assert _socket_setting_value("code_hosts", "github_hosts", ["GitHub.Acme.Corp", "https://ghe.acme.corp/"]) == [
        "github.acme.corp", "ghe.acme.corp"]
    assert _socket_setting_value("code_hosts", "gitlab_hosts", []) == []
    with pytest.raises(ValueError, match="wildcards"):
        _socket_setting_value("code_hosts", "gitlab_hosts", ["*.acme.corp"])
    with pytest.raises(ValueError, match="can't be changed from the app"):
        _socket_setting_value("code_hosts", "tokens", ["x"])

    base = {"schema": lumi_policy.SCHEMA}
    policy = lumi_policy.parse({**base, "settings": {"code_hosts.github_hosts": ["ghe.acme.corp"]}}, source="test")
    assert policy.locked("code_hosts", "github_hosts") and not policy.locked("code_hosts", "gitlab_hosts")
    for value, message in (("ghe.acme.corp", "must list host names"), (["*.acme.corp"], "wildcards"),
                           ([5], "isn't a host name")):
        with pytest.raises(lumi_policy.PolicyError, match=message):
            lumi_policy.parse({**base, "settings": {"code_hosts.gitlab_hosts": value}}, source="test")
