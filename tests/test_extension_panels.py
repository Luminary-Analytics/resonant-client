"""Panels from capability packs (lumi/gui/extension_panels.py, docs/extensions.md#panels).

A pack's panel is its own HTML, served under /panels/<panel token>/ into
<iframe sandbox="allow-scripts">. These tests cover what the server decides:
which manifests load, which packs list and open panels, and what a panel URL
serves, with which headers, to whom. tests/extension_panels.test.cjs checks the
page's side of the bridge, and tests/extension_panels.browser.cjs checks the
whole path in a real browser.
"""

from __future__ import annotations

import asyncio
import json
import sys
import types
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from lumi import policy as lumi_policy
from lumi.engine.capability_packs import (
    CapabilityPackManager,
    approve_pack,
    pack_relative_path,
    revoke_pack_approval,
)
from lumi.gui import app as gui
from lumi.gui import extension_panels, ws_commands
from lumi.paths import state_home
from tests.gui_access import BASE_URL, LocalClient

REPO = Path(__file__).resolve().parents[1]
PANEL_HTML = "<!doctype html><html><head><title>Demo</title><link rel=\"stylesheet\" href=\"style.css\"></head>" \
             "<body><h1>Demo</h1><script src=\"panel.js\"></script></body></html>"


# ── The manifest ────────────────────────────────────────────────────


