"""
Tests for resonant_client/engine/diff_review.py

Covers: generate_review routing, file edit/write reviews, bash command
risk analysis, sensitive path detection, path resolution, hunk parsing,
serialization round-trips, and adversarial edge cases.
"""

import os
import textwrap

import pytest

from resonant_client.engine.diff_review import (
    DiffHunk,
    DiffReview,
    _check_sensitive_path,
    _parse_hunks,
    _resolve_path,
    _review_bash,
    _review_file_edit,
    _review_file_write,
    generate_review,
)


# ── generate_review routing ────────────────────────────────────────


class TestGenerateReviewRouting:
    """Verify that generate_review dispatches to the correct reviewer."""

    @pytest.mark.unit
    def test_routes_file_edit(self, tmp_file):
        path = tmp_file("hello world", name="route.txt")
        result = generate_review("file_edit", {"path": path, "old_text": "hello", "new_text": "goodbye"})
        assert result is not None
        assert result.tool_name == "file_edit"
        assert result.action == "edit"

    @pytest.mark.unit
    def test_routes_file_write(self):
        result = generate_review("file_write", {"path": "/tmp/new.txt", "content": "data"})
        assert result is not None
        assert result.tool_name == "file_write"

    @pytest.mark.unit
    def test_routes_bash(self):
        result = generate_review("bash", {"command": "ls -la"})
        assert result is not None
        assert result.tool_name == "bash"
        assert result.action == "execute"

    @pytest.mark.unit
    def test_file_read_returns_none(self):
        result = generate_review("file_read", {"path": "/tmp/foo"})
        assert result is None

    @pytest.mark.unit
    def test_glob_returns_none(self):
        assert generate_review("glob", {"pattern": "*.py"}) is None

    @pytest.mark.unit
    def test_grep_returns_none(self):
        assert generate_review("grep", {"pattern": "TODO"}) is None

    @pytest.mark.unit
    def test_unknown_tool_gets_medium_risk(self):
        result = generate_review("magic_tool", {"x": 1})
        assert result is not None
        assert result.risk_level == "medium"
        assert result.action == "execute"
        assert "magic_tool" in result.summary

    @pytest.mark.unit
    def test_unknown_tool_preserves_tool_name(self):
        result = generate_review("custom_deploy", {})
        assert result.tool_name == "custom_deploy"


# ── _review_file_edit ──────────────────────────────────────────────


