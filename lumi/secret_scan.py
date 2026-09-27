"""Remove secrets from what Lumi sends to a model.

Two layers, applied to the conversation just before each model request:

* The API keys saved in Lumi (and the provider keys in its environment) are
  always replaced in tool output. No model needs Lumi's own credentials.
  Values shorter than 16 characters are ignored, so dummy keys such as
  ``ollama`` never match ordinary text.
* With Settings > Privacy > "Scan for secrets" on, well-known credential
  formats (cloud and platform keys, tokens, private keys, passwords in
  connection strings and ``.env`` lines) are replaced as well, in tool output
  and in the messages you send.

A replacement reads ``[REDACTED <kind>]`` so the model knows something was
there and why it can't see it. The scan covers Lumi's own model requests;
Codex and Claude Code read files through their own tools and are not scanned.

``redact_for_sharing`` is stricter, for what Lumi shares with an
organization's Lumi Cloud (organization oversight): it also removes
credentials without a well-known format, such as an ``Authorization``
header, a ``--password`` option or a secret query parameter.
"""

from __future__ import annotations

import math
import os
import re
import secrets
import threading
from collections import Counter
from typing import Any, Iterable

from .secrets_store import PLACEHOLDER, PROVIDER_KEY_ENV

MIN_KNOWN_LENGTH = 16
KNOWN_KIND = "saved API key"
_MARK = "secret_scan"

# Other credentials commonly present in a developer's environment. They are
# only used as exact values to look for, never sent or stored.
_ENV_SECRETS = PROVIDER_KEY_ENV + (
    "GITHUB_TOKEN", "GH_TOKEN", "GITLAB_TOKEN", "HF_TOKEN", "HUGGING_FACE_HUB_TOKEN",
    "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN", "NPM_TOKEN", "SLACK_BOT_TOKEN",
)

# (kind, pattern). A pattern with a ``secret`` group replaces only that group,
# keeping the surrounding name (``DB_PASSWORD=``) readable. Specific formats come
# before generic ones: an Anthropic key would otherwise read as an OpenAI key.
#
# They run on text other people wrote (web pages, command output), so each
# must take linear time: a match starts only where a token starts (a
# lookbehind, not ``\b``, where the token may contain ``-`` or ``.``), and a
# name is consumed once (a lookahead finds the keyword, a possessive
# quantifier takes the name). ``tests/test_secret_scan.py`` times them on
# hostile text.
_NOT_ALREADY = r"(?!\[REDACTED)(?![$<{])"
PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    # The body stops at the next BEGIN, so many headers without an END don't
    # each scan to the end of the text.
    ("private key", re.compile(
        r"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY(?: BLOCK)?-----(?:(?!-----BEGIN )[\s\S])*?"
        r"-----END (?:[A-Z0-9]+ )*PRIVATE KEY(?: BLOCK)?-----")),
    ("AWS access key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("AWS secret key", re.compile(
        r"(?i)aws_secret_access_key[\"']?\s*[:=]\s*[\"']?(?P<secret>[A-Za-z0-9/+=]{40})(?![A-Za-z0-9/+=])")),
    ("GitHub token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{22,})\b")),
    ("GitLab token", re.compile(r"\bglpat-[A-Za-z0-9_\-]{20,}")),
    ("Slack token", re.compile(r"\bxox[abposr]-[A-Za-z0-9-]{10,}")),
    ("Slack webhook", re.compile(r"https://hooks\.slack\.com/services/[A-Za-z0-9/_\-]{20,}")),
    ("Stripe key", re.compile(r"\b(?:sk|rk)_live_[A-Za-z0-9]{20,}")),
    ("Anthropic key", re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{20,}")),
    ("OpenAI key", re.compile(r"\bsk-(?:proj-|svcacct-|admin-)?[A-Za-z0-9_\-]{20,}")),
    ("Google API key", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}(?![0-9A-Za-z_\-])")),
    ("Hugging Face token", re.compile(r"\bhf_[A-Za-z0-9]{30,}")),
    ("npm token", re.compile(r"\bnpm_[A-Za-z0-9]{36}\b")),
    ("Azure storage key", re.compile(r"(?i)AccountKey=(?P<secret>[A-Za-z0-9+/=]{40,})")),
    ("JSON web token", re.compile(
        r"(?<![A-Za-z0-9_-])eyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}")),
    ("password in a URL", re.compile(
        r"(?<![a-zA-Z0-9+.\-])[a-zA-Z][a-zA-Z0-9+.\-]*://[^\s:/@]+:" + _NOT_ALREADY + r"(?P<secret>[^\s@/]{4,})@")),
    ("secret in .env", re.compile(
        r"(?m)^[ \t]*(?:export[ \t]+)?"
        r"(?=[A-Z0-9_]*(?:PASSWORD|PASSWD|SECRET|TOKEN|API_KEY|APIKEY|PRIVATE_KEY|ACCESS_KEY))[A-Z0-9_]++"
        r"[ \t]*=[ \t]*[\"']?" + _NOT_ALREADY + r"(?P<secret>[^\s\"'#]{8,})")),
)

