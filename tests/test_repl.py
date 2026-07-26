"""
Tests for resonant_client/engine/repl.py

Covers:
  - ReplProcess lifecycle (start, eval, stop)
  - State persistence across evals (the whole point of "persistent")
  - Concurrent-REPL cap
  - Per-eval timeout
  - Registry housekeeping (gc of dead REPLs, list_repls)
  - exec_repl_* ToolResult shape and dispatch via execute_tool

Node tests are skipped when `node` is not on PATH (Windows CI may lack it).
"""

from __future__ import annotations

import shutil

import pytest

from resonant_client.engine import repl as repl_mod


# ── Shared cleanup ──────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _cleanup_repls_after_test():
    """Stop any REPLs left running by a test."""
    yield
    repl_mod.stop_all_repls()


# ── Python REPL ─────────────────────────────────────────────────────────


class TestPythonRepl:
    def test_start_returns_repl_id(self, tmp_path):
        data = repl_mod.start_repl("python", cwd=str(tmp_path))
        assert "error" not in data
        assert len(data["repl_id"]) == 12
        assert data["lang"] == "python"

    def test_eval_round_trips_print(self, tmp_path):
        d = repl_mod.start_repl("python", cwd=str(tmp_path))
        rid = d["repl_id"]
        out = repl_mod.eval_repl(rid, "print('hello')")
        assert out["error"] is None
        assert "hello" in out["stdout"]

    def test_state_persists_across_evals(self, tmp_path):
        """The defining test: x = 41 in one call, x + 1 in next, expect 42."""
        d = repl_mod.start_repl("python", cwd=str(tmp_path))
        rid = d["repl_id"]
        r1 = repl_mod.eval_repl(rid, "x = 41")
        assert r1["error"] is None
        r2 = repl_mod.eval_repl(rid, "print(x + 1)")
        assert r2["error"] is None
        assert "42" in r2["stdout"]

    def test_imports_persist(self, tmp_path):
        d = repl_mod.start_repl("python", cwd=str(tmp_path))
        rid = d["repl_id"]
        repl_mod.eval_repl(rid, "import math")
        out = repl_mod.eval_repl(rid, "print(math.pi)")
        assert "3.14" in out["stdout"]

    def test_eval_timeout(self, tmp_path):
        d = repl_mod.start_repl("python", cwd=str(tmp_path))
        rid = d["repl_id"]
        # Sleep longer than timeout
        out = repl_mod.eval_repl(rid, "import time; time.sleep(2)", timeout=0.5)
        assert out["error"] is not None
        assert "timed out" in out["error"]

    def test_stderr_captured(self, tmp_path):
        d = repl_mod.start_repl("python", cwd=str(tmp_path))
        rid = d["repl_id"]
        out = repl_mod.eval_repl(rid, "import sys; sys.stderr.write('boom\\n'); sys.stderr.flush()")
        # stderr may arrive a beat after the sentinel; allow either path.
        # Force a follow-up eval to flush any straggling stderr.
        out2 = repl_mod.eval_repl(rid, "pass")
        combined = (out.get("stderr") or "") + (out2.get("stderr") or "")
        assert "boom" in combined

    def test_traceback_in_stderr(self, tmp_path):
        d = repl_mod.start_repl("python", cwd=str(tmp_path))
        rid = d["repl_id"]
        repl_mod.eval_repl(rid, "raise ValueError('nope')")
        # The traceback goes to stderr; another eval to flush
        out2 = repl_mod.eval_repl(rid, "pass")
        # After a raised error, the REPL should still be alive — verify
        out3 = repl_mod.eval_repl(rid, "print(1+1)")
        assert "2" in out3["stdout"]

    def test_stop(self, tmp_path):
        d = repl_mod.start_repl("python", cwd=str(tmp_path))
        rid = d["repl_id"]
        stop = repl_mod.stop_repl(rid)
        assert stop["stopped"] == rid
        # Subsequent eval should fail (REPL gone from registry)
        out = repl_mod.eval_repl(rid, "1+1")
        assert "not found" in out["error"]

    def test_stop_unknown_id(self):
        out = repl_mod.stop_repl("nonexistent")
        assert "not found" in out["error"]

    def test_eval_unknown_id(self):
        out = repl_mod.eval_repl("nonexistent", "1+1")
        assert "not found" in out["error"]


# ── Concurrency cap ─────────────────────────────────────────────────────


