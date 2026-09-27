"""What the organization's DLP rules look for (lumi/dlp.py), all in linear time.

* Built-in detectors: payment card numbers (brand prefix and Luhn checked),
  US Social Security numbers (with separators), IBANs (country length and
  mod-97 checked), the credential formats of ``secret_scan.PATTERNS``, and
  email addresses.
* Keyword rules: literal words and phrases, compiled into one prefix tree.
* Pattern rules: regular expressions an administrator writes. Python's regex
  engine backtracks, so ``check_pattern`` refuses what could make a scan slow:
  unbounded repeats (``*``, ``+``, ``{n,}``), backreferences, matches longer
  than ``MAX_MATCH_WIDTH`` characters, and repeats that compete for the same
  characters beyond ``MAX_WAYS`` combinations (``\\d{1,5}\\d{1,5}`` counts 25;
  ``[a-z]{1,63}\\.`` counts one, because nothing it gives back can start a
  dot). The combinations times the longest match stay under ``MAX_COST``, so a
  scan takes at most about that many steps per character, whatever the text.

Every finder takes the text and yields ``(start, end)`` spans.
"""

from __future__ import annotations

import re
from re import _constants as _sre
from re import _parser as _sre_parse
from typing import Callable, Iterable, Iterator, NamedTuple

Finder = Callable[[str], Iterable[tuple[int, int]]]

MAX_MATCH_WIDTH = 256
MAX_WAYS = 16
MAX_COST = 128


class PatternError(ValueError):
    """A pattern rule Lumi won't run; the message says why and how to fix it."""


# The built-in patterns start with a character class so the regex engine can
# skip ahead to candidates, and check the character before a match with a
# lookbehind after that first character: a match starts only where its run of
# characters starts, so no character is read again from a later position.

# ── Payment cards ────────────────────────────────────────────────────────────

_CARD_RUN = re.compile(r"[0-9](?<![0-9][0-9])[0-9]{0,18}(?:[ -][0-9]{1,19})*(?![0-9])")
_GROUP = re.compile(r"[0-9]+")
_PLAIN = bytes(c - 48 if 48 <= c <= 57 else 0 for c in range(256))
_DOUBLED = bytes((2 * (c - 48)) - (9 if c > 52 else 0) if 48 <= c <= 57 else 0 for c in range(256))


def luhn_ok(digits: str) -> bool:
    """Whether ``digits`` passes the Luhn checksum card numbers carry."""
    raw = digits.encode("ascii")
    return (sum(raw[-1::-2].translate(_PLAIN)) + sum(raw[-2::-2].translate(_DOUBLED))) % 10 == 0


def card_brand_ok(digits: str) -> bool:
    """Whether ``digits`` has a card network's prefix and length (Visa, Mastercard,
    American Express, Discover, JCB, Diners Club, UnionPay)."""
    length = len(digits)
    if not 13 <= length <= 19:
        return False
    first = digits[0]
    if first == "4":
        ok = length in (13, 16, 19)
    elif first == "5":
        ok = length == 16 and "51" <= digits[:2] <= "55"
    elif first == "2":
        ok = length == 16 and "2221" <= digits[:4] <= "2720"
    elif first == "3":
        two = digits[:2]
        if two in ("34", "37"):
            ok = length == 15
        elif two == "35":
            ok = length >= 16 and "3528" <= digits[:4] <= "3589"
        elif two == "30":
            ok = length >= 14 and digits[:3] <= "305"
        else:
            ok = length >= 14 and two in ("36", "38", "39")
    elif first == "6":
        four = digits[:4]
        ok = length >= 16 and (four == "6011" or "6440" <= four <= "6599" or "6200" <= four <= "6299")
    else:
        ok = False
    return ok and digits != first * length


