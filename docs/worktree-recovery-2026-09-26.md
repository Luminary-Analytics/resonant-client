# Worktree recovery, September 26, 2026

This records source integration and validation, not a release or packaged-app
qualification. AI Employee work and its heartbeat remain paused. No provider
credentials, user settings or installed bundle were changed.

## Scope and preservation

The recovery started from `origin/main` at
`51d1a8b543e83b3a599901748b18c1f81c8b6454` in the separate
`codex/finish-open-work` worktree. The original checkout was 294 commits behind
that baseline and had a local launch-configuration edit.

All 29 pre-existing worktrees were inventoried. The 10 dirty worktrees were
backed up with their staged, unstaged and complete binary patches, exact
modified-file contents and untracked files. The local `../recovery` directory
beside the integration checkout holds `inventory.json`, those backups and the
four PR specifications. `../verify_recovery.py` compares the original HEADs,
status, patches and saved file contents with the backups. Its final comparison
found no changes to any original worktree. No original worktree was reset,
stashed, cleaned, removed or taken out of an interrupted merge.

## Integrated work

| Origin | Recovered behavior |
| --- | --- |
| [PR 69](https://github.com/Luminary-Analytics/resonant-client/pull/69), stacked [PR 73](https://github.com/Luminary-Analytics/resonant-client/pull/73) | `/plan` dispatch, Pause/Resume, Stop and cancellation without starting another specialist or repair request |
| `competent-curran-006fb9` | Running plans reconnect after page reload and retain usable controls |
| `nostalgic-wright-ae7a7a` | Autonomous completion listeners remain attached when a page sends plan commands |
| `angry-goldberg-ec9c64` | Reflection activity appears under its own plan card |
| `busy-leavitt-b6b2f0` / `specialist-settings-hooks` | Settings and approved capability-pack hooks reach plan, Mission and reflection specialists |
| [PR 85](https://github.com/Luminary-Analytics/resonant-client/pull/85), `wonderful-elion-35af1d` | Terminal sessions retain project trust, policy, hooks and literal rendering, including refusal text |
| [PR 86](https://github.com/Luminary-Analytics/resonant-client/pull/86) | Dictation continues through pauses, supports keyboard controls and optional transcription services |
| `eloquent-shannon-33347b` | Page content security policy, external appearance bootstrap and native bridge compatibility |
| `practical-boyd-1b1e6c` | Obsolete Agents-pane rendering removed; live worker controls retained |
| `recursing-thompson-c04926` | Preview tabs support keyboard navigation |

The specialist-policy WIP in `recursing-pare-aa5c3e` is superseded by main's
merged policy changes, including PR 83. Its execution-policy tests are already
on main. The alternate `specialist-settings-hooks-on-main` commit `e61a28b`
contains the same hook wiring against an older baseline; the integrated hook
branch additionally reconciles policy behavior and tests. Temporary detached
checkouts duplicate already included histories. Other clean development
worktrees are ancestors of this integration. `gh-pages` remains a separate
deployment history. Local launch configuration stays with the original checkout.

## Review and corrections

Standards and originating PR specifications were reviewed separately. The
reviews found five distinct integration issues (draft isolation appeared in
both reviews). All five were corrected and covered by behavioral regressions.

| Axis | Finding and correction |
| --- | --- |
| Standards and Spec | Late browser recognition or service transcription could modify another conversation's draft. Draft changes cancel dictation before changing ownership; sending discards pending results without restoring sent text or moving focus. |
| Standards | Invalid or expired organization policy still allowed browser recognition. Both dictation paths now refuse, including automatic fallback. |
| Standards | An adopted plan wrote control audit events and read/restored snapshots under the newly selected project. Those operations now use the plan's originating project; restored in-memory and saved graphs agree. |
| Spec | Another plan's events could replace the graph while Stop targeted the followed plan. Graph events are scoped to that plan; a snapshot arriving before acceptance is retained and drawn when followed. |
| Spec | Reconnecting after a plan ended offline could leave Running or Stopping controls active. An authoritative empty running-plan list settles them to Ended without asserting successful completion. |

The Standards reviewer completed its follow-up with no unresolved findings.
The Spec reviewer's follow-up encountered a Codex authentication error; the
primary reviewer checked those corrections and their regressions directly.
No authentication configuration was changed in response.

Three groups of old test fixtures also needed the new settings/hook interface:
terminal tests, socket command tests and the autonomous listener regression.
Their behavioral assertions were retained. The earlier failed and interrupted
test logs remain beside the checkout and are not counted as passes.

The Voice settings and guide now explicitly distinguish optional, separately
billed OpenAI transcription from coding through ChatGPT/Codex sign-in. No API
key is required by this recovery or was added to the app.

## Validation

- Full Python suite: `python -m pytest -q -n 2 -o faulthandler_timeout=60`
  passed with 4,626 passed and 5 skipped in 230.44 seconds. The run used an
  isolated home and disabled keychain; its log is `../pytest-complete.log`.
- Five Node test files: 128 passed.
- `python -m ruff check .`, JavaScript syntax checks for `app.js` and
  `settings_view.js`, and `git diff --check` passed.
- Real browser events against an isolated local fixture exercised `/plan`,
  Pause, reload while paused, Resume and Stop. Keyboard navigation reached
  preview tabs and plan controls. Stop retained focus, abandoned unstarted
  steps and prevented the verifier from starting.
- At 375 by 812 pixels the controls fit, with document width equal to viewport
  width. The disabled Dictate control was keyboard reachable and explained
  that dictation was off. No warning/error console entries were observed
  during those checks with the content security policy active.
- The later billing-help wording was source- and syntax-checked. Its second
  browser check could not complete after a fixture restart because the
  browser tool rejected the connection-error page's URL.
- Backend and browser model responses were scripted. Audio recording and
  transcription were simulated in tests. No live microphone, paid provider
  request, native packaged window or release build was used for qualification.

At the end of the initial recovery, the source branch was local and the four
existing PRs and their remote branches were unchanged. The user subsequently
authorized publication, merging into main and worktree cleanup. The integration
PR records those GitHub operations; the local cleanup archive retains the
original inventory, patches, file backups and a Git bundle before worktrees
are retired. Building or deploying a release remains a separate operation.

The first publication check found that the macOS policy-profile generator
imported the transcription HTTP client even for policies without voice
settings. The client now loads only during transcription. Three regressions
run the actual profile generator with Python's site packages disabled, checking
ordinary policies, valid voice settings and rejection of invalid voice settings.
