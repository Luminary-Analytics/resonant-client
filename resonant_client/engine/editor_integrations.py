"""First-party setup and workflow policy for opt-in community editor bridges.

Bridge programs and editor plugins remain upstream dependencies. This module
owns their configuration contract, not their tool implementations.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from urllib.parse import urlsplit


EDITORS = {
    "blender": {
        "title": "Blender", "summary": "Model scenes, materials, animation, and renders.",
        "url": "https://github.com/ahujasid/blender-mcp",
        "field": "port", "label": "Blender add-on port", "default": "9876",
        "steps": [
            "Install uv, then run: uvx blender-mcp==1.9.1 install-addon",
            "In Blender, enable MCP for Blender in Preferences > Add-ons.",
            "Open its viewport sidebar panel and start the MCP server. Connect below.",
        ],
        "probe": ("get_scene_info", {}),
        "guidance": "Inspect the active .blend and scene before edits. Preserve existing objects. "
                    "Use Blender Python only through the discovered bridge tools. Verify materials, "
                    "transforms and output with a viewport capture or render; save only to the intended project.",
    },
    "unity": {
        "title": "Unity", "summary": "Edit scenes, GameObjects, assets, and scripts; run editor tests.",
        "url": "https://github.com/CoplayDev/unity-mcp",
        "field": "url", "label": "Unity bridge URL", "default": "http://127.0.0.1:8080/mcp",
        "steps": [
            "In Package Manager > Add package from git URL, use:",
            "https://github.com/CoplayDev/unity-mcp.git?path=/MCPForUnity#v10.0.0",
            "Open Window > MCP for Unity, start the local HTTP server and connect the editor. Copy its URL below.",
        ],
        "probe": ("manage_scene", {"action": "get_hierarchy"}),
        "guidance": "Identify the Unity project and active editor instance before edits; select the "
                    "intended instance when several are open. Wait for compilation/domain reload. "
                    "Inspect console errors, run relevant EditMode/PlayMode tests, and verify the scene visually. "
                    "Do not edit Library/ or generated .meta GUIDs, and do not claim a build from source edits alone.",
    },
    "unreal": {
        "title": "Unreal Engine 5", "summary": "Inspect levels, place actors, and work with Blueprints.",
        "url": "https://github.com/chongdashu/unreal-mcp",
        "field": "directory", "label": "Unreal MCP Python folder", "default": "",
        "steps": [
            "Download or clone chongdashu/unreal-mcp from the setup guide.",
            "Copy MCPGameProject/Plugins/UnrealMCP into your project's Plugins folder; enable and build it for your UE5 version.",
            "Install uv, open that project in Unreal Editor, and select the bridge repository's Python folder below.",
        ],
        "probe": ("get_actors_in_level", {}),
        "guidance": "Inspect the open Unreal project and level before edits. Use discovered actor and "
                    "Blueprint tools; never invent nodes or APIs. Compile changed Blueprints, inspect errors "
                    "and verify in the viewport or PIE. Preserve existing assets; do not edit binary .uasset "
                    "files as text. Distinguish editor verification from a packaged game build.",
    },
}


def server_name(editor: str) -> str:
    """Return a reserved name without colliding with user-created bridge entries."""
    if editor not in EDITORS:
        raise ValueError("Unknown creative editor")
    return f"resonant_{editor}"


def build_config(editor: str, value: str, *, check_dependencies: bool = True) -> dict:
    """Build a local-only, secret-free bridge configuration from one setup field."""
    server_name(editor)
    value = str(value or EDITORS[editor]["default"]).strip()
    config = {"enabled": True, "editor_integration": editor}
    if editor == "unity":
        parsed = urlsplit(value)
        if (parsed.scheme != "http" or parsed.hostname not in {"localhost", "127.0.0.1", "::1"}
                or parsed.username or parsed.password or parsed.query or parsed.fragment):
            raise ValueError("Use the local Unity HTTP bridge URL without credentials or query parameters.")
        # Force port validation (urlsplit otherwise accepts invalid port text).
        if parsed.port == 0:
            raise ValueError("The bridge port must be between 1 and 65535.")
        return {**config, "transport": "http", "url": value}
    executable = "uvx" if editor == "blender" else "uv"
    command = shutil.which(executable)
    if check_dependencies and not command:
        raise ValueError(f"Install uv and restart Resonant so {executable} is available, then connect again.")
    if editor == "blender":
        try:
            port = int(value)
        except ValueError:
            raise ValueError("Enter a Blender port between 1 and 65535.") from None
        if not 1 <= port <= 65535:
            raise ValueError("Enter a Blender port between 1 and 65535.")
        return {**config, "transport": "stdio", "command": command or executable,
                "args": ["blender-mcp==1.9.1"],
                "env": {"BLENDER_HOST": "127.0.0.1", "BLENDER_PORT": str(port), "DISABLE_TELEMETRY": "true"}}
    directory = Path(value).expanduser()
    if not value or not directory.is_absolute():
        raise ValueError("Choose the absolute path to the Unreal MCP Python folder.")
    if check_dependencies and not (directory / "unreal_mcp_server.py").is_file():
        raise ValueError("That folder does not contain unreal_mcp_server.py.")
    return {**config, "transport": "stdio", "command": command or executable,
            "args": ["--directory", str(directory), "run", "unreal_mcp_server.py"]}


def configured_editors(settings) -> dict:
    """Return only entries explicitly installed through the built-in setup."""
    servers = settings.get("mcp_servers") if settings else {}
    return {name: data for name, data in (servers or {}).items()
            if isinstance(data, dict) and data.get("editor_integration") in EDITORS
            and name == server_name(data["editor_integration"])}


def catalog(settings, manager) -> list[dict]:
    """Describe configuration separately from live MCP connectivity."""
    configured = configured_editors(settings)
    runtime = {row["name"]: row for row in manager.list_servers()}
    rows = []
    for editor, spec in EDITORS.items():
        name = server_name(editor)
        cfg, state = configured.get(name, {}), runtime.get(name, {})
        value = spec["default"]
        if editor == "unity":
            value = cfg.get("url", value)
        elif editor == "blender":
            value = cfg.get("env", {}).get("BLENDER_PORT", value)
        elif cfg.get("args"):
            args = cfg["args"]
            if "--directory" in args and args.index("--directory") + 1 < len(args):
                value = args[args.index("--directory") + 1]
        rows.append({"id": editor, "name": name, **{k: spec[k] for k in
                    ("title", "summary", "url", "field", "label", "steps")},
                     "value": value, "configured": bool(cfg), "enabled": bool(cfg.get("enabled")),
                     "connected": bool(state.get("connected")), "tools": state.get("tools", 0),
                     "error": state.get("error", "")})
    return rows


def workflow_instructions(settings) -> str:
    """Add bounded editor guidance only after the user has enabled a bridge."""
    enabled = [data["editor_integration"] for data in configured_editors(settings).values()
               if data.get("enabled")]
    if not enabled:
        return ""
    lines = ["Creative editor integrations: use their discovered MCP tools for editor operations. "
             "First verify the open project/scene matches the user's intended workspace. "
             "A connected MCP server alone does not prove the editor is open. If unavailable, "
             "report setup requirements; do not invent successful editor actions. Editor-side scripts "
             "run with the editor's filesystem access, outside Resonant's path sandbox."]
    lines.extend(f"{EDITORS[key]['title']}: {EDITORS[key]['guidance']}" for key in enabled)
    return "\n".join(lines)


def cli_arguments(settings, kind: str) -> list[str]:
    """Pass only managed, secret-free local bridges to one CLI invocation."""
    configs = {}
    for name, data in configured_editors(settings).items():
        if not data.get("enabled"):
            continue
        # Rebuild from the constrained setup fields, never forward arbitrary env
        # or headers from generic MCP settings into process arguments.
        editor = data["editor_integration"]
        if editor == "unity":
            value = data.get("url", "")
        elif editor == "blender":
            value = data.get("env", {}).get("BLENDER_PORT", "9876")
        else:
            args = data.get("args", [])
            value = args[1] if len(args) >= 2 and args[0] == "--directory" else ""
        try:
            cfg = build_config(editor, value, check_dependencies=False)
        except (ValueError, TypeError):
            continue
        if cfg["transport"] == "http":
            configs[name] = {"url": cfg["url"]}
        else:
            configs[name] = {k: cfg[k] for k in ("command", "args", "env") if k in cfg}
    if kind == "claude-code":
        return ["--mcp-config", json.dumps({"mcpServers": {
            name: {**cfg, "type": "http" if "url" in cfg else "stdio"}
            for name, cfg in configs.items()}})] if configs else []
    result = []
    for name, cfg in configs.items():
        # JSON strings/arrays and simple inline tables are valid TOML values.
        cfg = {**cfg, "startup_timeout_sec": 30, "tool_timeout_sec": 60}
        for key, value in cfg.items():
            encoded = ("{" + ", ".join(f'{json.dumps(k)} = {json.dumps(v)}' for k, v in value.items()) + "}"
                       if isinstance(value, dict) else json.dumps(value))
            result.extend(["-c", f"mcp_servers.{name}.{key}={encoded}"])
    return result
