# Writing a capability pack

A capability pack bundles things Lumi can use: agents, skills, lifecycle hooks,
MCP servers and model providers, plus metadata. It is a folder with a
`lumi-pack.json` manifest. Code: `lumi/engine/capability_packs.py`. To add a
model provider, start from [Extensions](extensions.md), which has a
template, an SDK and a checker.

Nothing in a pack runs until a person approves it, and a manifest can't
approve itself. See [how trust works](#trust) below.

## Where packs live

| Location | Scope |
|---|---|
| `<project>/.lumi/packs/<pack>/` | The project. The repository ships it; each person approves it at that location. |
| `~/.lumi/packs/<pack>/` | Personal: yours in every project. **Install from Git** puts packs here. |
| A folder named by `plugins.<id>.path` in settings.json | Personal |

## The manifest

```json
{
  "id": "review-helpers",
  "name": "Review helpers",
  "version": "1.2.0",
  "description": "A reviewer agent, a review skill and a start hook.",
  "agents": ["agents/reviewer.md"],
  "skills": ["skills/review.md"],
  "hooks": [
    {"hook_type": "session_start", "command": "python hooks/start.py"},
    {"hook_type": "pre_tool_use", "tool_name": "bash", "command": "python hooks/check.py",
     "input_format": "json", "timeout_seconds": 10}
  ],
  "mcp_servers": {"docs": {"command": "python", "args": ["servers/docs.py"]}},
  "permissions": ["read_project"],
  "commands": [], "recipes": [], "ui_panels": [],
  "metadata": {"homepage": "https://example.com/review-helpers"}
}
```

| Field | Meaning |
|---|---|
| `id` | Letters, digits, `.`, `_` and `-`, up to 80 characters. It names the pack in settings, in policy (`extensions.allowed_packs`) and as its folder when installed from Git (required there). |
| `name`, `version`, `description` | Shown in Settings > Capability packs. |
| `agents` | Paths inside the pack to agent files, described below. The `task` tool can delegate to them by name. |
| `skills` | Paths to skill files. Skills that match the conversation are offered to the model, which loads them with `skill_view`. |
| `hooks` | Commands run at lifecycle events, described below. |
| `mcp_servers` | Named MCP servers, `{"command", "args", "env"}` or `{"url"}`. They are registered as `<pack id>-<name>`. |
| `permissions`, `commands`, `recipes`, `ui_panels`, `metadata` | Shown for review and listed in the pack catalog. They grant nothing by themselves. |
| `manifest_version`, `lumi` | The Extension SDK's manifest version (1) and the Lumi versions the pack works with. A pack without `manifest_version` is read as version 0. See [Extensions](extensions.md#the-manifest). |
| `providers` | Model providers Lumi starts for requests to their models. Only personal packs provide them. See [Extensions](extensions.md). |

A pack's files may not reach outside its folder. Agents and skills are read
only from inside the pack, and symbolic links make it unverifiable.

### Agent files

Markdown with front matter:

```markdown
---
name: pack-reviewer
description: Reviews a change for bugs and missing tests
tools: [file_read, grep, git_diff]
model_role: review
max_steps: 20
---
You review changes independently. Report file:line, severity and a fix.
```

`tools` limits what the agent may call. `model_role` picks a model from
Models for roles. `isolation` and `model` are optional.

### Skill files

Plain Markdown. A `description:` line near the top is what the model sees
first, and the rest loads on demand.

### Hooks

| Field | Meaning |
|---|---|
| `hook_type` | `session_start`, `session_end`, `user_prompt_submit`, `pre_tool_use`, `post_tool_use`, `pre_tool_batch`, `post_tool_batch`, `before_model`, `after_model`, `permission_request`, `subagent_start`, `subagent_stop`, `task_created`, `task_completed`, `pre_compact`, `post_compact`, `checkpoint_created`, `checkpoint_restored`, `validation_complete`, `user_input_request`, `worktree_create`, `worktree_remove`, `session_error` |
| `command` | The shell command, run in the project. |
| `tool_name`, `matcher` | For tool hooks: which tool triggers it. |
| `input_format` | `env` (default; event values in environment variables) or `json` (the event on standard input). |
| `timeout_seconds` | How long the hook may run, in seconds. Default 30. At the limit Lumi stops the command and everything it started. |

Hooks in the `hooks` list of `settings.json` take the same fields.

With `json`, a hook may answer with JSON on standard output: `decision`
(`allow`, `ask` or `deny`), `reason`, and `additional_context` for the model.

#### Gate hooks fail closed

Seven hook types decide whether something happens. A hook of one of these
types that exits non-zero, runs past its `timeout_seconds` or can't be started
blocks it, and the reason names the hook:

| Hook type | A block means |
|---|---|
| `pre_tool_use` | The tool call doesn't run. The model reads the reason as the call's result. |
| `pre_tool_batch` | The `task_batch` call doesn't run, as above. |
| `permission_request` | The call is denied. These hooks are asked only when nobody can answer an approval. |
| `before_model` | The model request isn't made, and the turn ends with an error. |
| `task_completed` | The result isn't accepted. After a non-zero exit or a `deny`, the model is asked to address the reason. After a timeout or a failure to start, the turn ends with an error instead, since the model can't fix the hook. |
| `subagent_stop` | The worker's handoff is marked failed. |
| `validation_complete` | The model is told the validation gate rejected completion. |

When a hook of any other type fails, Lumi logs it and carries on.

Give a gate hook that does slow work, such as running tests, a
`timeout_seconds` longer than that work takes. A hook that runs out of time is
stopped, and what it guards is blocked.

Hook commands get a clean environment without Lumi's model keys. An `env` hook
finds the tool call's arguments in `LUMI_TOOL_ARGS`. Linux allows 128 KiB per
environment value, so there a larger call can't start an `env` hook, and a gate
hook then blocks it. A `json` hook reads the arguments from standard input, and
`LUMI_TOOL_ARGS` is empty for it when they're larger than 64 KiB.

## Trust

- **Approval pins content.** Settings > Capability packs shows what a pack would
  run: its hooks, its MCP servers, its model providers, and repository files
  its commands name.
  Approving it records a digest of every file in the pack plus those files.
- **Any change turns it off.** Before each hook runs, before each request to
  one of its model providers, and before the pack contributes skills, agents
  or servers, Lumi checks the digest again. A changed pack stays off until it
  is reviewed again.
- **Location-bound.** An approval covers the pack at that folder only. Copying a
  pack elsewhere needs a new approval.
- **Organization limits.** A policy can limit packs by id
  (`extensions.allowed_packs`) and Git sources by URL
  (`extensions.allowed_sources`). See [Organization policy](enterprise-policy.md).

## Sharing a pack

Put the pack in a public Git repository, with `lumi-pack.json` at the root or
in a folder. Anyone can then install it with **Settings > Capability packs >
Install from Git**, pinned to a commit, tag or branch. The pack arrives turned
off, and installing a newer commit asks for a new review. See the
[desktop workflow](desktop-workflow.md).
