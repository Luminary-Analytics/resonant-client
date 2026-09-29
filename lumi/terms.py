"""Lumi's terms: the End User License Agreement and, for pre-release builds, the Alpha and Beta Test Terms.

The texts ship with Lumi in ``lumi/legal/`` (rendered from ``lumi/legal/templates/`` with the facts in
``lumi/legal/terms.json`` by ``packaging/legal_texts.py``), so they can be read offline: Settings > About
Lumi, ``lumi terms show`` and the installers' license pages.

**Nothing sends a model request until the terms in force are accepted**: the EULA's current version and,
when this build is a pre-release (its version carries a pre-release label, such as ``0.21.0-beta.1`` or
``0.20.0.dev0``), the Alpha and Beta Test Terms' current version. The privacy notice is read, not accepted.

Where it's checked:

* organization oversight's gate (lumi/oversight.py), which every turn path already asks:
  ``oversight.admit`` before each turn and each model request (``Session.run``), and ``oversight.refusal``
  or ``oversight.gate`` at the entry points outside a turn (the app's message box, plans, missions,
  autonomous sessions, Team, model comparisons, evaluations, dictation, Engram, and requests outside a
  turn such as titles). Both ask here first, and a refusal carries ``REFUSAL_CODE`` so the app shows the
  terms, not the notice;
* underneath every model request (lumi/dlp.py): ``dlp.check_request`` and ``dlp.guarded``, which wraps
  every backend's request methods, refuse while the terms wait, even for a request under ``dlp.permit``
  such as a warm-up; so do the requests Lumi makes over plain HTTP (Ollama's warm-up and tool probe) and
  ``request_purpose.auxiliary_stream``. tests/test_dlp.py lists every use with its gate.

Who is asked, and where (each computer user for themselves, unless the machine policy accepted):

* the desktop app shows a dialog at first launch and when a version changes; its Accept counts only on a
  trusted click or key press, and every open window unlocks;
* the terminal UI asks for a typed yes before it does anything else with a model, warm-ups included;
* ``lumi run`` at an interactive terminal asks for a typed yes; without one it needs ``--accept-terms
  <value>`` or ``LUMI_ACCEPT_TERMS`` naming the versions in force, and otherwise exits 2 with the value;
* ``lumi gateway`` asks before it starts (typed yes, the flag or the variable);
* ``lumi terms accept <value>`` accepts from any command line;
* nobody is asked by work that runs with no one there: scheduled tasks, tasks from Slack and Teams (not
  even taken from Lumi Cloud until then), and the app's background requests wait.

Acceptance counts when one of these holds:

* this computer user accepted each document's current version, and the text Lumi ships for it is the text
  they accepted: each acceptance is recorded in ``~/.lumi/legal/acceptance.json`` per computer user (the
  document, its version, the SHA-256 of the text, when, how, and which Lumi), and a record whose SHA-256 no
  longer matches the shipped text counts as pending. A new version asks again; so does a changed text,
  which tests/test_legal_texts.py makes come with a new version (each version's hash is pinned in
  ``terms.json``). A new published date alone doesn't;
* the machine policy accepts for the organization's people (``legal.accepted_by_organization``,
  ``policy.terms_accepted_by``): only from a source only an administrator can write (the HKLM Group Policy
  key or the file its ``PolicyFile`` names, a configuration profile, the machine policy file where only
  administrators can change it), never ``LUMI_POLICY_FILE``, a Lumi Cloud policy, Settings or a project;
* outside the desktop app, ``LUMI_ACCEPT_TERMS`` names each document's current version (``eula-1.0``, or
  ``eula-1.0,alpha-terms-1.0`` for a pre-release), for CI and containers. ``lumi run`` records it. The app
  itself (``mark_app_process``) always asks its person, unless the machine policy accepted.

Each version takes effect for a person on the day they accept it; ``published`` in ``terms.json`` is the
day its text was written, which a release build refuses to have after its own date (packaging/legal_texts.py).

Standard library only at import: ``lumi terms`` and the gate stay cheap.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import sys
import threading
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

ENVIRONMENT = "LUMI_ACCEPT_TERMS"
# The error code of a turn or a request refused until the terms are accepted (Session.run, the app).
REFUSAL_CODE = "terms_not_accepted"
RECORD_KIND = "lumi.terms-acceptance/v1"
# The documents a person accepts, in the order they're shown. The privacy notice is a notice: it's read, not accepted.
ACCEPTED_DOCUMENTS = ("eula", "alpha_terms")
READABLE_DOCUMENTS = ("eula", "alpha_terms", "privacy")
# How documents are named on the command line and in acceptance values (``eula-1.0,alpha-terms-1.0``).
NAMES = {"eula": "eula", "alpha_terms": "alpha-terms", "privacy": "privacy"}
# How an acceptance was given: the app's dialog, a typed yes at a terminal, ``lumi terms accept``,
# ``lumi run --accept-terms`` or ``LUMI_ACCEPT_TERMS`` (lumi run, lumi gateway).
SURFACES = ("app", "terminal", "command", "flag", "environment")
HISTORY_LIMIT = 200
_MONTHS = ("January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
           "November", "December")
# A pre-release: a SemVer label (0.21.0-beta.1) or a PEP 440 one (0.20.0a1, 0.20.0rc2, 0.19.2.dev11).
# packaging/legal_texts.py keeps the same rule for the installers; tests/test_legal_texts.py checks they agree.
_PRERELEASE = re.compile(r"-|(?<=[\d.])(?:a|b|c|rc|alpha|beta|pre|preview|dev)\d*$", re.IGNORECASE)
# The comment packaging/legal_texts.py puts at the top of each text it writes.
_GENERATED = re.compile(r"\A\s*<!--.*?-->\s*", re.DOTALL)

_lock = threading.RLock()
_facts: tuple[Any, dict] | None = None
_record: tuple[Any, dict] | None = None
# SHA-256 of each shipped text, by file and its (mtime, size): the gate compares records with it.
_hashes: dict[str, tuple[Any, str]] = {}
# Tests only: True treats the terms as accepted in this process (tests/conftest.py); None checks.
_assumed: bool | None = None
# The desktop app: LUMI_ACCEPT_TERMS doesn't apply there (mark_app_process).
_app_process = False


class TermsError(ValueError):
    """An acceptance that can't be recorded; the message says why and what to accept instead."""


