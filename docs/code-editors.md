# Code editors: VS Code and JetBrains IDEs

Use Lumi from the editor you write code in, while the Lumi app is open on the
same computer:

- send the selected lines, a file or every open file into your message in
  Lumi, with or without a question;
- open what Lumi's latest change-making turn changed beside your files.

Nothing reaches a model from the editor. Files are added to Lumi's composer as
[`@file:` attachments](modern-agent-runtime.md#context-broker-and-code-intelligence), such as
`@file:src/app.py#L10-24` for lines 10 to 24, and you send the message from
Lumi, where you can change it first.

## Install

Open **Settings > Code editors** in Lumi.

- **VS Code**: **Install in VS Code** packs Lumi's extension and installs it
  with VS Code's own command line. The same button appears for Cursor,
  Windsurf, VSCodium and VS Code Insiders when their command is on PATH.
  If VS Code is open, run **Developer: Reload Window** there. VS Code 1.75 or
  later.
- **JetBrains IDEs** (IntelliJ IDEA, PyCharm, WebStorm, Rider, GoLand and the
  others): **Add to JetBrains IDEs** adds External Tools to every IDE whose
  settings Lumi finds. Restart an IDE that is open.

Or from a terminal:

```bash
lumi editor vscode --install
```

```bash
lumi editor jetbrains --install
```

`lumi editor vscode --out <folder>` saves the `.vsix` instead, for
**Extensions: Install from VSIX...** or for another computer's editor;
`--editor cursor` (or `code-insiders`, `windsurf`, `codium`) chooses the
editor. `lumi editor jetbrains --out Lumi.xml` saves the tools file, which
goes in the IDE's settings folder under `tools/`. The VS Code button needs the
`code` command: in VS Code on macOS run **Shell Command: Install 'code'
command in PATH**; on Windows the installer's **Add to PATH** option adds it.

## In VS Code

In the editor's right-click menu and the Command Palette (**Lumi:**):

| Command | What it does |
| --- | --- |
| Send Selection to Lumi | Adds the selected lines. Several selections become several attachments; a selection that ends at the start of a line leaves that line out. With nothing selected, the whole file. |
| Ask Lumi About Selection... | The same, with a question you type first. |
| Send File to Lumi | Adds the whole file. Also in the Explorer and tab menus, for several selected files. |
| Send Open Files to Lumi | Adds every open file. |
| Review Lumi's Changes | Lists the files the open Lumi session's latest change-making turn changed, and opens each one, or all of them, in a side-by-side diff: the version from before that turn on the left, the file now on the right. |
| Check the Connection to Lumi | Says whether Lumi is running and which project it has open. |

Lumi reads files from disk, so when a file has unsaved changes the extension
asks whether to save it first or send the saved version.

## In JetBrains IDEs

The tools are under **Tools > External Tools > Lumi** and in the editor's
right-click menu: **Send File to Lumi**, **Send Selection to Lumi**, **Ask
Lumi About Selection** (asks for the question) and **Show Lumi's Changes**,
which prints the changed files and their differences in the Run window.
JetBrains IDEs have no side-by-side view for these yet; for that, use the
IDE's own Git changes view, or VS Code.

## From a terminal

The same commands the JetBrains tools run:

```bash
lumi editor status
```

```bash
lumi editor send src/app.py --lines 10-24 --text "Why does this fail?"
```

```bash
lumi editor changes --diff
```

```bash
lumi editor diff src/app.py
```

## What "Lumi's changes" compares with

Before the first tool call in a turn that may change files, Lumi saves a
snapshot of the project (its [checkpoints](modern-agent-runtime.md)). Review
Lumi's Changes compares each file now with that snapshot, from the most
recent turn in the open session that changed anything. So a later question
such as "explain what you did" doesn't hide the changes, and edits you made
yourself since then show up too. A snapshot is made with a temporary git
index: your staged changes and files are untouched.

Codex and Claude Code change files inside their own tools, so there is no
snapshot of their turns. For those, Lumi lists the files they reported and
compares them with the last commit. In a folder that isn't a git repository,
snapshots are zip archives, and only the files the turn reported are listed.

## What editors can do, and what they can't

- Editors reach only the Lumi app on the same computer. While it runs, Lumi
  keeps its local address and a token made for that launch in
  `editor-bridge.json` in its state folder (normally `~/.lumi`, or
  `LUMI_STATE_HOME`), readable only by you, and removes it when it closes.
- The token lets a program add files to your message and read the list of
  changed files and their earlier versions. It can't send a message, start a
  turn, change settings or reach the conversation. Requests from web pages are
  refused.
- Only files inside the project Lumi has open can be added. Files the
  project's [exclusions](enterprise-policy.md) cover are neither added nor
  shown, and a whole file over 512 KB needs a selection instead.
- **Settings > Privacy & security > Code editors** turns the bridge off; an
  organization can lock it off with `security.editor_bridge` in its
  [policy](enterprise-policy.md).

## For contributors

- `lumi/gui/editor_bridge.py`: the bridge file, the token check and the
  `/api/editor/<action>` endpoints (`status`, `context`, `changes`,
  `before`). `context` pushes an `editor_context` event to the page, which
  appends the text to the composer (`app.js`, `_applyEditorContext`).
- `lumi/engine/context_broker.py`: `@file:path#L10-24` attaches only those
  lines.
- `lumi/code_editors/`: the VS Code extension in `vscode/` (plain JavaScript,
  no dependencies; `bridge.js` holds the parts that don't need VS Code), the
  `.vsix` packer and the JetBrains External Tools, and `cli.py` for
  `lumi editor`.
- Tests: `tests/test_editor_bridge.py`, `tests/test_code_editors.py`, and
  `tests/vscode_extension.test.cjs`, which runs the extension against a
  simulated VS Code API and a stand-in bridge. Nothing in the test suite
  starts VS Code or a JetBrains IDE, or installs into one.
