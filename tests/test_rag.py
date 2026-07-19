"""
Tests for resonant_client/engine/rag.py

Covers: CodebaseIndex lifecycle, keyword/symbol/path/import search,
context generation, caching, language detection, symbol/import extraction,
summary building, incremental indexing, filtering, thread safety, and edge cases.
"""

import json
import threading
import time

import pytest

from resonant_client.engine.rag import (
    CodebaseIndex,
    IndexEntry,
    MAX_FILE_SIZE,
    _build_summary,
    _detect_language,
    _extract_imports,
    _extract_symbols,
)


# ======================================================================
# CodebaseIndex: init, index(), properties, get_stats
# ======================================================================


class TestCodebaseIndexLifecycle:
    """Tests for CodebaseIndex construction, indexing, and stats."""

    @pytest.mark.unit
    def test_init_sets_project_path(self, tmp_project):
        idx = CodebaseIndex(tmp_project)
        assert idx.project_path == tmp_project

    @pytest.mark.unit
    def test_not_indexed_initially(self, tmp_project):
        idx = CodebaseIndex(tmp_project)
        assert idx.is_indexed is False

    @pytest.mark.unit
    def test_file_count_zero_before_index(self, tmp_project):
        idx = CodebaseIndex(tmp_project)
        assert idx.file_count == 0

    @pytest.mark.integration
    def test_index_marks_as_indexed(self, tmp_project):
        idx = CodebaseIndex(tmp_project)
        idx.index()
        assert idx.is_indexed is True

    @pytest.mark.integration
    def test_index_returns_stats(self, tmp_project):
        idx = CodebaseIndex(tmp_project)
        stats = idx.index()
        assert "files_indexed" in stats
        assert "files_scanned" in stats
        assert "elapsed_ms" in stats
        assert "total_files" in stats
        assert stats["files_indexed"] > 0

    @pytest.mark.integration
    def test_file_count_after_index(self, tmp_project):
        idx = CodebaseIndex(tmp_project)
        idx.index()
        # main.py, app.js, server.go, lib/auth.py, config.yaml = 5
        assert idx.file_count >= 5

    @pytest.mark.integration
    def test_index_while_already_indexing(self, tmp_project):
        idx = CodebaseIndex(tmp_project)
        idx._indexing = True
        result = idx.index()
        assert result == {"status": "already_indexing"}

    @pytest.mark.integration
    def test_get_stats_returns_languages(self, tmp_project):
        idx = CodebaseIndex(tmp_project)
        idx.index()
        stats = idx.get_stats()
        assert "languages" in stats
        assert stats["total_files"] > 0
        assert stats["total_lines"] > 0
        assert "python" in stats["languages"]

    @pytest.mark.integration
    def test_get_stats_includes_project_path(self, tmp_project):
        idx = CodebaseIndex(tmp_project)
        stats = idx.get_stats()
        assert stats["project_path"] == str(tmp_project)

    @pytest.mark.unit
    def test_init_with_string_path(self, tmp_project):
        idx = CodebaseIndex(str(tmp_project))
        assert idx.project_path == tmp_project

    @pytest.mark.unit
    def test_is_indexing_false_by_default(self, tmp_project):
        idx = CodebaseIndex(tmp_project)
        assert idx.is_indexing is False


# ======================================================================
# Search: keyword, symbol, path, import, language boost
# ======================================================================


