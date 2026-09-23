# Creative editors

For current local-candidate evidence, see [September 14 Blender qualification](blender-qualification-20260914.md).
Published editor setup and local dev11 managed-job capabilities have separate
release status. A readable export or a completed render manifest is not complete
scene, motion, recovery or autonomous-build qualification.

SONN Client provides built-in setup for **Blender**, **Unity**, and **Unreal Engine 5**
under **Settings > Creative editors**. The connection lifecycle, model tools,
screenshots, and workflow instructions are integrated into SONN Client. The actual
editor plugins and MCP servers are maintained by their upstream projects.

Available in [SONN Client 0.19.0](v0.19.0-release-notes.md).

## Connect an editor

1. Install the editor and its bridge using that card's Setup instructions.
2. Open the intended project in the editor and start its bridge.
3. Enter its local connection value and choose **Connect**.
4. **Bridge connected** means MCP initialization and tool discovery succeeded.
   Choose **Check editor** to read the open scene or level. Inspect that response
   before asking the agent to modify it; it may contain an editor-side error.
5. Return to chat and describe the scene, asset, or game change you want.

Reconnect from these cards after restarting SONN Client. Saved entries are not
automatically launched during app startup, so editor discovery cannot delay
saved-project navigation. **Disable** disconnects the bridge and excludes it
from subsequent turns. Connection changes and checks wait for the active run
to finish. Unrelated manually configured MCP servers remain separate.

## Blender