def legal_dir() -> Path:
    """``lumi/legal``: beside this module in a source tree, a wheel and PyInstaller's bundle alike."""
    return Path(__file__).resolve().with_name("legal")


def facts() -> dict:
    """``lumi/legal/terms.json``, read again when it changes."""
    global _facts
    path = legal_dir() / "terms.json"
    stat = path.stat()
    key = (stat.st_mtime_ns, stat.st_size)
    with _lock:
        if _facts is not None and _facts[0] == key:
            return _facts[1]
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or not isinstance(data.get("documents"), dict):
            raise ValueError("lumi/legal/terms.json lists no documents")
        _facts = (key, data)
        return data


def long_date(iso: str) -> str:
    """``2026-09-29`` as "September 29, 2026", whatever the computer's language."""
    try:
        value = date.fromisoformat(str(iso))
    except ValueError:
        return str(iso)
    return f"{_MONTHS[value.month - 1]} {value.day}, {value.year}"


@dataclass(frozen=True)
class Document:
    """One of Lumi's legal texts, as ``terms.json`` describes it.

    ``published`` is the day this version's text was written. A version takes effect for a person on the
    day they accept it, never before, so no text names an effective date after that day.
    """

    id: str
    title: str
    file: str
    version: str
    published: str

    @property
    def token(self) -> str:
        """How an acceptance value names this version: ``eula-1.0``."""
        return f"{NAMES.get(self.id, self.id)}-{self.version}"

    @property
    def described(self) -> str:
        return f"the {self.title} (version {self.version})"

    def as_dict(self) -> dict:
        return {"id": self.id, "title": self.title, "version": self.version, "published": self.published,
                "published_text": long_date(self.published), "token": self.token,
                "name": NAMES.get(self.id, self.id)}


def document(doc_id: str) -> Document:
    entry = facts()["documents"].get(doc_id)
    if not isinstance(entry, dict):
        raise KeyError(f"No legal document {doc_id!r}")
    return Document(doc_id, str(entry.get("title") or doc_id), str(entry.get("file") or ""),
                    str(entry.get("version") or ""), str(entry.get("published") or ""))


