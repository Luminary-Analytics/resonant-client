---
name: Rigorous Grill — Spec Refinement
description: 5-beat question pattern that consistently produces dispatchable specs. Codified from the linux-bridge field run where all 27 questions rated 5/5.
version: 1.0.0
pinned: true
triggers:
  - refine spec
  - grill the user
  - clarify before building
  - rigorous-mode interview
  - scope a vision into a sprint
---

# Rigorous Grill — Spec Refinement

When the user describes a vision-scope or ambiguous goal, the grill specialist runs a structured interview to produce a typed, dispatchable `## Final spec`. The 5-beat pattern below was codified from the 2026-05-03 linux-bridge field run (`docs/field-observations/2026-05-03-resonant-linux-bridge.md`) where 27 questions all rated 5/5 — the highest signal we've ever gotten on grill quality.

## The 5 beats per question

1. **Acknowledge** — one line referencing the user's last answer. ("Got it. One runtime, managed under `~/.resonant/runtimes/`.")
2. **Bridge** — one sentence motivating the next question. Connect it to the user's stated goal so they don't feel interrogated. ("Now let's talk about install/launch plumbing — because 'one-click install' is doing a lot of work.")
3. **Frame as options (a/b/c)** — give 2-4 named alternatives. Avoids open-ended answers that drift.
4. **Recommend ONE with explicit rationale** — don't hedge. The user can override; if you don't recommend, you've punted the decision back to them.
5. **Invite override** — single phrase ("...so I want your call.") that signals the user has the final say.

## Specific quality signals to watch for

- **Active scope-narrowing**: when the user's answer creates a synthesis-confirmation moment, ASK that as the next question instead of moving to a new axis. (Q5b in the linux-bridge run was "drop games entirely from v0.1?" — a synthesis of the previous game-vs-app decisions, not a new topic.)
- **Push back on subjective criteria**: if the user says "feel natural" or "simple and reliable," don't accept it. Ask for the concrete behavior that proves it.
- **Recognize vision-vs-feature mismatch early**: Q1 should pin down the v0.1 slice. If the user is describing a vision, recommend a launcher / detector / docs / single-feature slice as the first concrete artifact.

## Anti-patterns

- Asking 3+ questions about the same axis in a row — spreads boredom.
- Hedged recommendations ("maybe option B, or A could work too") — just commit.
- Fillers like "Great question!" — wastes a turn the user paid for.
- Open-ended "what do you think?" without options.
- Re-confirming things the user already answered (signals you weren't listening).

## Output gate

Before emitting `## Final spec`, the grill must produce typed acceptance criteria — at least 3 `[bash]` checks for code-changing missions. The spec-validity gate (v0.5.6a2) refuses to dispatch if the spec is missing this section, so producing it is non-optional.

## Related

- See `resonant_client/orchestration/grill_me.py` for the system prompt that drives this pattern.
- The full 5-beat exemplar lives in the rigorous-mode addendum block.
- `tests/test_rigorous_grill.py` pins the specific phrases the prompt uses.
