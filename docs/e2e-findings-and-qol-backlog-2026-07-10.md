# End-to-end findings and QOL backlog — 2026-07-10

## Release-candidate validation

| Surface | Result |
| --- | --- |
| Python test suite | 2,685 passed, 2 skipped with 24 xdist workers |
| Static analysis | Ruff clean across `resonant_client` |
| GLM live autonomous smoke | `minimal` converged in 1 iteration / 32.1s |
| DeepSeek Pro live autonomous smoke | `minimal` converged in 1 iteration / 42.1s |
| Source GUI | HTTP 200, WebSocket 101, Settings and command palette exercised, no browser console warnings/errors |
| Wheel | Built, installed into a fresh virtual environment, version and model-profile import verified |
| Frozen GUI | PyInstaller build succeeded; HTTP 200, WebSocket 101, clean stderr, model runtime populated |
| Frozen bundle | Clean-room build: 49.3 MiB / 227 files, with SHA-256 manifest and 150 MiB / 400-file regression gates |
| Installer wrapper | Not run locally because Inno Setup is not installed; release workflow YAML validates |

## Bugs and cleanup completed

1. **Smoke CLI crashed on Windows CP-1252 consoles.** Unicode status glyphs
   raised `UnicodeEncodeError` before JSON and Markdown results could be saved.
   Console output now uses portable ASCII status labels.
2. **Wrapped refined intents were truncated after roadmap reload.** Roadmap
   items use a one-line grammar, but grill-spec descriptions retained embedded
   newlines. The planner received only “containing exactly the” and substituted
   an unrelated learned precedent. Item title, description, and completion-note
   fields are now normalized at the canonical mutation/writer boundary.
3. **Sub-agent role prompts were defined but not wired.** Child sessions now
   receive their build/explore/plan role contract and structured handoff schema.
4. **Generated package metadata was tracked despite being ignored.** The stale
   `resonant_client.egg-info` files still advertised version 0.2.0, proprietary
   licensing, and removed provider extras. They are removed from source control.
5. **Frozen builds still force-imported removed provider SDKs.** Anthropic and
   OpenAI hidden imports and CI extras were removed, shrinking the bundle and
   dependency graph substantially.
6. **Frozen `--version` CI check could not capture output.** The GUI-subsystem
   executable needs explicit stdout/stderr redirection via `Start-Process`.
   Both build and release workflows now use that path.
7. **Decision-park integration tests were scheduling-sensitive.** Their event
   wait allowed only two seconds under a 24-worker suite. The synchronization
   ceiling is now five seconds without adding runtime delays.
8. **Static-analysis debt.** Sixty-nine safe unused imports, assignments, and
   trivial f-string issues were removed; the package is Ruff-clean.

## Prioritized QOL and feature improvements

All eight scoped improvements below received a first production pass on
2026-07-10. The implementation includes focused regression coverage plus the
full validation matrix above. The final smoke-artifact explorer remains a
follow-on candidate rather than part of this eight-item pass.

| Improvement | Implemented surface |
| --- | --- |
| Deterministic repair items | Failed blocking criteria synthesize stable, deduplicated Tier-1 work with captured evidence. |
| Prompt inspector | Settings shows active family, model, role, exact named layers, token estimates, and prompt hash. |
| Evaluation dashboard | Background GLM/DeepSeek variance runs stream progress and persist convergence, timing, and baseline deltas outside the repo. |
| Agent activity | Preview tree tracks task workers and plan specialists; completed workers expose full bounded handoffs. |
| Context cockpit | Live effective window, threshold, composition, prompt layers, sources, largest tool results, todos, and compaction count. |
| Iteration recovery | Every autonomous iteration writes a content-complete Git checkpoint; Settings supports compare and recovery-branch-preserving restore. |
| Chat evidence groups | Reads, searches, and validation commands collapse into inspectable Evidence groups; errors remain expanded. |
| Packaging gates | Temporary-venv clean builds, deterministic file manifest, required assets, forbidden SDKs, size and file-count CI gates. |

### P0 — Deterministic acceptance-failure repair items

**Why:** The failed GLM smoke showed the mission can finish every roadmap item,
discover a failed deterministic criterion, and stop as `stuck` if REFLECT does
not add a repair item. The runtime already has exact failure evidence and should
not depend entirely on the model to convert it into work.

**Shape:** When all items are checked but blocking criteria fail, synthesize one
deduplicated repair item containing the criterion, command, captured evidence,
and last relevant diff. Let REFLECT refine or merge it, but guarantee pending
work exists before applying the stuck rule.

