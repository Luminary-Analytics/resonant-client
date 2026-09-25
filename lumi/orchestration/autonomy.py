"""
Full Autonomy enforcement — the irreversibility floor.

Tenet: the agent does anything the user could do, without asking. The plan-graph
viz is the intervention surface, not a gate. The ONLY actions that pause for
explicit approval are the ones that can't be undone. Everything else just runs.

Rules are pure functions over (tool_name, args, project_path, settings) so they
compose cleanly with the existing sandbox / permission flow. They return a
`FloorViolation` with a human-readable reason; if no rule fires, the action is
allowed without prompting.
"""

from __future__ import annotations

import os
import re
import shlex
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional


# Defaults — overridable per-project via settings (autonomy_*_overrides).
DEFAULT_PROTECTED_BRANCHES = ("main", "master", "prod", "production")
DEFAULT_PROTECTED_BRANCH_PREFIXES = ("release/",)
def default_protected_paths() -> tuple[Path, ...]:
    """Resolved at call time, never at import time, so the protected
    list follows Path.home() — tests patch home after this module is
    already imported."""
    return (
        Path.home() / ".ssh",
        Path.home() / ".aws",
        Path.home() / ".kube",
        Path.home() / ".gnupg",
        Path("/etc"),
    )
DEFAULT_BUDGET_USD_MAX = 5.00


# ── Result type ─────────────────────────────────────────────────────────


@dataclass
class FloorViolation:
    """Returned by a rule when the action hits the irreversibility floor."""
    rule: str               # "protected_branch_force_push" / "rm_rf_outside_project" / ...
    reason: str             # human-readable explanation for the prompt
    severity: str = "hard"  # only "hard" today; reserved for future "soft" gating
    suggested_action: str = ""  # optional: "use --force-with-lease instead"


# ── Settings adapter ────────────────────────────────────────────────────


@dataclass
class AutonomySettings:
    """Snapshot of the autonomy-related settings, passed to each rule."""
    protected_branches: tuple[str, ...] = DEFAULT_PROTECTED_BRANCHES
    protected_branch_prefixes: tuple[str, ...] = DEFAULT_PROTECTED_BRANCH_PREFIXES
    protected_paths: tuple[Path, ...] = field(default_factory=default_protected_paths)
    budget_usd_max: float = DEFAULT_BUDGET_USD_MAX

    @classmethod
    def from_settings(cls, settings) -> "AutonomySettings":
        """Pull autonomy fields from a SettingsManager-like object."""
        if settings is None:
            return cls()
        get = settings.get
        protected_branches_raw = get("general", "autonomy_protected_branches", None)
        protected_paths_raw = get("general", "autonomy_external_paths", None)
        budget = get("general", "budget_usd_max", DEFAULT_BUDGET_USD_MAX)
        return cls(
            protected_branches=tuple(protected_branches_raw or DEFAULT_PROTECTED_BRANCHES),
            protected_paths=tuple(
                Path(p) for p in (protected_paths_raw or [])
            ) or default_protected_paths(),
            budget_usd_max=float(budget or DEFAULT_BUDGET_USD_MAX),
        )


# ── Path helpers ────────────────────────────────────────────────────────


def _is_path_inside(child: Path, parent: Path) -> bool:
    """True iff `child` resolves to a path under `parent`. Handles missing dirs."""
    try:
        c = Path(child).expanduser().resolve(strict=False)
        p = Path(parent).expanduser().resolve(strict=False)
    except (OSError, RuntimeError):
        return False
    try:
        c.relative_to(p)
        return True
    except ValueError:
        return False


def _is_branch_protected(branch: str, settings: AutonomySettings) -> bool:
    if not branch:
        return False
    if branch in settings.protected_branches:
        return True
    return any(branch.startswith(prefix) for prefix in settings.protected_branch_prefixes)


# ── Rules ───────────────────────────────────────────────────────────────


