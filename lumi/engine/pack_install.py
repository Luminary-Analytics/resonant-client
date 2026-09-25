"""Install a capability pack from a git repository, pinned to one commit.

Settings > Capability packs > Install from Git (or ``install_from_git``)
fetches exactly one commit of an https repository into ``~/.lumi/packs/<id>``.
Installing never trusts the pack: it arrives "not approved", and nothing in
it runs until the person reviews what it would run and approves it, like
any other pack (lumi/engine/capability_packs.py). The approval pins the
content digest, so the files can't change underneath it.

Why a commit, not a branch or tag: both can be moved to other code after the
review. ``resolve`` turns a tag or branch into the commit it names now, so
people can pick a release by name, and the installation records that commit.

The fetch is deliberately narrow: https only, no credentials in the URL, no
credential helper or prompt, no submodules, and one commit at depth 1. The
``.git`` folder is dropped afterwards; ``plugins[<id>]["source"]`` in the
settings records where the pack came from.
"""

from __future__ import annotations

import fnmatch
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import urllib.parse
from dataclasses import dataclass
from pathlib import Path

from ..paths import state_home

_COMMIT = re.compile(r"[0-9a-f]{40}")
_REF = re.compile(r"[A-Za-z0-9._/+-]{1,200}")
_SUBDIR = re.compile(r"[A-Za-z0-9._/-]{1,200}")
_GIT_TIMEOUT = 120


class PackInstallError(RuntimeError):
    """The installation was refused or failed; the message says why."""


@dataclass(frozen=True)
class InstalledPack:
    id: str
    name: str
    version: str
    path: str
    url: str
    commit: str
    subdir: str = ""


def normalize_url(url: str, *, allow_local: bool = False) -> str:
    """An https repository URL without credentials, or PackInstallError."""
    text = str(url or "").strip()
    if allow_local and text and "://" not in text:
        return text  # tests only: a local repository path
    parts = urllib.parse.urlsplit(text)
    if parts.scheme != "https" or not parts.hostname:
        raise PackInstallError("Use the repository's https address, for example https://github.com/owner/repo.")
    if parts.username or parts.password:
        raise PackInstallError("Leave credentials out of the address; packs install from public repositories.")
    if parts.query or parts.fragment:
        raise PackInstallError("The repository address can't have a query or fragment.")
    path = parts.path.rstrip("/")
    if not path or path == "/":
        raise PackInstallError("Include the repository path, for example https://github.com/owner/repo.")
    return urllib.parse.urlunsplit(("https", parts.netloc.lower(), path, "", ""))


def source_allowed(url: str, allowed: tuple[str, ...] | None) -> bool:
    """Whether the organization's ``extensions.allowed_sources`` permits ``url``."""
    if allowed is None:
        return True
    candidate = url.removesuffix(".git").lower()
    return any(fnmatch.fnmatchcase(candidate, pattern.removesuffix(".git").lower()) for pattern in allowed)


def _git(args: list[str], *, cwd: Path | None = None, local: bool = False) -> str:
    git = shutil.which("git")
    if not git:
        raise PackInstallError("Installing from a repository needs Git. Install it and try again.")
    config = ["-c", "credential.helper=", "-c", "core.askPass=", "-c", "submodule.recurse=false",
              "-c", "advice.detachedHead=false"]
    if not local:
        config += ["-c", "protocol.allow=never", "-c", "protocol.https.allow=always"]
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "", "SSH_ASKPASS": "",
           "GIT_CONFIG_NOSYSTEM": "1", "GCM_INTERACTIVE": "never"}
    try:
        completed = subprocess.run([git, *config, *args], cwd=cwd, env=env, capture_output=True,
                                   text=True, timeout=_GIT_TIMEOUT)
    except subprocess.TimeoutExpired as exc:
        raise PackInstallError("Git took too long; check the address and your network.") from exc
    except OSError as exc:
        raise PackInstallError(f"Git couldn't run: {exc}") from exc
    if completed.returncode != 0:
        detail = (completed.stderr.strip().splitlines() or ["unknown error"])[-1][:300]
        raise PackInstallError(f"Git failed: {detail}")
    return completed.stdout


def _force_rmtree(path: Path) -> None:
    """Delete a tree, including the read-only files Git writes on Windows."""
    def retry(function, target, _error):
        os.chmod(target, stat.S_IWRITE)
        function(target)

    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=retry)
    else:  # pragma: no cover - Python 3.11
        shutil.rmtree(path, onerror=retry)


