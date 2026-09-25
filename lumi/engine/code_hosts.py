"""Pull requests on GitLab, Bitbucket and Azure DevOps, for the ``github_pr_*`` tools.

The pull request tools (engine/github_tools.py) keep their names, which
policies and permission rules refer to, and work on whichever host the
project's ``origin`` remote points at:

* **GitLab**: gitlab.com, or a self-managed host whose name contains
  "gitlab", the host GitLab CI runs on (``CI_SERVER_HOST``) or one listed in
  ``LUMI_GITLAB_HOSTS``. Merge requests, their discussions and approvals, and
  pipeline jobs.
* **Bitbucket Cloud** (bitbucket.org): pull requests, comments, approvals and
  Pipelines steps.
* **Azure DevOps** (dev.azure.com, ``*.visualstudio.com``): pull requests,
  comment threads, reviewer votes and builds.

Tokens come from Settings > API keys (``gitlab``, ``bitbucket``,
``azure_devops``) or the environment (``GITLAB_TOKEN``; ``BITBUCKET_TOKEN``;
``AZURE_DEVOPS_TOKEN``, or ``SYSTEM_ACCESSTOKEN`` in Azure Pipelines). They go
only in request headers, and the secret scan removes Settings' values from
anything sent to a model. A Bitbucket app password is given as
``username:app-password``.
"""

from __future__ import annotations

import base64
import os
import re
from dataclasses import dataclass
from typing import Any, Callable
from urllib.parse import quote, unquote

from .github_tools import _LOG_TAIL_LINES, GitHubError, _branch, _clip, _git

_key_source: Callable[[str], str] = lambda name: ""  # noqa: E731
_transport: Any = None  # httpx.MockTransport in tests
NAMES = {"gitlab": "GitLab", "bitbucket": "Bitbucket", "azure": "Azure DevOps"}
_REMOTE = re.compile(r"^(?:(?:https?|ssh)://(?:[^@/]+@)?([^/:]+)(?::\d+)?/|[^@/:]+@([^:/]+):)(.+?)(?:\.git)?/?$")
_FAILED = {"failed", "failure", "canceled", "cancelled", "error", "stopped"}
_RUNNING = {"running", "pending", "created", "preparing", "waiting_for_resource", "scheduled", "inprogress",
            "in_progress", "notstarted", "postponed"}


class HostError(GitHubError):
    """A call to a code host that can't be made or failed; the message says what to do."""


def configure(settings: Any) -> None:
    """Read tokens from Settings at use, so a changed key applies at once."""
    global _key_source
    _key_source = ((lambda name: str(settings.get("api_keys", name, "") or "")) if settings is not None
                   else (lambda name: ""))


def set_transport_for_tests(transport: Any) -> None:
    global _transport
    _transport = transport


@dataclass(frozen=True)
class Remote:
    kind: str  # gitlab, bitbucket, azure
    host: str
    path: tuple[str, ...]  # gitlab: (namespace/project,); bitbucket: (workspace, repo); azure: (org, project, repo)

    @property
    def label(self) -> str:
        return "/".join(self.path)


def _gitlab_host(host: str) -> bool:
    extra = {h.strip().lower() for h in os.environ.get("LUMI_GITLAB_HOSTS", "").split(",") if h.strip()}
    return "gitlab" in host or host == os.environ.get("CI_SERVER_HOST", "").lower() or host in extra


def parse_remote(url: str) -> Remote | None:
    """The GitLab, Bitbucket or Azure DevOps repository a remote URL names; None for anything else."""
    match = _REMOTE.match(url.strip())
    if not match:
        return None
    host = (match.group(1) or match.group(2) or "").lower()
    parts = [unquote(part) for part in match.group(3).split("/") if part]
    if host in ("dev.azure.com", "ssh.dev.azure.com") or host.endswith(".visualstudio.com"):
        if host == "ssh.dev.azure.com" and len(parts) == 4 and parts[0] == "v3":
            return Remote("azure", host, (parts[1], parts[2], parts[3]))
        if host == "dev.azure.com" and len(parts) == 4 and parts[2] == "_git":
            return Remote("azure", host, (parts[0], parts[1], parts[3]))
        if host.endswith(".visualstudio.com"):
            if parts and parts[0].lower() == "defaultcollection":
                parts = parts[1:]
            if len(parts) == 3 and parts[1] == "_git":
                return Remote("azure", host, (host.split(".")[0], parts[0], parts[2]))
        return None
    if host == "bitbucket.org" and len(parts) == 2:
        return Remote("bitbucket", host, (parts[0], parts[1]))
    if _gitlab_host(host) and len(parts) >= 2:
        return Remote("gitlab", host, ("/".join(parts),))
    return None


