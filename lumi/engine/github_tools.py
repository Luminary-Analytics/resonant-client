"""Pull request tools: read a PR's reviews and checks, open, update and comment
on it. They're named for GitHub and also work on GitLab, Bitbucket and Azure
DevOps (engine/code_hosts.py), chosen by the ``origin`` remote.

The repository comes from the project's ``origin`` remote (github.com or a
GitHub Enterprise Server host). The token is ``api_keys.github`` from
Settings, or ``GITHUB_TOKEN`` / ``GH_TOKEN`` (set in GitHub Actions); it is
sent only in the request header, never in tool output, and the secret scan
removes its value from anything sent to a model.

``github_pr_view`` and ``github_check_log`` only read and are approved like
``git_log``. ``github_pr_create``, ``github_pr_comment`` and
``github_pr_update`` change things other people see, so auto-edit asks first.
"""

from __future__ import annotations

import os
import re
import subprocess
import time
from dataclasses import dataclass
from typing import Any, Callable

_token_source: Callable[[], str] = lambda: ""  # noqa: E731
_transport: Any = None  # httpx.MockTransport in tests
_BODY_LIMIT = 1_500
_LOG_TAIL_LINES = 150


def configure(settings: Any) -> None:
    """Read the token from Settings at use, so a changed key applies at once."""
    global _token_source
    _token_source = (lambda: str(settings.get("api_keys", "github", "") or "")) if settings is not None else (lambda: "")
    from . import code_hosts, issue_trackers, review_gate  # the same Settings

    code_hosts.configure(settings)
    issue_trackers.configure(settings)
    review_gate.configure(settings)


def set_transport_for_tests(transport: Any) -> None:
    global _transport
    _transport = transport


def _token() -> str:
    return _token_source() or os.environ.get("GITHUB_TOKEN", "") or os.environ.get("GH_TOKEN", "")


class GitHubError(Exception):
    """A GitHub call that can't be made or failed; the message says what to do."""


@dataclass(frozen=True)
class Repo:
    host: str
    owner: str
    name: str

    @property
    def api(self) -> str:
        override = os.environ.get("GITHUB_API_URL", "").rstrip("/")
        if override:
            return override
        return "https://api.github.com" if self.host == "github.com" else f"https://{self.host}/api/v3"

    @property
    def path(self) -> str:
        return f"/repos/{self.owner}/{self.name}"


_REMOTE = re.compile(r"^(?:https?://(?:[^@/]+@)?([^/:]+)(?::\d+)?/|git@([^:]+):|ssh://git@([^/:]+)(?::\d+)?/)"
                     r"([^/]+)/(.+?)(?:\.git)?/?$")


def _git(cwd: str, *args: str) -> str:
    try:
        completed = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise GitHubError(f"git {' '.join(args)} failed: {exc}") from exc
    if completed.returncode != 0:
        raise GitHubError((completed.stderr or completed.stdout).strip() or f"git {' '.join(args)} failed")
    return completed.stdout.strip()


def _github_host(host: str) -> bool:
    """github.com, an Enterprise Server host (by name, or the one Actions runs on)."""
    from urllib.parse import urlsplit

    server = (urlsplit(os.environ.get("GITHUB_SERVER_URL", "")).hostname or "").lower()
    return host == "github.com" or "github" in host or (bool(server) and host == server)


def repo_for(cwd: str) -> Repo:
    url = _git(cwd, "remote", "get-url", "origin")
    match = _REMOTE.match(url)
    host = (match.group(1) or match.group(2) or match.group(3) or "").lower() if match else ""
    if not match or not _github_host(host):
        # Never echo credentials a remote URL may carry.
        raise GitHubError(f"The origin remote ({url.split('@')[-1]}) isn't a GitHub repository.")
    return Repo(host=host, owner=match.group(4), name=match.group(5))


def _branch(cwd: str) -> str:
    """The current branch, also before its first commit; 'HEAD' when detached."""
    try:
        return _git(cwd, "symbolic-ref", "--short", "-q", "HEAD")
    except GitHubError:
        return "HEAD"


