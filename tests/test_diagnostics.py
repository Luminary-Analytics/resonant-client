"""
Tests for the v0.3.4 diagnostics bundle (Help → Save Diagnostics).

The bundle's job is to ship redacted user logs to GitHub issues without
leaking API keys. Coverage is split:
  - `redact()` — defense-in-depth pattern coverage
  - `build_diagnostics_zip()` — end-to-end ZIP shape, file-selection
    rules, and *whole-file* redaction (one full pipeline assertion that
    no secret leaks survive the round-trip)
"""

from __future__ import annotations

import json
import time
import zipfile
from pathlib import Path

import pytest

from resonant_client.gui.diagnostics import (
    LATEST_N_INTENTS,
    LATEST_N_SESSIONS,
    build_diagnostics_zip,
    default_output_dir,
    redact,
)


# ── redact() — pattern coverage ──────────────────────────────────────────


class TestRedactPrefixedTokens:
    def test_redacts_openai_sk_token(self):
        assert redact("OPENAI_API_KEY=sk-abc1234567890XYZ") != \
               "OPENAI_API_KEY=sk-abc1234567890XYZ"
        assert "sk-abc1234567890XYZ" not in redact("token sk-abc1234567890XYZ in log")

    def test_redacts_github_pat(self):
        line = "GITHUB_TOKEN=ghp_AbC123dEf456GhI789jKl0mNo123pQr456sTuV"
        result = redact(line)
        assert "AbC123dEf456GhI789" not in result
        assert "[REDACTED]" in result

    def test_does_not_clobber_normal_text(self):
        # Pattern is anchored on the prefix — random text shouldn't trip.
        line = "this is a normal log line with no secrets"
        assert redact(line) == line


class TestRedactAuthHeaders:
    def test_bearer_token_stripped(self):
        line = 'Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.payload.sig'
        result = redact(line)
        assert "eyJhbGciOiJIUzI1NiI" not in result
        assert "Authorization: Bearer [REDACTED]" in result

    def test_x_api_key_header(self):
        line = 'x-api-key: skq_AbCdEf0123456789'
        result = redact(line)
        assert "AbCdEf0123456789" not in result
        assert "[REDACTED]" in result

    def test_lowercase_authorization(self):
        line = 'authorization: bearer abc123def456ghi'
        result = redact(line)
        assert "abc123def456ghi" not in result


class TestRedactJsonFields:
    def test_api_key_field(self):
        line = '{"api_key": "sk-secretvalue1234", "model": "claude-sonnet"}'
        result = redact(line)
        assert "sk-secretvalue1234" not in result
        assert '"model": "claude-sonnet"' in result  # other fields untouched

    def test_password_field(self):
        line = '{"password": "hunter2-correct-horse"}'
        result = redact(line)
        assert "hunter2-correct-horse" not in result

    def test_token_field(self):
        line = '{"token": "abcdefghijklmnop"}'
        result = redact(line)
        assert "abcdefghijklmnop" not in result

    def test_secret_field(self):
        # Mixed-case "Secret" should still match (?i flag).
        line = '{"Secret": "very-very-secret-value"}'
        assert "very-very-secret-value" not in redact(line)


class TestRedactEnvAssignments:
    def test_anthropic_api_key(self):
        line = "ANTHROPIC_API_KEY=sk-ant-api03-abc123"
        result = redact(line)
        assert "sk-ant-api03-abc123" not in result

    def test_openai_api_key(self):
        line = "OPENAI_API_KEY=sk-proj-abcdef123456"
        result = redact(line)
        assert "sk-proj-abcdef123456" not in result

    def test_github_token_env(self):
        line = "GITHUB_TOKEN=ghp_unredacted_should_redact"
        result = redact(line)
        assert "ghp_unredacted_should_redact" not in result


class TestRedactNoiseTolerance:
    def test_empty_string(self):
        assert redact("") == ""

    def test_no_secrets_pass_through_unchanged(self):
        line = "INFO    2026-05-01 12:00 session.start backend=ollama"
        assert redact(line) == line


# ── build_diagnostics_zip() — end-to-end ─────────────────────────────────


