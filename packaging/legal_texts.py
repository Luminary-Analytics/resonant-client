"""Lumi's legal texts: one file of facts, rendered into every text Lumi ships.

``lumi/legal/terms.json`` holds the facts the texts depend on (the legal
entity, its type and state of formation, the governing law, the venue and the
notices address) and each document's version and effective date. The texts
are written in ``lumi/legal/templates/`` with ``{{...}}`` for those facts::

    {{entity.legal_name}}  {{entity.type}}  {{entity.state_of_formation}}
    {{governing_law}}  {{venue}}  {{notices_email}}
    {{doc.eula.title}}  {{doc.eula.version}}  {{doc.eula.effective}}  {{doc.eula.year}}

This script renders them, and it is the documented way to change them: edit
the template or terms.json, then run ``render`` and commit both::

    python packaging/legal_texts.py render
    python packaging/legal_texts.py check          # a text isn't what render writes: exit 1 (tests do this)
    python packaging/legal_texts.py release-check  # facts still to be provided: warnings (pull requests)
    python packaging/legal_texts.py release-check --release --version 0.20.0   # a release: errors
    python packaging/legal_texts.py rtf --out dist/legal [--version 0.20.0-beta.1]

What it writes (``OUTPUTS``): the texts the app shows and bundles
(``lumi/legal/EULA.md``, ``ALPHA-TERMS.md``, ``PRIVACY.md``), the Extension
SDK's license (``sdk/LICENSE`` and the copy that travels inside the
``lumi_extension`` package) and the VS Code extension's
(``lumi/code_editors/vscode/LICENSE.txt``).

``rtf`` writes what the installers show on their license page:
``license.rtf`` (the EULA, followed by the Alpha and Beta Test Terms when the
version is a pre-release), ``eula.rtf``, ``privacy.rtf`` and, for a
pre-release, ``alpha-terms.rtf``. Inno Setup (packaging/installer.iss), the
MSI (packaging/build_msi.ps1) and the macOS DMG and PKG
(packaging/build_macos.sh) use them.

A fact that still reads ``[[TO BE PROVIDED: ...]]`` is a warning in pull
request CI and an error in a release build, which also refuses a version
below 0.20.0: Lumi 0.19.x and earlier were published under the MIT License.

Standard library only, and Python 3.9 or later: build_macos.sh runs it with
the Mac's own python3.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LEGAL = ROOT / "lumi" / "legal"
FACTS = LEGAL / "terms.json"
TEMPLATES = LEGAL / "templates"
# (template, output): the texts Lumi ships, rendered from the templates with terms.json.
OUTPUTS = (
    ("EULA.md", "lumi/legal/EULA.md"),
    ("ALPHA-TERMS.md", "lumi/legal/ALPHA-TERMS.md"),
    ("PRIVACY.md", "lumi/legal/PRIVACY.md"),
    ("SDK-LICENSE.txt", "sdk/LICENSE"),
    ("SDK-LICENSE.txt", "sdk/python/lumi_extension/LICENSE"),
    ("VSCODE-LICENSE.txt", "lumi/code_editors/vscode/LICENSE.txt"),
)
# The first line of each rendered Markdown text: lumi/terms.py leaves it out of what it shows.
GENERATED = ("<!-- Rendered by packaging/legal_texts.py from lumi/legal/templates/{template} and "
             "lumi/legal/terms.json. Edit those, then run: python packaging/legal_texts.py render -->\n")
PLACEHOLDER = re.compile(r"\[\[TO BE PROVIDED:[^\]]*\]\]")
TOKEN = re.compile(r"\{\{\s*([A-Za-z0-9_.]+)\s*\}\}")
# A pre-release version, as lumi/terms.py decides it (tests/test_legal_texts.py checks the two agree).
PRERELEASE = re.compile(r"-|(?<=[\d.])(?:a|b|c|rc|alpha|beta|pre|preview|dev)\d*$", re.IGNORECASE)
# The first release under these terms: 0.6.3 through 0.19.x were published under the MIT License.
FIRST_PROPRIETARY = (0, 20, 0)
MONTHS = ("January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
          "November", "December")


class LegalTextError(ValueError):
    """A template or terms.json that can't be rendered; the message says where."""


def load_facts(path: Path = FACTS) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("documents"), dict):
        raise LegalTextError(f"{path} lists no documents")
    return data


def long_date(iso: str) -> str:
    value = date.fromisoformat(iso)
    return f"{MONTHS[value.month - 1]} {value.day}, {value.year}"