def find_cards(text: str) -> Iterator[tuple[int, int]]:
    """Card numbers written as one run of 13-19 digits, or in groups separated by
    single spaces or hyphens, the first of four digits and the rest of three to
    six (4111 1111 1111 1111, 3782-822463-10005)."""
    for match in _CARD_RUN.finditer(text):
        run = match.group()
        if len(run) < 13:
            continue
        base = match.start()
        groups = [group.span() for group in _GROUP.finditer(run)]
        index = 0
        while index < len(groups):
            start, end = groups[index]
            if end - start >= 13:
                if card_brand_ok(run[start:end]) and luhn_ok(run[start:end]):
                    yield base + start, base + end
                index += 1
                continue
            best = -1
            if end - start == 4:
                digits = run[start:end]
                for last in range(index + 1, len(groups)):
                    group_start, group_end = groups[last]
                    if not 3 <= group_end - group_start <= 6:
                        break
                    digits += run[group_start:group_end]
                    if len(digits) > 19:
                        break
                    if len(digits) >= 13 and card_brand_ok(digits) and luhn_ok(digits):
                        best = last
            if best >= 0:
                yield base + start, base + groups[best][1]
                index = best + 1
            else:
                index += 1


# ── US Social Security numbers ───────────────────────────────────────────────
#
# Only with separators (123-45-6789 or 123 45 6789): nine bare digits are too
# often something else. Areas 000, 666 and 900-999, group 00 and serial 0000
# are never issued.
_SSN = re.compile(r"[0-9](?<![0-9-][0-9])[0-9]{2}([- ])[0-9]{2}\1[0-9]{4}(?![0-9-])")


def find_ssns(text: str) -> Iterator[tuple[int, int]]:
    for match in _SSN.finditer(text):
        number = match.group()
        area, group, serial = number[:3], number[4:6], number[7:]
        if area not in ("000", "666") and area[0] != "9" and group != "00" and serial != "0000":
            yield match.span()


# ── IBANs ────────────────────────────────────────────────────────────────────

# Lengths by country from the IBAN registry (ISO 13616).
IBAN_LENGTHS = {
    "AD": 24, "AE": 23, "AL": 28, "AT": 20, "AZ": 28, "BA": 20, "BE": 16, "BG": 22, "BH": 22, "BI": 27,
    "BR": 29, "BY": 28, "CH": 21, "CR": 22, "CY": 28, "CZ": 24, "DE": 22, "DJ": 27, "DK": 18, "DO": 28,
    "EE": 20, "EG": 29, "ES": 24, "FI": 18, "FK": 18, "FO": 18, "FR": 27, "GB": 22, "GE": 22, "GI": 23,
    "GL": 18, "GR": 27, "GT": 28, "HN": 28, "HR": 21, "HU": 28, "IE": 22, "IL": 23, "IQ": 23, "IS": 26,
    "IT": 27, "JO": 30, "KW": 30, "KZ": 20, "LB": 28, "LC": 32, "LI": 21, "LT": 20, "LU": 20, "LV": 21,
    "LY": 25, "MC": 27, "MD": 24, "ME": 22, "MK": 19, "MN": 20, "MR": 27, "MT": 31, "MU": 30, "NI": 28,
    "NL": 18, "NO": 15, "OM": 23, "PK": 24, "PL": 28, "PS": 29, "PT": 25, "QA": 29, "RO": 24, "RS": 22,
    "RU": 33, "SA": 24, "SC": 31, "SD": 18, "SE": 24, "SI": 19, "SK": 24, "SM": 27, "SO": 23, "ST": 25,
    "SV": 28, "TL": 23, "TN": 24, "TR": 26, "UA": 29, "VA": 22, "VG": 24, "XK": 20, "YE": 30,
}
_IBAN_START = re.compile(r"[A-Za-z](?<![A-Za-z0-9][A-Za-z])[A-Za-z][0-9]{2}(?=[ ]?[A-Za-z0-9])")
# The rest of an IBAN of each length: letters and digits, with at most one
# space between two of them, and not run on into another letter or digit.
_IBAN_REST = {length: re.compile(r"(?:[ ]?[A-Za-z0-9]){%d}(?![A-Za-z0-9])" % (length - 4))
              for length in set(IBAN_LENGTHS.values())}