@pytest.fixture
def sample_resonant_dir(tmp_path):
    """Build a realistic-looking ~/.resonant tree:
      logs/
        resonant-startup.log               (with secrets)
        2026-05-01/aaaa.jsonl              (with secrets)
        2026-05-01/bbbb.jsonl              (empty — should be skipped)
      projects/<hash>/intents/<id>/audit.jsonl (with secrets)
      settings.json                        (with secrets)
    """
    rd = tmp_path / ".resonant"
    rd.mkdir()
    (rd / "settings.json").write_text(
        '{"general": {"api_key": "sk-leakthis123abc"}, "theme": "dark"}',
        encoding="utf-8",
    )

    logs = rd / "logs"
    (logs / "2026-05-01").mkdir(parents=True)
    (logs / "resonant-startup.log").write_text(
        "INFO startup\nANTHROPIC_API_KEY=sk-ant-api03-startupleak\n",
        encoding="utf-8",
    )
    (logs / "2026-05-01" / "aaaa.jsonl").write_text(
        '{"event": "tool_call", "args": {"api_key": "sk-mysecret999xyz"}}\n'
        '{"event": "session.start"}\n',
        encoding="utf-8",
    )
    # Empty placeholder log — must be skipped, not bundled.
    (logs / "2026-05-01" / "bbbb.jsonl").write_text("", encoding="utf-8")

    intents = rd / "projects" / "abc123" / "intents" / "intent01"
    intents.mkdir(parents=True)
    (intents / "audit.jsonl").write_text(
        '{"kind": "tool_call", "args": {"command": "curl -H Authorization: Bearer xyzleaktoken123 https://api"}}\n',
        encoding="utf-8",
    )
    return rd


class TestBuildDiagnosticsZip:
    def test_zip_is_created_with_expected_name(self, sample_resonant_dir, tmp_path):
        out = tmp_path / "out"
        zip_path = build_diagnostics_zip(sample_resonant_dir, out, version="0.3.4")
        assert zip_path.exists()
        assert zip_path.name.startswith("resonant-diagnostics-")
        assert zip_path.suffix == ".zip"

    def test_meta_includes_version_and_platform(self, sample_resonant_dir, tmp_path):
        zip_path = build_diagnostics_zip(sample_resonant_dir, tmp_path / "out", version="0.3.4")
        with zipfile.ZipFile(zip_path) as zf:
            meta = zf.read("meta.txt").decode("utf-8")
        assert "version: 0.3.4" in meta
        assert "platform:" in meta
        assert "python:" in meta

    def test_settings_in_meta_is_redacted(self, sample_resonant_dir, tmp_path):
        # Settings.json contains a planted secret — meta.txt embeds the
        # contents and must redact before writing.
        zip_path = build_diagnostics_zip(sample_resonant_dir, tmp_path / "out", version="0.3.4")
        with zipfile.ZipFile(zip_path) as zf:
            meta = zf.read("meta.txt").decode("utf-8")
        assert "sk-leakthis123abc" not in meta
        assert '"theme": "dark"' in meta  # non-secret content preserved

    def test_startup_log_redacted(self, sample_resonant_dir, tmp_path):
        zip_path = build_diagnostics_zip(sample_resonant_dir, tmp_path / "out", version="0.3.4")
        with zipfile.ZipFile(zip_path) as zf:
            log = zf.read("logs/resonant-startup.log").decode("utf-8")
        assert "sk-ant-api03-startupleak" not in log
        assert "[REDACTED]" in log

    def test_session_jsonl_redacted(self, sample_resonant_dir, tmp_path):
        zip_path = build_diagnostics_zip(sample_resonant_dir, tmp_path / "out", version="0.3.4")
        with zipfile.ZipFile(zip_path) as zf:
            data = zf.read("logs/2026-05-01/aaaa.jsonl").decode("utf-8")
        assert "sk-mysecret999xyz" not in data
        assert "session.start" in data  # non-secret event preserved

    def test_intent_audit_redacted(self, sample_resonant_dir, tmp_path):
        zip_path = build_diagnostics_zip(sample_resonant_dir, tmp_path / "out", version="0.3.4")
        with zipfile.ZipFile(zip_path) as zf:
            data = zf.read("intents/abc123/intent01/audit.jsonl").decode("utf-8")
        assert "xyzleaktoken123" not in data
        assert "[REDACTED]" in data

    def test_empty_session_log_is_skipped(self, sample_resonant_dir, tmp_path):
        # bbbb.jsonl in the fixture is 0-bytes — useless for triage,
        # don't bundle it.
        zip_path = build_diagnostics_zip(sample_resonant_dir, tmp_path / "out", version="0.3.4")
        with zipfile.ZipFile(zip_path) as zf:
            names = set(zf.namelist())
        assert "logs/2026-05-01/bbbb.jsonl" not in names

    def test_no_secrets_anywhere_in_bundle(self, sample_resonant_dir, tmp_path):
        # Belt-and-suspenders: open every file in the zip and assert none
        # contain any planted secret. Catches future redaction regressions
        # even if specific patterns get accidentally weakened.
        zip_path = build_diagnostics_zip(sample_resonant_dir, tmp_path / "out", version="0.3.4")
        planted = (
            "sk-leakthis123abc",
            "sk-ant-api03-startupleak",
            "sk-mysecret999xyz",
            "xyzleaktoken123",
        )
        leaked = []
        with zipfile.ZipFile(zip_path) as zf:
            for name in zf.namelist():
                blob = zf.read(name).decode("utf-8", errors="replace")
                for secret in planted:
                    if secret in blob:
                        leaked.append((name, secret))
        assert leaked == [], f"secrets leaked in bundle: {leaked}"

    def test_creates_output_dir_if_missing(self, sample_resonant_dir, tmp_path):
        # `~/Downloads` always exists in practice but the helper should
        # be robust to a missing target dir.
        out = tmp_path / "deep" / "nested" / "out"
        zip_path = build_diagnostics_zip(sample_resonant_dir, out, version="0.3.4")
        assert zip_path.exists()
        assert out.is_dir()

    def test_handles_completely_empty_resonant_dir(self, tmp_path):
        # Fresh install — no logs yet. Bundle should still produce a
        # valid (essentially empty) zip with just meta.txt.
        empty_rd = tmp_path / ".resonant"
        empty_rd.mkdir()
        zip_path = build_diagnostics_zip(empty_rd, tmp_path / "out", version="0.3.4")
        assert zip_path.exists()
        with zipfile.ZipFile(zip_path) as zf:
            assert "meta.txt" in zf.namelist()