class TestSearch:
    """Tests for CodebaseIndex.search() keyword matching."""

    @pytest.fixture(autouse=True)
    def _indexed(self, tmp_project):
        self.idx = CodebaseIndex(tmp_project)
        self.idx.index()

    @pytest.mark.integration
    def test_keyword_match_finds_file(self):
        results = self.idx.search("main")
        assert any("main" in r.path.lower() for r in results)

    @pytest.mark.integration
    def test_symbol_match_finds_class(self):
        results = self.idx.search("Authenticator")
        assert any("auth" in r.path for r in results)

    @pytest.mark.integration
    def test_symbol_match_finds_function(self):
        results = self.idx.search("verify_token")
        assert any("auth" in r.path for r in results)

    @pytest.mark.integration
    def test_path_matching(self):
        results = self.idx.search("server")
        assert any("server.go" in r.path for r in results)

    @pytest.mark.integration
    def test_bidirectional_path_matching(self):
        """'authentication' should find auth.py because 'auth' is in the term."""
        results = self.idx.search("authentication")
        assert any("auth" in r.path for r in results)

    @pytest.mark.integration
    def test_import_matching(self):
        results = self.idx.search("react")
        assert any("app.js" in r.path for r in results)

    @pytest.mark.integration
    def test_language_boost(self):
        results = self.idx.search("python class")
        python_results = [r for r in results if r.language == "python"]
        assert len(python_results) > 0

    @pytest.mark.integration
    def test_max_results_respected(self):
        results = self.idx.search("file", max_results=2)
        assert len(results) <= 2

    @pytest.mark.integration
    def test_scores_between_zero_and_one(self):
        results = self.idx.search("main")
        for r in results:
            assert 0.0 < r.score <= 1.0

    @pytest.mark.integration
    def test_results_sorted_by_score_descending(self):
        results = self.idx.search("main")
        if len(results) > 1:
            scores = [r.score for r in results]
            assert scores == sorted(scores, reverse=True)

    @pytest.mark.integration
    def test_search_no_match(self):
        results = self.idx.search("xyzzy_nonexistent_thing_12345")
        assert len(results) == 0

    @pytest.mark.integration
    def test_search_result_has_context(self):
        results = self.idx.search("App")
        if results:
            assert results[0].context != ""

    @pytest.mark.integration
    def test_search_result_has_language(self):
        results = self.idx.search("server")
        go_results = [r for r in results if r.language == "go"]
        assert len(go_results) > 0

    @pytest.mark.integration
    def test_search_result_has_symbols(self):
        results = self.idx.search("Authenticator")
        matching = [r for r in results if "auth" in r.path]
        assert matching
        assert len(matching[0].symbols) > 0

    @pytest.mark.integration
    def test_search_result_to_dict(self):
        results = self.idx.search("main")
        if results:
            d = results[0].to_dict()
            assert "path" in d
            assert "score" in d
            assert "context" in d


# ======================================================================
# get_context_for_prompt
# ======================================================================


class TestGetContextForPrompt:
    """Tests for context formatting for LLM prompts."""

    @pytest.mark.integration
    def test_not_indexed_returns_empty(self, tmp_project):
        idx = CodebaseIndex(tmp_project)
        assert idx.get_context_for_prompt("anything") == ""

    @pytest.mark.integration
    def test_indexed_returns_formatted(self, tmp_project):
        idx = CodebaseIndex(tmp_project)
        idx.index()
        ctx = idx.get_context_for_prompt("main")
        assert "--- RELEVANT FILES ---" in ctx
        assert "--- END RELEVANT FILES ---" in ctx

    @pytest.mark.integration
    def test_max_files_limits_results(self, tmp_project):
        idx = CodebaseIndex(tmp_project)
        idx.index()
        ctx = idx.get_context_for_prompt("main", max_files=1)
        relevant = ctx.split("--- RELEVANT FILES ---", 1)[-1]
        # Count query-specific entries, excluding the fixed repo map.
        file_lines = [line for line in relevant.split("\n") if line.startswith("- ")]
        assert len(file_lines) <= 1

    @pytest.mark.integration
    def test_no_match_returns_empty(self, tmp_project):
        idx = CodebaseIndex(tmp_project)
        idx.index()
        ctx = idx.get_context_for_prompt("xyzzy_nonexistent_12345")
        assert "--- REPO MAP" in ctx
        assert "--- RELEVANT FILES ---" not in ctx

    @pytest.mark.integration
    def test_repo_map_prioritizes_referenced_files_with_symbols(self, tmp_project):
        idx = CodebaseIndex(tmp_project)
        idx.index()
        repo_map = idx.get_repo_map(max_tokens=300)
        assert "REPO MAP" in repo_map
        assert "auth.py" in repo_map
        assert "Authenticator" in repo_map


# ======================================================================
# Cache: save/load round-trip, version mismatch, corruption
# ======================================================================