def iban_ok(compact: str) -> bool:
    """Whether an IBAN without spaces has its country's length and a valid mod-97 check."""
    compact = compact.upper()
    if IBAN_LENGTHS.get(compact[:2]) != len(compact) or not compact[2:4].isdigit():
        return False
    if not (compact.isascii() and compact.isalnum()):
        return False
    remainder = 0
    for character in compact[4:] + compact[:4]:
        value = int(character, 36)
        remainder = (remainder * (100 if value > 9 else 10) + value) % 97
    return remainder == 1


def find_ibans(text: str) -> Iterator[tuple[int, int]]:
    """IBANs, compact or printed in groups with single spaces (DE89 3704 0044 0532 0130 00)."""
    position = 0
    while True:
        start = _IBAN_START.search(text, position)
        if start is None:
            return
        length = IBAN_LENGTHS.get(start.group()[:2].upper())
        rest = _IBAN_REST[length].match(text, start.end()) if length else None
        if rest is not None and iban_ok(start.group() + rest.group().replace(" ", "")):
            yield start.start(), rest.end()
            position = rest.end()
        else:
            position = start.start() + 1


# ── Credentials (secret_scan's patterns) ─────────────────────────────────────

# Text every match of a secret_scan pattern contains, so a pattern whose text
# isn't there is skipped without scanning. A kind missing here always runs.
_SECRET_ANCHORS = {
    "private key": ("-----BEGIN ",),
    "AWS access key": ("AKIA", "ASIA"),
    "GitHub token": ("ghp_", "gho_", "ghu_", "ghs_", "ghr_", "github_pat_"),
    "GitLab token": ("glpat-",),
    "Slack token": ("xox",),
    "Slack webhook": ("hooks.slack.com",),
    "Stripe key": ("_live_",),
    "Anthropic key": ("sk-ant-",),
    "OpenAI key": ("sk-",),
    "Google API key": ("AIza",),
    "Hugging Face token": ("hf_",),
    "npm token": ("npm_",),
    "JSON web token": ("eyJ",),
    "password in a URL": ("://",),
    "secret in .env": ("=",),
}
# Patterns that ignore case: their anchors are looked for in lowercased text.
_SECRET_ANCHORS_ANY_CASE = {
    "AWS secret key": ("aws_secret_access_key",),
    "Azure storage key": ("accountkey=",),
}


def find_secrets(text: str) -> Iterator[tuple[int, int]]:
    """Credentials in the formats secret_scan knows; a match with a ``secret`` group
    covers only that group (the value in ``DB_PASSWORD=value``)."""
    from .secret_scan import PATTERNS

    lowered = None
    spans: list[tuple[int, int]] = []
    for kind, pattern in PATTERNS:
        anchors = _SECRET_ANCHORS.get(kind)
        if anchors is not None:
            if not any(anchor in text for anchor in anchors):
                continue
        elif kind in _SECRET_ANCHORS_ANY_CASE:
            lowered = text.lower() if lowered is None else lowered
            if not any(anchor in lowered for anchor in _SECRET_ANCHORS_ANY_CASE[kind]):
                continue
        has_secret = "secret" in pattern.groupindex
        for match in pattern.finditer(text):
            if has_secret and match.group("secret") is not None:
                spans.append(match.span("secret"))
            else:
                spans.append(match.span())
    # An Anthropic key is also an OpenAI-looking key: count each secret once.
    yield from merge_spans(spans)


def merge_spans(spans: Iterable[tuple[int, int]]) -> list[tuple[int, int]]:
    """Sorted spans with overlapping ones joined."""
    merged: list[tuple[int, int]] = []
    for start, end in sorted(spans):
        if merged and start < merged[-1][1]:
            if end > merged[-1][1]:
                merged[-1] = (merged[-1][0], end)
        else:
            merged.append((start, end))
    return merged


# ── Email addresses ──────────────────────────────────────────────────────────