def document_id(name: str) -> str:
    """A document's id from how people type it (``alpha-terms``, ``alpha_terms``, ``EULA``); KeyError otherwise."""
    wanted = str(name or "").strip().lower().replace("_", "-")
    for doc_id, typed in NAMES.items():
        if wanted == typed:
            return doc_id
    raise KeyError(f"No legal document {name!r}; choose {', '.join(NAMES.values())}.")


def is_prerelease(version: str) -> bool:
    """Whether a Lumi version is a pre-release build (alpha, beta, release candidate or development build)."""
    return bool(_PRERELEASE.search(str(version or "").strip()))


def this_version() -> str:
    from . import __version__

    return __version__


def required(version: str | None = None) -> list[Document]:
    """The terms in force for this build: the EULA, and for a pre-release the Alpha and Beta Test Terms."""
    documents = [document("eula")]
    if is_prerelease(version or this_version()):
        documents.append(document("alpha_terms"))
    return documents


def acceptance_value(documents: list[Document] | None = None) -> str:
    """What ``--accept-terms``, ``LUMI_ACCEPT_TERMS`` and ``lumi terms accept`` take: ``eula-1.0,alpha-terms-1.0``."""
    return ",".join(doc.token for doc in (required() if documents is None else documents))


def parse_value(value: str) -> dict[str, str]:
    """``{"eula": "1.0", "alpha_terms": "1.0"}`` from ``eula-1.0,alpha-terms-1.0``; TermsError otherwise."""
    found: dict[str, str] = {}
    by_name = sorted(((name, doc_id) for doc_id, name in NAMES.items() if doc_id in ACCEPTED_DOCUMENTS),
                     key=lambda item: len(item[0]), reverse=True)
    for part in re.split(r"[,\s]+", str(value or "").strip()):
        if not part:
            continue
        for name, doc_id in by_name:
            if part.lower().startswith(name + "-") and len(part) > len(name) + 1:
                found[doc_id] = part[len(name) + 1:]
                break
        else:
            raise TermsError(f"{part[:60]!r} doesn't name a document and its version, such as eula-1.0.")
    if not found:
        raise TermsError("It names no document; name each one with its version, such as eula-1.0.")
    return found


# ── Who accepted ─────────────────────────────────────────────────────────────


def record_path() -> Path:
    from .paths import state_home

    return state_home() / "legal" / "acceptance.json"


def _os_user() -> str:
    from .oversight import os_user

    return os_user()


def _read_record() -> dict:
    """The acceptance record, read again when it changes; {} when there's none (or it isn't one)."""
    global _record
    path = record_path()
    try:
        stat = path.stat()
    except OSError:
        return {}
    key = (str(path), stat.st_mtime_ns, stat.st_size)
    with _lock:
        if _record is not None and _record[0] == key:
            return _record[1]
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError):
        data = {}
    if not isinstance(data, dict) or data.get("kind") != RECORD_KIND:
        data = {}
    with _lock:
        _record = (key, data)
    return data


def accepted(person: str | None = None) -> dict[str, dict]:
    """This computer user's acceptances by document: ``{"eula": {"version": "1.0", "accepted_at": ..., ...}}``."""
    people = _read_record().get("people")
    entry = people.get(person if person is not None else _os_user()) if isinstance(people, dict) else None
    if not isinstance(entry, dict):
        return {}
    return {doc_id: dict(value) for doc_id, value in entry.items() if isinstance(value, dict)}


def organization_acceptance() -> tuple[str, str]:
    """(organization, where its machine policy is) when a machine policy accepts the terms for this computer."""
    from . import policy

    return policy.terms_accepted_by()


def _environment_versions() -> dict[str, str]:
    """What ``LUMI_ACCEPT_TERMS`` accepts in this process: nothing in the desktop app, or when it can't be read."""
    if _app_process:
        return {}
    value = os.environ.get(ENVIRONMENT, "")
    if not value.strip():
        return {}
    try:
        return parse_value(value)
    except TermsError:
        return {}


def is_current(entry: Any, doc: Document) -> bool:
    """Whether an acceptance record's entry accepted ``doc`` as Lumi ships it now: its version, and the
    SHA-256 of its text. An entry for another text of the same version (a text changed without a new
    version, or a copy of Lumi whose texts were altered) counts as pending."""
    if not isinstance(entry, dict) or str(entry.get("version") or "") != doc.version:
        return False
    return str(entry.get("sha256") or "") == text_sha256(doc.id)