class TestCache:
    """Tests for index cache persistence."""

    @pytest.mark.integration
    def test_save_load_round_trip(self, tmp_project):
        idx = CodebaseIndex(tmp_project)
        idx.index()
        count_before = idx.file_count

        # Create a new index from same path -- should load from cache
        idx2 = CodebaseIndex(tmp_project)
        assert idx2.file_count == count_before
        assert idx2.is_indexed is True

    @pytest.mark.integration
    def test_version_mismatch_recovery(self, tmp_project):
        idx = CodebaseIndex(tmp_project)
        idx.index()
        cache_file = tmp_project / ".resonant" / "index.json"
        # Tamper version
        data = json.loads(cache_file.read_text(encoding="utf-8"))
        data["version"] = 999
        cache_file.write_text(json.dumps(data), encoding="utf-8")

        idx2 = CodebaseIndex(tmp_project)
        assert idx2.file_count == 0
        assert idx2.is_indexed is False

    @pytest.mark.adversarial
    def test_corrupt_json_recovery(self, tmp_project):
        idx = CodebaseIndex(tmp_project)
        idx.index()
        cache_file = tmp_project / ".resonant" / "index.json"
        cache_file.write_text("{{{invalid json!!!", encoding="utf-8")

        idx2 = CodebaseIndex(tmp_project)
        assert idx2.file_count == 0

    @pytest.mark.adversarial
    def test_missing_fields_in_cached_entries(self, tmp_project):
        """Cached entries with missing fields should use defaults."""
        cache_file = tmp_project / ".resonant" / "index.json"
        data = {
            "version": 1,
            "last_indexed": time.time(),
            "entries": {
                "some/file.py": {
                    # Minimal -- all optional fields missing
                }
            },
        }
        cache_file.write_text(json.dumps(data), encoding="utf-8")

        idx = CodebaseIndex(tmp_project)
        assert idx.file_count == 1
        entry = idx._entries["some/file.py"]
        assert entry.language == ""
        assert entry.size == 0
        assert entry.symbols == []
        assert entry.imports == []

    @pytest.mark.integration
    def test_cache_file_created_on_index(self, tmp_project):
        idx = CodebaseIndex(tmp_project)
        idx.index()
        cache_file = tmp_project / ".resonant" / "index.json"
        assert cache_file.exists()


# ======================================================================
# _detect_language
# ======================================================================


class TestDetectLanguage:
    """Tests for language detection from file paths."""

    @pytest.mark.unit
    @pytest.mark.parametrize("ext,expected", [
        (".py", "python"),
        (".js", "javascript"),
        (".ts", "typescript"),
        (".jsx", "react"),
        (".tsx", "react-ts"),
        (".java", "java"),
        (".go", "go"),
        (".rs", "rust"),
        (".c", "c"),
        (".cpp", "cpp"),
        (".h", "c-header"),
        (".hpp", "cpp-header"),
        (".cs", "csharp"),
        (".rb", "ruby"),
        (".php", "php"),
        (".swift", "swift"),
        (".kt", "kotlin"),
        (".scala", "scala"),
        (".lua", "lua"),
        (".sh", "shell"),
        (".bash", "shell"),
        (".zsh", "shell"),
        (".ps1", "powershell"),
        (".bat", "batch"),
        (".html", "html"),
        (".css", "css"),
        (".scss", "scss"),
        (".less", "less"),
        (".vue", "vue"),
        (".svelte", "svelte"),
        (".json", "json"),
        (".yaml", "yaml"),
        (".yml", "yaml"),
        (".toml", "toml"),
        (".ini", "ini"),
        (".md", "markdown"),
        (".sql", "sql"),
        (".graphql", "graphql"),
        (".proto", "protobuf"),
        (".tf", "terraform"),
        (".hcl", "hcl"),
        (".r", "r"),
        (".jl", "julia"),
        (".m", "objective-c"),
    ])
    def test_extension_mapping(self, ext, expected):
        assert _detect_language(f"somedir/file{ext}") == expected

    @pytest.mark.unit
    def test_dockerfile_special_case(self):
        assert _detect_language("Dockerfile") == "docker"
        assert _detect_language("path/to/Dockerfile") == "docker"

    @pytest.mark.unit
    def test_makefile_special_case(self):
        assert _detect_language("Makefile") == "make"
        assert _detect_language("some/dir/Makefile") == "make"

    @pytest.mark.unit
    def test_unknown_extension(self):
        assert _detect_language("file.xyz123") == "unknown"

    @pytest.mark.unit
    def test_case_insensitive_extension(self):
        # The implementation lowercases the extension
        assert _detect_language("FILE.PY") == "python"

    @pytest.mark.unit
    def test_case_insensitive_dockerfile(self):
        # basename is lowered before comparison
        assert _detect_language("dockerfile") == "docker"