_EMAIL = re.compile(
    r"[A-Za-z0-9._%+-](?<![A-Za-z0-9._%+-][A-Za-z0-9._%+-])[A-Za-z0-9._%+-]{0,63}@"
    r"[A-Za-z0-9-]{1,63}(?:\.[A-Za-z0-9-]{1,63}){0,8}\.[A-Za-z]{2,24}(?![A-Za-z0-9-])")


def find_emails(text: str) -> Iterator[tuple[int, int]]:
    if "@" not in text:
        return
    for match in _EMAIL.finditer(text):
        yield match.span()


DETECTORS: dict[str, Finder] = {
    "credit_card": find_cards,
    "us_ssn": find_ssns,
    "iban": find_ibans,
    "secrets": find_secrets,
    "email": find_emails,
}


# ── Keyword rules ────────────────────────────────────────────────────────────

_END = ""  # a key no character can be: a keyword ends at this node


def _is_word_character(character: str) -> bool:
    return character.isalnum() or character == "_"


def _emit(node: dict) -> str:
    """One trie node as a regex in which sibling branches start with different characters."""
    branches = [re.escape(character) + _emit(child)
                for character, child in sorted(node.items()) if character != _END]
    if _END in node:
        # Last, so a longer keyword through this node is preferred.
        branches.append(r"(?!\w)" if node[_END] else "")
    return branches[0] if len(branches) == 1 else "(?:" + "|".join(branches) + ")"


def keyword_pattern(words: Iterable[str], *, case_sensitive: bool, whole_word: bool) -> re.Pattern[str]:
    """One regex for literal keywords: a prefix tree, so a scan reads each character
    once per keyword prefix it could continue, not once per keyword.

    ``whole_word`` keeps "Falcon" from matching inside "Falconry": a keyword that
    starts or ends with a letter, digit or underscore needs a boundary there.
    """
    def fold(character: str) -> str:
        if case_sensitive:
            return character
        lower = character.lower()
        return lower if len(lower) == 1 else character

    trees: dict[bool, dict] = {True: {}, False: {}}
    for word in words:
        bounded_start = whole_word and _is_word_character(word[0])
        node = trees[bounded_start]
        for character in word:
            node = node.setdefault(fold(character), {})
        node[_END] = whole_word and _is_word_character(word[-1])
    parts = []
    if trees[True]:
        parts.append(r"(?<!\w)" + _emit(trees[True]))
    if trees[False]:
        parts.append(_emit(trees[False]))
    source = parts[0] if len(parts) == 1 else "(?:" + ")|(?:".join(parts) + ")"
    return re.compile(source, 0 if case_sensitive else re.IGNORECASE)


# ── Pattern rules: the bounded-regex check ──────────────────────────────────
#
# Character sets are sorted, disjoint (low, high) code point ranges. The check
# needs to know when two sets can't share a character, so every set here is
# an over-approximation of what the regex engine accepts (and, for negated
# classes, built from an under-approximation of what they exclude): a wrong
# answer can only make the check stricter, never let a slow pattern through.

_TOP = 0x10FFFF
_ALL = ((0, _TOP),)
_NONE: tuple = ()
_NON_ASCII = ((0x80, _TOP),)
_ASCII_LETTERS = ((0x41, 0x5A), (0x61, 0x7A))
_DIGITS = ((0x30, 0x39),)
_WORD = ((0x30, 0x39), (0x41, 0x5A), (0x5F, 0x5F), (0x61, 0x7A))
_SPACE_EXACT = ((0x09, 0x0D), (0x20, 0x20))
_SPACE_WIDE = ((0x09, 0x0D), (0x1C, 0x20))


def _normalize(spans: Iterable[tuple[int, int]]) -> tuple:
    merged: list[tuple[int, int]] = []
    for low, high in sorted(spans):
        if merged and low <= merged[-1][1] + 1:
            merged[-1] = (merged[-1][0], max(high, merged[-1][1]))
        else:
            merged.append((low, high))
    return tuple(merged)