class TestDefaultOutputDir:
    def test_returns_path_object(self):
        # Just sanity — the resolution logic depends on the user's home,
        # so we don't assert a specific path. We assert the call returns
        # something usable.
        result = default_output_dir()
        assert isinstance(result, Path)
        assert result.is_dir()


# ── v0.5.9a5 enrichments ───────────────────────────────────────────────


@pytest.fixture
def enriched_resonant_dir(tmp_path):
    """Fixture with v0.5.9a5 additions: costs.json + per-iteration
    metadata under intents/<id>/iterations/."""
    rd = tmp_path / ".resonant"
    rd.mkdir()
    (rd / "settings.json").write_text(
        '{"general": {}, "theme": "dark"}', encoding="utf-8",
    )
    # costs.json — just dates + numbers, no secrets.
    (rd / "costs.json").write_text(
        '{"daily": {"2026-05-01": {"input_tokens": 12000, '
        '"output_tokens": 3000, "cost_usd": 0.45}}}',
        encoding="utf-8",
    )
    logs = rd / "logs"
    (logs / "2026-05-01").mkdir(parents=True)
    (logs / "resonant-startup.log").write_text(
        "INFO startup ok\n", encoding="utf-8",
    )

    # Intent with iter metadata files.
    intent_dir = rd / "projects" / "p1" / "intents" / "intent01"
    intent_dir.mkdir(parents=True)
    (intent_dir / "audit.jsonl").write_text(
        '{"kind": "tool_call"}\n', encoding="utf-8",
    )
    iters = intent_dir / "iterations"
    iters.mkdir()
    (iters / "iter-001.json").write_text(
        '{"iter": 1, "model": "deepseek-v4-flash:cloud", '
        '"verdict": "continue", "duration_seconds": 42}',
        encoding="utf-8",
    )
    (iters / "iter-002.json").write_text(
        '{"iter": 2, "model": "deepseek-v4-pro:cloud", '
        '"verdict": "satisfied", "duration_seconds": 128}',
        encoding="utf-8",
    )
    return rd


