"""Data loss prevention (DLP): an organization's rules for content sent to model providers.

An organization policy's ``dlp`` section (lumi/policy.py) enables built-in
detectors and adds custom rules. Every outgoing model request passes
``check_request`` before its backend sees it: turns (``Session._model_stream``),
auxiliary requests (``engine/request_purpose.auxiliary_stream``: titles,
compaction, image descriptions, skill extraction), and ``check_text`` for
planning classification (``Session.should_plan``), structured-output repair,
SONN employee advice and ``[vision]`` acceptance checks. The app, ``lumi run``,
the chat gateway, the terminal UI and Team workers all send through those.

Backends' request methods are ``guarded``: while an organization's policy
applies, they refuse a call that doesn't come through ``send`` (a checked
request) or ``permit`` (fixed text, such as a warm-up), so a new path that
skips the check fails closed instead of sending. tests/test_dlp.py also lists
every call that sends to a model.

The check runs on the request as it will be sent (history, the new message,
tool results, instructions), after ``secret_scan``, on a normalized copy of
each text (``dlp_detectors.normalized``: Unicode spaces, dashes and digits in
their ASCII form, invisible characters removed). Each rule has an action:

* ``flag``: send, and record;
* ``redact``: replace each match with ``[REDACTED:<rule>]`` in the copy that is
  sent. The conversation kept on this computer keeps the original. Tool call
  arguments are JSON: only their keys and values change, and arguments with a
  duplicated key go out as the JSON the tool received (each key's last
  value). A model's signed reasoning can't be edited, so reasoning with a match
  is left out instead;
* ``block``: refuse the request with a message that names the rule and where
  the content was, never the content. The conversation entry that held it is
  marked (``dlp_withheld``), and later requests send a notice in its place, so
  one blocked paste doesn't end the conversation.

A rule's ``scope`` limits it to kinds of content (``KINDS``). Content made of
several kinds (a compaction summary, an auxiliary request's transcript, a
message Lumi wrote, which can quote tool output) is ``mixed``, and every rule
checks it.

Every flag, redaction and block is recorded in the audit log (``dlp.finding``:
rule, action, content kind, count; never the matched text). Content already
recorded for a session, provider and model isn't recorded again when later
requests send it again.

An optional external service (``dlp.service``) gets the text after the
built-in redactions and answers allow, redact or block. Its verdicts are
remembered for each text; while a service is configured, an entry that was
blocked stays out of later requests and isn't sent to the service again.

A ``dlp`` section Lumi can't use refuses every model request
(``policy.blocked_reason``) until it's fixed; the rest of the policy still
applies. See docs/dlp.md.

What Lumi sends its organization's Lumi Cloud about a turn (organization
oversight, lumi/oversight.py and security_flags.py: messages, replies,
titles, tool arguments, flag excerpts) passes ``shareable`` first, whole,
before anything is cut from it, so it never carries text these rules or the
service redacted or withheld.
"""

from __future__ import annotations

import contextvars
import functools
import hashlib
import json
import logging
import re
import threading
import urllib.parse
from collections import Counter, OrderedDict
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, NoReturn

from . import dlp_detectors
from .dlp_detectors import Finder, PatternError

logger = logging.getLogger(__name__)

VERSION = 1
ACTIONS = ("flag", "redact", "block")
# What a request carries: what the person typed, what they attached (@file,
# @diff, @issue and other context attachments, and their images' text), tool
# results and file contents, system/project instructions and notes, and the
# model's own earlier replies and tool calls.
KINDS = ("prompt", "attachment", "tool_result", "instructions", "model_output")
MIXED = "mixed"
DETECTORS = tuple(dlp_detectors.DETECTORS)

MAX_RULES = 100
MAX_KEYWORDS = 500
MAX_KEYWORD_CHARS = 100
MAX_TOTAL_KEYWORD_CHARS = 100_000
MAX_PATTERN_CHARS = 500
# A request larger than this isn't checked, and so isn't sent.
MAX_REQUEST_CHARS = 16_000_000
MAX_SERVICE_CHARS = 4_000_000

KIND_LABELS = {
    "prompt": "your message",
    "attachment": "an attachment",
    "tool_result": "a tool result",
    "instructions": "the instructions",
    "model_output": "an earlier reply",
    MIXED: "the conversation",
}

_RULE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9 ._-]{0,63}")
_SERVICE_RULE = "dlp-service"


class DlpError(ValueError):
    """A dlp section Lumi can't apply; the message says why."""


class Blocked(Exception):
    """A model request that must not be sent. ``message`` never contains matched text.

    ``entries`` are the indices of the request's conversation history entries
    that held blocked content (see ``mark_withheld``).
    """

    def __init__(self, message: str, *, code: str = "dlp_blocked", entries: tuple[int, ...] = ()):
        super().__init__(message)
        self.message = message
        self.code = code
        self.entries = tuple(entries)


# ── The policy section ─────────────────────────────────────────────────────


@dataclass(frozen=True)
class Rule:
    name: str
    action: str
    kinds: frozenset  # the kinds of content it checks; empty means every kind
    source: str       # "detector", "keywords" or "pattern"
    find: Finder = field(compare=False, repr=False)

    def applies_to(self, kind: str) -> bool:
        return kind == MIXED or not self.kinds or kind in self.kinds


@dataclass(frozen=True)
class Service:
    url: str
    timeout: float
    on_error: str


@dataclass(frozen=True)
class DlpPolicy:
    rules: tuple
    service: Service | None
    fingerprint: str  # identifies these rules in caches

    def summary(self) -> dict:
        """What Settings shows: rule names, actions and scope, never keywords or patterns."""
        return {
            "rules": [{"name": rule.name, "action": rule.action, "type": rule.source,
                       "scope": sorted(rule.kinds) if rule.kinds else []} for rule in self.rules],
            "service": urllib.parse.urlsplit(self.service.url).hostname if self.service else None,
            "service_on_error": self.service.on_error if self.service else None,
        }


def _only(value: dict, allowed: set, where: str) -> None:
    unknown = sorted(str(key) for key in value if key not in allowed)
    if unknown:
        raise DlpError(f"{where} has unknown keys: {', '.join(unknown)} (allowed: {', '.join(sorted(allowed))}).")


def _action(value: Any, where: str) -> str:
    if value not in ACTIONS:
        raise DlpError(f"{where}.action must be one of {', '.join(ACTIONS)}.")
    return value


def _scope(value: Any, where: str) -> frozenset:
    if value is None:
        return frozenset()
    if (not isinstance(value, list) or not value
            or not all(isinstance(kind, str) and kind in KINDS for kind in value)):
        raise DlpError(f"{where}.scope must list some of {', '.join(KINDS)}.")
    return frozenset(value)


def _flag(value: Any, where: str, default: bool) -> bool:
    if value is None:
        return default
    if not isinstance(value, bool):
        raise DlpError(f"{where} must be true or false.")
    return value


