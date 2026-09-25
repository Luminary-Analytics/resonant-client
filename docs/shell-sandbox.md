# Shell sandbox and command guardrails

Lumi limits what the agent's commands can do in three layers:

1. **Path checks, always on.** File tools (read, write, edit, search) stay
   inside the project (`lumi/engine/sandbox.py`), and commands start in it.
   This applies to `lumi run` and the [terminal UI](terminal-ui.md) too.
2. **Guardrails, always on.** A short list of commands is never run, in any
   permission mode (`lumi/engine/guardrails.py`).
3. **The shell sandbox, off by default.** On macOS and Linux, commands can
   write only to the project and temporary folders
   (`lumi/engine/os_sandbox.py`).

Codex and Claude Code run their own tool loops, so none of these apply
inside them; Settings > Privacy & security can turn them off.

## Commands that are never run

| What | For example |
| --- | --- |
| Deleting the whole file system or your home folder | `rm -rf /`, `rm -rf ~`, `Remove-Item -Recurse C:\`, `rd /s /q C:\`, `del /s /q C:\*` |
| Changing permissions on the whole file system | `chmod -R 777 /`, `chown -R me /` |
| Formatting or partitioning disks | `mkfs.ext4 /dev/sda1`, `format C:`, `diskpart` |
| Writing raw data over a disk | `dd if=/dev/zero of=/dev/sda` |
| A fork bomb | `:(){ :\|:& };:` |
| Shutting down or restarting | `shutdown`, `reboot`, `halt`, `poweroff`, `Stop-Computer`, `Restart-Computer` |

They're refused before any approval prompt, in every permission mode and
ahead of organization and repository rules, so no `allow` rule reaches them.
They're refused again just before a command, check, job or preview starts, in
case a hook rewrote the command after it was approved. The model is told to
ask you to run the command yourself if it's really needed.

The match is on the command's text: `rm -rf ./build` and `echo shutdown`
run, and a command written to hide from the list can get past it. The list
catches mistakes. The shell sandbox is what limits where commands can write.

## The shell sandbox

**Settings > Privacy & security > Shell sandbox** chooses where the agent's
commands can write:

- **Anywhere you can** (the default).
- **Only the project and temporary folders.** An organization's policy can
  require this with `"security.shell_sandbox": "project"`.

With the sandbox on, these run inside it: shell commands (`bash`), checks
(`check_run`), managed jobs (`job_start`) and previews (`preview_start`).

- **Writable:** the project folder and any other folder the session may
  write to, the temporary folders (the system's, `$TMPDIR`, `/tmp`,
  `/var/tmp`, and on macOS `/private/var/folders`), and the terminal and
  null devices.
- **Read-only:** everything else, including Git's own folder (`.git`) in
  the project. Hooks and settings written there would run outside the
  sandbox the next time anyone commits. The agent's Git tools
  (`git_commit`, `git_branch_create`) work; `git commit` in the shell
  doesn't.
- **Unchanged:** reading files, running programs and the network.

| Computer | How | What it needs |
| --- | --- | --- |
| macOS | `sandbox-exec` with a Seatbelt profile | Nothing; it's part of macOS |
| Linux | `bwrap` (bubblewrap) | The `bubblewrap` package and unprivileged user namespaces. Ubuntu 24.04 limits those through AppArmor; allow them with `sudo sysctl -w kernel.apparmor_restrict_unprivileged_userns=0` or an AppArmor profile for `bwrap` |
| Windows | Not available yet | |

Settings says whether a sandbox can run on this computer and, if not, why.
**When the sandbox is on and can't run, the agent can't run commands,
checks, jobs or previews.** Each is refused with the reason, and nothing
runs unprotected.

### What to expect

- **Caches in the home folder.** npm, pip, cargo and similar tools can't
  write their caches. Point them at a temporary folder or the project, for
  example `npm_config_cache`, `PIP_CACHE_DIR` or `CARGO_HOME`.
- **Services outside the sandbox.** A program that asks something outside
  the sandbox to act isn't confined by it: the Docker daemon, a database
  server, or `ssh` to another computer.
- **Other tools.** MCP servers, hooks, and the Git and GitHub tools run
  outside the sandbox, as before.

## For administrators

```json
{
  "schema": "lumi.policy/v1",
  "organization": "Example Corp",
  "settings": {"security.shell_sandbox": "project"}
}
```

The value must be `"off"` or `"project"`; any other value makes the policy
invalid, which blocks requests until it's fixed. On Windows computers the
policy stops the agent from running commands, so apply it to macOS and
Linux computers, or accept that. See [organization policy](enterprise-policy.md).

## What has been checked

- `tests/test_guardrails.py`: each listed command and everyday lookalikes;
  every tier, a repository's and an organization's `allow` rules; the check
  before a command, job or preview starts.
- `tests/test_os_sandbox.py`: the setting, refusing commands when the sandbox
  can't run, the bubblewrap arguments and the Seatbelt profile. Its live test
  writes in the project and temporary folder, and fails to write in the home
  folder and `.git`. It runs in CI on Ubuntu (bubblewrap) and macOS
  (Seatbelt) in `.github/workflows/sandbox.yml`, where it passed on
  September 25, 2026, and is skipped on Windows.
