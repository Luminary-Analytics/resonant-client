"""Tests for markdown todo parsing in the engine session."""

import pytest

from resonant_client.engine.session import parse_markdown_todos


class TestParseMarkdownTodos:

    @pytest.mark.unit
    def test_none_when_no_tasks(self):
        assert parse_markdown_todos("Just some prose.\n- item without checkbox") is None

    @pytest.mark.unit
    def test_parses_dash_checkbox(self):
        text = """Plan:
- [ ] First
- [x] Second
- [X] Third
"""
        items = parse_markdown_todos(text)
        assert items is not None
        assert len(items) == 3
        assert items[0] == {"text": "First", "done": False}
        assert items[1] == {"text": "Second", "done": True}
        assert items[2] == {"text": "Third", "done": True}

    @pytest.mark.unit
    def test_star_bullet(self):
        text = "* [ ] A\n* [x] B"
        items = parse_markdown_todos(text)
        assert items == [
            {"text": "A", "done": False},
            {"text": "B", "done": True},
        ]