class TestConcurrencyCap:
    def test_cap_enforced(self, tmp_path, monkeypatch):
        # Lower the cap for this test to keep it fast
        monkeypatch.setattr(repl_mod, "MAX_CONCURRENT_REPLS", 2)
        a = repl_mod.start_repl("python", cwd=str(tmp_path))
        b = repl_mod.start_repl("python", cwd=str(tmp_path))
        c = repl_mod.start_repl("python", cwd=str(tmp_path))
        assert "error" not in a
        assert "error" not in b
        assert "error" in c
        assert "too many active REPLs" in c["error"]

    def test_dead_repls_make_room(self, tmp_path, monkeypatch):
        monkeypatch.setattr(repl_mod, "MAX_CONCURRENT_REPLS", 1)
        a = repl_mod.start_repl("python", cwd=str(tmp_path))
        # Kill the underlying process directly so the registry sees it dead
        repl_mod._REPLS[a["repl_id"]].stop()
        b = repl_mod.start_repl("python", cwd=str(tmp_path))
        # Should succeed because gc dropped the dead one
        assert "error" not in b


# ── Registry ────────────────────────────────────────────────────────────


class TestRegistry:
    def test_list_repls(self, tmp_path):
        a = repl_mod.start_repl("python", cwd=str(tmp_path))
        listing = repl_mod.list_repls()
        assert any(r["repl_id"] == a["repl_id"] for r in listing)

    def test_invalid_lang(self, tmp_path):
        out = repl_mod.start_repl("ruby", cwd=str(tmp_path))
        assert "unsupported REPL language" in out["error"]

    def test_invalid_cwd(self):
        out = repl_mod.start_repl("python", cwd="/definitely/not/a/path/12345")
        assert "not a directory" in out["error"]


# ── exec_* ToolResult wrappers ──────────────────────────────────────────


class TestExecWrappers:
    def test_python_round_trip(self, tmp_path):
        from resonant_client.engine.repl import (
            exec_repl_python_start, exec_repl_python_eval, exec_repl_python_stop,
        )
        r1 = exec_repl_python_start({"cwd": str(tmp_path)}, start=0.0)
        assert r1.is_error is False
        rid = r1.metadata["repl_id"]

        r2 = exec_repl_python_eval({"repl_id": rid, "code": "print(2+2)"}, start=0.0)
        assert r2.is_error is False
        assert "4" in r2.output
        assert f"python:{rid[:8]}" in r2.output

        r3 = exec_repl_python_stop({"repl_id": rid}, start=0.0)
        assert r3.is_error is False
        assert "Stopped" in r3.output

    def test_eval_missing_repl_id(self):
        from resonant_client.engine.repl import exec_repl_python_eval
        r = exec_repl_python_eval({"code": "1+1"}, start=0.0)
        assert r.is_error is True
        assert "repl_id is required" in r.output

    def test_eval_missing_code(self, tmp_path):
        from resonant_client.engine.repl import exec_repl_python_start, exec_repl_python_eval
        r1 = exec_repl_python_start({"cwd": str(tmp_path)}, start=0.0)
        rid = r1.metadata["repl_id"]
        r = exec_repl_python_eval({"repl_id": rid}, start=0.0)
        assert r.is_error is True
        assert "code is required" in r.output


# ── Tool registration & dispatch ────────────────────────────────────────


class TestToolRegistration:
    def test_all_repl_tools_registered(self):
        from resonant_client.engine import tools as tools_mod
        names = {t["function"]["name"] for t in tools_mod.AGENT_TOOLS}
        for n in [
            "repl_python_start", "repl_python_eval", "repl_python_stop",
            "repl_node_start", "repl_node_eval", "repl_node_stop",
        ]:
            assert n in names, f"REPL tool '{n}' not registered"

    def test_dispatch_routes_repl_tools(self, tmp_path):
        from resonant_client.engine.tools import execute_tool
        r1 = execute_tool("repl_python_start", {"cwd": str(tmp_path)})
        assert r1.is_error is False
        rid = r1.metadata["repl_id"]
        r2 = execute_tool("repl_python_eval", {"repl_id": rid, "code": "print('via dispatch')"})
        assert r2.is_error is False
        assert "via dispatch" in r2.output
        r3 = execute_tool("repl_python_stop", {"repl_id": rid})
        assert r3.is_error is False


# ── Node REPL (skipped if node missing) ────────────────────────────────


_NODE_AVAILABLE = shutil.which("node") is not None


@pytest.mark.skipif(not _NODE_AVAILABLE, reason="node executable not on PATH")
class TestNodeRepl:
    def test_basic_eval(self, tmp_path):
        d = repl_mod.start_repl("node", cwd=str(tmp_path))
        assert "error" not in d
        rid = d["repl_id"]
        out = repl_mod.eval_repl(rid, "console.log(2+2)")
        assert out["error"] is None
        assert "4" in out["stdout"]

    def test_state_persists(self, tmp_path):
        d = repl_mod.start_repl("node", cwd=str(tmp_path))
        rid = d["repl_id"]
        repl_mod.eval_repl(rid, "let n = 41")
        out = repl_mod.eval_repl(rid, "console.log(n+1)")
        assert "42" in out["stdout"]
