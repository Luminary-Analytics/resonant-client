# Lumi for VS Code

Use Lumi from VS Code while the Lumi app is open on the same computer.

- **Send Selection to Lumi** (editor right-click menu): adds the selected
  lines to your message in Lumi as an attachment, such as
  `@file:src/app.py#L10-24`. Several selections become several attachments.
- **Ask Lumi About Selection...**: the same, with a question you type first.
- **Send File to Lumi** (editor, tab and Explorer menus) and **Send Open Files
  to Lumi**: adds whole files.
- **Review Lumi's Changes**: lists the files the open Lumi session's latest
  change-making turn changed, and opens each one beside its earlier version.
- **Check the Connection to Lumi**: says whether Lumi is running and which
  project it has open.

Nothing is sent to a model from VS Code: the files go into Lumi's composer,
and you send the message from Lumi. Lumi reads saved files, so the extension
offers to save a file with unsaved changes first.

Files must be in the project Lumi has open. Files that project's privacy
settings exclude are neither attached nor shown.

The extension talks only to Lumi on this computer. While Lumi runs, it keeps
its local address and a token for that launch in `editor-bridge.json` in its
state folder (normally `~/.lumi`, or `LUMI_STATE_HOME`). Turning off
**Settings > Security > Code editors** in Lumi closes the bridge.

Install it from Lumi's **Settings > Code editors**, or run
`lumi editor vscode --install`. VS Code 1.75 or later.