def _request(repo: Repo, method: str, path: str, *, json: Any = None, params: dict | None = None,
             accept: str = "application/vnd.github+json") -> Any:
    import httpx

    from ..net import client_options

    token = _token()
    if not token:
        raise GitHubError("No GitHub token: add one in Settings > API keys (GitHub), or set GITHUB_TOKEN.")
    headers = {"Authorization": f"Bearer {token}", "Accept": accept, "X-GitHub-Api-Version": "2022-11-28",
               "User-Agent": "lumi"}
    try:
        # Log downloads redirect to storage on another host; httpx drops the
        # Authorization header when a redirect leaves the API's origin.
        # GraphQL lives at /api/graphql on GitHub Enterprise Server, not under /api/v3.
        url = (repo.api.removesuffix("/v3") + path) if path == "/graphql" else repo.api + path
        with httpx.Client(**client_options(timeout=30.0, transport=_transport), follow_redirects=True) as client:
            response = client.request(method, url, headers=headers, json=json, params=params)
    except httpx.HTTPError as exc:
        raise GitHubError(f"GitHub didn't answer: {type(exc).__name__}") from exc
    if response.status_code >= 400:
        try:
            message = str(response.json().get("message") or "")
        except ValueError:
            message = ""
        raise GitHubError(f"GitHub {method} {path} returned {response.status_code}{': ' + message if message else ''}")
    if accept == "text/plain":
        return response.text
    return response.json() if response.content else {}


def _clip(text: Any, limit: int = _BODY_LIMIT) -> str:
    value = str(text or "").strip()
    return value if len(value) <= limit else value[:limit] + f"… [{len(value) - limit} more characters]"


def _current_pr(repo: Repo, cwd: str, number: Any) -> dict:
    if number:
        return _request(repo, "GET", f"{repo.path}/pulls/{int(number)}")
    branch = _branch(cwd)
    pulls = _request(repo, "GET", f"{repo.path}/pulls", params={"head": f"{repo.owner}:{branch}", "state": "open"})
    if not pulls:
        raise GitHubError(f"There is no open pull request for {branch}. Open one with github_pr_create.")
    return pulls[0]


# ── Reading ─────────────────────────────────────────────────────────────────


def pr_view(cwd: str, number: Any = None) -> tuple[str, dict]:
    repo = repo_for(cwd)
    pr = _current_pr(repo, cwd, number)
    n = pr["number"]
    reviews = _request(repo, "GET", f"{repo.path}/pulls/{n}/reviews")
    review_comments = _request(repo, "GET", f"{repo.path}/pulls/{n}/comments", params={"per_page": 100})
    conversation = _request(repo, "GET", f"{repo.path}/issues/{n}/comments", params={"per_page": 100})
    checks = _request(repo, "GET", f"{repo.path}/commits/{pr['head']['sha']}/check-runs",
                      params={"per_page": 100}).get("check_runs", [])

    decisions = {}
    for review in reviews:
        if review.get("state") in {"APPROVED", "CHANGES_REQUESTED", "DISMISSED"}:
            decisions[(review.get("user") or {}).get("login", "")] = review["state"]
    lines = [
        f"PR #{n} {pr['title']!r} ({pr['state']}{', draft' if pr.get('draft') else ''}) {pr['html_url']}",
        f"{pr['base']['ref']} <- {pr['head']['ref']} at {pr['head']['sha'][:10]}"
        + (f"; reviews: {', '.join(f'{who} {state.lower()}' for who, state in decisions.items())}" if decisions else ""),
    ]
    failing = [c for c in checks if c.get("conclusion") in {"failure", "timed_out", "cancelled", "action_required"}]
    pending = [c for c in checks if c.get("status") != "completed"]
    lines.append(f"\nChecks: {len(checks)} ({len(failing)} failing, {len(pending)} running)")
    for check in checks:
        state = check.get("conclusion") or check.get("status") or "?"
        mark = "✗" if check in failing else ("…" if check in pending else "✓")
        lines.append(f"  {mark} {check.get('name')} ({state}, job {check.get('id')})")
    lines.append(f"\nReview comments ({len(review_comments)}):")
    for comment in review_comments:
        where = f"{comment.get('path')}:{comment.get('line') or comment.get('original_line') or '?'}"
        reply = f" (reply to {comment['in_reply_to_id']})" if comment.get("in_reply_to_id") else ""
        outdated = " [outdated]" if comment.get("position") is None else ""
        lines.append(f"- [id {comment['id']}] {where}{outdated} @{(comment.get('user') or {}).get('login')}{reply}: "
                     f"{_clip(comment.get('body'))}")
    lines.append(f"\nConversation ({len(conversation)}):")
    for comment in conversation:
        lines.append(f"- [id {comment['id']}] @{(comment.get('user') or {}).get('login')}: {_clip(comment.get('body'))}")
    lines.append(f"\nBody:\n{_clip(pr.get('body'), 3_000)}")
    metadata = {"number": n, "url": pr["html_url"], "failing_checks": [c.get("name") for c in failing],
                "review_comments": len(review_comments), "repo": f"{repo.owner}/{repo.name}",
                "state": "merged" if pr.get("merged_at") else pr.get("state", ""),
                "decisions": decisions}
    return "\n".join(lines), metadata