def _check_force_push_to_protected_branch(
    tool_name: str, args: dict, project_path: str, settings: AutonomySettings,
) -> Optional[FloorViolation]:
    """`bash` invocations or `git_*` calls that force-push to a protected branch."""
    if tool_name == "bash":
        cmd = str(args.get("command", ""))
        # Cheap parse — exact tokens. Real shells do worse with weird quoting,
        # but we only need to catch the common patterns the agent emits.
        try:
            tokens = shlex.split(cmd, posix=False)
        except ValueError:
            tokens = cmd.split()
        lower = [t.lower() for t in tokens]
        if "git" not in lower:
            return None
        if "push" not in lower:
            return None
        is_force = any(t in {"--force", "-f", "--force-with-lease"} for t in tokens)
        if not is_force:
            return None
        # --force-with-lease is the safer variant — let it through
        if any(t == "--force-with-lease" for t in tokens):
            return None
        # Try to identify a protected target branch.
        # Tokens we care about:
        #   - bare branch names (`main`, `release/1.2`)
        #   - `refspec:remote-branch` (`HEAD:main`)
        #   - `refs/heads/<branch>` (`refs/heads/release/1.2`)
        protected_hit = None
        for token in tokens:
            if token.startswith("-"):
                continue  # flags
            candidates = [token]
            # Refspec form: src:dst → check both halves
            if ":" in token:
                candidates.extend(token.split(":"))
            # refs/heads/X form: peel the prefix once
            stripped = []
            for cand in candidates:
                if cand.startswith("refs/heads/"):
                    stripped.append(cand[len("refs/heads/"):])
            candidates.extend(stripped)
            for cand in candidates:
                # Skip likely-remote-name tokens (single segment, common remote names)
                if cand in {"origin", "upstream", "remote"}:
                    continue
                if _is_branch_protected(cand, settings):
                    protected_hit = cand
                    break
            if protected_hit:
                break
        if protected_hit:
            return FloorViolation(
                rule="protected_branch_force_push",
                reason=f"`git push --force` targets protected branch {protected_hit!r}",
                suggested_action="Use --force-with-lease to fail safely on remote drift, "
                                 "or push to a different branch.",
            )
    return None


def _check_rm_rf_outside_project(
    tool_name: str, args: dict, project_path: str, settings: AutonomySettings,
) -> Optional[FloorViolation]:
    """`rm -rf <path>` that resolves outside the project root."""
    if tool_name != "bash":
        return None
    cmd = str(args.get("command", ""))
    if not re.search(r"\brm\s+(-[rRf]+\b|-[a-zA-Z]*r[a-zA-Z]*\b)", cmd):
        return None
    try:
        tokens = shlex.split(cmd, posix=False)
    except ValueError:
        tokens = cmd.split()
    # Find the `rm` call and inspect its non-flag arguments
    for i, tok in enumerate(tokens):
        if tok != "rm":
            continue
        targets = [t for t in tokens[i + 1:] if not t.startswith("-")]
        for target in targets:
            target_path = Path(target).expanduser()
            if not target_path.is_absolute():
                target_path = (Path(project_path) / target).resolve(strict=False)
            if not _is_path_inside(target_path, Path(project_path)):
                return FloorViolation(
                    rule="rm_rf_outside_project",
                    reason=f"`rm -r` targets {str(target_path)!r}, outside the project root",
                    suggested_action="Restrict the path to inside the project.",
                )
        break
    return None


def _check_writes_to_protected_paths(
    tool_name: str, args: dict, project_path: str, settings: AutonomySettings,
) -> Optional[FloorViolation]:
    """file_write / file_edit / bash redirects that touch ~/.ssh, ~/.aws, /etc, etc."""
    candidate_paths: list[Path] = []
    if tool_name in {"file_write", "file_edit"}:
        path = args.get("path") or args.get("file_path")
        if path:
            candidate_paths.append(Path(path).expanduser())
    elif tool_name == "bash":
        cmd = str(args.get("command", ""))
        for protected in settings.protected_paths:
            protected_str = str(protected)
            if protected_str and protected_str in cmd:
                candidate_paths.append(protected / ".touched")  # any path inside
    for cand in candidate_paths:
        if not cand.is_absolute():
            cand = (Path(project_path) / cand).resolve(strict=False)
        for protected in settings.protected_paths:
            if _is_path_inside(cand, protected):
                return FloorViolation(
                    rule="writes_to_protected_path",
                    reason=f"Action targets a protected path: {cand} (protected: {protected})",
                    suggested_action="Edit the file in your editor instead — "
                                     "we don't auto-edit OS-level config or credential dirs.",
                )
    return None


