"""``lumi editor``: use the open Lumi app from a terminal or a JetBrains IDE.

    lumi editor status
    lumi editor send FILE [FILE ...] [--lines 10-24] [--text "question"]
    lumi editor changes [--diff]
    lumi editor diff FILE
    lumi editor vscode [--install] [--editor code] [--out FOLDER]
    lumi editor jetbrains [--install] [--out FILE]

``status``, ``send``, ``changes`` and ``diff`` talk to the Lumi app that is
running, through its editor bridge (gui/editor_bridge.py). ``send`` adds files
to the message in Lumi's composer; the person sends it from Lumi.
"""

from __future__ import annotations

import argparse
import difflib
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlencode, urlsplit

from . import VSCODE_FAMILY, build_vsix, install_jetbrains, install_vscode, jetbrains_tools_xml

NOT_RUNNING = ("Lumi isn't running, or its Code editors switch (Settings > Privacy & security) is off. "
               "Open Lumi, then try again.")
STATUS = {"added": "new", "deleted": "deleted", "renamed": "renamed", "modified": "changed"}


class EditorError(Exception):
    """A failure to report to the person, without a traceback."""


def _bridge() -> dict:
    from ..gui.editor_bridge import read_bridge

    bridge = read_bridge()
    if bridge is None:
        raise EditorError(NOT_RUNNING)
    parts = urlsplit(str(bridge["url"]))
    # The token and the files go only to Lumi on this computer.
    if parts.scheme != "http" or parts.hostname not in ("127.0.0.1", "localhost", "::1"):
        raise EditorError("Lumi's bridge file doesn't point at this computer.")
    return bridge


def call(method: str, route: str, body: dict | None = None, *, raw: bool = False):
    """Call the running app; parsed JSON, or bytes with ``raw``."""
    bridge = _bridge()
    data = None if body is None else json.dumps(body).encode("utf-8")
    headers = {"Authorization": f"Bearer {bridge['token']}", "Accept": "application/json"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(str(bridge["url"]).rstrip("/") + route, data=data, method=method,
                                     headers=headers)
    # Never through a proxy: the app is on this computer.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=60) as response:
            payload = response.read()
    except urllib.error.HTTPError as exc:
        try:
            message = json.loads(exc.read()).get("error") or ""
        except (ValueError, AttributeError):
            message = ""
        raise EditorError(message or f"Lumi answered {exc.code}.") from None
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, ConnectionRefusedError):
            raise EditorError(NOT_RUNNING) from None
        raise EditorError(f"Couldn't reach Lumi: {exc.reason}") from None
    return payload if raw else json.loads(payload)


def parse_lines(value: str | None) -> tuple[int, int] | None:
    """``10-24`` or ``10`` as (first, last); None for nothing usable.

    JetBrains fills ``$SelectionStartLine$-$SelectionEndLine$``, which can be
    partly empty when nothing is selected.
    """
    numbers = [int(part) for part in (value or "").replace(" ", "").split("-") if part.isdigit()]
    numbers = [number for number in numbers if number > 0]
    if not numbers:
        return None
    return min(numbers), max(numbers)


def _status(args) -> int:
    data = call("GET", "/api/editor/status")
    window = "" if data.get("window") else " Its window isn't open, so files can't be added to a message."
    print(f"Lumi {data.get('version')} is running with {data.get('project') or 'no project'} open.{window}")
    return 0


def _send(args) -> int:
    lines = parse_lines(args.lines)
    items = []
    for name in args.files:
        item: dict = {"path": str(Path(name).resolve())}
        if lines:
            item.update(start_line=lines[0], end_line=lines[1])
        items.append(item)
    result = call("POST", "/api/editor/context", {"items": items, "text": args.text or "", "source": args.source})
    if result.get("attached"):
        print(f"Added to your message in Lumi: {', '.join(result['attached'])}. Send it from Lumi.")
    for item in result.get("skipped") or []:
        print(f"Not added: {item['path']}: {item['reason']}", file=sys.stderr)
    return 0


def _text(content: bytes) -> list[str] | None:
    if b"\0" in content[:8192]:
        return None
    # Line endings a checkout converted are not differences.
    return content.decode("utf-8", "replace").replace("\r\n", "\n").splitlines(keepends=True)