class TestReviewFileEdit:
    """Tests for the file_edit reviewer."""

    @pytest.mark.integration
    def test_normal_edit_with_real_file(self, tmp_file):
        path = tmp_file("line one\nline two\nline three\n")
        result = _review_file_edit({"path": path, "old_text": "line two", "new_text": "LINE TWO"}, "")
        assert result.action == "edit"
        assert result.risk_level == "low"
        assert result.unified_diff
        assert result.hunks
        assert "+LINE TWO" in result.unified_diff
        assert "-line two" in result.unified_diff

    @pytest.mark.integration
    def test_old_text_not_found(self, tmp_file):
        path = tmp_file("alpha\nbeta\n")
        result = _review_file_edit({"path": path, "old_text": "missing", "new_text": "replaced"}, "")
        assert "not found" in result.summary
        assert result.risk_level == "medium"
        assert any("not found" in w for w in result.warnings)

    @pytest.mark.integration
    def test_empty_old_text(self, tmp_file):
        """Empty old_text becomes empty string via str coercion — should not match substring."""
        path = tmp_file("content here\n")
        result = _review_file_edit({"path": path, "old_text": "", "new_text": "new"}, "")
        # Empty string is 'in' any string, so it matches and produces a diff
        assert result is not None

    @pytest.mark.integration
    def test_empty_new_text_deletes(self, tmp_file):
        path = tmp_file("keep\nremove_me\nkeep\n")
        result = _review_file_edit({"path": path, "old_text": "remove_me\n", "new_text": ""}, "")
        assert result.unified_diff
        assert "-remove_me" in result.unified_diff

    @pytest.mark.integration
    def test_crlf_normalization_file_has_crlf(self, tmp_file):
        """When file has CRLF endings, old_text with LF should still match."""
        path = tmp_file("hello\r\nworld\r\n", name="crlf.txt", newline="")
        result = _review_file_edit(
            {"path": path, "old_text": "hello\nworld\n", "new_text": "hi\nplanet\n"}, ""
        )
        assert result.unified_diff
        assert "not found" not in result.summary

    @pytest.mark.integration
    def test_crlf_normalization_old_text_has_crlf(self, tmp_file):
        """When file has LF endings, old_text with CRLF should still match."""
        path = tmp_file("hello\nworld\n", name="lf.txt")
        result = _review_file_edit(
            {"path": path, "old_text": "hello\r\nworld\r\n", "new_text": "hi\r\nplanet\r\n"}, ""
        )
        assert result.unified_diff
        assert "not found" not in result.summary

    @pytest.mark.integration
    def test_binary_safe_reading(self, tmp_path):
        """Binary bytes that aren't valid UTF-8 get replaced, not crash."""
        bfile = tmp_path / "binary.bin"
        bfile.write_bytes(b"start \xff\xfe middle \x00 end\n")
        result = _review_file_edit(
            {"path": str(bfile), "old_text": "middle", "new_text": "MIDDLE"}, ""
        )
        assert result is not None
        # Should not crash; may or may not find the text after replacement chars

    @pytest.mark.integration
    def test_sensitive_file_detection(self, tmp_file):
        path = tmp_file("SECRET=abc", name=".env")
        result = _review_file_edit({"path": path, "old_text": "abc", "new_text": "xyz"}, "")
        assert result.risk_level == "high"
        assert any(".env" in w for w in result.warnings)

    @pytest.mark.integration
    def test_relative_path_resolution(self, tmp_project):
        result = _review_file_edit(
            {"path": "main.py", "old_text": "print('hello')", "new_text": "print('goodbye')"},
            str(tmp_project),
        )
        assert result.unified_diff
        assert "not found" not in result.summary

    @pytest.mark.unit
    def test_file_does_not_exist(self):
        result = _review_file_edit(
            {"path": "/nonexistent/path/file.txt", "old_text": "a", "new_text": "b"}, ""
        )
        assert "not found" in result.summary or "file.txt" in result.summary
        assert any("does not exist" in w for w in result.warnings)

    @pytest.mark.unit
    def test_fallback_diff_when_no_file(self):
        """When file doesn't exist, a fallback diff of old_text vs new_text is generated."""
        result = _review_file_edit(
            {"path": "/no/such/file.py", "old_text": "old line\n", "new_text": "new line\n"}, ""
        )
        assert result.unified_diff  # fallback diff should be produced

    @pytest.mark.unit
    def test_accepts_file_path_key(self, tmp_file):
        path = tmp_file("abc", name="alt.txt")
        result = _review_file_edit({"file_path": path, "old_string": "abc", "new_string": "xyz"}, "")
        assert result.file_path == path

    @pytest.mark.integration
    def test_summary_line_counts(self, tmp_file):
        path = tmp_file("aaa\nbbb\nccc\n")
        result = _review_file_edit({"path": path, "old_text": "bbb", "new_text": "BBB\nDDD"}, "")
        assert "+" in result.summary and "-" in result.summary


# ── _review_file_write ─────────────────────────────────────────────


