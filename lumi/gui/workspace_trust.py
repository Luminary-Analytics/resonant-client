"""Trust for what a project brings with it: instructions and execution rules.

A repository can carry instruction files (AGENTS.md, LUMI.md, CLAUDE.md and
the others in ``project_instructions.INSTRUCTION_FILES``) and project notes
(``.lumi/memory.json``) that go into the model's prompt, and a
``lumi-policy.json`` whose ``allow`` rules let Auto-edit run the calls they
match without asking (engine/policies.py). Cloning a repository shouldn't
hand it any of these. Until the user trusts a project:

* its instruction files and notes are not loaded, nor is a codebase index
  summary (a committed ``.lumi/index.json`` would otherwise count as built);
* its policy keeps only ``deny`` and ``prompt`` rules, which can only make
  Lumi more careful;
* automatic lint and test runs, which execute repository code, are skipped.

Trust is remembered per folder. If ``lumi-policy.json`` changes after the
user trusted the project, its ``allow`` rules are ignored again until the
user reviews the change. Capability packs keep their own per-pack approval.

On first run, projects already in Recent projects are trusted, so upgrading
changes nothing for folders the user had already chosen to work in. Their
policy's ``allow`` rules still wait for one review: before trust existed they
never skipped a prompt, so honoring them unreviewed would change what Lumi
does in those folders without asking.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Iterable

from ..paths import state_home
from .project_instructions import INSTRUCTION_FILES

logger = logging.getLogger(__name__)

TRUST_FILE = "trusted_projects.json"
POLICY_FILES = ("lumi-policy.json", "resonant-policy.json")


@dataclass
class TrustStatus:
    """What a project brings and whether Lumi uses it."""

    project_path: str
    decision: str = ""              # "trusted", "restricted" or "" (not asked yet)
    instructions: list[str] = field(default_factory=list)
    notes: bool = False             # .lumi/memory.json in the repository
    policy_file: str = ""
    policy_allows: int = 0          # allow rules the policy file contains
    policy_changed: bool = False    # the policy file isn't the version the user trusted
    policy_digest: str = ""         # SHA-256 of the policy file this status read

    @property
    def trusted(self) -> bool:
        return self.decision == "trusted"

    @property
    def has_content(self) -> bool:
        return bool(self.instructions or self.policy_file or self.notes)

    @property
    def needs_decision(self) -> bool:
        return self.has_content and (self.decision == "" or (self.trusted and self.policy_changed))

    @property
    def load_instructions(self) -> bool:
        return self.trusted

    @property
    def honor_policy_allows(self) -> bool:
        return self.trusted and not self.policy_changed

    def to_dict(self) -> dict:
        data = asdict(self)
        data.update(
            trusted=self.trusted,
            needs_decision=self.needs_decision,
            load_instructions=self.load_instructions,
            honor_policy_allows=self.honor_policy_allows,
        )
        return data


def _key(project_path: str) -> str:
    return os.path.normcase(os.path.realpath(os.path.abspath(project_path)))


def policy_file(project_path: str) -> str:
    for name in POLICY_FILES:
        candidate = os.path.join(project_path, name)
        if os.path.isfile(candidate):
            return candidate
    return ""


def _policy_facts(path: str) -> tuple[str, int]:
    """The policy file's digest and how many allow rules Lumi would use from it."""
    if not path:
        return "", 0
    try:
        raw = Path(path).read_bytes()
    except OSError:
        return "", 0
    from ..engine.policies import PolicyAction, repository_rules

    allows = 0
    try:
        # A file with mistakes contributes no allow rules (repository_rules).
        rules, _problems = repository_rules(json.loads(raw.decode("utf-8")))
        allows = sum(1 for rule in rules if rule.action == PolicyAction.ALLOW.value)
    except (ValueError, RecursionError):
        pass
    return hashlib.sha256(raw).hexdigest(), allows


class WorkspaceTrust:
    """The user's trust decisions, stored in ``trusted_projects.json``.

    The app passes ``recent_projects``, and its first run with trust records
    them. ``lumi run``, the terminal UI and model comparisons only read
    decisions and pass none: they never create the file, or the app's first
    run would find it and trust none of the Recent projects.
    """

    def __init__(self, path: str | Path | None = None, *, recent_projects: Iterable[str] | None = None):
        self._path = Path(path) if path else state_home() / TRUST_FILE
        self._lock = threading.Lock()
        self._projects: dict[str, dict] = {}
        if self._path.exists():
            try:
                data = json.loads(self._path.read_text(encoding="utf-8"))
                self._projects = dict(data.get("projects") or {})
            except (ValueError, OSError):
                logger.warning("Could not read %s; asking about projects again", self._path)
        elif recent_projects is not None:
            # First run with trust: keep every project the user already works in.
            for project in recent_projects:
                if project and os.path.isdir(project):
                    self._record(project, "trusted", note="already in Recent projects", review_allows=True)
            self._save()

    def status(self, project_path: str) -> TrustStatus:
        instructions = [
            rel for rel in INSTRUCTION_FILES
            if os.path.isfile(os.path.join(project_path, rel))
        ]
        policy = policy_file(project_path)
        digest, allows = _policy_facts(policy)
        notes = any(
            os.path.isfile(os.path.join(project_path, folder, "memory.json"))
            for folder in (".lumi", ".resonant")
        )
        with self._lock:
            entry = dict(self._projects.get(_key(project_path)) or {})
        decision = entry.get("decision", "")
        changed = bool(decision == "trusted" and policy and entry.get("policy_digest") != digest)
        return TrustStatus(
            project_path=project_path,
            decision=decision,
            instructions=instructions,
            notes=notes,
            policy_file=os.path.basename(policy),
            policy_allows=allows,
            policy_changed=changed,
            policy_digest=digest,
        )

    def trust(self, project_path: str) -> TrustStatus:
        self._record(project_path, "trusted")
        self._save()
        return self.status(project_path)

    def restrict(self, project_path: str) -> TrustStatus:
        self._record(project_path, "restricted")
        self._save()
        return self.status(project_path)

    def forget(self, project_path: str) -> None:
        with self._lock:
            self._projects.pop(_key(project_path), None)
        self._save()

    def decisions(self) -> list[dict]:
        """Every remembered decision, for Settings."""
        with self._lock:
            items = [dict(entry, key=key) for key, entry in self._projects.items()]
        return sorted(items, key=lambda item: item.get("path", "").lower())

    def _record(self, project_path: str, decision: str, *, note: str = "", review_allows: bool = False) -> None:
        digest, allows = _policy_facts(policy_file(project_path))
        if review_allows and allows:
            # No trusted version yet, so status() reports the policy as
            # changed and its allow rules stay off until the user trusts it.
            digest = ""
        with self._lock:
            self._projects[_key(project_path)] = {
                "path": os.path.abspath(project_path),
                "decision": decision,
                "policy_digest": digest,
                "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                **({"note": note} if note else {}),
            }

    def _save(self) -> None:
        with self._lock:
            data = {"version": 1, "projects": self._projects}
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
            os.replace(tmp, self._path)
        except OSError:
            logger.warning("Could not save %s", self._path, exc_info=True)