def detect(cwd: str) -> Remote | None:
    """The project's origin as a GitLab, Bitbucket or Azure DevOps repository; None otherwise."""
    return parse_remote(_git(cwd, "remote", "get-url", "origin"))


def _secret(name: str, *env: str) -> str:
    return _key_source(name) or next((os.environ[e] for e in env if os.environ.get(e)), "")


def _auth(remote: Remote) -> dict[str, str]:
    kind = remote.kind
    if kind == "gitlab":
        token = _secret("gitlab", "GITLAB_TOKEN")
        if not token:
            raise HostError("No GitLab token: add one in Settings > API keys (GitLab), or set GITLAB_TOKEN.")
        return {"PRIVATE-TOKEN": token}
    if kind == "bitbucket":
        token = _secret("bitbucket", "BITBUCKET_TOKEN")
        if not token:
            raise HostError("No Bitbucket token: add an access token (or username:app-password) in Settings > "
                            "API keys (Bitbucket), or set BITBUCKET_TOKEN.")
        if ":" in token:
            return {"Authorization": "Basic " + base64.b64encode(token.encode()).decode()}
        return {"Authorization": f"Bearer {token}"}
    pat = _secret("azure_devops", "AZURE_DEVOPS_TOKEN", "AZURE_DEVOPS_EXT_PAT")
    if pat:
        return {"Authorization": "Basic " + base64.b64encode(f":{pat}".encode()).decode()}
    if os.environ.get("SYSTEM_ACCESSTOKEN"):
        return {"Authorization": f"Bearer {os.environ['SYSTEM_ACCESSTOKEN']}"}
    raise HostError("No Azure DevOps token: add a personal access token in Settings > API keys (Azure DevOps), "
                    "or set AZURE_DEVOPS_TOKEN.")


def _request(remote: Remote, method: str, url: str, *, json: Any = None, params: dict | None = None,
             text: bool = False) -> Any:
    import httpx

    from ..net import client_options

    headers = {**_auth(remote), "Accept": "text/plain" if text else "application/json", "User-Agent": "lumi"}
    try:
        with httpx.Client(**client_options(timeout=30.0, transport=_transport), follow_redirects=True) as client:
            response = client.request(method, url, headers=headers, json=json, params=params)
    except httpx.HTTPError as exc:
        raise HostError(f"{NAMES[remote.kind]} didn't answer: {type(exc).__name__}") from exc
    if response.status_code >= 400:
        message = ""
        try:
            data = response.json()
            message = str(data.get("message") or data.get("error") or "") if isinstance(data, dict) else ""
            if isinstance(data, dict) and isinstance(data.get("error"), dict):
                message = str(data["error"].get("message") or "")
        except ValueError:
            pass
        where = url.split("?")[0].split("/", 3)[-1]
        raise HostError(f"{NAMES[remote.kind]} {method} /{where} returned {response.status_code}"
                        f"{': ' + message[:300] if message else ''}")
    if text:
        return response.text
    return response.json() if response.content else {}


def _tail(text: str, lines: int) -> str:
    all_lines = str(text).splitlines()
    wanted = max(20, min(int(lines or _LOG_TAIL_LINES), 1_000))
    errors = [line for line in all_lines[:-wanted] if re.search(r"\b(error|failed|failure|exception)\b", line, re.I)][-40:]
    parts = ["Earlier error lines:\n" + "\n".join(errors)] if errors else []
    parts.append(f"Last {min(wanted, len(all_lines))} of {len(all_lines)} lines:\n" + "\n".join(all_lines[-wanted:]))
    return "\n\n".join(parts)


