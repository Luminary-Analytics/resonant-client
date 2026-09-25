# Code intelligence (language servers)

The agent can ask a language server about your code with the `code_intel`
tool. The server has parsed and type-checked the project, so its answers are
more precise than a text search:

| Action | What the agent learns |
| --- | --- |
| `definition` | Where a symbol is defined. |
| `references` | Every place a symbol is used, for example before renaming it. |
| `hover` | A symbol's type and documentation. |
| `diagnostics` | A file's errors and warnings, as the server reports them. |
| `symbols` | A file's classes, functions and methods, with their lines. |

The agent names the file, the line and the symbol on it. Answers list
`file:line:column` with the line's text. In the chat, calls show as
**Code intelligence** with a count, such as "5 found" or "1 warning".

## Which server

Lumi starts one server per language per project the first time the agent
needs it, and keeps it for later questions. A server nobody has asked
anything for 10 minutes is stopped, and every server stops when Lumi exits.

Lumi uses the first of these it finds on your PATH for a file's type:

| Files | Server |
| --- | --- |
| Python | `pyright-langserver --stdio`, then `pylsp` |
| TypeScript and JavaScript | `typescript-language-server --stdio` |
| Rust | `rust-analyzer` |
| Go | `gopls` |
| C and C++ | `clangd` |
| C# | `csharp-ls`, then `omnisharp -lsp` |
| Java | `jdtls` |
| Lua | `lua-language-server` |

To use another server, or one that isn't on your PATH, add it to
`lsp_servers` in `~/.lumi/settings.json`. Servers named there come first:

```json
{
  "lsp_servers": {
    "pyright": {"command": "C:\\tools\\pyright\\pyright-langserver.exe --stdio"},
    "my-server": {"command": ["/opt/my-server/bin/serve", "--stdio"], "extensions": [".foo", ".bar"]},
    "old-gopls": {"command": "gopls", "enabled": false}
  }
}
```

`command` is the program and its arguments, as a string or a list. Give the
file types with `extensions` or `languages` (such as `["python"]`); without
either, a server whose program is one of the well-known ones above serves that
one's file types. `"enabled": false` turns an entry off.

The status popover's **LSP** tab lists the servers Lumi would use, which are
installed, which are missing for the project's languages, and which are
running or failed to start.

## Trust, exclusions and the sandbox

- **Trusted projects only.** Some servers run the project's own code
  (rust-analyzer runs build scripts; Java servers run the build), so servers
  start only in projects you trust, as automatic lint and test runs do. Trust
  a project in **Settings > Privacy & security > Project trust**. Until then
  the agent is told to use search and file reads instead.
- **Exclusions.** The agent can't ask about an excluded file, and answers
  never list places in excluded files; they say how many were left out.
  Places outside the project, such as a library's definition, are listed
  without their text.
- **Environment and sandbox.** Servers get the environment other children
  get, without Lumi's provider keys, and run in the
  [shell sandbox](shell-sandbox.md) when it's on.
- **Read only.** Lumi never asks a server to change files, and refuses a
  server's request to apply edits.

A server that fails to start is reported with the end of its output, and Lumi
doesn't try it again for a minute. When a server reports no diagnostics in
time, the agent is told the server said nothing, not that the file is clean.

## Not covered yet

- Renames, formatting, code actions and completions.
- Searching symbols across the whole project; the agent uses `grep` for that.
- Hover text comes from the server as is.
- Servers aren't managed from Lumi Cloud, and the list of well-known servers
  can't be changed by policy.
