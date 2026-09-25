"""
RAG (Retrieval-Augmented Generation) for the Lumi engine.

Indexes project codebases for semantic search, enabling the model to
quickly find relevant files and code without manual glob/grep exploration.

Two modes:
1. Local index: Fast file-path + content hashing with keyword search
2. Engram-backed: Full semantic embeddings via engram server

The local index is always available. Engram enhances it with semantic search.
"""

import hashlib
import heapq
import json
import logging
import os
import re
import shutil
import stat
import subprocess
import threading
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from ..paths import project_dir

logger = logging.getLogger(__name__)


# ── File Type Configuration ──────────────────────────────────────────

# Extensions to index
INDEXABLE_EXTENSIONS = {
    ".py", ".js", ".ts", ".jsx", ".tsx", ".java", ".go", ".rs", ".c", ".cpp",
    ".h", ".hpp", ".cs", ".rb", ".php", ".swift", ".kt", ".scala", ".lua",
    ".sh", ".bash", ".zsh", ".ps1", ".bat", ".cmd",
    ".html", ".css", ".scss", ".less", ".vue", ".svelte",
    ".json", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf",
    ".md", ".rst", ".txt",
    ".sql", ".graphql", ".proto",
    ".dockerfile", ".tf", ".hcl",
    ".r", ".jl", ".m",  # R, Julia, MATLAB/Objective-C
}

# Directories to always skip
SKIP_DIRS = {
    ".git", ".svn", ".hg",
    "node_modules", "__pycache__", ".pytest_cache", ".mypy_cache",
    ".tox", ".nox", ".venv", "venv", "env",
    "dist", "build", "target", "out", "bin", "obj",
    ".next", ".nuxt", ".output",
    ".resonant-worktrees", ".lumi-worktrees",
    "vendor", "third_party",
    ".idea", ".vscode",
    "coverage", "htmlcov",
}

# Max file size to index (256KB)
MAX_FILE_SIZE = 256 * 1024

# Files indexed per project. Past this, the rest of a very large monorepo is
# left out (the stats say "truncated") rather than making every index slow.
MAX_INDEXED_FILES = 100_000

# An import that matches more files than this is too ambiguous to count.
AMBIGUOUS_IMPORT_TARGETS = 3

# Files read concurrently while indexing, and how many are held at once.
READ_THREADS = 8
READ_BATCH = 256

# Extensionless files worth indexing.
EXTENSIONLESS = {"makefile", "dockerfile", "procfile", "gemfile", "rakefile", "vagrantfile", "jenkinsfile"}


@dataclass
class IndexEntry:
    """A single indexed file."""
    path: str           # Relative path from project root
    language: str       # Detected language
    size: int           # File size in bytes
    lines: int          # Line count
    hash: str           # Content hash for change detection
    symbols: list[str] = field(default_factory=list)  # Functions, classes, etc.
    summary: str = ""   # Brief content summary
    imports: list[str] = field(default_factory=list)   # Import statements
    last_indexed: float = 0.0
    mtime_ns: int = 0  # Filesystem identity for fast unchanged-file detection
    # Lower-cased search fields, built on first search (not saved).
    _search: tuple | None = field(default=None, init=False, repr=False, compare=False)

    def search_fields(self) -> tuple:
        """(path, path parts, symbols, imports, summary, language), lower-cased once."""
        if self._search is None:
            path_lower = self.path.lower()
            parts = {part for part in re.split(r'[\s_.\-/]+', path_lower) if part}
            self._search = (path_lower, parts, [s.lower() for s in self.symbols], " ".join(self.imports).lower(),
                            self.summary.lower(), self.language.lower())
        return self._search

    def to_dict(self) -> dict:
        return {
            "path": self.path,
            "language": self.language,
            "size": self.size,
            "lines": self.lines,
            "symbols": self.symbols,
            "summary": self.summary,
            "imports": self.imports,
        }


@dataclass
class SearchResult:
    """A search result from the index."""
    path: str
    score: float        # 0.0 to 1.0 relevance
    context: str        # Why this file matched
    language: str = ""
    symbols: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "path": self.path,
            "score": self.score,
            "context": self.context,
            "language": self.language,
            "symbols": self.symbols,
        }