def _service(value: Any) -> Service | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise DlpError("dlp.service must be an object with a url.")
    _only(value, {"url", "timeout_seconds", "on_error"}, "dlp.service")
    url = value.get("url")
    parts = urllib.parse.urlsplit(url.strip()) if isinstance(url, str) else None
    local = parts is not None and parts.hostname in ("localhost", "127.0.0.1", "::1")
    if (parts is None or not parts.hostname or parts.username or parts.password
            or not (parts.scheme == "https" or (parts.scheme == "http" and local))):
        raise DlpError("dlp.service.url must be an https URL without a user name or password "
                       "(http only for localhost).")
    timeout = value.get("timeout_seconds", 5)
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 0.5 <= timeout <= 60:
        raise DlpError("dlp.service.timeout_seconds must be a number from 0.5 to 60.")
    on_error = value.get("on_error", "block")
    if on_error not in ("block", "allow"):
        raise DlpError("dlp.service.on_error must be \"block\" or \"allow\".")
    return Service(url.strip(), float(timeout), on_error)


def parse_section(value: Any) -> DlpPolicy:
    """Validate a policy's ``dlp`` section and build its rules; DlpError says what's wrong."""
    if not isinstance(value, dict):
        raise DlpError("dlp must be an object.")
    _only(value, {"version", "detectors", "rules", "service"}, "dlp")
    version = value.get("version")
    if isinstance(version, bool) or version != VERSION:
        raise DlpError(f"dlp.version must be {VERSION}; this version of Lumi can't apply version {version!r}.")
    rules: list[Rule] = []
    detectors = value.get("detectors")
    if detectors is not None:
        if not isinstance(detectors, dict):
            raise DlpError("dlp.detectors must be an object naming detectors.")
        for name, setting in detectors.items():
            if name not in dlp_detectors.DETECTORS:
                raise DlpError(f"dlp.detectors: unknown detector {name!r} (known: {', '.join(DETECTORS)}).")
            where = f"dlp.detectors.{name}"
            if isinstance(setting, str):
                setting = {"action": setting}
            if not isinstance(setting, dict):
                raise DlpError(f"{where} must be an action or an object with an action.")
            _only(setting, {"action", "scope"}, where)
            rules.append(Rule(name, _action(setting.get("action"), where), _scope(setting.get("scope"), where),
                              "detector", dlp_detectors.DETECTORS[name]))
    custom = value.get("rules")
    if custom is not None:
        if not isinstance(custom, list) or len(custom) > MAX_RULES:
            raise DlpError(f"dlp.rules must be a list of up to {MAX_RULES} rules.")
        keyword_chars = 0
        for index, rule in enumerate(custom):
            where = f"dlp.rules[{index}]"
            if not isinstance(rule, dict):
                raise DlpError(f"{where} must be an object.")
            _only(rule, {"name", "keywords", "pattern", "action", "scope", "case_sensitive", "whole_word"}, where)
            name = rule.get("name")
            if not isinstance(name, str) or not _RULE_NAME.fullmatch(name):
                raise DlpError(f"{where}.name must be 1 to 64 letters, digits, spaces, dots, underscores or "
                               "hyphens, starting with a letter or digit.")
            where = f"dlp.rules[{index}] ({name})"
            action = _action(rule.get("action"), where)
            kinds = _scope(rule.get("scope"), where)
            case_sensitive = _flag(rule.get("case_sensitive"), f"{where}.case_sensitive", False)
            if ("keywords" in rule) == ("pattern" in rule):
                raise DlpError(f"{where} needs either keywords or a pattern.")
            if "keywords" in rule:
                words = rule["keywords"]
                if (not isinstance(words, list) or not 1 <= len(words) <= MAX_KEYWORDS
                        or not all(isinstance(word, str) and word.strip() and len(word) <= MAX_KEYWORD_CHARS
                                   for word in words)):
                    raise DlpError(f"{where}.keywords must list 1 to {MAX_KEYWORDS} words or phrases of up to "
                                   f"{MAX_KEYWORD_CHARS} characters.")
                # Compared the way text is: normalized, each run of whitespace one space.
                words = [dlp_detectors.keyword_text(word) for word in words]
                if not all(words):
                    raise DlpError(f"{where}.keywords has an entry with no visible characters.")
                keyword_chars += sum(map(len, words))
                if keyword_chars > MAX_TOTAL_KEYWORD_CHARS:
                    raise DlpError(f"dlp.rules hold more than {MAX_TOTAL_KEYWORD_CHARS:,} characters of keywords.")
                whole_word = _flag(rule.get("whole_word"), f"{where}.whole_word", True)
                compiled = dlp_detectors.keyword_pattern(words, case_sensitive=case_sensitive, whole_word=whole_word)
                rules.append(Rule(name, action, kinds, "keywords", _keyword_finder(compiled)))
            else:
                if "whole_word" in rule:
                    raise DlpError(f"{where}.whole_word applies only to keywords.")
                pattern = rule["pattern"]
                if not isinstance(pattern, str) or not pattern or len(pattern) > MAX_PATTERN_CHARS:
                    raise DlpError(f"{where}.pattern must be a regular expression of up to "
                                   f"{MAX_PATTERN_CHARS} characters.")
                try:
                    finder = dlp_detectors.pattern_finder(pattern, case_sensitive=case_sensitive)
                except PatternError as exc:
                    raise DlpError(f"{where}.pattern {exc}.") from exc
                rules.append(Rule(name, action, kinds, "pattern", finder))
    seen: set[str] = set()
    for rule in rules:
        if rule.name.casefold() in seen:
            raise DlpError(f"dlp: two rules are named {rule.name!r}; names must be unique.")
        seen.add(rule.name.casefold())
    fingerprint = hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)
                                 .encode("utf-8")).hexdigest()[:16]
    return DlpPolicy(tuple(rules), _service(value.get("service")), fingerprint)


def _keyword_finder(compiled: re.Pattern[str]) -> Finder:
    def find(text: str) -> Iterator[tuple[int, int]]:
        for match in compiled.finditer(text):
            yield match.span()
    return find


# ── What a request sends ───────────────────────────────────────────────────

_REASONING_FIELDS = ("reasoning_content", "thinking", "reasoning")
_PART_KEYS = ("text", "description", "caption", "alt_text", "transcript", "extracted_text")
_DETAIL_KEYS = ("thinking", "text", "summary")
_PROMPT_PURPOSES = frozenset({"primary", "title", "planning"})
# Checks of text that isn't a model request (a feedback report the person sends): Lumi's terms don't
# hold them back in check_request. Every other purpose is a model request (or Engram's memory server).
NOT_MODEL_REQUESTS = frozenset({"feedback"})
WITHHELD = "dlp_withheld"


@dataclass
class _Item:
    kind: str
    text: str
    path: tuple            # where the text is, from the request's top
    entry: int = -1        # its conversation_history index
    json_path: tuple | None = None  # inside the JSON string at ``path`` (tool arguments)
    json_key: bool = False  # the text is that JSON object key, not its value
    droppable: bool = False  # reasoning: left out rather than edited


