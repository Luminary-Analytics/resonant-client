"""Files the agent may never read, list or send.

Exclusion rules are gitignore-style patterns, relative to the project root:

* ``.env``, ``*.pem``, ``id_rsa*`` — a pattern without a slash matches that
  name at any depth;
* ``secrets/**``, ``/config/prod.yaml`` — a pattern containing a slash is
  anchored at the project root (a leading slash is optional);
* ``**`` spans directories, ``*`` and ``?`` stay within one name, and a
  trailing slash (``build/``) matches a directory. Excluding a directory
  excludes everything in it.

Rules come from Settings > Privacy & security (``privacy.excluded_paths``),
the project's ``.lumiignore`` file and organization policy. A ``.lumiignore``
only adds restrictions, so it is honored without trusting the project.
Negated patterns (``!keep.txt``) are not supported and are ignored.

Excluded files are refused by the file tools and left out of search results,
git diffs, the codebase index and attachments. Shell commands can still reach
them: pair exclusions with command rules and the secret scan.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Callable, Iterable

IGNORE_FILE = ".lumiignore"

# Offered by Settings as a starting point; nothing is excluded by default.
COMMON_SECRET_PATTERNS = (
    ".env", ".env.*", "*.pem", "*.key", "*.p12", "*.pfx",
    "id_rsa*", "id_ecdsa*", "id_ed25519*", ".npmrc", ".pypirc", ".netrc",
)


@dataclass(frozen=True)
class Rule:
    pattern: str
    source: str
    regex: re.Pattern[str]
    anchored: bool
    body: str
    directory_only: bool

    def describe(self) -> str:
        return f"'{self.pattern}' ({self.source})"


def _translate(body: str) -> str:
    """Regex for a glob body: ``**`` spans directories, ``*``/``?`` don't."""
    out: list[str] = []
    i = 0
    while i < len(body):
        char = body[i]
        if body.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif body.startswith("/**", i) and i + 3 == len(body):
            out.append("(?:/.*)?")
            i += 3
        elif body.startswith("**", i):
            out.append(".*")
            i += 2
        elif char == "*":
            out.append("[^/]*")
            i += 1
        elif char == "?":
            out.append("[^/]")
            i += 1
        elif char == "[":
            end = body.find("]", i + 2)
            if end == -1:
                out.append(re.escape(char))
                i += 1
            else:
                inner = body[i + 1:end]
                if inner.startswith("!"):
                    inner = "^" + inner[1:]
                out.append("[" + inner.replace("\\", "\\\\") + "]")
                i = end + 1
        else:
            out.append(re.escape(char))
            i += 1
    return "".join(out)


def compile_rule(pattern: str, source: str) -> Rule | None:
    """A rule for one pattern, or None for blank lines, comments and negations."""
    text = str(pattern or "").strip().replace("\\", "/")
    if not text or text.startswith("#") or text.startswith("!"):
        return None
    directory_only = text.endswith("/")
    body = text.strip("/")
    if not body:
        return None
    anchored = text.startswith("/") or "/" in body
    prefix = "^" if anchored else "^(?:.*/)?"
    # A matched directory excludes its contents; a directory-only pattern must
    # match a directory, which for a file path means something follows it.
    suffix = "/.*$" if directory_only else "(?:/.*)?$"
    flags = re.IGNORECASE if os.name == "nt" else 0
    return Rule(text, source, re.compile(prefix + _translate(body) + suffix, flags), anchored, body, directory_only)


def read_ignore_file(root: str) -> list[str]:
    """Patterns from the project's .lumiignore (empty when there is none)."""
    try:
        with open(os.path.join(root, IGNORE_FILE), encoding="utf-8") as handle:
            return handle.read().splitlines()
    except (OSError, UnicodeDecodeError):
        return []