def _checks_lines(checks: list[tuple[str, str, str]]) -> tuple[list[str], list[str]]:
    """Lines for (name, state, job id) checks, and the failing names."""
    failing = [name for name, state, _ in checks if state.lower() in _FAILED]
    running = [name for name, state, _ in checks if state.lower() in _RUNNING]
    lines = [f"\nChecks: {len(checks)} ({len(failing)} failing, {len(running)} running)"]
    for name, state, job in checks:
        mark = "✗" if state.lower() in _FAILED else ("…" if state.lower() in _RUNNING else "✓")
        lines.append(f"  {mark} {name} ({state}{f', job {job}' if job else ''})")
    return lines, failing


def _require_branch(cwd: str) -> str:
    branch = _branch(cwd)
    if branch == "HEAD":
        raise HostError("The project is on a detached HEAD; create a branch first (git_branch_create).")
    return branch


# ── GitLab ──────────────────────────────────────────────────────────────────


class GitLab:
    def __init__(self, remote: Remote):
        self.remote = remote
        api = os.environ.get("CI_API_V4_URL", "") if remote.host == os.environ.get("CI_SERVER_HOST", "").lower() else ""
        self.api = (api or f"https://{remote.host}/api/v4").rstrip("/")
        self.project = f"{self.api}/projects/{quote(remote.path[0], safe='')}"

    def _get(self, path: str, **params) -> Any:
        return _request(self.remote, "GET", self.project + path, params=params or None)

    def _mr(self, cwd: str, number: Any) -> dict:
        if number:
            return self._get(f"/merge_requests/{int(number)}")
        branch = _branch(cwd)
        found = self._get("/merge_requests", source_branch=branch, state="opened")
        if not found:
            raise HostError(f"There is no open merge request for {branch}. Open one with github_pr_create.")
        return found[0]

    def view(self, cwd: str, number: Any = None) -> tuple[str, dict]:
        mr = self._mr(cwd, number)
        iid = mr["iid"]
        try:
            approvers = [a["user"]["username"] for a in self._get(f"/merge_requests/{iid}/approvals").get("approved_by", [])]
        except HostError:
            approvers = []
        discussions = self._get(f"/merge_requests/{iid}/discussions", per_page=100)
        pipeline = mr.get("head_pipeline") or mr.get("pipeline") or {}
        jobs = self._get(f"/pipelines/{pipeline['id']}/jobs", per_page=100) if pipeline.get("id") else []
        lines = [f"MR !{iid} {mr['title']!r} ({mr['state']}{', draft' if mr.get('draft') or mr.get('work_in_progress') else ''}) "
                 f"{mr['web_url']}",
                 f"{mr['target_branch']} <- {mr['source_branch']} at {str(mr.get('sha') or '')[:10]}"
                 + (f"; approved by {', '.join(approvers)}" if approvers else "")]
        check_lines, failing = _checks_lines([(j.get("name", "?"), j.get("status", "?"), str(j.get("id", "")))
                                              for j in jobs])
        lines += check_lines
        threads = [d for d in discussions if any(not n.get("system") for n in d.get("notes", []))]
        comments = 0
        lines.append(f"\nDiscussions ({len(threads)}):")
        for discussion in threads:
            notes = [n for n in discussion.get("notes", []) if not n.get("system")]
            first = notes[0]
            position = first.get("position") or {}
            where = f" {position.get('new_path')}:{position.get('new_line') or position.get('old_line') or '?'}" \
                if position.get("new_path") else ""
            resolved = " [resolved]" if first.get("resolved") else ""
            lines.append(f"- [id {discussion['id']}]{where}{resolved} @{(first.get('author') or {}).get('username')}: "
                         f"{_clip(first.get('body'))}")
            for reply in notes[1:]:
                lines.append(f"  - @{(reply.get('author') or {}).get('username')}: {_clip(reply.get('body'))}")
            comments += len(notes)
        lines.append(f"\nDescription:\n{_clip(mr.get('description'), 3_000)}")
        return "\n".join(lines), {"number": iid, "url": mr["web_url"], "failing_checks": failing,
                                  "review_comments": comments, "repo": self.remote.label, "host": "gitlab"}

    def check_log(self, cwd: str, job: Any, lines: int) -> tuple[str, dict]:
        text = _request(self.remote, "GET", f"{self.project}/jobs/{int(job)}/trace", text=True)
        return _tail(text, lines), {"job_id": int(job), "lines": len(str(text).splitlines())}

    def create(self, cwd: str, *, title: str, body: str, base: str, draft: bool, push: bool) -> tuple[str, dict]:
        branch = _require_branch(cwd)
        if push:
            _git(cwd, "push", "--set-upstream", "origin", branch)
        base = base or self._get("").get("default_branch", "main")
        if branch == base:
            raise HostError(f"The project is on {base}, the target branch; create a branch for the change first.")
        mr = _request(self.remote, "POST", f"{self.project}/merge_requests", json={
            "source_branch": branch, "target_branch": base, "title": f"Draft: {title}" if draft else title,
            "description": body})
        return f"Opened MR !{mr['iid']} {mr['web_url']} ({branch} -> {base}{', draft' if draft else ''})", {
            "number": mr["iid"], "url": mr["web_url"]}

    def comment(self, cwd: str, *, body: str, number: Any, reply_to: Any) -> tuple[str, dict]:
        mr = self._mr(cwd, number)
        if reply_to:
            note = _request(self.remote, "POST", f"{self.project}/merge_requests/{mr['iid']}/discussions/"
                            f"{quote(str(reply_to), safe='')}/notes", json={"body": body})
        else:
            note = _request(self.remote, "POST", f"{self.project}/merge_requests/{mr['iid']}/notes", json={"body": body})
        return f"Commented on MR !{mr['iid']}: {mr['web_url']}#note_{note.get('id')}", {
            "number": mr["iid"], "comment_id": note.get("id"), "url": mr["web_url"]}

    def update(self, cwd: str, *, number: Any, title: str, body: str | None, ready: bool) -> tuple[str, dict]:
        mr = self._mr(cwd, number)
        changes: dict[str, Any] = {}
        new_title = title or mr["title"]
        if ready:  # a GitLab draft is its title's prefix
            new_title = re.sub(r"^\s*(?:\[draft\]|draft:|\(draft\)|wip:|\[wip\])\s*", "", new_title, flags=re.I)
        if new_title != mr["title"]:
            changes["title"] = new_title
        if body is not None:
            changes["description"] = body
        if not changes:
            raise HostError("Nothing to change: give a title, a body, or ready=true.")
        mr = _request(self.remote, "PUT", f"{self.project}/merge_requests/{mr['iid']}", json=changes)
        return f"Updated MR !{mr['iid']} {mr['web_url']}", {"number": mr["iid"], "url": mr["web_url"]}