def context(facts: dict) -> dict:
    """What ``{{...}}`` can name: the facts as written, and each document's dates in words."""
    values = {key: value for key, value in facts.items() if not key.startswith("_") and key != "documents"}
    documents = {}
    for doc_id, entry in facts["documents"].items():
        effective = str(entry.get("effective") or "")
        try:
            year = str(date.fromisoformat(effective).year)
            written = long_date(effective)
        except ValueError as exc:
            raise LegalTextError(f"documents.{doc_id}.effective isn't a date (YYYY-MM-DD): {effective!r}") from exc
        documents[doc_id] = {**entry, "effective": written, "effective_iso": effective, "year": year}
    values["doc"] = documents
    return values


def _lookup(values: dict, name: str) -> str:
    current = values
    for part in name.split("."):
        if not isinstance(current, dict) or part not in current:
            raise LegalTextError(f"{{{{{name}}}}} isn't in lumi/legal/terms.json")
        current = current[part]
    if isinstance(current, (dict, list)):
        raise LegalTextError(f"{{{{{name}}}}} names a group, not a value")
    return str(current)


def render_text(template: str, facts: dict) -> str:
    values = context(facts)
    return TOKEN.sub(lambda match: _lookup(values, match.group(1)), template)


def rendered(facts: dict | None = None) -> dict[str, str]:
    """{output path relative to the repository: text} for every output."""
    facts = load_facts() if facts is None else facts
    texts = {}
    for template, output in OUTPUTS:
        body = render_text((TEMPLATES / template).read_text(encoding="utf-8").replace("\r\n", "\n"), facts)
        if output.endswith(".md"):
            body = GENERATED.format(template=template) + body
        texts[output] = body
    return texts


def write(texts: dict[str, str]) -> list[str]:
    changed = []
    for output, text in texts.items():
        path = ROOT / output
        current = path.read_text(encoding="utf-8").replace("\r\n", "\n") if path.exists() else None
        if current != text:
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(text)
            changed.append(output)
    return changed


def stale(texts: dict[str, str] | None = None) -> list[str]:
    """Outputs that aren't what ``render`` writes (line endings aside: Git may check them out with CRLF)."""
    texts = rendered() if texts is None else texts
    wrong = []
    for output, text in texts.items():
        path = ROOT / output
        if not path.exists() or path.read_text(encoding="utf-8").replace("\r\n", "\n") != text:
            wrong.append(output)
    return wrong


def placeholders(facts: dict | None = None) -> list[str]:
    """``key: [[TO BE PROVIDED: ...]]`` for every fact still to be provided."""
    facts = load_facts() if facts is None else facts
    found = []

    def walk(value, where):
        if isinstance(value, dict):
            for key, item in value.items():
                if not str(key).startswith("_"):
                    walk(item, f"{where}.{key}" if where else str(key))
        elif isinstance(value, list):
            for index, item in enumerate(value):
                walk(item, f"{where}[{index}]")
        elif isinstance(value, str):
            found.extend(f"{where}: {match}" for match in PLACEHOLDER.findall(value))

    walk(facts, "")
    return found


def is_prerelease(version: str) -> bool:
    return bool(PRERELEASE.search(str(version or "").strip()))


def lumi_version() -> str:
    text = (ROOT / "lumi" / "__init__.py").read_text(encoding="utf-8")
    match = re.search(r'__version__\s*=\s*"([^"]+)"', text)
    if not match:
        raise LegalTextError("lumi/__init__.py has no __version__")
    return match.group(1)


def version_problem(version: str) -> str:
    """Why ``version`` can't be released under these terms, or ''."""
    match = re.match(r"(\d+)\.(\d+)\.(\d+)", str(version))
    if not match:
        return f"{version!r} isn't a release version (X.Y.Z)."
    if tuple(int(part) for part in match.groups()) < FIRST_PROPRIETARY:
        return (f"Lumi {version} can't be released under these terms: 0.6.3 through 0.19.x were published under "
                "the MIT License, so the first release under the End User License Agreement is 0.20.0 or later.")
    return ""


def release_problems(version: str, *, release: bool) -> list[str]:
    problems = [f"Still to be provided in lumi/legal/terms.json: {item}" for item in placeholders()]
    problems += [f"{output} isn't what `python packaging/legal_texts.py render` writes." for output in stale()]
    if release:
        problem = version_problem(version)
        if problem:
            problems.append(problem)
    return problems


# ── RTF for the installers' license pages ──────────────────────────────────