class ExclusionRules:
    """Matches paths against every configured exclusion rule for one project.

    Built with ``for_project``, the rules stay current: the project's
    .lumiignore is re-read when it changes on disk, and the Settings and
    policy sources, when given as callables, are asked again on each check.
    """

    def __init__(
        self,
        root: str = "",
        rules: Iterable[tuple[str, str]] = (),
        *,
        watch_ignore_file: bool = False,
        sources: Iterable[tuple[Callable[[], Iterable[str]], str]] = (),
    ):
        self.root = os.path.normcase(os.path.realpath(root)) if root else ""
        self._fixed = list(rules)
        self._sources = list(sources)
        self._ignore_path = os.path.join(root, IGNORE_FILE) if (root and watch_ignore_file) else ""
        self._state: tuple = ()
        self.rules: list[Rule] = []
        self._refresh()

    @classmethod
    def for_project(
        cls,
        root: str,
        *,
        settings_patterns: Iterable[str] | Callable[[], Iterable[str]] = (),
        policy_patterns: Iterable[str] | Callable[[], Iterable[str]] = (),
    ) -> "ExclusionRules":
        sources: list[tuple[Callable[[], Iterable[str]], str]] = []
        for patterns, label in ((policy_patterns, "organization policy"), (settings_patterns, "Settings")):
            if callable(patterns):
                sources.append((patterns, label))
            else:
                fixed = list(patterns)
                sources.append(((lambda fixed=fixed: fixed), label))
        return cls(root, watch_ignore_file=True, sources=sources)

    def _stamp(self) -> tuple[int, int] | None:
        if not self._ignore_path:
            return None
        try:
            stat = os.stat(self._ignore_path)
        except OSError:
            return None
        return stat.st_mtime_ns, stat.st_size

    def _refresh(self) -> None:
        """Rebuild when a source or the .lumiignore changed (cheap when nothing did).

        Loops over many paths refresh once and then use ``checker()``.
        """
        dynamic = []
        for source, label in self._sources:
            try:
                patterns = tuple(str(p) for p in (source() or ()) if isinstance(p, str))
            except Exception:
                patterns = ()
            dynamic.append((patterns, label))
        stamp = self._stamp()
        state = (tuple(dynamic), stamp)
        if self._state and state == self._state:
            return
        ignore = read_ignore_file(os.path.dirname(self._ignore_path)) if stamp else []
        ordered = [*self._fixed]
        for patterns, label in dynamic:
            ordered += [(p, label) for p in patterns]
        ordered += [(p, IGNORE_FILE) for p in ignore]
        rules: list[Rule] = []
        seen: set[str] = set()
        for pattern, source in ordered:
            rule = compile_rule(pattern, source)
            if rule and rule.pattern not in seen:
                seen.add(rule.pattern)
                rules.append(rule)
        self.rules = rules
        self._state = state

    def __bool__(self) -> bool:
        self._refresh()
        return bool(self.rules)

    def relative(self, path: str) -> str | None:
        """The path relative to the project root with forward slashes, or None outside it."""
        if not self.root:
            return None
        full = os.path.normcase(os.path.realpath(os.path.abspath(path)))
        try:
            rel = os.path.relpath(full, self.root)
        except ValueError:  # another drive
            return None
        if rel == os.curdir or rel.startswith(os.pardir):
            return None
        return rel.replace(os.sep, "/")

    def match(self, path: str) -> Rule | None:
        """The first rule that excludes ``path``, or None."""
        self._refresh()
        return self._match(path)

    def checker(self) -> Callable[[str], bool]:
        """``is_excluded`` for a loop: sources are checked once, up front."""
        self._refresh()
        return lambda path: self._match(path) is not None

    def _match(self, path: str) -> Rule | None:
        if not self.rules or not path:
            return None
        rel = self.relative(path)
        if rel is None:
            # Outside the project, only unanchored rules (names at any depth) apply.
            candidate = os.path.abspath(path).replace(os.sep, "/").lstrip("/")
            return next((r for r in self.rules if not r.anchored and r.regex.match(candidate)), None)
        return next((r for r in self.rules if r.regex.match(rel)), None)

    def is_excluded(self, path: str) -> bool:
        return self.match(path) is not None

    def match_tree(self, path: str) -> Rule | None:
        """A rule excluding ``path`` or, for a folder, everything in it.

        Used for search roots: ``build/`` excludes the folder's contents,
        which a search rooted at ``build`` would otherwise read.
        """
        return self.match(path) or self.match(os.path.join(path, "_"))

    def filter_paths(self, paths: Iterable[str]) -> tuple[list[str], int]:
        """Paths that are not excluded, and how many were removed."""
        items = list(paths)
        excluded = self.checker()
        kept = [path for path in items if not excluded(path)]
        return kept, len(items) - len(kept)

    def git_pathspecs(self) -> list[str]:
        """``git`` pathspecs that leave excluded files out, relative to the project root."""
        self._refresh()
        specs: list[str] = []
        for rule in self.rules:
            base = rule.body if rule.anchored else "**/" + rule.body
            if not rule.directory_only:
                specs.append(":(exclude,glob)" + base)
            specs.append(":(exclude,glob)" + base + "/**")
        return specs

    def refusal(self, path: str, rule: Rule) -> str:
        """The message a tool returns for an excluded file."""
        shown = self.relative(path) or path
        return (
            f"'{shown}' is excluded by {rule.describe()}. Lumi does not read, "
            "list or send excluded files; ask the user to share what you need."
        )