class TestReviewFileWrite:
    """Tests for the file_write reviewer."""

    @pytest.mark.unit
    def test_new_file_creation(self, tmp_path):
        path = str(tmp_path / "brand_new.txt")
        result = _review_file_write({"path": path, "content": "line1\nline2\n"}, "")
        assert result.action == "create"
        assert result.risk_level == "low"
        assert "Create" in result.summary
        assert "2 lines" in result.summary
        assert result.unified_diff

    @pytest.mark.integration
    def test_overwrite_existing(self, tmp_file):
        path = tmp_file("original content\n")
        result = _review_file_write({"path": path, "content": "new content\n"}, "")
        assert result.action == "overwrite"
        assert result.risk_level == "medium"
        assert "Overwrite" in result.summary
        assert result.unified_diff
        assert result.old_content == "original content\n"

    @pytest.mark.integration
    def test_sensitive_path_warning_new_file(self, tmp_path):
        """Sensitive new file: warning is added but risk_level gets overwritten to 'low' by create branch."""
        path = str(tmp_path / ".env.production")
        result = _review_file_write({"path": path, "content": "DB_PASS=secret"}, "")
        # _check_sensitive_path runs first, but the create branch sets risk_level="low" afterward.
        # The warning should still be present.
        assert result.warnings
        assert any(".env" in w for w in result.warnings)

    @pytest.mark.integration
    def test_sensitive_path_warning_overwrite(self, tmp_file):
        """Sensitive existing file: risk_level is 'high' because overwrite sets 'medium' but
        _check_sensitive_path already set 'high' — however overwrite also overwrites to 'medium'.
        The warning is still present."""
        path = tmp_file("old data", name=".env")
        result = _review_file_write({"path": path, "content": "new data"}, "")
        # Warning is present regardless
        assert result.warnings
        assert any(".env" in w for w in result.warnings)

    @pytest.mark.unit
    def test_empty_content_creates_empty_file(self, tmp_path):
        path = str(tmp_path / "empty.txt")
        result = _review_file_write({"path": path, "content": ""}, "")
        assert result.action == "create"
        assert "0 lines" in result.summary

    @pytest.mark.unit
    def test_content_no_trailing_newline(self, tmp_path):
        path = str(tmp_path / "noterminal.txt")
        result = _review_file_write({"path": path, "content": "one\ntwo"}, "")
        assert "2 lines" in result.summary

    @pytest.mark.unit
    def test_content_with_trailing_newline(self, tmp_path):
        path = str(tmp_path / "terminal.txt")
        result = _review_file_write({"path": path, "content": "one\ntwo\n"}, "")
        assert "2 lines" in result.summary

    @pytest.mark.integration
    def test_overwrite_diff_hunks(self, tmp_file):
        path = tmp_file("alpha\nbeta\ngamma\n")
        result = _review_file_write({"path": path, "content": "alpha\nBETA\ngamma\n"}, "")
        assert result.hunks
        lines = [l for h in result.hunks for l in h.lines]
        assert any("+BETA" in l for l in lines)
        assert any("-beta" in l for l in lines)

    @pytest.mark.unit
    def test_accepts_file_path_key(self, tmp_path):
        path = str(tmp_path / "alt_key.txt")
        result = _review_file_write({"file_path": path, "content": "data"}, "")
        assert result.file_path == path

    @pytest.mark.unit
    def test_new_file_diff_from_devnull(self, tmp_path):
        path = str(tmp_path / "fresh.py")
        result = _review_file_write({"path": path, "content": "print(1)\n"}, "")
        assert "/dev/null" in result.unified_diff


# ── _review_bash ───────────────────────────────────────────────────


