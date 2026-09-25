"""A small language server for tests of lumi/engine/lsp.py (not a test module).

It knows Python-like `class`/`def` lines, reports a warning for each line with
TODO_WARN and an error for each TODO_ERROR, and writes Windows URIs the way
VS Code's libraries do (file:///c%3A/...). Environment switches:

* FAKE_LSP_CRASH=1   exit before answering initialize
* FAKE_LSP_SILENT=1  never publish diagnostics
* FAKE_LSP_PULL=1    offer pull diagnostics (textDocument/diagnostic)
"""

import json
import os
import re
import sys
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit

if os.environ.get("FAKE_LSP_CRASH") == "1":
    sys.stderr.write("fake server: configuration is broken\n")
    sys.stderr.flush()
    sys.exit(3)

stdin, stdout = sys.stdin.buffer, sys.stdout.buffer
documents: dict[str, str] = {}
root = ""


def to_uri(path: str) -> str:
    path = os.path.abspath(path)
    if os.name == "nt":
        return "file:///" + quote(path[0].lower() + path[1:].replace("\\", "/"), safe="/")
    return Path(path).as_uri()


def to_path(uri: str) -> str:
    location = unquote(urlsplit(uri).path)
    if os.name == "nt" and re.match(r"^/[A-Za-z]:", location):
        location = location[1:]
    return os.path.normcase(os.path.abspath(location))


def send(message: dict) -> None:
    body = json.dumps(message).encode("utf-8")
    stdout.write(b"Content-Length: %d\r\n\r\n" % len(body) + body)
    stdout.flush()


def read() -> dict | None:
    length = None
    while True:
        line = stdin.readline()
        if not line:
            return None
        line = line.strip()
        if not line:
            break
        name, _, value = line.decode().partition(":")
        if name.lower() == "content-length":
            length = int(value)
    return json.loads(stdin.read(length))


def text_of(path: str) -> str:
    return documents.get(path) or Path(path).read_text(encoding="utf-8")


def project_files() -> list[str]:
    found = {os.path.normcase(os.path.abspath(p)) for p in Path(root).rglob("*.py")}
    return sorted(found | set(documents))


def units(text: str) -> int:
    """UTF-16 code units, as LSP positions count them."""
    return len(text.encode("utf-16-le")) // 2


def index_of(line: str, count: int) -> int:
    for index in range(len(line) + 1):
        if units(line[:index]) >= count:
            return index
    return len(line)


def word_at(path: str, position: dict) -> str:
    line = text_of(path).split("\n")[position["line"]]
    index = index_of(line, position["character"])
    for match in re.finditer(r"\w+", line):
        if match.start() <= index <= match.end():
            return match.group()
    return ""


def diagnostics(path: str) -> list[dict]:
    items = []
    for number, line in enumerate(text_of(path).split("\n")):
        for marker, severity in (("TODO_ERROR", 1), ("TODO_WARN", 2)):
            column = line.find(marker)
            if column >= 0:
                items.append({"range": {"start": {"line": number, "character": units(line[:column])},
                                        "end": {"line": number,
                                                "character": units(line[:column + len(marker)])}},
                              "severity": severity, "source": "fake", "code": marker.lower(),
                              "message": f"{marker} found"})
    return items


def publish(path: str) -> None:
    if os.environ.get("FAKE_LSP_SILENT") != "1":
        send({"jsonrpc": "2.0", "method": "textDocument/publishDiagnostics",
              "params": {"uri": to_uri(path), "diagnostics": diagnostics(path)}})


def symbols(path: str) -> list[dict]:
    result, current = [], None
    for number, line in enumerate(text_of(path).split("\n")):
        match = re.match(r"^(\s*)(class|def)\s+(\w+)", line)
        if not match:
            continue
        where = {"start": {"line": number, "character": len(match.group(1))},
                 "end": {"line": number, "character": len(line)}}
        item = {"name": match.group(3), "kind": 5 if match.group(2) == "class" else 12,
                "range": where, "selectionRange": where, "children": []}
        if match.group(1) and current is not None:
            item["kind"] = 6
            current["children"].append(item)
        else:
            result.append(item)
            current = item if match.group(2) == "class" else None
    return result


def answer(method: str, params: dict):
    if method == "initialize":
        global root
        root = to_path(params["rootUri"])
        capabilities = {"textDocumentSync": 1, "definitionProvider": True, "referencesProvider": True,
                        "hoverProvider": True, "documentSymbolProvider": True}
        if os.environ.get("FAKE_LSP_PULL") == "1":
            capabilities["diagnosticProvider"] = {"interFileDependencies": False, "workspaceDiagnostics": False}
        return {"capabilities": capabilities, "serverInfo": {"name": "fake"}}
    if method == "shutdown":
        return None
    path = to_path(params["textDocument"]["uri"])
    if method == "textDocument/diagnostic":
        return {"kind": "full", "items": diagnostics(path)}
    if method == "textDocument/documentSymbol":
        return symbols(path)
    word = word_at(path, params["position"])
    if method == "textDocument/hover":
        return {"contents": {"kind": "markdown", "value": f"```python\n{word}\n```\nDocs for {word}."}}
    locations = []
    for file in project_files():
        for number, line in enumerate(text_of(file).split("\n")):
            if method == "textDocument/definition":
                match = re.match(rf"^\s*(class|def)\s+({word})\b", line)
                spans = [match.span(2)] if match else []
            else:
                spans = [m.span() for m in re.finditer(rf"\b{re.escape(word)}\b", line)]
            for start, end in spans:
                locations.append({"uri": to_uri(file), "range": {
                    "start": {"line": number, "character": units(line[:start])},
                    "end": {"line": number, "character": units(line[:end])}}})
    return locations


def main() -> None:
    while True:
        message = read()
        if message is None:
            return
        method = message.get("method")
        if method == "exit":
            return
        if method == "initialized":
            # Servers ask their clients things; the client has to answer.
            send({"jsonrpc": "2.0", "id": "config-1", "method": "workspace/configuration",
                  "params": {"items": [{"section": "fake"}]}})
            continue
        if method in ("textDocument/didOpen", "textDocument/didChange"):
            document = message["params"]["textDocument"]
            path = to_path(document["uri"])
            documents[path] = (document.get("text") if method == "textDocument/didOpen"
                               else message["params"]["contentChanges"][-1]["text"])
            publish(path)
            continue
        if "id" in message and method is None:
            continue  # the client's answer to our request
        if "id" in message:
            send({"jsonrpc": "2.0", "id": message["id"], "result": answer(method, message.get("params") or {})})


if __name__ == "__main__":
    main()