def _history_kind(entry: dict) -> str | None:
    role = entry.get("role")
    if role == "tool_catalog":
        return None  # tool definitions, not content
    if role == "user":
        # A message Lumi wrote (a hook's context, a nudge, a recovery prompt)
        # can quote tool arguments and results: every rule checks it.
        return MIXED if entry.get("input_origin") == "generated" else "prompt"
    if role == "tool_result":
        return "tool_result"
    if role == "system":
        return "instructions"
    if role in ("assistant", "tool_call"):
        content = entry.get("content")
        if entry.get("preserved_context") is not None or (
                isinstance(content, str) and content.startswith("[Previous conversation summary]")):
            return MIXED  # a compaction summary quotes requirements and tool evidence
        return "model_output"
    return MIXED


def _content_items(items: list, content: Any, path: tuple, kind: str, attachment_kind: str, entry: int) -> None:
    if isinstance(content, str):
        if content:
            items.append(_Item(kind, content, path, entry))
        return
    if not isinstance(content, list):
        return
    for index, part in enumerate(content):
        if isinstance(part, str):
            if part:
                items.append(_Item(kind, part, path + (index,), entry))
        elif isinstance(part, dict):
            part_kind = kind if str(part.get("type") or "text") == "text" else attachment_kind
            for key in _PART_KEYS:
                if isinstance(part.get(key), str) and part[key]:
                    items.append(_Item(part_kind, part[key], path + (index, key), entry))


# Parsed tool arguments by their JSON text, so a long conversation's calls
# aren't parsed again for every request. Bounded by entries and characters.
_json_lock = threading.Lock()
_json_cache: OrderedDict[str, tuple] = OrderedDict()
_json_chars = 0
_INVALID = object()


def _parsed_json(text: str) -> tuple[Any, bool]:
    """Tool arguments parsed (``_INVALID`` if they aren't JSON), and whether parsing
    dropped anything: a duplicated key keeps only its last value, as the tool sees it."""
    global _json_chars
    with _json_lock:
        if text in _json_cache:
            _json_cache.move_to_end(text)
            return _json_cache[text]
    lossy = False

    def pairs(members: list) -> dict:
        nonlocal lossy
        result: dict = {}
        for key, value in members:
            lossy = lossy or key in result
            result[key] = value
        return result

    try:
        value = json.loads(text, object_pairs_hook=pairs)
    except (ValueError, RecursionError):
        value, lossy = _INVALID, False
    parsed = (value, lossy)
    with _json_lock:
        if text not in _json_cache:
            _json_cache[text] = parsed
            _json_chars += len(text)
            while _json_cache and (len(_json_cache) > 2_000 or _json_chars > 8_000_000):
                old, _ = _json_cache.popitem(last=False)
                _json_chars -= len(old)
    return parsed


def _leaves(value: Any, prefix: tuple = ()) -> Iterator[tuple[tuple, str, bool]]:
    """(path, text, is_key) for the object keys, strings and numbers in parsed JSON
    (numbers can be card numbers)."""
    if isinstance(value, str):
        yield prefix, value, False
    elif isinstance(value, bool) or value is None:
        return
    elif isinstance(value, (int, float)):
        yield prefix, str(value), False
    elif isinstance(value, dict):
        for key, item in value.items():
            yield prefix + (key,), key, True
            yield from _leaves(item, prefix + (key,))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from _leaves(item, prefix + (index,))


def _json_items(items: list, lossy: set, text: str, path: tuple, kind: str, entry: int) -> None:
    parsed, dropped = _parsed_json(text)
    leaves = None
    if parsed is not _INVALID:
        try:
            leaves = list(_leaves(parsed))
        except RecursionError:  # nested too deeply to walk: check it as text
            leaves = None
    if leaves is None:
        items.append(_Item(kind, text, path, entry))
        return
    if dropped:
        # The text holds values the parsed arguments don't (a duplicated key):
        # send the arguments as parsed, so nothing unchecked goes out.
        lossy.add(path)
    for json_path, leaf, is_key in leaves:
        if leaf:
            items.append(_Item(kind, leaf, path, entry, json_path=json_path, json_key=is_key))


def _entry_items(items: list, lossy: set, entry: dict, index: int) -> None:
    kind = _history_kind(entry)
    if kind is None:
        return
    base = ("conversation_history", index)
    attachment = "attachment" if entry.get("role") == "user" else kind
    _content_items(items, entry.get("content"), base + ("content",), kind, attachment, index)
    if isinstance(entry.get("assistant_content"), str) and entry["assistant_content"]:
        items.append(_Item("model_output", entry["assistant_content"], base + ("assistant_content",), index))
    for name in _REASONING_FIELDS:
        if isinstance(entry.get(name), str) and entry[name]:
            items.append(_Item("model_output", entry[name], base + (name,), index, droppable=True))
    details = entry.get("reasoning_details")
    if isinstance(details, list):
        for number, detail in enumerate(details):
            if isinstance(detail, dict):
                for key in _DETAIL_KEYS:
                    if isinstance(detail.get(key), str) and detail[key]:
                        items.append(_Item("model_output", detail[key], base + ("reasoning_details", number, key),
                                           index, droppable=True))
    image = entry.get("image")
    if isinstance(image, dict):
        for key in _PART_KEYS[1:]:
            if isinstance(image.get(key), str) and image[key]:
                items.append(_Item(kind, image[key], base + ("image", key), index))
    arguments = entry.get("arguments")
    if isinstance(arguments, str) and arguments:
        _json_items(items, lossy, arguments, base + ("arguments",), "model_output", index)
    calls = entry.get("response_tool_calls")
    if isinstance(calls, list):
        for number, call in enumerate(calls):
            function = call.get("function") if isinstance(call, dict) else None
            if isinstance(function, dict) and isinstance(function.get("arguments"), str) and function["arguments"]:
                _json_items(items, lossy, function["arguments"],
                            base + ("response_tool_calls", number, "function", "arguments"), "model_output", index)


def _instruction_spans(instructions: str, segments: Any) -> list[tuple[str, int, int]]:
    """(kind, start, end) parts of the instructions; ``segments`` are (kind, text) pairs
    that the instructions start with (Session._run_turn), anything after is instructions."""
    spans: list[tuple[str, int, int]] = []
    offset = 0
    if segments:
        joined = "".join(text for _kind, text in segments)
        if instructions.startswith(joined):
            for kind, text in segments:
                if text:
                    spans.append((kind if kind in KINDS else MIXED, offset, offset + len(text)))
                offset += len(text)
    if offset < len(instructions):
        spans.append(("instructions", offset, len(instructions)))
    return spans