def _union(*sets: tuple) -> tuple:
    return _normalize(span for spans in sets for span in spans)


def _overlaps(first: tuple, second: tuple) -> bool:
    i = j = 0
    while i < len(first) and j < len(second):
        if first[i][1] < second[j][0]:
            i += 1
        elif second[j][1] < first[i][0]:
            j += 1
        else:
            return True
    return False


def _complement(spans: tuple) -> tuple:
    gaps, low = [], 0
    for start, end in spans:
        if start > low:
            gaps.append((low, start - 1))
        low = end + 1
    if low <= _TOP:
        gaps.append((low, _TOP))
    return tuple(gaps)


class _Info(NamedTuple):
    ways: int        # combinations a backtracking match may try at one position
    low: int         # shortest match
    high: int        # longest match
    first: tuple     # characters a match can start with
    nullable: bool   # can match empty text


_SATURATE = 10 ** 12


def _multiply(a: int, b: int) -> int:
    return min(_SATURATE, a * b)


def _power(base: int, exponent: int) -> int:
    result = 1
    for _ in range(exponent):
        result = _multiply(result, base)
        if result in (0, 1, _SATURATE):
            break
    return result


class _Checker:
    def __init__(self) -> None:
        self._firsts: dict[int, tuple[tuple, bool]] = {}

    # Character sets: (under, over) approximations.
    @staticmethod
    def _cased(spans: tuple, flags: int) -> tuple:
        """``spans`` plus every character that case-insensitive matching treats as equal."""
        if not flags & re.IGNORECASE:
            return spans
        extra = []
        for low, high in spans:
            for start, end, shift in ((0x41, 0x5A, 32), (0x61, 0x7A, -32)):
                a, b = max(low, start), min(high, end)
                if a <= b:
                    extra.append((a + shift, b + shift))
        result = _union(spans, tuple(extra))
        if not flags & re.ASCII:
            # Kelvin sign, long s and dotted/dotless i fold to ASCII letters;
            # other letters fold among themselves outside ASCII.
            if _overlaps(spans, _ASCII_LETTERS):
                result = _union(result, _NON_ASCII)
            if _overlaps(spans, _NON_ASCII):
                result = _union(result, _NON_ASCII, _ASCII_LETTERS)
        return result

    @staticmethod
    def _category(code, flags: int) -> tuple[tuple, tuple]:
        ascii_only = bool(flags & re.ASCII)
        table = {
            _sre.CATEGORY_DIGIT: (_DIGITS, _DIGITS if ascii_only else _union(_DIGITS, _NON_ASCII)),
            _sre.CATEGORY_WORD: (_WORD, _WORD if ascii_only else _union(_WORD, _NON_ASCII)),
            _sre.CATEGORY_SPACE: (_SPACE_EXACT, _SPACE_EXACT if ascii_only else _union(_SPACE_WIDE, _NON_ASCII)),
        }
        negated = {
            _sre.CATEGORY_NOT_DIGIT: _sre.CATEGORY_DIGIT,
            _sre.CATEGORY_NOT_WORD: _sre.CATEGORY_WORD,
            _sre.CATEGORY_NOT_SPACE: _sre.CATEGORY_SPACE,
        }
        if code in table:
            return table[code]
        if code in negated:
            under, over = table[negated[code]]
            return _complement(over), _complement(under)
        return _NONE, _ALL

    def _unit(self, op, value, flags: int) -> tuple[tuple, tuple]:
        if op is _sre.LITERAL:
            return ((value, value),), self._cased(((value, value),), flags)
        if op is _sre.NOT_LITERAL:
            return _complement(self._cased(((value, value),), flags)), _complement(((value, value),))
        if op is _sre.ANY:
            return _NONE, _ALL
        if op is _sre.RANGE:
            low, high = value
            return ((low, high),), self._cased(((low, high),), flags)
        if op is _sre.CATEGORY:
            return self._category(value, flags)
        if op is _sre.IN:
            negate = False
            unders, overs = [], []
            for item_op, item_value in value:
                if item_op is _sre.NEGATE:
                    negate = True
                    continue
                under, over = self._unit(item_op, item_value, flags)
                unders.append(under)
                overs.append(over)
            under, over = _union(*unders), _union(*overs)
            return (_complement(over), _complement(under)) if negate else (under, over)
        return _NONE, _ALL

    # First characters and emptiness, without follow sets (memoized per subpattern).
    def _first(self, items, flags: int) -> tuple[tuple, bool]:
        key = id(items)
        if key in self._firsts:
            return self._firsts[key]
        first: tuple = _NONE
        nullable = True
        for op, value in items:
            item_first, item_nullable = self._item_first(op, value, flags)
            first = _union(first, item_first)
            if not item_nullable:
                nullable = False
                break
        self._firsts[key] = (first, nullable)
        return first, nullable

    def _item_first(self, op, value, flags: int) -> tuple[tuple, bool]:
        if op in (_sre.LITERAL, _sre.NOT_LITERAL, _sre.ANY, _sre.IN):
            return self._unit(op, value, flags)[1], False
        if op is _sre.SUBPATTERN:
            _group, add, remove, sub = value
            return self._first(sub.data, (flags | add) & ~remove)
        if op is _sre.ATOMIC_GROUP:
            return self._first(value.data, flags)
        if op is _sre.BRANCH:
            results = [self._first(alternative.data, flags) for alternative in value[1]]
            return _union(*(first for first, _ in results)), any(nullable for _, nullable in results)
        if op in (_sre.MAX_REPEAT, _sre.MIN_REPEAT, _sre.POSSESSIVE_REPEAT):
            low, _high, sub = value
            first, nullable = self._first(sub.data, flags)
            return first, nullable or low == 0
        return _NONE, True  # anchors and lookarounds consume nothing

    # The walk: ``follow`` is what may come right after this part of the pattern.
    def sequence(self, items, follow: tuple, flags: int) -> _Info:
        infos = []
        after = follow
        for op, value in reversed(list(items)):
            info = self.node(op, value, after, flags)
            infos.append(info)
            after = _union(info.first, after) if info.nullable else info.first
        infos.reverse()
        ways, low, high, first, nullable = 1, 0, 0, _NONE, True
        for info in infos:
            ways = _multiply(ways, info.ways)
            low += info.low
            high += info.high
            if nullable:
                first = _union(first, info.first)
                nullable = info.nullable
        return _Info(ways, low, high, first, nullable)

    def node(self, op, value, follow: tuple, flags: int) -> _Info:
        if op in (_sre.LITERAL, _sre.NOT_LITERAL, _sre.ANY, _sre.IN):
            return _Info(1, 1, 1, self._unit(op, value, flags)[1], False)
        if op is _sre.AT:
            return _Info(1, 0, 0, _NONE, True)
        if op is _sre.SUBPATTERN:
            _group, add, remove, sub = value
            return self.sequence(sub.data, follow, (flags | add) & ~remove)
        if op is _sre.ATOMIC_GROUP:
            inner = self.sequence(value.data, _NONE, flags)
            self.bounded(inner)
            return inner._replace(ways=1)  # never backtracked into
        if op is _sre.BRANCH:
            infos = [self.sequence(alternative.data, follow, flags) for alternative in value[1]]
            first = _union(*(info.first for info in infos))
            nullable = any(info.nullable for info in infos)
            distinct = not nullable and all(
                not _overlaps(a.first, b.first) for index, a in enumerate(infos) for b in infos[index + 1:])
            # Alternatives that start with different characters: at most one
            # gets past its first character.
            ways = max(info.ways for info in infos) if distinct else sum(info.ways for info in infos)
            return _Info(min(ways, _SATURATE), min(info.low for info in infos),
                         max(info.high for info in infos), first, nullable)
        if op in (_sre.MAX_REPEAT, _sre.MIN_REPEAT, _sre.POSSESSIVE_REPEAT):
            low, high, sub = value
            if high == _sre.MAXREPEAT:
                raise PatternError("repeats must have a limit: write {0,100} instead of *, "
                                   "{1,100} instead of + and {n,m} instead of {n,}")
            body_first, _nullable = self._first(sub.data, flags)
            body = self.sequence(sub.data, _union(body_first, follow) if high > 1 else follow, flags)
            if op is _sre.POSSESSIVE_REPEAT:
                self.bounded(body)
                ways = 1
            else:
                # Giving back a repeat's characters helps only if what follows
                # can start with one of them.
                competing = body.nullable or _overlaps(body.first, follow)
                ways = _multiply(high - low + 1 if competing else 1, _power(body.ways, high))
            return _Info(ways, low * body.low, high * body.high, body.first, low == 0 or body.nullable)
        if op in (_sre.ASSERT, _sre.ASSERT_NOT):
            self.bounded(self.sequence(value[1].data, _NONE, flags))
            return _Info(1, 0, 0, _NONE, True)
        if op in (_sre.GROUPREF, _sre.GROUPREF_EXISTS):
            raise PatternError("backreferences and conditional groups (\\1, (?P=name), (?(1)...)) "
                               "aren't supported")
        raise PatternError(f"uses a construct Lumi can't check ({op})")

    @staticmethod
    def bounded(info: _Info) -> None:
        if info.high > MAX_MATCH_WIDTH:
            raise PatternError(f"can match more than {MAX_MATCH_WIDTH} characters; lower its repeat limits")
        if info.ways > MAX_WAYS:
            raise PatternError("has repeats that compete for the same characters, which makes scans slow; "
                               "separate them with a character they can't match, or lower their limits")
        if info.ways * max(1, info.high) > MAX_COST:
            raise PatternError("could take too long on some text: shorten its longest match or make its "
                               "repeats more specific")


