"""Bounded read-only Git previews; patch text is untrusted presentation data."""

from __future__ import annotations

from pathlib import Path
import subprocess
import time

from .git_boundary import disabled_filter_options, git_bytes as _git_bytes
from .models import Conflict, Scope, ScopeDenied, require_id


def inspect_candidate(integration, scope: Scope, run_id: str, candidate_id: str, *, max_diff_bytes: int) -> dict:
    """Require captured owner scope and stable candidate identity before/after."""
    if not isinstance(scope, Scope):
        raise TypeError("Candidate preview requires captured owner scope")
    require_id(run_id)
    require_id(candidate_id)
    if type(max_diff_bytes) is not int or not 1 <= max_diff_bytes <= 1024 * 1024:
        raise ValueError("Candidate diff limit must be between one byte and one MiB")
    def capture():
        with integration.store._connection() as connection:
            integration.store._run(connection, scope, run_id)
            row = connection.execute("SELECT * FROM integration_candidates WHERE id=? AND run_id=? AND repo_key=?",
                (candidate_id, run_id, integration.repo_key)).fetchone()
            if row is None:
                raise ScopeDenied("Candidate is unavailable in this captured run and repository")
            return dict(row)
    record = capture()
    try:
        base = integration._revision(record["base_revision"])
        revision = integration._revision(record["result_revision"])
    except ValueError as exc:
        raise Conflict("Candidate has no immutable result available for preview") from exc
    deadline = time.monotonic() + 10
    original = Path(record["path"])
    try:
        path = original.resolve(strict=True)
    except OSError as exc:
        raise Conflict("Candidate worktree is unavailable for exact preview") from exc
    if path != original or path == integration.root or not path.is_relative_to(integration.root):
        raise ScopeDenied("Candidate worktree moved outside its captured runtime root")
    # Preserve CRLF/attribute normalization, but status must not invoke an
    # executable clean/process filter. Read only names, never configured argv.
    configuration, cut = _git_bytes(integration, path, ("config", "--null", "--name-only", "--list"),
                                     limit=65536, deadline=deadline)
    if cut:
        raise Conflict("Candidate Git configuration exceeds the preview limit")
    overrides = disabled_filter_options(configuration)
    def read(args, limit):
        return _git_bytes(integration, path, args, limit=limit, deadline=deadline, overrides=overrides)
    def check_input():
        if original.resolve(strict=True) != path:
            raise Conflict("Candidate worktree path changed during preview")
        common, cut = read(("rev-parse", "--git-common-dir"), 65536)
        if cut or (path / common.decode("utf-8").strip()).resolve() != integration._common:
            raise ScopeDenied("Candidate worktree belongs to another repository")
        head, cut = read(("rev-parse", "HEAD"), 128)
        if cut or head.decode("ascii").strip() != revision:
            raise Conflict("Candidate input no longer matches its immutable revision")
        dirty, cut = read(("status", "--porcelain=v1", "-z", "--untracked-files=all"), 1)
        if cut or dirty:
            raise Conflict("Candidate worktree changed after its immutable revision")
    try:
        check_input()
        diff_args = ("diff", "--no-ext-diff", "--no-textconv", "--no-renames")
        names, names_cut = read((*diff_args, "--name-only", "-z", base, revision, "--"), 65536)
        entries = names.split(b"\0")[:-1]  # Never expose a partial filename.
        paths_cut = names_cut or len(entries) > 200
        changed_paths = [value.decode("utf-8", "replace") for value in entries[:200]]
        raw_diff, diff_cut = read((*diff_args, "--no-color", "--src-prefix=a/", "--dst-prefix=b/", base, revision, "--"),
                                  max_diff_bytes)
        check_input()
        current = capture()
        if any(current[key] != record[key] for key in
               ("path", "repo_key", "base_revision", "result_revision", "manifest_json", "epoch")):
            raise Conflict("Candidate identity changed during preview")
    except (OSError, UnicodeError, subprocess.SubprocessError) as exc:
        raise Conflict("Candidate preview could not observe its exact stored input") from exc
    encoded = raw_diff.decode("utf-8", "replace").encode("utf-8")
    if len(encoded) > max_diff_bytes:
        diff_cut = True
    return {"candidate_id": candidate_id, "base_revision": base, "result_revision": revision,
            "changed_paths": changed_paths, "changed_paths_truncated": paths_cut,
            "diff": encoded[:max_diff_bytes].decode("utf-8", "ignore"), "diff_truncated": diff_cut,
            "errors": []}
