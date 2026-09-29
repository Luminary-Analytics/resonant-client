"""Lumi's legal texts: one file of facts, rendered into every text Lumi ships.

``lumi/legal/terms.json`` holds the facts the texts depend on (the legal
entity, its type and state of formation, the governing law, the venue and the
notices address) and, for each document, its version, the day its text was
published and the SHA-256 that pins that version's text. The texts are
written in ``lumi/legal/templates/`` with ``{{...}}`` for those facts::

    {{entity.legal_name}}  {{entity.type}}  {{entity.state_of_formation}}
    {{governing_law}}  {{venue}}  {{notices_email}}
    {{doc.eula.title}}  {{doc.eula.version}}  {{doc.eula.published}}  {{doc.eula.year}}

This script renders them, and it is the documented way to change them: edit
the template or terms.json, then run ``render`` and commit both::

    python packaging/legal_texts.py render
    python packaging/legal_texts.py check          # a text isn't what render writes, or its pin: exit 1
    python packaging/legal_texts.py release-check  # facts still to be provided: warnings (pull requests)
    python packaging/legal_texts.py release-check --release --version 0.20.0   # a release: errors
    python packaging/legal_texts.py rtf --out dist/legal [--version 0.20.0-beta.1]

What it writes (``OUTPUTS``): the texts the app shows and bundles
(``lumi/legal/EULA.md``, ``ALPHA-TERMS.md``, ``PRIVACY.md``), the Extension
SDK's license (``sdk/LICENSE`` and the copy that travels inside the
``lumi_extension`` package) and the VS Code extension's
(``lumi/code_editors/vscode/LICENSE.txt``).

**Pins.** Each document's ``sha256`` is the hash of its rendered text (the
Markdown without the generated-file comment, as lumi/terms.py hashes it; the
SDK license as written). ``check`` fails when a text no longer matches its
pin: a change to what a text says needs a new ``version`` and a new pin, and
render prints the hash to pin. An acceptance records the hash of the text it
accepted, so the app never counts one for a text it didn't show.

``rtf`` writes what the installers show on their license page:
``license.rtf`` (the EULA, followed by the Alpha and Beta Test Terms when the
version is a pre-release), ``eula.rtf``, ``privacy.rtf`` and, for a
pre-release, ``alpha-terms.rtf``; and the versions ``license.rtf`` holds, as
``license-versions.json`` and ``license-versions.iss`` (Inno Setup skips its
license page when an installation already showed those versions). Inno Setup
(packaging/installer.iss), the MSI (packaging/build_msi.ps1) and the macOS
DMG and PKG (packaging/build_macos.sh) use them. Square brackets are written
as RTF escapes, so an MSI never reads them as properties.

A release (``release-check --release``) fails while a fact still reads
``[[TO BE PROVIDED: ...]]`` or any rendered text holds one, a text isn't what
render writes or isn't its pinned version's, a document's published date is
after the day of the build, or the version isn't ``X.Y.Z`` or
``X.Y.Z-alpha.N``, ``-beta.N`` or ``-rc.N`` from 0.20.0 on: Lumi's releases
under these terms start above every version it published under the MIT
License, and GitHub, the update feeds and the installers tell a pre-release
by its hyphen. Pull request CI shows the same problems as warnings.

Standard library only, and Python 3.9 or later: build_macos.sh runs it with
the Mac's own python3.
"""

from __future__ import annotations

import argparse
import hashlib
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
# What a release tag's version may be: X.Y.Z, or X.Y.Z-alpha.N, -beta.N or -rc.N for a pre-release. PEP 440
# spellings such as 0.20.0rc1 aren't: .github/workflows/release.yml, the update feeds
# (packaging/update_appcast.py) and GitHub's pre-release flag tell a pre-release by this hyphen.
RELEASE_VERSION = re.compile(r"(\d+)\.(\d+)\.(\d+)(?:-(alpha|beta|rc)\.(\d+))?")
# The first release under these terms, above every version published under the MIT License (Resonant
# Client, Resonant and SONN Client 0.6.3a1 through 0.19.1; see EULA section 5.4).
FIRST_PROPRIETARY = (0, 20, 0)
# The documents whose text a version pins (``sha256`` in terms.json), and the rendered output each is hashed
# from: the Markdown without its generated-file comment (as lumi/terms.py reads it), the SDK license whole.
PINNED = {"eula": "lumi/legal/EULA.md", "alpha_terms": "lumi/legal/ALPHA-TERMS.md",
          "privacy": "lumi/legal/PRIVACY.md", "sdk_license": "sdk/LICENSE"}
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
        published = str(entry.get("published") or "")
        try:
            year = str(date.fromisoformat(published).year)
            written = long_date(published)
        except ValueError as exc:
            raise LegalTextError(f"documents.{doc_id}.published isn't a date (YYYY-MM-DD): {published!r}") from exc
        documents[doc_id] = {**entry, "published": written, "published_iso": published, "year": year}
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