def _collect(request: dict, message_kind: str, segments: Any) -> tuple[list[_Item], set]:
    """The request's texts, and the tool-argument paths whose JSON must go out as parsed."""
    items: list[_Item] = []
    lossy: set = set()
    instructions = request.get("instructions")
    if isinstance(instructions, str) and instructions:
        for number, (kind, start, end) in enumerate(_instruction_spans(instructions, segments)):
            items.append(_Item(kind, instructions[start:end], ("instructions", number)))
    history = request.get("conversation_history")
    history = history if isinstance(history, list) else []
    user_msg = request.get("user_msg")
    if user_msg:
        # The message is usually in the history too, and backends leave the
        # separate copy out when the two match: check them alike, so they still do.
        for entry in reversed(history):
            if isinstance(entry, dict) and entry.get("role") == "user" and entry.get("content") == user_msg:
                message_kind = _history_kind(entry) or message_kind
                break
    _content_items(items, user_msg, ("user_msg",), message_kind, "attachment", -1)
    for index, entry in enumerate(history):
        if isinstance(entry, dict):
            _entry_items(items, lossy, entry, index)
    return items, lossy


# ── Scanning ──────────────────────────────────────────────────────────────

# Scan results by (rules, kind, text): a conversation sends the same history
# with every request, and only new text needs a scan. Bounded by characters.
_CACHE_CHARS = 16_000_000
_cache_lock = threading.Lock()
_cache: OrderedDict[tuple, tuple] = OrderedDict()
_cache_chars = 0


@dataclass(frozen=True)
class _Match:
    rule: int
    start: int
    end: int


def _scan(policy: DlpPolicy, kind: str, text: str) -> tuple[tuple[_Match, ...], str]:
    """Matches in ``text`` for the rules that check ``kind``, and a digest of it when any matched.

    Rules read the normalized copy (dlp_detectors.normalized); spans are the original's.
    """
    global _cache_chars
    key = (policy.fingerprint, kind, text)
    with _cache_lock:
        cached = _cache.get(key)
        if cached is not None:
            _cache.move_to_end(key)
            return cached
    applicable = [(index, rule) for index, rule in enumerate(policy.rules) if rule.applies_to(kind)]
    found: list[_Match] = []
    if applicable:
        copy = dlp_detectors.normalized(text)
        for index, rule in applicable:
            for start, end in rule.find(copy.text):
                if end > start:
                    found.append(_Match(index, *copy.span(start, end)))
    matches = tuple(found)
    digest = hashlib.sha256(f"{kind}\0{text}".encode("utf-8", "surrogatepass")).hexdigest()[:24] if matches else ""
    result = (matches, digest)
    with _cache_lock:
        if key not in _cache:
            _cache[key] = result
            _cache_chars += len(text)
            while _cache_chars > _CACHE_CHARS and _cache:
                (_, _, old), _ = _cache.popitem(last=False)
                _cache_chars -= len(old)
    return result


def scan_text(text: str, kind: str = MIXED, policy: DlpPolicy | None = None) -> list[tuple[str, str, int, int]]:
    """(rule, action, start, end) for each match in ``text`` (tests and diagnostics)."""
    policy = policy or active()
    if policy is None:
        return []
    matches, _ = _scan(policy, kind, text)
    return [(policy.rules[m.rule].name, policy.rules[m.rule].action, m.start, m.end) for m in matches]


def _redact(text: str, spans: list[tuple[int, int, str]]) -> str:
    """``text`` with each span replaced by its label; overlapping spans join (first label wins)."""
    pieces, cursor = [], 0
    merged: list[list] = []
    for start, end, label in sorted(spans):
        if merged and start < merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end, label])
    for start, end, label in merged:
        pieces.append(text[cursor:start])
        pieces.append(f"[REDACTED:{label}]")
        cursor = end
    pieces.append(text[cursor:])
    return "".join(pieces)


def entry_blocked(entry: Any) -> bool:
    """Whether a conversation entry is withheld, or holds text a block rule matches.

    For work done ahead of the request that will carry the entry (describing
    its images); records nothing, since that request reports the block.
    """
    if not isinstance(entry, dict):
        return False
    if entry.get(WITHHELD):
        return True
    rules = active()
    if rules is None or not any(rule.action == "block" for rule in rules.rules):
        return False
    items: list[_Item] = []
    _entry_items(items, set(), entry, 0)
    return any(rules.rules[match.rule].action == "block"
               for item in items for match in _scan(rules, item.kind, item.text)[0])


# ── Records ────────────────────────────────────────────────────────────────

_seen_lock = threading.Lock()
_seen: OrderedDict[tuple, None] = OrderedDict()


def _first_time(key: tuple) -> bool:
    with _seen_lock:
        if key in _seen:
            _seen.move_to_end(key)
            return False
        _seen[key] = None
        while len(_seen) > 100_000:
            _seen.popitem(last=False)
        return True


def _record(event: str, audit_fields: dict | None, **data: Any) -> None:
    from . import audit

    fields = dict(audit_fields or {})
    audit.record(event, session=str(fields.get("session") or ""), project=str(fields.get("project") or ""),
                 **({"agent": fields["agent"]} if fields.get("agent") else {}), **data)


# ── The check ─────────────────────────────────────────────────────────────


@dataclass
class Checked:
    """A request after DLP: what to send, and what to tell the person."""

    request: dict
    notice: str = ""
    redacted: dict = field(default_factory=dict)  # rule -> matches replaced for the first time


def active() -> DlpPolicy | None:
    """The DLP rules in force, if the organization's policy has any."""
    from .policy import current

    policy = current()
    return policy.dlp if policy is not None else None


def check_text(text: str, *, purpose: str, kind: str = MIXED, provider: str = "", model: str = "",
               audit_fields: dict | None = None) -> str:
    """A single prompt (classification, structured output) as it may be sent; Blocked if it may not."""
    checked = check_request({"user_msg": text}, purpose=purpose, provider=provider, model=model,
                            audit_fields=audit_fields, message_kind=kind)
    return checked.request["user_msg"]


def check_request(request: dict, *, purpose: str, provider: str = "", model: str = "",
                  audit_fields: dict | None = None, segments: Any = None,
                  message_kind: str | None = None) -> Checked:
    """Apply the organization's DLP rules to one model request before it is sent.

    ``request`` holds the backend's keyword arguments (``user_msg``,
    ``conversation_history``, ``instructions``, …); the result's ``request``
    has the same keys, with redactions applied to copies. Raises Blocked when
    the request must not be sent: Lumi's terms aren't accepted yet
    (``terms.REFUSAL_CODE``; before anything reaches a DLP service), a block
    rule matched, the service refused or couldn't answer (``on_error:
    block``), or the organization's policy (its ``dlp`` section included)
    can't be used. Send the result with ``send``.
    """
    from .policy import blocked_reason, current

    if purpose not in NOT_MODEL_REQUESTS:
        refuse_until_terms_accepted()
    refusal = blocked_reason()
    if refusal:
        raise Blocked(refusal, code="policy_blocked")
    policy = current()
    rules = policy.dlp if policy is not None else None
    if rules is None or (not rules.rules and rules.service is None):
        return Checked(request)
    if message_kind is None:
        message_kind = "prompt" if purpose in _PROMPT_PURPOSES else MIXED
    context = {"purpose": purpose, "provider": provider, "model": model}
    try:
        return _check(request, rules, policy.organization, message_kind, segments, audit_fields, context)
    except Blocked:
        raise
    except Exception as exc:  # a check that fails must not let the request through
        logger.exception("The DLP check failed")
        _record("dlp.error", audit_fields, reason="internal", error=type(exc).__name__, **context)
        raise Blocked("Your organization's data loss prevention rules couldn't check this request, so nothing "
                      "was sent. Try again, or ask your administrator.") from exc


