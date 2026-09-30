"""Git for Lumi's own work, without the programs a repository names.

Lumi runs Git by itself: the status the page asks for when a project opens,
indexing, ``@diff``, checkpoints, hand-offs, the editor bridge, agent
worktrees, model comparisons, the Git popover and the agent's Git and GitHub
tools. A repository's own Git settings can name programs that Git then
starts: an fsmonitor hook and the hooks in ``.git/hooks``, filters' clean
and process commands on status, diff and add, diff drivers, gpg to show
signatures, ssh and credential helpers on push. A clone never brings these
settings, but a folder that arrives with its own ``.git`` (an archive, a
share, a copied checkout) can.

Every such call goes through ``run`` (or ``argv``):

* Git is the installed one (``lumi/executables.py``).
* Always ``core.fsmonitor=false``, ``core.hooksPath`` set to an empty folder
  Lumi owns, ``safe.bareRepository=explicit``, ``log.showSignature=false``
  and ``protocol.ext.allow=never``; ``--no-optional-locks`` for status and
  ``--no-ext-diff --no-textconv`` for diff, show and log. Only commits and
  pushes someone asked for (the Git popover, the agent's commit and
  pull-request tools) keep a trusted project's hooks, as its own
  ``git commit`` would.
* In a project the person hasn't trusted (``gui/workspace_trust.py``), Lumi
  first reads the repository's settings (``git config``, which runs nothing),
  with the files they include and its submodules' settings. If they name
  programs, Lumi runs no other Git there: ``GitRefused`` carries ``NOTICE``.

The agent's own shell runs Git as the person would, by design.
"""

from __future__ import annotations

import os
import subprocess
import threading
import time
from typing import Any, Sequence

from .executables import program

NOTICE = ("Git features are off for this project until you trust it: its Git settings run programs "
          "({names}). Trust it in Settings > Privacy & security > Project trust to use them.")

# Settings that make the Git commands Lumi runs start a program. The fixed
# options replace core.fsmonitor and core.hooksPath for every command, so
# those don't count (a husky project sets core.hooksPath itself); diff
# drivers do, although diffs also get --no-ext-diff --no-textconv.
_PROGRAM_KEYS = frozenset({
    "core.sshcommand", "core.askpass", "core.gitproxy", "core.editor", "core.pager", "sequence.editor",
    "diff.external", "gpg.program", "gpg.openpgp.program", "gpg.x509.program", "gpg.ssh.program",
    "gpg.ssh.defaultkeycommand", "credential.helper", "uploadpack.packobjectshook",
    "core.alternaterefscommand",
})
_PROGRAM_SUFFIXES = (
    ("filter.", (".clean", ".smudge", ".process")),
    ("diff.", (".command", ".textconv")),
    ("merge.", (".driver",)),
    ("credential.", (".helper",)),
    ("remote.", (".uploadpack", ".receivepack")),
)
# Git never lets an alias replace one of its commands, but one named like a
# command Lumi runs says the repository expects something else to happen.
_LUMI_COMMANDS = frozenset({
    "status", "diff", "log", "show", "ls-files", "rev-parse", "cat-file", "add", "commit", "commit-tree",
    "write-tree", "read-tree", "reset", "worktree", "branch", "merge", "stash", "update-ref", "show-ref",
    "for-each-ref", "remote", "push", "config", "checkout",
})
_MAX_REPOSITORIES = 64   # a repository and its submodules, checked for one project
_MAX_INCLUDES = 16

_hooks_lock = threading.Lock()
_checks: dict[str, tuple[float, str]] = {}
_checks_lock = threading.Lock()
_CHECK_SECONDS = 2.0  # one page action asks Git several things; read the settings once


class GitRefused(RuntimeError):
    """Lumi runs no Git in this project: its settings name programs and it isn't trusted."""


_fallback_hooks: list[str] = []


def empty_hooks_folder() -> str:
    """A folder Lumi owns and keeps empty, given to Git as ``core.hooksPath``."""
    import tempfile
    from pathlib import Path

    from .paths import state_home

    with _hooks_lock:
        try:
            folder = state_home() / "git-no-hooks"
            folder.mkdir(parents=True, exist_ok=True)
        except OSError:  # a home Lumi can't write to: a private temporary folder instead
            if not _fallback_hooks:
                _fallback_hooks.append(tempfile.mkdtemp(prefix="lumi-git-no-hooks-"))
            folder = Path(_fallback_hooks[0])
        for entry in folder.iterdir():  # never a hook, whatever put one there
            try:
                entry.unlink()
            except OSError:
                pass
    return str(folder)


def _split(args: Sequence[str]) -> tuple[list[str], str, list[str]]:
    """Leading ``-c name=value`` pairs, the subcommand, and its arguments."""
    config: list[str] = []
    index = 0
    while index + 1 < len(args) and args[index] == "-c":
        config += list(args[index:index + 2])
        index += 2
    rest = list(args[index:])
    return config, (rest[0] if rest else ""), rest[1:]


