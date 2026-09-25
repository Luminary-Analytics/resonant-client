"""Sharing a conversation as a read-only copy in Lumi Cloud (lumi_cloud/shares.py there).

The copy holds people's messages, Lumi's replies, and a line for each action
(the tool and what it acted on), marked when it failed. It never holds what
the tools returned: file contents, command output and pages stay on this
computer. Saved key values and anything that looks like a secret are
removed before it leaves. Lumi Cloud shows it at a link, to the
organization's members or, when the organization allows it, to anyone with
the link.

The link of each shared conversation is remembered in ``shares.json`` in
Lumi's state folder, so Share shows it again and can stop it.
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any

from .paths import state_home

SHARES_FILE = "shares.json"
_lock = threading.Lock()


def _redact(text: str) -> str:
    from . import secret_scan

    return secret_scan.redact_text(str(text or ""), patterns=True)[0]


def _action(name: str, arguments: Any) -> str:
    args = arguments if isinstance(arguments, dict) else {}
    path = str(args.get("path") or args.get("file_path") or "")
    command = " ".join(str(args.get("command") or "").split())
    if name == "bash" and command:
        return f"Ran `{command[:300]}`"
    if name == "file_write" and path:
        return f"Wrote {path}"
    if name in ("file_edit", "file_replace") and path:
        return f"Edited {path}"
    if name == "file_read" and path:
        return f"Read {path}"
    if name in ("glob", "grep") and (args.get("pattern") or args.get("query")):
        return f"Searched for {str(args.get('pattern') or args.get('query'))[:120]}"
    return f"Used {name}" + (f" on {path}" if path else "")


def export(display_events: list[dict], *, title: str, project_path: str, model: str) -> dict:
    """The copy to share: messages, replies and actions, without tool results or secrets."""
    entries: list[dict] = []
    actions: dict[str, dict] = {}
    for event in display_events:
        kind = event.get("event")
        if kind == "user_message" and str(event.get("text") or "").strip():
            entries.append({"role": "user", "text": _redact(event["text"])})
        elif kind == "text.done" and str(event.get("text") or "").strip():
            entries.append({"role": "assistant", "text": _redact(event["text"])})
        elif kind == "tool.call" and event.get("name"):
            entry = {"role": "action", "text": _redact(_action(str(event["name"]), event.get("arguments")))}
            entries.append(entry)
            if event.get("call_id"):
                actions[str(event["call_id"])] = entry
        elif kind == "tool.result" and (event.get("is_error") or event.get("denied")):
            entry = actions.get(str(event.get("call_id") or ""))
            if entry is not None:
                entry["error"] = True
    return {"title": _redact(title or "Shared session")[:200],
            "project": os.path.basename(os.path.normpath(project_path or "")) if project_path else "",
            "model": model[:120], "entries": entries}


# ── Which conversations are shared ──────────────────────────────────────────


def _path() -> Path:
    return state_home() / SHARES_FILE


def remembered() -> dict:
    try:
        data = json.loads(_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def remember(session_id: str, share: dict | None) -> None:
    with _lock:
        data = remembered()
        if share:
            data[session_id] = {"id": share.get("id"), "url": share.get("url"), "visibility": share.get("visibility"),
                                "shared_at": time.time()}
        else:
            data.pop(session_id, None)
        path = _path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def share(cloud: Any, copy: dict, *, organization_id: str, visibility: str) -> dict:
    """Send the copy to Lumi Cloud; its answer: id, url and visibility."""
    return cloud.account_call("POST", "/api/v1/shares",
                              json={**copy, "organization_id": organization_id, "visibility": visibility})


def stop(cloud: Any, share_id: str) -> None:
    cloud.account_call("DELETE", f"/api/v1/shares/{share_id}")
