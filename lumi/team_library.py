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

Syncing replaces the copy, so an archived item disappears at the next sync,
and signing out of Lumi Cloud deletes it.
"""

from __future__ import annotations

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
         "prompts": sum(1 for item in org["items"] if item["kind"] == "prompt")}
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