def argv(*args: str, project: str | os.PathLike | None = None, hooks: bool = False) -> list[str]:
    """The program and arguments for ``git <args>`` run by Lumi itself.

    ``args`` may start with ``-c name=value`` pairs before the subcommand.
    ``hooks`` keeps the repository's hooks: only for a trusted project's
    commit or push that someone asked for (``run`` checks the trust).
    """
    config, subcommand, rest = _split(args)
    command = [program("git", exclude=[project] if project else ()),
               "-c", "core.fsmonitor=false", "-c", "safe.bareRepository=explicit",
               "-c", "log.showSignature=false", "-c", "protocol.ext.allow=never"]
    if not hooks:
        command += ["-c", f"core.hooksPath={empty_hooks_folder()}"]
    command += config
    if subcommand == "status":
        command.append("--no-optional-locks")
    if subcommand:
        command.append(subcommand)
    if subcommand in ("diff", "show", "log"):
        command += ["--no-ext-diff", "--no-textconv"]
    return command + rest


def _read(folder: str, *args: str) -> subprocess.CompletedProcess:
    """A ``git`` command that only reads (configuration, the index), with the fixed options."""
    from .processes import background_process_kwargs

    return subprocess.run(argv(*args, project=folder), cwd=folder, capture_output=True, stdin=subprocess.DEVNULL,
                          timeout=30, **background_process_kwargs())


def _records(output: bytes, fields: int) -> list[list[str]]:
    parts = output.decode("utf-8", "replace").split("\0")
    return [parts[index:index + fields] for index in range(0, len(parts) - fields + 1, fields)]


def _include_target(folder: str, origin: str, value: str) -> str:
    """The file an ``include.path`` or ``includeIf.*.path`` names, as Git resolves it."""
    path = value.strip()
    if not path:
        return ""
    if path.startswith("~"):
        return os.path.expanduser(path)
    if os.path.isabs(path):
        return path
    source = origin[len("file:"):] if origin.startswith("file:") else ""
    if not source:
        return ""
    source = source if os.path.isabs(source) else os.path.join(folder, source)
    return os.path.normpath(os.path.join(os.path.dirname(source), path))


def _is_program(name: str, value: str) -> bool:
    value = value.strip()
    if name in _PROGRAM_KEYS:
        return bool(value)
    if name.startswith("alias."):
        return name[len("alias."):] in _LUMI_COMMANDS
    for prefix, suffixes in _PROGRAM_SUFFIXES:
        if name.startswith(prefix) and name.endswith(suffixes) and name.count(".") >= 2:
            return bool(value)
    return False


def _setting_names(folder: str) -> list[str]:
    """Names of the repository's own settings that run programs.

    The repository's local and worktree settings, with the files they include
    (Git evaluates ``includeIf`` conditions here; one whose condition doesn't
    hold now is read too, since it may hold for a later command).
    """
    done = _read(folder, "config", "--list", "--show-scope", "--show-origin", "--includes", "-z")
    if done.returncode == 0:
        records = [(origin, item) for scope, origin, item in _records(done.stdout, 3)
                   if scope in ("local", "worktree")]
    else:  # before Git 2.26, or not a repository
        done = _read(folder, "config", "--local", "--list", "--show-origin", "--includes", "-z")
        if done.returncode != 0:
            return []  # not a repository: the command that follows fails on its own
        records = [(origin, item) for origin, item in _records(done.stdout, 2)]
    names: list[str] = []
    pending: list[tuple[str, str]] = []
    seen: set[str] = set()
    while True:
        for origin, item in records:
            name, _, value = item.partition("\n")
            name = name.lower()
            if _is_program(name, value) and name not in names:
                names.append(name)
            if name.startswith("includeif.") and name.endswith(".path"):
                pending.append((origin, value))
        records = []
        while pending and not records and len(seen) < _MAX_INCLUDES:
            origin, value = pending.pop()
            target = _include_target(folder, origin, value)
            if not target or target in seen or not os.path.isfile(target):
                continue
            seen.add(target)
            done = _read(folder, "config", "--file", target, "--list", "--show-origin", "--includes", "-z")
            if done.returncode == 0:
                records = [(origin, item) for origin, item in _records(done.stdout, 2)]
        if not records:
            return names


def _submodules(folder: str) -> list[str]:
    """Working folders of the repository's submodules that have their own repository.

    Status, diff and add look inside them with the submodule's own settings.
    """
    done = _read(folder, "ls-files", "--stage", "-z", "--", ":/")
    if done.returncode != 0:
        return []
    found = []
    for record in done.stdout.decode("utf-8", "replace").split("\0"):
        meta, _, path = record.partition("\t")
        if meta.startswith("160000 ") and path:
            location = os.path.normpath(os.path.join(folder, path))
            if os.path.exists(os.path.join(location, ".git")):
                found.append(location)
    return found