def unfinished(texts: dict[str, str] | None = None) -> list[str]:
    """``output: [[TO BE PROVIDED: ...]]`` for each placeholder a rendered text still holds, wherever it came
    from (a fact, or one written into a template), and ``output: {{name}}`` for anything left unrendered."""
    texts = rendered() if texts is None else texts
    found = []
    for output, text in texts.items():
        found += [f"{output}: {match}" for match in PLACEHOLDER.findall(text)]
        found += [f"{output}: {match.group(0)}" for match in TOKEN.finditer(text)]
    return found


def pinned_text(doc_id: str, texts: dict[str, str] | None = None) -> str:
    """The text a document's pin hashes: its rendered output, without the generated-file comment."""
    texts = rendered() if texts is None else texts
    output = PINNED[doc_id]
    text = texts[output]
    return text[len(text.split("\n", 1)[0]) + 1:] if output.endswith(".md") else text


def text_sha256(doc_id: str, texts: dict[str, str] | None = None) -> str:
    return hashlib.sha256(pinned_text(doc_id, texts).encode("utf-8")).hexdigest()


def pin_problems(facts: dict | None = None, texts: dict[str, str] | None = None) -> list[str]:
    """Documents whose text isn't the one ``terms.json`` pins for their version (``sha256``)."""
    facts = load_facts() if facts is None else facts
    texts = rendered(facts) if texts is None else texts
    problems = []
    for doc_id in PINNED:
        entry = facts["documents"].get(doc_id) or {}
        digest = text_sha256(doc_id, texts)
        if str(entry.get("sha256") or "") != digest:
            problems.append(
                f"{PINNED[doc_id]} isn't the text pinned for {doc_id} version {entry.get('version')!r} "
                f"(documents.{doc_id}.sha256 in lumi/legal/terms.json). A change to what a text says needs a new "
                f"version: raise documents.{doc_id}.version, then pin sha256 {digest}.")
    return problems


def date_problems(facts: dict | None = None, today: date | None = None) -> list[str]:
    """Documents published after ``today`` (the build's day): a version applies from the day it's accepted,
    so no text may name a date after the day someone could accept it."""
    facts = load_facts() if facts is None else facts
    today = today or date.today()
    problems = []
    for doc_id, entry in facts["documents"].items():
        try:
            published = date.fromisoformat(str(entry.get("published") or ""))
        except ValueError:
            problems.append(f"documents.{doc_id}.published isn't a date (YYYY-MM-DD).")
            continue
        if published > today:
            problems.append(f"documents.{doc_id}.published is {published.isoformat()}, after this build's day "
                            f"({today.isoformat()}): use the day the text was written.")
    return problems


def is_prerelease(version: str) -> bool:
    return bool(PRERELEASE.search(str(version or "").strip()))


def lumi_version() -> str:
    text = (ROOT / "lumi" / "__init__.py").read_text(encoding="utf-8")
    match = re.search(r'__version__\s*=\s*"([^"]+)"', text)
    if not match:
        raise LegalTextError("lumi/__init__.py has no __version__")
    return match.group(1)


def version_problem(version: str) -> str:
    """Why ``version`` can't be a release under these terms, or ''."""
    match = RELEASE_VERSION.fullmatch(str(version or ""))
    if not match:
        return (f"{version!r} isn't a release version: tag a release vX.Y.Z, or vX.Y.Z-alpha.N, vX.Y.Z-beta.N or "
                "vX.Y.Z-rc.N for a pre-release. PEP 440 spellings such as 0.20.0rc1 or 0.20.0.dev1 aren't "
                "released: GitHub, the update feeds and the installers tell a pre-release by its hyphen.")
    if tuple(int(part) for part in match.groups()[:3]) < FIRST_PROPRIETARY:
        return (f"Lumi {version} can't be released under these terms: releases under the End User License "
                "Agreement start at 0.20.0, above every version published under the MIT License (Resonant Client, "
                "Resonant and SONN Client 0.6.3a1 through 0.19.1).")
    return ""