# ── What leaves this computer: stricter (organization oversight) ────────────
#
# A model request keeps the patterns above: a model may need a hash or a long
# identifier to do its work. What Lumi shares with an organization's Lumi
# Cloud (lumi/oversight.py, lumi/security_flags.py) also loses credentials in
# commands, URLs and messages that have no well-known format. These run after
# PATTERNS, in linear time like them: a name is taken whole by a possessive
# quantifier and judged by a function, never searched for a keyword by
# backtracking.
_NOT_ALREADY_QUOTED = r"(?![\"']?\[REDACTED)(?![\"']?[$<{])"
# A value: quoted, or up to the end of the argument.
_VALUE = r"(?:\"[^\"\n]*\"|'[^'\n]*'|[^\s\"',;&)}\]]+)"
# Words that make a name a secret's when they are a whole part of it
# (db_password, apiKey, X-Auth-Token, client-secret), not inside another word.
_SECRET_WORD = re.compile(
    r"(?i)^(?:password|passwd|passphrase|pwd|pass|secret|token|apikey|api_key|api-key|accesskey|access_key|"
    r"access-key|secretkey|secret_key|secret-key|privatekey|private_key|private-key|credential|credentials|"
    r"clientsecret|client_secret|client-secret|authtoken|auth_token|auth-token|accesstoken|access_token|"
    r"access-token|refreshtoken|refresh_token|refresh-token|sessionid|session_id|session-id|sessiontoken|"
    r"session_token|session-token|privatetoken|private_token|private-token|signature|sig|jwt|auth)$")
# Environment variables are one upper-case word: PGPASSWORD, MYSQL_PWD, GITHUB_TOKEN.
_ENV_SECRET = re.compile(r"PASSWORD|PASSWD|PASSPHRASE|SECRET|TOKEN|API_?KEY|ACCESS_?KEY|PRIVATE_?KEY|CREDENTIAL"
                         r"|_PWD$|_PASS$|_AUTH$")
# Query parameters that carry credentials whatever else they are called.
_URL_SECRET = re.compile(r"(?i)^(?:key|code|sig|signature|auth|session|x-amz-signature|x-amz-credential|"
                         r"x-amz-security-token|x-goog-signature|x-goog-credential)$")
_NOT_A_SECRET = frozenset({"true", "false", "yes", "no", "none", "null", "on", "off"})


def _name_parts(name: str) -> list[str]:
    """``db_password`` → [db, password]; ``githubToken`` → [github, token]; ``X-Api-Key`` → [x, api, key, api-key]."""
    split = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", name)
    parts = [part for part in re.split(r"[_.\-]+", split) if part]
    # Two-word names (api key, access token) as one part too.
    return parts + [f"{a}_{b}" for a, b in zip(parts, parts[1:])]


def _secret_name(name: str, *, url: bool = False) -> bool:
    name = name.lstrip("-")
    if not name or len(name) > 200:
        return False
    if url and _URL_SECRET.match(name):
        return True
    if name.isupper():  # an environment variable; PWD and OLDPWD are folders
        return bool(_ENV_SECRET.search(name))
    return any(_SECRET_WORD.match(part) for part in _name_parts(name))