# ======================================================================
# _extract_symbols
# ======================================================================


class TestExtractSymbols:
    """Tests for symbol extraction across languages."""

    @pytest.mark.unit
    def test_python_class(self):
        code = "class MyClass:\n    pass\n"
        symbols = _extract_symbols(code, "python")
        assert "MyClass" in symbols

    @pytest.mark.unit
    def test_python_def(self):
        code = "def my_func():\n    pass\n"
        symbols = _extract_symbols(code, "python")
        assert "my_func" in symbols

    @pytest.mark.unit
    def test_python_async_def(self):
        code = "async def fetch_data():\n    pass\n"
        symbols = _extract_symbols(code, "python")
        assert "fetch_data" in symbols

    @pytest.mark.unit
    def test_python_multiple_symbols(self):
        code = (
            "class A:\n    pass\n"
            "class B:\n    pass\n"
            "def func_one(): pass\n"
            "async def func_two(): pass\n"
        )
        symbols = _extract_symbols(code, "python")
        assert set(symbols) >= {"A", "B", "func_one", "func_two"}

    @pytest.mark.unit
    def test_javascript_function(self):
        code = "function doStuff() {}\n"
        symbols = _extract_symbols(code, "javascript")
        assert "doStuff" in symbols

    @pytest.mark.unit
    def test_javascript_generator_function(self):
        code = "function* genItems() { yield 1; }\n"
        symbols = _extract_symbols(code, "javascript")
        assert "genItems" in symbols

    @pytest.mark.unit
    def test_javascript_class(self):
        code = "class Widget {}\n"
        symbols = _extract_symbols(code, "javascript")
        assert "Widget" in symbols

    @pytest.mark.unit
    def test_javascript_const_arrow(self):
        code = "const helper = (x) => x + 1;\n"
        symbols = _extract_symbols(code, "javascript")
        assert "helper" in symbols

    @pytest.mark.unit
    def test_javascript_export_default(self):
        code = "export default function Main() {}\n"
        symbols = _extract_symbols(code, "javascript")
        assert "Main" in symbols

    @pytest.mark.unit
    def test_typescript_symbols(self):
        code = "function tsFunc() {}\nclass TsClass {}\n"
        symbols = _extract_symbols(code, "typescript")
        assert "tsFunc" in symbols
        assert "TsClass" in symbols

    @pytest.mark.unit
    def test_go_func(self):
        code = "func main() {}\nfunc (s *Server) Start() {}\n"
        symbols = _extract_symbols(code, "go")
        assert "main" in symbols
        assert "Start" in symbols

    @pytest.mark.unit
    def test_go_type(self):
        code = "type Server struct{}\n"
        symbols = _extract_symbols(code, "go")
        assert "Server" in symbols

    @pytest.mark.unit
    def test_rust_fn(self):
        code = "fn process() {}\n"
        symbols = _extract_symbols(code, "rust")
        assert "process" in symbols

    @pytest.mark.unit
    def test_rust_struct(self):
        code = "struct Config {}\n"
        symbols = _extract_symbols(code, "rust")
        assert "Config" in symbols

    @pytest.mark.unit
    def test_rust_enum(self):
        code = "enum Color { Red, Blue }\n"
        symbols = _extract_symbols(code, "rust")
        assert "Color" in symbols

    @pytest.mark.unit
    def test_rust_trait(self):
        code = "trait Drawable { fn draw(&self); }\n"
        symbols = _extract_symbols(code, "rust")
        assert "Drawable" in symbols
        assert "draw" in symbols

    @pytest.mark.unit
    def test_rust_impl(self):
        code = "impl Widget { fn new() -> Self {} }\n"
        symbols = _extract_symbols(code, "rust")
        assert "Widget" in symbols
        assert "new" in symbols

    @pytest.mark.unit
    def test_java_class(self):
        code = "class UserService {}\n"
        symbols = _extract_symbols(code, "java")
        assert "UserService" in symbols

    @pytest.mark.unit
    def test_java_interface(self):
        code = "interface Runnable {}\n"
        symbols = _extract_symbols(code, "java")
        assert "Runnable" in symbols

    @pytest.mark.unit
    def test_java_method(self):
        code = "public void processData(String arg) {}\n"
        symbols = _extract_symbols(code, "java")
        assert "processData" in symbols

    @pytest.mark.unit
    def test_csharp_class_and_method(self):
        code = "class MyController {}\npublic int GetCount() {}\n"
        symbols = _extract_symbols(code, "csharp")
        assert "MyController" in symbols
        assert "GetCount" in symbols

    @pytest.mark.unit
    def test_ruby_class(self):
        code = "class User\nend\n"
        symbols = _extract_symbols(code, "ruby")
        assert "User" in symbols

    @pytest.mark.unit
    def test_ruby_module(self):
        code = "module Auth\nend\n"
        symbols = _extract_symbols(code, "ruby")
        assert "Auth" in symbols

    @pytest.mark.unit
    def test_ruby_def(self):
        code = "def authenticate\nend\n"
        symbols = _extract_symbols(code, "ruby")
        assert "authenticate" in symbols

    @pytest.mark.unit
    def test_c_struct(self):
        code = "struct Point { int x; int y; };\n"
        symbols = _extract_symbols(code, "c")
        assert "Point" in symbols

    @pytest.mark.unit
    def test_cpp_class(self):
        code = "class Widget {};\n"
        symbols = _extract_symbols(code, "cpp")
        assert "Widget" in symbols

    @pytest.mark.unit
    def test_c_function(self):
        code = "int compute(int a, int b)\n{\n    return a + b;\n}\n"
        symbols = _extract_symbols(code, "c")
        assert "compute" in symbols

    @pytest.mark.unit
    def test_symbols_capped_at_50(self):
        code = "\n".join(f"def func_{i}(): pass" for i in range(60))
        symbols = _extract_symbols(code, "python")
        assert len(symbols) == 50

    @pytest.mark.unit
    def test_unknown_language_returns_empty(self):
        code = "some random text\n"
        symbols = _extract_symbols(code, "unknown")
        assert symbols == []

    @pytest.mark.unit
    def test_react_ts_uses_js_patterns(self):
        code = "function Component() {}\nclass Store {}\n"
        symbols = _extract_symbols(code, "react-ts")
        assert "Component" in symbols
        assert "Store" in symbols