def _write(path: Path, text: str) -> None:
    """Exactly these bytes: text mode would turn newlines into CRLF on Windows."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))


def _load(folder: Path, panels, files: dict[str, str] | None = None):
    """Load a pack with these ``ui_panels`` the way Lumi does; ``files`` are written into it first."""
    folder.mkdir(parents=True, exist_ok=True)
    for name, text in (files if files is not None else {"panels/stats/index.html": PANEL_HTML}).items():
        _write(folder / name, text)
    _write(folder / "lumi-pack.json", json.dumps({"id": "p", "name": "P", "manifest_version": 1, "ui_panels": panels}))
    pack, _ = CapabilityPackManager(folder.parent / "no-project", roots=())._load(folder)
    return pack


def _panel(**fields):
    return {"id": "stats", "title": "Build stats", "entry": "panels/stats/index.html", **fields}


def test_a_panel_loads_with_its_id_title_and_entry(tmp_path):
    pack = _load(tmp_path / "pack", [_panel(title="  Build\n stats ", extra="ignored")])
    assert pack.problem == ""
    assert pack.ui_panels == [{"id": "stats", "title": "Build stats", "entry": "panels/stats/index.html"}]
    # A pack without panels still loads.
    assert _load(tmp_path / "plain", None, files={}).problem == ""


@pytest.mark.parametrize("panels, problem", [
    ("stats", "a list of up to 10"),
    ([_panel(id=f"p{n}") for n in range(11)], "a list of up to 10"),
    (["stats"], "needs an id"),
    ([_panel(id="Build Stats")], "needs an id"),
    ([_panel(id="")], "needs an id"),
    ([_panel(), _panel(title="Again")], "two panels are called stats"),
    ([_panel(title="")], "needs a title"),
    ([_panel(title="x" * 61)], "needs a title"),
    ([_panel(title=7)], "needs a title"),
    ([_panel(title="Build\u202estats")], "needs a title"),
    ([_panel(entry="panels/stats/panel.js")], "must be an .html file"),
    ([_panel(entry="/panels/stats/index.html")], "must be an .html file"),
    ([_panel(entry="../outside/index.html")], "must be an .html file"),
    ([_panel(entry="panels/../panels/stats/index.html")], "must be an .html file"),
    ([_panel(entry="panels/./stats/index.html")], "must be an .html file"),
    ([_panel(entry=".hidden/index.html")], "must be an .html file"),
    ([_panel(entry="panels\\stats\\index.html")], "must be an .html file"),
    ([_panel(entry="C:/panels/index.html")], "must be an .html file"),
    ([_panel(entry="panels//stats/index.html")], "must be an .html file"),
    ([_panel(entry="nul.html")], "must be an .html file"),
    ([_panel(entry="panels/stats/index.html.")], "must be an .html file"),
    ([_panel(entry=["panels/stats/index.html"])], "must be an .html file"),
    ([_panel(entry="panels/stats/missing.html")], "isn't a file in the pack"),
    ([_panel(entry="panels/stats")], "must be an .html file"),
])
def test_a_panel_the_manifest_gets_wrong_makes_the_pack_invalid(tmp_path, panels, problem):
    pack = _load(tmp_path / "pack", panels)
    assert problem in pack.problem, pack.problem
    assert pack.status == "unverifiable" and pack.ui_panels == [] and not pack.trusted


def test_a_panel_entry_is_bounded_in_size(tmp_path):
    big = "<!doctype html>" + "x" * (1024 * 1024)
    pack = _load(tmp_path / "pack", [_panel()], files={"panels/stats/index.html": big})
    assert "larger than 1 MB" in pack.problem


@pytest.mark.parametrize("value, expected", [
    ("index.html", "index.html"),
    ("a/b/c.css", "a/b/c.css"),
    ("", None), ("/a.html", None), ("a/", None), ("a//b", None), ("..", None), ("a/../b", None),
    (".git/config", None), ("a/.env", None), ("a\\b", None), ("c:a", None), ("a\x00b", None),
    ("con.txt", None), ("LPT1", None), ("a./b", None), ("a /b", None), ("a" * 301, None),
    ("/".join(["a"] * 17), None), (None, None), (5, None),
])
def test_pack_relative_path(value, expected):
    assert pack_relative_path(value) == expected


# ── An app with an approved pack that has a panel ───────────────────


@pytest.fixture
def gui_state(tmp_path, monkeypatch):
    from lumi.gui import sessions

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.delenv("LUMI_STATE_HOME", raising=False)
    monkeypatch.setattr(sessions, "_is_pytest_temp_path", lambda _: False)
    project = tmp_path / "project"
    project.mkdir()
    state = gui.AppState()
    state.project.set_project(str(project))
    state.available_backends = {"test": {}}
    state.codebase_index = object()
    monkeypatch.setattr(gui, "state", state)
    extension_panels.grants.clear()
    yield state
    extension_panels.grants.clear()


def _write_demo_pack(root: Path, pack_id: str = "demo") -> Path:
    folder = root / pack_id
    files = {
        "panels/demo/index.html": PANEL_HTML,
        "panels/demo/panel.js": "document.title = 'ready';\n",
        "panels/demo/style.css": "body { margin: 0; }\n",
        "panels/demo/data.json": '{"ok": true}\n',
        "panels/demo/notes.py": "print('not a panel file')\n",
        "panels/demo/.secret.txt": "hidden\n",
        "hooks/check.js": "console.log('outside the panel folder')\n",
    }
    for name, text in files.items():
        _write(folder / name, text)
    _write(folder / "lumi-pack.json", json.dumps({
        "id": pack_id, "name": "Demo pack", "version": "1.0.0", "manifest_version": 1,
        "ui_panels": [{"id": "demo", "title": "Demo panel", "entry": "panels/demo/index.html"}],
    }))
    return folder


def _approve(state, pack_id: str = "demo") -> None:
    manager = CapabilityPackManager(state.project.project_path, configured=state.settings.get("plugins") or {})
    pack = next(p for p in manager.discover() if p.id == pack_id)
    state.settings.set("plugins", None, approve_pack(state.settings.get("plugins") or {}, pack,
                                                     reviewed_digest=pack.digest))


@pytest.fixture
def demo(gui_state):
    folder = _write_demo_pack(state_home() / "packs")
    _approve(gui_state)
    return folder


class _Socket:
    def __init__(self):
        self.sent = []

    async def send_json(self, payload):
        self.sent.append(payload)


def _send(state, command: str, message: dict | None = None, socket: _Socket | None = None) -> list[dict]:
    socket = socket or _Socket()
    asyncio.run(ws_commands.HANDLERS[command](
        ws_commands.CommandContext(ws=socket, state=state, msg=message or {}),
    ))
    return socket.sent


def _open(state, socket: _Socket | None = None) -> dict:
    [reply] = _send(state, "extension_panel_open", {"pack_id": "demo", "panel_id": "demo", "request_id": "r1"},
                    socket)
    assert reply["event"] == "extension_panel_opened" and reply["request_id"] == "r1"
    return reply


def _get(path: str, **headers):
    with TestClient(gui.app, base_url=BASE_URL) as client:
        return client.get(path, headers=headers)


def test_an_approved_packs_panel_is_listed_and_opens(demo, gui_state):
    [listing] = _send(gui_state, "extension_panels")
    assert listing["enabled"] is True and listing["reason"] == ""
    assert listing["panels"] == [{"pack": "demo", "pack_name": "Demo pack", "panel": "demo", "title": "Demo panel"}]

    reply = _open(gui_state)
    assert "error" not in reply
    assert (reply["pack"], reply["panel"], reply["title"], reply["pack_name"]) == \
        ("demo", "demo", "Demo panel", "Demo pack")
    assert reply["url"] == f"/panels/{reply['token']}/index.html"
    assert len(reply["token"]) == 32


def test_a_panel_page_gets_its_own_policy_and_the_bridge(demo, gui_state):
    token = _open(gui_state)["token"]
    page = _get(f"/panels/{token}/index.html")

    assert page.status_code == 200
    assert page.headers["content-type"] == "text/html; charset=utf-8"
    directives = dict((part.split()[0], part.split()[1:]) for part in page.headers["content-security-policy"].split("; "))
    base = f"http://127.0.0.1:48123/panels/{token}/"
    assert directives == {
        "default-src": ["'none'"],
        "script-src": [base],
        "style-src": [base],
        "img-src": [base, "data:", "blob:"],
        "connect-src": ["'none'"],
        "frame-src": ["'none'"],
        "child-src": ["'none'"],
        "worker-src": ["'none'"],
        "object-src": ["'none'"],
        "base-uri": ["'none'"],
        "form-action": ["'none'"],
        "frame-ancestors": ["'self'"],
        # Opaque origin however the URL is opened; never allow-same-origin.
        "sandbox": ["allow-scripts"],
    }
    assert page.headers["x-content-type-options"] == "nosniff"
    assert page.headers["cache-control"] == "no-store"
    assert page.headers["referrer-policy"] == "no-referrer"
    assert "camera=()" in page.headers["permissions-policy"]
    assert "x-frame-options" not in page.headers
    # Lumi's bridge comes first in <head>, before the panel's own scripts.
    assert page.text.startswith(f'<!doctype html><html><head><script src="/panels/{token}/.lumi/bridge.js"></script>'
                                "<title>Demo</title>")

    bridge = _get(f"/panels/{token}/.lumi/bridge.js")
    assert bridge.status_code == 200 and bridge.headers["content-type"] == "text/javascript; charset=utf-8"
    assert bridge.content == (REPO / "lumi" / "gui" / "static" / "panel_frame.js").read_bytes()
    script = _get(f"/panels/{token}/panel.js")
    assert script.status_code == 200 and script.text == "document.title = 'ready';\n"
    assert _get(f"/panels/{token}/style.css").headers["content-type"] == "text/css; charset=utf-8"
    assert "sandbox allow-scripts" in script.headers["content-security-policy"]


@pytest.mark.parametrize("path", [
    "%2e%2e/%2e%2e/lumi-pack.json",   # the pack's manifest, above the panel's folder
    "..%2f..%2fhooks%2fcheck.js",
    "%2e%2e%5c%2e%2e%5clumi-pack.json",
    "lumi-pack.json",                 # a pack file outside the panel's folder
    "notes.py",                       # a type panels can't serve
    "data.json",                      # nor can they load JSON: no fetch
    ".secret.txt",                    # dot files are never served
    "missing.js",
    "INDEX.HTML",                     # only the approved names, exactly
    "index.html%00.js",
    "index.html::$DATA",
    "",
])
def test_only_the_panels_own_approved_files_are_served(demo, gui_state, path):
    token = _open(gui_state)["token"]
    response = _get(f"/panels/{token}/{path}")
    assert response.status_code == 404, response.text
    assert "sandbox allow-scripts" in response.headers["content-security-policy"]
    # The token still works: a refused path isn't a reason to close the panel.
    assert _get(f"/panels/{token}/index.html").status_code == 200


def test_a_panel_url_needs_a_live_token_and_this_servers_host(demo, gui_state):
    token = _open(gui_state)["token"]
    assert _get("/panels/" + "A" * 32 + "/index.html").status_code == 404
    assert _get("/panels/short/index.html").status_code == 404
    assert _get(f"/panels/{token}/index.html", Host="evil.example:48123").status_code == 403
    # Another site's page is refused; a sandboxed frame sends no Origin, or "null".
    assert _get(f"/panels/{token}/index.html", Origin="https://evil.example").status_code == 403
    assert _get(f"/panels/{token}/index.html", Origin="null").status_code == 200
    assert _get(f"/panels/{token}/index.html", Origin=BASE_URL).status_code == 200
    with TestClient(gui.app, base_url=BASE_URL) as client:
        assert client.post(f"/panels/{token}/index.html").status_code == 405


def test_a_panel_page_named_with_spaces_opens(gui_state):
    folder = _write_demo_pack(state_home() / "packs")
    _write(folder / "panels" / "demo" / "Build stats é.html", PANEL_HTML)
    manifest = json.loads((folder / "lumi-pack.json").read_text(encoding="utf-8"))
    manifest["ui_panels"][0]["entry"] = "panels/demo/Build stats é.html"
    _write(folder / "lumi-pack.json", json.dumps(manifest))
    _approve(gui_state)
    reply = _open(gui_state)
    assert reply["url"] == f"/panels/{reply['token']}/Build%20stats%20%C3%A9.html"
    assert _get(reply["url"]).status_code == 200


def test_a_panel_loads_only_as_a_frame_and_its_scripts_styles_and_images(demo, gui_state):
    token = _open(gui_state)["token"]
    for destination in ("iframe", "frame"):
        assert _get(f"/panels/{token}/index.html", **{"Sec-Fetch-Dest": destination}).status_code == 200
    assert _get(f"/panels/{token}/panel.js", **{"Sec-Fetch-Dest": "script"}).status_code == 200
    assert _get(f"/panels/{token}/style.css", **{"Sec-Fetch-Dest": "style"}).status_code == 200
    # A panel URL pasted into the address bar, or fetched, is refused.
    for destination in ("document", "empty", "worker", "object", "embed"):
        refused = _get(f"/panels/{token}/index.html", **{"Sec-Fetch-Dest": destination})
        assert refused.status_code == 403 and "opens inside Lumi" in refused.text


def test_closing_the_panel_withdraws_its_token_but_only_for_its_page(demo, gui_state):
    page = _Socket()
    token = _open(gui_state, page)["token"]
    _send(gui_state, "extension_panel_close", {"token": token}, _Socket())
    assert _get(f"/panels/{token}/index.html").status_code == 200
    _send(gui_state, "extension_panel_close", {"token": token}, page)
    assert _get(f"/panels/{token}/index.html").status_code == 404


def test_a_page_that_goes_away_takes_its_panels_with_it(demo, gui_state):
    with LocalClient(gui.app) as client:
        with client.websocket_connect("/ws") as ws:
            ws.send_json({"command": "extension_panel_open", "pack_id": "demo", "panel_id": "demo",
                          "request_id": "r"})
            while (reply := ws.receive_json())["event"] != "extension_panel_opened":
                pass
            assert _get(reply["url"]).status_code == 200
    assert _get(reply["url"]).status_code == 404


def test_a_revoked_or_disabled_pack_loses_its_panels_at_once(demo, gui_state):
    token = _open(gui_state)["token"]
    manager = CapabilityPackManager(gui_state.project.project_path, configured=gui_state.settings.get("plugins"))
    pack = next(p for p in manager.discover() if p.id == "demo")
    gui_state.settings.set("plugins", None, revoke_pack_approval(gui_state.settings.get("plugins"), pack))

    refused = _get(f"/panels/{token}/panel.js")
    assert refused.status_code == 403 and "Approve the Demo pack" in refused.text
    [listing] = _send(gui_state, "extension_panels")
    assert listing["panels"] == []
    assert "Approve the Demo pack" in _open(gui_state)["error"]

    # Approved again, the old token stays withdrawn; a new one works.
    _approve(gui_state)
    assert _get(f"/panels/{token}/panel.js").status_code == 404
    token = _open(gui_state)["token"]
    assert _get(f"/panels/{token}/panel.js").status_code == 200

    # Disabled: the approval's enabled flag.
    plugins = json.loads(json.dumps(gui_state.settings.get("plugins")))
    for approval in plugins["demo"]["approvals"].values():
        approval["enabled"] = False
    gui_state.settings.set("plugins", None, plugins)
    assert _get(f"/panels/{token}/panel.js").status_code == 403
    assert _send(gui_state, "extension_panels")[0]["panels"] == []


def test_a_changed_pack_loses_its_panels(demo, gui_state):
    token = _open(gui_state)["token"]
    _write(demo / "panels" / "demo" / "panel.js", "fetch('https://evil.example')\n")
    refused = _get(f"/panels/{token}/panel.js")
    assert refused.status_code == 403 and "changed since you approved it" in refused.text
    assert _send(gui_state, "extension_panels")[0]["panels"] == []
    assert "changed since you approved it" in _open(gui_state)["error"]


def test_the_bytes_served_are_the_bytes_approved(demo, gui_state, monkeypatch):
    """A file that changes after the pack's check, before it is read, is refused."""
    token = _open(gui_state)["token"]
    verified = CapabilityPackManager.verified_files

    def stale(self, pack):
        hashes = verified(self, pack)
        return {**hashes, "panels/demo/panel.js": "0" * 64} if hashes else hashes

    monkeypatch.setattr(CapabilityPackManager, "verified_files", stale)
    refused = _get(f"/panels/{token}/panel.js")
    assert refused.status_code == 403 and "changed since you approved it" in refused.text


