"""Isolated writer results and evidence-bound local Git integration.

All Git effects happen in owned worktrees until an explicitly approved apply.
The repository lock coordinates SONN processes; other Git clients still rely on
Git's index locking and preflight checks. This is not an OS sandbox: trusted
check programs run with the local user's process permissions.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import signal
import subprocess
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from typing import Any, Iterator

from lumi.processes import background_process_kwargs, close_windows_job, windows_kill_job

from ..artifacts import project_state_dir
from .argv_process import ArgvResult, ManagedArgvProcess, effect_support
from .git_boundary import disabled_filter_options, git_bytes, has_custom_merge_driver, trusted_git_executable
from .integration_processes import IntegrationProcesses
from .models import AdmissionClosed, AttemptContext, Conflict, RunAuthority, Scope, ScopeDenied, StaleAuthority, require_id
from .policy import AssignmentGrant, normalize_scope
from .store import SwarmStore, _id, _json
from .supervisor import SwarmSupervisor


@dataclass(frozen=True, slots=True)
class CheckSpec:
    """A trusted named subprocess check, never model-supplied shell text."""

    key: str
    argv: tuple[str, ...]
    timeout_seconds: float = 120

    def __post_init__(self) -> None:
        require_id(self.key)
        if (type(self.argv) is not tuple or not self.argv
                or any(type(value) is not str or not value or "\0" in value for value in self.argv)):
            raise ValueError("Check argv must be a nonempty tuple of strings")
        if isinstance(self.timeout_seconds, bool) or not isinstance(self.timeout_seconds, (int, float)) or not 1 <= self.timeout_seconds <= 1200:
            raise ValueError("Check timeout must be between 1 and 1200 seconds")


@dataclass(frozen=True, slots=True)
class ApplyApproval:
    """Trusted explicit approval bound to one owner, candidate and exact base.

    Construct this only in an authenticated user/policy adapter. Typed Python
    values are not remote authentication, and no model tool accepts this object.
    """

    id: str
    scope: Scope
    run_id: str
    candidate_id: str
    expected_base: str
    target_revision: str
    expires_at: float

    def __post_init__(self) -> None:
        for value in (self.id, self.run_id, self.candidate_id, self.expected_base, self.target_revision):
            require_id(value)
        if isinstance(self.expires_at, bool) or not isinstance(self.expires_at, (int, float)) or not math.isfinite(self.expires_at):
            raise ValueError("Application approval needs a finite expiry")


class SwarmIntegration:
    """Durable intent and observation around isolated, bounded Git operations."""

    @staticmethod
    def writer_support() -> dict[str, str | bool]:
        """Expose the writer containment envelope before reserving or dispatch."""
        return effect_support()

    @staticmethod
    def _require_writer_support() -> None:
        support = effect_support()
        if not support["supported"]:
            raise ScopeDenied(support["reason"])

    def __init__(self, store: SwarmStore, project_path: str | Path, *, root: str | Path | None = None,
                 managed_effects=None) -> None:
        self.store = store
        if managed_effects is not None and managed_effects.store.path.resolve() != store.path.resolve():
            raise ScopeDenied("Managed effects must use the captured native swarm store")
        self.managed_effects = managed_effects
        self._processes = IntegrationProcesses(store)
        self.project = Path(project_path).expanduser().resolve(strict=True)
        self.root = (Path(root) if root else project_state_dir(self.project) / "swarm" / "worktrees").resolve()
        if self.root == self.project or self.root.is_relative_to(self.project):
            raise ValueError("Swarm worktrees must be outside the user's checkout")
        self.root.mkdir(parents=True, exist_ok=True)
        self._git_executable = trusted_git_executable(self.project, self.root)
        self._hooks = self.root / "disabled-hooks"
        self._hooks.mkdir(exist_ok=True)
        top_level = Path(self._git(self.project, "rev-parse", "--show-toplevel").stdout.strip()).resolve(strict=True)
        if top_level != self.project:
            raise ScopeDenied("Supervised writers require the captured project to be the Git repository root")
        common = self._git(self.project, "rev-parse", "--git-common-dir").stdout.strip()
        self._common = (self.project / common).resolve()
        self.repo_key = hashlib.sha256(os.path.normcase(str(self._common)).encode()).hexdigest()
        self._lock_path = self._common / "sonn-swarm-integration.lock"

    def capture_base(self) -> dict[str, str]:
        """Capture clean committed input without modifying the checkout."""
        with self._repository_lock():
            if not self._clean(self.project):
                raise Conflict("Writer baseline requires a clean committed checkout; existing work is preserved")
            branch = self._branch(self.project)
            if not branch:
                raise Conflict("Writer setup requires a checked-out branch, not a detached revision")
            return {"base_revision": self._revision(self._head(self.project)),
                    "target_branch": branch}

    def _checkout_identity(self) -> dict[str, str]:
        """Identify the captured checkout, not merely its shared object store."""
        top = Path(self._git(self.project, "rev-parse", "--show-toplevel").stdout.strip()).resolve(strict=True)
        common = (self.project / self._git(self.project, "rev-parse", "--git-common-dir").stdout.strip()).resolve(strict=True)
        git_dir = Path(self._git(self.project, "rev-parse", "--absolute-git-dir").stdout.strip()).resolve(strict=True)
        if top != self.project or common != self._common:
            raise Conflict("The captured application checkout or repository changed")
        return {"path": os.path.normcase(str(top)), "git_dir": os.path.normcase(str(git_dir)),
                "common_dir": os.path.normcase(str(common))}

    def _require_application_checkout(self, manifest: dict[str, Any]) -> None:
        # Linked worktrees share repo_key and may have identical HEAD commits.
        # Neither another worktree nor a detached/different branch proves that
        # this approved application reached its originally captured destination.
        if manifest.get("target_checkout") != self._checkout_identity():
            raise Conflict("Application requires its exact captured checkout identity")
        if not manifest.get("target_branch") or self._branch(self.project) != manifest["target_branch"]:
            raise Conflict("Application deferred: the captured checkout branch changed")

    def inspect_candidate(self, scope: Scope, run_id: str, candidate_id: str, *, max_diff_bytes: int = 65536) -> dict[str, Any]:
        """Read a bounded exact candidate diff, including historical epochs."""
        from .integration_preview import inspect_candidate
        return inspect_candidate(self, scope, run_id, candidate_id, max_diff_bytes=max_diff_bytes)

    def _execute(self, authority, kind, effect_id, argv, cwd, *, environment=None, timeout_seconds=60, max_output_bytes=8 * 1024 * 1024):
        """Commit process identity before the helper receives executable argv."""
        self._require_writer_support()
        argv, cwd = tuple(argv), Path(cwd).resolve(strict=True)
        managed = self.managed_effects
        if authority.scope.tenant_id != f"personal:{authority.scope.owner_id}" and managed is None:
            raise ScopeDenied("Organization integration requires central owner-effect admission")
        if managed is not None:
            # Freeze the exact environment that the gate receives. Only its
            # hash enters the managed journal or central admission descriptor.
            environment = dict(os.environ if environment is None else environment)
        process_id = self._processes.intent(authority, kind, effect_id, argv, cwd)
        process = ManagedArgvProcess()
        def started(owned):
            self._processes.owned(authority, process_id, owned)
            if kind == "check":
                with self.store._connection(write=True) as connection:
                    connection.execute("UPDATE integration_checks SET job_id=? WHERE id=?", (process_id, effect_id))
            self._processes.invoke(authority, process_id, claim=(
                (lambda connection: managed.claim_effect(authority, process_id, connection)) if managed else None))
        try:
            try:
                if managed is not None:
                    managed.prepare_effect(authority, process_id, environment=environment,
                        timeout_seconds=timeout_seconds, max_output_bytes=max_output_bytes)
                    self._admit(authority)
                return process.execute(argv, cwd, environment=environment, timeout_seconds=timeout_seconds,
                    max_output_bytes=max_output_bytes, on_started=started, cancel_check=lambda: self._admit(authority))
            finally:
                if process.pid is None or (process.cleanup_confirmed and process.created_at is None):
                    self._processes.not_started(authority.scope, authority.run_id, process_id)
                elif process.cleanup_confirmed:
                    self._processes.stopped(authority.scope, authority.run_id, process_id, process)
                if managed is not None:
                    managed.observe_effect(authority, process_id, getattr(process, "result", None),
                        cleanup_confirmed=process.pid is None or process.cleanup_confirmed)
        except BaseException as exc:
            observation = getattr(process, "result", None)
            if isinstance(observation, ArgvResult):
                exc._swarm_effect_observation = observation
            raise

    def _git(self, cwd: Path, *args: str, check: bool = True, effect=None) -> subprocess.CompletedProcess[str]:
        # Model-authored attributes must not turn host Git into an arbitrary
        # command executor. Keep CRLF normalization and non-executable config.
        # These fixed plumbing operations do not normalize a working-tree
        # file or select an attribute driver. Avoid a second process on each
        # read-only writer identity check; every effect still reads fresh config.
        metadata_only = bool(args) and (args[0] in ("rev-parse", "symbolic-ref", "ls-tree", "ls-files")
                                       or args[:2] == ("cat-file", "blob"))
        names = b""
        if not metadata_only:
            names, truncated = git_bytes(self, cwd, ("config", "--null", "--name-only", "--list"),
                                         limit=65536, deadline=time.monotonic() + 10)
            if truncated:
                raise Conflict("Git configuration exceeds the safe inspection limit")
        overrides = disabled_filter_options(names)
        if args and args[0] in ("cherry-pick", "merge") and has_custom_merge_driver(names):
            raise Conflict("Custom Git merge drivers are not supported during supervised integration")
        if args and args[0] == "diff":
            args = ("diff", "--no-ext-diff", "--no-textconv", *args[1:])
        # Inherited GIT_DIR/INDEX_FILE must not redirect a captured repository.
        environment = {key: value for key, value in os.environ.items() if not key.upper().startswith("GIT_")}
        environment.update(GIT_TERMINAL_PROMPT="0", GIT_AUTHOR_NAME="Lumi",
                           GIT_AUTHOR_EMAIL="swarm@lumi.local", GIT_COMMITTER_NAME="Lumi",
                           GIT_COMMITTER_EMAIL="swarm@lumi.local", GIT_NO_REPLACE_OBJECTS="1", GIT_OPTIONAL_LOCKS="0")
        command = [self._git_executable, "--no-pager", "-c", f"core.hooksPath={self._hooks}", "-c", "commit.gpgsign=false",
                   "-c", "core.fsmonitor=false", "-c", "gc.auto=0",
                   *[value for option in overrides for value in ("-c", option)], *args]
        if effect is not None:
            observed = self._execute(*effect, command, cwd, environment=environment)
            if observed.interruption is not None:
                raise observed.interruption
            if observed.timed_out:
                raise Conflict("Owned Git operation exceeded its time limit")
            if observed.cancelled or not observed.output_complete or observed.output_truncated:
                raise Conflict("Owned Git operation output was not completely observed")
            result = subprocess.CompletedProcess(command, observed.exit_code,
                observed.stdout.decode("utf-8", "strict"), observed.stderr.decode("utf-8", "strict"))
            if check and result.returncode:
                # Git's first error line says what went wrong (a live run's was
                # "fatal: '$GIT_DIR' too big"); keep it short and single-line.
                reason = next((line.strip() for line in result.stderr.splitlines() if line.strip()), "")[:200]
                raise Conflict(f"Owned Git operation failed (exit {result.returncode}"
                               f"{': ' + reason if reason else ''}); retained process evidence requires inspection")
            return result
        if not metadata_only and (not args or args[0] not in {"status", "diff"}):
            raise ScopeDenied("Mutating Git commands require captured durable process ownership")
        process = subprocess.Popen(command, cwd=cwd, env=environment, stdin=subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                   encoding="utf-8", errors="strict", **background_process_kwargs(new_process_group=True))
        job = None
        try:
            job = windows_kill_job(process)
            stdout, stderr = process.communicate(timeout=60)
            result = subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
        finally:
            if job:
                close_windows_job(job)
            elif os.name != "nt":
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            elif process.poll() is None:
                process.kill()
            process.wait(timeout=5)
            process.stdout.close()
            process.stderr.close()
        if check and result.returncode:
            raise Conflict(result.stderr.strip() or "Git operation failed")
        return result

    @contextmanager
    def _repository_lock(self, timeout: float = 5) -> Iterator[None]:
        """Coordinate this module across processes without an in-memory lock."""
        handle = self._lock_path.open("a+b")
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        deadline = time.monotonic() + timeout
        try:
            while True:
                try:
                    handle.seek(0)
                    if os.name == "nt":
                        import msvcrt
                        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError as exc:
                    if time.monotonic() >= deadline:
                        raise Conflict("Another integration operation owns this repository") from exc
                    time.sleep(0.02)
            try:
                yield
            finally:
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()

    @staticmethod
    def _revision(value: str) -> str:
        if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", value):
            raise ValueError("Integration requires a pinned full Git commit hash")
        return value

    def _head(self, path: Path) -> str:
        return self._git(path, "rev-parse", "HEAD").stdout.strip()

    def _branch(self, path: Path) -> str:
        return self._git(path, "symbolic-ref", "--quiet", "HEAD", check=False).stdout.strip()

    def _clean(self, path: Path) -> bool:
        return not self._git(path, "status", "--porcelain=v1", "-z", "--untracked-files=all").stdout

    def _unchanged(self, path: Path) -> bool:
        """A candidate still holds exactly its revision's tracked content.

        Its committed revision is what checks verify and what is applied, so
        files a check creates (bytecode, caches, reports) don't change it: a
        live team's Python check wrote __pycache__ and was refused as changed
        input. A modified tracked file still means the check saw other content.
        """
        return not self._git(path, "status", "--porcelain=v1", "-z", "--untracked-files=no").stdout

    def _path(self, value: str) -> Path:
        original = Path(value)
        path = original.resolve(strict=True)
        if original != path:
            raise ScopeDenied("Worktree path changed through a filesystem alias")
        if path == self.root or not path.is_relative_to(self.root):
            raise ScopeDenied("Worktree is outside the captured runtime root")
        common = (path / self._git(path, "rev-parse", "--git-common-dir").stdout.strip()).resolve()
        if common != self._common:
            raise ScopeDenied("Worktree does not belong to the captured repository")
        return path

    def validate_writer(self, context: AttemptContext, writer_id: str) -> dict[str, Any]:
        """Recheck the captured isolated writer before admitting a new effect.

        Filesystem/Git checks run outside a database transaction. The final
        transaction repeats identity, lease, state and grant checks. An executor
        must still enforce the returned path/scope at its actual file boundary.
        """
        def capture() -> dict[str, Any]:
            with self.store._connection() as connection:
                attempt = self.store._attempt(connection, context)
                self.store._admitting(self.store._run(connection, context.scope, context.run_id))
                if attempt["state"] not in ("leased", "running"):
                    raise Conflict("Writer attempt is no longer active")
                record = connection.execute("SELECT * FROM writer_worktrees WHERE id=? AND run_id=? AND attempt_id=? AND epoch=? AND repo_key=?",
                                            (writer_id, context.run_id, context.attempt_id, context.epoch, self.repo_key)).fetchone()
                if record is None:
                    raise ScopeDenied("Writer is unavailable to this captured attempt")
                if record["state"] != "active":
                    raise Conflict("Writer lease no longer admits file effects")
                if _json(json.loads(record["manifest_json"])["grant"]) != attempt["grant_json"]:
                    raise Conflict("Writer grant changed after isolation")
                return dict(record)
        record = capture()
        path = self._path(record["path"])
        if self._head(path) != record["base_revision"]:
            raise Conflict("Writer Git history no longer matches its pinned input")
        if capture() != record:
            raise Conflict("Writer ownership changed during validation")
        return record

    def _admit(self, authority: RunAuthority) -> None:
        with self.store._connection() as connection:
            run = self.store._authority(connection, authority)
            self.store._admitting(run)

    def _record(self, authority: RunAuthority, table: str, identity: str) -> dict[str, Any]:
        with self.store._connection() as connection:
            self.store._authority(connection, authority)
            row = connection.execute(f"SELECT * FROM {table} WHERE id=? AND run_id=? AND repo_key=?",
                                     (identity, authority.run_id, self.repo_key)).fetchone()
            if row is None:
                raise ScopeDenied("Integration record is unavailable in this run and repository")
            if row["epoch"] != authority.epoch:
                raise StaleAuthority("Integration record belongs to a superseded execution epoch")
            return dict(row)

    def _outcome(
        self, authority: RunAuthority, table: str, identity: str, state: str, *,
        revision: str = "", manifest: dict[str, Any] | None = None, error: str = "",
        historical: bool = False,
    ) -> None:
        with self.store._connection(write=True) as connection:
            fresh_observation = not historical
            if historical:
                current = self.store._run(connection, authority.scope, authority.run_id)
                retained = connection.execute(f"SELECT state,process_protocol FROM {table} WHERE id=? AND run_id=? AND epoch=? AND repo_key=?",
                    (identity, authority.run_id, authority.epoch, self.repo_key)).fetchone()
                if (current["epoch"] != authority.epoch and retained is not None
                        and retained["state"] not in {"creating", "finalizing", "preparing", "uncertain"}):
                    return  # A new owner's observed disposition wins over a late old-host failure.
                try:
                    self.store._authority(connection, authority)
                    fresh_observation = True
                except StaleAuthority:
                    pass
                kind = {"writer_worktrees": "writer", "integration_candidates": "candidate"}.get(table)
                if state == "uncertain" and fresh_observation and kind and retained and retained["process_protocol"] == 1:
                    unresolved = connection.execute("SELECT 1 FROM integration_processes WHERE run_id=? "
                        "AND effect_kind=? AND effect_id=? AND state NOT IN ('stopped','not_started') LIMIT 1",
                        (authority.run_id, kind, identity)).fetchone()
                    if unresolved is None:
                        # The exception unwound this sole effect owner. Retain
                        # its partial isolated tree as failed, never as success.
                        # Every possible subprocess is already observed closed.
                        state = "failed"
            else:
                self.store._admitting(self.store._authority(connection, authority))
            updates = "state=?,result_revision=?"
            args: list[Any] = [state, revision]
            if manifest is not None:
                updates += ",manifest_json=?"
                args.append(_json(manifest))
            changed = connection.execute(f"UPDATE {table} SET {updates} WHERE id=? AND run_id=? AND epoch=? AND repo_key=?",
                                         (*args, identity, authority.run_id, authority.epoch, self.repo_key)).rowcount
            if not changed:
                raise ScopeDenied("Integration outcome does not belong to this captured run")
            self.store._event(connection, authority.run_id, f"integration_{state}",
                              {"record_id": identity, "record_type": table, "revision": revision, "error": error})
            if fresh_observation:
                SwarmSupervisor(self.store)._control_checkpoint(connection, authority.run_id)

    def create_writer(self, authority: RunAuthority, context: AttemptContext, *, base_revision: str) -> dict[str, Any]:
        """Create an isolated branch from a pinned clean input; never fall back."""
        self._require_writer_support()
        self.store._same_run(authority, context)
        self._revision(base_revision)
        with self._repository_lock():
            self._admit(authority)
            if self._head(self.project) != base_revision or not self._clean(self.project):
                raise Conflict("Writer baseline requires the clean checkout at its disclosed pinned revision")
            target_branch = self._branch(self.project)
            target_checkout = self._checkout_identity()
            with self.store._connection(write=True) as connection:
                self.store._admitting(self.store._authority(connection, authority))
                attempt = self.store._attempt(connection, context)
                if attempt["state"] not in ("leased", "running"):
                    raise Conflict("Writer attempt is no longer active")
                grant = AssignmentGrant.from_dict(json.loads(attempt["grant_json"]))
                if not grant.write_roots:
                    raise Conflict("Writer isolation requires an explicit write grant")
                existing = connection.execute("SELECT * FROM writer_worktrees WHERE attempt_id=?", (context.attempt_id,)).fetchone()
                if existing:
                    if existing["base_revision"] != base_revision or existing["repo_key"] != self.repo_key:
                        raise Conflict("Writer identity already binds another baseline")
                    if existing["state"] in ("active", "ready"):
                        return dict(existing)
                    raise Conflict("Existing writer intent requires explicit inspection, not automatic replay")
                identity = _id()
                # Git names a worktree's admin folder (.git/worktrees/<name>) after
                # this folder; on Windows a long name can push it past the path
                # limit ("'$GIT_DIR' too big"), so keep the name short.
                path = self.root / f"writer-{identity[:16]}"
                manifest = {"branch": f"codex/swarm-writer-{identity}", "target_branch": target_branch,
                            "target_checkout": target_checkout, "grant": grant.to_dict()}
                connection.execute("INSERT INTO writer_worktrees(id,run_id,attempt_id,epoch,repo_key,path,base_revision,state,manifest_json,process_protocol) "
                                   "VALUES(?,?,?,?,?,?,?,'creating',?,1)",
                                   (identity, authority.run_id, context.attempt_id, authority.epoch, self.repo_key,
                                    str(path), base_revision, _json(manifest)))
                self.store._event(connection, authority.run_id, "writer_create_intent", {"writer_id": identity, "base_revision": base_revision})
            try:
                self._git(self.project, "worktree", "add", "--no-checkout", "-b", manifest["branch"], str(path), base_revision,
                          effect=(authority, "writer", identity))
                # Discover destination-specific includes before any checkout.
                # This is a newly created isolated tree, never the user's tree.
                self._git(path, "reset", "--hard", base_revision, effect=(authority, "writer", identity))
                self._outcome(authority, "writer_worktrees", identity, "active")
            except BaseException as exc:
                self._outcome(authority, "writer_worktrees", identity, "uncertain", error=str(exc), historical=True)
                raise
            return self._record(authority, "writer_worktrees", identity)

    @staticmethod
    def _allowed(path: str, roots: tuple[str, ...]) -> bool:
        normalized = normalize_scope(path)
        if any(part.casefold() == ".git" for part in PurePosixPath(normalized).parts):
            return False
        return any(root == "." or normalized == root or normalized.startswith(root + "/") for root in roots)

    def _changed_paths(self, path: Path, base: str) -> list[str]:
        # --no-renames exposes both deleted and added names, so moving a file
        # cannot hide an out-of-scope source behind an allowed destination.
        tracked = self._git(path, "diff", "--name-only", "-z", "--no-renames", base, "--").stdout
        untracked = self._git(path, "ls-files", "--others", "--exclude-standard", "-z").stdout
        return sorted(set(name for name in (tracked + untracked).split("\0") if name))

    def _check_paths(self, path: Path, changed: list[str], roots: tuple[str, ...]) -> None:
        for name in changed:
            if not self._allowed(name, roots):
                raise Conflict("Writer changed a path outside its immutable write scope")
            target = (path / name).resolve()
            if target != path and not target.is_relative_to(path):
                raise Conflict("Writer path resolves outside its isolated worktree")

    def _check_tree(self, path: Path, revision: str | None = None) -> None:
        symlinks: dict[str, str] = {}
        entries = (self._git(path, "ls-tree", "-rz", revision).stdout if revision
                   else self._git(path, "ls-files", "--stage", "-z").stdout)
        for entry in entries.split("\0"):
            if not entry:
                continue
            metadata, name = entry.split("\t", 1)
            mode, second, third = metadata.split(" ")
            digest, stage = (third, "0") if revision else (second, third)
            if stage != "0":
                raise Conflict("Writer index contains unresolved conflicts")
            if mode == "160000":
                raise Conflict("Submodule trees require a separately qualified integration path")
            if mode == "120000":
                symlinks[name] = self._git(path, "cat-file", "blob", digest).stdout
        for name in symlinks:
            pending = list(PurePosixPath(name).parts)
            components: list[str] = []
            expansions = 0
            while pending:
                part = pending.pop(0)
                if part in ("", "."):
                    continue
                if part == "..":
                    if not components:
                        raise Conflict("Symlink target escapes the isolated worktree")
                    components.pop()
                    continue
                if part.casefold() == ".git":
                    raise Conflict("Symlink target exposes Git administration")
                current = "/".join([*components, part])
                if current in symlinks:
                    expansions += 1
                    if expansions > len(symlinks) + 1:
                        raise Conflict("Symlink cycle cannot form a verified candidate")
                    target = symlinks[current].replace("\\", "/")
                    if target.startswith("/") or ":" in target:
                        raise Conflict("Symlink target escapes the isolated worktree")
                    pending = target.split("/") + pending
                else:
                    components.append(part)

    def finalize_writer(self, authority: RunAuthority, context: AttemptContext, writer_id: str) -> dict[str, Any]:
        """Commit a scoped immutable result, preserving rejected work for review."""
        self.store._same_run(authority, context)
        with self._repository_lock():
            record = self._record(authority, "writer_worktrees", writer_id)
            if record["attempt_id"] != context.attempt_id:
                raise ScopeDenied("Writer does not belong to this attempt")
            with self.store._connection() as connection:
                attempt = self.store._attempt(connection, context)
                if attempt["grant_json"] != _json(json.loads(record["manifest_json"])["grant"]):
                    raise Conflict("Writer grant changed after isolation")
                if connection.execute("SELECT 1 FROM process_observations WHERE attempt_id=? AND state!='stopped'", (context.attempt_id,)).fetchone():
                    raise Conflict("Writer finalization requires observed owned-process termination")
            if record["state"] == "ready":
                return record
            if record["state"] != "active":
                raise Conflict("Writer is not available for a first finalization")
            self._admit(authority)
            path = self._path(record["path"])
            if self._head(path) != record["base_revision"]:
                raise Conflict("Writer changed its pinned Git history outside the integration protocol")
            manifest = json.loads(record["manifest_json"])
            grant = AssignmentGrant.from_dict(manifest["grant"])
            changed = self._changed_paths(path, record["base_revision"])
            try:
                self._check_paths(path, changed, grant.write_roots)
            except BaseException as exc:
                self._outcome(authority, "writer_worktrees", writer_id, "rejected", error=str(exc))
                raise
            self._outcome(authority, "writer_worktrees", writer_id, "finalizing")
            try:
                self._git(path, "add", "--all", "--", ".", effect=(authority, "writer", writer_id))
                self._check_tree(path)
                self._check_paths(path, self._changed_paths(path, record["base_revision"]), grant.write_roots)
                if changed:
                    self._git(path, "commit", "-m", f"SONN swarm writer {context.attempt_id}", effect=(authority, "writer", writer_id))
                result = self._head(path)
                # Validate the immutable committed result too. A writer editing
                # during finalization cannot smuggle an additional path between
                # the preflight and Git's index/commit operations.
                changed = [name for name in self._git(path, "diff", "--name-only", "-z", "--no-renames",
                                                     record["base_revision"], result, "--").stdout.split("\0") if name]
                self._check_paths(path, changed, grant.write_roots)
                self._check_tree(path, result)
                manifest.update(changed_paths=changed, base_revision=record["base_revision"], result_revision=result)
                self._outcome(authority, "writer_worktrees", writer_id, "ready", revision=result, manifest=manifest)
            except BaseException as exc:
                self._outcome(authority, "writer_worktrees", writer_id, "uncertain", error=str(exc), historical=True)
                raise
            return self._record(authority, "writer_worktrees", writer_id)

    def prepare_candidate(
        self, authority: RunAuthority, *, writer_ids: tuple[str, ...],
        required_checks: tuple[CheckSpec, ...], candidate_id: str | None = None,
        criterion_checks: dict[str, dict[str, str]] | None = None,
    ) -> dict[str, Any]:
        """Combine immutable writer revisions in a separate integration tree.

        The trusted planner may bind each work criterion to a declared check
        here, before checks run. Omitting that mapping permits verification but
        creates no acceptance contract; it cannot be supplied later by review.
        """
        if not writer_ids or len(writer_ids) != len(set(writer_ids)):
            raise ValueError("Candidate needs unique writer identities")
        if not required_checks or len({check.key for check in required_checks}) != len(required_checks):
            raise ValueError("Candidate needs unique trusted check specifications")
        identity = candidate_id or _id()
        require_id(identity)
        mapping = json.loads(_json(criterion_checks)) if criterion_checks is not None else {}
        if not isinstance(mapping, dict) or any(not isinstance(value, dict) for value in mapping.values()):
            raise ValueError("Criterion checks must map work IDs to criterion/check names")
        with self._repository_lock():
            self._admit(authority)
            writers = [self._record(authority, "writer_worktrees", writer_id) for writer_id in writer_ids]
            if any(writer["state"] != "ready" for writer in writers):
                raise Conflict("All writer manifests must be finalized")
            with self.store._connection() as connection:
                contracts = {}
                for writer in writers:
                    attempt = connection.execute("SELECT a.state,a.process_state,r.state AS accounting, "
                                                 "s.candidate_revision,s.work_revision,w.revision,w.id AS work_item_id,w.specification "
                                                 "FROM attempts a JOIN reservations r ON r.attempt_id=a.id "
                                                 "JOIN work_items w ON w.id=a.work_item_id "
                                                 "LEFT JOIN submissions s ON s.attempt_id=a.id WHERE a.id=?",
                                                 (writer["attempt_id"],)).fetchone()
                    actions = connection.execute("SELECT 1 FROM action_receipts WHERE attempt_id=? AND state!='completed'",
                                                 (writer["attempt_id"],)).fetchone()
                    if attempt["state"] != "submitted" or attempt["process_state"] != "stopped" or attempt["accounting"] != "settled" or actions:
                        raise Conflict("Candidate construction requires submitted writers with observed termination and resolved effects")
                    SwarmSupervisor._resolved_attempt(connection, writer["attempt_id"])
                    if attempt["candidate_revision"] != writer["result_revision"] or attempt["work_revision"] != attempt["revision"]:
                        raise Conflict("Writer submission does not bind its finalized result and current requirements")
                    contracts[attempt["work_item_id"]] = json.loads(attempt["specification"])["criteria"]
                if criterion_checks is not None:
                    if set(mapping) != set(contracts):
                        raise Conflict("Criterion mapping must cover exactly the candidate work items")
                    check_keys = {check.key for check in required_checks}
                    for work_id, criteria in contracts.items():
                        if not criteria or set(mapping[work_id]) != set(criteria):
                            raise Conflict("Criterion mapping must cover every declared work criterion")
                        if any(not isinstance(key, str) or key not in check_keys for key in mapping[work_id].values()):
                            raise Conflict("Every criterion must bind a declared candidate check")
            bases = {writer["base_revision"] for writer in writers}
            if len(bases) != 1:
                raise Conflict("Writers must share the same immutable input base")
            base = bases.pop()
            branches = {json.loads(writer["manifest_json"])["target_branch"] for writer in writers}
            if len(branches) != 1:
                raise Conflict("Writers do not share a captured application branch")
            checkouts = {_json(json.loads(writer["manifest_json"]).get("target_checkout")) for writer in writers}
            if len(checkouts) != 1 or "null" in checkouts:
                raise Conflict("Writers require the same retained application checkout identity")
            manifest = {"writers": [{"id": writer["id"], "attempt_id": writer["attempt_id"],
                                     "result_revision": writer["result_revision"]} for writer in writers],
                        "checks": [asdict(check) for check in required_checks], "target_branch": branches.pop(),
                        "target_checkout": json.loads(checkouts.pop()), "criterion_checks": mapping}
            path = self.root / f"candidate-{hashlib.sha256(identity.encode()).hexdigest()[:16]}"  # See create_writer.
            with self.store._connection(write=True) as connection:
                self.store._admitting(self.store._authority(connection, authority))
                existing = connection.execute("SELECT * FROM integration_candidates WHERE id=?", (identity,)).fetchone()
                if existing:
                    if existing["run_id"] != authority.run_id or existing["repo_key"] != self.repo_key:
                        raise ScopeDenied("Candidate is unavailable in this run")
                    if existing["manifest_json"] != _json(manifest):
                        raise Conflict("Candidate identity already binds different inputs")
                    if existing["state"] in ("ready", "verified", "failed", "conflict", "applied"):
                        return dict(existing)
                    raise Conflict("Uncertain candidate intent cannot be replayed")
                connection.execute("INSERT INTO integration_candidates(id,run_id,epoch,repo_key,path,base_revision,state,manifest_json,process_protocol) "
                                   "VALUES(?,?,?,?,?,?,'preparing',?,1)",
                                   (identity, authority.run_id, authority.epoch, self.repo_key, str(path), base, _json(manifest)))
                self.store._event(connection, authority.run_id, "candidate_prepare_intent", {"candidate_id": identity, **manifest})
            try:
                self._git(self.project, "worktree", "add", "--no-checkout", "--detach", str(path), base,
                          effect=(authority, "candidate", identity))
                self._git(path, "reset", "--hard", base, effect=(authority, "candidate", identity))
                revisions = [writer["result_revision"] for writer in writers if writer["result_revision"] != base]
                if revisions:
                    merged = self._git(path, "cherry-pick", "--no-commit", *revisions, check=False, effect=(authority, "candidate", identity))
                    if merged.returncode:
                        self._outcome(authority, "integration_candidates", identity, "conflict", error=merged.stderr)
                        return self._record(authority, "integration_candidates", identity)
                    self._git(path, "commit", "-m", f"SONN combined candidate {identity}", effect=(authority, "candidate", identity))
                self._check_tree(path)
                result = self._head(path)
                self._outcome(authority, "integration_candidates", identity, "ready", revision=result)
            except BaseException as exc:
                self._outcome(authority, "integration_candidates", identity, "uncertain", error=str(exc), historical=True)
                raise
            return self._record(authority, "integration_candidates", identity)

    def run_check(self, authority: RunAuthority, candidate_id: str, check_key: str, *, receipt_id: str | None = None) -> dict[str, Any]:
        """Run a declared check on its exact candidate under owned job limits."""
        self._require_writer_support()
        identity = receipt_id or _id()
        require_id(identity)
        with self._repository_lock():
            record = self._record(authority, "integration_candidates", candidate_id)
            self._admit(authority)
            if record["state"] not in ("ready", "failed", "verified"):
                raise Conflict("Candidate is unavailable for verification")
            path = self._path(record["path"])
            if self._head(path) != record["result_revision"] or not self._unchanged(path):
                raise Conflict("Check input no longer matches the immutable candidate")
            checks = {item["key"]: item for item in json.loads(record["manifest_json"])["checks"]}
            if check_key not in checks:
                raise ScopeDenied("Check is not part of this candidate's trusted specification")
            check = checks[check_key]
            with self.store._connection(write=True) as connection:
                self.store._admitting(self.store._authority(connection, authority))
                old = connection.execute("SELECT * FROM integration_checks WHERE id=?", (identity,)).fetchone()
                if old:
                    if old["candidate_id"] != candidate_id or old["check_key"] != check_key:
                        raise Conflict("Check receipt identity already binds another execution")
                    if old["state"] == "running":
                        raise Conflict("Uncertain check intent requires inspection, not replay")
                    return dict(old)
                connection.execute("INSERT INTO integration_checks(id,candidate_id,check_key,candidate_revision,argv_json,state,process_protocol) "
                                   "VALUES(?,?,?,?,?,'running',1)", (identity, candidate_id, check_key, record["result_revision"], _json(check["argv"])))
                self.store._event(connection, authority.run_id, "candidate_check_intent", {"candidate_id": candidate_id, "receipt_id": identity})
            observed = None
            try:
                # Each check starts from exactly the candidate's revision: files an
                # earlier check left (caches, generated code) are removed from this
                # Lumi-owned worktree first. Checks run code the writers wrote, so
                # they get the environment without Lumi's provider keys.
                self._git(path, "clean", "-ffdxq", effect=(authority, "check", identity))
                from lumi.secrets_store import child_env
                observed = self._execute(authority, "check", identity, check["argv"], path,
                    environment=child_env(), timeout_seconds=check["timeout_seconds"], max_output_bytes=65536)
                if observed.interruption is not None:
                    raise observed.interruption
                self._admit(authority)
                exact = self._head(path) == record["result_revision"] and self._unchanged(path)
                state = "passed" if observed.exit_code == 0 and observed.output_complete and exact else "failed"
                if observed.timed_out or observed.cancelled:
                    state = "timed_out" if observed.timed_out else "cancelled"
                if not exact:
                    state = "input_changed"
                output = (observed.stdout + observed.stderr).decode("utf-8", "replace")
                if observed.output_truncated:
                    output += "\n[Check output truncated at the configured byte limit]"
                with self.store._connection(write=True) as connection:
                    self.store._admitting(self.store._authority(connection, authority))
                    connection.execute("UPDATE integration_checks SET state=?,exit_code=?,output=? WHERE id=?",
                                       (state, observed.exit_code, output, identity))
                    all_passed = all(connection.execute(
                        "SELECT state FROM integration_checks WHERE candidate_id=? AND check_key=? "
                        "AND candidate_revision=? ORDER BY rowid DESC LIMIT 1", (candidate_id, key, record["result_revision"]),
                    ).fetchone()[0] == "passed" if connection.execute(
                        "SELECT 1 FROM integration_checks WHERE candidate_id=? AND check_key=?", (candidate_id, key),
                    ).fetchone() else False for key in checks)
                    connection.execute("UPDATE integration_candidates SET state=? WHERE id=?",
                                       ("verified" if all_passed and exact else "failed", candidate_id))
                    self.store._event(connection, authority.run_id, "candidate_check_observed", {"receipt_id": identity, "state": state})
                    SwarmSupervisor(self.store)._control_checkpoint(connection, authority.run_id)
                    return dict(connection.execute("SELECT * FROM integration_checks WHERE id=?", (identity,)).fetchone())
            except BaseException as exc:
                partial = getattr(exc, "_swarm_effect_observation", None)
                if observed is None and isinstance(partial, ArgvResult):
                    observed = partial
                with self.store._connection(write=True) as connection:
                    current = self.store._run(connection, authority.scope, authority.run_id)
                    retained = connection.execute("SELECT state FROM integration_checks WHERE id=?", (identity,)).fetchone()
                    if (current["epoch"] != authority.epoch and retained is not None
                            and retained["state"] not in ("running", "uncertain")):
                        raise  # A fresh owner's cleanup observation is authoritative.
                    fresh = False
                    try:
                        self.store._authority(connection, authority)
                        fresh = True
                    except StaleAuthority:
                        pass
                    unresolved = connection.execute("SELECT 1 FROM integration_processes WHERE run_id=? "
                        "AND effect_kind='check' AND effect_id=? AND state NOT IN ('stopped','not_started') LIMIT 1",
                        (authority.run_id, identity)).fetchone()
                    known_stop = fresh and isinstance(exc, AdmissionClosed) and unresolved is None
                    # A current owner's durable gate receipts can prove the
                    # check never received executable argv (for example a
                    # central policy denial). Absence alone is not evidence;
                    # once any invocation marker exists, retain uncertainty.
                    never_invoked = fresh and unresolved is None and connection.execute(
                        "SELECT 1 FROM integration_processes WHERE run_id=? AND effect_kind='check' AND effect_id=? LIMIT 1",
                        (authority.run_id, identity)).fetchone() is not None and connection.execute(
                        "SELECT 1 FROM integration_processes WHERE run_id=? AND effect_kind='check' AND effect_id=? AND invoked=1 LIMIT 1",
                        (authority.run_id, identity)).fetchone() is None
                    known_stop = known_stop or never_invoked
                    state = "cancelled" if known_stop else "uncertain"
                    output = (observed.stdout + observed.stderr).decode("utf-8", "replace") if observed else ""
                    if observed and observed.output_truncated:
                        output += "\n[Check output truncated at the configured byte limit]"
                    output += f"\n[Check interrupted: {exc}]"
                    connection.execute("UPDATE integration_checks SET state=?,exit_code=?,output=? WHERE id=?",
                                       (state, observed.exit_code if observed else None, output, identity))
                    connection.execute("UPDATE integration_candidates SET state=? WHERE id=?", ("failed" if known_stop else "uncertain", candidate_id))
                    self.store._event(connection, authority.run_id, "candidate_check_interrupted", {"receipt_id": identity, "state": state})
                    if fresh:
                        SwarmSupervisor(self.store)._control_checkpoint(connection, authority.run_id)
                raise

    def apply(self, authority: RunAuthority, candidate_id: str, *, approval: ApplyApproval) -> dict[str, Any]:
        """Apply only explicitly authorized, verified work to an unchanged base."""
        if (approval.scope, approval.run_id, approval.candidate_id) != (authority.scope, authority.run_id, candidate_id):
            raise ScopeDenied("Application approval is not bound to this run and candidate")
        require_id(approval.id)
        if approval.expires_at <= self.store.clock():
            raise Conflict("Application approval has expired")
        with self._repository_lock():
            self._admit(authority)
            candidate = self._record(authority, "integration_candidates", candidate_id)
            if (approval.expected_base, approval.target_revision) != (candidate["base_revision"], candidate["result_revision"]):
                raise Conflict("Application approval does not match the verified candidate")
            with self.store._connection() as connection:
                existing = connection.execute("SELECT * FROM integration_applications WHERE id=?", (approval.id,)).fetchone()
                if existing:
                    if existing["candidate_id"] != candidate_id or existing["approval_json"] != _json(asdict(approval)):
                        raise Conflict("Approval identity already binds different semantics")
                    if existing["state"] == "applied":
                        return dict(existing)
                    raise Conflict("Previous application intent requires explicit inspection")
            if candidate["state"] != "verified":
                raise Conflict("Only an independently checked exact candidate can be applied")
            path = self._path(candidate["path"])
            if self._head(path) != candidate["result_revision"] or not self._unchanged(path):
                raise Conflict("Candidate changed after verification")
            if self._head(self.project) != candidate["base_revision"] or not self._clean(self.project):
                raise Conflict("Application deferred: user checkout is dirty or its base changed")
            manifest = json.loads(candidate["manifest_json"])
            self._require_application_checkout(manifest)
            ignored = [name for name in self._git(self.project, "ls-files", "--others", "--ignored", "--exclude-standard", "-z").stdout.split("\0") if name]
            changed = [name for name in self._git(self.project, "diff", "--name-only", "-z", "--no-renames",
                                                candidate["base_revision"], candidate["result_revision"], "--").stdout.split("\0") if name]
            if any(left.casefold() == right.casefold() or left.casefold().startswith(right.casefold() + "/")
                   or right.casefold().startswith(left.casefold() + "/") for left in ignored for right in changed):
                raise Conflict("Application deferred: candidate would replace an ignored user file")
            with self.store._connection(write=True) as connection:
                self.store._admitting(self.store._authority(connection, authority))
                connection.execute("INSERT INTO integration_applications(id,candidate_id,expected_base,target_revision,state,approval_json,process_protocol) "
                                   "VALUES(?,?,?,?,'applying',?,1)", (approval.id, candidate_id, candidate["base_revision"],
                                                                   candidate["result_revision"], _json(asdict(approval))))
                self.store._event(connection, authority.run_id, "candidate_apply_intent", {"candidate_id": candidate_id, "approval_id": approval.id})
            try:
                # Fast-forward avoids merge commits and never resets or stashes
                # the user. Git itself rechecks the index/worktree during apply.
                self._git(self.project, "merge", "--ff-only", "--no-edit", "--no-autostash", "--no-overwrite-ignore", candidate["result_revision"],
                          effect=(authority, "application", approval.id))
                observed = self._head(self.project)
                if observed != candidate["result_revision"]:
                    raise Conflict("Application outcome differs from the approved revision")
                self._require_application_checkout(manifest)
                self._admit(authority)
                with self.store._connection(write=True) as connection:
                    self.store._admitting(self.store._authority(connection, authority))
                    connection.execute("UPDATE integration_applications SET state='applied',observed_revision=? WHERE id=?", (observed, approval.id))
                    connection.execute("UPDATE integration_candidates SET state='applied' WHERE id=?", (candidate_id,))
                    self.store._event(connection, authority.run_id, "candidate_applied", {"candidate_id": candidate_id, "revision": observed})
                    SwarmSupervisor(self.store)._control_checkpoint(connection, authority.run_id)
                    return dict(connection.execute("SELECT * FROM integration_applications WHERE id=?", (approval.id,)).fetchone())
            except BaseException as exc:
                with self.store._connection(write=True) as connection:
                    self.store._run(connection, authority.scope, authority.run_id)
                    changed = connection.execute("UPDATE integration_applications SET state='uncertain' WHERE id=? "
                        "AND state IN ('applying','uncertain')", (approval.id,)).rowcount
                    if changed:
                        self.store._event(connection, authority.run_id, "candidate_apply_uncertain", {"approval_id": approval.id, "error": str(exc)})
                raise

    def reconcile_application(self, authority: RunAuthority, approval_id: str) -> dict[str, Any]:
        """Inspect an uncertain apply; never retry it or reset the user's work.

        A current recovery owner may record a historical epoch's observed ref.
        This acknowledges an already observed effect and grants no new effect.
        """
        with self._repository_lock():
            with self.store._connection() as connection:
                self.store._authority(connection, authority)
                application = connection.execute(
                    "SELECT a.*,c.epoch AS candidate_epoch,c.manifest_json AS candidate_manifest FROM integration_applications a JOIN integration_candidates c ON c.id=a.candidate_id "
                    "WHERE a.id=? AND c.run_id=? AND c.repo_key=?", (approval_id, authority.run_id, self.repo_key),
                ).fetchone()
                if application is None:
                    raise ScopeDenied("Application is unavailable in this run and repository")
                if application["state"] not in ("applying", "uncertain"):
                    return dict(application)
                if application["process_protocol"] != 1:
                    raise Conflict("Historical application has no durable process containment proof")
                if application["candidate_epoch"] == authority.epoch:
                    unresolved = connection.execute("SELECT 1 FROM integration_processes WHERE run_id=? "
                        "AND effect_kind='application' AND effect_id=? AND state NOT IN ('stopped','not_started') LIMIT 1",
                        (authority.run_id, approval_id)).fetchone()
                    if unresolved:
                        raise Conflict("Application process cleanup must be observed before inspecting its result")
            self._require_application_checkout(json.loads(application["candidate_manifest"]))
            if application["candidate_epoch"] != authority.epoch:
                self._processes.reconcile_effect(authority, "application", approval_id,
                    evidence="Observe owned application subprocess cleanup before ref inspection")
            observed = self._head(self.project)
            self._require_application_checkout(json.loads(application["candidate_manifest"]))
            state = ("applied" if observed == application["target_revision"] else
                     "not_applied" if observed == application["expected_base"] else "uncertain")
            with self.store._connection(write=True) as connection:
                self.store._authority(connection, authority)
                connection.execute("UPDATE integration_applications SET state=?,observed_revision=? WHERE id=?",
                                   (state, observed, approval_id))
                if state == "applied":
                    connection.execute("UPDATE integration_candidates SET state='applied' WHERE id=?", (application["candidate_id"],))
                self.store._event(connection, authority.run_id, "candidate_apply_reconciled",
                                  {"approval_id": approval_id, "state": state, "observed_revision": observed})
                SwarmSupervisor(self.store)._control_checkpoint(connection, authority.run_id)
                return dict(connection.execute("SELECT * FROM integration_applications WHERE id=?", (approval_id,)).fetchone())