def check_log(cwd: str, job_id: Any, lines: int = _LOG_TAIL_LINES) -> tuple[str, dict]:
    repo = repo_for(cwd)
    text = _request(repo, "GET", f"{repo.path}/actions/jobs/{int(job_id)}/logs", accept="text/plain")
    all_lines = str(text).splitlines()
    wanted = max(20, min(int(lines or _LOG_TAIL_LINES), 1_000))
    # Error lines from anywhere, then the end of the log, where failures land.
    errors = [line for line in all_lines[:-wanted] if re.search(r"\b(error|failed|failure|exception)\b", line, re.I)][-40:]
    parts = []
    if errors:
        parts.append("Earlier error lines:\n" + "\n".join(errors))
    parts.append(f"Last {min(wanted, len(all_lines))} of {len(all_lines)} lines:\n" + "\n".join(all_lines[-wanted:]))
    return "\n\n".join(parts), {"job_id": int(job_id), "lines": len(all_lines)}


# ── Changing ────────────────────────────────────────────────────────────────


def pr_create(cwd: str, *, title: str, body: str = "", base: str = "", draft: bool = False,
              push: bool = True) -> tuple[str, dict]:
    if not str(title or "").strip():
        raise GitHubError("Give the pull request a title.")
    repo = repo_for(cwd)
    branch = _branch(cwd)
    if branch == "HEAD":
        raise GitHubError("The project is on a detached HEAD; create a branch first (git_branch_create).")
    if push:
        # Never force: a rejected push means someone else changed the branch.
        _git(cwd, "push", "--set-upstream", "origin", branch)
    base = base or _request(repo, "GET", repo.path).get("default_branch", "main")
    if branch == base:
        raise GitHubError(f"The project is on {base}, the base branch; create a branch for the change first.")
    pr = _request(repo, "POST", f"{repo.path}/pulls",
                  json={"title": title, "body": body, "head": branch, "base": base, "draft": bool(draft)})
    return f"Opened PR #{pr['number']} {pr['html_url']} ({branch} -> {base}{', draft' if draft else ''})", {
        "number": pr["number"], "url": pr["html_url"]}


def pr_comment(cwd: str, *, body: str, number: Any = None, reply_to: Any = None) -> tuple[str, dict]:
    if not str(body or "").strip():
        raise GitHubError("Write the comment's text in `body`.")
    repo = repo_for(cwd)
    pr = _current_pr(repo, cwd, number)
    if reply_to:
        comment = _request(repo, "POST", f"{repo.path}/pulls/{pr['number']}/comments/{int(str(reply_to))}/replies",
                           json={"body": body})
    else:
        comment = _request(repo, "POST", f"{repo.path}/issues/{pr['number']}/comments", json={"body": body})
    return f"Commented on PR #{pr['number']}: {comment.get('html_url', '')}", {
        "number": pr["number"], "comment_id": comment.get("id"), "url": comment.get("html_url")}