def repository_programs(folder: str | os.PathLike) -> list[str]:
    """Settings of the repository at ``folder``, and of its submodules, that run programs; [] for none."""
    names: list[str] = []
    queue = [os.fspath(folder)]
    visited: set[str] = set()
    while queue and len(visited) < _MAX_REPOSITORIES:
        current = queue.pop(0)
        key = os.path.normcase(os.path.abspath(current))
        if key in visited:
            continue
        visited.add(key)
        own = _setting_names(current)
        for name in own:
            if name not in names:
                names.append(name)
        if not own:
            queue.extend(_submodules(current))
    if queue:
        names.append("more submodules than Lumi checks")
    return names


_trusted_here: set[str] = set()


def _folder_key(folder: str | os.PathLike) -> str:
    return os.path.normcase(os.path.realpath(os.path.abspath(os.fspath(folder))))


def trust_for_this_process(project: str | os.PathLike) -> None:
    """``lumi run --trust-project``: this process trusts ``project`` and the folders inside it."""
    _trusted_here.add(_folder_key(project))


def trusted(project: str | os.PathLike) -> bool:
    """Whether the person trusts ``project`` (gui/workspace_trust.py), read afresh."""
    from .gui.workspace_trust import WorkspaceTrust

    if _trusted_here:
        key = _folder_key(project)
        if any(key == root or key.startswith(root.rstrip(os.sep) + os.sep) for root in _trusted_here):
            return True
    try:
        return WorkspaceTrust().status(os.fspath(project)).trusted
    except Exception:  # an unreadable decision is no trust
        return False


def refusal(folder: str | os.PathLike, *, project: str | os.PathLike | None = None,
            trusted_project: bool | None = None) -> str:
    """Why Lumi runs no Git in ``folder`` (a project, or a checkout of one), or "".

    ``project`` is the project whose trust applies (``folder`` by default);
    ``trusted_project`` is the caller's own answer, when it has one.
    """
    folder = os.fspath(folder)
    if trusted_project is None:
        trusted_project = trusted(project or folder)
    if trusted_project:
        return ""
    key = os.path.normcase(os.path.abspath(folder))
    now = time.monotonic()
    with _checks_lock:
        cached = _checks.get(key)
    if cached and now - cached[0] < _CHECK_SECONDS:
        return cached[1]
    try:
        names = repository_programs(folder)
    except (OSError, subprocess.SubprocessError):
        names = []  # Git missing or stuck: the command itself reports that
    reason = NOTICE.format(names=", ".join(names[:5]) + (", …" if len(names) > 5 else "")) if names else ""
    with _checks_lock:
        if len(_checks) > 256:
            _checks.clear()
        _checks[key] = (now, reason)
    return reason


def run(folder: str | os.PathLike, *args: str, project: str | os.PathLike | None = None,
        trusted_project: bool | None = None, hooks: bool = False, env: dict[str, str] | None = None,
        input: str | bytes | None = None, text: bool = True,
        timeout: float | None = 60) -> subprocess.CompletedProcess:
    """Run ``git <args>`` in ``folder`` for Lumi (see the module's docstring).

    Raises ``GitRefused`` in an untrusted project whose settings name programs,
    ``ProgramNotFound`` (a ``FileNotFoundError``) without Git, and
    ``subprocess.TimeoutExpired``. The result's ``returncode`` is Git's.
    """
    from .processes import background_process_kwargs

    folder = os.fspath(folder)
    owner = os.fspath(project) if project else folder
    if trusted_project is None:
        trusted_project = trusted(owner)
    reason = refusal(folder, project=owner, trusted_project=trusted_project)
    if reason:
        raise GitRefused(reason)
    kwargs: dict[str, Any] = dict(cwd=folder, capture_output=True, timeout=timeout, env=env,
                                  **background_process_kwargs())
    if input is not None:
        kwargs["input"] = input
    else:
        # Never the caller's own input: that can be a protocol pipe (a Team worker's).
        kwargs["stdin"] = subprocess.DEVNULL
    if text:
        kwargs.update(text=True, encoding="utf-8", errors="replace")
    return subprocess.run(argv(*args, project=owner, hooks=bool(hooks and trusted_project)), **kwargs)


def status_entries(raw: str) -> tuple[str | None, list[dict]]:
    """Parse ``git status --porcelain=v1 -z`` (with or without ``-b``).

    Returns the branch header (without ``## ``) and entries of
    ``{"x", "y", "path"}``, plus ``"from"`` (the old name) for a rename or
    copy. Names are never stripped: they can start or end with spaces.
    """
    fields = raw.split("\0")
    header = None
    entries = []
    index = 0
    while index < len(fields):
        record = fields[index]
        index += 1
        if record.startswith("## ") and header is None and not entries:
            header = record[3:]
            continue
        if len(record) < 4 or record[2] != " ":
            continue
        entry = {"x": record[0], "y": record[1], "path": record[3:]}
        if "R" in record[:2] or "C" in record[:2]:
            if index < len(fields):
                entry["from"] = fields[index]
                index += 1
        entries.append(entry)
    return header, entries