def pending(version: str | None = None, *, use_environment: bool = True) -> list[Document]:
    """The terms this person must still accept before anything reaches a model ([] once nothing is pending).

    ``use_environment=False`` leaves ``LUMI_ACCEPT_TERMS`` out: what's accepted on record (or by the
    organization), so ``lumi run`` knows whether to record what the variable accepts.
    """
    if _assumed is True:
        return []
    documents = required(version)
    if organization_acceptance()[0]:
        return []
    mine = accepted()
    environment = _environment_versions() if use_environment else {}
    return [doc for doc in documents if not is_current(mine.get(doc.id), doc) and environment.get(doc.id) != doc.version]


def _names(documents: list[Document]) -> str:
    described = [doc.described for doc in documents]
    return described[0] if len(described) == 1 else ", ".join(described[:-1]) + " and " + described[-1]


def refusal(place: str = "app", version: str | None = None) -> str:
    """Why nothing may reach a model until the terms are accepted; '' once they are. Never raises.

    ``place`` says how the person can accept, in the words of the surface that refused: ``app`` (the app's
    dialog), ``terminal`` (lumi run or the terminal UI with someone there), ``headless`` (a run nobody can
    answer: CI, a schedule), ``gateway`` (a chat of the chat gateway), ``chat_task`` (a task from chat) or
    ``request`` (a model request refused underneath any surface: lumi/dlp.py).
    A check that fails refuses: the terms decide whether Lumi may be used at all.
    """
    try:
        waiting = pending(version)
        if not waiting:
            return ""
        names, value = _names(waiting), acceptance_value(required(version))
    except Exception as exc:
        logger.exception("Lumi couldn't check whether its terms were accepted")
        return (f"Lumi couldn't read its terms ({exc}), so nothing is sent to a model. Reinstall Lumi, or ask "
                "your administrator.")
    if place == "terminal":
        return (f"Lumi won't send anything to a model until you accept its terms: {names}. Accept them in the "
                f"Lumi app, type yes when lumi run or the terminal UI shows them, or run "
                f"`lumi terms accept {value}` after reading them (`lumi terms show`).")
    if place == "headless":
        return (f"Lumi's terms haven't been accepted on this computer: {names}. Read them with `lumi terms show` "
                f"(or in the Lumi app), then accept them with --accept-terms {value} or {ENVIRONMENT}={value}.")
    if place == "gateway":
        return (f"Lumi on this computer won't run chat requests until its terms are accepted: {names}. Accept "
                f"them in the Lumi app, or with `lumi terms accept {value}` where the gateway runs.")
    if place == "chat_task":
        return f"Lumi on this computer won't run requests until its terms are accepted in the Lumi app: {names}."
    if place == "request":
        return (f"Lumi won't send anything to a model until its terms are accepted: {names}. Accept them in the "
                f"Lumi app, or with `lumi terms accept {value}` after reading them (`lumi terms show`).")
    return f"Lumi won't send anything to a model until you accept its terms: {names}. Choose Review terms above the message box."


def request_refusal() -> str:
    """Why a model request can't be sent now because the terms wait, or ''; for the checks underneath every
    request (lumi/dlp.py, the backends' own HTTP requests). Never raises; cheap once accepted."""
    if _assumed is True:
        return ""
    return refusal("request")


# ── Accepting ────────────────────────────────────────────────────────────────


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def text(doc_id: str) -> str:
    """A document's text as Lumi ships it (Markdown, ``\\n`` line endings, without the generated-file comment)."""
    doc = document(doc_id)
    raw = (legal_dir() / doc.file).read_text(encoding="utf-8").replace("\r\n", "\n")
    return _GENERATED.sub("", raw, count=1)


def text_sha256(doc_id: str) -> str:
    """The SHA-256 of ``text(doc_id)``: what an acceptance records and the gate compares, and what
    ``terms.json`` pins for each version (tests/test_legal_texts.py). Read again when the file changes."""
    path = legal_dir() / document(doc_id).file
    stat = path.stat()
    key = (str(path), stat.st_mtime_ns, stat.st_size)
    with _lock:
        cached = _hashes.get(doc_id)
        if cached is not None and cached[0] == key:
            return cached[1]
    digest = hashlib.sha256(text(doc_id).encode("utf-8")).hexdigest()
    with _lock:
        _hashes[doc_id] = (key, digest)
    return digest