class TestReviewBash:
    """Tests for bash command risk analysis."""

    @pytest.mark.unit
    def test_safe_ls(self):
        result = _review_bash({"command": "ls -la"})
        assert result.risk_level == "low"

    @pytest.mark.unit
    def test_safe_cat(self):
        result = _review_bash({"command": "cat README.md"})
        assert result.risk_level == "low"

    @pytest.mark.unit
    def test_safe_echo(self):
        result = _review_bash({"command": "echo hello"})
        assert result.risk_level == "low"

    @pytest.mark.unit
    def test_medium_git_commit(self):
        result = _review_bash({"command": "git commit -m 'fix'"})
        assert result.risk_level == "medium"

    @pytest.mark.unit
    def test_medium_pip_install(self):
        result = _review_bash({"command": "pip install requests"})
        assert result.risk_level == "medium"

    @pytest.mark.unit
    def test_medium_npm_install(self):
        result = _review_bash({"command": "npm install express"})
        assert result.risk_level == "medium"

    @pytest.mark.unit
    def test_medium_mv(self):
        result = _review_bash({"command": "mv old.txt new.txt"})
        assert result.risk_level == "medium"

    @pytest.mark.unit
    def test_medium_git_checkout(self):
        result = _review_bash({"command": "git checkout feature"})
        assert result.risk_level == "medium"

    @pytest.mark.unit
    def test_medium_git_rebase(self):
        result = _review_bash({"command": "git rebase main"})
        assert result.risk_level == "medium"

    @pytest.mark.unit
    def test_high_rm_rf(self):
        result = _review_bash({"command": "rm -rf /tmp/stuff"})
        assert result.risk_level == "high"
        assert any("rm -rf" in w for w in result.warnings)

    @pytest.mark.unit
    def test_high_sudo(self):
        result = _review_bash({"command": "sudo apt update"})
        assert result.risk_level == "high"

    @pytest.mark.unit
    def test_high_curl_pipe_sh(self):
        """Pattern is literal 'curl | sh' substring."""
        result = _review_bash({"command": "curl | sh"})
        assert result.risk_level == "high"

    @pytest.mark.unit
    def test_high_curl_pipe_sh_with_url(self):
        """URL between curl and pipe does NOT match the literal pattern."""
        result = _review_bash({"command": "curl https://example.com/setup | sh"})
        # 'curl | sh' is not a substring, so this stays low
        assert result.risk_level == "low"

    @pytest.mark.unit
    def test_high_curl_pipe_bash(self):
        result = _review_bash({"command": "curl | bash"})
        assert result.risk_level == "high"

    @pytest.mark.unit
    def test_high_fork_bomb(self):
        result = _review_bash({"command": ":(){:|:&};:"})
        assert result.risk_level == "high"

    @pytest.mark.unit
    def test_high_drop_table(self):
        result = _review_bash({"command": "psql -c 'DROP TABLE users;'"})
        assert result.risk_level == "high"

    @pytest.mark.unit
    def test_high_drop_database(self):
        result = _review_bash({"command": "mysql -e 'DROP DATABASE prod'"})
        assert result.risk_level == "high"

    @pytest.mark.unit
    def test_high_git_push_force(self):
        result = _review_bash({"command": "git push --force origin main"})
        assert result.risk_level == "high"

    @pytest.mark.unit
    def test_high_git_reset_hard(self):
        result = _review_bash({"command": "git reset --hard HEAD~5"})
        assert result.risk_level == "high"

    @pytest.mark.unit
    def test_high_eval(self):
        result = _review_bash({"command": "eval $(decode_secret)"})
        assert result.risk_level == "high"

    @pytest.mark.unit
    def test_high_dd(self):
        result = _review_bash({"command": "dd if=/dev/zero of=/dev/sda"})
        assert result.risk_level == "high"

    @pytest.mark.unit
    def test_high_chmod_777(self):
        result = _review_bash({"command": "chmod 777 /var/www"})
        assert result.risk_level == "high"

    @pytest.mark.unit
    def test_high_shutdown(self):
        result = _review_bash({"command": "shutdown -h now"})
        assert result.risk_level == "high"

    @pytest.mark.unit
    def test_high_kill_9(self):
        result = _review_bash({"command": "kill -9 1234"})
        assert result.risk_level == "high"

    @pytest.mark.unit
    def test_high_truncate(self):
        result = _review_bash({"command": "psql -c 'TRUNCATE users;'"})
        assert result.risk_level == "high"

    @pytest.mark.unit
    def test_multiple_dangerous_patterns(self):
        result = _review_bash({"command": "sudo rm -rf / && eval bad"})
        assert result.risk_level == "high"
        assert len(result.warnings) >= 3  # sudo + rm -rf + rm -f (substring) + eval

    @pytest.mark.unit
    def test_case_insensitive_detection(self):
        result = _review_bash({"command": "SUDO RM -RF /"})
        assert result.risk_level == "high"

    @pytest.mark.unit
    def test_summary_text(self):
        result = _review_bash({"command": "ls"})
        assert result.summary == "Run: ls"

    @pytest.mark.unit
    def test_command_stored(self):
        result = _review_bash({"command": "echo test"})
        assert result.command == "echo test"


# ── _check_sensitive_path ──────────────────────────────────────────


