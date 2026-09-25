"""Measure the codebase index (lumi/engine/rag.py) on a large synthetic repository.

    python scripts/benchmark_index.py --files 20000 --ignored 20000

It writes a repository to a temporary folder: packages of Python and
TypeScript modules that import each other, and a ``generated/`` tree that
``.gitignore`` excludes. Then it times a cold index, an unchanged re-index,
a forced re-index of files already read (the indexer's own cost), an index
after touching 1% of the files, keyword searches and the repo map, and
reports the cache size. Nothing outside the temporary folder is touched.

The cold index includes each file's first read. On Windows with real-time
antivirus, reading a file written moments before is much slower than
reading one again, so the cold number is pessimistic for a repository that
has been on disk for a while.
"""

from __future__ import annotations

import argparse
import json
import random
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lumi.engine.rag import CodebaseIndex  # noqa: E402

WORDS = ("order", "invoice", "customer", "payment", "shipment", "ledger", "account", "session", "report", "token",
         "cache", "queue", "worker", "policy", "audit", "budget", "device", "member", "search", "index")


def _python_module(rng: random.Random, package: int, module: int, packages: int) -> str:
    imports = [f"from pkg{rng.randrange(packages)}.module{rng.randrange(40)} import helper" for _ in range(3)]
    noun = rng.choice(WORDS)
    body = [f'"""{noun.title()} handling for package {package}."""', *imports, ""]
    for index in range(2):
        body += [f"class {noun.title()}Service{index}:", f"    def process_{noun}(self, value):",
                 "        return helper(value) + 1", ""]
    for index in range(5):
        body += [f"def {noun}_step_{index}(items):", "    total = 0", "    for item in items:",
                 "        total += len(str(item))", "    return total", ""]
    return "\n".join(body)


def _typescript_module(rng: random.Random, package: int, module: int, packages: int) -> str:
    noun = rng.choice(WORDS)
    lines = [f"import {{ helper }} from '../../pkg{rng.randrange(packages)}/src/module{rng.randrange(40)}';"
             for _ in range(3)]
    lines += [f"export class {noun.title()}Store{module} {{", f"  load{noun.title()}(id: string) {{",
              "    return helper(id);", "  }", "}"]
    lines += [f"export const {noun}Handler{i} = (value: number) => value * {i};" for i in range(5)]
    return "\n".join(lines) + "\n"


def build_repository(root: Path, files: int, ignored: int, seed: int = 7) -> None:
    rng = random.Random(seed)
    packages = max(1, files // 80)
    written = 0
    for package in range(packages):
        for module in range(80):
            if written >= files:
                break
            if module % 3 == 2:
                path = root / "web" / f"pkg{package}" / "src" / f"module{module}.ts"
                text = _typescript_module(rng, package, module, packages)
            else:
                path = root / f"pkg{package}" / f"module{module}.py"
                text = _python_module(rng, package, module, packages)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
            written += 1
    generated = root / "generated"
    for index in range(ignored):
        path = generated / f"batch{index // 500}" / f"artifact{index}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"id": index, "values": list(range(20))}), encoding="utf-8")
    (root / ".gitignore").write_text("generated/\n*.log\n", encoding="utf-8")
    (root / "README.md").write_text("# Synthetic monorepo\n", encoding="utf-8")
    if shutil.which("git"):
        subprocess.run(["git", "init", "-q"], cwd=root, check=False)


def _timed(action):
    start = time.perf_counter()
    result = action()
    return time.perf_counter() - start, result


def run(files: int, ignored: int, folder: str | None) -> dict:
    root = Path(folder) if folder else Path(tempfile.mkdtemp(prefix="lumi-index-bench-"))
    try:
        seconds, _ = _timed(lambda: build_repository(root, files, ignored))
        report: dict = {"files": files, "ignored_files": ignored, "build_seconds": round(seconds, 2)}

        index = CodebaseIndex(root)
        seconds, stats = _timed(index.index)
        report["cold_index_seconds"] = round(seconds, 2)
        report["cold_index_stats"] = {k: stats.get(k) for k in ("files_scanned", "files_indexed", "total_files",
                                                                "truncated")}
        cache = root / ".lumi" / "index.json"
        report["cache_megabytes"] = round(cache.stat().st_size / 1_000_000, 2) if cache.exists() else 0

        seconds, _ = _timed(index.index)
        report["unchanged_reindex_seconds"] = round(seconds, 2)

        # Everything again with the files already read once: the indexer's own
        # cost, without first-read costs such as an antivirus scanning new files.
        seconds, _ = _timed(lambda: index.index(force=True))
        report["forced_reindex_seconds"] = round(seconds, 2)

        touched = sorted(p for p in root.rglob("module*.py"))[: max(1, files // 100)]
        for path in touched:
            path.write_text(path.read_text(encoding="utf-8") + "\n# touched\n", encoding="utf-8")
        seconds, stats = _timed(index.index)
        report["reindex_after_1pct_change_seconds"] = round(seconds, 2)
        report["reindexed_files"] = stats.get("files_indexed")

        timings = []
        for query in ("invoice service", "payment step", "customer store handler", "ledger", "audit policy",
                      "session token cache", "worker queue", "device member", "report budget", "search index"):
            seconds, _ = _timed(lambda q=query: index.search(q, max_results=10))
            timings.append(seconds)
        report["search_ms_median"] = round(statistics.median(timings) * 1000, 1)
        report["search_ms_max"] = round(max(timings) * 1000, 1)

        seconds, repo_map = _timed(lambda: index.get_repo_map(max_tokens=1000))
        report["repo_map_seconds"] = round(seconds, 2)
        report["repo_map_chars"] = len(repo_map)

        seconds, _ = _timed(lambda: CodebaseIndex(root))
        report["load_cached_index_seconds"] = round(seconds, 2)
        return report
    finally:
        if not folder:
            shutil.rmtree(root, ignore_errors=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--files", type=int, default=20_000, help="source files to write")
    parser.add_argument("--ignored", type=int, default=20_000, help="files under a .gitignore'd folder")
    parser.add_argument("--dir", help="write the repository here and keep it (default: a temporary folder)")
    args = parser.parse_args()
    print(json.dumps(run(args.files, args.ignored, args.dir), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
