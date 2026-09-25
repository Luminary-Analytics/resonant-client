# Desktop workflow

Applies to Lumi 0.19.1. Provider connections and the unified sidebar arrived in
v0.17.0; v0.17.1 added the compact toolbar and session rows, and v0.17.2 adds
the new-session project chooser. v0.18.0 adds [SONN setup](sonn.md). See [release notes](v0.19.0-release-notes.md)
and [Unreleased](unreleased.md).

## Getting started

On a first run the empty chat shows a **Get started** checklist
(`lumi/gui/onboarding.py`) with three steps. Each step reads the app's real
state and is ticked when done:

1. **Connect a model:** done when any provider lists a model. Before that,
   **Open Connections** goes to Settings > Connections (Ollama on this
   computer, API keys, Sign in with ChatGPT, custom connections).
2. **Open a project:** done when a project other than the Playground is open.
   - **Choose a folder** opens the folder picker.
   - **Try the sample project** creates
     `Documents/Lumi Projects/lumi-sample` (a tiny Python program with a bug to
     find) and opens it. An existing sample folder is never overwritten.
3. **Finish a first task:** done after the first turn that ends without an
   error (`onboarding.first_task_done`). **Use a suggested task** puts a prompt
   in the composer; nothing is sent until you send it. In the sample project the
   prompt asks Lumi to add tests, run them and fix the bug they find.

The **×** button hides the checklist for good (`onboarding.dismissed`), and it
goes away by itself once all three steps are done.

## Projects and sessions

The sidebar is one scrollable list of projects and their sessions. Use **Add
project** beside the Projects heading to open a folder. Select a project name
to activate it; its disclosure control expands or collapses saved conversations.
The project action menu offers project management, and the plus button starts
a session in that project.

**New session** (`Ctrl+N`) opens a fresh draft in the selected project. A saved
conversation is created when the first message is submitted. Selecting a saved
session restores its conversation and model choice. Drafts are scoped to their
project/session and survive navigation and reloads.

**Project chooser:** New session and `Ctrl+N` first open a searchable
list of existing projects. Select one to start a draft there, or choose
**Choose folder…** to open the desktop folder picker for another project.
Browser mode offers an absolute-path field when a native picker is unavailable.
Cancel preserves the current session and draft. The plus button beside a project
continues to start a session there directly. A new conversation appears beneath
its chosen project after the first message; no empty session is saved by opening
or cancelling the chooser.

**Browser pages (Unreleased):** the app server accepts only pages opened from
a one-time launch link. In the desktop window, **File > Open in Browser** (also
in the command palette) opens the running app in your default browser.
`lumi-gui --browser` prints a one-time link instead. A used link cannot open
another browser, but the tab it opened keeps working through reloads and
reconnects, as do new tabs of that browser at the same address, until Lumi exits. After a restart, paste the new link into an existing tab. A page
opened without a link explains how to get one. See [Unreleased](unreleased.md).

**Find projects or sessions** matches project names, paths, and session titles.
The scope selector offers **All sessions** and **Pinned**. Expanded projects
initially show six matching sessions, plus the active conversation if needed.
**Show more** reveals another 20; search can find sessions outside the visible
slice. Project/session menus remain available by keyboard.

**Compact layout (v0.17.1):** folder icons replace the text disclosure arrows.
Sessions use single-line 32-pixel rows with ellipsis for long titles. Hover to
read the full title, date, role, and pinned detail. A pin icon remains visible;
working and needs-input indicators remain visible while idle dots are hidden.
Project action buttons appear on hover or keyboard focus, and stay visible on
touch devices. The active session has a quiet background highlight.

## Search and toolbar

**Search Lumi** (`Ctrl+K`) opens the command palette for actions, projects,
and sessions. It complements the sidebar's local filter.

The toolbar provides runtime status, managed project previews,
project notes, and the browser/design preview panel. Managed previews list
running project servers; the preview-panel toggle opens the adjacent viewing
surface. These are separate controls.