def _parse(pattern: str, case_sensitive: bool):
    flags = 0 if case_sensitive else re.IGNORECASE
    try:
        compiled = re.compile(pattern, flags)
        tree = _sre_parse.parse(pattern, flags)
    except (re.error, RecursionError, OverflowError) as exc:
        raise PatternError(f"isn't a valid regular expression ({exc})") from exc
    info = _Checker().sequence(tree.data, _NONE, tree.state.flags)
    _Checker.bounded(info)
    if info.nullable or info.low == 0:
        raise PatternError("can match empty text")
    return compiled, tree


def check_pattern(pattern: str, *, case_sensitive: bool) -> re.Pattern[str]:
    """Compile an administrator's pattern if it scans in bounded time per character.

    Raises PatternError naming the problem otherwise.
    """
    return _parse(pattern, case_sensitive)[0]


def _required_literal(tree) -> str:
    """The longest run of plain characters every match contains, or ''."""
    best = current = ""
    for op, value in tree.data:
        if op is _sre.LITERAL:
            current += chr(value)
            if len(current) > len(best):
                best = current
        else:
            current = ""
    return best if len(best) >= 2 else ""


def pattern_finder(pattern: str, *, case_sensitive: bool) -> Finder:
    """A checked pattern rule's finder (PatternError if the pattern isn't allowed).

    Text without the pattern's fixed characters (``.corp.example.com`` in
    ``[a-z0-9-]{1,63}\\.corp\\.example\\.com``) is skipped without a regex scan.
    Ignoring case, that shortcut is taken only for ASCII text and characters,
    where lowercasing agrees with the regex engine.
    """
    compiled, tree = _parse(pattern, case_sensitive)
    literal = _required_literal(tree)
    ignore_case = bool(tree.state.flags & re.IGNORECASE)
    if ignore_case and not literal.isascii():
        literal = ""
    folded = literal.lower()

    def find(text: str) -> Iterator[tuple[int, int]]:
        if literal:
            if not ignore_case:
                if literal not in text:
                    return
            elif text.isascii() and folded not in text.lower():
                return
        for match in compiled.finditer(text):
            if match.end() > match.start():
                yield match.span()
    return find