Requires Blender and [Blender MCP](https://github.com/ahujasid/blender-mcp).
SONN Client pins the server to `blender-mcp==1.9.1` and disables its telemetry.

- Install [uv](https://docs.astral.sh/uv/getting-started/installation/), then run
  `uvx blender-mcp==1.9.1 install-addon`.
- Enable **MCP for Blender** in Blender's Preferences > Add-ons. Open the
  viewport sidebar panel and start the server.
- Use port `9876`, or enter the add-on's chosen port in SONN Client. The host is
  always `127.0.0.1`. First connection can download the pinned server through uv.

The scene check invokes `get_scene_info`. Discovered tools cover scene/object
inspection, Blender Python, materials, and viewport captures. Asset-service
tools depend on upstream configuration; SONN Client does not supply their keys.

Try: “Inspect the active scene, create a low-poly crate beside the origin, and
verify its dimensions and materials with a viewport capture.”

## Unity

Uses [MCP for Unity](https://github.com/CoplayDev/unity-mcp). In Unity Package
Manager, choose **Add package from git URL**:

```text
https://github.com/CoplayDev/unity-mcp.git?path=/MCPForUnity#v10.0.0
```

Open **Window > MCP for Unity**, start the local HTTP server, and connect the
editor. Copy its URL into SONN Client; the default is `http://127.0.0.1:8080/mcp`.
The built-in profile accepts only loopback HTTP URLs without credentials or
query strings. Remote/authenticated servers belong in generic MCP settings.

The scene check invokes `manage_scene` with `action: get_hierarchy`. Standard
MCP resources are also exposed to native models through
`resonant_list_resources` and `resonant_read_resource`, including editor state
and instance discovery. With several Unity instances, the agent must select
the intended one using the bridge's discovered instance tool before edits.

Try: “Inspect this Unity scene, add a controllable player, wait for script
compilation, check console errors, and run the relevant PlayMode tests.”

## Unreal Engine 5

Uses [Unreal MCP](https://github.com/chongdashu/unreal-mcp). Download or clone
that repository, then copy `MCPGameProject/Plugins/UnrealMCP` into your project's
`Plugins` folder. Enable and build the plugin for your installed UE5 version,
following the upstream guide. A compatible C++ toolchain may be required.

Install uv, open the project in Unreal Editor, and enter the absolute path to
the bridge repository's **Python** folder in SONN Client. It must contain
`unreal_mcp_server.py`. SONN Client invokes `uv --directory <folder> run
unreal_mcp_server.py` as an argument vector, including paths with spaces.
The local checkout determines the Unreal bridge revision; SONN Client does not
silently clone, upgrade, or rebuild it.

The scene check invokes `get_actors_in_level`. Available actor, viewport, and
Blueprint operations come from the connected server's actual tool catalog;
support across UE5 versions depends on the editor plugin build.

Try: “Inspect the current level, build a small obstacle course using existing
assets, compile modified Blueprints, and verify the result in PIE.”

## Models, permissions, and evidence

- Ollama, EXO, Kimi, OpenRouter, and SONN use SONN Client's native MCP execution.
  Tool and vision capability remains dependent on the selected model. MCP
  images enter the existing image-result path; text-only models cannot see them.
- Enabled built-in bridges are passed to **Codex** using per-process `-c`
  overrides and to **Claude Code** using `--mcp-config`. Their global settings
  are not edited. These CLI handoffs occur only in **Full-auto** because their
  non-interactive adapters cannot relay editor-action approvals to SONN Client.
  The CLIs retain ownership of their own tool execution and policies.
- Suggest and Auto-edit modes require approval for native MCP actions; Full-auto
  uses the existing auto-approval behavior. Editor scripts run with the editor's
  filesystem access, **outside SONN Client's project path sandbox**.
- The agent receives bounded instructions to identify the project before edits,
  preserve assets, wait for compilation, and verify the result in the editor.
  A connected bridge, source edit, successful scene check, and packaged game
  build are distinct evidence.
- Tool discovery follows up to 20 pages. Stdio requests wait at most 60 seconds;
  HTTP has a 3-second connection and 60-second read timeout. Failed transport
  calls disconnect and are never automatically retried: an editor operation
  may already have run, so inspect its state before retrying. Cancellation
  cannot roll back work already accepted by an editor.

## Validation recorded September 13, 2026

- Full regression suite: **3,222 passed, 2 skipped**. All 11 Node UI tests,
  Ruff, JavaScript syntax checks, and 668 relative documentation links passed.
- Clean Windows bundle: **55.6 MiB, 256 files**. The packaged app passed HTTP,
  WebSocket, changed-asset equality, editor catalog, connection/probe/disable,
  and browser-control checks with clean logs. This is an unreleased local build.

- Live **Blender 5.1 + blender-mcp 1.9.1**: initialized and discovered 28 tools,
  inspected a factory scene, created `ResonantFixtureCube` through Blender
  Python, and verified it through `get_object_info`. A separate owned Blender
  process and isolated fixture were used; personal projects were not modified.
- Browser controls exercised against an isolated local Unity MCP fixture:
  Connect, scene check, Disable, validation errors, keyboard input, and compact
  layout. This is transport/UI evidence, not a live Unity editor evaluation.
- Automated checks cover configuration, CLI handoff and permission modes,
  MCP notification handling, pagination, bounded startup, and screenshot results.
- Live Unity/UE5 scene changes, Blueprint compilation, Unity domain reloads,
  and model-driven editor tasks remain unverified. CLI handoff is covered by
  command-construction tests, and the installed Codex CLI accepted the generated
  Unity profile in a configuration-listing check. No billable CLI/model turn was
  run for this work.

## September14 Blender qualification follow-up

A live blender-mcp1.9.1 check showed that get_scene_info requires user_prompt.
The local dev10 candidate supplies the explicit Check editor action; the old
empty-argument probe failed schema validation despite a connected bridge.
The repaired production command handler passed against Blender5.1.2;23editor/MCP
regression checks pass. Model-driven project builds and long-render recovery
remain separate qualification gates.
## Long render jobs (local dev11 candidate)

Use `job_start` with the actual foreground Blender worker command for long
renders. Ordinary `bash` tools clean up their child processes when they finish;
changing detached-process flags does not provide a supported job lifetime.
Managed jobs return promptly, expose bounded logs through `job_status`, and
support `job_cancel`. There is one running job per project and a maximum
20-minute deadline. Jobs survive browser reconnects, but the native client
process owns their trees and stops them on exit. Store resumable manifests in
the project, inspect checkpoints after restart, then explicitly start the
worker again. No automatic command replay or arbitrary PID adoption occurs.
An exit code of zero does not prove that rendered artifacts meet the task.
