# The terminal UI

`lumi` with no subcommand (also `lumi-tui`) opens a chat with the agent in the
terminal, with a model from Ollama. The project is the folder it starts in,
and the same rules as in the desktop app and [`lumi run`](headless.md) apply
there. The code is `lumi/tui.py`.

```bash
lumi --dir ~/code/app
```

```bash
lumi --model qwen3-coder:30b --approve
```

## Options

| Option | Meaning |
| --- | --- |
| `--dir DIR` | The project folder (default: the current folder) |
| `--model MODEL` | The Ollama model. Otherwise Settings' default model when Ollama is the default provider, or you choose from the list |
| `--ollama-url URL` | Where Ollama runs (default: `OLLAMA_HOST`, then the address in Settings, then this computer) |
| `--approve` | Ask before changes and commands (Ask). Without it the agent doesn't ask (Bypass) |
| `--auto-plan` | Plan first when a request looks complex |
| `--max-tokens N` | The longest reply, in tokens (default 4096) |

## What the agent does without asking

| Mode | Choose it with | What happens |
| --- | --- | --- |
| Bypass | the default, or `/approve off` | Tools run without asking. |
| Ask | `--approve`, or `/approve on` | Reads and searches run; file changes, commands and everything else wait for `Allow …? [Y/n]`. |

Some calls are refused before anyone is asked, in every mode:

- the [command guardrails](shell-sandbox.md#commands-that-are-never-run);
- your organization's shell rules ([organization policy](enterprise-policy.md));
- merging, and pushing to the default branch, while agent changes wait for
  a reviewer ([code review](code-review.md));
- in Ask, the commands Auto-edit refuses as well, such as recursive deletes;
- the project's `lumi-policy.json` deny rules.

A `prompt` rule in your organization's or the project's policy asks about the
calls it matches, in Bypass too. A command your organization sends for a
[second person's approval](second-approval.md) doesn't run from the terminal,
which has no Lumi Cloud connection to ask.

Your organization's policy decides which modes you can use. If it doesn't
allow Bypass, the terminal starts in the first mode it allows (Ask or
Auto-edit, which edits files and asks about the rest) and the banner says
so. `--approve` or `/approve` for a mode it doesn't allow is refused. Only the
models it allows are offered, and a policy that is invalid or has expired
stops the terminal before it starts.

## The project

- File tools stay inside the project, and commands start in it. With the
  [shell sandbox](shell-sandbox.md) on, commands run in it or not at all.
- Excluded files (Settings > Privacy & security, your organization's policy,
  the project's `.lumiignore`) are never read, listed or sent.
- Budgets, usage records, the secret scan and the
  [audit log](audit-log.md) apply. Records name each terminal session
  `tui:<id>`.
- **Trust.** The repository's instructions (`AGENTS.md`, `LUMI.md` and the
  others), notes (`.lumi/memory.json`) and `lumi-policy.json` allow rules apply
  only to a project you trust in the desktop app
  ([project trust](desktop-workflow.md#project-trust-and-lumi-policyjson)).
  The terminal never trusts a project itself; its banner says when a
  project's own instructions and allow rules are off.
- **Hooks.** The hooks in your settings (`hooks` in `settings.json`) run as
  they do in the app, so a guard you set up applies here too. Capability
  packs aren't loaded in the terminal: no pack hooks, skills or MCP servers.

`/cd FOLDER` moves to another project. The conversation continues, and the
new folder's trust, exclusions, rules and sandbox apply.

## Commands

| Command | |
| --- | --- |
| `/help` | These commands |
| `/plan` | Plan mode: think, show the plan for your approval, then act |
| `/autoplan` | Plan first when a request looks complex |
| `/model` | Switch model (only the ones your organization allows); clears the conversation |
| `/cd FOLDER` | Change the project folder |
| `/clear` | Start the conversation again |
| `/status` | What Ollama reports |
| `/approve on`, `/approve off` | Ask before changes (Ask), or don't (Bypass) |
| `/quit` | Leave (Ctrl+D too) |
