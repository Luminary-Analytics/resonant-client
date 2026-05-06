---
name: Autonomous Mission — Iter Discipline
description: Conventions every autonomous mission iter follows. Codified from v0.5.6 → v0.5.9 field findings.
version: 1.0.0
pinned: true
triggers:
  - run autonomous mission
  - work through roadmap
  - loop on phase-1 sub-missions
  - iter convention
---

# Autonomous Mission — Iter Discipline

When the autonomous mission daemon dispatches a Phase-1 sub-mission to work on a roadmap item, the sub-mission follows these conventions to keep the outer loop healthy. Most of these were learned from real field observations and codified into the codebase via specific alphas; the references at the end point to where each rule was added.

## Per-iter must-haves

1. **Pick exactly ONE roadmap item per iter.** Multi-item iters muddy the audit log and break atomic resume.
2. **Write a `commit_sha` to the audit log when committing work.** The daemon's `validate_sha` hook depends on this for resume / orphan detection.
3. **Don't bypass the sandbox.** If a tool result hits a sandbox denial, surface it — never re-run with a different flag to evade it.
4. **Annotate criterion failures with diagnoses, not just status.** When a `[bash]` or `[chrome]` check fails, REFLECT must update the criterion line in `roadmap.md` with the actual failure mode (e.g. `*(FAIL: actual recipes live at src-tauri/recipes/, not root)*`). Pure pass/fail loses information.

## Per-iter must-NOT-haves

1. **Don't repeat-tool with identical args 3+ times in a row.** The cycle guard (v0.4.11) will fire. Pivot, ask for help via `await_user`, or summarize what you found and stop.
2. **Don't rely on side-effects from the previous iter's IMPLEMENT specialist.** Each iter starts fresh; explicitly check the file/state you need.
3. **Don't claim `verdict=satisfied` if there are unpassed `[bash]` or `[chrome]` criteria.** The daemon's cross-check (v0.5.9a3) will catch you and downgrade with structured `verdict_overridden=True` provenance, which is worse than honestly reporting `continue`.

## When you can't move forward

- **Path-mismatch on a `[bash]` criterion**: emit a structured `decision_request` (v0.5.8a2) with options "move file to criterion-path / update criterion to actual path / both / neither". The daemon parks; the user picks; you re-run REFLECT with their choice in context.
- **External dependency missing on the host**: don't fake it. `await_user` for guidance on whether to skip the criterion, install the dep, or change platform.
- **Truly stuck**: emit `verdict=blocked` with a precise reason. The daemon's atomic terminal-state transition (v0.5.6a3) ensures the GUI badge / session record / roadmap status all converge.

## Cost discipline

- Each iter has a `pause-after-current-iter` flag (v0.5.9a4); respect it on the next top-of-loop check.
- Per-iter cost is tracked (v0.5.9a2) and visible in the GUI; if a single iter cost exceeds 2× the running average, REFLECT should comment on whether it was justified.

## Related

- `resonant_client/gui/autonomous_loop.py` — the daemon's outer loop.
- `resonant_client/orchestration/specialists.py` — REFLECT's system prompt with the failure-annotation rule.
- `docs/field-observations/2026-05-03-resonant-linux-bridge.md` — the post-mortem these rules were codified from.
- `docs/v0.5.6-release-notes.md` through `docs/v0.5.9-release-notes.md` — the alphas that shipped each rule.
