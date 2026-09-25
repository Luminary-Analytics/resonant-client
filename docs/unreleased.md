# Unreleased

**September 23, 2026: AI Employee work remains PAUSED by the user.**
The [consolidated product checkpoint](D:/Repos/Lumina_DO/SelfOrganizingNN/product/AI_EMPLOYEES_CHECKPOINT_2026_09_23.md)
records subsequent paid research, negative/control results and remaining work.
No added SONN learning value or qualified employee/router release is established.
The heartbeat remains paused. Documentation maintenance does not resume work,
spending or grants, and changes no native implementation or installed bundle.
The dated September 15/18 records below are historical.

## September 25 the terminal UI keeps the rules `lumi run` keeps — source only, not released

**The terminal's session had none of them.** `lumi` with no subcommand
(`lumi/tui.py` `main`) built a bare `Session`: no project, path sandbox, file
exclusions, execution policy or hook runner, and project content counted as
trusted. Driven through the unchanged `main()` with a scripted model, in
Bypass (its default):

- a command the organization's shell rules deny ran and wrote its file, and
  `git push nowhere main` ran with agent changes waiting for review: both
  rules live in the execution policy;
- `.env` (excluded in Settings), `deploy.pem` (excluded by the organization's
  policy) and a file outside the project were read into the conversation;
- the person's own `pre_tool_use` hook in Settings didn't run;
- a policy allowing only Ask and Auto-edit didn't stop Bypass.

Only the guardrails held, at dispatch (`check_floor`). With `--approve` (the
read-only suggest tier, without its policy) the terminal asked about all three
commands, the guardrail one included, and ran the organization-denied command
and the push once they were approved.

- **One way to scope a session** (`headless.scope_session`): the project, path
  sandbox, exclusions, trust, instructions and the tier's execution policy
  (guardrails, review gate and organization shell rules first, the project's
  `lumi-policy.json` layered on). It works everything out before setting
  anything, so a failure leaves the session as it was. `lumi run`'s
  `build_session` uses it; its behavior is unchanged.
- **The terminal builds its session with it** (`tui.build_session`) and never
  trusts a project itself. Settings and policy apply as in `lumi run`
  (`headless._configure`: network, secret scan, audit log, prices, usage,
  budgets, review gate and code hosts, shell sandbox) before it starts;
  before, only the audit log, prices, usage, budgets and GitHub were set up.
  The Ollama address and default model are read through `SettingsManager`,
  so values a policy locks apply.
- **Modes.** Bypass, the default, is the full-auto tier. `--approve` and
  `/approve on` are Ask, which asks before changes and commands as before,
  and also refuses what Auto-edit refuses (recursive deletes, `curl … | sh`).
  `/approve` rebuilds the execution policy with the tier; it used to flip
  only the tier. The organization's `permissions.allowed_modes` applies:
  without Bypass the terminal starts in the first allowed mode it has (Ask
  or Auto-edit) and the banner says so, a mode chosen with `--approve` or
  `/approve` that the policy doesn't allow is refused, and with none of its
  modes allowed it doesn't start.
- **Models.** Only the models the policy allows are offered, at start and in
  `/model`; with none allowed, or an invalid or expired policy
  (`policy.blocked_reason`), the terminal doesn't start. `Session.run` still
  refuses a blocked model on each turn.
- **The approval prompt is always passed** (`run_embedded`), so in Bypass a
  `prompt` rule in the organization's or the project's policy asks, instead
  of being refused as if nobody could answer.
- **Hooks, decided deliberately:** the person's Settings hooks run
  (`HookRunner(settings)`), as in the app. They are the person's own
  configuration, not repository content, and a guard they set up shouldn't
  be skipped because they typed in a terminal. Capability packs, with their
  hooks, skills and MCP servers, still aren't loaded, as in `lumi run`.
  `lumi run`, the chat gateway and tasks from chat still don't run hooks.
- **`/cd`** moves the session with the folder: its sandbox, exclusions, trust
  and execution policy. A folder that can't be opened changes nothing.
- **The app's first run still trusts Recent projects.** Building a
  `WorkspaceTrust` created `trusted_projects.json`, so running `lumi run` (or,
  with this change, the terminal) before the app's first run with trust left
  an empty file, and the app then trusted none of the Recent projects, against
  the upgrade promise in "client security" below. Only a caller that passes
  Recent projects, the app, records that first run now; `lumi run`, the
  terminal and model comparisons only read decisions.
- Computer use follows Settings (a policy can lock it off); it was always on.
  Audit and usage records name the session `tui:<id>`. The banner shows the
  mode, and whether an untrusted project's instructions and allow rules are
  off.
- Guide: [the terminal UI](terminal-ui.md). The README, AGENTS.md,
  ARCHITECTURE.md and the shell sandbox, audit log, usage and organization
  policy guides mention the terminal.

This settles the "Not exercised" note of "the terminal says why a tool call
was refused" below: the terminal's own sessions now have hooks and an
execution policy.

Validation on September 25, 2026:

- `tests/test_tui_session.py` (15 tests) runs the real `main()` in an isolated
  state folder, with Ollama, the model (the streaming stub) and the keyboard
  scripted:
  - in Bypass, the organization's rule, the review gate, both exclusions, the
    path sandbox and a Settings hook each refuse their call with the reason
    the model is told, nothing is asked, and no secret reaches the
    conversation;
  - with `--approve`, the guardrail, the organization's rule and `rm -rf`
    are refused before any prompt, a read runs, a command answered "y" runs
    and an edit answered "n" doesn't;
  - an untrusted project's `prompt` rule asks in Bypass;
  - a policy that locks the shell sandbox on (where it can't run) and
    computer use off reaches the session;
  - the policy's modes (fallback, refused `/approve off`, refused
    `--approve`, none usable), an invalid policy, blocked models at start and
    in `/model`, and no allowed model;
  - repository instructions apply only once the project is trusted, and the
    terminal records no trust decision and leaves no trust file, so the app's
    first run still trusts the project from Recent projects (also
    `tests/test_exclusions_and_trust.py`; both tests failed with the file
    created as before);
  - `/cd` applies the new folder's `.lumiignore` and sandbox, and a missing
    folder changes nothing; `/approve on` and `off` switch the tier's rules;
  - a `scope_session` that fails leaves the session as it was.
- Each piece of the change undone in turn (13 variants: no scoping, no hooks,
  the old prompt rule, no shell sandbox setup, computer use always on, `/cd`
  or `/approve` without re-scoping, mode or model policy ignored, trust
  granted, the suggest tier for `--approve`, assigning while scoping, an
  invalid policy ignored) failed at least one of these tests. The files were
  restored after each.
- A script driving `main()` in a throwaway home showed the gap above before
  the change; after it, all seven calls were refused with nothing asked, in
  both modes, and the policy moved the default to Ask.
- The related suites (chat gateway, exclusions and trust, guardrails,
  `lumi run`, model comparisons, shell sandbox, tasks from chat, review gate,
  scheduled tasks, `test_tui.py`): 208 passed, 1 skipped.
- Merged with main through #68: full `pytest` 4,409 passed, 5 skipped and 1
  failed. In `tests/test_repl.py::TestExecWrappers::test_python_round_trip`
  a fresh Python REPL's first eval (30 s limit) returned an error while about
  a dozen sessions shared this computer; the file passed alone (22 tests),
  and `lumi/engine/repl.py` isn't part of this change. `ruff check .` is
  clean (ruff 0.12.12), `node --check` passes for `app.js` and
  `settings_view.js`, the four Node UI test files pass (70 tests), and
  `git diff --check` is clean. The real `~/.resonant` was unchanged and no
  `~/.lumi` was created.

Not exercised: a real terminal window and a live model. Ollama wasn't
reachable (10.0.0.131 timed out, and nothing listens locally), so the
terminal's Ollama detection, warm-up and model listing ran against stubs.
The docs site wasn't built locally (MkDocs isn't installed here);
`tests/test_docs_links.py` passes.

## September 25 refused tool calls say why — source only, not released

**A refused call's row said only "denied".** When a hook, a policy rule, a
tool boundary, a second approver or an approval nobody could answer stops a
tool call, the model gets the reason as the call's result. The app added a
line reading "✗ denied" (and "not run" on a command's row) and dropped the
reason. A guard hook that timed out looked the same as the user's own Deny.

- **The reason shows under the call's row** (`lumi/gui/static/app.js`,
  `_settleDeniedToolRow`), always set as text, since it can come from a hook,
  a policy, a repository or the model.
  - A command, edit or write row reads "✗ … not run" with the reason above its
    expandable detail. Expanded, a command that never ran shows "(not run)"
    rather than "(no output)".
  - Other rows read "✗ not run", with the reason on the line below.
  - A refusal whose call has no row gets a line of its own. The separate
    "✗ denied" line is gone.
  - The user's own Deny still reads just "denied".
  - Refusals are amber and errors red: a refused call didn't run, so it
    didn't fail.
- **A refused Evidence call no longer reads ✓.** Reads, searches and check
  commands in the collapsed Evidence group showed ✓ when the refusal wasn't
  also marked as an error (a hook, an approval). They show ✗ with the reason
  open. The group's header counts "N not run", and the group stays open
  without the error styling.
- **A worker's refused calls** get the same, in the worker's own rows.
  **Codex and Claude Code** rows are unchanged: those CLIs run their own tools
  and approvals, and Lumi forwards their observations with `denied` false, so
  there is no refusal to show.
- **Fixed along the way:**
  - An inline result went to the last row of its tool, so a tool called twice
    in a step could put its status or reason on the other call's row. A result
    now finds the row with its own call id, among its own lane's rows only, so
    a worker that reuses one of the parent's call ids doesn't reach the
    parent's row.
  - An Evidence call's output, once opened, sat beside its row and squeezed
    the pattern or path to nothing. It takes a line of its own, like a reason.

Validation on September 25, 2026:

- Four tests in `tests/ui_recovery.test.cjs` drive the real `handleEvent`,
  `renderToolCall` and `renderToolResult`, and the Evidence group from
  `run_cards.js`, in a small DOM that parses the rows' markup and keeps every
  `innerHTML` write:
  - command rows refused by a timed-out hook and by a policy, and one the user
    denied;
  - inline rows whose tool name, call id and reason are hostile markup: the
    reason stays text, no markup write carries it, and each result reaches its
    own row;
  - a refusal whose call has no row;
  - a refused Evidence call next to a passing one and a denied one;
  - a worker's refused write and `task` in its lane, while the parent's `task`
    with the same call id succeeds.
- On the previous `app.js` and `run_cards.js`, all four failed: six rows for
  three calls (the extra "✗ denied" lines), ✓ for the refused Evidence call,
  and no reason in the worker's row.
- After rebasing on main (with gate hooks failing closed, second approvals
  and worker transcripts): full `pytest` 4,294 passed, 5 skipped.
  `ruff check .` clean, `node --check` passes for `app.js` and
  `settings_view.js`, the four Node UI test files pass (65 tests),
  `git diff --check` clean.