def release_problems(version: str, *, release: bool, today: date | None = None) -> list[str]:
    """Everything that stops ``version`` shipping these texts (a release fails on any; pull requests warn)."""
    facts = load_facts()
    texts = rendered(facts)
    problems = [f"Still to be provided in lumi/legal/terms.json: {item}" for item in placeholders(facts)]
    problems += [f"Still to be provided in {item}" for item in unfinished(texts)]
    problems += [f"{output} isn't what `python packaging/legal_texts.py render` writes." for output in stale(texts)]
    problems += pin_problems(facts, texts)
    problems += date_problems(facts, today)
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
        elif char in "[]":
            # An MSI reads [name] in its license text as a property and puts the property's value, or nothing,
            # in its place: a hex escape shows the bracket without writing one.
            out.append(f"\\'{code:02x}")
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


def license_versions(version: str, facts: dict | None = None) -> dict[str, str]:
    """The documents ``license.rtf`` holds for ``version``, with their versions: the EULA, and the Alpha and
    Beta Test Terms for a pre-release (an empty version when a stable build doesn't hold them)."""
    facts = load_facts() if facts is None else facts
    documents = facts["documents"]
    return {"eula": str(documents["eula"]["version"]),
            "alpha_terms": str(documents["alpha_terms"]["version"]) if is_prerelease(version) else ""}


def _iss_string(value: str) -> str:
    if not re.fullmatch(r"[0-9A-Za-z.\-]*", value):
        raise LegalTextError(f"A document version must be letters, digits, dots and dashes: {value!r}")
    return value


def write_rtf(out: Path, version: str, facts: dict | None = None) -> dict[str, Path]:
    """The installers' RTF files for ``version`` in ``out``, and the versions license.rtf holds; {name: path}.

    ``license-versions.iss`` defines ``LicenseEulaVersion`` and ``LicenseAlphaTermsVersion`` for
    packaging/installer.iss, which records them when it installs and skips its license page when an
    installation already showed the same versions. That record is the installer's convenience, never a
    person's acceptance: Lumi asks each person itself (lumi/terms.py).
    """
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
    versions = license_versions(version, facts)
    files["license-versions.json"] = json.dumps({"lumi_version": version, "documents": versions}, indent=2) + "\n"
    files["license-versions.iss"] = (
        f"; Rendered by packaging/legal_texts.py rtf for Lumi {version}: the versions of Lumi's terms that\n"
        "; license.rtf holds. packaging/installer.iss records them and skips its license page when they match.\n"
        f'#define LicenseEulaVersion "{_iss_string(versions["eula"])}"\n'
        f'#define LicenseAlphaTermsVersion "{_iss_string(versions["alpha_terms"])}"\n')
    written = {}
    for name, text in files.items():
        path = out / name
        # RTF is 7-bit: every other character is already a \u escape (the version files are ASCII too).
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
    release = commands.add_parser("release-check", help="what stops a release: facts to provide, stale or "
                                  "unpinned texts, future dates, the version (errors with --release)")
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
            # A changed text needs a new version and pin; say which, with the hash to pin.
            for problem in pin_problems():
                print(f"note: {problem}")
            return 0
        if args.command == "check":
            wrong = stale()
            for output in wrong:
                print(f"{output} isn't what `python packaging/legal_texts.py render` writes; run it and commit the "
                      "result.", file=sys.stderr)
            pins = [] if wrong else pin_problems()
            for problem in pins:
                print(problem, file=sys.stderr)
            return 1 if wrong or pins else 0
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
        versions = license_versions(version)
        included = (f"the EULA {versions['eula']} and the Alpha and Beta Test Terms {versions['alpha_terms']}"
                    if versions["alpha_terms"] else f"the EULA {versions['eula']}")
        print(f"Wrote {', '.join(sorted(written))} to {args.out} for Lumi {version} (license.rtf: {included}).")
        return 0
    except (LegalTextError, OSError, ValueError, KeyError) as exc:
        print(f"legal_texts: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