**Acceptance:** Seed `hello.txt` with the wrong content; the next iteration must
receive a repair item and converge without a human decision.

### P0 — Model-profile and prompt inspector

**Why:** Prompt behavior is now family-aware but invisible. Users cannot tell
which profile was selected, how large it is, or whether a specialist received
its scoped contract.

**Shape:** Add a read-only “Prompt” panel showing detected family, contract
version/hash, role, project-instruction source, approximate tokens, and collapsed
layer previews. Add an advanced per-model override file with reset-to-default.

**Acceptance:** Switching GLM → DeepSeek updates only the family layer; the
invariant contract hash stays stable and no secret/runtime context is displayed.

### P1 — Built-in model evaluation dashboard

**Why:** The smoke CLI has the right metrics but prompt tuning still requires
manual commands and JSON comparison.

**Shape:** A Settings → Evaluations view that runs selected fixed specs against
GLM/DeepSeek, streams phase progress, and charts convergence, wall time, tool
steps, malformed calls, edit repairs, compactions, and duplicated work against
the saved baseline.

**Acceptance:** One click runs `minimal` across both families and produces a
side-by-side, exportable result without writing artifacts into the active repo.

### P1 — Agent activity tree and handoff viewer

**Why:** Sub-agent and specialist work currently appears as interleaved chat
events. Long missions need a durable view of ownership and evidence.

**Shape:** A collapsible tree for parent → worker/specialist with assignment,
state, elapsed time, tool count, changed files, verification, and final handoff.
Allow retrying a failed bounded worker with its prior evidence attached.

**Acceptance:** A three-specialist mission visibly preserves dependency order,
never attributes a child write to the parent, and exposes every handoff after a
session reload.

### P1 — Context cockpit

**Why:** The composer shows an approximate token count, but users cannot see the
effective model context, reserved output, pinned context, tool-result pressure,
or what compaction removed.

**Shape:** Show effective `num_ctx`, used/reserved tokens, cache-stable prefix,
largest context contributors, compaction timeline, and current goal/checklist
ledger. Offer “compact now” and “pin/unpin” for user-selected artifacts.

**Acceptance:** A forced small-context test visibly compacts before overflow,
retains the goal and decisions, and explains which tool outputs were pruned.

### P1 — Iteration checkpoints and rollback

**Why:** Autonomous iterations currently fix forward. A bad worker can leave a
dirty workspace that contaminates every later attempt.

**Shape:** Create a lightweight git checkpoint before each autonomous iteration,
record its SHA in the roadmap log, and offer compare, restore, or branch-from-
checkpoint. Never roll back automatically when user changes overlap.

**Acceptance:** A deliberately broken iteration can be restored to its exact
pre-iteration tree while preserving the failed attempt on a recovery branch.

### P2 — Codex-style chat evidence grouping with Resonant identity

**Why:** The current visual shell is strong, but long sessions still make tool
evidence compete with the narrative.

**Shape:** Group consecutive reads/searches into collapsible activity cards;
keep edits, test results, decisions, and failures expanded. Add a sticky compact
task ledger, “files changed” summary, phase separators, jump-to-latest, and a
final verification card. Retain the purple/teal Resonant accents and agent-tree
motif instead of visually cloning Codex.

**Acceptance:** A 30-step session can be scanned for objective, current phase,
changed files, failing check, and next action without expanding read-only calls.

### P2 — Clean-room package and size gates

**Why:** Local PyInstaller builds can absorb globally installed packages and hide
missing-dependency problems. The stale SDK imports inflated this build by 28.5 MB
and more than 2,400 files.

**Shape:** Add a clean virtual-environment build script, assert an allowlist of
top-level bundled packages, and fail CI when bundle size/file count grows beyond
an acknowledged threshold. Archive the PyInstaller warning file with CI output.

**Acceptance:** The same commit produces a bundle within a small size tolerance
on a clean runner and cannot silently reintroduce removed provider SDKs.

### P2 — Smoke artifact and failure explorer

**Why:** Live smoke details are split across a JSON record, Markdown report,
temporary project, roadmap, plan snapshots, audit log, and skill artifacts.

**Shape:** Add one run manifest linking every artifact and a GUI explorer that
opens the failed criterion, responsible plan node, exact tool call, diff, and
stop-rule decision in a single timeline. Include a “rerun from checkpoint” action.

**Acceptance:** Selecting a failed smoke explains the first causal divergence
without manually searching `~/.resonant`.