def _escape(text: str) -> str:
    out = []
    for char in text:
        code = ord(char)
        if char in "\\{}":
            out.append("\\" + char)
        elif code < 128:
            out.append(char)
        elif code <= 0xFFFF:
            out.append(f"\\u{code if code < 32768 else code - 65536}?")
        else:  # outside the BMP: a UTF-16 surrogate pair
            code -= 0x10000
            for unit in (0xD800 + (code >> 10), 0xDC00 + (code & 0x3FF)):
                out.append(f"\\u{unit - 65536}?")
    return "".join(out)


_INLINE = re.compile(
    r"`([^`]+)`"                                  # code
    r"|\*\*(.+?)\*\*"                             # bold
    r"|\[([^\]]+)\]\(([^)\s]+)\)"                 # link
    r"|(?<![\w*])\*(?![\s*])(.+?)(?<![\s*])\*(?![\w*])"  # italic with *
    r"|(?<!\w)_(?![\s_])(.+?)(?<![\s_])_(?!\w)")  # italic with _


def _inline(text: str) -> str:
    out, position = [], 0
    for match in _INLINE.finditer(text):
        out.append(_escape(text[position:match.start()]))
        code, bold, label, url, star, underscore = match.groups()
        if code is not None:
            out.append("{\\f1 " + _escape(code) + "}")
        elif bold is not None:
            out.append("{\\b " + _inline(bold) + "}")
        elif label is not None:
            out.append(_inline(label))
            # A web address is written out (an installer can't follow a link); a link within the texts isn't.
            if url.startswith(("http://", "https://")) and url != label:
                out.append(" (" + _escape(url) + ")")
        else:
            out.append("{\\i " + _inline(star if star is not None else underscore) + "}")
        position = match.end()
    out.append(_escape(text[position:]))
    return "".join(out)


_LIST = re.compile(r"^(\s*)([-*+]|\d+[.)])\s+(.*)$")
_HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
_HEADING_SIZES = {1: 30, 2: 24, 3: 21}