def _secret_value(value: str) -> bool:
    bare = value.strip("\"'")
    return bool(bare) and not bare.isdigit() and bare.lower() not in _NOT_A_SECRET


def _named(match: re.Match[str], *, url: bool = False) -> bool:
    return _secret_name(match.group("name"), url=url) and _secret_value(match.group("secret"))


def _looks_random(match: re.Match[str]) -> bool:
    """Whether a long token looks like a credential: both cases, digits, and the variety of random text.

    Random letters rarely run lower-case for 7 or more, while a long
    CamelCase name (``TestOrganizationPolicy2Factor``) spells words; very
    varied text counts whatever its runs.
    """
    token = match.group("secret")
    if not (any(c.islower() for c in token) and any(c.isupper() for c in token)
            and sum(c.isdigit() for c in token) >= 2):
        return False
    counts = Counter(token)
    entropy = -sum(n / len(token) * math.log2(n / len(token)) for n in counts.values())
    longest = max((len(run) for run in re.findall(r"[a-z]+", token)), default=0)
    return entropy >= 4.6 or (entropy >= 4.0 and longest <= 6)


# (kind, pattern, whether a match is a secret: None for always).
SHARING_PATTERNS: tuple[tuple[str, re.Pattern[str], Any], ...] = (
    # A private key whose END was cut off: the header and the base64 after it.
    ("private key", re.compile(r"-----BEGIN [A-Z0-9 ]{0,40}PRIVATE KEY(?: BLOCK)?-----[A-Za-z0-9+/=\s]*"), None),
    # Authorization: Bearer <token>, Basic <base64>, token <x> (headers, curl -H, JSON).
    ("authorization header", re.compile(
        r"(?i)(?<![A-Za-z0-9-])(?:proxy-)?authorization[\"']?[ \t]*[:=][ \t]*[\"']?"
        r"(?:(?:bearer|basic|token|digest|negotiate|apikey|api-key|sso-key|key)[ \t]+)?"
        + _NOT_ALREADY + r"(?P<secret>[^\s\"',;]+)"), None),
    ("cookie", re.compile(
        r"(?i)(?<![A-Za-z0-9-])(?:set-)?cookie[\"']?[ \t]*:[ \t]*[\"']?" + _NOT_ALREADY
        + r"(?P<secret>[^\"'\r\n]+)"), None),
    # mysql -pPASSWORD (the password right after -p), sshpass -p x, curl -u user:password.
    ("password or token option", re.compile(
        r"(?<![\w.-])(?i:mysql|mysqldump|mysqladmin|mysqlimport|mysqlshow|mysqlcheck|mysqlsh|mariadb|"
        r"mariadb-dump)(?:\.exe)?(?![\w.-])[^\n;&|]{0,300}?[ \t]-p(?!\s)" + _NOT_ALREADY
        + r"(?P<secret>[^\s\"']+)"), None),
    ("password or token option", re.compile(
        r"(?<![\w-])sshpass[ \t]+-p[ \t]*" + _NOT_ALREADY + r"(?P<secret>[^\s\"']+)"), None),
    ("password or token option", re.compile(
        r"(?<![^\s\"'])(?:-u|--user)(?:[ \t]+|=)[\"']?[^\s:\"']+:" + _NOT_ALREADY + r"(?P<secret>[^\s\"'@]+)"),
        None),
    # --password=x, --token x, --api-key "x", --db-password=x.
    ("password or token option", re.compile(
        r"(?<![^\s\"'=(])--?(?P<name>[A-Za-z0-9][A-Za-z0-9-]*+)(?:=|[ \t]+)(?!-)" + _NOT_ALREADY_QUOTED
        + r"(?P<secret>" + _VALUE + r")"), _named),
    # ?api_key=x, &token=x, ?key=x, &X-Amz-Signature=x, ?code=x in a URL.
    ("secret in a URL", re.compile(
        r"(?<=[?&;])(?P<name>[A-Za-z0-9_.\-]++)=" + _NOT_ALREADY + r"(?P<secret>[^&#\s\"']+)"),
        lambda match: _named(match, url=True)),
    # PGPASSWORD=x psql, set GITHUB_TOKEN=x, db_password = x, apiKey=x (not ==).
    ("secret assignment", re.compile(
        r"(?<![A-Za-z0-9_.\-])(?P<name>[A-Za-z0-9_.\-]++)[ \t]*=(?!=)[ \t]*" + _NOT_ALREADY_QUOTED
        + r"(?P<secret>" + _VALUE + r")"), _named),
    # "password": "x", password: x, X-Api-Key: x (JSON, YAML, headers).
    ("secret assignment", re.compile(
        r"(?<![A-Za-z0-9_.\-])(?P<name>[A-Za-z0-9_.\-]++)[\"']?[ \t]*:[ \t]*" + _NOT_ALREADY_QUOTED
        + r"(?P<secret>" + _VALUE + r")"), _named),
    # user:x@host with a password shorter than PATTERNS' four characters.
    ("password in a URL", re.compile(
        r"(?<![a-zA-Z0-9+.\-])[a-zA-Z][a-zA-Z0-9+.\-]*://[^\s:/@]+:" + _NOT_ALREADY + r"(?P<secret>[^\s@/]+)@"),
        None),
    # A long random-looking token with no name or known format. Hex (hashes,
    # commit ids) and UUIDs have one case and stay; "/" ends a token, so paths stay.
    ("random-looking token", re.compile(
        r"(?<![A-Za-z0-9_+=\-])(?P<secret>[A-Za-z0-9_+=\-]{32,})(?![A-Za-z0-9_+=\-])"), _looks_random),
)