**Compact layout (v0.17.1):** Previews and Project notes become icon buttons
with accessible names and hover tooltips. Search has dedicated layout space so
the `Ctrl+K` hint and right-side controls do not overlap. The shortcut hint hides
on compact screens; the search button remains available.

## Profile, settings, and Echo

Open **Profile and settings** at the bottom-left of the sidebar for **SONN account
& credits**, **Provider connections**, and **Settings**. The footer stays below the
scrollable projects. `Ctrl+,` and the application menu open Settings with the
sidebar hidden. Tab or arrow keys navigate the menu; Escape returns focus.

Set your project URL and private invitation under **Settings > Connections > Network / API keys**,
then choose **SONN account & credits > Connect / refresh SONN account**. This uses
SONN's authenticated workspace API without generating tokens or changing models.
The account identifier, available credits, reserved credits, and total charged
come from SONN. A timestamp marks the last balance check; these are snapshots.
Account reads happen on demand, not on startup. See [SONN setup](sonn.md).

SONN currently reports a user identifier, not a full name or avatar. **Profile > Display name** is an optional local label; the authenticated account identifier
remains visible in account details. ChatGPT/OpenRouter connections and their
usage are separate from the SONN account. SONN uses prepaid credits, not a claimed
subscription plan. Billing-off and test-checkout states are labeled explicitly.
Manage credits or enter the invitation in the web workspace through **Open SONN
workspace**; no token is placed in that link or copied into browser storage.

**Show Echo** enables an optional teal companion above the profile button. Hide it
from the menu, its close button, or **Settings > Pets**. This preference survives
restarts. Echo makes no model calls and sends no notifications. Its gentle working
animation respects reduced-motion preferences; chat progress remains authoritative.

## Settings navigation

Settings temporarily replaces the project sidebar with its own navigation and
**Back to app**. Returning restores the session and its draft; opening Settings
does not change the saved sidebar preference or preview layout. Pages are grouped
under Personal, Integrations, Coding, and Advanced. General separates Permissions,
Models, and Workflow; Profile, Appearance, Pets, Account, and Usage have focused
pages. Connections groups provider sign-in, Network, and API keys.

**Search settings** filters categories using setting labels and help text. It
never searches saved field values, secrets, or account data. Arrow keys navigate
categories; Enter opens one. Escape clears a search, and **Clear search** recovers
from no matches. At narrow phone widths, categories become a horizontal strip.

Text fields save when focus leaves them; switches and selects save when changed.
Background responses defer rebuilding an actively edited field. Account, editor,
usage, and diagnostic requests load on the relevant page rather than all at once.

**Appearance** offers the Dark, Light and Match system themes, density and base
font size. The server renders the saved values into the page (`gui/appearance.py`),
so they apply from the first paint and survive restarts; the desktop window
keeps no browser storage between launches. Match system follows the operating
system's light or dark setting as it changes (`static/appearance.js`). Styles use
theme tokens rather than color literals, because the light theme redefines
every token.

## Permission modes and approvals