def _check(request: dict, rules: DlpPolicy, organization: str, message_kind: str, segments: Any,
           audit_fields: dict | None, context: dict) -> Checked:
    provider, model = context["provider"], context["model"]
    items, lossy = _collect(request, message_kind, segments)
    if sum(len(item.text) for item in items) > MAX_REQUEST_CHARS:
        _record("dlp.error", audit_fields, reason="too_large", **context)
        raise Blocked(f"This request is larger than your organization's data loss prevention rules can check "
                      f"({MAX_REQUEST_CHARS:,} characters), so nothing was sent. Start a new conversation or "
                      "attach less.")
    history = request.get("conversation_history")
    history = history if isinstance(history, list) else []
    scans = [_scan(rules, item.kind, item.text) for item in items]

    def blocking(matches: tuple) -> list:
        return [m for m in matches if rules.rules[m.rule].action == "block"]

    marked = {index for index, entry in enumerate(history) if isinstance(entry, dict) and entry.get(WITHHELD)}
    if rules.service is not None:
        # The service may be what blocked a marked entry. Asking it again could
        # only refuse this request too, or, when it can't answer, send what it
        # once blocked: while there is a service, a marked entry stays out.
        withheld = marked
    else:
        # A marked entry stays out while a rule still blocks it (an
        # administrator may have relaxed the rule since).
        withheld = {item.entry for item, (matches, _) in zip(items, scans)
                    if item.entry in marked and blocking(matches)}
    blocked: Counter = Counter()
    entries: set[int] = set()
    for item, (matches, _digest) in zip(items, scans):
        if item.entry in withheld:
            continue
        for match in blocking(matches):
            blocked[(rules.rules[match.rule].name, item.kind)] += 1
            if item.entry >= 0:
                entries.add(item.entry)
    if blocked:
        for (name, kind), count in blocked.items():
            _record("dlp.finding", audit_fields, rule=name, action="block", kind=kind, count=count,
                    source="rule", **context)
        raise Blocked(_block_message(blocked, entries), entries=tuple(sorted(entries)))

    texts: dict[int, str] = {}     # item index -> the text to send
    dropped: set[int] = set()      # reasoning items to leave out
    new_redactions: Counter = Counter()
    for index, (item, (matches, digest)) in enumerate(zip(items, scans)):
        if item.entry in withheld or not matches:
            continue
        found: Counter = Counter()
        spans = []
        for match in matches:
            rule = rules.rules[match.rule]
            found[(rule.name, rule.action)] += 1
            if rule.action == "redact":
                spans.append((match.start, match.end, rule.name))
        if spans:
            if item.droppable:
                dropped.add(index)
            else:
                texts[index] = _redact(item.text, spans)
        for (name, action), count in found.items():
            key = (str((audit_fields or {}).get("session") or ""), provider, model, name, action, digest)
            if _first_time(key):
                _record("dlp.finding", audit_fields, rule=name, action=action, kind=item.kind, count=count,
                        source="rule", **context)
                if action == "redact":
                    new_redactions[name] += count

    if rules.service is not None:
        _ask_service(rules.service, rules.fingerprint, organization, items, texts, dropped, withheld,
                     new_redactions, audit_fields, context)

    return Checked(_rebuilt(request, items, texts, dropped, withheld, lossy, rules, scans),
                   notice=_notice(new_redactions), redacted=dict(new_redactions))


def _block_message(blocked: Counter, entries: set[int], *, by: str = "rules") -> str:
    """Names the rules and where they matched, never what matched."""
    where = ", ".join(f"{name} in {KIND_LABELS.get(kind, kind)}" for (name, kind) in blocked)
    message = f"Your organization's data loss prevention {by} blocked this request ({where}). Nothing was sent."
    kinds = {kind for _name, kind in blocked}
    if entries:
        message += (" That content stays on this computer and is left out of later requests. "
                    "Rephrase without it, or ask your administrator.")
    elif "attachment" in kinds:
        message += " Remove that attachment and send again, or ask your administrator."
    elif kinds & {"instructions", "tool_result"}:
        message += " Remove it from the project's instructions, notes or attached context, or ask your administrator."
    else:
        message += " Rephrase without it, or ask your administrator."
    return message


def _notice(redactions: Counter) -> str:
    if not redactions:
        return ""
    total = sum(redactions.values())
    names = ", ".join(f"{name} ({count})" if count > 1 else name for name, count in redactions.most_common())
    noun = "match" if total == 1 else "matches"
    return (f"Your organization's data loss prevention rules redacted {total} {noun} before sending "
            f"({names}). The conversation on this computer keeps the original.")


def mark_withheld(history: list, entries: tuple[int, ...]) -> None:
    """Mark conversation entries a block came from, so later requests leave them out."""
    for index in entries:
        if 0 <= index < len(history) and isinstance(history[index], dict):
            history[index][WITHHELD] = True


# ── Rebuilding the request ────────────────────────────────────────────────

_LEAF = object()
_DROP = object()
_NO_LEAF = object()


def _patched(value: Any, tree: dict) -> Any:
    if _LEAF in tree:
        return tree[_LEAF]
    if isinstance(value, dict):
        result: Any = dict(value)
    elif isinstance(value, list):
        result = list(value)
    else:
        return value
    for key, sub in tree.items():
        leaf = sub.get(_LEAF, _NO_LEAF)
        if leaf is _DROP:
            if isinstance(result, dict):
                result.pop(key, None)
        elif leaf is not _NO_LEAF:
            # A new value: the key needn't be there yet (an entry saved without content).
            result[key] = leaf
        else:
            result[key] = _patched(value[key], sub)
    return result


def _put(tree: dict, path: tuple, value: Any) -> None:
    node = tree
    for key in path:
        node = node.setdefault(key, {})
    node[_LEAF] = value