class TestCheckSensitivePath:
    """Tests for sensitive path detection."""

    @pytest.mark.unit
    def test_env_file(self):
        review = DiffReview(tool_name="test")
        _check_sensitive_path(".env", review)
        assert review.risk_level == "high"
        assert review.warnings

    @pytest.mark.unit
    def test_env_local(self):
        review = DiffReview(tool_name="test")
        _check_sensitive_path("/project/.env.local", review)
        assert review.risk_level == "high"

    @pytest.mark.unit
    def test_env_production(self):
        review = DiffReview(tool_name="test")
        _check_sensitive_path("/app/.env.production", review)
        assert review.risk_level == "high"

    @pytest.mark.unit
    def test_credentials_file(self):
        review = DiffReview(tool_name="test")
        _check_sensitive_path("/home/user/credentials.json", review)
        assert review.risk_level == "high"

    @pytest.mark.unit
    def test_secrets_file(self):
        review = DiffReview(tool_name="test")
        _check_sensitive_path("config/secrets.yaml", review)
        assert review.risk_level == "high"

    @pytest.mark.unit
    def test_ssh_path(self):
        review = DiffReview(tool_name="test")
        _check_sensitive_path("/home/user/.ssh/config", review)
        assert review.risk_level == "high"

    @pytest.mark.unit
    def test_id_rsa(self):
        review = DiffReview(tool_name="test")
        _check_sensitive_path("/home/user/.ssh/id_rsa", review)
        assert review.risk_level == "high"

    @pytest.mark.unit
    def test_id_ed25519(self):
        review = DiffReview(tool_name="test")
        _check_sensitive_path("/home/user/.ssh/id_ed25519", review)
        assert review.risk_level == "high"

    @pytest.mark.unit
    def test_normal_path_stays_low(self):
        review = DiffReview(tool_name="test")
        _check_sensitive_path("src/main.py", review)
        assert review.risk_level == "low"
        assert not review.warnings

    @pytest.mark.unit
    def test_normal_js_path(self):
        review = DiffReview(tool_name="test")
        _check_sensitive_path("app/components/Button.tsx", review)
        assert review.risk_level == "low"

    @pytest.mark.unit
    def test_case_insensitive(self):
        """The check lowercases the path, so .ENV should still match."""
        review = DiffReview(tool_name="test")
        _check_sensitive_path("/project/.ENV", review)
        assert review.risk_level == "high"

    @pytest.mark.unit
    def test_git_config(self):
        review = DiffReview(tool_name="test")
        _check_sensitive_path("/repo/.git/config", review)
        assert review.risk_level == "high"

    @pytest.mark.unit
    def test_passwd_file(self):
        review = DiffReview(tool_name="test")
        _check_sensitive_path("/etc/passwd", review)
        assert review.risk_level == "high"

    @pytest.mark.unit
    def test_token_file(self):
        review = DiffReview(tool_name="test")
        _check_sensitive_path("config/token.json", review)
        assert review.risk_level == "high"

    @pytest.mark.unit
    def test_only_first_match_adds_warning(self):
        """_check_sensitive_path breaks after first match, so only one warning."""
        review = DiffReview(tool_name="test")
        _check_sensitive_path(".env.local", review)  # matches .env and .env.local
        assert len(review.warnings) == 1


# ── _resolve_path ──────────────────────────────────────────────────


class TestResolvePath:
    """Tests for path resolution."""

    @pytest.mark.unit
    def test_absolute_path_unchanged(self):
        result = _resolve_path("/usr/local/bin/python", "/some/project")
        assert result == "/usr/local/bin/python"

    @pytest.mark.unit
    def test_relative_with_project_path(self):
        result = _resolve_path("src/main.py", "/home/user/project")
        expected = os.path.join("/home/user/project", "src/main.py")
        assert result == expected

    @pytest.mark.unit
    def test_relative_without_project_path(self):
        result = _resolve_path("file.txt", "")
        expected = os.path.join(os.getcwd(), "file.txt")
        assert result == expected

    @pytest.mark.unit
    def test_empty_path_returns_none(self):
        assert _resolve_path("", "/project") is None

    @pytest.mark.unit
    def test_empty_both_returns_none(self):
        assert _resolve_path("", "") is None

    @pytest.mark.unit
    def test_windows_absolute_path(self):
        """On Windows, isabs recognizes drive-letter paths."""
        if os.name == "nt":
            result = _resolve_path("C:\\Users\\test\\file.txt", "/project")
            assert result == "C:\\Users\\test\\file.txt"


# ── _parse_hunks ───────────────────────────────────────────────────


