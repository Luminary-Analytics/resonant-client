"""Remove a linked Git worktree Lumi made, without following links out of it.

Teams' writers and combined candidates, agent worktrees and model comparisons
work in linked worktrees under Lumi's runtime folder. What runs there (a
check's ``npm install`` with a ``file:`` dependency, a model's shell command)
can leave a directory junction or symbolic link pointing outside the
worktree. ``git worktree remove --force`` follows a junction on Windows and
deletes the files it points to: a review emptied a real ``file:`` dependency
outside the repository that way. And ``git worktree prune`` isn't limited to
Lumi's worktrees: it also forgets the person's own worktree whose folder is
away for the moment (an unplugged drive, a folder being moved).

So Lumi removes its worktrees itself, never with Git:

1. read the worktree's ``.git`` pointer (``gitdir: <common>/worktrees/<name>``);
2. unlink every symbolic link and junction inside it, without following it;
3. delete the folder (``shutil.rmtree``, which doesn't follow junctions
   either since Python 3.8);
4. remove only that worktree's ``$GIT_COMMON_DIR/worktrees/<name>`` entry,
   and only when the entry points back at this folder.

Git then no longer lists the worktree, and its branch can be deleted.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import os
from pathlib import Path
import shutil
import stat
import sys

# A reparse point that names another file or folder (a junction, a symbolic
# link): IsReparseTagNameSurrogate. Other reparse points (cloud placeholders,
# deduplicated files) are the file itself.
_NAME_SURROGATE = 0x20000000


@dataclass
class WorktreeRemoval:
    """What ``remove_worktree`` did."""

    path: str
    removed: bool = False  # the folder is gone (or was already)
    admin_entries: list[str] = field(default_factory=list)  # $GIT_COMMON_DIR/worktrees/<name> removed
    links: int = 0  # links unlinked inside it, never followed
    locked: bool = False  # `git worktree lock`ed by someone: left as it is
    error: str = ""


def _extended(path: str) -> str:
    """The path Windows lets past 260 characters (node_modules nests deeply); unchanged elsewhere."""
    if sys.platform != "win32":
        return path
    absolute = os.path.abspath(path)
    if absolute.startswith("\\\\?\\"):
        return absolute
    if absolute.startswith("\\\\"):
        return "\\\\?\\UNC\\" + absolute[2:]
    return "\\\\?\\" + absolute


def _plain(path: str) -> str:
    if path.startswith("\\\\?\\UNC\\"):
        return "\\\\" + path[8:]
    return path[4:] if path.startswith("\\\\?\\") else path


def _is_link(info: os.stat_result) -> bool:
    """A symbolic link or junction (checked with ``follow_symlinks=False``)."""
    if stat.S_ISLNK(info.st_mode):
        return True
    attributes = getattr(info, "st_file_attributes", 0)
    return bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
                and getattr(info, "st_reparse_tag", 0) & _NAME_SURROGATE)


def _unlink_link(path: str, info: os.stat_result) -> None:
    """Remove the link itself; what it points to is untouched."""
    directory = (getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_DIRECTORY", 0x10)
                 if sys.platform == "win32" else False)
    if directory:
        os.rmdir(path)  # a junction or directory link: rmdir removes the link, never the target's files
    else:
        os.unlink(path)


def unlink_links(folder: str | os.PathLike) -> int:
    """Unlink every link and junction below ``folder`` without following any; returns how many."""
    count = 0
    pending = [_extended(os.fspath(folder))]
    while pending:
        current = pending.pop()
        try:
            entries = list(os.scandir(current))
        except OSError:
            continue
        for entry in entries:
            try:
                info = entry.stat(follow_symlinks=False)
            except OSError:
                continue
            if _is_link(info):
                _unlink_link(entry.path, info)
                count += 1
            elif stat.S_ISDIR(info.st_mode):
                pending.append(entry.path)
    return count


def _writable_and_retry(function, path, _error) -> None:
    """Read-only files (a package's, Git's objects) can't be deleted on Windows until writable."""
    if _is_link(os.stat(path, follow_symlinks=False)):
        raise PermissionError(f"Refused to change a link's target: {path}")
    os.chmod(path, stat.S_IWRITE)
    function(path)


def remove_tree(path: str | os.PathLike) -> int:
    """Delete a folder Lumi owns, links inside it unlinked rather than followed; returns links unlinked.

    A link or junction given as ``path`` itself is unlinked, never followed.
    Raises ``OSError`` when something can't be deleted (a file in use).
    """
    target = _extended(os.fspath(path))
    try:
        info = os.stat(target, follow_symlinks=False)
    except FileNotFoundError:
        return 0
    if _is_link(info):
        _unlink_link(target, info)
        return 1
    if not stat.S_ISDIR(info.st_mode):
        os.unlink(target)
        return 0
    links = unlink_links(target)
    if sys.version_info >= (3, 12):
        shutil.rmtree(target, onexc=_writable_and_retry)
    else:  # pragma: no cover - Python 3.11
        shutil.rmtree(target, onerror=_writable_and_retry)
    return links


