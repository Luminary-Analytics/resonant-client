from resonant_client.engine.tool_presentation import tool_presentation


def test_core_file_tools_expose_diff_intent_and_locations():
    result = tool_presentation("file_edit", {"path": "src/app.py"})

    assert result == {
        "kind": "edit",
        "view": "diff",
        "label": "Edit file",
        "locations": ["src/app.py"],
        "interactive": False,
    }


def test_mcp_style_tool_names_are_inferred_without_a_registry_entry():
    result = tool_presentation(
        "mcp_filesystem_write_file",
        {"file_path": "notes/result.md"},
    )

    assert result["kind"] == "write"
    assert result["locations"] == ["notes/result.md"]
    assert result["view"] == "diff"


def test_file_deliverables_do_not_include_the_command_working_directory():
    result = tool_presentation(
        "mcp_filesystem_write_file",
        {"file_path": "notes/result.md", "cwd": "D:/Repos/project"},
    )

    assert result["locations"] == ["notes/result.md"]


def test_browser_and_desktop_calls_expose_interactive_views():
    browser = tool_presentation("browser_navigate", {"url": "https://example.com"})
    desktop = tool_presentation("computer_click", {"x": 10, "y": 20})

    assert browser["kind"] == "web"
    assert browser["target"] == "https://example.com"
    assert browser["interactive"] is True
    assert desktop["kind"] == "desktop"
    assert desktop["view"] == "computer"


def test_unknown_location_bearing_tools_render_as_resources():
    result = tool_presentation("acme_fetch_asset", {"paths": ["a.txt", "b.txt"]})

    assert result["kind"] == "resource"
    assert result["locations"] == ["a.txt", "b.txt"]