# Setting names whose values are credentials wherever they appear (an MCP
# server's env, a gateway token, an HTTP header).
SENSITIVE_NAME = re.compile(
    r"(?i)(api[_-]?key|apikey|access[_-]?key|secret|token|passw(or)?d|authorization|credential|private[_-]?key|cookie)"
)

_lock = threading.Lock()
_state: dict[str, Any] = {"patterns": False, "known": (), "generation": secrets.token_hex(4)}


def configure(settings: Any) -> dict[str, Any]:
    """Load the scan setting and the values of saved keys. Returns a status summary."""
    enabled = bool(settings.get("privacy", "secret_scan", False)) if settings is not None else False
    values = tuple(sorted(secret_values(settings, min_length=MIN_KNOWN_LENGTH), key=len, reverse=True))
    with _lock:
        if enabled != _state["patterns"] or values != _state["known"]:
            # Scrubbed history entries carry the generation they were checked
            # against; a new one makes every entry be checked again once.
            _state.update(patterns=enabled, known=values, generation=secrets.token_hex(4))
    return {"secret_scan": enabled}


def reset() -> None:
    """Back to the defaults: no pattern scan, no known values (tests and shutdown)."""
    with _lock:
        _state.update(patterns=False, known=(), generation=secrets.token_hex(4))


def secret_values(settings: Any = None, *, min_length: int = MIN_KNOWN_LENGTH) -> set[str]:
    """The credential values Lumi knows: saved keys, sensitive settings and environment keys."""
    found: set[str] = set()
    if settings is not None:
        names = settings.get("api_keys") or {}
        for name in names if isinstance(names, dict) else ():
            # get() resolves keys kept in the OS credential store.
            found.add(str(settings.get("api_keys", name, "") or ""))
        get_all = getattr(settings, "get_all", None)
        if callable(get_all):
            _sensitive_strings(get_all(), found)
    for name in _ENV_SECRETS:
        found.add(str(os.environ.get(name) or ""))
    return {value.strip() for value in found
            if len(value.strip()) >= min_length and value.strip() != PLACEHOLDER}


def _sensitive_strings(node: Any, found: set[str], *, sensitive: bool = False) -> None:
    if isinstance(node, dict):
        for key, value in node.items():
            _sensitive_strings(value, found, sensitive=sensitive or bool(SENSITIVE_NAME.search(str(key))))
    elif isinstance(node, list):
        for item in node:
            _sensitive_strings(item, found, sensitive=sensitive)
    elif sensitive and isinstance(node, str):
        found.add(node)