def accept(versions: dict[str, str], surface: str, *, version: str | None = None) -> bool:
    """Record that this computer user accepted ``versions`` (``{document: version}``); True once nothing is pending.

    Only the terms in force can be accepted. A version that isn't a document's current one (a page or a
    command still showing older terms), or leaving out a document this person hasn't accepted yet (or
    accepted in another text), records nothing and returns False. A document this build doesn't need (the
    Alpha and Beta Test Terms on a stable build) isn't recorded.
    """
    if surface not in SURFACES:
        raise ValueError(f"Unknown surface {surface!r}")
    from . import audit
    from .file_lock import exclusive

    documents = required(version)
    named = {str(doc_id): str(value) for doc_id, value in (versions or {}).items()}
    user = _os_user()
    mine = accepted(user)
    if any(doc.id in named and named[doc.id] != doc.version for doc in documents):
        return False
    missing = [doc for doc in documents if not is_current(mine.get(doc.id), doc)]
    if any(doc.id not in named for doc in missing):
        return False
    chosen = [doc for doc in documents if named.get(doc.id) == doc.version]
    if not chosen:
        return not missing
    from . import __version__

    at = _now()
    entries = {doc.id: {"version": doc.version, "sha256": text_sha256(doc.id), "accepted_at": at,
                        "surface": surface, "lumi_version": __version__} for doc in chosen}
    path = record_path()
    with _lock, exclusive(path.with_name(".lock")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, RecursionError):
            data = {}
        if not isinstance(data, dict) or data.get("kind") != RECORD_KIND:
            data = {"kind": RECORD_KIND}
        people = data.get("people") if isinstance(data.get("people"), dict) else {}
        person = people.get(user) if isinstance(people.get(user), dict) else {}
        people[user] = {**person, **entries}
        history = data.get("history") if isinstance(data.get("history"), list) else []
        history.extend({"os_user": user, "document": doc_id, **entry} for doc_id, entry in entries.items())
        data.update(people=people, history=history[-HISTORY_LIMIT:])
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(data, indent=1, sort_keys=True), encoding="utf-8")
        os.replace(temporary, path)
    audit.record("terms.accepted", surface=surface, os_user=user, lumi_version=__version__,
                 documents={doc_id: entry["version"] for doc_id, entry in entries.items()},
                 sha256={doc_id: entry["sha256"] for doc_id, entry in entries.items()})
    return True


def accept_value(value: str, surface: str, *, version: str | None = None) -> str:
    """Accept what an acceptance value names (``lumi terms accept``, ``--accept-terms``, ``LUMI_ACCEPT_TERMS``).

    '' once nothing is pending; otherwise why not, with the value that would accept the terms in force.
    """
    documents = required(version)
    wanted = acceptance_value(documents)
    try:
        named = parse_value(value)
    except TermsError as exc:
        raise TermsError(f"{exc} Lumi's terms in force are {wanted}.") from None
    stale = [f"{NAMES[doc.id]}-{named[doc.id]}" for doc in documents if doc.id in named and named[doc.id] != doc.version]
    if stale:
        raise TermsError(f"{', '.join(stale)} isn't the version in force. Read the current terms (`lumi terms show`) "
                         f"and accept them with {wanted}.")
    if not accept(named, surface, version=version):
        left = [doc.token for doc in documents if doc.id not in named]
        raise TermsError(f"It doesn't name {', '.join(left)}. Read the terms (`lumi terms show`) and accept them "
                         f"with {wanted}.")
    return ""


# ── What the app and the command line show ─────────────────────────────────