def resolve(url: str, ref: str, *, allow_local: bool = False) -> str:
    """The commit a tag, branch or commit names in the repository now."""
    url = normalize_url(url, allow_local=allow_local)
    ref = str(ref or "").strip()
    if _COMMIT.fullmatch(ref.lower()):
        return ref.lower()
    if not _REF.fullmatch(ref) or ref.startswith("-"):
        raise PackInstallError("Give a commit (40 hex characters), a tag or a branch.")
    output = _git(["ls-remote", "--", url, ref, f"refs/tags/{ref}^{{}}", f"refs/tags/{ref}", f"refs/heads/{ref}"],
                  local=allow_local)
    commits: dict[str, str] = {}
    for line in output.splitlines():
        sha, _, name = line.partition("\t")
        commits[name.strip()] = sha.strip()
    # An annotated tag's own object isn't a commit; its peeled entry (^{}) is.
    for name in (f"refs/tags/{ref}^{{}}", f"refs/tags/{ref}", f"refs/heads/{ref}", ref):
        if commits.get(name):
            return commits[name]
    raise PackInstallError(f"{ref} isn't a tag or branch in {url}.")


def install_from_git(url: str, commit: str, *, subdir: str = "", dest_root: Path | None = None,
                     allowed_sources: tuple[str, ...] | None = None, allow_local: bool = False,
                     expect_id: str = "", expect_digest: str = "") -> InstalledPack:
    """Fetch ``commit`` and install the pack it contains; it still needs approval.

    ``expect_id`` and ``expect_digest`` (an organization registry's pin) are
    checked before anything already installed is replaced.
    """
    from .capability_packs import CapabilityPackError, CapabilityPackManager, _manifest_path

    url = normalize_url(url, allow_local=allow_local)
    if not source_allowed(url, allowed_sources):
        raise PackInstallError("Your organization's policy doesn't allow packs from this repository.")
    commit = str(commit or "").strip().lower()
    if not _COMMIT.fullmatch(commit):
        raise PackInstallError("Pin the pack to a full commit (40 hex characters); use Check to resolve a tag.")
    subdir = str(subdir or "").strip().strip("/")
    if subdir and (not _SUBDIR.fullmatch(subdir) or ".." in subdir.split("/")):
        raise PackInstallError("The folder must be a plain path inside the repository.")
    root = Path(dest_root) if dest_root is not None else state_home() / "packs"
    root.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="lumi-pack-", dir=root) as scratch:
        checkout = Path(scratch) / "checkout"
        checkout.mkdir()
        _git(["init", "--quiet"], cwd=checkout, local=allow_local)
        _git(["fetch", "--quiet", "--depth", "1", "--no-tags", "--", url, commit], cwd=checkout, local=allow_local)
        # Links would be checked out as plain files; see the check below too.
        _git(["-c", "core.symlinks=false", "checkout", "--quiet", "--detach", commit],
             cwd=checkout, local=allow_local)
        head = _git(["rev-parse", "HEAD"], cwd=checkout, local=allow_local).strip()
        if head != commit:
            raise PackInstallError(f"Git checked out {head}, not {commit}.")
        _force_rmtree(checkout / ".git")
        source = checkout / subdir if subdir else checkout
        if not source.is_dir() or _manifest_path(source) is None:
            where = f"{subdir}/" if subdir else "the repository root"
            raise PackInstallError(f"No lumi-pack.json in {where} at that commit.")
        if any(path.is_symlink() for path in source.rglob("*")):
            raise PackInstallError("The pack contains symbolic links, which packs can't have.")
        # Parse it as a personal pack, so a bad manifest is refused before it lands.
        try:
            pack, data = CapabilityPackManager(Path(scratch) / "no-project", roots=())._load(source)
        except CapabilityPackError as exc:
            raise PackInstallError(f"The pack's manifest is invalid: {exc}") from exc
        if not data.get("id") or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}", pack.id):
            raise PackInstallError("The pack's manifest needs an id usable as a folder name.")
        if expect_id and pack.id != expect_id:
            raise PackInstallError(f"That commit holds the pack {pack.id}, not {expect_id}.")
        if expect_digest:
            from .pack_signing import signed_digest

            if signed_digest(source) != expect_digest:
                raise PackInstallError("The files at that commit don't match the content digest your organization "
                                       "pinned, so nothing was installed.")
        target = root / pack.id
        staged = Path(scratch) / "staged"
        shutil.move(str(source), str(staged))
        if target.exists():
            _force_rmtree(target)
        shutil.move(str(staged), str(target))
    return InstalledPack(id=pack.id, name=pack.name, version=pack.version, path=str(target),
                         url=url, commit=commit, subdir=subdir)


def remove(pack_id: str, *, dest_root: Path | None = None) -> bool:
    """Delete a pack installed from git; False when nothing was there."""
    root = Path(dest_root) if dest_root is not None else state_home() / "packs"
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}", str(pack_id or "")):
        raise PackInstallError("Unknown pack.")
    target = root / pack_id
    if not target.is_dir():
        return False
    _force_rmtree(target)
    return True


def source_record(installed: InstalledPack) -> dict:
    """What ``plugins[<id>]["source"]`` keeps about an installation."""
    return {"type": "git", "url": installed.url, "commit": installed.commit, "subdir": installed.subdir,
            "installed_at": int(time.time())}