- In the browser pane, from an isolated home with a scripted Ollama stub, in
  Ask mode, with two `pre_tool_use` hooks in `settings.json` (one exits 1 with
  a message on stderr, one sleeps past `timeout_seconds: 2`) and the built-in
  recursive-delete deny:
  - A `grep` the hook refused read ✗ "not run" with the hook's message open,
    under a `grep` that found "2 matches ✓". The group read "Evidence ·
    Searching codebase · 1 not run".
  - `git push` refused by the hook, `rm -rf build` refused by the policy
    ("Blocked by policy: Recursive delete blocked — use a safer alternative")
    and a `task` refused by the hook each read "not run" with their reason.
  - `npm publish`, denied in the approval dialog with Escape and, in a second
    run, with **Deny**, read just "denied". Focus went back to the message box.
  - The hook that never answered: `make release` read "not run" with "Blocked
    by hook: pre_tool_use hook \`slow-guard\` timed out after 2 s; gate hooks
    block when they give no answer. Raise its timeout_seconds if it needs
    longer."
  - The model's closing message listed the same reasons it had been given.
    After a reload, the replayed session showed the same rows, and so did a
    last run on the branch rebased over worker transcripts.
  - At 375 px there was no horizontal scroll, including for reasons with 120
    to 160 character unbroken tokens, which wrapped inside their rows.
  - On a refused Evidence call, Enter closed the reason and Space opened it
    again. `aria-expanded` followed and focus stayed on the item.
  - The real `~/.resonant` was unchanged, no `~/.lumi` was created, and no Lumi
    credential was stored. `~/.codex` changed during the run while the user's
    own Codex was running; the fixture never started Codex, so those writes
    are unattributed.

Not exercised: a live model, a packaged build, Codex or Claude Code, the
"no approval prompt is available" refusal in the app (a Node test covers its
text) and a second approver's refusal (it takes the same path, with the
approval's message as the reason). The terminal UI shows the reason too; see
the next section.

## September 25 a mistyped organization policy no longer counts as no policy — source only, not released

**A typo in an administrator's policy could switch the whole policy off.** A
section of the wrong type made the first `lumi.policy.load()` raise instead of
reporting an invalid policy. Examples are `"permissions": "ask only"`,
`"models": [...]`, `"mcp": 5` and `"trusted_keys": [...]`. A policy file that
isn't UTF-8 text did the same, such as the UTF-16 that Windows PowerShell
5.1's `Out-File` writes, and so did JSON nested too deeply. In the app, the
first caller was the updater, which logged the error as its own and carried
on. Every later call then saw no policy and no error: nothing was enforced,
model requests weren't refused, and a command the organization's shell rules
deny ran. Some values also loosened what they control when written as text:
`"allow_stdio": "no"` allowed command-based MCP servers,
`"require_signed": "true"` stopped requiring signed capability packs, and
`"registry_only": "true"` let packs outside the organization's registry run.

- **`load()` never raises** (`lumi/policy.py`). A policy that exists but
  can't be read or used is an error state on the first call and every later
  one. Model requests are refused until IT fixes it (`blocked_reason`).
  Failures `parse()` doesn't anticipate fail closed too.
- **`parse()` checks each section's type.** `settings`, `permissions`,
  `models`, `mcp`, `extensions`, `files`, `pricing`, `shell`, `approvals` and
  `cloud` must be objects. `trusted_keys` must map key ids to text.
  `grace_days` must be a whole number; text such as `"3"` still works. A
  missing or `null` section counts as empty.
- **True-or-false values must be `true` or `false`:** `mcp.allow_stdio`,
  `extensions.require_signed` and `extensions.registry_only`.
- **A policy file may start with a UTF-8 byte order mark.** Windows PowerShell
  5.1 writes one for `-Encoding utf8`, and it used to make the policy
  invalid. This covers the machine policy file, `LUMI_POLICY_FILE`, Group
  Policy's `PolicyFile` and `policy-keys.json`.
- **Lumi Cloud:** a downloaded policy with such a mistake isn't applied. It
  fails with "The organization's policy wasn't applied: permissions must be
  an object" instead of an `AttributeError` in the check-in. A stored policy
  Lumi can't use is reported in Settings, whatever the failure. The machine
  policy applies instead, or none for an organization joined in the app, as
  for other unusable downloads.
- **Not changed:** a budget rule's `block_unpriced` still counts only a
  literal `true` (`lumi/budgets.py`, which Settings' budgets share).

Validation on September 25, 2026:

- Full `pytest` after merging main (the pack registry and checkpoint
  Timeline): 4,380 passed, 5 skipped. `ruff check .` clean, the 66 Node tests
  in AGENTS.md pass, `git diff --check` clean.
- New tests in `test_policy.py` and `test_cloud.py`:
  - each mistyped section and value, and null sections still parsing;
  - a machine policy with a mistyped section, a UTF-16 file, `trusted_keys`
    as a list, `grace_days` as a list, or JSON nested 100,000 deep. The
    first and a later `load()` both return the error, and model requests are
    refused;
  - a failure `parse()` doesn't anticipate, or one reading the policy text,
    fails closed. One in a Lumi Cloud policy leaves the machine policy in
    force;
  - a file with a UTF-8 byte order mark applies;
  - a downloaded Lumi Cloud policy with a mistyped section isn't applied,
    and a stored one is reported, not raised.
- Against main before this change, 27 of the 29 new cases fail. The other two
  pass there too: `parse()` already refused a list for `trusted_keys` (it was
  `load()` that crashed first), and null sections already parsed.
- In the browser pane, from an isolated home with the scripted Ollama stub.
  The `LUMI_POLICY_FILE` policy had `"permissions": "ask only"` and a shell
  rule denying one command:
  - on main before this change, the updater logged the `AttributeError` at
    startup and no error showed. In Full-auto, the denied command ran and
    wrote its file;
  - with this change, the same message failed with "The organization policy
    at … is invalid: permissions must be an object. Ask your administrator to
    fix it." The model received no request, and no file was written.
    Settings > Privacy & security > Organization policy showed the same
    error;
  - the real `~/.resonant` was unchanged, and no `~/.lumi` or Lumi credential
    entries appeared.

Not exercised: a real Group Policy registry value or macOS configuration
profile, a packaged build, macOS and Linux.

## September 25 the terminal says why a tool call was refused — source only, not released

**A refused call printed only "✗ denied".** When a hook, a policy rule, a tool
boundary, the session's allowlist, malformed arguments or an approval nobody
could answer stops a tool call, the model gets the reason as the call's result.
The terminal UI (`lumi/tui.py`) dropped it, so a guard hook that timed out
looked the same as the person's own Deny.

- **The reason prints under the call** (`_render_tool_result`), which now reads
  "✗ not run". The reason is dimmed, and wrapped to the terminal's width inside
  the call's gutter. It can come from a hook, a policy, a repository or the
  model, so it is escaped (`rich.markup.escape`) and printed without emoji
  codes or highlighting: `[bold]`, `[/]` or `:x:` in it print as written.
- The person's own Deny ("Tool execution denied by user.") still reads just
  "✗ denied".
- **A refused read or search no longer looks like a result.** The terminal
  collapses a step of only reads and searches to one line per call. There, a
  refused `grep` read "0 matches", a `glob` "0 files", and a `file_read`
  showed nothing. They read "✗ not run", with the reason under them.

Validation on September 25, 2026:

- `tests/test_tui.py` (14 tests) captures the TUI's console as plain text, 72
  columns wide:
  - each refusal text the engine gives (a hook that timed out, a policy rule,
    a tool boundary, the allowlist, malformed arguments, no approval prompt, a
    task batch hook) prints "✗ not run" and the reason word for word, every
    line inside the gutter and the width;
  - the person's Deny, and an empty result, print "✗ denied" alone;
  - a reason with `[/]`, `[bold red]…[/bold red]`, a link tag, `:x:` and a
    trailing backslash prints exactly as written;
  - a reason with a 164-character path folds inside the gutter, nothing lost;
  - two real sessions (the streaming stub) run through `run_embedded`, as the
    TUI runs each message. A `pre_tool_use` hook that exits 1 with a bracketed
    message refuses a `grep` in a collapsed step and a command: both read
    "✗ not run" with the message the model was given, and the command didn't
    run. With approvals on (`--approve`), the TUI's own "Allow bash? [Y/n]"
    prompt, answered "n" through prompt_toolkit's pipe input, reads "✗ denied"
    alone and the command didn't run; answered "y" in a throwaway copy, it ran.
- On the previous `tui.py`, 10 of the 14 failed. The four Deny tests passed, as
  they should.
- On main at 1d1ffbe: full `pytest` 4,353 passed, 5 skipped. `ruff check .`
  clean (ruff 0.12.12; `pyproject.toml` pins the rule set), `node --check`
  passes for `app.js` and `settings_view.js`, the four Node UI test files pass
  (61 tests), and `git diff --check` is clean. `tests/test_tui.py` also passes
  with Rich 15.0.0, the release lock's version (14.0.0 is installed here).
  A first full run ended at 22% with exit code 127 and no failure reported,
  in `tests/test_computer_use_upgrades.py`; that file passed alone (25 tests),
  and the full rerun passed.
- Rebased on main at 3e74b97 (the organization's pack registry): full `pytest`
  4,359 passed, 5 skipped. `ruff check .`, both `node --check` runs, the four
  Node UI test files (61 tests) and `git diff --check` passed again.
- Real sessions from an isolated home, rendered through `consume_events` into a
  recorded Rich console at 80 and 52 columns: a refused `grep` under one that
  found "2 matches", a `pre_tool_use` hook that timed out after 2 s on
  `git push origin main`, and `npm publish` denied by the approval callback.
  The reasons wrapped inside the gutter at both widths, and the Deny read
  "✗ denied". The real `~/.resonant` was unchanged and no `~/.lumi` was
  created.

Not exercised: the TUI in a terminal window with a live model; the console was
captured instead. When this landed, the `lumi` TUI built its session without
hooks or an execution policy (`tui.py` `main`), so from it a hook or policy
refusal couldn't happen; "the terminal UI keeps the rules `lumi run` keeps"
above changes that. The tests give the session they pass to `run_embedded` a
hook.

## September 25 checkpoint Timeline — source only, not released

- **The Timeline is back** (`lumi/gui/static/app.js`, `openTimeline`;
  [guide](desktop-workflow.md#undoing-changes-the-timeline)).
  - **Timeline** in the chat header lists the open conversation's
    checkpoints, newest first, by what each was saved before, for example
    "Before writing notes.txt". The command palette and the session's menu
    open it too.
  - **Compare** (Git projects) shows what changed since a checkpoint.
  - **Restore…** asks for Files, Conversation, or Files and conversation. It
    says what each does and where your current files will be kept, and it
    waits for the current run to stop.
  - The Timeline left the page with the Agents pane in v0.14.0, so restoring
    a conversation or a non-Git snapshot had no desktop control.
- **Checkpoints belong to the saved conversation**
  (`AppState.bind_conversation_checkpoints`). They were filed under a random
  id for each session build, so an app restart, or a draft's first message,
  started an empty Timeline.
- **A worker's checkpoint restores files only.** It holds the worker's
  conversation. Checkpoints now record whether a worker saved them
  (`metadata.subagent`), and the server refuses the other modes.
- **The list sends only what each call acted on**: a path, or a command's
  first line. It sent each checkpoint's full arguments, including a write's
  whole file.
- **Each restore is marked in the chat** by a display-only `timeline.restored`
  event. Without it, a conversation restored mid-turn showed "Response didn't
  start" and Retry, as if the turn had crashed, after every reload. The
  model's conversation never contains the event.
- For Codex and Claude Code conversations, the Timeline says that their own
  changes have no checkpoints.
- Replay fixes found on the way:
  - A replayed turn that never ended kept a ticking "thinking" row inside its
    **Work details**.
  - A reloaded conversation counted each turn's tools on top of the earlier
    turns', for example "2 actions" for a turn with one.

Validation on September 25, 2026:

- Full `pytest` 4,351 passed, 5 skipped, after merging `main`; `ruff check`
  clean.
- `tests/test_checkpoint_timeline_ui.py` (6 tests) covers:
  - the list without a call's contents;
  - a worker's checkpoint, refused for the conversation, restored for files;
  - checkpoints that outlive a rebuilt session, and a draft's first turn
    saving to its conversation;
  - the worker flag on checkpoints;
  - the chat marker after a files and a conversation restore, kept out of the
    model's conversation.
- `tests/ui_recovery.test.cjs` adds 5 Timeline tests: labels, restore choices
  and messages, the confirm flow, CLI connections, and a restored
  conversation's replay. The three UI node suites: 58 passed.
- In the browser pane, with an isolated home and a scripted local model:
  - A non-Git project, where two turns wrote `notes.txt`. The Timeline listed
    two "Before writing notes.txt" file snapshots. **Files** put back the
    original and kept the replaced copy in a recovery archive.
    **Conversation**, chosen and confirmed with the keyboard, rewound the
    chat.
  - After an app restart, the Timeline still listed them. **Files and
    conversation** put back the first turn's file and ended the chat with
    "Files and conversation restored to before writing notes.txt", with no
    crash banner, after a reload too. A new message carried on from there
    and saved a third checkpoint.
  - The header button, the palette and the session menu each opened it.
    Escape returned focus to the Timeline button. At 375px nothing scrolled
    sideways.
  - A Git project. **Compare** listed `M notes.txt` with its diff stat.
    **Files** put the file back and kept the replaced copy on a
    `lumi-recovery/…` branch.
  - Fixes from this run:
    - The header button was hidden while autonomous sessions are off, because
      it shared their CSS class.
    - A restored conversation showed the crash banner.
    - The thinking row and the tool counts in replays.
    - The Restore buttons had identical accessible names; they now include
      the checkpoint's time.
    - At 375px the checkpoint details were cut off.

Not exercised: a Codex or Claude Code conversation (its Timeline text is
tested only in node); a packaged build.

## September 25 the organization's pack registry — source only, not released

- **The registry in policy** (`lumi/policy.py`, `lumi/engine/capability_packs.py`,
  [guide](extensions.md#your-organizations-registry)).
  - `extensions.registry` lists the packs an organization approves: an id,
    a public https repository, a full commit, an optional folder and an
    optional content digest.
  - `extensions.registry_only` turns off every other pack, and a registry
    pack at another version, with the reason in Settings.
  - A pack matches its entry by its content digest when one is pinned.
    Otherwise it must have been installed from that repository, commit and
    folder.
  - Lumi Cloud's new Extensions page writes the registry
    (Luminary-Analytics/lumi-cloud#26).
- **Settings > Capability packs** lists the registry under the organization's name.
  - Each entry shows whether it's installed at the pinned version, with
    **Install** or **Install this version**. Registry packs' cards say
    whether they're that version.
  - A registry install fetches exactly the pinned commit. It refuses a pack
    whose id or content digest differs before replacing what's installed.
  - People still approve each pack.
- **`lumi extension check`** prints the content digest to pin.
- **Line endings.** Signatures and registry digests now count CRLF as LF in
  text files. A file with a NUL byte still counts byte for byte. The
  browser check below found this: the fixture's digest came from a CRLF
  working copy and the installed checkout had LF, so the pin never matched.
  Signatures from #61 had the same problem across Windows and other systems.

Validation on September 25, 2026:

- `tests/test_pack_registry.py` (5 tests) covers:
  - the policy's checks;
  - `registry_only` with a digest pin and with install records, and
    without the switch;
  - the digest `lumi extension check` prints;
  - the Settings commands with a real Git repository: installing, a newer
    pin turning the old install off until updated, and refusals (a wrong
    digest leaving the install alone, an id not in the registry, a commit
    holding another pack).
- `test_pack_signing.py` adds line-ending, binary and lone-CR cases.

In the browser pane, with a pilot policy whose registry pinned one pack by
commit and digest, and a stray personal pack:

- The stray pack was off: "Fixture Org's policy allows only packs from its
  registry."
- The registry listed Review tools at its commit, "content pinned · Not
  installed".
- **Install** failed until the line-ending fix. After it, the pack
  installed "at this version", waited for approval, and was approved and
  active.

Not yet: approving a registry pack for everyone, private repositories (a
GitHub App), and update notices.

## September 25 signed capability packs — source only, not released

- **Publisher signatures** (`lumi/engine/pack_signing.py`,
  [guide](extensions.md#signing-a-pack)).
  - `lumi extension keygen` makes an Ed25519 key, and `lumi extension sign`
    writes `lumi-pack.sig`. It covers every file in the pack but itself,
    signed together with the publisher's name and key.
  - A key's id is the first 16 hex digits of the SHA-256 of the key.
- **Lumi checks the signature whenever it loads a pack.** Settings >
  Capability packs shows the result:
  - **Verified**, under the name you or your organization gave the key;
  - **Signed with a key you haven't trusted**, which offers to trust it;
  - **Invalid**: the files changed or the signature is malformed. The pack
    can't be approved.
  - **Not signed**.
  - Trusting a key approves nothing.
  - **Trusted publishers** lists yours, with **Forget**, and your
    organization's.
- **Organization policy.** `extensions.trusted_publishers` names publishers
  and their keys. `extensions.require_signed` turns off every pack one of
  them didn't sign. Keys a person trusted don't count, and then no Trust
  buttons appear. Lumi Cloud's Policy page writes both
  (Luminary-Analytics/lumi-cloud#25).
- `lumi extension check` shows a pack's signature too.

Validation on September 25, 2026: `tests/test_pack_signing.py` (13 tests)
covers:

- signing and checking, and keys that are trusted, unknown, or match only
  by id;
- tampering: an edited file, an added file, a changed publisher name, a
  wrong key id, a new format, unreadable JSON;
- refusing a missing publisher, a non-key and an RSA key;
- an invalid signature turning a pack off;
- an organization requiring its publishers, where a key the person trusted
  doesn't count, and the policy's validation;
- trusting and forgetting a publisher through the Settings commands,
  including refusing one whose files changed;
- the CLI's keygen (never overwriting a key), sign and check.

In the browser pane, with four personal packs:

- The genuine pack showed "Signed as “Acme” with key 1966 f367 e44a 956b".
  A look-alike signed "Acme" with another key showed its own key. The
  unsigned pack showed "Not signed". The tampered pack was off, with its
  reason shown once and no Approve button.
- **Trust “Acme”** made only the genuine pack "Signed by Acme · verified",
  still not approved. **Forget** undid it.
- With a pilot policy trusting the key as "Acme IT" and requiring
  signatures:
  - The genuine pack showed "Signed by Acme IT" and could be approved.
  - The look-alike and the unsigned pack were off, with "Fixture Org's
    policy turns off packs that a publisher it trusts didn't sign."
  - The policy's publisher was listed without Forget.
  - Two fixes came from this run: the Trust buttons now name their key and
    pack for screen readers, and they no longer appear under such a policy.

Not yet: signed updates approving themselves, a revocation list, an
organization registry of packs.

## September 25 model providers as extensions (Extension SDK v1) — source only, not released

- **Model providers from capability packs** (`lumi/engine/provider_extensions.py`,
  [guide](extensions.md)).
  - A pack's manifest can declare `providers`: a command, or one per system,
    and the models it offers.
  - Lumi starts the provider for each request and talks to it in JSON lines
    over standard input and output: `models`, and `stream` answered with
    text, tool calls, then `done` (usage, and a cost when the provider knows
    it) or `error`.
- **Connections.** Settings > Connections has a new type, **A provider from a
  capability pack**, which picks an approved pack's provider.
  - The connection's key reaches the process as `LUMI_PROVIDER_API_KEY`.
  - **Test connection** starts the provider.
  - Models the manifest lists are offered without starting it.
- **Trust.**
  - Only approved, enabled personal packs provide models. The pack is
    resolved again before every start, so a changed or revoked pack stops at
    once.
  - Settings > Capability packs shows each provider's command before
    approval.
  - The process gets the usual child environment, a data folder outside the
    pack, and `PYTHONPYCACHEPREFIX`. Without that, a Python provider wrote
    `__pycache__` into its pack on its first run and turned the pack off. It
    also keeps `.pyc` files shipped in a pack from running instead of the
    reviewed source.
  - A bare program name comes from absolute PATH entries only; Windows would
    otherwise look in Lumi's current folder first.
- **Manifest version 1** (`lumi/engine/capability_packs.py`):
  - `manifest_version`; a newer one doesn't load, and Settings says why;
  - `lumi`, the Lumi versions a pack works with;
  - checked `providers`.
- **The SDK** (`sdk/`):
  - `lumi_extension`, a Python package using only the standard library:
    `Provider`, `serve`, the events and `data_dir`;
  - `lumi_extension.testing`, which runs a provider as Lumi does;
  - `sdk/templates/provider-python`, with an offline `echo` model, an
    OpenAI-compatible `remote` model and tests;
  - `sdk/new_pack.py`, which makes a pack from the template;
  - `sdk/schema/lumi-pack.schema.json`, for editors.
- **`lumi extension check <folder>`** (`lumi/extension_check.py`) loads a
  pack with Lumi's rules, lists its models and asks the first one a short
  question.

Validation on September 25, 2026: `tests/test_provider_extensions.py` (32
tests) starts real provider processes. It covers:

- the manifest's rules and version requirements;
- the template, made with `sdk/new_pack.py`: approved, answering in a real
  session with a `file_read` tool call, still approved afterwards with no
  `__pycache__` in the pack, and refused after an edit;
- packs inside a project never providing models;
- the template's own tests passing;
- `lumi extension check` on a working, a failing and a too-new pack;
- the key and environment the process gets, and no other model keys;
- failures: an exit code with the key removed from the message, output that
  isn't JSON, a timeout;
- Stop ending the process;
- the events Lumi reads, including usage, cost and unknown types;
- unfinished and refused answers;
- the conversation as text, with notices for images;
- PATH lookup that ignores the current folder and relative entries;
- the Connections page's data, connection checks, the SDK's `serve` and test
  kit, and the manifest schema (skipped without jsonschema).

Removing the `PYTHONPYCACHEPREFIX` line made the session test fail: the pack
turned itself off after its first run.

In the browser pane, with the template installed in an isolated
`~/.lumi/packs`:

- The pack's review listed the provider's command for each system. It was
  approved there.
- Adding a connection of the new type showed only the fields that apply and
  named the connection after the provider.
- **Test connection** started the provider through a PATH lookup of
  `python`: "Connected · 2 models available: echo, remote". Saving put both
  models in the model menu.
- With `echo`, the message `call file_read {"path": "README.md"}` made the
  provider call the tool; Lumi ran it, and the next answer quoted the file.
  The pack folder had no `__pycache__` afterwards.
- After `provider.py` was edited, the next message failed with "Approve the
  Acme models pack in Settings > Capability packs to use its models. It
  changed since you approved it." The pack showed "Changed since approval ·
  off" until it was approved again.

Not yet: signed packs, an organization registry, long-running providers,
images and reasoning levels.

## September 25 worker transcripts and controls — source only, not released

The Agents pane that held a worker's transcript and its pause, resume, stop,
steer and restart controls left the page in v0.14.0. The server commands
stayed, but nothing in the app reached them. They're back, next to the work
([desktop workflow](desktop-workflow.md)):

- **While a worker runs** (`static/run_cards.js`, `static/app.js`): the run
  details' **Sub-tasks** list gives it **Pause** (then **Resume**), **Stop**
  and **Steer…**. Steer opens a small dialog; the worker reads the direction
  before its next step and keeps going. A paused worker says "Paused", a
  stopping one "Stopping…".
- **Once it stopped**, its block offers **Transcript**: a dialog with its
  status, steps, model and assignment, then its messages, each tool call with
  its own result (matched by call id), your steering, and errors. A worker
  that failed, was stopped or was interrupted when Lumi closed also offers
  **Restart**, which waits for the current run to finish. The original keeps
  its record and reads "restarted".
- **A restart is a turn of its own** (`lumi/engine/session.py`,
  `restart_agent`). It used to yield only the worker's events, so the page
  never showed it running, and after a reload it looked interrupted and
  offered to resume finished work. It now starts with session.start and ends
  with a session.end whose outcome comes from the worker's handoff: its
  changed files and summary, or a failure. It claims no named checks. The
  app shows the turn's message as it starts (`agent.restarted` carries it).
- **Interrupted turns after a reload** (`static/app.js`): a turn that Lumi
  closed during kept a running card, which hides its activity until a live
  dock opens, so its work (including an interrupted worker) couldn't be seen.
  It now shows as interrupted, or paused next to **Continue**, with its work
  under **Work details**.
- Removed the old pane's detail renderer (`showRuntimeAgentDetail`). Its
  **Restart** also sent a stray `agent_runtime_control` "restart", which the
  server rejected.

Validation on September 25, 2026:

- Full `pytest` with `main` merged in: 4,294 passed, 5 skipped. `ruff check .`
  clean, the UI node tests (`ui_recovery`, `appearance`, `autonomous_view`)
  53 passed, `git diff --check` clean.
- `test_agent_restart_dispatch.py`: a restart's events sit between
  session.start and session.end, a changed file ends it `changed_unverified`
  with that file, a failed worker or refused dispatch ends it `failed`, and a
  refused restart yields nothing. `test_worker_restart_turn.py` runs a stuck
  worker's restart through the real `_run_session_streaming`: the file
  changed, and the recorded display events end with session.end. 5 of these
  6 fail on the previous `session.py`.
- Six node tests cover which controls each status gets, the Sub-tasks
  buttons and their escaping, what each control sends (a restart waits for
  the current run), transcript entries (call ids, denials, failures, calls
  with no result, steering, errors), the restart turn's start, and settling
  an interrupted card. Seven mutants each fail one of them: Restart for a
  completed worker, a restart during a run, results not matched by call id,
  steering left out, settling a card no longer on the page, an unescaped
  worker name, and controls kept while stopping.
- In the browser pane, with an isolated home and a scripted
  Ollama-compatible model whose parent delegates an edit of `notes.txt` to a
  build worker, in Full-auto:
  - With the worker's request held for 25 s: **Pause** showed "Paused" and
    **Resume**; **Resume** brought back its clock. **Steer…** opened with
    focus in its text box, and **Enter** sent "Keep the rest of notes.txt
    unchanged", which reached the worker's next request as `<user_steer>`.
    **Stop** showed "Stopping…", and the worker ended with blocker
    "Interrupted", **Transcript** and **Restart**.
  - **Transcript** listed its status, steps, model and start, the assignment,
    "Edit file notes.txt — done" with its output, and the interruption.
    **Escape** closed it and focus returned to the button. At 375 px it fit
    without horizontal scrolling.
  - With a worker held mid-request, the app was killed and started again. The
    turn showed as paused, with its work under **Work details** (before this,
    its activity stayed hidden). The worker read "Interrupted when Lumi
    closed" with **Transcript** and **Restart**. **Restart** ran a new turn,
    "Restarting build agent (interrupted after 0 steps)", which changed
    `notes.txt` and finished "Changed — verify"; the old block then read
    "restarted" without **Restart**. After a reload both turns showed the
    same, the first as "Interrupted", with no resume banner.
  - The real `~/.resonant/settings.json` was unchanged, no `~/.lumi` was
    created, and no Lumi credential was stored. Settings > Connections was
    not opened.

Not exercised: a live model, a packaged build, Codex or Claude Code workers
(they run their own tools), and `task_batch` workers side by side.

## September 25 a broken lumi-policy.json can't drop organization rules — source only, not released

**A repository could turn off its organization's shell rules.** Committing a
`lumi-policy.json` that wasn't an object holding a list of rule objects was
enough, whether or not the user trusted the project. `[]`, `{"rules": "x"}`
or `{"rules": [1]}` made building the execution policy fail. The app caught
the error and fell back to the permission mode's own rules, without the
organization's `shell.rules`, which are meant to outrank everything but the
guardrails. `lumi run` crashed instead. `{"rules": 5}` and `{"rules": null}`
did the same, and also broke the project trust check. A rule with a value of
the wrong type, such as `"arg_patterns": "x"`, loaded and then failed the
tool calls checked against it.

- **Rules are checked when they load** (`lumi/engine/policies.py`,
  `PolicyRule.from_dict`):
  - `tool_pattern` is text;
  - `action` is `allow`, `prompt` or `deny`;
  - `arg_patterns` maps argument names to regular expressions that compile;
  - `arg_globs` maps them to a glob or a list of globs.

  Keys Lumi doesn't use are still ignored.
- **A mistake in a repository's file costs only that file's rules**
  (`repository_rules`). A warning in the log names each mistake.
  - A file that isn't an object with a `rules` list, or isn't readable JSON,
    contributes no rules.
  - A broken rule is dropped, and so are all the file's `allow` rules. Rules
    apply in order and the first match wins, so without the broken rule a
    later `allow` could let through what it was meant to stop. Auto-edit
    asks again about the commands those `allow` rules would have run. The
    file's valid `deny` and `prompt` rules still apply, since they only make
    Lumi more careful. Fixing the file brings the `allow` rules back.
  - A rule with an unknown action (such as `"ask"`) or a regular expression
    that doesn't compile used to be skipped silently. It now counts as a
    mistake, so the file's `allow` rules are off until it's fixed.
  - The trust banner and Settings count only `allow` rules that would apply.
- **The organization's rules always apply.** `with_organization_rules` adds
  them to the project's policy. The app's fallback, used when a project's
  policy can't be built, now keeps them as well as the mode's own rules.
- **An organization shell rule that can't be applied makes the policy
  invalid** (`lumi/policy.py`). As with any invalid policy, Lumi refuses
  model requests until IT fixes it, instead of failing tool calls. A `shell`
  section that isn't an object is invalid too.
- **Not changed:** intent specialists (`orchestration/runner.py`) still run
  with the Full-auto rules alone. They get neither the organization's shell
  rules nor the project's policy.

Validation on September 25, 2026, after merging main (including the
repository allow rules change):

- Full `pytest`: 4,223 passed, 5 skipped, after merging main again (worker
  tool lists, hand-offs, team skills). `ruff check .` clean, the 54 Node
  tests in AGENTS.md pass, `git diff --check` clean.
- New tests check that the refused command's file wasn't written:
  - `test_permission_decisions.py` covers 11 malformed files, from `[]` to
    rules with values of the wrong type, a file that isn't UTF-8 and JSON
    nested 100,000 deep. In each, a Full-auto `Session.run` refuses a
    command that an organization rule denies, with the organization's
    reason, and other calls are still checked. A file with a broken rule
    keeps its `deny` and `prompt` rules, drops its `allow`, and logs the
    rule's number. Once the file is fixed, the `allow` applies again.
  - `test_repository_allow_rules.py`: in a trusted project in Auto-edit, a
    command the file's `allow` rule would run without asking asks once the
    file also holds a broken rule, and the file's `deny` still refuses its
    command.
  - `test_gui_permission_modes.py` uses the app's own run loop and `approve`
    handler, in a trusted project whose file is `[]`, `{"rules": 5}` or
    `{"rules": [1]}`. The command is refused without a prompt. With the
    project's policy made to fail, the fallback refuses it too and keeps the
    guardrails.
  - `test_headless.py`: `lumi run --mode bypass --trust-project` with `[]`,
    `{"rules": 5}` or a file that isn't UTF-8 refuses the command (exit 3,
    `denied_calls` 1) instead of crashing.
  - `test_execution_policy.py`, `test_policy.py` and
    `test_exclusions_and_trust.py` cover each kind of bad rule, organization
    policies with bad rules (also through `load()`), and the trust check.
- Against main before this change, 28 of the 33 new cases in those files
  fail. The other five are cases main already handled: a file that isn't
  UTF-8, which the repository allow rules change skips (two cases), and
  three trust-check shapes (`[]`, not UTF-8, not JSON). The new unit tests
  can't import there. In the app's run loop, the command the organization
  denies ran.
- In the browser pane, from an isolated home with the scripted Ollama stub
  and an organization policy for "Acme" (`LUMI_POLICY_FILE`) that denies
  one command:
  - with `lumi-policy.json` set to `{"rules": 5}`, the trust banner offered
    the file, and **Trust this project** worked. In Full-auto the command
    Acme denies was refused ("Blocked by policy: Acme: not from the agent's
    shell") and wrote nothing, and `mkdir allowed-dir` ran;
  - with a valid file, a `file_write` deny and an `allow` for
    `mkdir allowed*`, trusted in Auto-edit, `mkdir allowed-dir` ran without
    asking. The audit log recorded the approval by `project_policy`;
  - with a broken rule added, the banner offered no approval-skipping rules.
    After **Trust the change**, the same command opened Command Review, and
    **Deny** kept it from running. The write was refused ("Blocked by policy:
    repository: frozen"), and the command Acme denies was refused without a
    prompt;
  - the log named the mistake each time the policy was built. There were no
    console errors;
  - the real `~/.resonant` was unchanged afterwards. No `~/.lumi` or Lumi
    credential entries appeared. The fixture's own `CODEX_HOME` was never
    created. `~/.codex` logs changed during the run, as the user's own Codex
    writes them every few minutes; those writes are unattributed.

Not exercised: a packaged build, a live model, Codex or Claude Code (they run
their own tool loops), macOS and Linux.

## September 25 worker handoffs in the conversation — source only, not released

- **A worker's handoff shows under its block** (`lumi/gui/static/app.js`,
  `handleSubagentEnd`). The line that ends a worker's block in the task's
  activity now says whether it changed files, for example "✓ build · 2 steps ·
  12.2s · 1 file changed" or "no files changed". Opening it shows the
  handoff's changed files, checks, blockers, other evidence and next step. A
  file opens with a click or **Enter**. A failed or blocked worker's handoff
  opens by itself. The Agents pane that used to show handoffs left the page in
  v0.14.0, so the app had no place that showed them.
- A failed worker's result line uses the error color instead of the success
  color.
- "Opening *file*…" showed "â€¦" instead of an ellipsis.
- The runtime guide and [known issues](known-issues.md) now name the views
  that lost their entry point with the Agents pane: the checkpoint Timeline,
  traces and the artifact list. (Worker transcripts and controls, and the
  checkpoint Timeline, are back; see their sections above.)

Validation on September 25, 2026:

- Full `pytest` 4,149 passed, 5 skipped, before the last merge of `main`;
  `ruff check` clean; the UI node tests (`ui_recovery`, `appearance`,
  `autonomous_view`) 47 passed after it.
- A new node test covers:
  - the result line: counts, "no files changed", and no claim about files
    without a handoff;
  - which parts show, with the engine's step and time evidence left out;
  - opening on failure or blockers;
  - escaping of paths and blockers;
  - the 12-file limit.
- In the browser pane, with an isolated home and a scripted
  Ollama-compatible model that delegates an edit of `notes.txt` to a build
  worker, in Ask mode, allowing the task:
  - **Reject** showed "✓ build · 2 steps · 10.5s · no files changed" and
    the next step. **Accept** showed "1 file changed" and **Changed files:
    notes.txt**.
  - **Enter** on the result line opened it. **Enter** on the file sent
    `open_workspace_path` for `notes.txt`; the page intercepted it, so nothing
    opened.
  - After a reload, both handoffs showed the same. At 400 pixels wide the
    handoff fit without horizontal scrolling.
  - The real `~/.resonant/settings.json` was unchanged, no `~/.lumi` was
    created, no Lumi credential was stored, and the fixture's `CODEX_HOME`
    stayed empty.

## September 25 gate hooks fail closed — source only, not released

**A guard hook that hung or couldn't start let the tool run.** A gate hook
(`pre_tool_use`, `pre_tool_batch`, `before_model`, `permission_request`,
`task_completed`, `subagent_stop`, `validation_complete`) blocks when it exits
non-zero. One that ran past its `timeout_seconds`, or couldn't be started,
only recorded an error, and the call went ahead. On Windows the timeout wasn't
enforced either: Lumi killed the shell, then waited for the program the shell
had started, so a hung hook held the turn until it finished.

- **Gate hooks fail closed** (`lumi/engine/hooks.py`, `GATE_HOOK_TYPES`). A
  gate hook that times out or can't be started blocks, and the reason names
  it: "Blocked by hook: pre_tool_use hook \`slow-guard\` timed out after 2 s;
  gate hooks block when they give no answer. Raise its timeout_seconds if it
  needs longer." Other hook types still only log a failure.
- **The timeout is enforced.** At `timeout_seconds` Lumi stops the hook and
  everything it started (a job object on Windows, the process group
  elsewhere), even while writing the event to a hook that never reads it. A
  program that a hook finishing in time leaves running on purpose keeps
  running.
- **The model reads why** (`lumi/engine/session.py`). A blocked call's result
  carries the hook's reason: a JSON hook's `reason` (a JSON `deny` used to read
  "Blocked by hook: denied"), its error output, or the timeout. It is the
  blocking hook's reason, never one an earlier hook gave for allowing. A
  refused `task_batch` is now recorded in the conversation; before, only the
  app showed it.
- **A completion gate that gives no answer ends the turn** with "Completion
  was not accepted: …" instead of asking the model to try again. The model
  can't fix the hook, and a rejection doesn't use up a step, so each retry was
  another model request until a request allowance or a budget stopped the
  turn; neither is set by default.
- **Fixed along the way**, since each of these would now block instead of
  skipping the hook:
  - A JSON event with text a Windows code page can't encode (an arrow, an
    emoji, Japanese) couldn't be written, so the hook never ran. The event is
    ASCII-only JSON now, which JSON readers decode to the same text.
  - A session without a project ran hooks in "", where nothing can start.
    They run in Lumi's own folder instead.
  - A JSON hook no longer gets a copy of arguments larger than 64 KiB in
    `LUMI_TOOL_ARGS`; it reads them from standard input. On Linux, which allows
    128 KiB per environment value, the copy kept it from starting.
  - A JSON answer whose `hookSpecificOutput` wasn't an object raised an error
    after its `deny` was noted, and the call went ahead.

**If a gate hook is slow,** a run that used to time out without effect now
blocks. Set its `timeout_seconds` (default 30) above the time its work takes,
in the hook's entry in `settings.json` (`hooks`) or in its pack's manifest
([capability packs](packs.md#gate-hooks-fail-closed)). On Linux an `env` hook
can't start for a call whose arguments exceed 128 KiB, so a gate hook blocks
that call; `"input_format": "json"` avoids it. The tool row showed a refused
call only as "denied" until "refused tool calls say why" (above) put the
reason under it.

Validation on September 25, 2026:

- `test_hook_gates.py` (40 tests) runs real hook scripts through a real
  `HookRunner` and real `Session.run` turns:
  - a gate hook sleeping past a 1 s timeout blocks well within the limit, for
    `env` and JSON hooks, including a 100 KB event it never reads. The program
    it started is gone afterwards, both with the job object and with the
    `taskkill` fallback;
  - every gate type blocks, and every other type only logs, when its hook
    can't start (a missing project folder);
  - in real turns, a `file_write`, a permission request, a `task_batch` and a
    completion gate: nothing is written, the reason is in the result and in
    the conversation, and the completion gate makes one model request;
  - a program left running by a hook keeps running; a JSON event with an
    arrow, a check mark, Japanese and an emoji; large arguments on standard
    input only; the reason precedence; a JSON `deny`'s reason; a
    `hookSpecificOutput` string; a session without a project.
- On the unfixed code, 36 of the first 37 new tests failed. The one that
  passed guards the reason precedence that the session change relies on.
  Undoing each fix alone (twelve mutations, from the old `subprocess.run`
  timeout and killing only the shell to the batch refusal left out of the
  conversation) failed at least one test each.
- Linux (WSL Ubuntu 20.04, Python 3.13) ran `hooks.py` itself: timeouts
  returned at 1.0 s with the process group gone, also when `sh` stayed the
  parent; a 200 KB argument blocked an `env` hook with the advice to use JSON
  and reached a JSON hook on standard input; a program that left the process
  group was given up on after the 5 s grace.
- In the browser pane, from an isolated home with the scripted Ollama stub and
  a `pre_tool_use` hook in `settings.json` that sleeps past `timeout_seconds:
  2`: the model's `file_write` came back 2 s later as "Blocked by hook:
  pre_tool_use hook \`slow-guard\` timed out after 2 s; …", `guarded.txt`
  wasn't created, the hook's program was gone, and the turn ended as needing
  attention. The tool row read "denied" without the reason. The real
  `~/.resonant` and the credential store were untouched.
- After merging main, full `pytest`: 4,208 passed, 5 skipped. `ruff check .`
  clean, 46 Node UI tests pass, `git diff --check` clean.

Not exercised: macOS, a packaged build, Codex or Claude Code, a live model.

## September 25 workers run only the tools they were given — source only, not released

**A read-only worker could start a writing worker.** A delegated worker gets a
tool list for its type. An `explore` or `plan` worker has no write tools, and
no worker gets `task` or `task_batch`. The list offered to the model is only a
hint, and the check that enforces it (`lumi/engine/session.py`) sat after the
branches that run `task`, `task_batch`, `await_user`, `search_tools` and MCP
tools themselves, so it never applied to them. An `explore` worker whose model
called `task` anyway started a `build` worker, which wrote a file. An MCP tool
ran the same way. It never went past the conversation's permission mode: the
new worker had the same approvals.

- **The check comes first now**, for every tool, right after a worker's
  action guard. A tool outside the list is refused (`is_error` and `denied`)
  before any hook, policy or approval prompt, so Ask doesn't ask about a call
  that can't run.
- **Unchanged for the tools a list includes**: orchestration specialists list
  `await_user` and the MCP tools they may use, and those still run. A harness
  evaluator's empty list now refuses every tool. SONN workers keep their own
  file-only guard, checked first.
- Docs: [agent runtime](modern-agent-runtime.md#tool-approvals) and
  [Director Mode](director-mode.md).

Validation on September 25, 2026:

- Full `pytest` on the latest `main`: 4,156 passed, 5 skipped. `ruff check .`
  clean, the UI node tests (`ui_recovery`, `appearance`, `autonomous_view`)
  46 passed, `git diff --check` clean.
- `test_worker_tool_list.py` (7 tests) drives `Session.run` with a scripted
  model that answers as the parent, an `explore` worker or a `build` worker.
  The `explore` worker's `task`, `task_batch`, MCP tool, `await_user` and
  `search_tools` calls are refused. No other worker is recorded, the MCP
  server isn't called and no file is written. In a session that asks before
  changes, only the parent's own `task` is put to the user. A listed
  `await_user` still reaches the user.
- Against the previous `session.py`, 6 of the 7 failed. The listed tool
  passes on both.
- In the browser pane, with an isolated home and a scripted
  Ollama-compatible model, in Ask mode: the parent delegated to an `explore`
  worker, whose model then called `task` to start a `build` worker that
  writes `escalated.txt`. After **Allow** for the parent's own task, no other
  dialog appeared. The worker's `task` row showed **denied**, the model got no
  request from a `build` worker, and no file was written. On the previous
  `session.py`, the same run showed a second Command Review for the worker's
  `task`. **Allow** there started the `build` worker, and **Accept** on its
  write card created `escalated.txt`. The real `~/.resonant/settings.json`
  was unchanged, no `~/.lumi` was created, and no Lumi credential was stored.

Not exercised: a live model, a packaged build, orchestration specialists and
harness evaluators in the app (their existing tests pass), and Codex or Claude
Code, which run their own tools.

## September 25 a second person approves risky commands — source only, not released

- **`approvals` in organization policy** (`lumi/policy.py`,
  `lumi/engine/second_approval.py`, [guide](second-approval.md)) lists
  commands, as `fnmatch` patterns over the whole command, that someone else
  in the organization approves before they run. Lumi Cloud's Policy page
  writes it (Luminary-Analytics/lumi-cloud#24).
- **The session**: after the person's own approval, and only for commands the
  irreversibility floor doesn't refuse anyway, it asks Lumi Cloud through
  `lumi/approvals.py`. Saved keys and secret patterns are removed from the
  command first.
  - It emits `approval.wait` (waiting, then approved, denied, expired,
    cancelled or unavailable) and waits, cancellably, up to the policy's
    `wait_minutes`.
  - Only an approval runs the command. Otherwise the model is told why.
- **The conversation** shows a note with the command, who can approve and the
  outcome.

Validation on September 25, 2026: `test_second_approval.py` (8 tests) covers:

- which commands match;
- approved, denied, expired, stopped and unavailable, with a dropped
  connection while polling;
- secrets removed from the request;
- a real session running a command only once approved;
- nobody being asked about a force-push to main the floor refuses;
- which organization the requester uses.

`tests/conftest.py` resets the requester between tests.

A cross-check with the real Lumi Cloud of that branch in one process:

- The owner saved and published `git push --force*`, and the app's parser read
  it.
- Bob's app asked, and Ada got the email.
- Ada approved on the Approvals page, and the app's wait ended "approved by
  ada".

In the browser pane, Lumi Cloud and an isolated app signed in as Bob ran
together with a policy holding `git push*`, and a stub model asked to run
`git push origin main`:

- The conversation showed the waiting note naming Ada.
- Ada approved in the portal, the note changed to "Approved by ada. Running
  it.", and the push reached the fixture's remote.
- An earlier run with `git push --force origin main` showed approval being
  asked for a force-push the floor then refused. That led to the floor check
  before asking. The portal also capitalized commands' first letter, which is
  fixed there.

## September 25 agent changes wait for a reviewer — source only, not released

- **Settings > Code review > Agent changes wait for a reviewer**
  (`lumi/engine/review_gate.py`, [guide](code-review.md)), with **Reviewers**
  (GitHub usernames or `organization/team`).
  - An organization can lock both in its policy (`review.agent_changes`,
    `review.reviewers`; Luminary-Analytics/lumi-cloud#23). The client's policy
    parser checks their types.
- **While on, the agent doesn't merge or push to a default branch.** Deny
  rules for `gh pr merge`, `glab mr merge`, completing an Azure DevOps pull
  request, and `git push` naming main, master, trunk or production come right
  after the guardrails in every tier and project policy. That puts them ahead
  of repository and organization allow rules.
- **Its pull requests name the reviewers.** `github_pr_create` adds a line to
  the description.
  - On GitHub it requests review from the named people and teams.
  - It tells the model the change waits for review.
  - When the person is signed in, it reports the pull request to the
    organization's review queue in Lumi Cloud (`lumi/review_queue.py`), and
    `github_pr_view` reports its state as reviews come in.

Validation on September 25, 2026: `test_review_gate.py` (16 tests) covers:

- off by default;
- what's refused and what isn't (a feature-branch push, `main-refactor`,
  `echo gh pr merge`);
- every tier denying a merge, even with a repository allow rule and an
  organization allow rule;
- the pull request's description, the reviewer request and the queue report
  against a mocked GitHub;
- the state reported from `github_pr_view`;
- the Settings validation, and the policy lock's parsing.

A cross-check ran the real Lumi Cloud of that branch in-process: the app's
`ReviewQueue` registered a pull request and reported it approved. A policy
saved and published with the review lock parsed in the app as `True` with
`['octocat', 'acme/platform']`.

In the browser pane, with an isolated app, Settings > Code review turned on
with Space. A reviewer typed as "octo cat" was refused with a message, and the
typed text stayed. Corrected, it saved `['octocat', 'acme/platform']`.

## September 25 project notes for the team — source only, not released

- **Share with the team** on a note in **Project notes**
  (`team_library.share_note`, [guide](team-library.md#project-notes-for-the-team))
  proposes it to the organization in Lumi Cloud (Luminary-Analytics/lumi-cloud#22).
  - It carries its text, kind and source, the repository from the origin
    remote, and a line-ending-normalized hash of each source file.
  - A stale note can't be shared.
- **Recall**: the library's publishers approve notes in Lumi Cloud. Lumi syncs
  approved ones with the library and recalls up to six per turn, constraints
  first, as **Team project notes**.
  - Only in clones of the note's repository, and only while each source file's
    hash matches.
  - Each note carries who wrote it and who approved it.
  - They come from the organization, so project trust doesn't gate them.
- **Project notes lists them under From your team** with their provenance, and
  whether they're recalled here. **Settings > Lumi account > Team library**
  counts them.

Validation on September 25, 2026: `test_team_library.py` gained 3 tests
against a real temporary repository:

- recall for the same repository only, with a Windows-line-ending Makefile
  matching a note's hash, and an edited file excluding it;
- sharing, with its repository and hashes, and what sharing refuses;
- the notes dialog's commands, and a turn's instructions carrying the block.

In the browser pane, Lumi Cloud of that branch and an isolated app signed in
as Ada ran together, with a stub model recording its requests:

- Bob, from his own sign-in, proposed a note resting on the Makefile, and Ada
  approved it on the Library page.
- **Sync now** brought it to the app, and Project notes showed it under **From
  your team** as recalled. The fixture's Makefile had Windows line endings.
- "How do I run the tests?" gave the model the note with "by bob, approved by
  ada".
- Ada saved a note and chose **Share with the team**. The toast said it went
  to Acme for review, and the portal listed it as waiting.
- **Fixed along the way**: the Share button's title had replaced its visible
  text as its accessible name. The explanation is now a line in the dialog.

## September 25 team skills and prompts — source only, not released

- **Your organization's library** (`lumi/team_library.py`,
  [guide](team-library.md)): skills and prompts published and versioned in
  Lumi Cloud's **Library** (Luminary-Analytics/lumi-cloud#21).
  - Lumi syncs the latest versions when it starts, if its copy is more than
    15 minutes old, and from **Settings > Lumi account > Team library > Sync
    now**.
  - It keeps a copy in its state folder (`team/library.json`) and deletes it
    on sign-out.
- **Team skills** that match a request are listed for the agent next to pack
  skills (up to four), with their organization and version. `skill_view`
  reads `team:<organization>/<slug>`.
- **Team prompts**: a **❝** button beside the message box, shown once the
  organization has prompts.
  - It opens a searchable list; Enter inserts the first match into the
    message after anything typed, and arrow keys move through the list.
  - Nothing is sent until the person sends it.

Validation on September 25, 2026: `test_team_library.py` (2 tests) covers:

- syncing, and items that aren't valid being left out;
- matching (a trigger phrase outranks shared words; no match for an
  unrelated request);
- the skill list and `skill_view` for team skills;
- an archived item disappearing at the next sync, and a failed sync keeping
  the copy;
- the app's command syncing only when the copy is old or when asked;
- sign-out deleting the copy.

In the browser pane, Lumi Cloud of that branch and an isolated app signed in
as Ada ran together, with a stub model recording its requests:

- **Prompts.** The **❝** button appeared after the startup sync. It was
  reached with Tab, and the list opened with Enter. Typing "check" filtered it
  to "Review checklist", and Enter put that prompt in the message with focus
  back in the box.
- **Skills.** Sending "Cut the 2.0 release" gave the model Acme's "Cut a
  release (version 1)" in the skill list.
- **Updating.** After Ada published version 2 from the portal's form, **Sync
  now** brought it into the app's copy.
- **Layout.** The prompts dialog fit a 420-pixel window once its search field
  was made full width.

## September 25 hand-offs to a teammate or a CI run — source only, not released

- **Hand off…** in a conversation's menu (`lumi/handoff.py`,
  [guide](hand-offs.md)) passes the work on with its conversation (the Share
  copy: messages, replies and action lines, no tool results, secrets
  removed), a note, and where the work is. That's the repository's address
  without credentials, the branch and commit, and how much wasn't committed
  or pushed.
  - **To a teammate** through Lumi Cloud (Luminary-Analytics/lumi-cloud#20),
    which emails them.
  - **To a CI run** as `.lumi/handoffs/<name>.json` in the project, which
    `lumi run --handoff <file>` continues from. The task defaults to
    continuing it.
- **Hand-offs for you** appears under **New session** when work is waiting.
  - Each hand-off shows the note and where the work is, and suggests a recent
    project that is a clone of the repository.
  - It checks the chosen folder's branch and commit, and says what to do when
    they differ. Lumi never switches branches, fetches or pulls.
  - **Continue** keeps the hand-off in Lumi's state folder and starts a
    conversation there, with `@handoff:<id> Continue the work …` ready to
    review. **Dismiss** is the alternative.
- **`@handoff:` attaches a hand-off** (by id, or a file inside the project) as
  context framed as information, not instructions.
  - It is the context broker's first sticky attachment: pinned for the rest of
    the conversation.
  - When a conversation is reopened, the session attaches hand-offs mentioned
    in its history again (`ContextBroker.recall`).
- **Automatic titles** leave out `@provider:selector` attachments, so a
  continued hand-off or an `@file:` message gets a readable title.

Validation on September 25, 2026: `test_handoff.py` (8 tests) covers these
cases against a real temporary git repository:

- what a hand-off holds, and the redaction of a saved key, a GitHub token and
  a remote's credentials;
- the branch, commit, uncommitted and unpushed counts;
- the rendered context and its size limit;
- CI files, ids, and what loading refuses (outside the project, excluded, not
  a hand-off);
- the folder check: same commit, another branch, a missing commit, another
  repository and no repository;
- the sticky attachment, including after a session is rebuilt from history;
- `lumi run --handoff`;
- the app's commands with a fake Lumi Cloud.

`test_session_titles.py` covers titles without attachments.

In the browser pane, Lumi Cloud of that branch and an isolated app signed in
as Bob ran together, with a stub model on 127.0.0.1 recording its requests:

- **Receiving.** "1 hand-off for you" was reached with Tab and opened with
  Enter. The folder check said the folder was on main. After switching the
  fixture's branch it said the folder was "at the handed-off commit". Escape
  returned focus to the sidebar button.
- **Continuing.** Continue, pressed with Enter, opened a new conversation with
  the draft focused. Sending it gave the model the hand-off as a labeled
  attachment, and a follow-up without the mention still carried it.
- **Handing off.** Handing "Rate limit tweak" to Ada from the keyboard showed
  it waiting in her portal, with the note, the branch and "2 commits" not
  pushed.
- **To CI.** A CI hand-off saved `.lumi/handoffs/rate-limit-tweak-….json` and
  showed its `lumi run --handoff` command.
- **Layout.** The dialog fit a 420-pixel window.
- **Bug found and fixed.** The sidebar button's `display` rule had overridden
  `hidden`, leaving "0 hand-offs for you" in view.

## September 25 sharing a conversation — source only, not released

- **Share…** in a conversation's menu (`lumi/share.py`,
  [guide](lumi-cloud.md#sharing-a-conversation)) puts a read-only copy in the
  signed-in person's Lumi Cloud organization and shows its link, with **Copy
  link** and **Stop sharing**. Lumi Cloud shows it to the organization's
  members or, when an owner or admin allows it, to anyone with the link
  (Luminary-Analytics/lumi-cloud#19).
- The copy holds people's messages, Lumi's replies and a line for each
  action, marked when it failed. It never holds tool results. Saved keys and
  secret patterns are removed (`secret_scan.redact_text`), and the project
  appears by its folder's name. `CloudClient.account_call` reaches Lumi
  Cloud's API as the signed-in person. The links are remembered in
  `shares.json` in Lumi's state folder.
- The conversation menu is now buttons with menu roles: it opens with its
  first item focused, arrow keys move through it, Escape or Tab closes it and
  focus returns to the conversation. Enter or Space on a conversation's ⋯
  button opens the menu instead of the conversation.

Validation on September 25, 2026: `test_share.py` covers what the copy holds
(no tool output, failed and denied actions marked, a saved key and a GitHub
token removed, the folder's name only) and the dialog's commands with a fake
Lumi Cloud: status, sharing a conversation saved in another recent project,
stopping, a refusal and a conversation that isn't saved. Replay's lookup,
now shared with sharing, is checked on the same saved conversation.

A cross-check ran the real Lumi Cloud of that branch and these commands in
one process: a link share was refused until the owner allowed it; the
organization's link opened for its member with `no-store`, `no-referrer` and
`noindex` headers, sent a signed-out visitor to sign in, and showed "Nothing
is shared here" to someone outside the organization; tool output never
reached the page; stopping closed the link.

In the browser pane, with Lumi Cloud and an isolated app running together,
the conversation's menu was opened with the mouse and from the keyboard (Tab
to ⋯, Enter, arrows, Enter). The dialog refused a link share with the
organization's message, created the organization's link with Enter, showed it
again on reopening, fell back to selecting the link when the clipboard was
refused, closed with Escape with focus back on the ⋯ button, and stopped
sharing. The portal's page showed the conversation (failed action in red) in
dark and light, at phone width without sideways scrolling, and "Nothing is
shared here" after stopping. The dialog fit a 420-pixel window.

## September 25 worker handoffs report only what happened — source only, not released

**A worker's handoff listed files it never changed.** The handoff a delegated
worker (`task`, `task_batch`) returns named every file its `file_edit`,
`file_write` or `file_replace` calls asked for. An edit the user rejected, a
hook or policy blocked, that failed (`old_text` not found) or that a stop cut
off before it ran still reached the parent model, the agent registry and the
app as a changed file. The same handoff called every `check_run` without an
error "passed". So a check the user or a hook denied, which never ran, was
reported as passing, and Director Mode recorded it as passing validation.

- **Changed files come from results** (`lumi/engine/session.py`,
  `_execute_task`). A write call's path waits under its call id and counts
  only when that call's own result succeeds, as the audit log already did. A
  denial by the user or a hook isn't an error, so both `denied` and
  `is_error` are checked. A call with no result doesn't count. Files a
  successful result names itself, such as a Codex file change, now count
  too. A worktree's committed changes are still added.
- **A denied check is `not run`**, not "passed" (a policy block used to say
  "failed"). Director Mode records it as a validation that didn't pass, so it
  no longer satisfies the acceptance gate.
- **CLI tool calls without results** (`Session.run`): the branch for a CLI
  backend's `tool_call` events counted each call as a successful tool and a
  write's path as a changed file. No shipped backend reaches it: Claude Code
  sends only text, and Codex reports its tools as `external.tool`
  observations with results. The branch stays, so such a call never runs
  natively, but it no longer counts as evidence.

Validation on September 25, 2026:

- Full `pytest` with `main` merged in: 4,103 passed, 5 skipped. `ruff check .`
  clean, the UI node tests (`ui_recovery`, `appearance`, `autonomous_view`)
  46 passed, `git diff --check` clean.
- `test_worker_handoff.py` (10 tests) delegates through `Session.run` with a
  scripted backend. It reads the handoff from the `subagent.end` event, the
  parent's `task` result and the agent record:
  - an accepted edit is listed, and the file changed;
  - an edit rejected by the user, blocked by a hook or by policy, or failed
    is not listed, and the file is unchanged;
  - a worker stopped after its call, before the edit ran, lists nothing;
  - a Codex worker's file change counts only when its result succeeds;
  - a denied check is `not run`. In a Director run it's recorded as not
    passing, and the gate refuses the task.
- `test_session_text_branches.py`: a display-only CLI write is no changed
  file or successful tool.
- Against the previous `session.py`, 9 of the 11 new tests failed. The
  accepted edit and the failed Codex change pass on both.
- In the browser pane, before merging `main`, with an isolated home and a
  scripted Ollama-compatible model whose parent delegates an edit of
  `notes.txt` to a build worker, in Ask mode, allowing the task:
  - **Reject** left `notes.txt` unchanged. The handoff the page received in
    `subagent.end`, and the worker's agent record, listed no changed files.
    **Accept** changed the file and listed `notes.txt`.
  - On the previous `session.py`, the same **Reject** listed `notes.txt` in
    both.
  - This build has no element for the Agents panel that used to show the
    handoff (`agent-activity-tree`), so the handoff was read from the page's
    state. The conversation showed the worker's edit as denied.
  - The real `~/.resonant/settings.json` was unchanged, no `~/.lumi` was
    created, and no Lumi credential was stored. Settings > Connections was
    not opened.

Not exercised: a live model, a packaged build, a real Codex or Claude Code
worker, and `task_batch`, which runs each worker through the same code.

## September 25 repository allow rules — source only, not released

The trust banner, Settings and docs said a trusted repository's
`lumi-policy.json` `allow` rules skip approval, but the engine never did that.
It read a policy `allow` only as "not denied", as the policy design had since
April, so Auto-edit still asked. Now they skip Auto-edit's prompt:

- **Auto-edit and Plan** (`engine/session.py` `_repository_preapproves`,
  `engine/policies.py` `ExecutionPolicy.repository_allows`): a call the tier
  would ask about runs without asking when the policy as a whole allows it
  and the first matching rule in the project's own policy is `allow`. Rules
  now record where they came from (`PolicyRule.source`). So an organization
  `allow` that matches first neither skips the prompt itself nor hides the
  repository's answer.
- **Still decided first:** the guardrails, organization deny and ask rules,
  and Auto-edit's built-in denies. **Ask never skips** its prompt, and
  Full-auto doesn't ask anyway. Delegated workers inherit the same rules.
- **Chained commands still ask.** A rule's glob matches the whole command
  text, so `npm test*` also matched `npm test && …`. A `bash` or `check_run`
  command containing `;`, `&`, `|`, `<`, `>`, backquotes, `$(` or a line break
  isn't pre-approved, nor is a `job_start` or `preview_start` word containing
  one.
- **Unattended runs:** in `lumi run --trust-project --mode auto-edit`, and in a
  scheduled task set to **Edit files** on a trusted project, commands an allow
  rule matches now run instead of being refused.
- **Model comparisons** (`lumi/model_evals.py`) run the last commit with
  `--trust-project` when the project is trusted. They now also pass the new
  `lumi run --policy-digest`: the commit's allow rules apply only if its
  `lumi-policy.json` is the version the user trusted, and not at all while a
  change awaits review.
- **Trust binds to the file it read.** `TrustStatus.policy_digest` is the
  SHA-256 the trust check read. `project_execution_policy(policy_digest=…)`
  honors the allow rules only if the bytes it parses match that digest, so an
  edit made between the two reads doesn't slip through. A policy that isn't
  valid UTF-8 is now skipped with a warning, like invalid JSON.
- **Upgrades:** projects trusted on first run because they were in Recent
  projects keep their instructions, but a policy with allow rules waits for one
  review. Those rules never skipped a prompt before, so honoring them
  unreviewed would change what Lumi does there without asking.
- **Audit log:** such a call is an `approval` with `by: "project_policy"`.
- **Wording** (`static/app.js`, `static/settings_view.js`): "N rules that skip
  approval in Auto-edit". A policy that isn't the trusted version reads "You
  haven't reviewed this version of lumi-policy.json".
- Docs: [desktop workflow](desktop-workflow.md#project-trust-and-lumi-policyjson)
  (new section), [agent runtime](modern-agent-runtime.md#tool-approvals),
  [`lumi run`](headless.md), [scheduled tasks](scheduled-tasks.md),
  [model comparisons](model-comparisons.md),
  [organization policy](enterprise-policy.md) and [audit log](audit-log.md).

Validation on September 25, 2026:

- `tests/test_repository_allow_rules.py` (30 tests): each runs `Session.run`
  with a scripted model and an approval callback, and checks the folder the
  command creates. It covers the skip, what still asks (no match, chaining,
  untrusted), the repository's own rule order, guardrails and built-in denies,
  organization deny, ask and allow rules, Auto-edit versus Ask and suggest,
  unattended runs, delegated workers, the digest check and the audit record.
- GUI (`tests/test_gui_permission_modes.py`, through the real
  `_run_session_streaming` loop and `approve` handler): Auto-edit and Plan run
  a trusted rule's command without a prompt. Ask asks, and the Deny holds.
  Before trust and after a policy edit, Auto-edit asks.
- `tests/test_headless.py`: `lumi run` refuses the command without
  `--trust-project` or with a `--policy-digest` that doesn't match, and runs
  it with a matching digest or the flag alone.
- `tests/test_model_evals.py`: comparison runs pass no trust for an untrusted
  project, the trusted digest once it's trusted, and an empty digest after an
  unreviewed policy edit. `tests/test_exclusions_and_trust.py` covers the one
  review after upgrade and the reported digest.
- Nine mutants each switch off one part: the skip, the chaining check, the
  tier check, per-layer matching (twice), the digest, the upgrade review, and
  the digest in `lumi run` and in comparisons. Each fails at least one of
  these tests. The tier-check mutant also fails the GUI Ask case and
  `test_a_trusted_repositorys_allow_rules_do_not_skip_asks_approval` from the
  Ask change below.
- Full suite after merging main: 4092 passed, 5 skipped. Ruff, `node --check`
  and the node UI tests (42) pass.
- Browser, isolated fixture (temporary home and state, `LUMI_KEYCHAIN=off`, a
  scripted Ollama-compatible model, CLI adapters off). This ran before main
  (with the Ask change below) was merged into this branch:
  - The banner listed "lumi-policy.json with 2 rules that skip approval in
    Auto-edit".
  - Untrusted, `mkdir made` showed Command Review; after **Trust this
    project**, it ran with no dialog.
  - `mkdir other`, which no rule matches, still asked, and keyboard **Deny**
    kept it from running.
  - After an on-disk edit to the policy, the banner read "You haven't reviewed
    this version…" and the command asked again. **Trust the change**, reached
    with Shift+Tab, restored the skip.
  - Settings > Privacy & security > Project trust showed the new wording for a
    trusted project, the banner wrapped at 375 px, and the fixture's audit log
    recorded `project_policy` approvals.
  - Settings' text for a policy that isn't the trusted version was changed
    afterwards, and was checked by rendering `settings_view.js` in Node, not in
    the browser.

## September 25 code editors: VS Code and JetBrains IDEs — source only, not released

- **VS Code** ([guide](code-editors.md), `lumi/code_editors/vscode/`): Send
  Selection to Lumi, Ask Lumi About Selection, Send File to Lumi and Send
  Open Files to Lumi add them to the message in Lumi's composer as `@file:`
  attachments, and you send it from Lumi. Review Lumi's Changes opens each
  file the open session's latest change-making turn changed beside its
  version from before that turn. Also for Cursor, Windsurf and VSCodium. The
  extension is plain JavaScript without dependencies, so Lumi packs the .vsix
  itself (`lumi editor vscode`) and installs it with the editor's own command
  line from **Settings > Code editors**.
- **JetBrains IDEs**: External Tools that send the file or selection, ask
  about the selection and print Lumi's changes, added to each IDE found from
  Settings > Code editors or `lumi editor jetbrains --install`. `lumi editor
  status|send|changes|diff` work from a terminal too.
- **The editor bridge** (`lumi/gui/editor_bridge.py`): while Lumi runs, a
  token for that launch in `editor-bridge.json` in the state folder, readable
  only by you. It opens only `/api/editor/...`, refuses requests from web
  pages, takes only files inside the open project that aren't excluded, and
  can't send a message or start a turn. `security.editor_bridge` (**Settings
  > Privacy & security > Code editors**, lockable by policy) closes it.
- `@file:path#L10-24` attaches only those lines.
- **Fixed:** a tool call repeated with the same arguments in a later
  response or turn got no checkpoint, because call ids are unique only within
  one response. A hand edit between two identical writes could not be
  restored. Each response now starts afresh.

Validation on September 25, 2026, after merging main: full `pytest` 4,114 passed, 4 skipped;
`node --test` 54 passed, including `vscode_extension.test.cjs` (8 tests), which
runs the extension against a simulated VS Code API and a stand-in bridge.
`test_editor_bridge.py` (11) covers the token and file checks, line ranges,
and changes against git snapshots, snapshot archives and the last commit,
including line endings a checkout converted. `test_code_editors.py` (9)
covers the .vsix, the JetBrains tools and the command line against a local
server with a proxy set. The checkpoint fix has a test that fails without it.

In the browser pane, with an isolated fixture (a stub model, a fake `code`
command and a fake PyCharm settings folder, and no real editor on PATH):

- A turn changed `src/app.py`. `lumi editor changes --diff` against the
  running app printed the diff from the turn's snapshot.
- `lumi editor send src/app.py --lines 1-2 --text ...` put the question and
  `@file:src/app.py#L1-2` in the focused composer, with a toast. An excluded
  `.env` and a file outside the project were refused. Sending the message put
  exactly those two lines (46 characters) and `notes.txt` into the model's
  context.
- Settings > Code editors (found by searching "pycharm") ran the fake `code`
  with `--install-extension <temporary .vsix> --force`. It wrote
  `tools/Lumi.xml` into the fake PyCharm folder; that button was pressed from
  the keyboard.
- Turning Code editors off removed the bridge file, and the command line then
  refused. Turning it on brought the file back.
- The extension's own code ran against the running app with only the VS Code
  API simulated: its status message, Ask About Selection (the composer showed
  the question and `@file:src/app.py#L2-2`) and Review Lumi's Changes, with
  the earlier version fetched from the app.
- With a hand edit between two identical writes: before the fix the second
  turn had no checkpoint, and the review compared with the last commit. After
  it, there were two checkpoints, and the review's earlier version was the
  hand edit.

The packed .vsix installed with VS Code 1.125's `code --install-extension`
into a temporary extensions folder and user-data folder, where it was listed
as `luminary-analytics.lumi-vscode` 0.1.0. The extension was not run inside
VS Code, and no JetBrains IDE was run.

## September 25 tasks from Slack and Teams — source only, not released

- **Settings > Lumi account > Tasks from Slack and Teams**
  (`lumi/remote_tasks.py`, [guide](lumi-cloud.md#tasks-from-slack-and-teams)):
  on a computer enrolled with its person's own account, Lumi picks up the
  requests that person sends to Lumi in their organization's Slack or
  Microsoft Teams. Lumi Cloud relays these (Luminary-Analytics/lumi-cloud#18).
- Lumi checks every 20 seconds while it's open and runs requests one at a
  time in the chosen project and mode, with the default model, in a session
  built like `lumi run`'s. It asks in the chat, with Approve and Deny
  buttons, before actions the mode doesn't allow; no answer within 10 minutes
  or **stop** refuses them. The reply goes back to the chat.
- It is off until turned on, never runs on a managed computer, and an
  organization can lock `cloud.remote_tasks`. `CloudClient.device_call`
  reaches Lumi Cloud's device API with the device's token.

Validation on September 25, 2026: `test_remote_tasks.py` (6 tests) runs
against the fake Lumi Cloud of `test_cloud.py` extended with the task
endpoints:

- a real engine session in Ask mode writes a file only after the approval;
- denials, an unanswered approval and a stop from the chat;
- failures that still reach the chat;
- what keeps it from running;
- the Settings command, including an organization's lock.

A cross-check ran the real Lumi Cloud of that branch and this app together in
one process, with Slack faked at the HTTP layer. The app signed in through
the consent page and enrolled. A Slack message was queued, claimed ("Working
on it on <computer>."), asked about with buttons, approved and done. The file
was written and the reply posted. No real Slack or Teams was used.

In the browser pane, with an isolated fixture enrolled in a Lumi Cloud that
doesn't answer, Settings > Lumi account showed the section (found by
searching "slack"), with the open project filled in. The switch, pressed from
the keyboard, saved it on ("On. Checks for your requests every 20 seconds
while Lumi is open."). A folder that doesn't exist was refused with the
message, and the typed folder stayed in the field.

## September 25 issue trackers: Jira, Linear, GitHub and GitLab — source only, not released

- **Start from an issue** ([guide](issue-trackers.md),
  `lumi/engine/issue_trackers.py`): `@issue:ENG-12` attaches an issue to a
  message, and the `issue_view` tool reads one. The agent gets the title,
  state, assignee, labels, description and latest comments, presented as the
  issue's content rather than instructions.
- **Link back**: `issue_comment` comments on it (Ask and Auto-edit ask first).
- Issues are named by link, by `jira:`, `linear:`, `github:` or `gitlab:`
  and a key, by `#34` for this repository, or by a bare key when only Jira or
  only Linear is set up.
- **Settings > Issue trackers**: Jira Cloud (site, email, API token, REST v3
  with Atlassian documents), Jira Server or Data Center (a personal access
  token, REST v2), and Linear (an API key, GraphQL). GitHub and GitLab issues
  use the pull request tools' tokens. `JIRA_URL`, `JIRA_EMAIL`,
  `JIRA_API_TOKEN` and `LINEAR_API_KEY` work too.

Validation on September 25, 2026: `test_issue_trackers.py` (6 tests) runs
against mocked APIs. It covers naming by link, prefix, `#34` for GitHub and
GitLab origins, and bare keys with one, both or neither tracker set up. It
covers Atlassian documents both ways, and Jira Cloud and Server, including
their authentication headers and comment bodies. It covers Linear's query and
mutation, GitHub and GitLab issues and comments (without GitLab's system
notes), `@issue:` attachments, including a failure, and `issue_view` being
read-only. Full `pytest`: 4,142 passed, 4 skipped. In the browser pane, Settings >
Issue trackers saved a Jira site typed there. No real tracker was called.

## September 25 changed files count only edits that happened — source only, not released

- A task's **Changed files** and the "Review these changes" next-prompt
  suggestion count a file change only when that call's own result succeeds,
  matched by call id (`lumi/gui/static/app.js`). They used to count the
  model's `file_edit` or `file_write` call, so an edit the user rejected, a
  policy blocked, that failed (`old_text` not found) or that a cancel stopped
  before it ran still counted: the suggestion offered to review changes that
  never happened, and a card finished without the server's evidence (a turn
  ending in an error, or a replayed interrupted turn) listed the file.
- Replay rebuilds the list from the saved results the same way. A Codex file
  change counts when its result succeeds. A worker's tool events never count
  for the parent turn and can't complete a parent call that has the same id.
  The line and diff counts shown beside each file are unchanged.
- A worker's handoff is built by the engine, which now also lists only
  changes whose results succeeded ("worker handoffs report only what
  happened", above).

Validation on September 25, 2026:

- With `main` merged in: full `pytest` 4,054 passed, 5 skipped (before
  "untrusted text in the Git panel" landed); `ruff check` clean; the UI node
  tests (`ui_recovery`, `appearance`, `autonomous_view`) 46 passed.
- Three new tests drive the real `handleToolCall`, `handleToolResult` and
  `replayDisplayEvents` with rejected, policy-blocked, failed, unanswered,
  accepted, id-less, Codex and worker events. All three fail against the
  previous `app.js`, and they also catch counting a worker's result for the
  parent or ignoring `denied`.
- In the browser pane, with an isolated home and a scripted
  Ollama-compatible model:
  - Before the change, Ask still refused edits by policy, so Auto-edit with
    a project `lumi-policy.json` `prompt` rule for `file_edit` gave the
    Accept/Reject card. Reject left `notes.txt` unchanged, yet the next
    prompt suggested reviewing the changes, and after a provider error the
    Failed card listed `notes.txt` under **Changed files**.
  - After the change, the same setup, and an Ask policy block, listed no
    changed files and suggested no review.
  - Merged with "Ask asks before changes" below, in Ask mode: Reject, and
    Reject followed by a provider error, listed no changed files and
    suggested no review. Accept changed the file, listed `notes.txt` and
    suggested reviewing it. After a reload, only the accepted turn listed a
    file.

## September 25 chat gateway: approvals in the chat, and Slack — source only, not released

- **Approvals in the chat** (`lumi gateway`, [guide](chat-gateway.md)): the
  gateway used to run every chat's requests with nothing asked. It now takes
  `--mode ask` (the default), `auto-edit` or `bypass`. In Ask and Auto-edit
  modes an action the mode doesn't allow is sent to the chat with **Approve**
  and **Deny** buttons, and it runs only if approved. Nobody answering within
  `--approval-minutes` (10) refuses it. **stop** stops the running request,
  and **status** shows the project, mode, model and what's running. Both
  work while a request runs, and so do approve and deny. Commands work with
  or without the slash, since Slack keeps the slash for its own.
- **Sessions built like `lumi run`'s** (`lumi/headless.py`): a project
  (`--project`, `gateway.project`, or the current folder), its trust, file
  exclusions, the guardrails and shell sandbox, and the organization's modes
  and models. Before, gateway sessions had none of these. The gateway never
  trusts a project itself. Any provider `lumi run` supports works
  (`--backend`, `--model`).
- **Slack** over Socket Mode (`lumi/gateway/slack.py`), so no public address
  is needed. Direct messages go to the agent, and in channels the messages
  that mention the app. `gateway.slack_allowed` takes channel IDs (everyone
  in the channel) or user IDs (that person anywhere). The buttons are checked
  against the same list. Tokens go in settings.json's `api_keys`
  (`slack_bot`, `slack_app`; `telegram_bot` as before) or in
  `SLACK_BOT_TOKEN`, `SLACK_APP_TOKEN` and, new, `TELEGRAM_BOT_TOKEN`. Like
  the rest of the gateway's settings, they stay out of the Settings page.
- Saved key values are removed from approval requests before they're sent.
  `gateway.telegram_api_url` can point at a self-hosted Bot API server.
- Microsoft Teams needs a public HTTPS address, so it is left to Lumi Cloud.

Validation on September 25, 2026: full `pytest` 4,136 passed, 4 skipped;
`test_chat_gateway.py` (11 tests) covers
approve, deny, stop, status and queued requests on the gateway's two threads,
and unanswered approvals. It covers the Telegram adapter's buttons and
allowlist, and the Slack adapter's events, mentions, bot and edited messages,
buttons, acknowledgements and reconnecting, all against mock transports. It
checks a real engine session in Ask mode writing a file only after approval,
and the organization's allowed modes. The real `lumi gateway` process, run with
an isolated home against a stand-in Telegram Bot API and a stub model:

- asked "Lumi wants to write notes.txt (14 characters)." with Approve and Deny
  buttons;
- wrote nothing until Approve was pressed, then wrote the file and replied;
- answered status with the project, mode, model and "Idle.";
- refused a chat that wasn't allowed.

No real Telegram or Slack account was used.

## September 25 untrusted text in the Git panel, tool rows and plan graph — source only, not released

The escaping fix below (quotes) covered `escapeHtml`, but some views never
called it or used an escaper of their own:

- **Git panel:** the branch, changed file names, commit hashes and commit
  messages came from the repository and went into the page as HTML. A cloned
  repository's commit message could therefore run script in the app window,
  which holds the app's authenticated connection. All four are escaped now.
- **Tool rows:** a tool name the app doesn't know went into the row as HTML;
  so did desktop click and scroll arguments. MCP servers name their own tools,
  and a model chooses tool arguments. The CLI providers' activity rows showed
  raw tool names the same way. These are escaped now.
- Finding a tool's row by its name built a CSS selector from that name. A
  quote in the name threw an exception, and the row kept showing "running".
  These selectors use `CSS.escape` now.
- **Plan graph:** its own escaper left quotes, so a model-written goal could
  leave its `title` attribute. It escapes quotes now.
- Also escaped: hook names and commands under **Settings > Hooks** (commands
  with `<`, `>` or `&` also displayed wrongly), terminal call ids, the
  screenshot viewer's source, command palette ids and the backend label.

Validation on September 25, 2026:

- Three new tests in `ui_recovery.test.cjs` failed on main before the fix,
  one for each area: the Git panel (plus hooks), tool rows (native and CLI),
  and the plan graph. The plan graph test uses a text serializer that behaves
  like a browser's. After merging main, all 43 tests in the three Node test
  files pass, as do `ruff` and `git diff --check`. The full `pytest` run had
  4,054 passed and 5 skipped; the vendored-asset test skips when the
  gitignored assets are absent.
- In the browser pane, with an isolated home and a stub model: the fixture
  repository's last commit message was
  `x" onmouseover="window.pwned=1"><img src=x onerror="window.pwned=1">`, and
  the stub model called a tool with the same name.
  - The tool row showed the name as text. Its failure status arrived through
    the escaped selector.
  - The Git panel, opened from the command palette, showed the commit
    message and a changed file named `notes & 'quotes'.txt` as text.
  - Injecting the replaced commit template into the same page ran the
    image's `onerror` handler; the fixed renderer did not.
  - The real home and credential store were unchanged.
- Windows cannot create branch or file names containing `"`, `<` or `>`, so
  those Git cases are covered only by the unit test. So are the hooks list and
  the plan graph.

## September 25 Ask asks before changes — source only, not released

**Ask never asked about writes or shell commands.** The composer describes Ask
as "Always ask before making changes". But the app ran it on the read-only
`suggest` tier, whose built-in policy denies `file_write`, `file_edit`, `bash`
and `batch`. A deny is final, so no approval appeared: a model's `file_edit`
came back "Blocked by policy: Write operations blocked in suggest mode". Other
actions, such as `check_run`, did ask. The defect dates back at least to
commit 3cd7922.

- **Ask has its own `ask` tier** (`lumi/engine/policies.py`,
  `default_ask_policy`). Reads run. File writes, edits, shell commands and
  every other action show the approval prompt, and the answer decides.
  **Deny** stays final.
- **Still refused without asking:** a recursive `rm`, `chmod` on a system
  path, a download piped into a shell, and the guardrails. These are checked
  before a repository's `lumi-policy.json`, and organization shell rules still
  come first.
- **Nothing can switch Ask's approvals off.** Neither a repository's nor an
  organization's `allow` rule skips the prompt, because the tier asks before
  anything that isn't read-only. A repository can still forbid a change
  outright.
- **Delegated workers** inherit Ask and ask through the conversation's prompt.
  Work with no approval dialog, such as background sprint roles, still skips
  changes. As in Auto-edit, a PERMISSION_REQUEST hook can explicitly allow
  them.
- **Unchanged:** `lumi run --mode ask`, and so scheduled tasks and model
  comparisons set to **Read only (ask)**, keep the read-only `suggest` tier,
  since nobody can answer a prompt there. Codex and Claude Code still only
  read under Ask; they can't pass an approval request to Lumi.
- **The edit card is visible while the run waits** (`static/app.js`). Before a
  file edit or new file, the approval card with its diff went into the running
  task's activity list. The UI hides that list until the live status is
  opened, so the turn waited on a card nobody could see. The card now goes in
  the conversation, like an `await_user` question, including for a worker's
  edit. The dialog for commands was already visible.
- **Settings > General > Default permission mode** calls the mode **Ask
  permissions (ask before every change)** instead of "Suggest (read-only)".

Validation on September 25, 2026:

- Full `pytest` after merging main: 4,038 passed, 5 skipped. `ruff check .`
  clean, 36 Node UI tests pass, `git diff --check` clean.
- New tests drive real turns:
  - `test_permission_decisions.py` runs `Session.run` with an `on_permission`
    callback. It covers allow and deny for a new file, an edit and a command,
    reads without asking, a refused recursive `rm`, no prompt (fails closed),
    a repository's `allow` not skipping the prompt, `suggest` staying read-only,
    a worker asking through the parent, and the policy layer order.
  - `test_gui_permission_modes.py` runs the GUI's own run loop and `approve`
    handler, for allow and deny in Ask and a trusted repository's `allow *`.
  - `ui_recovery.test.cjs` checks the card's placement.
- On the unfixed code, 11 of the new and updated cases failed; every GUI case
  failed because no prompt appeared. Mutations were caught:
  - dropping the dangerous-command denies from Ask failed 5 tests;
  - letting the ask tier approve file writes failed 5;
  - the old card placement failed the UI test.
- In the browser pane, from an isolated home with the scripted Ollama stub,
  after choosing **Ask permissions** in the composer's mode menu:
  - a `file_edit` showed its card in the conversation. **Reject** left the
    file unchanged, and the model read "Tool execution denied by user.";
    **Accept** changed it;
  - `echo ran > ran.txt` opened the command dialog. **Escape** denied it (no
    file) and **Allow** ran it;
  - `rm -rf build` was refused without a prompt, and `build/` was intact;
  - a delegated worker's `file_write` asked through the conversation. Its card
    showed in the conversation, not inside the worker's block, and **Accept**
    wrote the file;
  - at 375 px the card fit with no horizontal scroll. **Shift+Tab** from the
    composer reached **Accept**, then **Reject**, with a visible focus ring;
    **Enter** on **Reject** left the file unchanged;
  - Settings listed **Ask permissions (ask before every change)**.

Not exercised: a live model, a packaged build, Codex or Claude Code, macOS and
Linux.

## September 25 pull requests on GitLab, Bitbucket and Azure DevOps — source only, not released

- The pull request tools (`github_pr_view`, `github_check_log`,
  `github_pr_create`, `github_pr_comment`, `github_pr_update`) now also work
  on **GitLab** merge requests (cloud or self-managed), **Bitbucket Cloud**
  and **Azure DevOps**, chosen by the `origin` remote
  (`lumi/engine/code_hosts.py`, [guide](github.md#other-hosts)): reviews and
  approvals, discussions and threads, CI jobs, steps and builds and their
  logs, opening, commenting, replying and updating. They keep their names,
  so policies and permission rules that name them cover every host.
- Tokens: **GitLab token**, **Bitbucket token** and **Azure DevOps token** in
  Settings > API keys, or `GITLAB_TOKEN`, `BITBUCKET_TOKEN`,
  `AZURE_DEVOPS_TOKEN` (and `SYSTEM_ACCESSTOKEN` in Azure Pipelines).

Validation on September 25, 2026: full `pytest` 4,031 passed, 4 skipped;
`test_code_hosts.py` (16 tests) against
mocked APIs covers remote URLs of each host and every tool on each host,
including authentication headers (PRIVATE-TOKEN; Basic for app passwords and
PATs; Bearer for access and job tokens), the encoded project paths, draft
handling and replies. `test_github_tools.py` still passes, with a GitLab
remote now taking the GitLab path. In the browser pane, searching Settings for
"gitlab" showed the GitHub, GitLab, Bitbucket and Azure DevOps token fields,
masked. No real host was called.

## September 25 comparing models on your own tasks — source only, not released

- **Settings > Model evaluations > Compare models on your tasks**
  ([guide](model-comparisons.md), `lumi/model_evals.py`): up to 20 tasks,
  each a prompt and a check command, run once per model (two to six) as an
  unattended `lumi run` in a detached git worktree of the project's last
  commit. The check decides pass or fail. The page shows passes, cost of
  priced requests and median time per model, and each run's status, cost,
  changed files and check output; diffs are kept. Runs never touch your
  checkout, and one comparison runs at a time, with **Stop**.
- **Fixed:** the app's HTML escaping didn't escape quotes, so text with a
  double quote put in an attribute (a form field's saved value, a button's
  label) was cut short or could add attributes. It escapes `"` and `'` now.

Validation on September 25, 2026:

- Full `pytest`: 4,015 passed, 4 skipped. `test_model_evals.py` (5 tests)
  with a real temporary git repository and a
  fake `lumi run`: validation (including the guardrails and policy), each
  model running each task in its own worktree with the checkout untouched
  and no worktree left, results and summary, stopping a run, an interrupted
  comparison, and the Settings commands. A new escaping test in
  `ui_recovery.test.cjs`.
- In the browser pane, with an isolated home and the stub model listing two
  models, one of which writes the file the check looks for: the form (with
  **Add task** and **Remove task**) created a comparison; **Run** showed
  progress as each run finished, then `stub:latest` passed 1/1 and
  `stub-b:latest` 0/1 with their check output; the project had no changes
  or leftover worktrees. The first attempt stored the check as `python -c`:
  re-rendering the form cut the quoted value, which is the escaping fix
  above.

## September 25 autonomous sessions: spending limit and an opt-in — source only, not released

- **Spending limit** ([guide](autonomous-sessions.md#the-spending-limit)):
  the launch card offers none, $5, $25 or $100. The mission stops
  (`spend_limit_reached`) once its priced model requests, iterations and
  reflect passes alike, reach it. It's checked before each iteration and at
  every heartbeat while one runs, which cancels the running iteration. The
  roadmap keeps `**Spending limit:**` and `**Spent so far:**`, so a mission
  resumed after a restart counts on.
- **Opt-in.** The v0.6.8 layout hid autonomous sessions entirely. They're now
  experimental and off by default: **Settings > General > Autonomous
  sessions (experimental)** shows an **∞** button in the message box. The
  AI Employee task button stays hidden.
- **Fixed:** the launch card required checkbox criteria (``- [ ] `[bash]` ``)
  while the spec prompt asks for ``- `[bash]` ``, so a spec written as asked
  never showed **Build autonomously**. Both forms are accepted on both sides
  now.
- Stop reasons read as words ("spending limit reached", "time budget used
  up") instead of codes.

Validation on September 25, 2026:

- `test_mission_spend.py` (14 tests): the roadmap fields, parsing limits, a
  stop before the next iteration, a running iteration cancelled at the limit,
  no limit, the tracker counting requests between iterations, starting and
  resuming with the spend carried over, and both criteria forms.
  `tests/autonomous_view.test.cjs` (4 tests): the launch card's spec check
  and stop-reason wording. The existing mission suites pass (282 tests),
  and the full `pytest` run: 3,995 passed, 4 skipped.
- In the browser pane, with an isolated home and the stub model: the switch
  in Settings saved and showed **∞**; the composer started a session; the
  stub's spec in the prompt's own format showed the launch card (before the
  fix it showed "no typed criteria"); choosing $25 read "stops at $25", and
  **Build autonomously** started a session whose banner read "spending
  limit: $25.00" and whose roadmap recorded the limit and "$0.00" spent
  (the stub is free). The session then paused as "stuck" because the stub
  can't build anything. Stop's confirmation was answered by a stubbed
  `window.confirm`. A real priced model wasn't used, so the limit was
  reached only in tests.

## September 25 code intelligence — source only, not released

- **`code_intel`** ([guide](code-intelligence.md), `lumi/engine/lsp.py`):
  the agent asks a language server for a symbol's definition, its
  references, its type and documentation, a file's diagnostics, or a file's
  outline. Lumi starts the server named in Settings' `lsp_servers` for the
  file's type, or a well-known one found on PATH (Pyright, pylsp,
  typescript-language-server, rust-analyzer, gopls, clangd, csharp-ls,
  OmniSharp, jdtls, lua-language-server), keeps it per project and stops it
  after 10 idle minutes or when Lumi exits.
- Servers start only in trusted projects, get the children's environment,
  pass the guardrails and run in the shell sandbox when it's on. Answers
  leave out excluded files, and a server that reports nothing isn't taken
  to mean a file is clean.
- **Project trust** can now be given to any project in Settings, not only
  one that brings instructions, since trust also lets Lumi run the project's
  code (language servers, automatic lint and tests).
- The status popover's LSP tab lists the servers Lumi would use and which
  are running or failed. `lumi run` passes Settings to its tools, as the app
  does, so it uses `lsp_servers` too.

Validation on September 25, 2026:

- Full `pytest`: 3,996 passed, 4 skipped. `test_lsp.py` (15 tests) against
  `tests/fake_lsp_server.py`: settings
  entries, choosing a server, UTF-16 positions, `file:///c%3A/` URIs,
  definition, references (with excluded files left out), hover, symbols,
  pushed and pulled diagnostics following file changes, a silent server,
  the trust requirement, a server that fails to start and isn't restarted
  at once, idle stopping and restarting, the session's path checks, process
  cleanup and the inventory.
- In the browser pane, with an isolated home, the stub model calling the tool
  and the fake server configured in `lsp_servers`: before trust the call was
  refused; **Trust this project** appeared for the plain project and trusted
  it; then references showed "5 found" with the list, and diagnostics
  "1 warning"; the LSP inventory showed the server running; after the app
  stopped, no server process was left. No real language server was run.

## September 25 scheduled tasks — source only, not released

- **Settings > Scheduled tasks** and `lumi schedule`
  ([guide](scheduled-tasks.md)): a saved prompt, project, model, permission
  mode and time limit that runs at set times through Task Scheduler,
  launchd or cron, so the app needn't be open. Each run is an unattended
  `lumi run`, so policy, budgets, exclusions, the audit log and the sandboxes
  apply, and a schedule never trusts a repository itself.
- Each run's result (status, answer, changed files, error, cost and the
  full `lumi run` summary) is kept, the last 30 per schedule. Settings shows
  the last run and its answer, and **Run now** starts a run in the
  background. A schedule never runs twice at once.
- `security.scheduled_tasks` (**Settings > Privacy & security**, lockable by
  policy) turns the feature off; a run the operating system still starts is
  refused and recorded. A schedule can't use a permission mode the policy
  doesn't allow.

Validation on September 25, 2026:

- Full `pytest`: 3,981 passed, 4 skipped. `test_schedules.py` (17 tests):
  validation, registering on save, unregistering on pause and removal, a refused registration saving nothing,
  the `lumi run` arguments, kept and pruned results, a real `lumi run`
  without a model recording why it failed, the running claim and its
  hand-over from **Run now**, the Task Scheduler, crontab and LaunchAgent
  entries (commands mocked), the command line, the Settings commands, the
  switch turning everything but pausing and removing off, and the policy's
  permission modes. No real scheduled task was registered.
- In the browser pane, with an isolated home, the stub model and a logging
  stand-in for Task Scheduler: a save with a missing folder showed the error
  and kept what was typed; a save registered the schedule; **Run now**
  (mouse and keyboard) started a real `lumi schedule run` process that
  completed against the stub, and the page showed "Running now", then the
  result and its answer, without losing keyboard focus; pause, resume and
  remove updated the entry and deleted the results. Turning **Scheduled
  tasks** off in Privacy & security showed a notice on the page and refused
  an add, keeping what was typed. Checked in the dark and light themes. The
  remove confirmation was answered by a stubbed `window.confirm`, since the
  pane can't press a native dialog's buttons.

## September 25 zero data retention — source only, not released

- **Connections that keep no data**: a custom connection can be marked
  **This endpoint keeps no prompts or responses** (`zero_retention`), shown
  as a "Zero retention" badge.
- **Policies can require it** (`models.require_zero_retention`,
  [organization policy](enterprise-policy.md)): only local Ollama and EXO
  models (Ollama's `-cloud` models run on ollama.com, so they don't count),
  marked connections, and providers the policy names in
  `models.zero_retention_providers` stay in the model menu; others are
  refused. The check is part of `Policy.model_allowed`, so every place that
  already checks models enforces it.

Validation on September 25, 2026:

- Full `pytest`: 3,963 passed, 4 skipped. `test_zero_retention.py`: the
  flag, each kind of provider under the requirement, Ollama cloud models,
  other rules still applying, invalid fields, an unreadable connection.
- In the browser pane, a custom connection saved with the box ticked showed
  the "Zero retention" badge and was stored with `zero_retention: true`.
  The first try stored `false`: the form's save payload lists its fields and
  didn't include the new one, which this check caught and which is fixed.

## September 25 an organization's shared model credit — source only, not released

- **Shared credit from Lumi Cloud** (`lumi/budgets.py`,
  [guide](usage-and-costs.md#an-organizations-shared-credit)): a check-in's
  answer can carry the organization's monthly credit and its spend so far.
  Lumi turns it into an `organization` budget that stops model requests
  when the month's spend across every computer, plus this computer's spend
  since the check-in, reaches the credit.
- It's kept across restarts, removed when a check-in no longer carries it,
  and ignored once its month has passed. Usage & cost lists it with the
  other budgets.

Validation on September 25, 2026: full `pytest` 3,960 passed, 4 skipped;
`test_cloud.py` (a check-in setting the
credit, a stop at $51 of $50 with this computer's spend since the check-in,
surviving a restart, removal, and last month's credit) passed with the
budget tests.

## September 25 cost per verified task and activity counts — source only, not released

- **Turn outcomes on this computer** (`lumi/activity.py`). Each finished
  turn, in the app or `lumi run`, adds a line to `~/.lumi/activity.jsonl`:
  its outcome (finished, error or stopped), whether a check the agent ran
  (`check_run`) passed, and how many files it changed. App crashes are
  counted too. No prompts, answers, file names or paths; lines are kept 90
  days.
- **Settings > Usage & cost** shows this month's tasks, verified tasks and
  the cost per verified task ([guide](usage-and-costs.md#cost-per-verified-task)).
- **Lumi Cloud check-ins** carry the same counts since the last check-in,
  for an organization's fleet health and adoption pages
  ([what a check-in sends](lumi-cloud.md)).

Validation on September 25, 2026:

- Full `pytest`: 3,959 passed, 4 skipped. `ruff check .` clean.
- `test_activity.py`: classifying turns (a failed or denied check verifies
  nothing), counts for a period, half-open windows, pruning old and broken
  lines, and counting crashes before the usual handlers.
- `test_cloud.py`: a check-in carries the counts, no content, and nothing
  twice.
- In the browser pane, from an isolated home with the scripted Ollama stub:
  a turn whose `check_run` passed and a turn with a plain command were
  recorded as two lines with no content, and Usage & cost showed 2 tasks,
  2 finished, 1 verified, and $0.00 per verified task (the stub is free).

## September 25 shell sandbox and command guardrails — source only, not released

See [shell sandbox and command guardrails](shell-sandbox.md).

- **Guardrails, always on** (`lumi/engine/guardrails.py`). A short list of
  commands is never run, in any permission mode:
  - deleting the whole file system, a drive or the home folder (`rm -rf /`,
    `rm -rf ~`, `Remove-Item -Recurse C:\`, `rd /s /q C:\`);
  - `chmod -R`/`chown -R` on `/`, `mkfs`, `format C:`, `diskpart`,
    `dd` onto a disk, a fork bomb;
  - shutting down or restarting.

  They're deny rules ahead of every tier's rules and ahead of organization
  and repository `allow` rules. The irreversibility floor checks them again
  before a command, check, job or preview starts, so a hook's rewrite or a
  session without an execution policy can't get past them.
- **The shell sandbox, off by default** (`lumi/engine/os_sandbox.py`,
  `security.shell_sandbox`: `"off"` or `"project"`). When it's on, the agent's
  commands, checks, jobs and previews can write only to the project, the
  temporary folders and terminal devices. Git's own folder stays read-only,
  because hooks written there would run outside the sandbox.
  - macOS uses `sandbox-exec` with a Seatbelt profile.
  - Linux uses bubblewrap.
  - Windows has no sandbox yet. With the setting on, commands are refused
    rather than run unprotected.
  - Settings > Privacy & security says whether a sandbox can run here. The
    check runs in the background at startup, so Settings never waits for it.
  - An organization can require the sandbox. A policy with any other value
    is invalid.
- **`lumi run` keeps file tools inside the project.** Headless sessions had
  no path sandbox; they now get the one the app uses.

Validation on September 25, 2026:

- Full `pytest` on Windows: 3,955 passed, 4 skipped (the live sandbox test
  skips on Windows). `ruff check .` clean.
- `test_guardrails.py` (29 commands that are refused, 26 everyday ones that
  aren't; every tier; repository and organization `allow` rules; the check
  before commands, jobs and previews start; nothing reaches a shell).
- `test_os_sandbox.py`: the setting and its values, Settings never waiting
  for the check, refusing commands, checks, jobs and previews when the
  sandbox can't run, the bubblewrap arguments and the Seatbelt profile, and
  a live test.
- The live test passed in CI (`sandbox.yml`) on macOS with Seatbelt and on
  Ubuntu with bubblewrap. Writing in the project and the temporary folder
  worked; writing in the home folder and in `.git` failed; listing the home
  folder worked. The first CI run failed because the test wrote to the
  suite's isolated home, which is under the temporary folder.
- `test_headless.py`: a headless session refuses a file write outside the
  project. `test_policy.py`: a policy with another sandbox value is invalid.
- In the browser pane, from an isolated home with the scripted Ollama stub:
  - with the sandbox off, `echo hello from lumi` ran (exit 0);
  - Settings > Privacy & security > Shell sandbox said "No sandbox can run
    here: Lumi has no shell sandbox on Windows yet." Choosing **Only the
    project and temporary folders** with the keyboard saved
    `security.shell_sandbox: "project"`, and the note added that the agent
    can't run commands;
  - the next command was refused: "The shell sandbox is on … No command was
    executed.";
  - in Full-auto, `mkfs.ext4 /dev/sda1` was refused before running: "Blocked
    by policy: Formatting a disk is never allowed, in any permission mode."

## September 25 large repositories — source only, not released

- **The codebase index follows `.gitignore`** ([guide](desktop-workflow.md#large-repositories)).
  In a Git repository, or a folder inside one, the file list comes from
  `git ls-files`; elsewhere the folder is walked as before. A repository in
  the home folder itself (dotfiles) isn't used for projects under it.
- **A cap of 100,000 files.** The index logs when it stops there.
- **Indexing does less work per file:**
  - each changed file is read once, on eight threads, and parsed once;
  - a missing tree-sitter is looked up once instead of once per file;
  - search reuses each file's lowercased fields;
  - the cache is compact JSON, replaced atomically, and written only when
    something changed.
- **The repo map scales.** An import counts for the files matching its most
  specific path suffix, and for none when more than three match. Before,
  one import credited every file sharing any suffix, such as all of a
  monorepo's `index.ts`. That took about a minute for 100,000 files.
- `scripts/benchmark_index.py` builds a synthetic repository and measures
  all of this.

Validation on September 25, 2026:

- Full `pytest`: 3,883 passed, 3 skipped.
- 10 tests in `test_rag_large_repos.py`:
  - `.gitignore` through Git, in the repository and in a folder inside it;
  - no repository in the home folder, the walk outside Git and the cap;
  - one read and one parse per changed file, and none when nothing changed;
  - the cache left alone when nothing changed;
  - the tree-sitter lookup, the cached search fields and import matching.
- Benchmarks on Windows (this machine's antivirus scans each newly written
  file on its first read):

| Repository | First index | Nothing changed | 1% changed | Search, median | Repo map | Cache |
| --- | --- | --- | --- | --- | --- | --- |
| 20,000 files plus 20,000 ignored, before | 213 s (ignored files included) | | | 93 ms | 1.9 s | 20 MB |
| The same, after | 15 s | 0.5 s | 0.8 s | 30 ms | 0.1 s | 9.8 MB |
| 100,002 files, after (capped at 100,000) | 76 s | 2.5 s | 4.0 s | 187 ms | 0.9 s | 49 MB |

  At 100,000 files the repo map took 59 s before the import change. The
  slowest search there took 691 ms, and the cache loaded in 0.5 s.

## September 25 Lumi account and Lumi Cloud enrollment — source only, not released

- **Settings > Lumi account** signs the app in to an organization's Lumi Cloud
  ([guide](lumi-cloud.md), `lumi/cloud.py`).
  - It follows OAuth 2.0 for native apps: the browser opens, the person
    approves, and the answer returns to a one-time listener on `127.0.0.1`,
    checked with PKCE and a state value.
  - The refresh token is kept in the credential store (`api_keys`), and the
    access token only in memory.
  - The page lists the person's organizations, roles and seats. **Sign out**
    also ends the sign-in on Lumi Cloud.
- **Use on this computer** enrolls the computer in an organization where
  the person has a seat.
  - Lumi generates an Ed25519 key. The private half stays in the credential
    store, and the device signs short assertions to get device tokens.
  - A machine policy with a `cloud` section enrolls managed computers with an
    administrator's enrollment token instead
    ([policy](enterprise-policy.md#lumi-cloud)).
- **Check-ins** run hourly in the background.
  - They send the version, the policy in force, and usage totals per model
    since the last check-in; never prompts, code or paths.
  - A new or nearly expired organization policy is downloaded, verified and
    applied. Right away, the app confirms which version is in force.
  - A revoked computer forgets its enrollment and the downloaded policy.
- **Applying the organization's policy** (`lumi/policy.py`).
  - A downloaded policy counts only when its signature verifies: against the
    keys an administrator set (with a machine policy), or the keys pinned when
    the person joined (without one).
  - With a machine policy, the cloud policy replaces its rules once it
    verifies; the machine rules apply before that and when a download fails.
  - An organization joined in the app never overrides a machine policy.
  - The running app applies a new policy at once: services, permission mode
    and file exclusions. Settings and the chat follow without a reload.
- Help text on the About page now points organizations to Lumi account.

Validation on September 25, 2026:

- `test_cloud.py`, 17 tests against a fake Lumi Cloud. They cover:
  - sign-in through the real loopback listener;
  - a callback with the wrong state, cancelling, and https addresses;
  - refresh rotation, and an ended sign-in signing out;
  - joining an organization and applying its signed policy, with the
    version confirmed at once;
  - usage totals without paths, and no seat meaning no enrollment;
  - a tampered download not applied, and a revoked computer forgetting its
    enrollment;
  - leaving on this computer;
  - managed enrollment from a machine policy, and a download signed by an
    untrusted key leaving the machine policy in force;
  - the page's status never carrying secrets.
- Full `pytest`: 3,873 passed, 3 skipped.
- End to end in the browser pane, with the real Lumi Cloud (development
  server) and this app in an isolated home:
  - Published a policy allowing only Ask and Plan, with secret scanning
    locked.
  - In Settings > Lumi account, entered the address and pressed Enter; the
    browser pane (standing in for the system browser) approved; the app
    showed the account and Acme Robotics.
  - **Use on this computer** enrolled it. Privacy & security showed the
    policy from Lumi Cloud (signed, valid for 14 days), and the mode
    picker changed from Full-auto to Ask, offering only Ask and Plan, all
    without a reload.
  - After a restart, publishing version 2 and pressing **Check in now**
    applied it, and Lumi Cloud's Devices page showed v2 at once.
  - The run found and fixed three problems:
    - the page kept an old policy snapshot until the app pushed settings
      after a policy change;
    - the page didn't redraw while the address field kept focus after Enter;
    - the fleet page showed no policy until the next hourly check-in.

## September 25 documentation site — source only, not published

- **The user and administrator guides build as a site** (`mkdocs.yml`, MkDocs
  with the Material theme, in Lumi's gold-on-night colors with light and dark
  modes). [The home page](index.md) groups them:
  - **Using Lumi**: getting started, models, enterprise sign-in, usage and
    costs, updates, macOS and plans;
  - **For organizations**: policy, Windows deployment, the audit log, running
    without a UI, and GitHub;
  - **Extending Lumi**: capability packs, skills and creative editors.
- **[Writing a capability pack](packs.md)** is new: the manifest's fields,
  agent and skill files, every hook type, how approval and digests work, and
  sharing a pack from Git.
- The **Docs** workflow builds the site with `mkdocs build --strict` on every
  pull request and keeps it as the `lumi-docs-site` artifact. It isn't
  published anywhere yet; where it goes (GitHub Pages or the Lumi website)
  needs the owner's decision. Build it locally with
  `pip install -r packaging/docs-requirements.txt` and `mkdocs serve`.
- Historical plans and release notes are built but left out of the site's
  navigation. `docs/README.md`, the repository's documentation index, isn't
  part of the site, because it and the site's home page would share an
  address.

Validation on September 25, 2026:

- `test_docs_links.py`: every page in the navigation exists, and every
  relative link in those pages points to a file in the repository.
- Full `pytest`: 3,856 passed, 3 skipped.
- MkDocs isn't installed on the development machine, so the site itself is
  built by the Docs workflow on the pull request.

## September 25 free for individuals: About Lumi and plans — source only, not released

- **Settings > About Lumi**, also opened from **Help > About Lumi**, which
  used to flash a one-line status. It shows:
  - the version, and who manages this copy (the policy's organization, or
    device management for an MSI install);
  - that the whole app is free for individuals, without an account;
  - what leaves the computer: prompts, code and keys go only to the chosen
    model providers, and Luminary Analytics receives only the update check;
  - the MIT license and, in an installed copy, where the third-party notices
    are.
- **[Plans](plans.md)** (a draft for product review) writes the free tier down
  and lists what Lumi Cloud will add for organizations. Paid plans and prices
  aren't decided.

Validation on September 25, 2026:

- `test_about.py`: the About command's version, license, notices and
  organization under a policy.
- In the browser pane, with a pilot policy from "Example Corp", Help > About
  Lumi opened Settings > About Lumi with "Managed by Example Corp" and the
  sections above.

## September 25 getting-started checklist — source only, not released

- **A first-run checklist in the empty chat** ([guide](desktop-workflow.md#getting-started),
  `lumi/gui/onboarding.py`) replaces the static welcome card. Its three steps
  follow real state:
  - **Connect a model**, with a shortcut to Connections;
  - **Open a project**, with the folder picker, or **Try the sample
    project**, which creates `Documents/Lumi Projects/lumi-sample`, a tiny
    Python program with a planted bug, and opens it;
  - **Finish a first task**: **Use a suggested task** fills the composer
    without sending. The first turn that ends without an error sets
    `onboarding.first_task_done`.
- The **×** hides the checklist for good (`onboarding.dismissed`). The page
  can't mark the task done itself. The checklist moves the empty chat up so
  short windows show it.

Validation on September 25, 2026:

- 4 tests in `test_onboarding.py`:
  - the sample project and its planted bug;
  - never overwriting an existing sample;
  - what counts as a finished turn;
  - the settings and socket commands, including refusing
    `first_task_done` from the page.
- Full `pytest`: 3,853 passed, 3 skipped. A contract test that found the old
  card's comment now looks for the checklist's.
- In the browser pane, from a fresh isolated home with the scripted Ollama
  stub as the only model:
  - the checklist showed the model step done;
  - **Try the sample project** created and opened `lumi-sample`;
  - **Use a suggested task** filled and focused the composer;
  - Enter ran the turn, the task step was ticked, and `first_task_done` was
    saved;
  - a new session showed no checklist;
  - in a second fresh run, **×** hid it and saved `dismissed`;
  - at 1280×720 the whole checklist was above the composer. In the 417-pixel
    browser pane it needs a scroll.

## September 25 image descriptions for text-only models — source only, not released

- **A vision model describes images for a chat model that can't see them**
  (`lumi/engine/image_descriptions.py`, [guide](models.md#images-for-models-that-cant-see-them)).
  - When the chat model's capabilities have no vision and **Models for
    roles** has a `vision` model, attached pictures and tool screenshots are
    described before the request.
  - Each image is described once (at most six per step), and the description
    is saved with the image and the model that wrote it.
  - Text-only adapters send `[Image: …, described by provider:model]` plus the
    description. These are the Ollama tool screenshots, custom connections
    with **Models accept images** off, and SONN's text transport, which still
    says a notice isn't evidence when there is no description.
  - The conversation shows a notice. Usage records the requests as
    `image_description`.
  - Two failures leave the image with its notice.
- Custom connections with **Models accept images** off now send text in place
  of images, in messages and tool screenshots alike. Connections without the
  setting keep sending images, as before.

Validation on September 25, 2026:

- 9 tests in `test_image_descriptions.py`:
  - finding undescribed images;
  - the labelled fallback;
  - a turn where a stand-in vision model describes an attached image once,
    records `image_description` usage, and isn't asked again;
  - tool screenshots;
  - nothing is asked without a vision model, or when the chat model sees;
  - failures and the retry cap;
  - a gateway without vision getting text for both images;
  - a gateway with vision still getting the image;
  - SONN passing a labelled description.
- The new conversation notice mirrors the fallback notice. It wasn't checked
  in the browser, because attaching an image needs a native file dialog the
  browser pane can't drive.

## September 25 capability packs from Git — source only, not released

- **Settings > Capability packs > Install from Git** (`lumi/engine/pack_install.py`, [guide](desktop-workflow.md)):
  - installs a pack from a public https repository, pinned to one commit;
  - a tag or branch is resolved to the commit it names now;
  - only that commit is fetched (depth 1, no submodules, no credential
    helper or prompt, links checked out as plain files);
  - the pack arrives turned off in `~/.lumi/packs/<id>` and runs only after
    the usual review and approval;
  - reinstalling at another commit drops the earlier approval, and **Remove**
    deletes a pack installed this way;
  - `plugins[<id>].source` records the URL, commit and folder.
- **Policy:** `extensions.allowed_sources` limits the repositories packs may
  come from.
- **Audit:** `extension.install` and `extension.remove`, with the commit.
- Refused: non-https addresses, credentials in the URL, folders with `..`,
  manifests without an `id`, and symbolic links.

Validation on September 25, 2026:

- 18 tests in `test_pack_install.py` (one is skipped on Windows), against
  local git repositories:
  - addresses and allowed sources;
  - lightweight and annotated tags, branches and commits resolving;
  - a pinned install that ignores newer commits, leaves no `.git` or scratch
    folders, and is discovered untrusted;
  - refusals;
  - a pack in a subfolder;
  - the Settings flow through the socket: install, approve, reinstall (back
    to needing approval), remove, and the audit records.
- In the browser pane (isolated home; a local repository stood in for the
  https remote, allowed only by the fixture):
  - an `http://` address was refused on the page, with the typed values
    kept;
  - the local repository at tag `v1` installed as "Not approved · off", with
    "Installed from … at commit 15d389f6ff93" and its hook listed;
  - **Approve and enable** made it "Approved · active";
  - **Remove** deleted it from `~/.lumi/packs` and from the settings.
  - This check found that a refusal wasn't shown while the form kept focus;
    install results now always redraw the page.

## September 25 macOS app and DMG — source only, not released

- **`Lumi.app` in `lumi-X.Y.Z.dmg`** for Apple silicon ([guide](macos.md)):
  - `packaging/build_macos.sh` builds it from the same hash-pinned lock (with
    its macOS PyObjC wheels) and `packaging/lumi.spec`, then checks it against
    `packaging/bundle-policy-macos.json`;
  - the spec now picks pywebview's WebKit backend on macOS and leaves out the
    Windows-only WinSparkle;
  - the app carries its version, a minimum of macOS 12, and usage strings for
    the microphone and automation.
- **Signing and notarization** (hardened runtime,
  `packaging/macos/entitlements.plist`, `notarytool`, stapling) run when a
  Developer ID and Apple credentials are configured as secrets; otherwise the
  build is unsigned and says so.
- **CI:** `.github/workflows/build-macos.yml` builds on `macos-latest` and
  starts the app: `--version`, `lumi updates`, and the GUI server with the
  page, the launch code and the WebSocket token check. It keeps the DMG as an
  artifact. Nothing is published.
- **Search without ripgrep** (macOS and Linux) now uses `grep -E`, so
  alternation, `+` and groups mean what they do in ripgrep.
- Third-party notices list ripgrep and WinSparkle only in Windows builds.

Validation on September 25, 2026, from PR #23's macOS build (`macos-latest`,
Apple silicon):

- The bundle was 85.4 MiB (232 files) and passed its policy. The DMG was
  32 MB, unsigned, with the expected warning.
- `lumi --version` and `lumi updates` ran.
- The GUI server served the page (52 KB), redeemed the launch code, refused
  the WebSocket without the token (403) and accepted it with the token (101),
  and its log was clean.
- The first run found that the launch link stayed in a frozen app's stdout
  buffer; those lines are now flushed.
- No Mac was available locally, so the native window, dictation, computer use
  and the Keychain are untested.

## September 25 updates that wait for running turns — source only, not released

- **An update never cuts off an agent turn** (`lumi/updater.py`, [guide](updates.md#installing-an-update)):
  - WinSparkle asks whether Lumi can close before running a verified
    installer. While a turn runs, Lumi says no, and WinSparkle holds the
    installer back.
  - Otherwise Lumi closes itself once the installer starts: the window closes
    and the local server stops, instead of the installer forcing it closed.
  - When Lumi can't tell whether a turn is running, it keeps the turn.
- **Update events in the audit log:** `update.check` (found, none, error),
  `update.deferred`, `update.install`, `update.skipped`, `update.postponed`
  and `update.cancelled`, with the version and feed.

Validation on September 25, 2026:

- 5 tests in `test_updates.py`:
  - the callbacks are registered before WinSparkle starts;
  - an update waits for a running turn, then closes the app;
  - an unknown state keeps the turn;
  - each event is recorded;
  - the vendored `WinSparkle.dll` exports every function Lumi declares (it is
    loaded, and nothing in it is called).
- The callbacks were called directly in these tests. A real WinSparkle update
  hasn't been installed with them yet, because that needs a published release.

## September 25 MSI for Intune, Configuration Manager and Group Policy — source only, not released

- **`lumi-X.Y.Z.msi`** ([guide](deploy-windows.md), `packaging/lumi.wxs`,
  `packaging/build_msi.ps1`, WiX 5):
  - per machine into `Program Files\Lumi`, silent with
    `msiexec /i … /qn`, with a Start menu shortcut;
  - upgrades in place (fixed upgrade code; a rebuild of the same version
    replaces it too);
  - removed with `msiexec /x`.
- **`POLICYFILE=…`** sets the machine policy's `PolicyFile` value on install and
  removes it on uninstall.
- **MSI copies never update themselves.** The package puts `lumi-install.json`
  beside `lumi.exe`, which turns updates off whatever settings or policy say.
  Settings > Updates and Check for Updates say why.
- **The EXE and the MSI refuse each other,** so one folder never has two
  installers.
- **`lumi updates`** prints the update settings in effect as JSON, for
  administrators and detection scripts.
- **Release workflow:** stable tags build, Authenticode-sign (when configured)
  and attach the MSI, and the download page offers it to administrators.
  **Build check:** every change builds the MSI, installs it silently on the
  Windows runner, checks it, and uninstalls it.

Validation on September 25, 2026:

- 7 tests in `test_msi_package.py`. They check that the package, the EXE
  installer, the policy reader and the update settings agree:
  - per-machine scope, the upgrade code and the shortcut;
  - the EXE's AppId in the launch condition;
  - the policy key for `POLICYFILE`;
  - the marker `build_msi.ps1` writes, and that it turns updates off;
  - the Check for Updates message;
  - `python -m lumi updates`.
- `test_publish_pages.py` covers hosting the MSI and the download page link.
- WiX isn't installed on the development computer, so the MSI is built only
  in CI. On PR #21's build check (WiX 5.0.2 on `windows-latest`), the MSI
  (29.4 MB) was built and installed silently with `POLICYFILE`. The files,
  marker, shortcut and policy value were present. The installed
  `lumi updates` reported `mode=off installed_by=msi channel=beta
  managed_by=CI`, and the MSI uninstalled cleanly.
- Not yet deployed through a real Intune tenant, Configuration Manager site or
  Group Policy.

## September 25 update channels, pins and turning updates off — source only, not released

- **Settings > Updates** ([guide](updates.md), `lumi/update_channels.py`), under
  Advanced:
  - **Check for updates:** automatically (once a day and on demand), only
    when asked, or never;
  - **Channel:** stable or beta;
  - **Stay on release line:** a pin such as `0.20`, which takes only that
    line's stable releases.

  These are read at startup like the policy. The page shows the installed
  version, the feed in use, the last check, and what applies after a restart.
- **Policy:** `updates.mode`, `updates.channel` and `updates.pin` can be locked.
  A value Lumi can't apply makes the policy invalid. An invalid machine policy
  pauses automatic checks.
- **Check for updates** says why nothing happens: updates are turned off by
  the organization or in Settings, will start after a restart, or this copy
  runs from source.
- **Running from source** no longer loads WinSparkle. Before, development runs
  and fixtures wrote the real HKCU WinSparkle key and could show its dialogs.
  `LUMI_UPDATER_FROM_SOURCE=1` restores the old behavior for testing the
  updater.
- **Release pipeline:**
  - `update_appcast.py` now writes `appcast.xml` (stable, unchanged address),
    `appcast-beta.xml`, and `appcast-X.Y.xml` for the four newest release lines.
  - Beta tags (`vX.Y.Z-beta.N`, `-alpha.N`, `-rc.N`) reach only the beta feed.
    Before this change they weren't published to Pages at all.
  - `publish_pages.py` keeps the newest installer of each recent line, so
    pinned installs can download their update, and always links the newest
    stable release.
- Settings text fields, like multi-line ones, keep typed text after a refused
  save so it can be corrected.

Validation on September 25, 2026:

- 28 tests in `test_updates.py`:
  - pins, feeds, settings.json values and their mistakes;
  - policy locks and invalid policy values;
  - the WinSparkle calls for each mode, run against a recording stand-in for
    the DLL;
  - status and restart-pending reporting;
  - the from-source guard;
  - Settings commands and messages.
- 6 tests in `test_update_feeds.py`:
  - version ordering (alpha < beta < rc < release);
  - a stable release in every feed, with idempotent re-runs;
  - betas only in the beta feed and retired by their release;
  - line feeds, including a fix for an older line;
  - refusal of unsigned releases and bad versions;
  - agreement between the feeds the client reads and those the script writes.
- `test_publish_pages.py` gained tests for line retention and betas.
- Full `pytest`: 3,810 passed, 2 skipped.
- In the browser pane, with an isolated home and a pilot policy from "Example
  Corp" that locks the channel:
  - Ctrl+, opened Settings, and searching "beta" found only Updates;
  - the channel showed "Managed by Example Corp" and was disabled;
  - the status read "Lumi 0.19.2.dev11 · Automatic, from the stable channel ·
    managed by Example Corp", noted that a copy running from source doesn't
    update itself, and disabled Check for updates;
  - the pin "latest" was refused with the reason and stayed in the field;
    "0.20" saved;
  - "After Lumi restarts" changed with each save, including while the mode
    select kept focus;
  - Help > Check for Updates said the copy doesn't update itself.
- No release has been published with these feeds. The WinSparkle calls were
  exercised against a stand-in, not the real DLL.

## September 25 enterprise sign-in for connections — source only, not released

- **Sign-in instead of a key** ([guide](connection-sign-in.md),
  `lumi/auth_tokens.py`), chosen under a custom connection's
  **Authentication**:
  - **OAuth client credentials** for OpenAI-compatible gateways, OpenAI and
    Anthropic proxies and Azure OpenAI: a token URL, client id, optional scope
    and audience, and the client secret, kept in the credential store. The
    secret goes only to the token endpoint; the model endpoint gets the access
    token, fetched again a minute before it expires.
  - **Microsoft Entra ID** for Azure OpenAI: an app registration (tenant,
    client id and secret), or, without a client id, this computer's sign-in
    through `azure-identity` when installed, else the Azure CLI. A client id
    without its secret is refused rather than falling back to the computer's
    identity. The Azure CLI is found on Windows (`az.cmd`), runs without a
    console window, and gets only plain tenant and resource arguments.
  - **Client certificates** for gateways that require mutual TLS, alongside
    any sign-in, verified against the same trust store. A key protected by a
    passphrase is refused with a message instead of a hidden prompt.
- **Test connection** signs in first, so it reports the identity provider's
  reason (for example *Sign-in was refused: invalid client*) instead of
  "listed no models".
- Model listing for Anthropic and OpenAI proxies uses the sign-in token and
  certificate too.

Validation on September 25, 2026:

- 19 tests in `test_signin.py`, against simulated endpoints:
  - token exchange, caching and refresh; a refusal that doesn't echo the
    secret;
  - Entra ID through an app registration and through a simulated Azure CLI,
    including a missing CLI, a failed `az login`, a client id without a
    secret, and tenant or scope values that could be interpreted by cmd.exe;
  - certificates generated in the test: loading, a bad file, a key with a
    passphrase;
  - a real TLS handshake with a local server that requires a client
    certificate from its CA: models are listed with the certificate, and
    the same trust without it is turned away;
  - connection validation for the new fields;
  - a gateway, an Anthropic proxy and an OpenAI proxy that receive the token
    and never the secret;
  - Azure OpenAI with Entra ID and a certificate, including its health check;
  - Test connection reporting a refused sign-in.
- Full `pytest`: 3,773 passed, 2 skipped.
- In the browser pane, with an isolated home, a simulated identity provider
  and a local HTTPS gateway that requires a client certificate:
  - a connection with OAuth client credentials and a certificate was added
    through the form. **Test connection** with a wrong secret showed "Sign-in
    was refused: Invalid client secret"; with the right one, "Connected · 1
    model available: gateway-coder";
  - after saving, the row read "secret stored", the secret was absent from
    the saved connection, and editing showed every sign-in field with
    "Stored client secret — leave blank to keep it";
  - a turn with `gateway-coder` read `app.py` and finished. All five gateway
    requests carried the token and the client certificate and none carried
    the secret; one token request served them all;
  - on Azure OpenAI with Entra ID, the client secret field appeared while a
    client id was typed and hid when it was cleared; Tab moved on to Scope.
  - This check found two form bugs, now fixed: the sign-in fields broke the
    form's rendering, and re-rendering on leaving the client id field lost
    keyboard focus.
- Not yet tried against a real identity provider, Entra tenant or mTLS
  gateway.

## September 25 fallback models, roles and capability overrides — source only, not released

- **Fallback models** ([guide](models.md)): **Settings > General > If the
  model fails, continue with** (`general.fallback_models`, up to five
  `provider:model` lines).
  - A request that fails before any answer (provider error, overload, a
    connection failure) retries the same step with the next usable model.
  - The conversation shows a notice, and the audit log records
    `model.fallback`.
  - Fallbacks the policy blocks, that an organization budget can't price, or
    that can't be built are skipped. The next turn tries the chosen model
    again.
  - `lumi run --fallback provider:model` adds to the list.
- **Models for roles:** **Settings > General > Models for roles**
  (`role provider:model` lines) feed the existing role router. A `summarize`
  model now names sessions and compacts long conversations. Titles no longer
  need the chat model to be a native one, so a session using Codex or Claude
  Code can be named by a local model. Title requests are recorded with their
  conversation.
- **Capability overrides:** policy `models.capabilities` states a model
  pattern's context window, vision, tools, reasoning levels, computer use or
  concurrency. The override wins over name inference and runtime reports.
- **Settings fields for lists:** multi-line fields, with typed text kept after
  a refused save. A successful save clears the error alert at once.

Validation on September 25, 2026:

- 13 new tests in `test_model_fallback.py`:
  - a fallback after a provider error and after a crashed stream;
  - skipped fallbacks (policy, a budget, a factory that fails);
  - no fallback after partial output;
  - `lumi run --fallback`;
  - list parsing, and the socket's normalization;
  - the summarize model naming a CLI-backed session;
  - capability overrides, and invalid ones refusing the policy.

  The title tests' stand-ins take the new usage context.
- Full `pytest`: 3,754 passed, 2 skipped.
- In the browser pane, with an isolated home, the chosen model on a
  connection to a closed port, and `ollama:stub:latest` as the fallback:
  - the turn showed "conn-broken:claude-sonnet-5 failed (Broken gateway
    connection failed: ConnectError); continuing with ollama:stub:latest."
    and finished with changed files and no error;
  - in Settings > General, invalid lines were refused with an alert and kept
    for fixing; corrected lines saved and cleared the alert.

## September 25 GitHub pull requests — source only, not released

- **GitHub tools** (`lumi/engine/github_tools.py`, [guide](github.md)), loaded
  on demand through `search_tools`:
  - `github_pr_view`: the branch's pull request, with reviews, review comments
    (ids, file:line, outdated), the conversation and every check with its job
    id;
  - `github_check_log`: the end of a GitHub Actions job's log, plus earlier
    error lines;
  - `github_pr_create`: pushes the branch, never forced, and opens the PR;
  - `github_pr_comment`: a comment, or a reply in a review thread;
  - `github_pr_update`: the title, the description, or ready for review.
- The two reading tools are approved like `git_log`. The three that change
  things ask first under Auto accept edits and Ask permissions.
- Works with github.com and GitHub Enterprise Server (`/api/v3`, or
  `GITHUB_API_URL`), with the repository taken from `origin`.
- The token is **Settings > API keys > GitHub token** (credential store), or
  `GITHUB_TOKEN`/`GH_TOKEN`. It goes only into request headers. Job-log
  downloads that redirect to other hosts don't carry it, and error messages
  never echo credentials from a remote URL.

Validation on September 25, 2026:

- 12 new tests in `test_github_tools.py`, against a mocked GitHub API and a
  temporary repository:
  - remote parsing, Enterprise hosts, and a foreign remote refused without
    echoing its password;
  - the PR view and a job log, where the token isn't sent to the log
    redirect's host;
  - a missing token;
  - opening, commenting, replying, updating and marking ready;
  - a real `Session.run` in auto-edit: the view ran unasked, the comment asked
    and was refused.
- Full `pytest`: 3,741 passed, 2 skipped.
- In the browser pane: searching Settings for "GitHub" found the new
  **GitHub token** field, and a typed token was saved (shown as Stored, the
  value never sent back to the page).

Not exercised: the real GitHub API.

## September 25 headless runs — source only, not released

- **`lumi run`** (`lumi/headless.py`, [guide](headless.md)) runs one task
  without a UI, for servers, containers and CI. Options:
  - the prompt from an argument, stdin (`-`) or `--prompt-file`;
  - `--provider`/`--model` (or `LUMI_PROVIDER`/`LUMI_MODEL`, or the desktop
    defaults);
  - `--mode ask|auto-edit|bypass` (default `auto-edit`);
  - `--trust-project`;
  - `--max-requests`, `--timeout`;
  - `--output json|text|jsonl`.

  Nothing asks a person: a disallowed tool call is refused, and a budget that
  needs approval stops the run. The run builds its session like the app does:
  - policy modes and models;
  - execution rules;
  - exclusions;
  - repository instructions only when the project is trusted or
    `--trust-project` is given.

  Budgets, usage records, the audit log and the secret scan apply.
- **The result** is JSON: status, the engine's outcome, text, errors, changed
  files, checks, tool calls, refused calls, model requests, usage and cost.
  Exit codes:
  - 0: completed;
  - 1: failed;
  - 2: the command or setup is wrong;
  - 3: stopped for a person (needs input, incomplete, refused an action, a
    budget, the request limit or the timeout).
- **Container:** `packaging/docker/Dockerfile` builds a `lumi` image with git
  and ripgrep, a non-root user and `LUMI_KEYCHAIN=off`. A new **Container
  build** workflow builds it and checks `lumi run` inside it. Nothing is pushed
  to a registry.
- The execution-rule layering (tier, the project's `lumi-policy.json`,
  organization shell rules first) moved from the GUI to
  `engine/policies.project_execution_policy`, so the app and `lumi run` share it.

Validation on September 25, 2026:

- 10 new tests in `test_headless.py`:
  - key sources, and refused setups;
  - a completed run with a changed file and its cost;
  - a refused edit reported as `needs_attention`;
  - a provider failure;
  - a budget stop, and a timeout;
  - the `jsonl` and `text` outputs, and stdin;
  - policy refusals (exit 2);
  - repository instructions only with trust.
- Full `pytest`: 3,729 passed, 2 skipped.
- `python -m lumi run` as a separate process against the scripted Ollama stub,
  in a throwaway home:
  - `--mode bypass` edited `app.py` and exited 0 with `completed`;
  - `--mode ask` exited 3 with `needs_attention` and one refused call;
  - both runs wrote audit and usage records;
  - missing provider and key setups exited 2 with a message.

Not exercised: a real model, and the container build locally. The Container
build workflow checks the image in CI.

## September 25 budgets — source only, not released

- **Budgets** (`lumi/budgets.py`, [guide](usage-and-costs.md#budgets)) act on
  the priced spend in the usage records. There are three thresholds:
  - an alert, shown once per period as a notice in the conversation;
  - an approval point, where Lumi asks "…continue anyway?" before the next
    model request. It asks once per period, or once per turn for a per-turn
    budget. **Stop**, or a run that can't ask (the gateway, a delegated
    worker), stops the turn;
  - a stop. A spent day or month budget refuses new turns up front, and a
    per-turn cap stops before the next request with "send Continue to go on".
- **Your limits:** **Settings > Usage & cost** adds **Ask before spending more
  than ($ per day)** and **Stop a turn after spending ($)** beside the existing
  daily alert. The alert is now enforced by the engine for the GUI, terminal UI
  and gateway alike; it used to be a GUI-only message. The page lists every
  budget with this period's spend and refreshes after a save.
- **Organization budgets:** policy `budgets` apply per user, per project (a
  folder glob) or per turn, by day or month. `block_unpriced` refuses unpriced
  models while that budget applies. Alerts, answers and stops are audited as
  `budget.warning`, `budget.approval` and `budget.block`.
- **Prompt fix:** the "needs your input" card kept only the sentences with a
  question mark, and split them at decimal points. A question with "$0.27" or
  "Python 3.12" showed only its tail. Sentences now end only at punctuation
  followed by a space.

Validation on September 25, 2026:

- 12 new tests in `test_budgets.py`:
  - rule parsing, and settings turned into rules;
  - levels by scope, user and project;
  - real `Session.run` turns: an alert shown once, the question answered with
    Continue, Stop, or unanswerable, a spent budget refusing a turn, the
    per-turn cap, and `block_unpriced`;
  - limit validation, and the budgets in the costs payload.

  One new node test covers the prompt's question text.
- Full `pytest`: 3,719 passed, 2 skipped.
- In the browser pane, with an isolated home, a policy budget and prices set
  so each stub call cost $0.07–$0.10:
  - the first turn showed "Today's spend is $0.07, past your $0.05 alert.";
  - the next turn asked "Today's spend is $0.27, past your $0.20 limit —
    continue anyway?". Continue resumed it; Stop ended it with "Stopped at your
    request…";
  - Usage & cost listed the organization's and the person's budgets with
    spend, and showed a new per-turn limit after it was saved.

## September 25 usage records and prices — source only, not released

- **Usage records** (`lumi/usage.py`, [guide](usage-and-costs.md)): one JSON
  line per model call in `~/.lumi/usage/YYYY-MM.jsonl`. Each record has:
  - the user, project, conversation and worker;
  - the purpose: `turn`, `subagent`, `title` or `compression`;
  - provider and model;
  - tokens, with cache reads and writes, normalized across providers
    (Ollama's `prompt_eval_count`, Claude Code's separate cache counts,
    Codex's `cached_input_tokens`);
  - the reported and computed cost, and the price source.

  Turns are recorded in `Session.run`, and auxiliary requests in
  `auxiliary_stream`, so titles and compaction are counted now.
- **Prices** (`lumi/pricing.py`) resolve in this order:
  - the provider's reported cost;
  - an organization policy's new `pricing.prices`;
  - the user's prices;
  - local models at $0;
  - subscriptions (Codex, Claude Code, Ollama cloud);
  - a bundled list, checked on September 25, 2026 against Anthropic's and
    OpenAI's published pages.

  The list corrects stale entries: `o3` was $10/$40 and is now $2/$8;
  `gpt-5.4` was $5/$20 and is now $2.50/$15; bare `opus` matched Opus 4's
  $15/$75. Cache writes are priced.
- **Unknown models are unpriced, never $0.** They used to fall back to free.
  Settings counts them separately, and the model table labels them.
- **Settings > Usage & cost** adds this month's calls by model and a **Prices**
  editor. Lines have the form `pattern input output [cached] [write]`, and
  prices an organization sets are listed there too.
- **`lumi usage`** prints a monthly summary by model, provider, project,
  purpose, user or session, or exports CSV or JSONL.
- **Audit fix:** a delegated worker's tool calls and errors, which the parent
  passes on for display, were recorded a second time under the parent. A
  worker's error also marked the parent turn as failed. The worker's own turn
  now records them once. `model.usage` audit records carry the cost and price
  source.
- **One set of totals:** Settings' Today, session and total figures now come
  from the usage records, so titles and compaction count too. Subscription
  calls are counted apart from unpriced ones.
- **Settings errors are visible:** a refused settings change (a bad price
  line, an invalid collector URL, a setting the organization manages) used to
  go to the hidden chat stream. It now shows as an alert on the Settings page,
  and the typed prices stay in place to fix.

Validation on September 25, 2026:

- 22 new tests in `test_pricing_usage.py`:
  - price order and cache pricing;
  - organization, user, local and subscription prices;
  - price lines, and a policy with bad prices;
  - token normalization;
  - the ledger with a second writer;
  - priced session turns, and a recorded title request;
  - worker events recorded once;
  - the costs command, the settings validation and the export;
  - the app counting every recorded call.
- Full `pytest`: 3,707 passed, 2 skipped.
- In the browser pane, with an isolated home and the stub serving an
  Anthropic-format connection (`claude-sonnet-5`), an OpenAI-compatible
  connection (`house-model`) and Ollama (`stub:latest`):
  - the records were priced from the catalog with cache reads at the cache
    rate, as unpriced, and as local $0, and the title request was recorded
    with purpose `title`;
  - Usage & cost showed matching totals ($0.0004 for 154 tokens) and the
    unpriced call;
  - a bad price line showed an alert and kept the text. After the fix, the
    next `house-model` calls were priced from the override.

## September 25 audit log — source only, not released

- **Audit log** (`lumi/audit.py`, [guide](audit-log.md)): hash-chained JSON
  lines in `~/.lumi/audit/`, one file per UTC day. `Session.run` now wraps the
  loop (`_run_turn`) and records every turn from the GUI, gateway, terminal UI
  and workers:
  - turn start and end with outcome;
  - model usage, with each provider's token names normalized;
  - tool calls and results;
  - file changes, including Codex's;
  - user and hook approvals;
  - secret redactions and errors.

  Settings changes (key names only) and project trust decisions are recorded
  too.
- **Tamper evidence:** each record hashes the previous one. **Settings >
  Privacy & security > Audit log status** verifies the whole chain. The GUI,
  terminal UI and gateway share one chain through an OS file lock.
- **Capture levels** (`privacy.audit_capture`):
  - metadata only (the default): names, outcomes, sizes, paths and digests;
  - redacted content;
  - full content.

  Saved keys are removed at every level.
- **Retention:** `privacy.audit_retention_days` (default 365) is separate from
  transcript retention; `privacy.audit_log` turns recording off.
- **OpenTelemetry export:** `audit.otlp_endpoint`, `audit.otlp_auth_header` and
  an `api_keys.otlp` token send spans over OTLP/HTTP JSON. They use GenAI
  semantic-convention attributes and a bounded background queue that drops
  instead of blocking. An organization policy can lock all of these settings.

Validation on September 25, 2026:

- 27 new tests in `test_audit_log.py`:
  - the chain: edits, removals, reordering, a second writer, threads, odd
    text, large records, day rollover;
  - capture levels and retention;
  - span attributes, batching, a failing collector, the background thread,
    and starting and stopping the export;
  - real `Session.run` turns: file change, usage, denial, stop, error, and a
    broken log;
  - the socket's validation of the new settings, and change records that name
    only keys that really changed.
- Full `pytest`: 3,686 passed, 2 skipped.
- In the browser pane, with an isolated home, the scripted Ollama stub and a
  local HTTP server standing in for a collector:
  - a turn produced ten chained records;
  - the collector received the batches with the `Bearer` token header and
    GenAI span attributes;
  - Privacy & security showed "Verified: 9 records". After one record was
    edited on disk, **Verify again**, pressed with the keyboard, reported
    "2026-09-25.jsonl:6 was changed";
  - after switching the capture level to redacted, a prompt's GitHub-format
    token was kept only as `[REDACTED GitHub token]`;
  - leaving a field unchanged recorded nothing.

Not exercised: a real OpenTelemetry collector product, and the packaged app.

## September 25 organization policy — source only, not released

- **`lumi.policy/v1`** (`lumi/policy.py`, [administrator guide](enterprise-policy.md)):
  a machine-wide policy from the registry (Group Policy/Intune), macOS managed
  preferences (a configuration profile), `ProgramData`, `/Library/Application
  Support` or `/etc`. `LUMI_POLICY_FILE` applies only when none of those exists,
  so a user can't replace their organization's policy.
- **Locked settings:** policy values win over the user's. Settings shows them
  disabled with "Managed by <organization>", and the app's settings commands
  refuse changes. This locks the secret scan, retention, the Codex/Claude Code,
  computer use and gateway switches, the network settings and others.
- **Enforcement:**
  - allowed permission modes (others hidden; a saved default outside the list
    becomes the first allowed mode);
  - allowed and blocked `provider:model` patterns (removed from the model menu
    and refused at spec construction and at the start of every turn);
  - extra file exclusions;
  - shell rules checked before built-in and repository rules;
  - MCP server allowlist and a switch for command-based servers;
  - capability pack allowlist.
- **Signed policies:** Ed25519 over the policy's canonical JSON, verified against
  keys only an administrator can set (registry `PolicyKeys`, `policy-keys.json`
  or the machine policy's `trusted_keys`). A signed policy with `expires_at`
  stays enforced for `grace_days` offline, then Lumi refuses model requests. An
  invalid policy also refuses requests instead of silently meaning "no policy".
- **Administrative templates:** `packaging/policy/lumi.admx` with
  `en-US/lumi.adml`, a macOS `lumi-policy.mobileconfig` and `example-policy.json`.
- Settings > Privacy & security starts with an **Organization policy** summary.
- `cryptography` is a core dependency (for Ed25519), and the release lock was
  regenerated.

Validation on September 25, 2026:

- 28 new tests: `test_policy.py` (parsing, signatures and tampering, expiry,
  source precedence, invalid policies, templates) and `test_policy_enforcement.py`
  (locked settings and socket refusal, modes, models in the UI data, org shell
  rules, exclusions, MCP, packs, refused turns).
- Full `pytest`: 3,659 passed, 2 skipped.
- In the browser pane, against an isolated home with a policy from
  `LUMI_POLICY_FILE` and a stub offering an allowed and a blocked model:
  - the permission menu offered only Ask and Auto accept edits (a saved
    Full-auto default became Ask);
  - the model list dropped the blocked model;
  - Privacy & security opened with the policy summary, and locked fields were
    disabled with "Managed by Example Corp" (the scan showed on although the
    saved value was off).

Not exercised: a real Group Policy or MDM deployment, signed delivery from Lumi
Cloud (not built yet), and the packaged app.

## September 25 client security: file exclusions, project trust, retention and tool switches — source only, not released

- **Files Lumi never reads** (`lumi/engine/exclusions.py`): gitignore-style
  patterns in **Settings > Privacy & security**, plus a project's `.lumiignore`.
  Both apply at once when edited.
  - File tools refuse excluded files, including `batch` children, and so does a
    search rooted at an excluded folder.
  - glob, grep, git status and git diff leave them out and say how many were
    hidden. The codebase index skips them.
  - `@file:` attachments explain the refusal instead of attaching.
    `@diff:working` excludes them from the diff text.
  - Opening a `file://` page in the browser tools now goes through the same
    sandbox and exclusion checks; before, it could read any local file.
  - Shell commands can still open excluded files.
- **Project trust** (`lumi/gui/workspace_trust.py`): until the user trusts a
  project, Lumi doesn't use what the repository brings:
  - its instruction files (AGENTS.md, LUMI.md, CLAUDE.md and similar), including
    in a Mission's first message;
  - its committed notes (`.lumi/memory.json`) and codebase summary;
  - the `allow` rules in its `lumi-policy.json` (its deny and ask rules still
    apply). This entry first said those rules let a cloned repository skip
    approval prompts; they didn't until "September 25 repository allow rules"
    above;
  - automatic lint and test runs, which execute the repository's own code.

  A banner in the chat offers **Trust this project** or **Keep restricted**.
  A policy file that changes after trust needs review again. Projects already in
  Recent projects are trusted on first run, so upgrading changes nothing for
  existing work (their policy's allow rules wait for one review; see
  "September 25 repository allow rules").
- **Transcript retention** (`lumi/gui/retention.py`): "Delete transcripts after
  (days)" deletes everything holding conversation content that was last touched
  before then, at startup and daily:
  - sessions and their ledgers;
  - drafts, checkpoints, worker records, artifacts, traces and mission audit logs;
  - recordings and dated logs.

  The open session is never deleted. The default keeps everything.
- **Tools outside Lumi's own loop:** switches for Codex and Claude Code (they
  leave Models; a selected one falls back to another provider), computer use
  (tools hidden and refused) and the chat gateway (`lumi gateway` refuses to
  start). Organization policy will be able to lock these.
- `@diff:` mentions no longer pass a selector starting with `-` to git, where
  `--output=<file>` would have written a file.

Validation on September 25, 2026:

- New tests: `test_exclusions_and_trust.py`, `test_client_security.py`,
  `test_retention.py`. They cover the real tools, git, the context broker,
  project notes across a trust change, the policy builder and `AppState` wiring.
- Full `pytest`: 3,631 passed, 2 skipped.
- In the browser pane against an isolated home with a scripted model:
  - The trust banner appeared for a project with AGENTS.md and a policy allow
    rule.
  - `.env` was excluded through Settings: the model's read of it was blocked
    with the rule named, and the file's value never reached the model.
  - **Trust this project** hid the banner, and the next request carried
    AGENTS.md (it hadn't before). The decision persisted with the policy
    digest.

Not exercised: organization policy locking these settings (next group), and
the packaged app.

## September 24 supply chain: pinned dependencies, audit, notices, SBOM and signing — source only, not released

- **Pinned, hash-checked release builds:** `scripts/build_clean.ps1` installs
  `packaging/requirements-release.txt` with `--require-hashes` instead of resolving
  `.[gui,desktop]` at build time. `python scripts/lock_release.py` (uv) regenerates
  it and the CI tools lock for every supported platform and Python version, and a
  test fails when `pyproject.toml` declares a dependency the lock does not pin.
- **Vulnerability audit:** a **Dependency audit** workflow runs pip-audit on every
  pin when the locks change and weekly. `packaging/audit_locks.py` removes platform
  markers first; otherwise pip-audit silently skips packages for other operating
  systems.
- **Third-party notices:** `THIRD_PARTY_NOTICES.txt` is generated from the build
  environment with each package's license texts, plus ripgrep, WinSparkle, the web
  assets and fonts, the Python runtime and PyInstaller's bootloader. It ships in
  the bundle (required by the bundle policy) and with each release.
- **License gate:** the build fails if a shipped Python package is GPL, AGPL or
  LGPL without a recorded review. The first run found two, both optional
  PyAutoGUI helpers that Lumi never calls: **MouseInfo** and **PyMsgBox** (GPL-3.0).
  They are now excluded from the bundle.
- **CycloneDX SBOM:** `build_clean.ps1 -SbomPath` writes a CycloneDX 1.6 SBOM of
  the build environment plus the bundled non-Python components, validated against
  the schema. The build check uploads it with the notices; releases attach both.
- **Authenticode:** the release workflow signs `lumi.exe` and the installer
  through `packaging/sign_windows.ps1` when a PFX or a cloud/hardware signing
  command is configured. The installer is signed before its EdDSA update
  signature. Without credentials the release continues unsigned with a warning.
  No certificate is configured yet, and macOS notarization waits for a macOS
  build pipeline.

Validation on September 24, 2026:

- 14 new tests in `test_release_supply_chain.py`: lock coverage and hashes,
  component versions against the fetch scripts, notices, the license gate, SBOM
  additions, marker stripping, and signing without credentials.
- A local `build_clean.ps1 -SbomPath` run from the locks built a 61.6 MiB bundle.
  - The notices listed 44 packages and 8 other components.
  - MouseInfo and PyMsgBox were absent from PyInstaller's module list.
  - The SBOM had 59 components and passed schema validation.
  - A scratch copy of `lumi.exe` reported its version.
- pip-audit on every pin of both locks: no known vulnerabilities.

Not exercised: Authenticode signing with a real certificate (none exists), the
release workflow itself (it runs only on a tag), and the new CI jobs until this
PR's checks run.

## September 24 network trust, credential store and secret hygiene — source only, not released

- **Corporate networks** (`lumi/net.py`, **Settings > Connections > Network**):
  - TLS is verified with the operating system's certificate store (`truststore`),
    so a company root certificate used for TLS inspection works without exporting
    PEM bundles. A toggle turns this off.
  - A configured proxy is exported as `HTTPS_PROXY`/`HTTP_PROXY`, and a bypass list
    is added to `NO_PROXY`. Local addresses always connect directly.
  - Clearing the proxy restores the variables the user's own environment had.
  - Proxy URLs with a user name or password are refused; authenticating proxies
    need a machine-level proxy or a local helper.
- **API keys in the OS credential store** (`lumi/secrets_store.py`): keys saved in
  Settings go to Windows Credential Manager, the macOS Keychain or the Secret
  Service (service `Lumi`), and `settings.json` keeps the placeholder
  `__keychain__`.
  - Existing plaintext keys move on first load.
  - Settings says where keys are kept.
  - The store is never used while settings live in the legacy `~/.resonant`
    folder: an older SONN Client sharing that folder would read the placeholder as
    its key.
  - `LUMI_KEYCHAIN=off` and `LUMI_KEYCHAIN_SERVICE` control it.
- **Keys stay out of child processes:** the agent's shell, hooks, stdio MCP servers,
  managed jobs and previews, the Python REPL, automatic tests and lint, and
  acceptance checks start without Lumi's model-provider keys
  (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY` and the others in
  `secrets_store.PROVIDER_KEY_ENV`). An MCP server's own `env` entries still apply.
  Codex and Claude Code keep their environment.
- **Secrets removed before each model request** (`lumi/secret_scan.py`):
  - The values of saved keys, sensitive settings and provider keys in the
    environment are always replaced in tool output. Values shorter than 16
    characters are ignored.
  - **Settings > Privacy & security > Scan for secrets** (off by default) also
    replaces well-known credential formats in tool output and in your messages:
    cloud and platform keys, tokens, private keys, passwords in connection
    strings and `.env` lines.
  - The model sees `[REDACTED <kind>]`, and the chat shows a note saying what was
    removed. Codex and Claude Code read files through their own tools and are not
    scanned.
- **Diagnostics redact by value:** **Help > Save diagnostics** removes the actual
  values of saved keys (including keys in the credential store) wherever they
  appear, in addition to the existing patterns. The embedded `settings.json` is
  masked field by field, so a triager still sees which providers were configured.
- Settings no longer rebuilds a form under a click. After a toggle or menu saved,
  clicking straight into a text or key field lost the typing, because the
  deferred refresh ran before the clicked field had focus.
- `truststore` and `keyring` are now core dependencies. `packaging/bundle-policy.json`
  requires keyring's entry-point metadata in the bundle, because without it the
  frozen app would silently keep keys in `settings.json`.

Validation on September 24, 2026:

- 53 new tests: `test_secret_hygiene.py`, `test_network_settings.py`,
  `test_secret_scan.py`.
  - The credential store is an in-memory keyring.
  - The shell, a hook and an MCP server are checked for the key they must not
    receive.
  - Two turns with a scripted backend confirm that the model never received a
    planted token or a saved key.
- Full `pytest`: 3,570 passed, 2 skipped.
- In the browser pane against an isolated home, with an in-memory keyring and the
  scripted Ollama stub:
  - The scan toggle, the proxy (a credentialed URL was refused, a valid one saved
    normalized), the bypass list and the certificate toggle (by keyboard) were set
    through Settings.
  - An OpenAI key was saved to the store and cleared.
  - A turn that read a file containing a fake GitHub token showed the redaction
    note, and the stub received `[REDACTED GitHub token]`, never the token.
  - The Privacy page was checked at phone width.

Not exercised: a real proxy or TLS-inspecting network, the real Windows Credential
Manager or macOS Keychain (the fixture replaced them), and the packaged app.

## September 24 model connections: Anthropic, OpenAI and custom endpoints — source only, not released

- **Anthropic (Claude):** a native Messages API adapter (`lumi/anthropic_api.py`)
  with tool use, extended thinking and prompt caching (system prompt, tools and the
  latest user turn). Signed thinking blocks are replayed within a tool loop only
  to the model that produced them. If a loop started without thinking, thinking is
  dropped for that request instead of failing.
  - The same adapter reaches Claude on Amazon Bedrock (SigV4 signing and AWS event
    stream decoding in-house, botocore used for credentials when installed, or a
    Bedrock API key) and Vertex AI (google-auth or the gcloud CLI).
- **OpenAI:** a Responses API adapter (`lumi/openai_api.py`) for OpenAI and Azure
  OpenAI. It is stateless (`store: false`). Encrypted reasoning items are replayed
  only to the same model, quota errors are not retried, and chat model discovery
  drops embedding, audio and image models.
- **Custom connections** (`lumi/connections.py`): OpenAI-compatible gateways, Azure
  OpenAI, Bedrock, Vertex, and Anthropic or Responses proxies are defined as
  validated data in **Settings > Connections**. Each has a test button and appears
  in the model menu under its name.
  - HTTPS is required outside localhost and private networks, and credentials in
    URLs are refused.
  - Keys are stored as `conn_<id>`, are written only by the connection commands,
    and are never returned to the page.
  - A connection in use by a running turn can't be edited or removed.
- Anthropic and OpenAI keys sit alongside the other API keys and have their own
  connection checks. Capability profiles now cover the Claude, GPT/o-series and
  Gemini families, where the generic fallback had assumed a 32K window.
- The runtime status shows a connection's name, not its internal key.

Validation on September 24, 2026:

- 38 new tests: `test_anthropic_api.py`, `test_openai_responses.py`,
  `test_connections.py`. The SigV4 signature matches botocore's for a Bedrock model
  path that needs double encoding.
- Full `pytest`: 3,517 passed, 2 skipped.
- In the browser pane against an isolated home, three connections were added
  through the Settings form (typing, selects and Enter), tested, saved, and used
  from the model menu. Each completed a scripted tool loop through its own wire
  format: Chat Completions, Anthropic Messages and OpenAI Responses.

Not exercised: live Anthropic, OpenAI, Azure, Bedrock or Vertex accounts (no keys
were used), extended thinking against a real model, and the packaged app.

## September 24 dark and light themes — source only, not released

- **The saved theme now survives a restart.** The desktop window uses a new
  port and private browser storage on every launch, and the page restored the
  theme only from browser storage. A Light choice therefore came back Dark, while
  Settings still showed Light; density and font size were lost the same way. The
  server now renders the saved appearance into the page (`gui/appearance.py`).
  The native window also opens in the theme's background instead of white.
- **Match system:** a third theme choice that follows the operating system's
  light or dark setting, including changes while Lumi is open.
- **Light theme coverage.** 268 color literals that only suited the dark
  theme now use theme tokens. They had left light mode with a dark composer,
  code blocks, task cards, menus and status popover, a dark "Review" button with
  dark text, and hints below 2.5:1 contrast. Code blocks get a light
  highlighting palette. New tokens cover gold-tinted text and chips, softer
  status text, and diff colors. Native controls and scrollbars follow the theme
  through `color-scheme`.
- Dark mode is unchanged. Computed colors of every element were compared before
  and after in the chat view, the command palette, the status popover, menus,
  the model and permission pickers, New session, and Settings. The only
  differences were the same colors written in a new format, one border moving
  from 12% to 15% opacity, and native checkboxes now following the dark scheme.

Validation on September 24, 2026, in the browser pane against an isolated home
and a scripted Ollama-compatible model (no live model):

- Light mode had no text below 3:1 and no dark surfaces in the chat, all 17
  Settings pages, the command palette, status popover, menus, the model and
  permission pickers, and New session. Only hint-level text is between 3.7:1
  and 4.5:1; dark mode's hints measure 3.3–3.8:1.
- Choosing Light in Settings saved it, and a reload with empty browser storage
  still showed Light.
- Match system followed an emulated OS switch from dark to light without a
  reload, and resolved on load.
- New tests: `tests/test_appearance.py` and `tests/appearance.test.cjs`.

Not exercised: the packaged desktop window (its native background color and a
real OS theme switch), macOS, and surfaces the fixture did not open (live-run
progress, steer queue, onboarding, mission and autonomous views). Those use the
same tokens, but no one has looked at them in light mode.

## September 24 rebrand to Lumi — source only, not released

SONN Client (originally Resonant) is now **Lumi**. SONN keeps its name as the
model service Lumi can connect to. The new identity is the Lantern mark, an L
holding a gold light on night; see [brand/README.md](../brand/README.md).

- **Name everywhere a user or admin looks:** window and page titles, menus,
  prompts, notifications, the browser extension, the diagnostics bundle, the
  Windows taskbar id, the model's system prompt, `lumi.exe`, the `lumi`,
  `lumi-gui`, `lumi-tui`, `lumi-smoke` and `lumi-skill` commands, and the
  `lumi` package and distribution.
- **Logo and icons:** new favicon, sidebar mark and wordmark, welcome and
  empty-chat icons, `lumi.ico` (16–256 px; 16 and 32 px pixel-aligned),
  notification PNG, a macOS `lumi.icns` on Apple's icon grid, and the Dock
  name and icon when run from source on macOS. `scripts/build_brand_assets.py`
  draws the rasters from the SVG masters in `brand/`.
- **Palette:** gold on night replaces SONN teal. The light theme uses bronze on
  paper. Warnings are orange so they are not confused with the accent. The
  titlebar and sidebar now follow the theme; the light theme previously drew a
  dark sidebar with dark text.
- **Installer:** "Lumi", `lumi-setup-X.Y.Z.exe`, `Program Files\Lumi`, a new
  AppId. It silently removes the pre-rebrand SONN Client/Resonant install
  first, so Apps & Features keeps one entry. The macOS `Lumi.app` bundle step
  is in `packaging/lumi.spec`, but no macOS build has been made or tested.

Compatibility:

- `~/.resonant` moves to `~/.lumi` on first launch. The move happens before the
  startup log opens. If it is blocked, for example by an older build that is
  still running, the old folder stays in use and the move is retried next
  launch.
- `RESONANT_*` environment variables are read as their `LUMI_*` names.
- Hooks receive both the `LUMI_*` and `RESONANT_*` variables.
- The `resonant*` commands remain as aliases.
- Projects keep an existing `.resonant/` folder, and new projects get `.lumi/`.
- `resonant-pack.json`, `resonant-policy.json` and `RESONANT.md` are still read,
  alongside `lumi-pack.json`, `lumi-policy.json` and `LUMI.md`.
- An existing `~/Documents/Resonant Projects` stays the default projects folder.
- Agent handoff and checkpoint commits are now authored `@lumi.local`.

Deliberately unchanged:

- The update feed stays at the current GitHub Pages address, because every
  installed SONN Client polls it.
- Moving the feed to a Lumi domain needs a bridge release. Rename the
  repository only after that, because GitHub does not redirect Pages project
  sites after a rename.
- The SONN conversation-id prefix, the Engram memory namespace, the editor
  MCP entry names (`resonant_blender`…), model-facing tool names, the
  harness output fence, the `refs/resonant/checkpoints` git refs and the
  checkpoint archive marker are unchanged, because saved data uses them.

Validation on September 24, 2026:

- `ruff` and `git diff --check`: clean.
- UI recovery checks: 30.
- Full `pytest` on the final source: 3,472 passed, 2 skipped.
- New `tests/test_rebrand_compat.py` covers the state move, a blocked move,
  overrides, legacy project folders, and legacy environment and hook variables.

The browser pane checked a source run with an isolated home:

- the launch link and socket;
- the sidebar mark and wordmark;
- the gold send button;
- Settings;
- the light theme;
- a 375 px width without horizontal overflow;
- no remaining "SONN Client" or "Resonant" text.

A local PyInstaller build of `lumi.exe` passed the bundle policy (146.6 MiB,
316 files). Run against an isolated home holding a legacy `~/.resonant`:

- `--version` printed `lumi 0.19.2.dev11` and moved the folder to `~/.lumi`;
- the page was titled Lumi and served the new icons;
- the launch code was redeemed;
- the socket returned 403 without the token and 101 with it;
- the startup log was clean;
- the executable carries the Lantern icon, and Windows `LoadImageW` (used for
  the window icon) loads `lumi.ico` at 16–256 px.

WinSparkle was disabled for that local run so it would not write the real
registry; CI checks its bundling. That build predates the final text-only
command-help changes.

Not exercised: compiling the installer (no Inno Setup locally) or upgrading
an installed SONN Client, a macOS build, and live models.

## September 24 local GUI access control — source only, not released

**Security fix.** The GUI server bound to 127.0.0.1 accepted any WebSocket
without a credential. Any local process, another account on the machine, or a
web page could open the socket; WebSockets are exempt from CORS, and DNS
rebinding defeated the only Origin check. Such a client could then run
`shell_exec`, rewrite settings including hooks and MCP servers, switch the
permission mode and answer approval prompts.

- Each launch creates an access token that the server never prints or logs.
  Pages redeem a one-time code from the launch link's URL fragment at
  `/api/access`. They keep the token in origin storage, which is port-isolated
  unlike cookies, and send it as a WebSocket subprotocol or `X-Lumi-Access` header.
- `/ws` and `/api/ui-state` also require an exact `Host` (`127.0.0.1:<port>`,
  `localhost:<port>`, or a literal non-loopback bind address) and this server's
  `Origin`. A refused handshake is closed before `accept()`, and the client
  receives HTTP 403. A Host guard covers every route; the page refuses framing.
- Launch links: the desktop window opens with one; `--browser` prints one;
  **File > Open in Browser** mints one through the desktop bridge only. A page
  without access shows how to get a link instead of retrying. Pasting a new link
  into an open tab reloads it and redeems the code. Diagnostics ZIPs redact
  launch links.
- `update_settings` accepts only the fields Settings edits. Hooks, LSP servers,
  plugins, the gateway, stdio MCP servers and whole-section writes are refused.
  HTTP MCP entries are rebuilt without `command`/`args`/`env`. The Ollama setup
  wizard's `values` payload was previously ignored, so its typed URL was never
  saved; it is now validated and saved.
- `set_permission_mode` requires an explicit known mode; the backends had
  treated a missing or unknown mode as Full-auto. `approve` requires an explicit
  `true`. Wildcard `--host` binds print and probe a loopback URL.

Compatibility: bookmarks and scripts that open the page or socket without a
launch link are refused. After a restart, a tab needs the new link.

Validation on September 24, 2026: 3,416 passed / 3 skipped (baseline before the
change 3,357 / 3), 29 UI recovery checks, ruff and `git diff --check`. Live
checks used an isolated home and a scripted Ollama-compatible model, not a live
model. In the browser pane, with real keyboard events, they covered:

- the locked page at desktop and phone widths, and link redemption;
- sending a message, F5 reload with draft restore, and a dropped socket with
  automatic reconnect;
- a run that continued across a mid-run reload;
- a stale tab after a restart, recovered by pasting the new link, and a reused
  link falling back to the stored token.

Raw HTTP against the live server returned 403 for missing, wrong, cross-origin,
dev-server-origin and rebinding handshakes, and 101 with `lumi.v1` otherwise.

The desktop window (pywebview 6.1, WebView2) redeemed its code and connected.
**Open in Browser** was triggered through its click handler from the fixture's
own `evaluate_js`; operating-system input was not used. The minted link,
recorded by a stub `webbrowser.open`, connected a browser pane that sent a
message.

Not exercised: a packaged build, the real default-browser handoff, macOS/Linux
webviews and live models.

## September 24 security fixes: capability-pack trust and tool approvals

Source-only; not bundled or released. These are separate from the AI Employee pause.

**Repository capability packs could approve themselves.** The client
discovered `<project>/.resonant/packs` automatically, and a pack's own manifest
could set `trust`, `enabled` and `sha256`. Opening a cloned repository could
then connect the pack's MCP servers and register its shell hooks. The digest
covered only the manifest.

- Trust and enablement now come only from user settings. Manifest `trust`,
  `enabled` and `sha256` are ignored.
- Approval is location-bound. A repository pack can only be trusted by a user
  approval of that pack directory, so an approval never follows a copied pack
  into another repository. Pinned trust by pack id still works for packs
  outside the project.
- Every approval pins one digest covering every file in the pack (except
  `.git`) plus the repository files that its hook and MCP commands name. Any
  change turns the pack off until it is approved again. Packs with links, more
  than 4,000 files, or more than 64 MB cannot be verified.
- Pack hooks re-verify the digest before each run. Skills, agents and MCP
  servers stop contributing once the pack changes.
- Pack hooks now live on per-session runners. Opening another project
  disconnects the previous project's pack MCP servers; previously only a
  settings reload removed them.
- **Settings > Capability packs** shows what each pack would run and offers
  Approve or Revoke. If the pack changed after the list was drawn, approval is
  refused with an explanation. A banner above the composer names packs that
  are waiting for review.
- A project's `resonant-policy.json` could weaken built-in denies: an earlier
  `allow` beat, for example, Auto-edit's recursive-delete deny. Built-in denies
  are now checked first. Repository rules can still tighten the policy, and a
  policy `prompt` rule now requires approval.

**A user's Deny could run the tool.** After a Deny, the engine emitted
PERMISSION_REQUEST and read `HookResult`'s default decision, "allow", as
approval. The GUI always attaches a hook runner, so in Ask mode a denied tool
ran anyway.

- The user's answer is final, and only an explicit `true` approves.
- A missing or unknown hook decision is no decision. When no prompt can be
  shown, only an explicit allow or deny from a matching PERMISSION_REQUEST hook
  decides; otherwise the call fails closed. Arguments rewritten by a hook are
  checked against the policy again.
- Auto-edit used to run shell and MCP actions without asking. The GUI attached
  a prompt only in Ask mode, and the engine fell back to a legacy
  `auto_approve` flag. Auto-edit now asks before shell, MCP, browser, desktop,
  REPL, process and git actions, and before any new tool. `auto_approve` now
  follows the autonomy tier. Background work without a prompt, such as sprint
  roles in Auto-edit, now skips calls that need approval.
- Changing the permission mode now updates the live session's tier and policy.
  Before, switching Full-auto to Ask mid-session kept auto-approving.
- Delegated workers ask through the parent's prompt, one at a time, instead of
  auto-approving; restarted workers use their own run's prompt.
- Every prompt has a request id. Late or stale answers are ignored instead of
  approving the next request, and a missing or non-boolean `approved` no longer
  counts as approval.
- The approval dialog now takes focus when it opens: Tab reaches Deny and
  Allow, Escape denies, and focus returns to the composer. Denied shell cards
  read "not run" instead of "running…".

Validation: `ruff`, 23 Node UI tests (one new), `git diff --check`, and the
full suite: 3,405 passed, 2 skipped. The new tests are in `test_permission_decisions.py`,
`test_gui_permission_modes.py` and `test_capability_pack_trust.py`. Of these,
45 were written before the fix: 41 failed against the unfixed code, and 4
contract tests passed. Two more cover the restarted-worker prompt and dropping
servers of a pack edited after approval; they were written with their fixes
and mutation-checked. Real browser events drove the actual app, WebSocket,
engine, hook runner and approve handler. The model was scripted, no provider
was called, and the home and project were temporary. Checked there:

- Opening a repository with a self-trusting pack ran neither its hook nor its
  MCP server.
- In Auto-edit, a shell command prompted. Deny left no file; Allow ran it.
- After switching to Ask mid-session, a keyboard Deny (Tab, then Enter) and an
  Escape each left no file.
- Approving a pack edited after review was refused. Approving the current
  content started its MCP server and ran its hook on the next turn.
- Editing the approved pack stopped the hook, showed "Changed since approval",
  and brought the banner back. Revoking worked.
- The Settings page and dialog fit a 375 px viewport without horizontal
  overflow.

No live model run, packaged build or CLI-provider path was exercised.

## September 15 AI Employee source integration — paused

The user paused implementation; see the [resume handoff](ai-employees-handoff.md).
Integrated durable SONN task/advice controllers, a task panel with assignment
inspection, scoped file-only workers, isolated artifact handoffs and recovery.
Full native candidate regression: **3,351 passed / 3 skipped**. Product-side real
native-process checks use scripted providers and synthetic release artifacts; they
do not establish learned quality/cost benefit. No new bundle or release was built.
Automatic supervision, trusted outcome attribution, host enrollment and full
qualification remain open. No paid training phase is authorized.

## Prior package checkpoint

**Latest local package: 0.19.2.dev11; not a public release.** Its full suite passed
3,322 tests (two skipped), 40 focused job/editor checks, and packaged source/asset
comparison across 146 files. Later browser picker changes remain source-only;
22 UI recovery checks and isolated actual-browser interaction pass. Historical
candidate sections below retain their original results.
See [Blender qualification](blender-qualification-20260914.md).

Historical candidate 0.19.2.dev3 follows [0.19.1](v0.19.1-release-notes.md).

Native SONN desktop turns bind outgoing requests to a saved project/session
identity across model switches and reloads. Generated titles and compression
requests use independent identities with learning disabled. Generated repair
and diagnostic user turns carry explicit learning exclusions; actual tool
observations retain their original role. These controls do not grant account
access or imply that retained observations are useful lessons.

Successful edits that alternate four times between the same two text states
trigger a specific diagnostic recovery message. This does not terminate the
run, erase failures, weaken assertions, or limit ordinary repository reads.

The clean packaged candidate passed the bundle gate. The client suite passed
3,280 tests with 2 skipped, plus 19 UI recovery tests and lint. A native desktop continuation after process restart reached the same hosted
SONN learning session and retained the new request. Generated model-summary
exclusions are contract-tested; a later live continuation compacted the saved
history and resumed coding under the same hosted session. Malformed-summary
fallback was verified in a no-network replay, not exercised in that live attempt.
Delegated-worker identities and automatic learning benefit remain unqualified.
This is a local candidate, not a published release.

The Windows clean-build script checks for a running target bundle before deleting
any build files. `-ValidateOnly` exercises that guard without changing files.
An operator build interrupted the first candidate; the guard prevents that
specific packaging mistake from recurring.

Malformed or empty model summaries can now recover through a complete archived
transcript plus mechanically retained requirements, checklist and tool evidence.
The archive must be readable through the allowed artifact tool; unavailable
storage or provider errors keep history intact and stop compaction. This fallback
does not infer decisions, successful checks or task completion. The actual failed
desktop transcript shrank from 51,144 to 12,307 estimated history tokens in a
no-network replay with a deliberately malformed summary.

The guided drawing repair finished with 7/7 backend tests, nine independent
browser checks, a six-round game and durable account/score recovery. The builder
exceeded its prompted 15-tool boundary (17 observed) and was stopped; its extra
Windows launch command failed quoting. These checks do not establish unattended
completion or an enforced tool-call budget.


## September 14 long-run candidate: 0.19.2.dev5

- Optional enforced main model-request allowance, counted through recovery loops;
  reaching it retains work and permits continuation instead of asking for /clear.
- Reject active project/session replacement; fix Stop to target the active engine
  and final saves to target the captured record. Save completed step history.
- Native dev4 qualification exposed the project-switch failure; it was stopped
  before the new application was submitted. Keep it as a failed product-path test.
- Two-project native qualification is ongoing. No beta or public release claim.

September 14 dev6 candidate: classify SONN HTTP failures without leaking provider
text, explain interrupted generation, disable inherited paid-request retries, and
make SONN timeout wording explicit. Targeted adapter/identity checks:41passed.
Native two-project qualification remains in progress.

Dev7 also rejects multiline Windows shell commands before dispatch. The Windows
command runner can otherwise return zero while ignoring code after a newline.
Write a project script and invoke it with a single-line command. Four boundary
checks pass; the observed silent non-execution is preserved in qualification.

Dev8 fixes browser screenshots in SONN tool history. The gateway currently accepts
text only: retain image artifacts locally, preserve tool text, and direct the
agent to DOM/accessibility/evaluation evidence. Do not append a synthetic human
image message. The actual failed GearDesk history was rejected by the gateway
parser before this fix and accepted afterward, without changing saved history.

- SONN learning queue admission now has a bounded, cancellable cooldown/retry.
  Only the structured pre-dispatch code qualifies; uncertain paid failures remain terminal.

- Fix Blender Check editor argument compatibility with pinned blender-mcp1.9.1; live command-handler check and regression coverage.
## Installers and updates served from GitHub Pages

The release workflow now copies each stable installer to the GitHub Pages site
under `downloads/vX.Y.Z/` and points the signed appcast there, instead of at the
GitHub Release asset. This lets the source repository become private without
breaking updates for installed apps, as long as Pages stays public. The feed URL
in `updater.py` is unchanged, so no client rebuild is required. The Pages site
keeps the three newest installers and is published as one fresh commit per
release. Pre-release tags no longer reach the appcast. The installer's publisher
and support links now point to getsonn.com and the download page.

## Local dev11 long-job candidate

Adds project-owned `job_start`, `job_status`, and `job_cancel` for bounded
foreground workers such as Blender rendering. Preserves ordinary shell cleanup
and separates worker completion from artifact verification. Jobs stop on client
exit; application checkpoint recovery is explicit. This is a local candidate,
not a published release or a completed Blender qualification.

### Browser folder picker recovery
Source candidate: browser pages sharing the desktop server now open an in-page path dialog based on the page native bridge, rather than sending a native picker command. The running dev11 Blender qualification package is unchanged. Twenty-two UI recovery checks and isolated browser keyboard/cancel interaction pass.