def _settings() -> tuple[bool, tuple[str, ...], str]:
    with _lock:
        return _state["patterns"], _state["known"], _state["generation"]


def redact_text(text: str, *, patterns: bool | None = None,
                known: Iterable[str] | None = None) -> tuple[str, Counter]:
    """Return ``text`` with secrets replaced, and a count of what was replaced by kind.

    ``patterns`` and ``known`` default to the configured scan (see ``configure``).
    """
    found: Counter = Counter()
    if not text or not isinstance(text, str):
        return text, found
    configured_patterns, configured_known, _ = _settings()
    use_patterns = configured_patterns if patterns is None else patterns
    values = configured_known if known is None else tuple(
        sorted({v for v in known if isinstance(v, str) and len(v) >= MIN_KNOWN_LENGTH}, key=len, reverse=True))
    for value in values:
        count = text.count(value)
        if count:
            text = text.replace(value, f"[REDACTED {KNOWN_KIND}]")
            found[KNOWN_KIND] += count
    if use_patterns:
        for kind, pattern in PATTERNS:
            text = pattern.sub(_replacer(kind, found), text)
    return text, found


def _replacer(kind: str, found: Counter, secret: Any = None):
    """A ``re.sub`` function that replaces a match (or its ``secret`` group), when ``secret(match)`` agrees."""
    label = f"[REDACTED {kind}]"

    def replace(match: re.Match[str]) -> str:
        if secret is not None and not secret(match):
            return match.group(0)
        found[kind] += 1
        if "secret" not in match.re.groupindex or match.group("secret") is None:
            return label
        start, end = match.span("secret")
        whole_start = match.start()
        whole = match.group(0)
        return whole[: start - whole_start] + label + whole[end - whole_start:]

    return replace


def redact_for_sharing(text: Any) -> tuple[str, Counter]:
    """``text`` as Lumi may share it outside this computer, and a count of what was removed by kind.

    Saved key values and the well-known formats go at every level, whatever
    the person's scan setting; then SHARING_PATTERNS: credentials in
    headers, options, assignments and URLs, a private key cut off before its
    end, and long random-looking tokens. Redact the whole text before cutting
    it: a secret cut in half no longer matches its pattern or its value.
    """
    value, found = redact_text(str(text or ""), patterns=True)
    for kind, pattern, secret in SHARING_PATTERNS:
        value = pattern.sub(_replacer(kind, found, secret), value)
    return value, found


def _redact_content(content: Any, *, patterns: bool) -> tuple[Any, Counter]:
    if isinstance(content, str):
        return redact_text(content, patterns=patterns)
    found: Counter = Counter()
    if isinstance(content, list):
        for part in content:
            if isinstance(part, dict) and isinstance(part.get("text"), str):
                part["text"], part_found = redact_text(part["text"], patterns=patterns)
                found.update(part_found)
    return content, found


def scrub_history(history: list[dict]) -> Counter:
    """Redact secrets in place in the entries the next request will send.

    Tool results are always checked for saved key values. With the scan on,
    well-known formats are removed too, and your own messages are included.
    Each entry is checked once per configuration.
    """
    patterns, known, generation = _settings()
    if not patterns and not known:
        return Counter()
    found: Counter = Counter()
    for entry in history:
        if not isinstance(entry, dict) or entry.get(_MARK) == generation:
            continue
        role = entry.get("role")
        if role == "tool_result" or (patterns and role == "user"):
            entry["content"], entry_found = _redact_content(entry.get("content"), patterns=patterns)
            found.update(entry_found)
            entry[_MARK] = generation
    return found


def scrub_user_message(text: Any) -> tuple[Any, Counter]:
    """Redact the message being sent when the scan is on (it matches its history copy)."""
    patterns, _, _ = _settings()
    if not patterns:
        return text, Counter()
    return _redact_content(text, patterns=True)


def describe(found: Counter) -> str:
    """One sentence for the user, such as 'Removed 2 secrets (GitHub token, private key)'."""
    total = sum(found.values())
    kinds = ", ".join(kind for kind, _ in found.most_common())
    noun = "secret" if total == 1 else "secrets"
    return f"Removed {total} {noun} ({kinds}) before sending to the model."