def pr_update(cwd: str, *, number: Any = None, title: str = "", body: str | None = None,
              ready: bool = False) -> tuple[str, dict]:
    repo = repo_for(cwd)
    pr = _current_pr(repo, cwd, number)
    changes: dict[str, Any] = {}
    if title:
        changes["title"] = title
    if body is not None:
        changes["body"] = body
    if changes:
        pr = _request(repo, "PATCH", f"{repo.path}/pulls/{pr['number']}", json=changes)
    if ready and pr.get("draft"):
        # Marking ready for review is only in the GraphQL API.
        _request(repo, "POST", "/graphql", json={
            "query": "mutation($id: ID!) { markPullRequestReadyForReview(input: {pullRequestId: $id}) "
                     "{ pullRequest { isDraft } } }",
            "variables": {"id": pr["node_id"]}})
    if not changes and not ready:
        raise GitHubError("Nothing to change: give a title, a body, or ready=true.")
    return f"Updated PR #{pr['number']} {pr['html_url']}", {"number": pr["number"], "url": pr["html_url"]}


# ── Tool entry points ───────────────────────────────────────────────────────


def _run(fn: Callable[..., tuple[str, dict]], start: float, *args: Any, **kwargs: Any):
    from .tools import ToolResult  # tools.py lists these definitions, so import at use

    try:
        output, metadata = fn(*args, **kwargs)
    except GitHubError as exc:
        return ToolResult(str(exc), is_error=True, elapsed=time.time() - start)
    except (KeyError, TypeError, ValueError) as exc:
        return ToolResult(f"Unexpected answer from GitHub: {exc}", is_error=True, elapsed=time.time() - start)
    return ToolResult(output, elapsed=time.time() - start, metadata=metadata)


def _other_host(cwd: str):
    """GitLab, Bitbucket or Azure DevOps behind origin (engine/code_hosts.py); None for GitHub."""
    from . import code_hosts

    try:
        return code_hosts.host_for(cwd)
    except GitHubError:  # no origin: the GitHub path says so
        return None


def exec_github_pr_view(args: dict, start: float):
    from . import review_gate

    cwd = args.get("cwd") or "."
    host = _other_host(cwd)
    result = _run(host.view if host else pr_view, start, cwd, args.get("number"))
    meta = getattr(result, "metadata", None) or {}
    if not getattr(result, "is_error", False) and not host and meta.get("url"):
        # The review queue follows the pull request's state (engine/review_gate.py).
        decisions = set((meta.get("decisions") or {}).values())
        status = ("merged" if meta.get("state") == "merged" else "closed" if meta.get("state") == "closed"
                  else "changes_requested" if "CHANGES_REQUESTED" in decisions
                  else "approved" if "APPROVED" in decisions else "waiting")
        review_gate.report(meta["url"], status)
    return result


def exec_github_check_log(args: dict, start: float):
    if not args.get("job_id"):
        from .tools import ToolResult

        return ToolResult("Give the job id from github_pr_view's checks.", is_error=True, elapsed=time.time() - start)
    cwd = args.get("cwd") or "."
    host = _other_host(cwd)
    return _run(host.check_log if host else check_log, start, cwd, args["job_id"],
                args.get("lines") or _LOG_TAIL_LINES)


def exec_github_pr_create(args: dict, start: float):
    from . import review_gate

    cwd = args.get("cwd") or "."
    host = _other_host(cwd)
    if host and not str(args.get("title") or "").strip():
        return _run(pr_create, start, cwd, title="")  # the same refusal on every host
    title = str(args.get("title") or "")
    result = _run(host.create if host else pr_create, start, cwd, title=title,
                  body=review_gate.pr_body(str(args.get("body") or "")), base=str(args.get("base") or ""),
                  draft=bool(args.get("draft")), push=args.get("push", True) is not False)
    if not result.is_error and review_gate.enabled():
        request = None
        if not host:
            repo = repo_for(cwd)
            number = int(result.metadata["number"])

            def request(people: list[str], teams: list[str]) -> None:
                _request(repo, "POST", f"{repo.path}/pulls/{number}/requested_reviewers",
                         json={"reviewers": people, "team_reviewers": teams})
        note = review_gate.after_create(cwd, result.metadata or {}, title, request=request)
        if note:
            result.output = f"{result.output}\n{note}"
    return result