class TestDiagnosticsEnrichments:
    def test_costs_json_included(self, enriched_resonant_dir, tmp_path):
        zip_path = build_diagnostics_zip(
            enriched_resonant_dir, tmp_path / "out", version="0.5.9",
        )
        with zipfile.ZipFile(zip_path) as zf:
            names = set(zf.namelist())
            assert "costs.json" in names
            content = zf.read("costs.json").decode("utf-8")
        # Token counts + dates round-trip unchanged.
        assert "12000" in content
        assert "2026-05-01" in content

    def test_iter_metadata_included(self, enriched_resonant_dir, tmp_path):
        zip_path = build_diagnostics_zip(
            enriched_resonant_dir, tmp_path / "out", version="0.5.9",
        )
        with zipfile.ZipFile(zip_path) as zf:
            names = set(zf.namelist())
        # Both iter-001 and iter-002 should be in the bundle under
        # the right path layout.
        assert "intents/p1/intent01/iterations/iter-001.json" in names
        assert "intents/p1/intent01/iterations/iter-002.json" in names

    def test_iter_metadata_content_preserved(
        self, enriched_resonant_dir, tmp_path,
    ):
        zip_path = build_diagnostics_zip(
            enriched_resonant_dir, tmp_path / "out", version="0.5.9",
        )
        with zipfile.ZipFile(zip_path) as zf:
            iter1 = zf.read(
                "intents/p1/intent01/iterations/iter-001.json",
            ).decode("utf-8")
        # Per-iter model attribution survives the bundle.
        assert "deepseek-v4-flash:cloud" in iter1
        assert "continue" in iter1

    def test_mission_summary_json_present(
        self, enriched_resonant_dir, tmp_path,
    ):
        zip_path = build_diagnostics_zip(
            enriched_resonant_dir, tmp_path / "out", version="0.5.9",
        )
        with zipfile.ZipFile(zip_path) as zf:
            names = set(zf.namelist())
            assert "mission-summary.json" in names
            summary = json.loads(
                zf.read("mission-summary.json").decode("utf-8"),
            )
        assert summary["schema_version"] == 1
        assert "captured_at_iso" in summary
        # The single intent in the fixture should be summarized.
        assert len(summary["intents"]) == 1
        intent_entry = summary["intents"][0]
        assert intent_entry["project_hash"] == "p1"
        assert intent_entry["intent_id"] == "intent01"
        assert intent_entry["iter_files_included"] == 2

    def test_no_iter_dir_no_crash(self, sample_resonant_dir, tmp_path):
        # The pre-v0.5.9a5 fixture has NO iterations/ dir under the
        # intent. The new code must handle that gracefully — no crash,
        # zero iter files included, but the rest of the bundle works.
        zip_path = build_diagnostics_zip(
            sample_resonant_dir, tmp_path / "out", version="0.5.9",
        )
        assert zip_path.exists()
        with zipfile.ZipFile(zip_path) as zf:
            names = set(zf.namelist())
            # Audit still present.
            assert any("audit.jsonl" in n for n in names)
            # Mission summary present even with zero iter files.
            assert "mission-summary.json" in names
            summary = json.loads(
                zf.read("mission-summary.json").decode("utf-8"),
            )
            # Single intent, zero iter files.
            assert summary["intents"][0]["iter_files_included"] == 0

    def test_no_costs_json_no_crash(self, sample_resonant_dir, tmp_path):
        # Fixture without costs.json — diagnostics shouldn't crash;
        # the file just won't be in the bundle.
        zip_path = build_diagnostics_zip(
            sample_resonant_dir, tmp_path / "out", version="0.5.9",
        )
        with zipfile.ZipFile(zip_path) as zf:
            names = set(zf.namelist())
        assert "costs.json" not in names

    def test_iter_metadata_capped(self, tmp_path):
        # Pile of iter files; only LATEST_N_ITERS_PER_INTENT should
        # be bundled.
        from resonant_client.gui.diagnostics import LATEST_N_ITERS_PER_INTENT
        rd = tmp_path / ".resonant"
        intent_dir = rd / "projects" / "p1" / "intents" / "i1"
        intent_dir.mkdir(parents=True)
        (intent_dir / "audit.jsonl").write_text(
            '{"kind": "x"}\n', encoding="utf-8",
        )
        iters = intent_dir / "iterations"
        iters.mkdir()
        # Write 50 iters; only the most-recent N are kept.
        for i in range(50):
            (iters / f"iter-{i:03d}.json").write_text(
                f'{{"iter": {i}}}', encoding="utf-8",
            )
        zip_path = build_diagnostics_zip(
            rd, tmp_path / "out", version="0.5.9",
        )
        with zipfile.ZipFile(zip_path) as zf:
            iter_names = [
                n for n in zf.namelist()
                if "iterations/iter-" in n
            ]
        assert len(iter_names) == LATEST_N_ITERS_PER_INTENT

    def test_empty_iter_files_skipped(self, tmp_path):
        # 0-byte iter file shouldn't pollute the bundle.
        rd = tmp_path / ".resonant"
        intent_dir = rd / "projects" / "p1" / "intents" / "i1"
        intent_dir.mkdir(parents=True)
        (intent_dir / "audit.jsonl").write_text(
            '{"k": "v"}\n', encoding="utf-8",
        )
        iters = intent_dir / "iterations"
        iters.mkdir()
        (iters / "good.json").write_text(
            '{"iter": 1}', encoding="utf-8",
        )
        (iters / "empty.json").write_text("", encoding="utf-8")
        zip_path = build_diagnostics_zip(
            rd, tmp_path / "out", version="0.5.9",
        )
        with zipfile.ZipFile(zip_path) as zf:
            names = set(zf.namelist())
        assert any("good.json" in n for n in names)
        assert not any("empty.json" in n for n in names)
