# Desktop workflow

Applies to v0.17.2. Provider connections and the unified sidebar arrived in
v0.17.0; v0.17.1 added the compact toolbar and session rows, and v0.17.2 adds
the new-session project chooser. See [release notes](v0.17.2-release-notes.md)
and [Unreleased](unreleased.md).

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

**Search Resonant** (`Ctrl+K`) opens the command palette for actions, projects,
and sessions. It complements the sidebar's local filter.

The toolbar provides runtime status, Settings, managed project previews,
project notes, and the browser/design preview panel. Managed previews list
running project servers; the preview-panel toggle opens the adjacent viewing
surface. These are separate controls.

**Compact layout (v0.17.1):** Previews and Project notes become icon buttons
with accessible names and hover tooltips. Search has dedicated layout space so
the `Ctrl+K` hint and right-side controls do not overlap. The shortcut hint hides
on compact screens; the search button remains available.

## ChatGPT/Codex and OpenRouter

1. Install Codex CLI. Open **Settings > Connections > Sign in with ChatGPT**,
   follow the browser link, then select **Refresh account & models**. Existing
   CLI authentication is reused; API-key authentication is labeled separately.
2. Add an OpenRouter key under **Settings > API keys**, or use
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

## Notes, skills, and costs

Project notes store sourced conventions, decisions, and procedures. Changed or
missing source files invalidate notes for recall. Skills provide reusable
procedures, with project scope, pinning, and suppression controls. See
[Priority improvements](priority-improvements.md) for limits and provenance.

Codex shows remaining subscription usage when the CLI account service reports
it. OpenRouter shows provider-reported run costs and connection usage; model
picker prices are catalog prices, which can differ from actual charges.