def markdown_to_rtf(markdown: str) -> str:
    """RTF paragraphs for the Markdown the legal texts use: headings, paragraphs, lists (nested by indent),
    block quotes, bold, italic, code and links. A table row is kept as a line of text."""
    lines = _COMMENT.sub("", markdown.replace("\r\n", "\n")).split("\n")
    paragraphs: list[str] = []
    buffer: list[str] = []
    item: tuple[int, str, list[str]] | None = None

    def flush_paragraph():
        nonlocal buffer
        if buffer:
            paragraphs.append("\\pard\\sa120\\sl264\\slmult1 " + _inline(" ".join(buffer)) + "\\par")
            buffer = []

    def flush_item():
        nonlocal item
        if item is not None:
            level, marker, words = item
            indent = 360 * (level + 1)
            label = "\\bullet" if marker in "-*+" else _escape(marker)
            paragraphs.append(f"\\pard\\li{indent}\\fi-260\\tx{indent}\\sa60\\sl264\\slmult1 {label}\\tab "
                              + _inline(" ".join(words)) + "\\par")
            item = None

    for raw in lines:
        line = raw.rstrip()
        if not line.strip():
            flush_paragraph()
            flush_item()
            continue
        heading = _HEADING.match(line)
        listed = _LIST.match(line)
        if heading:
            flush_paragraph()
            flush_item()
            level = len(heading.group(1))
            size = _HEADING_SIZES.get(level, 20)
            paragraphs.append(f"\\pard\\keepn\\sb{240 if level < 3 else 160}\\sa120\\b\\fs{size} "
                              + _inline(heading.group(2)) + "\\b0\\fs20\\par")
        elif re.fullmatch(r"\s*(-{3,}|\*{3,}|_{3,})\s*", line):
            flush_paragraph()
            flush_item()
        elif listed:
            flush_paragraph()
            flush_item()
            indent = len(listed.group(1).replace("\t", "    "))
            item = (min(indent // 2, 4), listed.group(2), [listed.group(3).strip()])
        elif item is not None and raw[:1] in (" ", "\t"):
            item[2].append(line.strip())
        elif line.lstrip().startswith(">"):
            flush_item()
            flush_paragraph()
            paragraphs.append("\\pard\\li360\\ri360\\sa120\\i " + _inline(line.lstrip()[1:].strip()) + "\\i0\\par")
        elif line.lstrip().startswith("|"):
            flush_item()
            flush_paragraph()
            cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
            if not all(re.fullmatch(r":?-{3,}:?", cell) for cell in cells if cell):
                paragraphs.append("\\pard\\sa60 " + _inline("  |  ".join(cells)) + "\\par")
        else:
            flush_item()
            buffer.append(line.strip())
    flush_paragraph()
    flush_item()
    return "\n".join(paragraphs)


def rtf_document(parts: list[str]) -> str:
    """A whole RTF file of ``parts`` (each from markdown_to_rtf), a page break between them."""
    header = ("{\\rtf1\\ansi\\ansicpg1252\\deff0\\uc1"
              "{\\fonttbl{\\f0\\fswiss\\fcharset0 Arial;}{\\f1\\fmodern\\fcharset0 Courier New;}}"
              "\\viewkind4\\f0\\fs20\n")
    return header + "\n\\page\n".join(parts) + "\n}\n"


def _document_markdown(doc_id: str, facts: dict) -> str:
    entry = facts["documents"][doc_id]
    template = (TEMPLATES / entry["file"]).read_text(encoding="utf-8").replace("\r\n", "\n")
    return render_text(template, facts)


def write_rtf(out: Path, version: str, facts: dict | None = None) -> dict[str, Path]:
    """The installers' RTF files for ``version`` in ``out``; {name: path}."""
    facts = load_facts() if facts is None else facts
    out.mkdir(parents=True, exist_ok=True)
    eula = markdown_to_rtf(_document_markdown("eula", facts))
    files = {"eula.rtf": rtf_document([eula]),
             "privacy.rtf": rtf_document([markdown_to_rtf(_document_markdown("privacy", facts))])}
    license_parts = [eula]
    if is_prerelease(version):
        titles = facts["documents"]
        intro = markdown_to_rtf(
            f"This pre-release build of Lumi ({version}) is licensed to you under the "
            f"{titles['eula']['title']} and the {titles['alpha_terms']['title']}, which follow. "
            "Accepting them here accepts both.")
        alpha = markdown_to_rtf(_document_markdown("alpha_terms", facts))
        files["alpha-terms.rtf"] = rtf_document([alpha])
        license_parts = [intro + "\n" + eula, alpha]
    files["license.rtf"] = rtf_document(license_parts)
    written = {}
    for name, text in files.items():
        path = out / name
        # RTF is 7-bit: every other character is already a \u escape.
        path.write_bytes(text.encode("ascii"))
        written[name] = path
    stale_alpha = out / "alpha-terms.rtf"
    if "alpha-terms.rtf" not in files and stale_alpha.exists():
        stale_alpha.unlink()
    return written


# ── Command line ────────────────────────────────────────────────────────────


def _annotate(level: str, message: str) -> str:
    """A GitHub Actions annotation in CI, plain text elsewhere."""
    if os.environ.get("GITHUB_ACTIONS") == "true":
        return f"::{level} title=Legal texts::{message}"
    return f"{level}: {message}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("render", help="write every text from the templates and terms.json")
    commands.add_parser("check", help="exit 1 when a text isn't what render writes")
    release = commands.add_parser("release-check", help="facts still to be provided (errors with --release)")
    release.add_argument("--release", action="store_true", help="a release build: problems fail it")
    release.add_argument("--version", default="", help="the version being released (default: lumi/__init__.py)")
    rtf = commands.add_parser("rtf", help="the installers' license RTF files")
    rtf.add_argument("--out", required=True, type=Path)
    rtf.add_argument("--version", default="", help="the version being built (default: lumi/__init__.py)")
    args = parser.parse_args(argv)
    try:
        if args.command == "render":
            changed = write(rendered())
            print("\n".join(f"Wrote {path}" for path in changed) or "Every text is up to date.")
            return 0
        if args.command == "check":
            wrong = stale()
            for output in wrong:
                print(f"{output} isn't what `python packaging/legal_texts.py render` writes; run it and commit the "
                      "result.", file=sys.stderr)
            return 1 if wrong else 0
        if args.command == "release-check":
            version = args.version or lumi_version()
            problems = release_problems(version, release=args.release)
            level = "error" if args.release else "warning"
            for problem in problems:
                print(_annotate(level, problem), file=sys.stderr if args.release else sys.stdout)
            if not problems:
                print(f"Lumi {version}: the legal texts are complete and up to date.")
            return 1 if problems and args.release else 0
        version = args.version or lumi_version()
        written = write_rtf(args.out, version)
        included = "the EULA and the Alpha and Beta Test Terms" if is_prerelease(version) else "the EULA"
        print(f"Wrote {', '.join(sorted(written))} to {args.out} for Lumi {version} (license.rtf: {included}).")
        return 0
    except (LegalTextError, OSError, ValueError, KeyError) as exc:
        print(f"legal_texts: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
