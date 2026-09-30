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

## September 30 The release's tests run in two jobs, like the pull request checks (source only, not released)

The first run of `v0.20.0-alpha.1` published nothing: its `test` job ran the
whole suite in one Windows job and was cancelled at its 30-minute limit, so
`release` was skipped (the Windows and macOS builds had passed). `release.yml`
now runs the suite as `tests.yml` and `team-tests.yml` do: `test` runs
everything but `tests/test_swarm_*.py` (30 minutes), and a new `team-test` job
runs the Team suite serially with the pinned ripgrep (60 minutes). `release`
waits for both. `tests/test_release_supply_chain.py` checks the split.

## September 30 The Pages publishing rehearsal works now that macOS feeds are live (source only, not released)

`publish-dry-run` (build-macos.yml) rehearses two releases on a copy of the
live gh-pages branch, signing with a throwaway key. Since 0.20.0-alpha.1 put
the first macOS feeds on the branch, signed with the real key, every run
failed: their signatures and their disk images' can't be verified with the
throwaway key. `scripts/rehearse_pages_publish.py` now starts from the copy
without the live macOS feeds, as the branch was before that release; the
rehearsal's releases then make them again with its own key. Real releases add
to the live feeds, and each one's `push_pages.py --check` covers that.
Checked locally against the live branch: every step passes.
