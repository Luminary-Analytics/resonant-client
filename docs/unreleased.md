# Unreleased

**September 23, 2026: AI Employee work remains PAUSED by the user.**
The [consolidated product checkpoint](D:/Repos/Lumina_DO/SelfOrganizingNN/product/AI_EMPLOYEES_CHECKPOINT_2026_09_23.md)
records subsequent paid research, negative/control results and remaining work.
No added SONN learning value or qualified employee/router release is established.
The heartbeat remains paused. Documentation maintenance does not resume work,
spending or grants, and changes no native implementation or installed bundle.
The dated September 15/18 records below are historical.

Changes on `main` that aren't in a release yet go here, newest first, each
in its own dated section. None yet: everything up to
[Lumi 0.20.0-alpha.1](v0.20.0-alpha.1-release-notes.md) moved to its
[change record](v0.20.0-alpha.1-change-record.md).

## September 30 Fixes from testing 0.20.0-alpha.1: turn card, Codex replies, profile corner (source only, not released)

Found by the owner on an installed 0.20.0-alpha.1 (upgraded from SONN Client,
GPT-6 Astra through the Codex CLI), with the prompt "Explain what this project
does, then suggest one small improvement and make it."

- **"Changed — verify" says which case applies.** The outcome rule is
  unchanged: changes count as verified only when every check Lumi itself
  observed passed, and the stable id stays `changed_unverified`. The card now
  says whether Lumi saw no check ("Changed — not checked"), a named check
  failed ("Changed — check failed", naming its command) or one passed before
  the last change ("Changed — check out of date"). With a CLI backend it adds
  that checks the model ran inside its own tools aren't visible to Lumi (for
  Codex: unless each runs as its own command). The session's evidence carries
  `cli_backend` and `unverified_reason` (`not_checked`, `check_failed`,
  `check_stale`) for headless runs and evaluations
  (`engine/turn_outcomes.py`).
- **A changed turn offers Verify changes, not Retry.** Retry and Retry
  another model stay for `incomplete` and `failed` (never for a DLP or policy
  refusal). Verify changes asks for checks Lumi can record: standalone
  commands for Codex, `check_run` for native models. Claude Code reports no
  tool activity to Lumi, so its card offers only Review and says so. The
  buttons form one row that wraps below the text at compact widths.
- **Codex and Claude Code replies aren't repeated.** A CLI turn is one step
  holding several agent messages; the final `text.done` carried all of them, so
  the reply showed the explanation and the improvement twice. Each message
  before the last now ends as its own `interim` `text.done` as it happens
  (dimmed while the turn runs, folded away after), and only the last is the
  reply, live and replayed. Deltas still stream as they arrive, and the model's
  history keeps the whole step. Claude Code's adapter no longer lets partial
  text hide a failed `result` or a non-zero exit.
- **The profile corner no longer says "SONN not connected"** for someone whose
  SONN key or URL is only a leftover. SONN counts as set up when Lumi found it
  or both its key and project URL are set; the corner shows the local display
  name and "Settings and connections" unless SONN's account answers, and the
  profile menu says SONN's state.

## September 30 The release's tests run in two jobs, like the pull request checks (source only, not released)

The first run of `v0.20.0-alpha.1` published nothing: its `test` job ran the
whole suite in one Windows job and was cancelled at its 30-minute limit, so
`release` was skipped (the Windows and macOS builds had passed). `release.yml`
now runs the suite as `tests.yml` and `team-tests.yml` do: `test` runs
everything but `tests/test_swarm_*.py` (30 minutes), and a new `team-test` job
runs the Team suite serially with the pinned ripgrep (60 minutes). `release`
waits for both. `tests/test_release_supply_chain.py` checks the split.