# ======================================================================
# _extract_imports
# ======================================================================


class TestExtractImports:
    """Tests for import extraction across languages."""

    @pytest.mark.unit
    def test_python_import(self):
        code = "import os\nimport sys\n"
        imports = _extract_imports(code, "python")
        assert "os" in imports
        assert "sys" in imports

    @pytest.mark.unit
    def test_python_from_import(self):
        code = "from pathlib import Path\n"
        imports = _extract_imports(code, "python")
        assert "pathlib" in imports

    @pytest.mark.unit
    def test_javascript_import_from(self):
        code = "import React from 'react';\n"
        imports = _extract_imports(code, "javascript")
        assert "react" in imports

    @pytest.mark.unit
    def test_javascript_require(self):
        code = "const fs = require('fs');\n"
        imports = _extract_imports(code, "javascript")
        assert "fs" in imports

    @pytest.mark.unit
    def test_javascript_dynamic_import(self):
        code = "const mod = import('lodash');\n"
        imports = _extract_imports(code, "javascript")
        assert "lodash" in imports

    @pytest.mark.unit
    def test_javascript_side_effect_import(self):
        code = "import 'polyfill';\n"
        imports = _extract_imports(code, "javascript")
        assert "polyfill" in imports

    @pytest.mark.unit
    def test_typescript_imports(self):
        code = "import { Component } from '@angular/core';\n"
        imports = _extract_imports(code, "typescript")
        assert "@angular/core" in imports

    @pytest.mark.unit
    def test_go_imports(self):
        code = 'package main\nimport "fmt"\nimport "net/http"\n'
        imports = _extract_imports(code, "go")
        assert "fmt" in imports
        assert "net/http" in imports

    @pytest.mark.unit
    def test_rust_use(self):
        code = "use std::collections::HashMap;\nuse serde::Serialize;\n"
        imports = _extract_imports(code, "rust")
        assert "std::collections::HashMap" in imports
        assert "serde::Serialize" in imports

    @pytest.mark.unit
    def test_java_import(self):
        code = "import java.util.List;\nimport java.io.File;\n"
        imports = _extract_imports(code, "java")
        assert "java.util.List" in imports
        assert "java.io.File" in imports

    @pytest.mark.unit
    def test_csharp_using(self):
        code = "using System;\nusing System.Collections.Generic;\n"
        imports = _extract_imports(code, "csharp")
        assert "System" in imports

    @pytest.mark.unit
    def test_c_include(self):
        code = '#include <stdio.h>\n#include "myheader.h"\n'
        imports = _extract_imports(code, "c")
        assert "stdio.h" in imports
        assert "myheader.h" in imports

    @pytest.mark.unit
    def test_cpp_include(self):
        code = '#include <iostream>\n#include <vector>\n'
        imports = _extract_imports(code, "cpp")
        assert "iostream" in imports
        assert "vector" in imports

    @pytest.mark.unit
    def test_imports_capped_at_30(self):
        code = "\n".join(f"import mod_{i}" for i in range(40))
        imports = _extract_imports(code, "python")
        assert len(imports) == 30

    @pytest.mark.unit
    def test_unknown_language_returns_empty(self):
        code = "random stuff\n"
        imports = _extract_imports(code, "unknown")
        assert imports == []