def unified_diff(data: dict, item: dict) -> str:
    """One changed file's differences, as a unified diff."""
    before = b""
    if item.get("before_path"):
        query = urlencode({"before": data["turn"]["before"], "path": item["before_path"]})
        before = call("GET", f"/api/editor/before?{query}", raw=True)
    current = Path(data["project"], *item["path"].split("/"))
    now = current.read_bytes() if item["status"] != "deleted" and current.is_file() else b""
    old, new = _text(before), _text(now)
    if old is None or new is None:
        return f"Binary file {item['path']} {STATUS.get(item['status'], 'changed')}.\n"
    diff = difflib.unified_diff(old, new, fromfile=f"a/{item.get('before_path') or item['path']}",
                                tofile=f"b/{item['path']}")
    return "".join(line if line.endswith("\n") else line + "\n\\ No newline at end of file\n" for line in diff)


def _changes(args) -> int:
    data = call("GET", "/api/editor/changes")
    turn = data.get("turn")
    if not turn or not data.get("files"):
        print("The open Lumi session hasn't changed any files yet.")
        return 0
    running = "" if turn.get("finished") else " (still running)"
    print(f"Lumi's changes{running}: {turn.get('prompt') or 'latest turn'}")
    print(f"Compared with {turn.get('compared_with')}.")
    for item in data["files"]:
        renamed = f" (was {item['before_path']})" if item["status"] == "renamed" else ""
        print(f"  {STATUS.get(item['status'], 'changed'):8} {item['path']}{renamed}")
    if data.get("more"):
        print(f"  and {data['more']} more")
    if args.diff:
        for item in data["files"]:
            print()
            sys.stdout.write(unified_diff(data, item))
    return 0


def _diff(args) -> int:
    data = call("GET", "/api/editor/changes")
    project = Path(data["project"])
    target = Path(args.file).resolve()
    try:
        relative = target.relative_to(project).as_posix()
    except ValueError:
        raise EditorError(f"{args.file} isn't in the project Lumi has open ({project}).") from None
    item = next((entry for entry in data.get("files") or [] if entry["path"] == relative), None)
    if item is None:
        print(f"Lumi's latest change-making turn didn't change {relative}.")
        return 0
    sys.stdout.write(unified_diff(data, item))
    return 0


def _vscode(args) -> int:
    if args.install:
        print(install_vscode(args.editor))
        print(f"If {VSCODE_FAMILY[args.editor]} is open, run \"Developer: Reload Window\" to start the extension.",
              file=sys.stderr)
        return 0
    target = build_vsix(args.out or os.getcwd())
    print(target)
    print(f'Install it with: {args.editor} --install-extension "{target}"', file=sys.stderr)
    return 0


def _jetbrains(args) -> int:
    if args.install:
        written = install_jetbrains()
        if not written:
            print("No JetBrains IDE settings were found. Save the tools with --out and copy the file into "
                  "your IDE's settings folder, in tools/.", file=sys.stderr)
            return 1
        for path in written:
            print(path)
        print("Restart any IDE that is open. The tools are in Tools > External Tools > Lumi and the editor's "
              "right-click menu.", file=sys.stderr)
        return 0
    text = jetbrains_tools_xml()
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
        print(args.out)
    else:
        sys.stdout.write(text)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="lumi editor", description="Use Lumi from a code editor.")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("status", help="whether Lumi is running, and the project it has open")
    send = commands.add_parser("send", help="add files to your message in Lumi")
    send.add_argument("files", nargs="+", help="files in the project Lumi has open")
    send.add_argument("--lines", help="only these lines, such as 10-24")
    send.add_argument("--text", default="", help="a question to put before the files")
    send.add_argument("--source", default="the command line", help=argparse.SUPPRESS)
    changes = commands.add_parser("changes", help="what the open session's latest change-making turn changed")
    changes.add_argument("--diff", action="store_true", help="print the differences too")
    diff = commands.add_parser("diff", help="one file's differences from before that turn")
    diff.add_argument("file")
    vscode = commands.add_parser("vscode", help="the VS Code extension: save the .vsix, or install it")
    vscode.add_argument("--install", action="store_true", help="install it with the editor's command line")
    vscode.add_argument("--editor", default="code", choices=sorted(VSCODE_FAMILY),
                        help="which editor's command line to use (default: code)")
    vscode.add_argument("--out", help="the folder to save the .vsix in (default: this folder)")
    jetbrains = commands.add_parser("jetbrains", help="External Tools for JetBrains IDEs")
    jetbrains.add_argument("--install", action="store_true", help="add them to every JetBrains IDE found")
    jetbrains.add_argument("--out", help="save the tools file here instead of printing it")
    args = parser.parse_args(argv)
    handler = {"status": _status, "send": _send, "changes": _changes, "diff": _diff,
               "vscode": _vscode, "jetbrains": _jetbrains}[args.command]
    try:
        return handler(args)
    except (EditorError, RuntimeError, ValueError, OSError) as exc:
        print(exc, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
