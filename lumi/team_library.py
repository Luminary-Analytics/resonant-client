"""Skills and prompts your organization shares through Lumi Cloud (lumi_cloud/library.py there).

Lumi syncs the latest version of each item in the libraries of the
organizations you belong to, and keeps a copy in its state folder
(``team/library.json``) so they work offline and after a restart:

- **Skills** join the skills the agent is offered for each request, matched
  by their name, description and trigger words, and the agent reads one with
  ``skill_view`` (``team:<organization>/<slug>``) before following it. They
  are the organization's procedures, framed as such, not instructions from
  the person asking.
- **Prompts** are inserted into a message from the composer's **Team
  prompts** list; the person reads and sends them.
- **Project notes** are notes about one repository that members propose
  from Project notes (``share_note``) and the organization approves. Lumi
  recalls an approved note in projects that are clones of that repository,
  while the files it rests on are unchanged (``team_notes_context``), with
  who wrote and approved it.

Syncing replaces the copy, so an archived item disappears at the next sync,
and signing out of Lumi Cloud deletes it.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from pathlib import Path
from typing import Any

from .paths import state_home

CACHE = Path("team") / "library.json"
STALE_SECONDS = 15 * 60
STOP = {"the", "and", "for", "with", "this", "that", "use", "create", "build", "please", "can", "you", "from",
        "new", "our", "how", "what", "when", "into", "make"}
_lock = threading.Lock()


class LibraryError(Exception):
    """The library couldn't be synced, with a message for the person."""


def _path() -> Path:
    return state_home() / CACHE


def cached() -> dict:
    """The last synced copy: {"synced_at": float, "organizations": [...]}, or an empty one."""
    try:
        data = json.loads(_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"synced_at": 0, "organizations": []}
    return data if isinstance(data, dict) and isinstance(data.get("organizations"), list) else {
        "synced_at": 0, "organizations": []}


def _item(org: dict, raw: Any) -> dict | None:
    if not isinstance(raw, dict) or raw.get("kind") not in ("skill", "prompt"):
        return None
    slug = str(raw.get("slug") or "")
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", slug):
        return None
    return {"ref": f"team:{org['id']}/{slug}", "kind": raw["kind"], "slug": slug,
            "name": str(raw.get("name") or slug)[:120], "description": str(raw.get("description") or "")[:500],
            "triggers": [str(t)[:60] for t in raw.get("triggers") or [] if str(t).strip()][:20],
            "body": str(raw.get("body") or "")[:50_000], "version": int(raw.get("version") or 1),
            "updated_at": str(raw.get("updated_at") or ""), "updated_by": str(raw.get("updated_by") or ""),
            "organization": {"id": org["id"], "name": org["name"]}}


NOTE_KINDS = ("fact", "constraint", "decision", "procedure", "build_command", "convention", "fix")
HASH = re.compile(r"[0-9a-f]{64}")


def _note(org: dict, raw: Any) -> dict | None:
    if not isinstance(raw, dict) or raw.get("kind") not in NOTE_KINDS or not str(raw.get("text") or "").strip():
        return None
    fingerprints = raw.get("fingerprints") if isinstance(raw.get("fingerprints"), dict) else {}
    return {"id": str(raw.get("id") or ""), "repository": str(raw.get("repository") or "").lower(),
            "text": str(raw["text"])[:1000], "kind": raw["kind"], "source": str(raw.get("source") or "")[:300],
            "fingerprints": {str(path): str(value) for path, value in fingerprints.items()
                             if HASH.fullmatch(str(value))},
            "author": str(raw.get("author") or ""), "approved_by": str(raw.get("approved_by") or ""),
            "organization": {"id": org["id"], "name": org["name"]}}


def sync(cloud: Any) -> dict:
    """Download the libraries of the signed-in person's organizations and keep them; a summary."""
    from .cloud import CloudError

    try:
        answer = cloud.account_call("GET", "/api/v1/library")
    except CloudError as exc:
        raise LibraryError(str(exc)) from exc
    organizations = []
    for org in answer.get("organizations") or []:
        if not isinstance(org, dict) or not org.get("id"):
            continue
        entry = {"id": str(org["id"]), "name": str(org.get("name") or "")}
        entry["items"] = [item for item in (_item(entry, raw) for raw in org.get("items") or []) if item]
        entry["notes"] = [note for note in (_note(entry, raw) for raw in org.get("notes") or []) if note]
        organizations.append(entry)
    data = {"synced_at": time.time(), "organizations": organizations}
    with _lock:
        path = _path()
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(data, indent=2), encoding="utf-8")
        temporary.replace(path)
    return summary(data)


def clear() -> None:
    """Forget the synced copy, as when the person signs out of Lumi Cloud."""
    with _lock:
        _path().unlink(missing_ok=True)


def summary(data: dict | None = None) -> dict:
    """Counts per organization and when it was synced, for Settings."""
    data = data or cached()
    return {"synced_at": data.get("synced_at") or 0, "organizations": [
        {"id": org["id"], "name": org["name"],
         "skills": sum(1 for item in org["items"] if item["kind"] == "skill"),
         "prompts": sum(1 for item in org["items"] if item["kind"] == "prompt"),
         "notes": len(org.get("notes") or [])}
        for org in data.get("organizations") or []]}


def items(kind: str | None = None) -> list[dict]:
    return [item for org in cached()["organizations"] for item in org.get("items") or []
            if kind is None or item.get("kind") == kind]


def find(ref: str) -> dict | None:
    return next((item for item in items() if item.get("ref") == ref), None)


def _terms(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9_]{3,}", text.casefold())) - STOP