def test_panel_files_are_bounded_in_size(demo, gui_state, monkeypatch):
    monkeypatch.setattr(extension_panels, "MAX_PANEL_FILE_BYTES", 10)
    token = _open(gui_state)["token"]
    assert _get(f"/panels/{token}/panel.js").status_code == 413


def test_settings_can_turn_panels_off(demo, gui_state):
    token = _open(gui_state)["token"]
    gui_state.settings.set("security", "extension_panels", False)
    [listing] = _send(gui_state, "extension_panels")
    assert listing == {"event": "extension_panels", "enabled": False, "panels": [],
                       "reason": "Panels from capability packs are off in Settings > Privacy & security.",
                       "project": str(gui_state.project.project_path)}
    assert _open(gui_state)["error"] == listing["reason"]
    assert _get(f"/panels/{token}/index.html").status_code == 403
    # The Settings page may change it, and only to on or off.
    assert ws_commands._socket_setting_value("security", "extension_panels", True) is True
    with pytest.raises(ValueError, match="on or off"):
        ws_commands._socket_setting_value("security", "extension_panels", "yes")


def _policy(**fields):
    return lumi_policy.Policy(organization="Acme", source="test", **fields)


@pytest.mark.parametrize("policy, reason", [
    (_policy(packs_allowed=("team-*",)), ""),
    (_policy(require_signed=True), ""),
    (_policy(registry_only=True), ""),
    (_policy(settings={"security.extension_panels": False}),
     "Acme's policy turns off panels from capability packs."),
    # A value the switch can't be leaves panels off.
    (_policy(settings={"security.extension_panels": "no"}), "Acme's policy turns off panels from capability packs."),
])
def test_organization_policy_refuses_panels(demo, gui_state, policy, reason):
    token = _open(gui_state)["token"]
    lumi_policy.set_for_tests(policy)
    [listing] = _send(gui_state, "extension_panels")
    assert listing["panels"] == [] and listing["reason"] == reason
    assert _open(gui_state)["error"]
    assert _get(f"/panels/{token}/index.html").status_code == 403