# ── Bitbucket Cloud ─────────────────────────────────────────────────────────


class Bitbucket:
    def __init__(self, remote: Remote):
        self.remote = remote
        workspace, repo = remote.path
        self.repo = f"https://api.bitbucket.org/2.0/repositories/{quote(workspace, safe='')}/{quote(repo, safe='')}"
        self.web = f"https://bitbucket.org/{workspace}/{repo}"

    def _get(self, path: str, **params) -> Any:
        return _request(self.remote, "GET", self.repo + path, params=params or None)

    def _pr(self, cwd: str, number: Any) -> dict:
        if number:
            return self._get(f"/pullrequests/{int(number)}")
        branch = _branch(cwd)
        found = self._get("/pullrequests", q=f'source.branch.name="{branch}" AND state="OPEN"').get("values", [])
        if not found:
            raise HostError(f"There is no open pull request for {branch}. Open one with github_pr_create.")
        return found[0]

    def _url(self, pr: dict) -> str:
        return ((pr.get("links") or {}).get("html") or {}).get("href") or f"{self.web}/pull-requests/{pr['id']}"

    def view(self, cwd: str, number: Any = None) -> tuple[str, dict]:
        pr = self._pr(cwd, number)
        n = pr["id"]
        comments = [c for c in self._get(f"/pullrequests/{n}/comments", pagelen=100).get("values", [])
                    if not c.get("deleted")]
        votes = [f"{(p.get('user') or {}).get('nickname') or (p.get('user') or {}).get('display_name')} "
                 f"{'approved' if p.get('approved') else p.get('state')}"
                 for p in pr.get("participants", []) if p.get("approved") or p.get("state")]
        branch = pr["source"]["branch"]["name"]
        checks = []
        for pipeline in self._get("/pipelines/", pagelen=20, sort="-created_on").get("values", []):
            if (pipeline.get("target") or {}).get("ref_name") != branch:
                continue
            for step in self._get(f"/pipelines/{quote(pipeline['uuid'], safe='')}/steps/").get("values", []):
                state = step.get("state") or {}
                status = (state.get("result") or {}).get("name") or state.get("name") or "?"
                checks.append((step.get("name") or "step", status, f"{pipeline['uuid']}:{step['uuid']}"))
            break  # the latest run for the branch
        head = str(((pr["source"].get("commit") or {}).get("hash")) or "")[:10]
        lines = [f"PR #{n} {pr['title']!r} ({pr['state'].lower()}{', draft' if pr.get('draft') else ''}) {self._url(pr)}",
                 f"{pr['destination']['branch']['name']} <- {branch} at {head}"
                 + (f"; reviews: {', '.join(votes)}" if votes else "")]
        check_lines, failing = _checks_lines(checks)
        lines += check_lines
        lines.append(f"\nComments ({len(comments)}):")
        for comment in comments:
            inline = comment.get("inline") or {}
            where = f" {inline.get('path')}:{inline.get('to') or inline.get('from') or '?'}" if inline.get("path") else ""
            reply = f" (reply to {comment['parent']['id']})" if comment.get("parent") else ""
            lines.append(f"- [id {comment['id']}]{where} @{(comment.get('user') or {}).get('nickname')}{reply}: "
                         f"{_clip((comment.get('content') or {}).get('raw'))}")
        lines.append(f"\nDescription:\n{_clip(pr.get('description'), 3_000)}")
        return "\n".join(lines), {"number": n, "url": self._url(pr), "failing_checks": failing,
                                  "review_comments": len(comments), "repo": self.remote.label, "host": "bitbucket"}

    def check_log(self, cwd: str, job: Any, lines: int) -> tuple[str, dict]:
        pipeline, _, step = str(job).partition(":")
        if not pipeline or not step:
            raise HostError("Give the job id github_pr_view lists for the step ({pipeline}:{step}).")
        text = _request(self.remote, "GET", f"{self.repo}/pipelines/{quote(pipeline, safe='')}/steps/"
                        f"{quote(step, safe='')}/log", text=True)
        return _tail(text, lines), {"job_id": str(job), "lines": len(str(text).splitlines())}

    def create(self, cwd: str, *, title: str, body: str, base: str, draft: bool, push: bool) -> tuple[str, dict]:
        branch = _require_branch(cwd)
        if push:
            _git(cwd, "push", "--set-upstream", "origin", branch)
        base = base or (self._get("").get("mainbranch") or {}).get("name", "main")
        if branch == base:
            raise HostError(f"The project is on {base}, the base branch; create a branch for the change first.")
        pr = _request(self.remote, "POST", f"{self.repo}/pullrequests", json={
            "title": title, "description": body, "draft": bool(draft),
            "source": {"branch": {"name": branch}}, "destination": {"branch": {"name": base}}})
        return f"Opened PR #{pr['id']} {self._url(pr)} ({branch} -> {base}{', draft' if draft else ''})", {
            "number": pr["id"], "url": self._url(pr)}

    def comment(self, cwd: str, *, body: str, number: Any, reply_to: Any) -> tuple[str, dict]:
        pr = self._pr(cwd, number)
        payload: dict[str, Any] = {"content": {"raw": body}}
        if reply_to:
            payload["parent"] = {"id": int(reply_to)}
        comment = _request(self.remote, "POST", f"{self.repo}/pullrequests/{pr['id']}/comments", json=payload)
        url = ((comment.get("links") or {}).get("html") or {}).get("href") or self._url(pr)
        return f"Commented on PR #{pr['id']}: {url}", {"number": pr["id"], "comment_id": comment.get("id"), "url": url}

    def update(self, cwd: str, *, number: Any, title: str, body: str | None, ready: bool) -> tuple[str, dict]:
        pr = self._pr(cwd, number)
        changes: dict[str, Any] = {}
        if title:
            changes["title"] = title
        if body is not None:
            changes["description"] = body
        if ready and pr.get("draft"):
            changes["draft"] = False
        if not changes:
            raise HostError("Nothing to change: give a title, a body, or ready=true.")
        pr = _request(self.remote, "PUT", f"{self.repo}/pullrequests/{pr['id']}", json=changes)
        return f"Updated PR #{pr['id']} {self._url(pr)}", {"number": pr["id"], "url": self._url(pr)}