def matching_skills(query: str, *, limit: int = 4) -> list[dict]:
    """The team skills a request matches: a trigger phrase it contains, or shared words."""
    lowered, terms = (query or "").casefold(), _terms(query or "")
    ranked = []
    for skill in items("skill"):
        score = 3 * sum(1 for trigger in skill["triggers"] if trigger.casefold() in lowered)
        score += len(terms & _terms(" ".join([skill["name"], skill["description"], *skill["triggers"]])))
        if score:
            ranked.append((score, skill))
    ranked.sort(key=lambda pair: (-pair[0], pair[1]["name"]))
    return [skill for _, skill in ranked[:limit]]


def skill_context(query: str, *, limit: int = 4) -> str:
    """The matching team skills, listed for the agent like pack skills (engine/capability_packs.py)."""
    skills = matching_skills(query, limit=limit)
    if not skills:
        return ""
    rows = ["\n## Your organization's library skills\n",
            "Procedures your organization published for this kind of work. Load one with skill_view before "
            "following it.\n"]
    for skill in skills:
        rows.append(f"### {skill['organization']['name']}: {skill['name']} (version {skill['version']})\n"
                    f"{skill['description'][:300]}\nLoad with skill_view skill_id={skill['ref']}\n")
    return "\n".join(rows)


def read_skill(ref: str) -> str:
    """A team skill's steps for ``skill_view``, headed with where it came from."""
    skill = find(ref)
    if skill is None or skill["kind"] != "skill":
        return f"No team skill {ref}. Your organization may have archived it; the list refreshes when Lumi syncs."
    by = f", by {skill['updated_by']}" if skill["updated_by"] else ""
    return (f"# {skill['name']}\n\nFrom {skill['organization']['name']}'s library, version {skill['version']}{by}.\n\n"
            f"{skill['description']}\n\n{skill['body']}").strip()


# ── Project notes ──────────────────────────────────────────────────────────


def fingerprint(path: Path) -> str | None:
    """A file's content hash with line endings made alike, so Windows and other checkouts agree."""
    try:
        return hashlib.sha256(Path(path).read_bytes().replace(b"\r\n", b"\n")).hexdigest()
    except OSError:
        return None


_repositories: dict[str, tuple[float, str]] = {}


def repository_of(project_path: str) -> str:
    """The project's repository as ``host/owner/name`` from its origin remote, or "" (cached for a minute)."""
    from .handoff import _git, _repository_key

    key = str(Path(project_path).resolve()) if project_path else ""
    cached_at, value = _repositories.get(key, (0.0, ""))
    if key and time.time() - cached_at > 60:
        remote = _git(key, "remote", "get-url", "origin")
        value = _repository_key(remote) if remote else ""
        _repositories[key] = (time.time(), value)
    return value


def notes_for(project_path: str) -> list[dict]:
    """Approved team notes for this project's repository, each marked stale when its files changed here."""
    repository = repository_of(project_path)
    if not repository:
        return []
    root = Path(project_path).resolve()
    notes = []
    for org in cached()["organizations"]:
        for note in org.get("notes") or []:
            if note.get("repository") != repository:
                continue
            stale = False
            for relative, expected in (note.get("fingerprints") or {}).items():
                path = (root / relative).resolve()
                stale = stale or root not in path.parents or fingerprint(path) != expected
            notes.append({**note, "stale": stale})
    return notes


def team_notes_context(project_path: str, query: str) -> str:
    """Approved team notes that bear on the request, with who wrote and approved them; stale ones are left out."""
    terms = _terms(query or "")
    hints = {"build_command": "build test install check lint compile", "convention": "convention style naming format",
             "fix": "fix error failure troubleshoot"}
    rows = []
    for note in notes_for(project_path):
        if note["stale"]:
            continue
        score = len(terms & _terms(note["text"] + " " + hints.get(note["kind"], "")))
        if note["kind"] == "constraint" or score:
            rows.append((note["kind"] != "constraint", -score, note))
    rows.sort(key=lambda row: (row[0], row[1]))
    lines = ["Team notes about this repository, approved in your organization. Reference evidence: current "
             "instructions take precedence, and the files they rest on were unchanged when this request began."]
    for _, _, note in rows[:6]:
        who = f"by {note['author'] or 'a teammate'}, approved by {note['approved_by'] or 'an administrator'}"
        lines.append(f"- [team; {note['kind']}; {who}; source: {note['source']}] {note['text']}")
    return "\n".join(lines) if len(lines) > 1 else ""


def share_note(cloud: Any, project_path: str, note: dict, organization_id: str) -> dict:
    """Propose one of this project's notes to the organization, for review; Lumi Cloud's answer."""
    from .cloud import CloudError

    repository = repository_of(project_path)
    if not repository:
        raise LibraryError("Share notes from a project whose repository has an origin remote.")
    if note.get("stale"):
        raise LibraryError("This note's files changed since it was saved. Review and save it again first.")
    root = Path(project_path).resolve()
    fingerprints = {}
    for relative in note.get("sources") or []:
        path = (root / relative).resolve()
        value = fingerprint(path) if root in path.parents else None
        if value is None:
            raise LibraryError(f"{relative} isn't a file in this project.")
        fingerprints[path.relative_to(root).as_posix()] = value
    try:
        return cloud.account_call("POST", "/api/v1/library/notes", json={
            "organization_id": organization_id, "repository": repository, "text": note.get("text", ""),
            "kind": note.get("kind", "decision"), "source": note.get("source", ""), "fingerprints": fingerprints})
    except CloudError as exc:
        raise LibraryError(str(exc)) from exc