def _check_destructive_sql_non_test(
    tool_name: str, args: dict, project_path: str, settings: AutonomySettings,
) -> Optional[FloorViolation]:
    """`bash` commands that issue DROP TABLE / TRUNCATE on a non-test DB."""
    if tool_name != "bash":
        return None
    cmd = str(args.get("command", "")).upper()
    if not re.search(r"\b(DROP\s+TABLE|TRUNCATE\s+TABLE|DROP\s+DATABASE|DELETE\s+FROM\s+\w+\s*;)\b", cmd):
        return None
    # Heuristic: command mentions an obvious test marker (anywhere, even inside
    # an identifier like `test_db`) → assume it's safe. The author of a `prod`
    # DROP can still slip past by naming things weirdly, but the floor only
    # exists to catch obvious foot-shoots.
    if re.search(r"(TEST|FIXTURE|SCRATCH|SANDBOX|TMP)", cmd):
        return None
    return FloorViolation(
        rule="destructive_sql_non_test",
        reason="Command contains DROP/TRUNCATE/DELETE on what appears to be a non-test database",
        suggested_action="Run against a test/fixture database, or include 'test' in the DB name.",
    )


def _check_external_message_send(
    tool_name: str, args: dict, project_path: str, settings: AutonomySettings,
) -> Optional[FloorViolation]:
    """MCP / browser actions that send messages externally on the user's behalf."""
    # Message-send tools by name — extend as MCP servers register more.
    if tool_name in {"slack_send_message", "email_send", "sms_send", "twitter_post", "linkedin_post"}:
        return FloorViolation(
            rule="external_message_send",
            reason=f"`{tool_name}` posts to an external channel on your behalf",
            suggested_action="Confirm the recipient + body before sending.",
        )
    # Browser submits to known message endpoints
    if tool_name == "browser_click":
        button_text = str(args.get("text", "") or args.get("selector", "")).lower()
        if any(t in button_text for t in ("send", "post", "publish", "tweet", "submit")):
            url = str(args.get("url", "") or "")
            if any(d in url for d in ("twitter.com", "x.com", "linkedin.com", "facebook.com",
                                       "slack.com", "mail.google.com")):
                return FloorViolation(
                    rule="external_message_send",
                    reason=f"Click would post to {url}",
                    suggested_action="Confirm the post manually before submitting.",
                )
    return None


def _check_budget_exceeded(
    tool_name: str, args: dict, project_path: str, settings: AutonomySettings,
) -> Optional[FloorViolation]:
    """Refuse further model calls when the running spend has crossed the budget cap.

    The rule receives the *cumulative* spend via args["_running_spend_usd"] (the
    audit log + cost tracker is the source of truth). When absent, the check is a
    no-op — the budget gate is meant to be enforced by the orchestrator wrapping
    each call with the running total.
    """
    spend = args.get("_running_spend_usd")
    if spend is None:
        return None
    try:
        spend_val = float(spend)
    except (TypeError, ValueError):
        return None
    if spend_val < settings.budget_usd_max:
        return None
    return FloorViolation(
        rule="budget_exceeded",
        reason=f"Cumulative spend ${spend_val:.2f} has hit the per-intent budget "
               f"cap (${settings.budget_usd_max:.2f}).",
        suggested_action="Raise general.budget_usd_max in Settings, or pause the intent.",
    )


# ── Registry + dispatcher ───────────────────────────────────────────────


# Order matters only for which violation surfaces first — they're all "hard".
RULES: tuple[Callable[..., Optional[FloorViolation]], ...] = (
    _check_force_push_to_protected_branch,
    _check_rm_rf_outside_project,
    _check_writes_to_protected_paths,
    _check_destructive_sql_non_test,
    _check_external_message_send,
    _check_budget_exceeded,
)


def check_floor(
    *,
    tool_name: str,
    args: Optional[dict] = None,
    project_path: str = "",
    settings=None,
) -> Optional[FloorViolation]:
    """Run all rules; return the first violation, or None if action is allowed.

    Callers (engine/tools.py before dispatch, or the GraphWalker) treat a
    non-None return as a hard checkpoint — pause and require explicit user
    approval through the chat. Everything else runs without prompting.
    """
    args = args or {}
    if tool_name in {'check_run', 'preview_start'}:
        if tool_name == 'preview_start':
            args = {**args, 'command': ' '.join(str(v) for v in args.get('command', []))}
        tool_name = 'bash'
    autonomy = AutonomySettings.from_settings(settings)
    project = project_path or os.getcwd()
    for rule in RULES:
        violation = rule(tool_name, args, project, autonomy)
        if violation is not None:
            return violation
    return None