# ======================================================================
# _build_summary
# ======================================================================


class TestBuildSummary:
    """Tests for summary string construction."""

    @pytest.mark.unit
    def test_includes_language(self):
        s = _build_summary("f.py", "python", [], [], 10)
        assert "python" in s

    @pytest.mark.unit
    def test_includes_line_count(self):
        s = _build_summary("f.py", "python", [], [], 42)
        assert "42 lines" in s

    @pytest.mark.unit
    def test_includes_defines_few_symbols(self):
        s = _build_summary("f.py", "python", ["foo", "bar"], [], 10)
        assert "defines foo, bar" in s

    @pytest.mark.unit
    def test_includes_defines_many_symbols_with_more(self):
        syms = ["a", "b", "c", "d", "e"]
        s = _build_summary("f.py", "python", syms, [], 10)
        assert "+2 more" in s

    @pytest.mark.unit
    def test_includes_uses_imports(self):
        s = _build_summary("f.py", "python", [], ["os", "sys"], 10)
        assert "uses os, sys" in s

    @pytest.mark.unit
    def test_unknown_language_excluded(self):
        s = _build_summary("f.xyz", "unknown", [], [], 10)
        assert "unknown" not in s
        assert "10 lines" in s

    @pytest.mark.unit
    def test_empty_everything(self):
        s = _build_summary("f.xyz", "unknown", [], [], 5)
        assert "5 lines" in s

    @pytest.mark.unit
    def test_import_path_shortening(self):
        s = _build_summary("f.py", "python", [], ["os.path", "collections.abc"], 10)
        assert "uses path, abc" in s


# ======================================================================
# Incremental indexing
# ======================================================================


class TestIncrementalIndexing:
    """Tests for change detection and incremental re-indexing."""

    @pytest.mark.integration
    def test_unchanged_files_not_reindexed(self, tmp_project):
        idx = CodebaseIndex(tmp_project)
        idx.index()
        stats2 = idx.index()
        # Second run should index 0 files (all unchanged)
        assert stats2["files_indexed"] == 0

    @pytest.mark.integration
    def test_unchanged_files_reuse_cached_hashes(self, tmp_project, monkeypatch):
        idx = CodebaseIndex(tmp_project)
        idx.index()

        def unexpected_hash(_path):
            raise AssertionError("unchanged files should not be opened and hashed")

        monkeypatch.setattr(idx, "_hash_file", unexpected_hash)
        stats = idx.index()

        assert stats["files_indexed"] == 0

    @pytest.mark.integration
    def test_changed_file_reindexed(self, tmp_project):
        idx = CodebaseIndex(tmp_project)
        idx.index()
        # Modify a file
        (tmp_project / "main.py").write_text("def new_func(): pass\n", encoding="utf-8")
        stats = idx.index()
        assert stats["files_indexed"] >= 1

    @pytest.mark.integration
    def test_deleted_file_removed(self, tmp_project):
        idx = CodebaseIndex(tmp_project)
        idx.index()
        count_before = idx.file_count
        (tmp_project / "config.yaml").unlink()
        stats = idx.index()
        assert idx.file_count < count_before
        assert stats.get("files_removed", 0) >= 1

    @pytest.mark.integration
    def test_force_reindexes_all(self, tmp_project):
        idx = CodebaseIndex(tmp_project)
        idx.index()
        stats = idx.index(force=True)
        assert stats["files_indexed"] > 0

    @pytest.mark.integration
    def test_reindex_invalidates_cached_repo_map(self, tmp_project):
        idx = CodebaseIndex(tmp_project)
        idx.index()
        before = idx.get_repo_map(max_tokens=10_000)

        (tmp_project / "new_module.py").write_text(
            "def newly_indexed_symbol():\n    return True\n",
            encoding="utf-8",
        )
        idx.index()
        after = idx.get_repo_map(max_tokens=10_000)

        assert "newly_indexed_symbol" not in before
        assert "newly_indexed_symbol" in after