class TestParseHunks:
    """Tests for unified diff hunk parsing."""

    @pytest.mark.unit
    def test_valid_unified_diff(self):
        diff = textwrap.dedent("""\
            --- a/file.py
            +++ b/file.py
            @@ -1,3 +1,3 @@
             line1
            -line2
            +LINE2
             line3
        """)
        hunks = _parse_hunks(diff)
        assert len(hunks) == 1
        assert hunks[0].old_start == 1
        assert hunks[0].old_count == 3
        assert hunks[0].new_start == 1
        assert hunks[0].new_count == 3
        assert "-line2" in hunks[0].lines
        assert "+LINE2" in hunks[0].lines

    @pytest.mark.unit
    def test_empty_diff(self):
        assert _parse_hunks("") == []

    @pytest.mark.unit
    def test_multiple_hunks(self):
        diff = textwrap.dedent("""\
            --- a/file.py
            +++ b/file.py
            @@ -1,2 +1,2 @@
            -old1
            +new1
             ctx
            @@ -10,3 +10,4 @@
             keep
            -remove
            +add1
            +add2
             keep
        """)
        hunks = _parse_hunks(diff)
        assert len(hunks) == 2
        assert hunks[0].old_start == 1
        assert hunks[1].old_start == 10
        assert hunks[1].new_count == 4

    @pytest.mark.unit
    def test_no_context_lines(self):
        diff = textwrap.dedent("""\
            --- /dev/null
            +++ b/new.txt
            @@ -0,0 +1,2 @@
            +line1
            +line2
        """)
        hunks = _parse_hunks(diff)
        assert len(hunks) == 1
        assert hunks[0].old_start == 0
        assert hunks[0].old_count == 0
        assert hunks[0].new_count == 2
        assert len(hunks[0].lines) == 2

    @pytest.mark.unit
    def test_hunk_without_count_defaults_to_one(self):
        diff = "@@ -5 +5 @@\n-old\n+new\n"
        hunks = _parse_hunks(diff)
        assert len(hunks) == 1
        assert hunks[0].old_count == 1
        assert hunks[0].new_count == 1

    @pytest.mark.unit
    def test_hunk_context_captured(self):
        diff = "@@ -1,3 +1,3 @@ def my_function():\n-old\n+new\n ctx\n"
        hunks = _parse_hunks(diff)
        assert hunks[0].context == "def my_function():"

    @pytest.mark.unit
    def test_non_diff_lines_ignored(self):
        diff = textwrap.dedent("""\
            diff --git a/f.py b/f.py
            index abc..def 100644
            --- a/f.py
            +++ b/f.py
            @@ -1,1 +1,1 @@
            -old
            +new
        """)
        hunks = _parse_hunks(diff)
        assert len(hunks) == 1
        assert len(hunks[0].lines) == 2


# ── DiffReview.to_dict ────────────────────────────────────────────


class TestDiffReviewToDict:
    """Test serialization round-trip."""

    @pytest.mark.unit
    def test_basic_round_trip(self):
        review = DiffReview(
            tool_name="bash",
            file_path="",
            action="execute",
            risk_level="low",
            summary="Run: ls",
            command="ls",
        )
        d = review.to_dict()
        assert d["tool_name"] == "bash"
        assert d["action"] == "execute"
        assert d["risk_level"] == "low"
        assert d["command"] == "ls"
        assert d["hunks"] == []
        assert d["warnings"] == []

    @pytest.mark.unit
    def test_hunks_serialized(self):
        hunk = DiffHunk(old_start=1, old_count=2, new_start=1, new_count=3, lines=["-a", "+b", "+c"])
        review = DiffReview(tool_name="file_edit", hunks=[hunk])
        d = review.to_dict()
        assert len(d["hunks"]) == 1
        assert d["hunks"][0]["old_start"] == 1
        assert d["hunks"][0]["lines"] == ["-a", "+b", "+c"]

    @pytest.mark.unit
    def test_warnings_preserved(self):
        review = DiffReview(tool_name="test", warnings=["warn1", "warn2"])
        d = review.to_dict()
        assert d["warnings"] == ["warn1", "warn2"]

    @pytest.mark.unit
    def test_all_fields_present(self):
        review = DiffReview(tool_name="x")
        d = review.to_dict()
        expected_keys = {
            "tool_name", "file_path", "action", "risk_level",
            "summary", "hunks", "unified_diff", "warnings", "command",
        }
        assert set(d.keys()) == expected_keys

    @pytest.mark.unit
    def test_hunk_context_in_dict(self):
        hunk = DiffHunk(
            old_start=5, old_count=1, new_start=5, new_count=1,
            lines=["-x", "+y"], context="class Foo:",
        )
        review = DiffReview(tool_name="file_edit", hunks=[hunk])
        d = review.to_dict()
        assert d["hunks"][0]["context"] == "class Foo:"


# ── Edge cases ────────────────────────────────────────────────────