class CodebaseIndex:
    """Indexes a project codebase for fast retrieval.

    Provides two types of search:
    - Keyword search: matches file paths, symbols, and content
    - Semantic search: uses engram embeddings (when available)
    """

    def __init__(self, project_path: str | Path, engram=None, *, max_files: int = MAX_INDEXED_FILES):
        self.project_path = Path(project_path)
        self.max_files = max(1, int(max_files))
        self._engram = engram  # Optional EngramIntegration
        self._entries: dict[str, IndexEntry] = {}  # rel_path -> entry
        self._lock = threading.Lock()
        self._indexing = False
        self._last_full_index: float = 0.0
        self._index_file = project_dir(self.project_path) / "index.json"
        self._repo_map_cache: dict[tuple, str] = {}
        self._repo_map_generation = 0
        # engine/exclusions.ExclusionRules, set by the app: excluded files are
        # neither indexed nor returned, even from an older cached index.
        self.exclusions = None

        # Try loading cached index
        self._load_cache()

    @property
    def file_count(self) -> int:
        return len(self._entries)

    @property
    def is_indexed(self) -> bool:
        return self._last_full_index > 0

    @property
    def is_indexing(self) -> bool:
        return self._indexing

    def index(self, force: bool = False) -> dict:
        """Index the entire codebase. Returns stats.

        In a Git repository the files come from ``git ls-files`` (tracked and
        untracked, without ignored ones), so ``.gitignore`` keeps generated
        trees out; elsewhere the folder is walked. Each changed file is read
        once and parsed once. Unchanged files (same size and modification
        time) aren't read at all.

        Args:
            force: If True, re-index all files even if unchanged.
        """
        if self._indexing:
            return {"status": "already_indexing"}

        self._indexing = True
        start = time.time()
        stats = {"files_scanned": 0, "files_indexed": 0, "files_skipped": 0, "errors": 0}
        current_paths: set[str] = set()
        todo: list[tuple[str, str, int, int]] = []
        changed = False

        try:
            listing, stats["listing"] = self._list_files()
            for rel_path in listing:
                stats["files_scanned"] += 1
                if not _indexable(rel_path):
                    stats["files_skipped"] += 1
                    continue
                full_path = os.path.join(self.project_path, rel_path)
                if self._excluded_path(full_path):
                    stats["files_skipped"] += 1
                    continue
                try:
                    file_stat = os.stat(full_path)
                except OSError:
                    stats["files_skipped"] += 1
                    continue
                size = file_stat.st_size
                # Regular files only (a submodule is a directory); skip empty and large ones.
                if not stat.S_ISREG(file_stat.st_mode) or size > MAX_FILE_SIZE or size == 0:
                    stats["files_skipped"] += 1
                    continue
                if len(current_paths) >= self.max_files:
                    stats["truncated"] = True
                    break
                current_paths.add(rel_path)

                existing = self._entries.get(rel_path)
                if not force and existing and existing.size == size and existing.mtime_ns == file_stat.st_mtime_ns:
                    continue
                todo.append((rel_path, full_path, size, file_stat.st_mtime_ns))

            # Read changed files a batch at a time on a few threads (opening a
            # file is I/O, and on Windows often an antivirus scan, so it
            # overlaps well), then parse them in order on this thread.
            with ThreadPoolExecutor(max_workers=READ_THREADS) as pool:
                for batch_start in range(0, len(todo), READ_BATCH):
                    batch = todo[batch_start:batch_start + READ_BATCH]
                    for (rel_path, _full, size, mtime_ns), data in zip(batch, pool.map(_read_bytes, batch)):
                        if data is None:
                            stats["errors"] += 1
                            continue
                        existing = self._entries.get(rel_path)
                        content_hash = hashlib.md5(data).hexdigest()[:12]
                        if not force and existing and existing.hash == content_hash:
                            existing.size = size
                            existing.mtime_ns = mtime_ns
                            changed = True
                            continue
                        try:
                            entry = self._index_content(rel_path, data.decode("utf-8", errors="replace"),
                                                        content_hash, size=size, mtime_ns=mtime_ns)
                        except Exception as e:
                            logger.debug(f"Failed to index {rel_path}: {e}")
                            stats["errors"] += 1
                            continue
                        with self._lock:
                            self._entries[rel_path] = entry
                        stats["files_indexed"] += 1
                        changed = True

            self._last_full_index = time.time()
            if stats.get("truncated"):
                logger.warning("Indexed the first %d files of %s; the rest of the repository is left out",
                               self.max_files, self.project_path)

            # Remove entries for deleted, excluded, empty, or newly oversized
            # files using paths collected during the primary walk.
            with self._lock:
                stale = [p for p in self._entries if p not in current_paths]
                for p in stale:
                    del self._entries[p]
                if stale:
                    stats["files_removed"] = len(stale)
                if stats["files_indexed"] or stale or changed:
                    self._repo_map_cache.clear()
                    self._repo_map_generation += 1

            # Save the cache only when something changed: for a large
            # repository it is megabytes of JSON.
            if changed or stale or not self._index_file.exists():
                self._save_cache()

            # Push to engram if available
            if self._engram and self._engram.enabled:
                self._push_to_engram()

        finally:
            self._indexing = False
            stats["elapsed_ms"] = int((time.time() - start) * 1000)
            stats["total_files"] = len(self._entries)

        logger.info(f"Indexed {stats['files_indexed']} files in {stats['elapsed_ms']}ms "
                     f"({stats['total_files']} total)")
        return stats

    def _list_files(self) -> tuple[Iterator[str], str]:
        """Relative paths to consider, and how they were found ("git" or "walk")."""
        listed = self._git_files()
        if listed is not None:
            return (path for path in listed if _visible(path)), "git"
        return self._walk_files(), "walk"

    def _git_files(self) -> list[str] | None:
        """Tracked and untracked files that .gitignore doesn't exclude, or None outside Git.

        The project can be a folder inside a repository (one part of a
        monorepo); ``git ls-files`` then lists that folder, relative to it.
        """
        if _repository_root(self.project_path) is None or not shutil.which("git"):
            return None
        try:
            result = subprocess.run(
                ["git", "-c", "core.quotepath=off", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
                cwd=self.project_path, capture_output=True, timeout=120,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except (OSError, subprocess.SubprocessError):
            return None
        if result.returncode != 0:
            return None
        # --cached and --others can both list a file that was just staged.
        return list(dict.fromkeys(p for p in result.stdout.decode("utf-8", "replace").split("\0") if p))

    def _walk_files(self) -> Iterator[str]:
        for dirpath, dirnames, filenames in os.walk(self.project_path):
            # Skip excluded directories
            dirnames[:] = [
                d for d in dirnames
                if d not in SKIP_DIRS and not d.startswith(".")
                # A directory rule excludes everything inside it.
                and not self._excluded_path(os.path.join(dirpath, d, "_"))
            ]
            for filename in filenames:
                yield os.path.relpath(os.path.join(dirpath, filename), self.project_path).replace("\\", "/")

    def search(self, query: str, max_results: int = 10) -> list[SearchResult]:
        """Search the index for relevant files.

        Combines keyword matching with optional semantic search.
        """
        results = []

        # Keyword search (always available)
        keyword_results = self._keyword_search(query, max_results * 2)
        results.extend(keyword_results)

        # Semantic search via engram (if available)
        if self._engram and self._engram.enabled:
            semantic_results = self._semantic_search(query, max_results)
            # Merge: boost files found by both methods
            seen = {r.path: r for r in results}
            for sr in semantic_results:
                if sr.path in seen:
                    # Boost score for files found by both methods
                    seen[sr.path].score = min(1.0, seen[sr.path].score + sr.score * 0.5)
                else:
                    results.append(sr)

        results = [r for r in results if not self._excluded_rel(r.path)]
        # Sort by score descending
        results.sort(key=lambda r: r.score, reverse=True)
        return results[:max_results]

    def get_context_for_prompt(self, query: str, max_files: int = 5) -> str:
        """Return a compact repo map plus query-specific file candidates."""
        if not self.is_indexed:
            return ""

        results = self.search(query, max_results=max_files)
        lines = ["\n\n" + self.get_repo_map(max_tokens=700)]
        if results:
            lines.append("--- RELEVANT FILES ---")
            for r in results:
                symbols_str = f" [{', '.join(r.symbols[:5])}]" if r.symbols else ""
                lines.append(f"- {r.path}{symbols_str}: {r.context}")
            lines.append("--- END RELEVANT FILES ---")
        return "\n".join(lines)

    def get_repo_map(self, max_tokens: int = 1_000) -> str:
        """Build a deterministic, dependency-weighted symbol orientation map.

        This uses the index's existing symbol/import data, so it is intentionally
        lightweight rather than a full AST renderer.  Files referenced by many
        peers rank first, followed by shallow entrypoints and symbol-rich files.
        """
        # The active exclusion rules are part of the key: adding one must not
        # leave its files in a map built before.
        rules = tuple(r.pattern for r in self.exclusions.rules) if self.exclusions else ()
        cache_key = (max(100, int(max_tokens)), rules)
        with self._lock:
            cached = self._repo_map_cache.get(cache_key)
            if cached is not None:
                return cached
            entries = [e for e in self._entries.values() if not self._excluded_rel(e.path)]
            generation = self._repo_map_generation
        if not entries:
            return ""

        # Every file is known by each suffix of its path without the extension,
        # with slashes or dots ("pkg/util", "util", "pkg.util"...). An import
        # counts for the files matching its most specific suffix only. Counting
        # every suffix, as before, credited all of a monorepo's many index.ts
        # or utils.py for each import of one of them, which grew quadratically
        # (about a minute for 100,000 files).
        identity_targets: dict[str, list[str]] = {}
        for entry in entries:
            no_ext = os.path.splitext(entry.path)[0].replace("\\", "/").lower()
            parts = no_ext.split("/")
            for index in range(len(parts)):
                suffix = parts[index:]
                for name in {"/".join(suffix), ".".join(suffix)}:
                    identity_targets.setdefault(name, []).append(entry.path)

        inbound = {entry.path: 0 for entry in entries}
        for source in entries:
            for raw_import in source.imports:
                imported = raw_import.strip("./").replace("\\", "/").lower()
                pieces = [piece for piece in imported.replace(".", "/").split("/") if piece]
                for index in range(len(pieces)):
                    suffix = pieces[index:]
                    targets = (identity_targets.get("/".join(suffix))
                               or identity_targets.get(".".join(suffix)))
                    if not targets:
                        continue
                    # A bare name shared by many files says nothing about which.
                    if len(targets) <= AMBIGUOUS_IMPORT_TARGETS:
                        for target in targets:
                            if target != source.path:
                                inbound[target] += 1
                    break

        def rank(entry: IndexEntry) -> tuple[float, str]:
            depth = entry.path.count("/")
            filename = entry.path.rsplit("/", 1)[-1].lower()
            entrypoint = 3 if filename in {
                "readme.md", "pyproject.toml", "package.json", "main.py", "app.py",
                "main.ts", "main.js", "cargo.toml", "go.mod",
            } else 0
            score = inbound[entry.path] * 10 + entrypoint + len(entry.symbols) * 0.08 - depth * 0.2
            return (-score, entry.path.lower())

        lines = ["--- REPO MAP (dependency-weighted, signatures only) ---"]
        budget_chars = max(400, int(max_tokens) * 4)
        used_chars = len(lines[0]) + 1
        # Only the top of the ranking fits the budget; don't sort every file
        # of a large repository to find it. A line is at least ~20 characters.
        for entry in heapq.nsmallest(budget_chars // 20 + 1, entries, key=rank):
            symbols = ", ".join(entry.symbols[:8]) or "(no indexed symbols)"
            line = f"- {entry.path}: {symbols}"
            if inbound[entry.path]:
                line += f" [referenced by {inbound[entry.path]} file(s)]"
            if used_chars + len(line) > budget_chars:
                break
            lines.append(line)
            used_chars += len(line) + 1
        lines.append("--- END REPO MAP ---")
        result = "\n".join(lines)
        with self._lock:
            # Do not let an in-flight build overwrite an invalidation from a
            # concurrent background reindex.
            if generation == self._repo_map_generation:
                self._repo_map_cache[cache_key] = result
        return result

    def _excluded_path(self, full_path: str) -> bool:
        return bool(self.exclusions) and self.exclusions.is_excluded(full_path)

    def _excluded_rel(self, rel_path: str) -> bool:
        return self._excluded_path(os.path.join(str(self.project_path), rel_path))

    def get_stats(self) -> dict:
        """Return index statistics."""
        with self._lock:
            langs = {}
            total_lines = 0
            for entry in self._entries.values():
                langs[entry.language] = langs.get(entry.language, 0) + 1
                total_lines += entry.lines

            return {
                "total_files": len(self._entries),
                "total_lines": total_lines,
                "languages": langs,
                "last_indexed": self._last_full_index,
                "is_indexing": self._indexing,
                "project_path": str(self.project_path),
            }

    # ── Internal: File Indexing ──────────────────────────────────────

    def _index_file_content(
        self,
        full_path: str,
        rel_path: str,
        content_hash: str,
        *,
        file_size: int | None = None,
        mtime_ns: int = 0,
    ) -> IndexEntry:
        """Index a single file's content, reading it from disk."""
        try:
            with open(full_path, "r", encoding="utf-8", errors="replace") as f:
                content = f.read()
        except Exception:
            return IndexEntry(
                path=rel_path,
                language=_detect_language(rel_path),
                size=0, lines=0, hash=content_hash,
                last_indexed=time.time(),
                mtime_ns=mtime_ns,
            )
        return self._index_content(rel_path, content, content_hash, size=file_size, mtime_ns=mtime_ns)

    def _index_content(self, rel_path: str, content: str, content_hash: str, *, size: int | None = None,
                       mtime_ns: int = 0) -> IndexEntry:
        """Index content already read: one parse gives both symbols and imports."""
        lines = content.count("\n") + (1 if content and not content.endswith("\n") else 0)
        language = _detect_language(rel_path)
        file_size = size
        symbols, imports = _extract_symbols_and_imports(content, language)

        # Build a brief summary
        summary = _build_summary(rel_path, language, symbols, imports, lines)

        return IndexEntry(
            path=rel_path,
            language=language,
            size=file_size if file_size is not None else len(content.encode("utf-8")),
            lines=lines,
            hash=content_hash,
            symbols=symbols,
            imports=imports,
            summary=summary,
            last_indexed=time.time(),
            mtime_ns=mtime_ns,
        )

    def _hash_file(self, path: str) -> str:
        """Quick content hash for change detection."""
        h = hashlib.md5()
        with open(path, "rb") as f:
            while chunk := f.read(8192):
                h.update(chunk)
        return h.hexdigest()[:12]

    # ── Internal: Search ─────────────────────────────────────────────

    def _keyword_search(self, query: str, max_results: int) -> list[SearchResult]:
        """Simple keyword-based search across the index."""
        query_lower = query.lower()
        terms = set(re.split(r'[\s_.\-/]+', query_lower))
        terms.discard("")

        results = []
        with self._lock:
            for path, entry in self._entries.items():
                score = 0.0
                reasons = []
                path_lower, path_parts, symbols_lower, imports_lower, summary_lower, lang_lower = (
                    entry.search_fields())

                # Path matching (bidirectional: term in path OR path-part in term)
                for term in terms:
                    if term in path_lower:
                        score += 0.3
                        reasons.append(f"path contains '{term}'")
                    else:
                        # Check if any path component is a prefix/substring of query term
                        for pp in path_parts:
                            if len(pp) >= 3 and pp in term:
                                score += 0.15
                                reasons.append(f"path part '{pp}' relates to '{term}'")
                                break

                # Symbol matching
                for term in terms:
                    matches = [s for s, lowered in zip(entry.symbols, symbols_lower) if term in lowered]
                    if matches:
                        score += 0.25 * len(matches)
                        reasons.append(f"defines {', '.join(matches[:3])}")

                # Import matching
                for term in terms:
                    if term in imports_lower:
                        score += 0.1
                        reasons.append(f"imports related to '{term}'")

                # Summary matching
                if summary_lower:
                    for term in terms:
                        if term in summary_lower:
                            score += 0.15

                # Language boost (if query mentions a language)
                if lang_lower in query_lower:
                    score += 0.1

                if score > 0:
                    results.append(SearchResult(
                        path=path,
                        score=min(1.0, score),
                        context="; ".join(reasons[:3]) if reasons else entry.summary,
                        language=entry.language,
                        symbols=entry.symbols[:10],
                    ))

        results.sort(key=lambda r: r.score, reverse=True)
        return results[:max_results]

    def _semantic_search(self, query: str, max_results: int) -> list[SearchResult]:
        """Semantic search via engram."""
        try:
            memories = self._engram.recall(
                f"codebase:{self.project_path.name} {query}",
                namespace=f"rag-{self.project_path.name}",
            )
            results = []
            for i, memory in enumerate(memories[:max_results]):
                # Parse the memory text back into a search result
                # Format: "path: summary"
                if ":" in memory:
                    path, context = memory.split(":", 1)
                    path = path.strip()
                    context = context.strip()
                else:
                    path = memory
                    context = ""

                # Check if this path exists in our index
                entry = self._entries.get(path)
                score = 1.0 - (i * 0.1)  # Rank-based scoring

                results.append(SearchResult(
                    path=path,
                    score=max(0.1, score),
                    context=context or (entry.summary if entry else ""),
                    language=entry.language if entry else "",
                    symbols=entry.symbols[:10] if entry else [],
                ))
            return results
        except Exception as e:
            logger.debug(f"Semantic search failed: {e}")
            return []

    def _push_to_engram(self):
        """Push index data to engram for semantic search."""
        try:
            namespace = f"rag-{self.project_path.name}"
            # Send file summaries as memories
            for path, entry in list(self._entries.items())[:200]:  # Cap at 200 files
                if entry.summary:
                    self._engram.remember(
                        f"{path}: {entry.summary}",
                        namespace=namespace,
                    )
        except Exception as e:
            logger.warning(f"Failed to push index to engram: {e}")

    # ── Internal: Cache ──────────────────────────────────────────────

    def _save_cache(self):
        """Save index to disk."""
        try:
            self._index_file.parent.mkdir(parents=True, exist_ok=True)
            data = {
                "version": 1,
                "last_indexed": self._last_full_index,
                "entries": {
                    path: {
                        "language": e.language,
                        "size": e.size,
                        "lines": e.lines,
                        "hash": e.hash,
                        "symbols": e.symbols,
                        "summary": e.summary,
                        "imports": e.imports,
                        "last_indexed": e.last_indexed,
                        "mtime_ns": e.mtime_ns,
                    }
                    for path, e in self._entries.items()
                },
            }
            # Compact, and replaced whole so a crash never leaves half a cache.
            temporary = self._index_file.with_name(self._index_file.name + ".tmp")
            with open(temporary, "w", encoding="utf-8") as f:
                json.dump(data, f, separators=(",", ":"))
            os.replace(temporary, self._index_file)
        except Exception as e:
            logger.debug(f"Failed to save index cache: {e}")

    def _load_cache(self):
        """Load index from disk cache."""
        try:
            if not self._index_file.exists():
                return
            with open(self._index_file, "r", encoding="utf-8") as f:
                data = json.load(f)

            if data.get("version") != 1:
                return

            self._last_full_index = data.get("last_indexed", 0)
            for path, edata in data.get("entries", {}).items():
                self._entries[path] = IndexEntry(
                    path=path,
                    language=edata.get("language", ""),
                    size=edata.get("size", 0),
                    lines=edata.get("lines", 0),
                    hash=edata.get("hash", ""),
                    symbols=edata.get("symbols", []),
                    summary=edata.get("summary", ""),
                    imports=edata.get("imports", []),
                    last_indexed=edata.get("last_indexed", 0),
                    mtime_ns=edata.get("mtime_ns", 0),
                )
            logger.info(f"Loaded {len(self._entries)} entries from index cache")
        except Exception as e:
            logger.debug(f"Failed to load index cache: {e}")


def _read_bytes(item: tuple[str, str, int, int]) -> bytes | None:
    try:
        with open(item[1], "rb") as handle:
            return handle.read()
    except OSError:
        return None


def _repository_root(project_path: Path) -> Path | None:
    """The Git repository holding the project: its own folder or one above it.

    The search stops below the home folder. A repository there (dotfiles
    kept in Git) often ignores everything it doesn't track, which would
    empty the index of every project under home.
    """
    try:
        here = project_path.resolve()
        home = Path.home().resolve()
    except (OSError, RuntimeError):
        return None
    for folder in (here, *here.parents):
        if folder == home or folder in home.parents:
            return None
        if (folder / ".git").exists():
            return folder
    return None


def _visible(rel_path: str) -> bool:
    """Whether a listed path is outside skipped and hidden directories (as the walk prunes them)."""
    parts = rel_path.split("/")[:-1]
    return not any(part in SKIP_DIRS or part.startswith(".") for part in parts)


def _indexable(rel_path: str) -> bool:
    name = rel_path.rsplit("/", 1)[-1].lower()
    ext = os.path.splitext(name)[1]
    return ext in INDEXABLE_EXTENSIONS if ext else name in EXTENSIONLESS


# ── Language Detection ───────────────────────────────────────────────

_EXT_TO_LANG = {
    ".py": "python", ".js": "javascript", ".ts": "typescript",
    ".jsx": "react", ".tsx": "react-ts", ".java": "java",
    ".go": "go", ".rs": "rust", ".c": "c", ".cpp": "cpp",
    ".h": "c-header", ".hpp": "cpp-header", ".cs": "csharp",
    ".rb": "ruby", ".php": "php", ".swift": "swift",
    ".kt": "kotlin", ".scala": "scala", ".lua": "lua",
    ".sh": "shell", ".bash": "shell", ".zsh": "shell",
    ".ps1": "powershell", ".bat": "batch", ".cmd": "batch",
    ".html": "html", ".css": "css", ".scss": "scss",
    ".less": "less", ".vue": "vue", ".svelte": "svelte",
    ".json": "json", ".yaml": "yaml", ".yml": "yaml",
    ".toml": "toml", ".ini": "ini", ".cfg": "config",
    ".md": "markdown", ".rst": "rst", ".txt": "text",
    ".sql": "sql", ".graphql": "graphql", ".proto": "protobuf",
    ".dockerfile": "docker", ".tf": "terraform", ".hcl": "hcl",
    ".r": "r", ".jl": "julia", ".m": "objective-c",
}


def _detect_language(path: str) -> str:
    """Detect programming language from file path."""
    name = os.path.basename(path).lower()
    if name == "dockerfile":
        return "docker"
    if name == "makefile":
        return "make"

    ext = os.path.splitext(path)[1].lower()
    return _EXT_TO_LANG.get(ext, "unknown")


# ── Symbol Extraction ────────────────────────────────────────────────

def _extract_symbols_and_imports(content: str, language: str) -> tuple[list[str], list[str]]:
    """Symbols and imports from one parse, with the regular-expression fallbacks."""
    try:
        from .code_intelligence import parse_code

        parsed = parse_code(content, language)
    except Exception:
        parsed = None
    symbols = parsed.symbols[:50] if parsed and parsed.symbols else _extract_symbols(content, language, parse=False)
    imports = parsed.imports[:30] if parsed and parsed.imports else _extract_imports(content, language, parse=False)
    return symbols, imports


def _extract_symbols(content: str, language: str, *, parse: bool = True) -> list[str]:
    """Extract function/class/type names from source code."""
    try:
        from .code_intelligence import parse_code

        parsed = parse_code(content, language) if parse else None
        if parsed and parsed.symbols:
            return parsed.symbols[:50]
    except Exception:
        pass
    symbols = []

    if language == "python":
        for m in re.finditer(r'^\s*(?:async\s+)?(?:class|def)\s+(\w+)', content, re.MULTILINE):
            symbols.append(m.group(1))
    elif language in ("javascript", "typescript", "react", "react-ts"):
        # function declarations (including generators: function*)
        for m in re.finditer(r'(?:function\*?|class)\s+(\w+)', content):
            symbols.append(m.group(1))
        # const/let arrow functions
        for m in re.finditer(r'(?:const|let|var)\s+(\w+)\s*=\s*(?:\([^)]*\)|[^=])\s*=>', content):
            symbols.append(m.group(1))
        # export default
        for m in re.finditer(r'export\s+(?:default\s+)?(?:function|class)\s+(\w+)', content):
            if m.group(1) not in symbols:
                symbols.append(m.group(1))
    elif language == "go":
        for m in re.finditer(r'^func\s+(?:\([^)]+\)\s+)?(\w+)', content, re.MULTILINE):
            symbols.append(m.group(1))
        for m in re.finditer(r'^type\s+(\w+)', content, re.MULTILINE):
            symbols.append(m.group(1))
    elif language == "rust":
        for m in re.finditer(r'(?:fn|struct|enum|trait|impl)\s+(\w+)', content):
            symbols.append(m.group(1))
    elif language == "java" or language == "kotlin" or language == "csharp":
        for m in re.finditer(r'(?:class|interface|enum)\s+(\w+)', content):
            symbols.append(m.group(1))
        for m in re.finditer(r'(?:public|private|protected|static)\s+\w+\s+(\w+)\s*\(', content):
            if m.group(1) not in symbols:
                symbols.append(m.group(1))
    elif language == "ruby":
        for m in re.finditer(r'(?:class|module|def)\s+(\w+)', content):
            symbols.append(m.group(1))
    elif language in ("c", "cpp", "c-header", "cpp-header"):
        for m in re.finditer(r'(?:class|struct|enum)\s+(\w+)', content):
            symbols.append(m.group(1))
        # Function declarations (simplified)
        for m in re.finditer(r'^[\w:*&]+\s+(\w+)\s*\([^;]*$', content, re.MULTILINE):
            name = m.group(1)
            if name not in ("if", "for", "while", "switch", "return") and name not in symbols:
                symbols.append(name)

    return symbols[:50]  # Cap at 50 symbols per file


def _extract_imports(content: str, language: str, *, parse: bool = True) -> list[str]:
    """Extract import/include statements."""
    try:
        from .code_intelligence import parse_code

        parsed = parse_code(content, language) if parse else None
        if parsed and parsed.imports:
            return parsed.imports[:30]
    except Exception:
        pass
    imports = []

    if language == "python":
        for m in re.finditer(r'^(?:from\s+(\S+)|import\s+(\S+))', content, re.MULTILINE):
            imports.append(m.group(1) or m.group(2))
    elif language in ("javascript", "typescript", "react", "react-ts"):
        # import ... from 'module' and import 'module'
        for m in re.finditer(r'''import\s+.*?from\s+['"]([^'"]+)['"]''', content):
            imports.append(m.group(1))
        # import('dynamic') and require('module')
        for m in re.finditer(r'''(?:import|require)\s*\(\s*['"]([^'"]+)['"]''', content):
            if m.group(1) not in imports:
                imports.append(m.group(1))
        # bare import 'module' (side-effect only)
        for m in re.finditer(r'''^\s*import\s+['"]([^'"]+)['"]''', content, re.MULTILINE):
            if m.group(1) not in imports:
                imports.append(m.group(1))
    elif language == "go":
        for m in re.finditer(r'"([^"]+)"', content[:2000]):  # Imports are at top
            imports.append(m.group(1))
    elif language == "rust":
        for m in re.finditer(r'use\s+(\S+)', content):
            imports.append(m.group(1).rstrip(";"))
    elif language in ("java", "kotlin", "csharp"):
        for m in re.finditer(r'(?:import|using)\s+(\S+)', content):
            imports.append(m.group(1).rstrip(";"))
    elif language in ("c", "cpp", "c-header", "cpp-header"):
        for m in re.finditer(r'#include\s*[<"]([^>"]+)', content):
            imports.append(m.group(1))

    return imports[:30]  # Cap at 30 imports


def _build_summary(path: str, language: str, symbols: list, imports: list, lines: int) -> str:
    """Build a brief summary for the index entry."""
    parts = []

    if language and language != "unknown":
        parts.append(language)

    parts.append(f"{lines} lines")

    if symbols:
        if len(symbols) <= 3:
            parts.append(f"defines {', '.join(symbols)}")
        else:
            parts.append(f"defines {', '.join(symbols[:3])} +{len(symbols)-3} more")

    if imports:
        # Just mention key imports
        key_imports = [i.split("/")[-1].split(".")[-1] for i in imports[:3]]
        parts.append(f"uses {', '.join(key_imports)}")

    return "; ".join(parts)