# ======================================================================
# SKIP_DIRS filtering
# ======================================================================


class TestSkipDirs:
    """Tests for directory exclusion during indexing."""

    @pytest.mark.integration
    def test_node_modules_skipped(self, tmp_project):
        nm = tmp_project / "node_modules" / "pkg"
        nm.mkdir(parents=True)
        (nm / "index.js").write_text("module.exports = {};", encoding="utf-8")

        idx = CodebaseIndex(tmp_project)
        idx.index()
        assert not any("node_modules" in p for p in idx._entries)

    @pytest.mark.integration
    def test_git_dir_skipped(self, tmp_project):
        gd = tmp_project / ".git" / "objects"
        gd.mkdir(parents=True)
        (gd / "abc123").write_text("blob", encoding="utf-8")

        idx = CodebaseIndex(tmp_project)
        idx.index()
        assert not any(".git" in p for p in idx._entries)

    @pytest.mark.integration
    def test_pycache_skipped(self, tmp_project):
        pc = tmp_project / "__pycache__"
        pc.mkdir()
        (pc / "mod.cpython-311.pyc").write_text("bytecode", encoding="utf-8")

        idx = CodebaseIndex(tmp_project)
        idx.index()
        assert not any("__pycache__" in p for p in idx._entries)

    @pytest.mark.integration
    def test_hidden_dirs_skipped(self, tmp_project):
        hd = tmp_project / ".hidden_dir"
        hd.mkdir()
        (hd / "secret.py").write_text("x = 1\n", encoding="utf-8")

        idx = CodebaseIndex(tmp_project)
        idx.index()
        assert not any(".hidden_dir" in p for p in idx._entries)


# ======================================================================
# MAX_FILE_SIZE
# ======================================================================


