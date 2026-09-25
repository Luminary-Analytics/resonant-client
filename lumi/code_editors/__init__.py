"""Lumi's code editor extensions: VS Code (and editors built on it) and JetBrains IDEs.

Both reach the running app through its editor bridge (gui/editor_bridge.py).
The VS Code extension in ``vscode/`` is plain JavaScript with no
dependencies, so Lumi packs the .vsix itself and installs it with the
editor's own command line (``code --install-extension``). JetBrains IDEs get
External Tools that run ``lumi editor`` commands (code_editors/cli.py).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path
from xml.sax.saxutils import escape, quoteattr

from ..processes import background_process_kwargs

VSCODE_DIR = Path(__file__).with_name("vscode")
VSCODE_FILES = ("package.json", "extension.js", "bridge.js", "README.md", "LICENSE.txt")
# Command lines of VS Code and editors built on it; each installs a .vsix the same way.
VSCODE_FAMILY = {
    "code": "VS Code",
    "code-insiders": "VS Code Insiders",
    "cursor": "Cursor",
    "windsurf": "Windsurf",
    "codium": "VSCodium",
}

_CONTENT_TYPES = (
    '<?xml version="1.0" encoding="utf-8"?>\n'
    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    '<Default Extension=".json" ContentType="application/json"/>'
    '<Default Extension=".js" ContentType="application/javascript"/>'
    '<Default Extension=".md" ContentType="text/markdown"/>'
    '<Default Extension=".txt" ContentType="text/plain"/>'
    '<Default Extension=".vsixmanifest" ContentType="text/xml"/>'
    "</Types>\n"
)


def vscode_manifest() -> dict:
    return json.loads((VSCODE_DIR / "package.json").read_text(encoding="utf-8"))


def _vsix_manifest(manifest: dict) -> str:
    """The package manifest a .vsix carries beside the extension's package.json."""
    identity = (f'<Identity Language="en-US" Id={quoteattr(manifest["name"])} '
                f'Version={quoteattr(manifest["version"])} Publisher={quoteattr(manifest["publisher"])} />')
    engine = quoteattr(manifest["engines"]["vscode"])
    return (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<PackageManifest Version="2.0.0" xmlns="http://schemas.microsoft.com/developer/vsx-schema/2011" '
        'xmlns:d="http://schemas.microsoft.com/developer/vsx-schema-design/2011">\n'
        "  <Metadata>\n"
        f"    {identity}\n"
        f"    <DisplayName>{escape(manifest['displayName'])}</DisplayName>\n"
        f'    <Description xml:space="preserve">{escape(manifest["description"])}</Description>\n'
        "    <Tags></Tags>\n"
        f"    <Categories>{escape(','.join(manifest.get('categories') or ['Other']))}</Categories>\n"
        "    <GalleryFlags>Public</GalleryFlags>\n"
        "    <Properties>\n"
        f'      <Property Id="Microsoft.VisualStudio.Code.Engine" Value={engine} />\n'
        '      <Property Id="Microsoft.VisualStudio.Code.ExtensionDependencies" Value="" />\n'
        '      <Property Id="Microsoft.VisualStudio.Code.ExtensionPack" Value="" />\n'
        '      <Property Id="Microsoft.VisualStudio.Code.ExtensionKind" Value="workspace" />\n'
        '      <Property Id="Microsoft.VisualStudio.Code.LocalizedLanguages" Value="" />\n'
        "    </Properties>\n"
        "    <License>extension/LICENSE.txt</License>\n"
        "  </Metadata>\n"
        '  <Installation><InstallationTarget Id="Microsoft.VisualStudio.Code"/></Installation>\n'
        "  <Dependencies/>\n"
        "  <Assets>\n"
        '    <Asset Type="Microsoft.VisualStudio.Code.Manifest" Path="extension/package.json" Addressable="true" />\n'
        '    <Asset Type="Microsoft.VisualStudio.Services.Content.Details" Path="extension/README.md" '
        'Addressable="true" />\n'
        '    <Asset Type="Microsoft.VisualStudio.Services.Content.License" Path="extension/LICENSE.txt" '
        'Addressable="true" />\n'
        "  </Assets>\n"
        "</PackageManifest>\n"
    )


def build_vsix(out_dir: str | os.PathLike) -> Path:
    """Pack the VS Code extension into ``out_dir``; returns the .vsix file."""
    manifest = vscode_manifest()
    target = Path(out_dir) / f"{manifest['name']}-{manifest['version']}.vsix"
    target.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", _CONTENT_TYPES)
        archive.writestr("extension.vsixmanifest", _vsix_manifest(manifest))
        for name in VSCODE_FILES:
            archive.write(VSCODE_DIR / name, f"extension/{name}")
    return target


def vscode_editors() -> list[dict]:
    """The editors in the VS Code family whose command line is on PATH."""
    found = []
    for command, name in VSCODE_FAMILY.items():
        path = shutil.which(command)
        if path:
            found.append({"command": command, "name": name, "path": path})
    return found