def test_a_policy_that_cant_be_used_turns_panels_off(demo, gui_state):
    lumi_policy.set_for_tests(None, error="The organization policy couldn't be loaded: bad JSON")
    [listing] = _send(gui_state, "extension_panels")
    assert listing["enabled"] is False and "couldn't be loaded" in listing["reason"]


def test_a_pack_the_policy_allows_keeps_its_panel(demo, gui_state):
    lumi_policy.set_for_tests(_policy(packs_allowed=("demo",)))
    assert [row["panel"] for row in _send(gui_state, "extension_panels")[0]["panels"]] == ["demo"]


def test_an_approved_project_pack_lists_its_panels(gui_state):
    project = Path(gui_state.project.project_path)
    _write_demo_pack(project / ".lumi" / "packs", "repo-pack")
    assert _send(gui_state, "extension_panels")[0]["panels"] == []  # not approved yet
    _approve(gui_state, "repo-pack")
    assert [row["pack"] for row in _send(gui_state, "extension_panels")[0]["panels"]] == ["repo-pack"]


def test_opening_is_checked_and_recorded(demo, gui_state, monkeypatch):
    from lumi import audit

    records = []
    monkeypatch.setattr(audit, "record", lambda kind, **fields: records.append((kind, fields)))
    for pack_id, panel_id in (("demo", "other"), ("missing", "demo"), ("../demo", "demo"), ("demo", "Demo!")):
        [reply] = _send(gui_state, "extension_panel_open", {"pack_id": pack_id, "panel_id": panel_id})
        assert reply["error"] and "token" not in reply
    assert records == []
    _open(gui_state)
    assert records == [("extension.panel_open", {"pack": "demo", "panel": "demo"})]