def _withhold(tree: dict, entry: dict, index: int, names: set) -> None:
    """Send a blocked entry as a notice: no text, arguments, reasoning or image of its own."""
    base = ("conversation_history", index)
    _put(tree, base + ("content",), "[Withheld: this content was blocked by your organization's data loss "
                                    f"prevention rules ({', '.join(sorted(names)) or 'a block rule'}).]")
    if entry.get("role") == "user":
        # Lumi wrote this copy, not the person: nothing may learn from it as their words.
        _put(tree, base + ("input_origin",), "generated")
    if isinstance(entry.get("assistant_content"), str):
        _put(tree, base + ("assistant_content",), "")
    for name in (*_REASONING_FIELDS, "reasoning_details", "image"):
        _put(tree, base + (name,), _DROP)
    if isinstance(entry.get("arguments"), str):
        _put(tree, base + ("arguments",), "{}")
    calls = entry.get("response_tool_calls")
    if isinstance(calls, list):
        for number, call in enumerate(calls):
            function = call.get("function") if isinstance(call, dict) else None
            if isinstance(function, dict) and isinstance(function.get("arguments"), str):
                _put(tree, base + ("response_tool_calls", number, "function", "arguments"), "{}")


def _rebuilt(request: dict, items: list, texts: dict, dropped: set, withheld: set, lossy: set,
             rules: DlpPolicy, scans: list) -> dict:
    """``request`` with its redactions, left-out reasoning and withheld entries, copying
    only the containers on the way to a change."""
    if not texts and not dropped and not withheld and not lossy:
        return request
    tree: dict = {}
    json_edits: dict[tuple, tuple[dict, dict]] = {}   # arguments path -> (values, keys) by JSON path
    instruction_parts: dict[int, str] = {}
    withheld_rules: dict[int, set] = {entry: set() for entry in withheld}
    for index, item in enumerate(items):
        if item.entry in withheld:
            withheld_rules[item.entry].update(rules.rules[m.rule].name for m in scans[index][0]
                                              if rules.rules[m.rule].action == "block")
            continue
        if index in dropped:
            # A model's reasoning can be signed: leave all of the entry's reasoning out.
            for name in (*_REASONING_FIELDS, "reasoning_details"):
                _put(tree, item.path[:2] + (name,), _DROP)
        elif index not in texts:
            continue
        elif item.path[0] == "instructions":
            instruction_parts[item.path[1]] = texts[index]
        elif item.json_path is not None:
            values, keys = json_edits.setdefault(item.path, ({}, {}))
            (keys if item.json_key else values)[item.json_path] = texts[index]
        else:
            _put(tree, item.path, texts[index])
    for path in lossy:
        if path[1] not in withheld:
            json_edits.setdefault(path, ({}, {}))
    for path, (values, keys) in json_edits.items():
        original: Any = request
        for key in path:
            original = original[key]
        # Keys and values change; the structure stays, so the arguments stay valid JSON.
        _put(tree, path, json.dumps(_json_rebuilt(_parsed_json(original)[0], (), values, keys),
                                    ensure_ascii=False))
    history = request.get("conversation_history")
    for entry in withheld:
        _withhold(tree, history[entry], entry, withheld_rules[entry])
    if instruction_parts:
        _put(tree, ("instructions",), "".join(instruction_parts.get(item.path[1], item.text)
                                              for item in items if item.path[0] == "instructions"))
    return _patched(request, tree) if tree else request


def _json_rebuilt(value: Any, path: tuple, values: dict, keys: dict) -> Any:
    """Parsed tool arguments with redacted values (by JSON path) and renamed keys."""
    if path in values:
        return values[path]
    if isinstance(value, dict):
        result: dict = {}
        for key, item in value.items():
            child = path + (key,)
            name = keys.get(child, key)
            if name in result:
                # Two keys redacted alike: number the later ones, so no value is lost.
                number = 2
                while f"{name} #{number}" in result:
                    number += 1
                name = f"{name} #{number}"
            result[name] = _json_rebuilt(item, child, values, keys)
        return result
    if isinstance(value, list):
        return [_json_rebuilt(item, path + (index,), values, keys) for index, item in enumerate(value)]
    return value


# ── Only checked requests reach a backend ─────────────────────────────────

_permit: contextvars.ContextVar[bool] = contextvars.ContextVar("lumi_dlp_permit", default=False)
REQUEST_METHODS = ("stream", "stream_auxiliary", "classify", "generate_structured")
_UNCHECKED = ("This model request didn't go through your organization's data loss prevention check, so "
              "nothing was sent. This is a problem in Lumi; please report it.")


@contextmanager
def permit() -> Iterator[None]:
    """Let model requests made inside through the guard: requests that ``check_request``
    or ``check_text`` returned, or fixed text such as a warm-up."""
    token = _permit.set(True)
    try:
        yield
    finally:
        _permit.reset(token)


def send(method: Any, /, *args: Any, **kwargs: Any) -> Any:
    """Call a backend's request method with a checked request.

    A stream it returns keeps the permit while it produces events (adapters
    call their parent class's stream as it runs), never while the caller
    handles them.
    """
    with permit():
        result = method(*args, **kwargs)
    if isinstance(result, Iterator):
        return _permitted(result)
    return result


def _permitted(stream: Iterator) -> Iterator:
    try:
        while True:
            with permit():
                try:
                    event = next(stream)
                except StopIteration:
                    return
            yield event
    finally:
        close = getattr(stream, "close", None)
        if callable(close):
            close()


def _enforced() -> bool:
    """Whether an organization's policy decides what goes to models (its DLP rules, or
    a policy that refuses every request)."""
    from .policy import blocked_reason, current

    policy = current()
    return (policy is not None and policy.dlp is not None) or bool(blocked_reason())


def terms_refusal() -> str:
    """Why no model request may be sent yet because Lumi's terms wait (lumi/terms.py), or ''.

    For requests Lumi makes over plain HTTP rather than a guarded method (Ollama's warm-up and tool
    probe): they ask this first and send nothing while it answers.
    """
    from . import terms

    return terms.request_refusal()


def refuse_until_terms_accepted() -> None:
    """Raise Blocked (``terms.REFUSAL_CODE``) while Lumi's terms wait to be accepted."""
    refusal = terms_refusal()
    if refusal:
        from .terms import REFUSAL_CODE

        raise Blocked(refusal, code=REFUSAL_CODE)


def guarded(method: Any) -> Any:
    """Mark a method that sends to a model.

    Until Lumi's terms are accepted (lumi/terms.py), every call raises Blocked with
    ``terms.REFUSAL_CODE``, under ``send`` or ``permit`` too: fixed text such as a warm-up
    is still a model request. While ``_enforced``, a call that didn't come through ``send``
    or ``permit`` raises Blocked instead of sending."""
    @functools.wraps(method)
    def call(*args: Any, **kwargs: Any) -> Any:
        refuse_until_terms_accepted()
        if not _permit.get() and _enforced():
            where = str(getattr(method, "__qualname__", "") or method)
            logger.error("Refused a model request that skipped the DLP check: %s", where)
            _record("dlp.error", None, reason="unchecked", error=where)
            raise Blocked(_UNCHECKED, code="dlp_unchecked")
        return method(*args, **kwargs)

    call.dlp_guarded = True  # type: ignore[attr-defined]
    return call


