"""Conservative pre-persistence secret rejection for the initial text gateway.

This is defense in depth, not an assurance that arbitrary text contains no secret.
It never rewrites a user's evidence silently. The caller must prepare a sanitized
copy and explicitly upload it again. Binary/image content remains unsupported
until it has a qualified inspection/redaction path.
"""

from __future__ import annotations

import re

from .models import InvalidRequest

_PATTERNS = tuple(re.compile(pattern) for pattern in (
    r"-----BEGIN (?:RSA |EC |OPENSSH |DSA |ENCRYPTED )?PRIVATE KEY-----",
    r"(?i)\b(?:authorization\s*[:=]\s*)?(?:bearer|basic)\s+[A-Za-z0-9._~+/=-]{12,}",
    r"\b(?:sk-(?:proj-|or-v1-)?[A-Za-z0-9_-]{20,}|gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|AKIA[A-Z0-9]{16})\b",
    r"(?i)[\"']?(?:api[_-]?key|access[_-]?token|refresh[_-]?token|client[_-]?secret|password)[\"']?\s*[:=]\s*[\"']?[^\s\"',;}]{8,}",
    r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b",
    r"(?i)https?://[^\s/:@]+:[^\s/@]+@",
))


def screen(content: bytes, media_type: str) -> None:
    """Reject unsupported encoding/classes or recognizable credential material."""
    if media_type not in {"text/plain", "application/json"}:
        raise InvalidRequest("this gateway currently accepts inspected UTF-8 text only")
    try:
        text = content.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        raise InvalidRequest("content must be complete UTF-8 text") from None
    if "\x00" in text or any(pattern.search(text) for pattern in _PATTERNS):
        raise InvalidRequest("content requires explicit secret removal before upload")