def test_a_failure_to_list_panels_keeps_the_connection(gui_state, monkeypatch):
    def broken(state):
        raise RuntimeError("disk gone")

    monkeypatch.setattr(extension_panels, "listing", broken)
    [reply] = _send(gui_state, "extension_panels")
    assert reply["enabled"] is False and reply["panels"] == []


# ── Pieces ──────────────────────────────────────────────────────────


def test_panel_tokens_are_bounded_in_number_and_age(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(extension_panels.time, "monotonic", lambda: clock[0])
    grants = extension_panels.PanelGrants(ttl=60, limit=2)
    pack = types.SimpleNamespace(id="demo", path=str(Path.cwd()))
    panel = {"id": "demo", "entry": "index.html"}
    first, second = (grants.issue(project="p", pack=pack, panel=panel, owner=1) for _ in range(2))
    third = grants.issue(project="p", pack=pack, panel=panel, owner=2)
    assert grants.get(first.token) is None and grants.get(second.token) and grants.get(third.token)
    grants.revoke_owner(1)
    assert grants.get(second.token) is None and grants.get(third.token)
    clock[0] += 61
    assert grants.get(third.token) is None
    assert len({grants.issue(project="p", pack=pack, panel=panel, owner=1).token for _ in range(5)}) == 5


@pytest.mark.parametrize("html, expected", [
    (b"<!DOCTYPE html><HTML lang=en><HEAD><title>t</title>", b"<!DOCTYPE html><HTML lang=en><HEAD>TAG<title>t</title>"),
    (b"<html><header>no head</header>", b"<html>TAG<header>no head</header>"),
    (b"<!doctype html><p>bare", b"<!doctype html>TAG<p>bare"),
    (b"<p>fragment", b"TAG<p>fragment"),
    (b"\xef\xbb\xbf<p>bom", b"\xef\xbb\xbfTAG<p>bom"),
])
def test_the_bridge_goes_first(html, expected):
    tag = b'<script src="/panels/T/.lumi/bridge.js"></script>'
    assert extension_panels.with_bridge(html, "T") == expected.replace(b"TAG", tag)


def test_a_packs_name_shows_as_one_plain_line():
    """The manifest's name isn't checked when a pack loads; the page gets it cleaned and bounded."""
    assert extension_panels._label("Acme\u202e\u200b  builds\n\x07") == "Acme builds"
    assert extension_panels._label("x" * 500) == "x" * 80
    assert extension_panels._label("\u200b") == "Unnamed"


def test_the_panel_policy_names_this_panels_path_only():
    policy = extension_panels.content_security_policy("localhost:5000", "tok", "https")
    assert "script-src https://localhost:5000/panels/tok/;" in policy
    # A bracketed IPv6 literal isn't a valid source: 'self' stands in.
    assert "script-src 'self';" in extension_panels.content_security_policy("[::1]:5000", "tok")
    assert "allow-same-origin" not in policy


def test_the_app_page_frames_only_its_own_server():
    from lumi.gui.local_access import content_security_policy

    assert "frame-src 'self'" in content_security_policy({"server": ("127.0.0.1", 5000), "scheme": "http"})


def test_lumi_extension_check_lists_panels(tmp_path, capsys):
    from lumi.extension_check import main

    folder = _write_demo_pack(tmp_path)
    assert main(["check", str(folder), "--no-run"]) == 0
    assert "Panel Demo panel opens panels/demo/index.html in a sandboxed frame." in capsys.readouterr().out


def test_the_manifest_schema_describes_panels():
    jsonschema = pytest.importorskip("jsonschema")
    schema = json.loads((REPO / "sdk" / "schema" / "lumi-pack.schema.json").read_text(encoding="utf-8"))
    jsonschema.validate({"id": "p", "ui_panels": [_panel()]}, schema)
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate({"id": "p", "ui_panels": [_panel(entry="panels/stats/panel.js")]}, schema)


def test_the_panels_assets_ship():
    spec = (REPO / "packaging" / "lumi.spec").read_text(encoding="utf-8")
    page = (REPO / "lumi" / "gui" / "templates" / "index.html").read_text(encoding="utf-8")
    for name in ("panels_view.js", "panels_view.css", "panel_frame.js"):
        assert f'"static" / "{name}"' in spec
        for policy in ("bundle-policy.json", "bundle-policy-linux.json", "bundle-policy-macos.json"):
            required = json.loads((REPO / "packaging" / policy).read_text(encoding="utf-8"))["required_globs"]
            assert f"_internal/lumi/gui/static/{name}" in required, (policy, name)
    assert '/static/panels_view.js?v=' in page and '/static/panels_view.css?v=' in page
    # panel_frame.js is served into panels, never loaded by the app page.
    assert "panel_frame.js" not in page


def test_the_desktop_window_accepts_only_plain_bridge_calls(monkeypatch):
    """A panel shares the desktop window, whose bridge WebKit gives every frame."""
    webview_util = pytest.importorskip("webview.util")
    from lumi.gui import webview_bridge

    calls = []
    platform = types.ModuleType("webview.platforms.lumi_test")
    platform.js_bridge_call = lambda window, name, param, value_id: calls.append((name, value_id))
    monkeypatch.setitem(sys.modules, "webview.platforms.lumi_test", platform)
    monkeypatch.setattr(webview_util, "js_bridge_call", webview_util.js_bridge_call)
    webview_bridge.check_bridge_calls()
    webview_bridge.check_bridge_calls()  # once is enough; twice changes nothing

    for name, value_id in (("minimize", "123"), ("pywebviewMoveWindow", "move"), ("is_maximized", "a1-b_2")):
        platform.js_bridge_call(object(), name, [], value_id)
    for name, value_id in (("minimize", '5"](0);steal();//'), ("__class__", "1"), ("_win", "1"),
                           ("api.close", "1"), ("close", None), (None, "1"), ("x" * 65, "1")):
        platform.js_bridge_call(object(), name, [], value_id)
    assert calls == [("minimize", "123"), ("pywebviewMoveWindow", "move"), ("is_maximized", "a1-b_2")]
    assert getattr(webview_util.js_bridge_call, "_lumi_checked", False)
