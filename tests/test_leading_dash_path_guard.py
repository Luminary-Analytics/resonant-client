"""Tests for v0.5.7a3 — `engine/tools.py::_validate_write_path` and
its integration into `_exec_file_write` and `_exec_file_edit`.

Linux-bridge field-observation #8: a file literally named `-p`
appeared at the project root mid-iteration, almost certainly from a
shell-tokenization slip where `mkdir -p src` got split into three
separate args and `-p` landed in a tool's `path` argument by mistake.

Coverage:
- Rejects basename starting with `-` (the field-observed case)
- Rejects intermediate path segments starting with `-`
- Accepts windows drive specs (C:\\) and . / .. specials
- Allows opt-in via `allow_leading_dash=true`
- Empty path is a no-op (a different error path catches that later)
- Both file_write and file_edit honor the validator
"""
from __future__ import annotations



from resonant_client.engine.tools import (
    _exec_file_edit,
    _exec_file_write,
    _validate_write_path,
)


# ── Direct validator unit tests ─────────────────────────────────────────


class TestValidateWritePath:
    def test_plain_path_passes(self):
        assert _validate_write_path("src/foo.py", allow_leading_dash=False) == ""

    def test_basename_starts_with_dash_rejected(self):
        # The field-observed case: `mkdir -p src` tokenized into
        # three args, `-p` landed in path.
        err = _validate_write_path("-p", allow_leading_dash=False)
        assert err
        assert "tokenization slip" in err
        assert "allow_leading_dash" in err

    def test_basename_dash_with_extension_rejected(self):
        # `-rc1.tag` — even with an extension, the leading dash makes
        # this a foot-gun for downstream tools (rm, find, etc.).
        err = _validate_write_path("-rc1.tag", allow_leading_dash=False)
        assert err

    def test_intermediate_segment_starts_with_dash_rejected(self):
        # `-p/foo.txt` would create a directory named `-p` then write
        # foo.txt into it. Same foot-gun, just one level deeper.
        err = _validate_write_path("-p/foo.txt", allow_leading_dash=False)
        assert err
        assert "-p" in err

    def test_deep_intermediate_segment_rejected(self):
        err = _validate_write_path("src/-evil/foo.txt", allow_leading_dash=False)
        assert err
        assert "-evil" in err

    def test_allow_leading_dash_opt_in(self):
        # Real edge case: a project may legitimately want a file
        # whose name starts with `-` (extremely rare but possible).
        # Opt-in escape hatch.
        assert _validate_write_path("-rc1.tag", allow_leading_dash=True) == ""
        assert _validate_write_path("-p/foo.txt", allow_leading_dash=True) == ""

    def test_empty_path_passes_validator(self):
        # The validator isn't responsible for empty-path detection;
        # downstream Path() construction will surface that. Stay
        # focused on the leading-dash check.
        assert _validate_write_path("", allow_leading_dash=False) == ""

    def test_dot_segments_skipped(self):
        # `./foo.txt` and `../foo.txt` are common; `.` and `..` are
        # NOT a leading-dash hazard.
        assert _validate_write_path("./foo.txt", allow_leading_dash=False) == ""
        assert _validate_write_path("../foo.txt", allow_leading_dash=False) == ""

    def test_windows_drive_spec_skipped(self):
        # Windows path like C:\\Users\\me\\foo.txt — the `C:` segment
        # ends with `:` (drive spec) and must not trigger validation.
        assert _validate_write_path("C:\\foo.txt", allow_leading_dash=False) == ""
        assert _validate_write_path("D:/Repos/proj/foo.txt", allow_leading_dash=False) == ""

    def test_unix_root_path_passes(self):
        assert _validate_write_path("/etc/hosts", allow_leading_dash=False) == ""


# ── Integration tests via _exec_file_write ──────────────────────────────


class TestFileWriteRejection:
    def test_dash_basename_returns_is_error(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        result = _exec_file_write(
            {"path": "-p", "content": "should not be written"},
            start=0.0,
        )
        assert result.is_error is True
        assert "Refusing to write" in result.output
        # File must not exist on disk.
        assert not (tmp_path / "-p").exists()

    def test_intermediate_dash_segment_blocked(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        result = _exec_file_write(
            {"path": "src/-evil/foo.txt", "content": "x"},
            start=0.0,
        )
        assert result.is_error is True
        assert not (tmp_path / "src" / "-evil" / "foo.txt").exists()
        # The `src/-evil/` directory shouldn't have been created either.
        assert not (tmp_path / "src" / "-evil").exists()

    def test_allow_leading_dash_opt_in_works(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        result = _exec_file_write(
            {
                "path": "-genuine.tag",
                "content": "intentional file",
                "allow_leading_dash": True,
            },
            start=0.0,
        )
        assert result.is_error is False
        assert (tmp_path / "-genuine.tag").exists()
        assert (tmp_path / "-genuine.tag").read_text(encoding="utf-8") == "intentional file"

    def test_normal_path_still_works(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        result = _exec_file_write(
            {"path": "src/foo.py", "content": "print('hi')"},
            start=0.0,
        )
        assert result.is_error is False
        assert (tmp_path / "src" / "foo.py").read_text(encoding="utf-8") == "print('hi')"


# ── Integration tests via _exec_file_edit ───────────────────────────────


class TestFileEditRejection:
    def test_dash_basename_returns_is_error(self, tmp_path, monkeypatch):
        # Pre-create a file (with allow_leading_dash) so we can test
        # that EDITING a leading-dash path is also blocked.
        monkeypatch.chdir(tmp_path)
        target = tmp_path / "-foo"
        target.write_text("original", encoding="utf-8")

        result = _exec_file_edit(
            {"path": "-foo", "old_text": "original", "new_text": "edited"},
            start=0.0,
        )
        assert result.is_error is True
        assert "Refusing to write" in result.output
        # File contents must be untouched.
        assert target.read_text(encoding="utf-8") == "original"

    def test_allow_leading_dash_opt_in_works(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        target = tmp_path / "-foo"
        target.write_text("original", encoding="utf-8")

        result = _exec_file_edit(
            {
                "path": "-foo",
                "old_text": "original",
                "new_text": "edited",
                "allow_leading_dash": True,
            },
            start=0.0,
        )
        assert result.is_error is False
        assert target.read_text(encoding="utf-8") == "edited"

    def test_normal_edit_still_works(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        target = tmp_path / "src" / "foo.py"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("print('original')", encoding="utf-8")

        result = _exec_file_edit(
            {
                "path": "src/foo.py",
                "old_text": "original",
                "new_text": "edited",
            },
            start=0.0,
        )
        assert result.is_error is False
        assert "edited" in target.read_text(encoding="utf-8")