def guard_backend(cls: type) -> type:
    """Class decorator for model backends: guard the request methods the class defines."""
    for name in REQUEST_METHODS:
        method = cls.__dict__.get(name)
        if callable(method) and not getattr(method, "dlp_guarded", False):
            setattr(cls, name, guarded(method))
    return cls


# ── The external DLP service ──────────────────────────────────────────────

_transport: Any = None  # tests install an httpx.MockTransport
_verdict_lock = threading.Lock()
# Verdicts by (url, rules, kind, text): the redactions of an allow or redact
# verdict, or _Refused for a text the service blocked.
_verdicts: OrderedDict[tuple, Any] = OrderedDict()


@dataclass(frozen=True)
class _Refused:
    rule: str


class _ServiceError(Exception):
    pass


def set_transport_for_tests(transport: Any) -> None:
    """Send service requests through ``transport`` (an httpx.MockTransport); None restores the network."""
    global _transport
    _transport = transport


def _service_rule(value: Any) -> str:
    return value if isinstance(value, str) and _RULE_NAME.fullmatch(value) else _SERVICE_RULE


def _parse_verdict(data: Any) -> tuple[str, str, list[tuple[str, str, str | None]], set[str]]:
    """(action, rule, redactions as (text, rule, item id or None), ids of the items a block names)."""
    if not isinstance(data, dict):
        raise _ServiceError("answer isn't a JSON object")
    action = data.get("action")
    if action not in ("allow", "redact", "block"):
        raise _ServiceError("answer has no allow, redact or block action")
    rule = _service_rule(data.get("rule"))
    named = data.get("items")
    blocked_items = {str(item) for item in named} if isinstance(named, list) and action == "block" else set()
    redactions: list[tuple[str, str, str | None]] = []
    if action == "redact":
        listed = data.get("redactions")
        if not isinstance(listed, list) or not listed or len(listed) > 1000:
            raise _ServiceError("redact answer has no redactions")
        for entry in listed:
            if isinstance(entry, str):
                text, name, item = entry, rule, None
            elif isinstance(entry, dict):
                text, name = entry.get("text"), _service_rule(entry.get("rule") or rule)
                item = str(entry["item"]) if entry.get("item") is not None else None
            else:
                raise _ServiceError("a redaction isn't text or an object")
            if not isinstance(text, str) or not text or len(text) > 10_000:
                raise _ServiceError("a redaction has no text")
            redactions.append((text, name, item))
    return action, rule, redactions, blocked_items


def _post(service: Service, payload: dict) -> Any:
    import httpx

    from . import __version__
    from .net import client_options

    try:
        with httpx.Client(**client_options(timeout=service.timeout, transport=_transport)) as client:
            response = client.post(service.url, json=payload, headers={"User-Agent": f"Lumi/{__version__}"})
    except httpx.TimeoutException as exc:
        raise _ServiceError("timed out") from exc
    except httpx.HTTPError as exc:
        raise _ServiceError(f"unreachable ({type(exc).__name__})") from exc
    if response.status_code >= 300:
        raise _ServiceError(f"HTTP {response.status_code}")
    try:
        return response.json()
    except ValueError as exc:
        raise _ServiceError("answer isn't JSON") from exc


_verdict_chars = 0
_verdict_version = 0  # changes with every verdict kept or dropped (``_service_memory``)
# Where a request the service blocked without naming its items keeps each of
# them: not a kind, so requests never read it; only ``shareable`` does.
_UNSHARED = "unshared"


def _remember_verdict(key: tuple, verdict: Any) -> None:
    """Keep a verdict for a text, bounded by entries and characters."""
    global _verdict_chars, _verdict_version
    with _verdict_lock:
        _verdict_version += 1
        if key in _verdicts:
            _verdicts[key] = verdict
            _verdicts.move_to_end(key)
            return
        _verdicts[key] = verdict
        _verdict_chars += len(key[-1])
        while _verdicts and (len(_verdicts) > 10_000 or _verdict_chars > 16_000_000):
            old, _ = _verdicts.popitem(last=False)
            _verdict_chars -= len(old[-1])


def _ask_service(service: Service, fingerprint: str, organization: str, items: list, texts: dict,
                 dropped: set, withheld: set, new_redactions: Counter, audit_fields: dict | None,
                 context: dict) -> None:
    """Send text the service hasn't judged to it and apply its verdict to ``texts``.

    Each distinct (kind, text) goes once: a message and its copy in the
    history are one item. Text it blocked before is refused without asking.
    """
    pending: dict[tuple[str, str], list[int]] = {}   # (kind, text) -> item indices
    known: dict[int, list] = {}
    refused: dict[tuple[str, str], tuple[str, list[int]]] = {}  # (kind, text) -> (rule, item indices)
    for index, item in enumerate(items):
        if item.entry in withheld or index in dropped:
            continue
        text = texts.get(index, item.text)
        key = (service.url, fingerprint, item.kind, text)
        with _verdict_lock:
            cached = _verdicts.get(key)
            if cached is not None:
                _verdicts.move_to_end(key)
        if cached is None:
            pending.setdefault((item.kind, text), []).append(index)
        elif isinstance(cached, _Refused):
            refused.setdefault((item.kind, text), (cached.rule, []))[1].append(index)
        else:
            known[index] = list(cached)
    if refused:
        _refuse(Counter((rule, kind) for (kind, _text), (rule, _) in refused.items()),
                [indices for _rule, indices in refused.values()], items, audit_fields, context)
    if pending:
        ordered = list(pending.items())
        try:
            if sum(len(text) for (_kind, text), _ in ordered) > MAX_SERVICE_CHARS:
                raise _ServiceError("request too large")
            payload = {
                "version": VERSION,
                "organization": organization,
                **context,
                "items": [{"id": str(number), "kind": kind, "text": text}
                          for number, ((kind, text), _) in enumerate(ordered)],
            }
            action, rule, redactions, blocked_items = _parse_verdict(_post(service, payload))
        except _ServiceError as exc:
            _record("dlp.error", audit_fields, reason="service", error=str(exc), on_error=service.on_error, **context)
            if service.on_error == "allow":
                _apply_known(items, texts, known, dropped, new_redactions, audit_fields, context)
                return
            raise Blocked("Your organization's data loss prevention service couldn't check this request "
                          f"({exc}), so nothing was sent. Try again, or ask your administrator.") from None
        if action == "block":
            named = [(key, indices) for number, (key, indices) in enumerate(ordered) if str(number) in blocked_items]
            for (kind, text), _indices in named:
                # Remembered, so the same text is refused again without asking.
                _remember_verdict((service.url, fingerprint, kind, text), _Refused(rule))
            if named:
                blocked = Counter((rule, kind) for (kind, _text), _ in named)
                groups = [indices for _key, indices in named]
            else:
                # The whole request: leave out of later requests everything in it
                # the service hadn't allowed before, since one of those was it.
                blocked = Counter({(rule, MIXED): 1})
                groups = [indices for _key, indices in ordered]
                # Nor may organization oversight share any of it (``shareable``),
                # apart from instructions, which it never shares and whose many
                # phrases would withhold ordinary messages.
                for (kind, text), _indices in ordered:
                    if kind != "instructions":
                        _remember_verdict((service.url, fingerprint, _UNSHARED, text), _Refused(rule))
            _refuse(blocked, groups, items, audit_fields, context)
        for number, ((kind, text), indices) in enumerate(ordered):
            applicable = tuple((value, name) for value, name, item in redactions
                               if (item is None or item == str(number)) and value in text)
            for index in indices:
                known[index] = list(applicable)
            _remember_verdict((service.url, fingerprint, kind, text), applicable)
    _apply_known(items, texts, known, dropped, new_redactions, audit_fields, context)