# ── Azure DevOps ────────────────────────────────────────────────────────────


class AzureDevOps:
    VOTES = {10: "approved", 5: "approved with suggestions", -5: "waiting for the author", -10: "rejected"}

    def __init__(self, remote: Remote):
        self.remote = remote
        org, project, repo = remote.path
        self.base = f"https://dev.azure.com/{quote(org, safe='')}/{quote(project, safe='')}"
        self.repo = f"{self.base}/_apis/git/repositories/{quote(repo, safe='')}"
        self.web = f"{self.base}/_git/{quote(repo, safe='')}"

    def _call(self, method: str, url: str, **kwargs) -> Any:
        params = {"api-version": "7.1", **(kwargs.pop("params", None) or {})}
        return _request(self.remote, method, url, params=params, **kwargs)

    def _pr(self, cwd: str, number: Any) -> dict:
        if number:
            return self._call("GET", f"{self.repo}/pullrequests/{int(number)}")
        branch = _branch(cwd)
        found = self._call("GET", f"{self.repo}/pullrequests", params={
            "searchCriteria.sourceRefName": f"refs/heads/{branch}", "searchCriteria.status": "active"}).get("value", [])
        if not found:
            raise HostError(f"There is no active pull request for {branch}. Open one with github_pr_create.")
        return found[0]

    def _url(self, pr: dict) -> str:
        return f"{self.web}/pullrequest/{pr['pullRequestId']}"

    @staticmethod
    def _branch_name(ref: str) -> str:
        return str(ref or "").removeprefix("refs/heads/")

    def view(self, cwd: str, number: Any = None) -> tuple[str, dict]:
        pr = self._pr(cwd, number)
        n = pr["pullRequestId"]
        threads = [t for t in self._call("GET", f"{self.repo}/pullRequests/{n}/threads").get("value", [])
                   if any(c.get("commentType") != "system" for c in t.get("comments", [])) and not t.get("isDeleted")]
        votes = [f"{r.get('displayName')} {self.VOTES[r['vote']]}" for r in pr.get("reviewers", [])
                 if r.get("vote") in self.VOTES]
        builds = self._call("GET", f"{self.base}/_apis/build/builds", params={
            "branchName": f"refs/pull/{n}/merge", "$top": 10, "queryOrder": "queueTimeDescending"}).get("value", [])
        latest: dict[str, dict] = {}
        for build in builds:  # the newest run of each pipeline
            latest.setdefault(((build.get("definition") or {}).get("name") or "build"), build)
        checks = [(name, build.get("result") or build.get("status") or "?", str(build.get("id", "")))
                  for name, build in latest.items()]
        lines = [f"PR #{n} {pr['title']!r} ({pr['status']}{', draft' if pr.get('isDraft') else ''}) {self._url(pr)}",
                 f"{self._branch_name(pr['targetRefName'])} <- {self._branch_name(pr['sourceRefName'])} at "
                 f"{str((pr.get('lastMergeSourceCommit') or {}).get('commitId') or '')[:10]}"
                 + (f"; reviews: {', '.join(votes)}" if votes else "")]
        check_lines, failing = _checks_lines(checks)
        lines += check_lines
        comments = 0
        lines.append(f"\nThreads ({len(threads)}):")
        for thread in threads:
            notes = [c for c in thread.get("comments", []) if c.get("commentType") != "system"]
            context = thread.get("threadContext") or {}
            line = ((context.get("rightFileStart") or {}).get("line")) or "?"
            where = f" {context.get('filePath')}:{line}" if context.get("filePath") else ""
            status = f" [{thread['status']}]" if thread.get("status") not in (None, "active", "unknown") else ""
            first = notes[0]
            lines.append(f"- [id {thread['id']}]{where}{status} @{(first.get('author') or {}).get('displayName')}: "
                         f"{_clip(first.get('content'))}")
            for reply in notes[1:]:
                lines.append(f"  - @{(reply.get('author') or {}).get('displayName')}: {_clip(reply.get('content'))}")
            comments += len(notes)
        lines.append(f"\nDescription:\n{_clip(pr.get('description'), 3_000)}")
        return "\n".join(lines), {"number": n, "url": self._url(pr), "failing_checks": failing,
                                  "review_comments": comments, "repo": self.remote.label, "host": "azure"}

    def check_log(self, cwd: str, job: Any, lines: int) -> tuple[str, dict]:
        build = int(job)
        records = self._call("GET", f"{self.base}/_apis/build/builds/{build}/timeline").get("records", [])
        logged = [r for r in records if (r.get("log") or {}).get("id")]
        failed = [r for r in logged if r.get("result") == "failed"]
        # The first failed task, else the last log (where a build ends).
        record = failed[0] if failed else (logged[-1] if logged else None)
        if record is None:
            raise HostError(f"Build {build} has no logs yet.")
        text = self._call("GET", f"{self.base}/_apis/build/builds/{build}/logs/{record['log']['id']}", text=True)
        header = f"{record.get('name')} ({record.get('result') or record.get('state')})\n"
        return header + _tail(text, lines), {"job_id": build, "lines": len(str(text).splitlines())}

    def create(self, cwd: str, *, title: str, body: str, base: str, draft: bool, push: bool) -> tuple[str, dict]:
        branch = _require_branch(cwd)
        if push:
            _git(cwd, "push", "--set-upstream", "origin", branch)
        base = base or self._branch_name(self._call("GET", self.repo).get("defaultBranch") or "refs/heads/main")
        if branch == base:
            raise HostError(f"The project is on {base}, the target branch; create a branch for the change first.")
        pr = self._call("POST", f"{self.repo}/pullrequests", json={
            "sourceRefName": f"refs/heads/{branch}", "targetRefName": f"refs/heads/{base}", "title": title,
            "description": body, "isDraft": bool(draft)})
        return f"Opened PR #{pr['pullRequestId']} {self._url(pr)} ({branch} -> {base}{', draft' if draft else ''})", {
            "number": pr["pullRequestId"], "url": self._url(pr)}

    def comment(self, cwd: str, *, body: str, number: Any, reply_to: Any) -> tuple[str, dict]:
        pr = self._pr(cwd, number)
        n = pr["pullRequestId"]
        if reply_to:
            comment = self._call("POST", f"{self.repo}/pullRequests/{n}/threads/{int(reply_to)}/comments",
                                 json={"content": body, "parentCommentId": 1, "commentType": 1})
            thread_id = int(reply_to)
        else:
            thread = self._call("POST", f"{self.repo}/pullRequests/{n}/threads", json={
                "comments": [{"parentCommentId": 0, "content": body, "commentType": 1}], "status": 1})
            comment, thread_id = (thread.get("comments") or [{}])[0], thread.get("id")
        return f"Commented on PR #{n} (thread {thread_id}): {self._url(pr)}", {
            "number": n, "comment_id": comment.get("id"), "thread_id": thread_id, "url": self._url(pr)}

    def update(self, cwd: str, *, number: Any, title: str, body: str | None, ready: bool) -> tuple[str, dict]:
        pr = self._pr(cwd, number)
        changes: dict[str, Any] = {}
        if title:
            changes["title"] = title
        if body is not None:
            changes["description"] = body
        if ready and pr.get("isDraft"):
            changes["isDraft"] = False
        if not changes:
            raise HostError("Nothing to change: give a title, a body, or ready=true.")
        pr = self._call("PATCH", f"{self.repo}/pullrequests/{pr['pullRequestId']}", json=changes)
        return f"Updated PR #{pr['pullRequestId']} {self._url(pr)}", {"number": pr["pullRequestId"], "url": self._url(pr)}


def host_for(cwd: str) -> GitLab | Bitbucket | AzureDevOps | None:
    """The host behind the project's origin, or None for GitHub (github_tools) and anything else."""
    remote = detect(cwd)
    if remote is None:
        return None
    return {"gitlab": GitLab, "bitbucket": Bitbucket, "azure": AzureDevOps}[remote.kind](remote)