The composer's mode menu applies immediately to the current conversation,
including a run in progress, for native providers. **Ask** runs read-only tools
and asks before everything else, including file edits and shell commands. It
refuses the commands Auto-edit refuses (a recursive `rm`, `chmod` on a system
path, a download piped into a shell) without asking, and a project's
`lumi-policy.json` can't turn its approvals off.
**Auto-edit** also accepts file edits and asks before shell, MCP, browser,
desktop and git actions, except the ones a trusted project's
`lumi-policy.json` allows ([project trust](#project-trust-and-lumi-policyjson)).
**Plan** uses Auto-edit approvals for native providers.
**Full-auto** runs everything inside the project sandbox. A project's
`lumi-policy.json` can require more approval but cannot lift a built-in
block. Codex and Claude Code can't pass an approval request to Lumi, so under
Ask and Plan they only read.

**Deny** is final: nothing, including a hook, runs the call afterward. The
approval dialog takes focus when it opens, so typing in the composer cannot
answer it. **Tab** reaches **Deny** and **Allow**, and **Escape** denies. A file
edit or new file is reviewed instead in a card in the conversation, with its
diff, **Reject** and **Accept**. The card doesn't take focus; **Shift+Tab** from
the composer reaches its buttons. Work that runs without an approval dialog,
such as background sprint roles, skips calls that need approval instead of
running them.

**Settings > Capability packs** lists packs from the project's `.lumi/packs`
(and a legacy `.resonant/packs`) and `~/.lumi/packs`, with the hooks and MCP servers each would run. Nothing
in a pack runs until you approve it there; a pack's own manifest cannot approve
it. Editing an approved pack turns it off until you review it again. When the
open project has packs waiting for review, the banner above the composer links
to that page.

**Install from Git** on the same page adds a pack from a repository:

- It takes a public https repository with a `lumi-pack.json`, a commit, tag
  or branch, and optionally a folder.
- A tag or branch is resolved to the commit it names now, and only that
  commit is fetched: depth 1, no submodules, no stored credentials.
- The pack lands in `~/.lumi/packs/<id>`, turned off. Review what it would
  run, then approve it.
- Installing a newer commit drops the old approval.
- **Remove** deletes a pack installed this way.
- The audit log records `extension.install` and `extension.remove` with the
  commit.
- Symbolic links are refused, and the pack's manifest must have its own `id`.
Settings follows the app theme: dark, light or match system.

## Project trust and lumi-policy.json

A project can bring instruction files (AGENTS.md, LUMI.md, CLAUDE.md and
similar), notes, a codebase summary and a `lumi-policy.json` (or a legacy
`resonant-policy.json`). Lumi uses them only after you choose **Trust this
project** in the banner or in **Settings > Project trust**.

Each rule in the policy names a tool and, optionally, patterns for its
arguments. The first rule that matches a call decides:

- `deny` refuses the call and `prompt` asks before it, in every mode and even
  before you trust the project, since they only make Lumi more careful.
- `allow` runs the call without asking in **Auto-edit** and **Plan**, once you
  trust the project. **Ask** still asks, and **Full-auto** doesn't ask anyway.

```json
{"rules": [
  {"tool_pattern": "bash", "action": "prompt", "arg_globs": {"command": "npm run deploy*"}},
  {"tool_pattern": "bash", "action": "allow", "arg_globs": {"command": ["npm test", "npm run lint*"]}}
]}
```

Some things come before a project's rules, so no `allow` reaches them: the
command guardrails, your organization's rules, and Auto-edit's own refusals
(a recursive `rm`, `chmod` on a system path, a download piped into a shell).
An allowed shell command still asks when it chains, pipes, substitutes or
redirects anything (`;`, `&`, `|`, `<`, `>`, backquotes, `$(` or a line
break), because a pattern like `npm run lint*` would match whatever follows.

If the file changes after you trusted the project, including an edit the
agent makes, its `allow` rules are off until you trust the new version.
Projects that were already in Recent projects when trust arrived are trusted,
but their `allow` rules wait for your review once. Each call an `allow` rule
runs is recorded in the [audit log](audit-log.md) as an approval by
`project_policy`.

If a rule has a mistake, such as `arg_patterns` that isn't an object of
regular expressions or an action other than `allow`, `prompt` or `deny`, Lumi
skips that rule and all of the file's `allow` rules until it's fixed. Its
valid `deny` and `prompt` rules still apply. A file that isn't JSON, or isn't
an object with a `rules` list, is ignored. The log names each mistake, and
your organization's rules apply either way.

## Anthropic, OpenAI and custom connections

1. Add an Anthropic or OpenAI key under **Settings > Connections > API keys**,
   or set `ANTHROPIC_API_KEY` / `OPENAI_API_KEY`, then check the connection to
   list the models the key can use.
2. **Custom connections** adds a gateway, Azure OpenAI, or Claude on Bedrock or
   Vertex AI. The form shows only the fields the chosen type and
   **Authentication** need; OAuth client credentials, Microsoft Entra ID and
   client certificates are described in
   [Signing in to enterprise model endpoints](connection-sign-in.md). **Test
   connection** signs in, checks credentials and lists models; saving (the
   button or Enter) closes the form and puts the models in **Models** under the
   connection's name.
3. A connection that is in use by a running turn cannot be edited or removed
   until the run finishes or stops. Removing a connection also deletes its key.
4. **This endpoint keeps no prompts or responses** marks a connection covered
   by a zero data retention agreement (a "Zero retention" badge). An
   organization policy that requires zero retention allows only these
   connections, local models and the providers it names.

## ChatGPT/Codex and OpenRouter

1. Install Codex CLI. Open **Settings > Connections > Sign in with ChatGPT**,
   follow the browser link, then select **Refresh account & models**. Existing
   CLI authentication is reused; API-key authentication is labeled separately.
2. Add an OpenRouter key under **Settings > Connections > API keys**, or use
   `OPENROUTER_API_KEY`. Under **Connections**, check the connection and refresh
   models. OpenRouter usage is separate from the ChatGPT subscription.
3. Open **Models** beside the composer to search providers and star favorites.
   The adjacent quick selector includes favorites, the current selection, and
   a small initial selection from each provider.
4. Check **Use for new sessions in this project** to save a project default.
   Leave it unchecked for a session-only override. Saved conversations retain
   their own choice; new sessions return to the project preference.

Search **Astra** for `gpt-6-astra` under ChatGPT/Codex or
`openai/gpt-6-astra` under OpenRouter. Codex account discovery determines the
available account catalog. Bootstrap entries are not an entitlement guarantee.
Adding Astra does not change existing defaults.

Finish or stop the active run before changing providers. History and drafts are
retained, but Codex receives a text handoff rather than the original provider's
native session. Image attachments are not included in that handoff. The current
workflow uses manual provider selection, not automatic cross-provider fallback.

Since 0.19.1, the Codex adapter streams progress messages and tool activity while
the CLI runs. Successful file-change events populate the completion card;
recognized standalone test/lint commands include their CLI exit result and
file fingerprints. Missing, failed, or stale checks remain visible as unverified.
This fixes delayed output and false missing-edit warnings after switching from
SONN to Codex; it does not rewrite previously saved completion cards.

## Notes, skills, and costs

Project notes store sourced conventions, decisions, and procedures. Changed or
missing source files invalidate notes for recall. Skills provide reusable
procedures, with project scope, pinning, and suppression controls. See
[Priority improvements](priority-improvements.md) for limits and provenance.

Codex shows remaining subscription usage when the CLI account service reports
it. OpenRouter shows provider-reported run costs and connection usage; model
picker prices are catalog prices, which can differ from actual charges.

## Large repositories

The codebase index (`lumi/engine/rag.py`) gives the model a repo map and
search over files, symbols and imports. On a large monorepo:

- **What gets indexed.** In a Git repository, or a folder inside one, the
  file list comes from Git, so anything `.gitignore` excludes stays out.
  `vendor`, `node_modules`, build output and hidden folders are skipped even
  when tracked. So are files you exclude (Settings > Privacy & security, or
  `.lumiignore`). A repository in your home folder itself (dotfiles) isn't
  used for projects under it.
- **The cap.** The index stops at 100,000 files and says so. Exclude trees
  you don't work in to bring more of the rest in.
- **Changes are cheap.** Only files whose size or modification time changed
  are read again, each read once and parsed once.
- **What it costs.** Measured with `scripts/benchmark_index.py` on Windows,
  on September 25, 2026:

| Repository | First index | Nothing changed | 1% changed | Search | Repo map |
| --- | --- | --- | --- | --- | --- |
| 20,000 files plus 20,000 ignored | 15 s | 0.5 s | 0.8 s | ~30 ms | 0.1 s |
| 100,002 files (the cap) | 76 s | 2.5 s | 4.0 s | ~190 ms | 0.9 s |

  Most of a first index is reading each file for the first time: on this
  machine, antivirus scans a newly written file. Re-indexing everything
  from files already read took 4.6 s for 20,000 files and 23 s for
  100,000. The saved index is about 10 MB and 49 MB.

To work in one part of a monorepo, open that folder as the project. The
index, and what the agent's tools see, then start there.

## Conversation progress, suggestions, and titles

Available in [0.18.2](v0.18.2-release-notes.md). The working
status follows the latest assistant output at the bottom of the active turn.
It stays below streamed text and tool details. Scrolling up to read history does
not pull you back down; the new-messages button returns to the current work.
Session rows are slightly indented beneath their project names.

After a completed turn, an empty composer can show a suggested next prompt.
**Tab** accepts it as an editable draft; **Enter** sends only after acceptance.
Type your own message to ignore it, or press **Escape** to dismiss it.
**Shift+Tab** continues normal keyboard navigation. Existing text and attachments
are preserved. Suggestions are transient and scoped to the current conversation;
they are not regenerated when replaying history. An accepted suggestion is saved
like any other draft.

Suggestions are chosen locally from the last reply's explicit next step, change
summary, or recommendation. They do not use an additional model request and are
not a claim that a model has planned or authorized the next action. Offering to
review changes, like the task card's **Changed files**, counts only edits whose
own result succeeded: an edit you reject, that a policy blocks, that fails or
that never ran doesn't count.

When the agent hands work to a worker (`task`), the worker's block in the
task's activity ends with its result, for example "✓ build · 2 steps · 12.2s ·
1 file changed". Open that line to read the worker's handoff: the files it
changed, its checks, any blockers and its suggested next step. Select a file,
or focus it and press **Enter**, to open it. A failed or blocked worker's
handoff opens by itself.

While a worker runs, open the run details ("Working for …") to find it under
**Sub-tasks** with **Pause** (then **Resume**), **Stop** and **Steer…**. Steer
sends it a direction without stopping it; it reads it before its next step.
Once a worker has stopped, its block offers **Transcript**: its messages, each
tool call with its result, your steering, and errors. A worker that failed,
was stopped, or was interrupted when Lumi closed also offers **Restart**, which
runs its assignment again as a new turn (after the current run finishes); the
original keeps its transcript and is marked restarted. After a reload, a turn
that Lumi closed during shows as interrupted, with its work under **Work
details**.

New sessions also get a short task title from the first prompt. A local title
appears immediately. With native model connections, a small tool-free request
refines it after the first turn; slow or failed requests keep the local title.
This uses the chosen model and can incur provider usage. Codex and Claude Code
use the local title without starting a separate CLI run. You can rename any
session yourself; automatic naming never overrides a manual title.

## Undoing changes: the Timeline

Before each change its tools make (writing or editing a file, a command that
may change files, a commit or new branch), Lumi saves a checkpoint of the
project's files and the conversation. **Timeline** in the chat header lists the
open conversation's checkpoints, newest first, by what each was saved before:
"Before writing notes.txt", "Before running npm test". It is also in the
command palette and the session's menu. In a Git project, **Compare** shows
what has changed since a checkpoint.

**Restore…** asks what to put back before it does anything:

- **Files**: the project's files go back to that point. Your current files are
  kept first, on a `lumi-recovery/…` branch in a Git project or in a recovery
  archive otherwise; the message after the restore says where.
- **Conversation**: the conversation goes back to that point, and later
  messages leave it. Your files don't change.
- **Files and conversation**: both.

A restore waits for the current run to stop. A checkpoint a worker saved
restores files only, since the conversation it holds is the worker's. The chat
notes each restore where it happened; send a message to carry on from there.
Checkpoints belong to the saved conversation, so its Timeline is still there
after Lumi restarts. Codex and Claude Code change files with their own tools,
so their changes have no checkpoints. **Settings > Checkpoints & recovery**
also lists a Git project's checkpoints.

## Creative editors

Settings > Creative editors provides guided Blender, Unity, and Unreal Engine 5
connections. Connect a running bridge, check its open scene, and return to chat.
Disable removes it from subsequent turns; reconnect after restarting Lumi.
See [setup, model support, and validation](creative-editors.md).