def exec_github_pr_comment(args: dict, start: float):
    cwd = args.get("cwd") or "."
    host = _other_host(cwd)
    if host and not str(args.get("body") or "").strip():
        return _run(pr_comment, start, cwd, body="")
    return _run(host.comment if host else pr_comment, start, cwd, body=str(args.get("body") or ""),
                number=args.get("number"), reply_to=args.get("reply_to"))


def exec_github_pr_update(args: dict, start: float):
    body = args.get("body")
    cwd = args.get("cwd") or "."
    host = _other_host(cwd)
    return _run(host.update if host else pr_update, start, cwd, number=args.get("number"),
                title=str(args.get("title") or ""), body=None if body is None else str(body),
                ready=bool(args.get("ready")))


GITHUB_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "github_pr_view",
            "description": "Show the pull request for the current branch (or `number`) on GitHub, GitLab (merge "
                           "request), Bitbucket or Azure DevOps: state, reviews, review comments or threads with "
                           "their ids, the conversation, and every check with its job id.",
            "parameters": {"type": "object", "properties": {
                "number": {"type": "integer", "description": "PR number (default: the current branch's open PR)"},
                "cwd": {"type": "string", "description": "Working directory (default: project root)"},
            }, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "github_check_log",
            "description": "Read the end of a check's log (a GitHub Actions job, GitLab job, Bitbucket Pipelines "
                           "step or Azure Pipelines build), plus earlier error lines, to find why it failed.",
            "parameters": {"type": "object", "properties": {
                "job_id": {"type": "string", "description": "The job id github_pr_view lists for the check"},
                "lines": {"type": "integer", "description": "How many final lines to show (default 150)"},
                "cwd": {"type": "string", "description": "Working directory (default: project root)"},
            }, "required": ["job_id"]},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "github_pr_create",
            "description": "Push the current branch (never forced) and open a pull request (a merge request on "
                           "GitLab) from it.",
            "parameters": {"type": "object", "properties": {
                "title": {"type": "string"},
                "body": {"type": "string", "description": "Markdown description"},
                "base": {"type": "string", "description": "Branch to merge into (default: the repository's default)"},
                "draft": {"type": "boolean"},
                "push": {"type": "boolean", "description": "Push the branch first (default true)"},
                "cwd": {"type": "string", "description": "Working directory (default: project root)"},
            }, "required": ["title"]},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "github_pr_comment",
            "description": "Comment on the pull request, or reply in a review thread with `reply_to` (the id "
                           "github_pr_view lists: a review comment, GitLab discussion or Azure DevOps thread).",
            "parameters": {"type": "object", "properties": {
                "body": {"type": "string", "description": "Markdown text"},
                "reply_to": {"type": "string", "description": "The id of the comment or thread to reply to"},
                "number": {"type": "integer", "description": "PR number (default: the current branch's open PR)"},
                "cwd": {"type": "string", "description": "Working directory (default: project root)"},
            }, "required": ["body"]},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "github_pr_update",
            "description": "Change the pull request's title or description, or mark a draft ready for review.",
            "parameters": {"type": "object", "properties": {
                "title": {"type": "string"},
                "body": {"type": "string", "description": "New Markdown description (replaces the old one)"},
                "ready": {"type": "boolean", "description": "Mark a draft ready for review"},
                "number": {"type": "integer", "description": "PR number (default: the current branch's open PR)"},
                "cwd": {"type": "string", "description": "Working directory (default: project root)"},
            }, "required": []},
        },
    },
]