def install_vscode(command: str = "code") -> str:
    """Install the extension with ``<command> --install-extension``; returns what the editor printed."""
    if command not in VSCODE_FAMILY:
        raise ValueError(f"Choose one of: {', '.join(VSCODE_FAMILY)}.")
    executable = shutil.which(command)
    if not executable:
        raise RuntimeError(
            f"{VSCODE_FAMILY[command]}'s `{command}` command isn't on PATH. In {VSCODE_FAMILY[command]}, run "
            "\"Shell Command: Install 'code' command in PATH\" from the Command Palette (macOS), or reinstall "
            "with \"Add to PATH\" (Windows)."
        )
    with tempfile.TemporaryDirectory(prefix="lumi-vsix-") as folder:
        vsix = build_vsix(folder)
        try:
            result = subprocess.run([executable, "--install-extension", str(vsix), "--force"],
                                    capture_output=True, text=True, encoding="utf-8", errors="replace",
                                    timeout=180, **background_process_kwargs())
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise RuntimeError(f"Could not run {command}: {exc}") from None
    output = "\n".join(part.strip() for part in (result.stdout, result.stderr) if part and part.strip())
    if result.returncode != 0:
        raise RuntimeError(output or f"{command} --install-extension failed ({result.returncode}).")
    return output or f"Installed in {VSCODE_FAMILY[command]}."


# ── JetBrains IDEs ────────────────────────────────────────────────────

# (name, description, `lumi editor` arguments, show the console for its output)
JETBRAINS_TOOLS = (
    ("Send File to Lumi", "Add this file to your message in Lumi.", 'send "$FilePath$" --source JetBrains', False),
    ("Send Selection to Lumi", "Add the selected lines to your message in Lumi.",
     'send "$FilePath$" --lines $SelectionStartLine$-$SelectionEndLine$ --source JetBrains', False),
    ("Ask Lumi About Selection", "Add the selected lines and a question to your message in Lumi.",
     'send "$FilePath$" --lines $SelectionStartLine$-$SelectionEndLine$ --text "$Prompt$" --source JetBrains', False),
    ("Show Lumi's Changes", "List what Lumi's latest turn changed, with the differences.", "changes --diff", True),
)
# Settings folders such as PyCharm2025.2 or IntelliJIdea2026.1.
_JETBRAINS_DIR = re.compile(r"^[A-Za-z]+\d{4}\.\d+$")


def lumi_command() -> tuple[str, str]:
    """(program, arguments before ``editor``) that start this installation's Lumi."""
    if getattr(sys, "frozen", False):
        return sys.executable, ""
    return sys.executable, "-m lumi"


def jetbrains_tools_xml() -> str:
    """The External Tools file (tools/Lumi.xml in an IDE's settings) that runs ``lumi editor``."""
    program, prefix = lumi_command()
    tools = []
    for name, description, arguments, show_output in JETBRAINS_TOOLS:
        parameters = f"{prefix} editor {arguments}".strip()
        tools.append(
            f"  <tool name={quoteattr(name)} description={quoteattr(description)} showInMainMenu=\"false\" "
            "showInEditor=\"true\" showInProject=\"true\" showInSearchPopup=\"false\" disabled=\"false\" "
            f"useConsole=\"true\" showConsoleOnStdOut=\"{str(show_output).lower()}\" showConsoleOnStdErr=\"true\" "
            "synchronizeAfterRun=\"true\">\n"
            "    <exec>\n"
            f"      <option name=\"COMMAND\" value={quoteattr(program)} />\n"
            f"      <option name=\"PARAMETERS\" value={quoteattr(parameters)} />\n"
            "      <option name=\"WORKING_DIRECTORY\" value=\"$ProjectFileDir$\" />\n"
            "    </exec>\n"
            "  </tool>"
        )
    return '<toolSet name="Lumi">\n' + "\n".join(tools) + "\n</toolSet>\n"


def jetbrains_config_dirs(env: dict | None = None, home: Path | None = None,
                          platform: str | None = None) -> list[Path]:
    """Each installed JetBrains IDE's settings folder."""
    env = os.environ if env is None else env
    home = Path.home() if home is None else home
    platform = sys.platform if platform is None else platform
    if platform == "win32":
        root = Path(env.get("APPDATA") or home / "AppData" / "Roaming") / "JetBrains"
    elif platform == "darwin":
        root = home / "Library" / "Application Support" / "JetBrains"
    else:
        root = Path(env.get("XDG_CONFIG_HOME") or home / ".config") / "JetBrains"
    if not root.is_dir():
        return []
    return sorted(path for path in root.iterdir() if path.is_dir() and _JETBRAINS_DIR.match(path.name))


def install_jetbrains(dirs: list[Path] | None = None) -> list[Path]:
    """Write the Lumi External Tools into each IDE's settings; returns the files written.

    An IDE reads them when it starts, so one that is open needs a restart.
    """
    text = jetbrains_tools_xml()
    written = []
    for folder in jetbrains_config_dirs() if dirs is None else dirs:
        target = Path(folder) / "tools" / "Lumi.xml"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
        written.append(target)
    return written
