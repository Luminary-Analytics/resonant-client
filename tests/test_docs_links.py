"""The documentation site's guides link only to files that exist (mkdocs.yml, docs/)."""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LINK = re.compile(r"\[[^\]]*\]\(([^)\s]+)\)")


def nav_pages() -> list[str]:
    """The Markdown files the site's navigation names (the ``nav:`` block of mkdocs.yml)."""
    text = (ROOT / "mkdocs.yml").read_text(encoding="utf-8")
    block = text[text.index("\nnav:\n"):]
    return re.findall(r"^\s*-\s*(?:[^:\n]+:\s*)?([\w./-]+\.md)\s*$", block, re.M)


def test_every_page_in_the_navigation_exists():
    pages = nav_pages()
    assert "index.md" in pages and "packs.md" in pages
    missing = [page for page in pages if not (ROOT / "docs" / page).is_file()]
    assert missing == []


def test_the_guides_link_to_files_that_exist():
    broken = []
    for page in nav_pages():
        path = ROOT / "docs" / page
        text = re.sub(r"```.*?```", "", path.read_text(encoding="utf-8"), flags=re.S)
        for target in LINK.findall(text):
            if re.match(r"^[a-z][a-z0-9+.-]*:", target) or target.startswith("#"):
                continue
            destination = (path.parent / target.partition("#")[0]).resolve()
            if not destination.exists():
                broken.append(f"{page}: {target}")
    assert broken == []