def _refuse(blocked: Counter, groups: list, items: list, audit_fields: dict | None, context: dict) -> NoReturn:
    """Record a service block and raise it, naming the history entries it came from."""
    for (name, kind), count in blocked.items():
        _record("dlp.finding", audit_fields, rule=name, action="block", kind=kind, count=count,
                source="service", **context)
    entries = {items[index].entry for indices in groups for index in indices if items[index].entry >= 0}
    raise Blocked(_block_message(blocked, entries, by="service"), entries=tuple(sorted(entries)))


def _apply_known(items: list, texts: dict, known: dict, dropped: set, new_redactions: Counter,
                 audit_fields: dict | None, context: dict) -> None:
    for index, redactions in known.items():
        if not redactions:
            continue
        item = items[index]
        text = texts.get(index, item.text)
        found: Counter = Counter()
        for value, name in redactions:
            count = text.count(value)
            if count:
                text = text.replace(value, f"[REDACTED:{name}]")
                found[name] += count
        if not found:
            continue
        if item.droppable:
            dropped.add(index)  # signed reasoning: left out, never edited
        else:
            texts[index] = text
        digest = hashlib.sha256(f"{item.kind}\0{item.text}".encode("utf-8", "surrogatepass")).hexdigest()[:24]
        for name, count in found.items():
            key = (str((audit_fields or {}).get("session") or ""), context["provider"], context["model"],
                   name, "redact:service", digest)
            if _first_time(key):
                _record("dlp.finding", audit_fields, rule=name, action="redact", kind=item.kind, count=count,
                        source="service", **context)
                new_redactions[name] += count


# ── Copies that aren't model requests ──────────────────────────────────────


# A text shorter than this counts only when it is the whole of what the
# service blocked, or all of what is shared: "ok" or a folder's name in a
# blocked text mustn't withhold every text holding it.
_SHARED_MATCH_CHARS = 8
_shared_memory: tuple = ()  # (url, rules, _verdict_version, blocked texts, {redacted value: rule})


def shareable(text: Any, kind: str = MIXED) -> str | None:
    """``text`` as DLP lets it leave this computer other than in a model request; None to withhold it.

    For organization oversight's copies (lumi/oversight.py, security_flags.py),
    checked whole before anything is cut from them. The rules that check
    ``kind`` (``mixed``: every rule) apply: a ``block`` match withholds the
    text, and ``redact`` matches read ``[REDACTED:<rule>]``. With a service,
    its remembered verdicts apply too: values it redacted are replaced
    wherever they are, and a text it blocked, or that was in a request it
    blocked without naming what, is withheld, as is a text holding it or
    part of it. It never asks the service, so text the service hasn't judged
    (Lumi's last reply) meets the rules alone. A dlp section Lumi can't use
    withholds everything, as it refuses every request; so does a check that
    fails. Records nothing.
    """
    value = str(text or "")
    if not value:
        return value
    try:
        from .policy import current

        policy = current()
        if policy is None:
            return value
        if policy.dlp_error:
            return None
        rules = policy.dlp
        if rules is None or (not rules.rules and rules.service is None):
            return value
        matches, _ = _scan(rules, kind if kind in KINDS else MIXED, value)
        if any(rules.rules[match.rule].action == "block" for match in matches):
            return None
        spans = [(match.start, match.end, rules.rules[match.rule].name) for match in matches
                 if rules.rules[match.rule].action == "redact"]
        result = _redact(value, spans) if spans else value
        if rules.service is None:
            return result
        blocked, redacted = _service_memory(rules)
        if blocked:
            # Spacing collapsed, and without the "…" of an excerpt already cut (security_flags.clip).
            forms = {" ".join(form.split()) for form in (value, result, value.rstrip("…"))}
            if any(_overlaps(judged, form) for judged in blocked for form in forms):
                return None
        for found, name in redacted.items():
            if found in result:
                result = result.replace(found, f"[REDACTED:{name}]")
        return result
    except Exception:
        logger.exception("The DLP check of a shared copy failed")
        return None


def _overlaps(judged: str, text: str) -> bool:
    """Whether ``text`` is ``judged``, holds it or is part of it (``judged`` with its spacing collapsed)."""
    if not judged or not text:
        return False
    if judged == text:
        return True
    shorter, longer = (judged, text) if len(judged) < len(text) else (text, judged)
    return len(shorter) >= _SHARED_MATCH_CHARS and shorter in longer


def _service_memory(rules: DlpPolicy) -> tuple[tuple[str, ...], dict[str, str]]:
    """The texts the service blocked (spacing collapsed), and the values it redacted (longest first)."""
    global _shared_memory
    url = rules.service.url
    with _verdict_lock:
        if _shared_memory[:3] == (url, rules.fingerprint, _verdict_version):
            return _shared_memory[3], _shared_memory[4]
        blocked: dict[str, None] = {}
        redacted: dict[str, str] = {}
        for key, verdict in _verdicts.items():
            if key[0] != url or key[1] != rules.fingerprint:
                continue
            if isinstance(verdict, _Refused):
                blocked[" ".join(key[-1].split())] = None
            else:
                for found, name in verdict:
                    if found:
                        redacted.setdefault(found, name)
        # Longest first, so a value holding another is replaced whole.
        ordered = dict(sorted(redacted.items(), key=lambda pair: -len(pair[0])))
        _shared_memory = (url, rules.fingerprint, _verdict_version, tuple(blocked), ordered)
        return _shared_memory[3], _shared_memory[4]


def reset_for_tests() -> None:
    """Forget cached scans, verdicts and recorded findings; use the network again."""
    global _cache_chars, _json_chars, _verdict_chars, _shared_memory
    with _cache_lock:
        _cache.clear()
        _cache_chars = 0
    with _seen_lock:
        _seen.clear()
    with _verdict_lock:
        _verdicts.clear()
        _verdict_chars = 0
        _shared_memory = ()
    with _json_lock:
        _json_cache.clear()
        _json_chars = 0
    set_transport_for_tests(None)