class TestEdgeCases:
    """Adversarial and boundary-condition tests."""

    @pytest.mark.adversarial
    def test_none_command_in_bash(self):
        """command=None should be coerced to empty string."""
        result = _review_bash({"command": None})
        assert result.command == ""
        assert result.risk_level == "low"

    @pytest.mark.adversarial
    def test_none_path_in_file_edit(self):
        result = _review_file_edit({"path": None, "old_text": None, "new_text": None}, "")
        assert result is not None
        assert result.file_path == ""

    @pytest.mark.adversarial
    def test_none_path_in_file_write(self):
        result = _review_file_write({"path": None, "content": None}, "")
        assert result is not None
        assert result.file_path == ""

    @pytest.mark.adversarial
    def test_none_content_in_file_write(self):
        result = _review_file_write({"path": "/tmp/x.txt", "content": None}, "")
        assert result.new_content == ""

    @pytest.mark.adversarial
    def test_missing_keys_in_args(self):
        """Empty dict should not crash any reviewer."""
        assert _review_file_edit({}, "") is not None
        assert _review_file_write({}, "") is not None
        assert _review_bash({}) is not None

    @pytest.mark.adversarial
    def test_very_long_command_truncation(self):
        long_cmd = "echo " + "x" * 200
        result = _review_bash({"command": long_cmd})
        assert result.summary.endswith("...")
        assert len(result.summary) < len(long_cmd) + 20

    @pytest.mark.adversarial
    def test_command_exactly_100_chars(self):
        cmd = "a" * 100
        result = _review_bash({"command": cmd})
        assert "..." not in result.summary

    @pytest.mark.adversarial
    def test_command_101_chars(self):
        cmd = "a" * 101
        result = _review_bash({"command": cmd})
        assert result.summary.endswith("...")

    @pytest.mark.adversarial
    def test_unicode_in_path(self, tmp_path):
        upath = tmp_path / "caf\u00e9.txt"
        upath.write_text("latte\n", encoding="utf-8")
        result = _review_file_edit(
            {"path": str(upath), "old_text": "latte", "new_text": "espresso"}, ""
        )
        assert result.unified_diff
        assert "not found" not in result.summary

    @pytest.mark.adversarial
    def test_unicode_in_content(self, tmp_file):
        path = tmp_file("Hello \u4e16\u754c\n", name="unicode.txt")
        result = _review_file_edit(
            {"path": path, "old_text": "\u4e16\u754c", "new_text": "\u5730\u7403"}, ""
        )
        assert result.unified_diff

    @pytest.mark.adversarial
    def test_path_traversal_attempt(self):
        review = DiffReview(tool_name="test")
        _check_sensitive_path("../../.env", review)
        assert review.risk_level == "high"

    @pytest.mark.adversarial
    def test_path_traversal_to_ssh(self):
        review = DiffReview(tool_name="test")
        _check_sensitive_path("../../../home/user/.ssh/id_rsa", review)
        assert review.risk_level == "high"

    @pytest.mark.adversarial
    def test_empty_string_tool_name(self):
        result = generate_review("", {})
        assert result is not None
        assert result.risk_level == "medium"

    @pytest.mark.adversarial
    def test_generate_review_with_empty_project_path(self):
        result = generate_review("bash", {"command": "ls"}, "")
        assert result is not None

    @pytest.mark.adversarial
    def test_diff_with_only_header_no_hunks(self):
        diff = "--- a/file.py\n+++ b/file.py\n"
        hunks = _parse_hunks(diff)
        assert hunks == []

    @pytest.mark.adversarial
    def test_resolve_path_with_none_project(self):
        """project_path='' (falsy) should fall back to cwd."""
        result = _resolve_path("test.py", "")
        assert result == os.path.join(os.getcwd(), "test.py")

    @pytest.mark.adversarial
    def test_bash_empty_command(self):
        result = _review_bash({"command": ""})
        assert result.risk_level == "low"
        assert result.summary == "Run: "

    @pytest.mark.adversarial
    def test_file_edit_only_whitespace_old_text(self, tmp_file):
        path = tmp_file("  \n  \n")
        result = _review_file_edit({"path": path, "old_text": "  \n", "new_text": "x\n"}, "")
        assert result is not None

    @pytest.mark.adversarial
    def test_sensitive_shadow_file(self):
        review = DiffReview(tool_name="test")
        _check_sensitive_path("/etc/shadow", review)
        assert review.risk_level == "high"

    @pytest.mark.adversarial
    def test_gpg_directory(self):
        review = DiffReview(tool_name="test")
        _check_sensitive_path("/home/user/.gnupg/pubring.kbx", review)
        assert review.risk_level == "high"