class TestMaxFileSize:
    """Tests for large file skipping."""

    @pytest.mark.integration
    def test_large_file_skipped(self, tmp_project):
        big = tmp_project / "huge.py"
        big.write_text("x = 1\n" * (MAX_FILE_SIZE // 5), encoding="utf-8")

        idx = CodebaseIndex(tmp_project)
        idx.index()
        assert not any("huge.py" in p for p in idx._entries)

    @pytest.mark.integration
    def test_normal_sized_file_indexed(self, tmp_project):
        small = tmp_project / "small.py"
        small.write_text("x = 1\n", encoding="utf-8")

        idx = CodebaseIndex(tmp_project)
        idx.index()
        assert any("small.py" in p for p in idx._entries)


# ======================================================================
# Thread safety
# ======================================================================


class TestThreadSafety:
    """Tests for concurrent access."""

    @pytest.mark.integration
    def test_concurrent_search_while_indexing(self, tmp_project):
        idx = CodebaseIndex(tmp_project)
        idx.index()
        errors = []

        def searcher():
            try:
                for _ in range(20):
                    idx.search("main")
            except Exception as e:
                errors.append(e)

        def reindexer():
            try:
                idx.index(force=True)
            except Exception as e:
                errors.append(e)

        threads = [
            threading.Thread(target=searcher),
            threading.Thread(target=searcher),
            threading.Thread(target=reindexer),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert len(errors) == 0


# ======================================================================
# Edge cases
# ======================================================================


class TestEdgeCases:
    """Adversarial and boundary-condition tests."""

    @pytest.mark.adversarial
    def test_empty_project(self, tmp_path):
        proj = tmp_path / "empty"
        proj.mkdir()
        (proj / ".resonant").mkdir()

        idx = CodebaseIndex(proj)
        stats = idx.index()
        assert idx.file_count == 0
        assert stats["files_indexed"] == 0

    @pytest.mark.adversarial
    def test_binary_files_skipped(self, tmp_project):
        # A .bin file is not in INDEXABLE_EXTENSIONS
        (tmp_project / "data.bin").write_bytes(b"\x00\x01\x02\xff" * 100)

        idx = CodebaseIndex(tmp_project)
        idx.index()
        assert not any("data.bin" in p for p in idx._entries)

    @pytest.mark.adversarial
    def test_zero_byte_file_skipped(self, tmp_project):
        (tmp_project / "empty.py").write_text("", encoding="utf-8")

        idx = CodebaseIndex(tmp_project)
        idx.index()
        assert not any("empty.py" in p for p in idx._entries)

    @pytest.mark.adversarial
    def test_deep_nesting(self, tmp_path):
        proj = tmp_path / "deep"
        proj.mkdir()
        (proj / ".resonant").mkdir()
        deep = proj / "a" / "b" / "c" / "d" / "e"
        deep.mkdir(parents=True)
        (deep / "leaf.py").write_text("def deep_func(): pass\n", encoding="utf-8")

        idx = CodebaseIndex(proj)
        idx.index()
        matches = [p for p in idx._entries if "leaf.py" in p]
        assert len(matches) == 1
        assert "a/b/c/d/e/leaf.py" in matches[0]

    @pytest.mark.adversarial
    def test_special_chars_in_filename(self, tmp_project):
        # Spaces and hyphens in filename
        (tmp_project / "my file-name.py").write_text("x = 1\n", encoding="utf-8")
        idx = CodebaseIndex(tmp_project)
        idx.index()
        assert any("my file-name.py" in p for p in idx._entries)

    @pytest.mark.adversarial
    def test_search_empty_query(self, tmp_project):
        idx = CodebaseIndex(tmp_project)
        idx.index()
        results = idx.search("")
        # Should not crash; may return results or not
        assert isinstance(results, list)

    @pytest.mark.adversarial
    def test_search_special_characters(self, tmp_project):
        idx = CodebaseIndex(tmp_project)
        idx.index()
        results = idx.search("foo/bar..baz---qux")
        assert isinstance(results, list)

    @pytest.mark.adversarial
    def test_index_entry_to_dict(self):
        entry = IndexEntry(
            path="test.py", language="python", size=100, lines=10,
            hash="abc123", symbols=["foo"], summary="test", imports=["os"],
        )
        d = entry.to_dict()
        assert d["path"] == "test.py"
        assert d["language"] == "python"
        assert d["symbols"] == ["foo"]
        assert d["imports"] == ["os"]
        # hash and last_indexed should NOT be in to_dict
        assert "hash" not in d
        assert "last_indexed" not in d

    @pytest.mark.adversarial
    def test_nonexistent_project_path(self, tmp_path):
        fake = tmp_path / "does_not_exist"
        idx = CodebaseIndex(fake)
        assert idx.file_count == 0

    @pytest.mark.adversarial
    def test_extensionless_makefile_indexed(self, tmp_project):
        (tmp_project / "Makefile").write_text("all:\n\techo hello\n", encoding="utf-8")
        idx = CodebaseIndex(tmp_project)
        idx.index()
        assert any("Makefile" in p for p in idx._entries)

    @pytest.mark.adversarial
    def test_extensionless_dockerfile_indexed(self, tmp_project):
        (tmp_project / "Dockerfile").write_text("FROM python:3.12\n", encoding="utf-8")
        idx = CodebaseIndex(tmp_project)
        idx.index()
        assert any("Dockerfile" in p for p in idx._entries)

    @pytest.mark.adversarial
    def test_unknown_extensionless_file_skipped(self, tmp_project):
        (tmp_project / "RANDOMFILE").write_text("stuff\n", encoding="utf-8")
        idx = CodebaseIndex(tmp_project)
        idx.index()
        assert not any("RANDOMFILE" in p for p in idx._entries)

    @pytest.mark.adversarial
    def test_cache_missing_resonant_dir(self, tmp_path):
        """Indexing creates .resonant dir if needed."""
        proj = tmp_path / "no_resonant"
        proj.mkdir()
        (proj / "hello.py").write_text("print('hi')\n", encoding="utf-8")
        idx = CodebaseIndex(proj)
        idx.index()
        assert (proj / ".resonant" / "index.json").exists()