def same_path(left: str | os.PathLike, right: str | os.PathLike) -> bool:
    """The same place, however it is spelled (Git writes C:/..., a short 8.3 name, another case)."""
    def key(value: str | os.PathLike) -> str:
        return os.path.normcase(os.path.realpath(_plain(os.fspath(value))))

    return key(left) == key(right)


def _gitdir_pointer(dot_git: Path) -> Path | None:
    """The folder a worktree's ``.git`` file names, or None when it isn't one."""
    try:
        if not dot_git.is_file() or dot_git.is_symlink():
            return None
        text = dot_git.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return None
    if not text.startswith("gitdir:"):
        return None
    pointer = Path(text[len("gitdir:"):].strip())
    return pointer if pointer.is_absolute() else dot_git.parent / pointer


def _points_back(entry: Path, worktree: Path) -> bool:
    """Whether an admin entry's ``gitdir`` file names this worktree's ``.git``."""
    try:
        text = (entry / "gitdir").read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return False
    return bool(text) and same_path(text, worktree / ".git")


def admin_entries(worktree: str | os.PathLike, common_dir: str | os.PathLike) -> list[Path]:
    """This worktree's entries in ``<common_dir>/worktrees``: its ``.git`` pointer's, and any
    whose ``gitdir`` names it (left behind when its folder was removed earlier)."""
    worktree, common = Path(worktree), Path(common_dir)
    registry = common / "worktrees"
    found: list[Path] = []
    pointer = _gitdir_pointer(worktree / ".git")
    if pointer is not None and same_path(pointer.parent, registry) and _points_back(pointer, worktree):
        found.append(pointer)
    try:
        candidates = [entry for entry in registry.iterdir() if entry.is_dir() and not entry.is_symlink()]
    except OSError:
        candidates = []
    for entry in candidates:
        if not any(same_path(entry, known) for known in found) and _points_back(entry, worktree):
            found.append(entry)
    return found


def remove_worktree(path: str | os.PathLike, *, common_dir: str | os.PathLike) -> WorktreeRemoval:
    """Remove a linked worktree of the repository whose ``$GIT_COMMON_DIR`` is ``common_dir``.

    The caller has checked that ``path`` is one of its own worktrees (under
    its runtime folder). Links inside are unlinked and never followed; only
    this worktree's admin entry in ``common_dir`` is removed, and only when it
    names this folder. A worktree someone locked (``git worktree lock``) is
    left as it is. Errors are reported in the result, not raised.
    """
    worktree = Path(os.path.abspath(os.fspath(path)))
    result = WorktreeRemoval(str(worktree))
    entries = admin_entries(worktree, common_dir)
    if any((entry / "locked").exists() for entry in entries):
        result.locked = True
        result.error = "The worktree is locked (git worktree lock); it was left as it is."
        return result
    try:
        result.links = remove_tree(worktree)
    except OSError as exc:
        result.error = f"{exc.strerror or exc}: {_plain(str(exc.filename or worktree))}"
        return result
    result.removed = True
    for entry in entries:
        try:
            remove_tree(entry)
        except OSError as exc:
            result.error = f"Couldn't remove Git's record of the worktree ({exc.strerror or exc})"
            continue
        result.admin_entries.append(str(entry))
    return result


def worktree_common_dir(path: str | os.PathLike) -> Path | None:
    """The ``$GIT_COMMON_DIR`` a linked worktree's own ``.git`` file names; None when it names none.

    Only that file and the ``commondir`` it leads to, never a folder further
    up (unlike repository_common_dir): a worktree whose pointer is gone is
    no repository's any more.
    """
    git_dir = _gitdir_pointer(Path(os.path.abspath(os.fspath(path))) / ".git")
    if git_dir is None:
        return None
    try:
        common = (git_dir / "commondir").read_text(encoding="utf-8", errors="replace").strip()
        return (git_dir / common).resolve() if common else None
    except OSError:
        return None


def folder_size(path: str | os.PathLike) -> int:
    """The bytes of the files in a folder, links not followed: what removing it frees."""
    total = 0
    pending = [_extended(os.fspath(path))]
    while pending:
        current = pending.pop()
        try:
            entries = list(os.scandir(current))
        except OSError:
            continue
        for entry in entries:
            try:
                info = entry.stat(follow_symlinks=False)
            except OSError:
                continue
            if _is_link(info):
                continue
            if stat.S_ISDIR(info.st_mode):
                pending.append(entry.path)
            else:
                total += info.st_size
    return total


def repository_common_dir(path: str | os.PathLike) -> Path | None:
    """The ``$GIT_COMMON_DIR`` of the repository containing ``path``, found without running Git.

    Git's own search, simplified: the nearest ``.git`` folder, or a ``.git``
    file's ``gitdir`` and that folder's ``commondir``. None outside a repository.
    """
    try:
        start = Path(os.path.abspath(os.fspath(path)))
    except (TypeError, ValueError):
        return None
    for folder in (start, *start.parents):
        dot_git = folder / ".git"
        try:
            if dot_git.is_dir():
                return dot_git.resolve()
        except OSError:
            return None
        git_dir = _gitdir_pointer(dot_git)
        if git_dir is None:
            continue
        try:
            common = (git_dir / "commondir").read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            common = ""
        try:
            return (git_dir / common).resolve() if common else git_dir.resolve()
        except OSError:
            return None
    return None