def status(version: str | None = None) -> dict:
    """The terms in force, whether this person accepted them, and whether an organization did. Never raises.

    ``readable`` lists the texts this build shows (About Lumi): the EULA, the Alpha and Beta Test Terms
    only on a pre-release build, and the privacy notice. A required document's ``previous_version`` is
    what this person accepted before, and ``changed`` says its text changed since they accepted it.
    """
    try:
        documents = required(version)
        prerelease = is_prerelease(version or this_version())
        mine = accepted()
        organization, source = organization_acceptance()
        waiting = pending(version)
        environment = _environment_versions()
        listed = []
        for doc in documents:
            entry = mine.get(doc.id) or {}
            current = is_current(entry, doc)
            listed.append({**doc.as_dict(), "accepted": current or environment.get(doc.id) == doc.version,
                           "accepted_at": str(entry.get("accepted_at") or "") if current else "",
                           "accepted_via": str(entry.get("surface") or "") if current else
                           ("environment" if environment.get(doc.id) == doc.version else ""),
                           "previous_version": "" if current else str(entry.get("version") or ""),
                           "changed": bool(entry) and not current})
        readable = [doc_id for doc_id in READABLE_DOCUMENTS if doc_id != "alpha_terms" or prerelease]
        return {
            "pending": bool(waiting),
            "pending_documents": [doc.id for doc in waiting],
            "prerelease": prerelease,
            "lumi_version": version or this_version(),
            "required": listed,
            "readable": readable,
            "documents": {doc_id: document(doc_id).as_dict() for doc_id in READABLE_DOCUMENTS},
            "organization": organization,
            "organization_source": source,
            "acceptance_value": acceptance_value(documents),
            "environment": ENVIRONMENT,
            "refusal": refusal("app", version) if waiting else "",
        }
    except Exception as exc:
        logger.exception("Lumi couldn't read its terms")
        return {"pending": True, "pending_documents": [], "required": [], "documents": {}, "organization": "",
                "error": f"Lumi couldn't read its terms ({exc}), so nothing is sent to a model. Reinstall Lumi, "
                         "or ask your administrator."}


def mark_app_process() -> None:
    """The desktop app asks its person, or relies on the machine policy: ``LUMI_ACCEPT_TERMS`` doesn't apply."""
    global _app_process
    _app_process = True


def set_for_tests(accepted_all: bool | None) -> None:
    """Tests: True treats the terms as accepted in this process; None checks them as Lumi does."""
    global _assumed, _record, _app_process
    with _lock:
        _assumed = accepted_all
        _record = None
        _app_process = False
        _hashes.clear()


# ── lumi terms ──────────────────────────────────────────────────────────────


def terminal_summary(documents: list[Document]) -> list[str]:
    """What a terminal shows before asking for a typed yes: each document, and where to read it."""
    lines = [f"  {doc.title}, version {doc.version} (published {long_date(doc.published)}; it applies from the "
             "day you accept it)" for doc in documents]
    shown = " ".join(f"`lumi terms show {NAMES[doc.id]}`" for doc in documents)
    lines.append(f"  Read them with {shown}, in the Lumi app (Settings > About Lumi), or in {legal_dir()}.")
    return lines


def main(argv: list[str] | None = None) -> int:
    """``lumi terms``: the terms in force and whether they're accepted here, as JSON.

    ``lumi terms show [eula|alpha-terms|privacy]`` prints a text (by default the terms in force), and
    ``lumi terms accept <value>`` accepts them for this computer user, as ``lumi run --accept-terms`` does.
    """
    args = list(sys.argv[2:] if argv is None else argv)
    command = args[0] if args else "status"
    if command == "status" and len(args) <= 1:
        info = status()
        print(json.dumps({key: info.get(key) for key in (
            "lumi_version", "prerelease", "pending", "pending_documents", "required", "organization",
            "organization_source", "acceptance_value", "error") if key in info}, indent=2))
        return 0
    if command == "show" and len(args) <= 2:
        try:
            ids = [document_id(args[1])] if len(args) == 2 else [doc.id for doc in required()]
            print("\n\n".join(text(doc_id).rstrip() for doc_id in ids))
        except (KeyError, OSError, ValueError) as exc:
            print(str(exc).strip("'\""), file=sys.stderr)
            return 1
        return 0
    if command == "accept" and len(args) == 2:
        try:
            accept_value(args[1], "command")
        except (TermsError, OSError, ValueError) as exc:
            print(f"lumi terms: {exc}", file=sys.stderr)
            return 1
        names = _names(required())
        print(f"Accepted {names} for {_os_user() or 'this computer user'}.")
        return 0
    print("usage: lumi terms [status] | lumi terms show [eula|alpha-terms|privacy] | lumi terms accept <value>",
          file=sys.stderr)
    return 2
