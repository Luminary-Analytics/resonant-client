"""A new tester's first run: the default mode, refusals, connections and notices.

Found running the packaged app the way an alpha tester would, on a new
Windows computer: a refused message left the composer "running", unattended
work ran in Full-auto whatever mode the conversation was in, Ollama could only
be set up from the welcome screen, ChatGPT sign-in said nothing new without
the Codex CLI, and the diagnostics file pointed at GitHub issues.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest

from lumi import policy as lumi_policy
from lumi.gui import swarming, ws_commands
from lumi.gui.app import AppState
from lumi.gui.settings import DEFAULT_PERMISSION_MODE, SettingsManager
from lumi.secrets_store import PLACEHOLDER
from tests.test_secret_hygiene import memory_keyring  # noqa: F401  (a fixture: an in-memory credential store)
from tests.test_ws_command_registry import _run, _StubWS


def _without_full_auto() -> None:
    lumi_policy.set_for_tests(lumi_policy.parse(
        {"schema": "lumi.policy/v1", "organization": "Acme",
         "permissions": {"allowed_modes": ["ask", "auto-edit"]}}, source="test"))


def _ctx(state, msg):
    return ws_commands.CommandContext(ws=_StubWS(), state=state, msg=msg, runs=None)


# ── New installs start in Auto-edit ────────────────────────────────────────


def test_a_new_install_starts_in_auto_edit_and_saves_it(tmp_path):
    path = tmp_path / "settings.json"
    settings = SettingsManager(path)
    assert DEFAULT_PERMISSION_MODE == "auto-edit"
    assert settings.get("general", "default_permission_mode") == "auto-edit"
    # Saved like every default, so a later default can't change this install.
    assert json.loads(path.read_text(encoding="utf-8"))["general"]["default_permission_mode"] == "auto-edit"
    assert SettingsManager(path).get("general", "default_permission_mode") == "auto-edit"


@pytest.mark.parametrize("saved", ["bypass", "ask", "auto-edit", "plan"])
def test_an_existing_settings_file_keeps_its_mode(tmp_path, saved):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"general": {"default_permission_mode": saved, "theme": "light"}}), encoding="utf-8")
    assert SettingsManager(path).get("general", "default_permission_mode") == saved


@pytest.mark.parametrize("general", [{"theme": "light"}, {"default_permission_mode": ""}, None],
                         ids=["no key", "empty", "no general section"])
def test_an_existing_file_without_a_mode_keeps_the_earlier_full_auto(tmp_path, general):
    # Every earlier first launch wrote Full-auto into the file; one without a
    # mode was written by hand or a tool, and ran in Full-auto until now.
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({} if general is None else {"general": general}), encoding="utf-8")
    assert SettingsManager(path).get("general", "default_permission_mode") == "bypass"
    assert json.loads(path.read_text(encoding="utf-8"))["general"]["default_permission_mode"] == "bypass"


def test_an_unreadable_settings_file_runs_on_the_new_default_and_is_kept(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text("{not json", encoding="utf-8")
    settings = SettingsManager(path)
    assert settings.get("general", "default_permission_mode") == "auto-edit"
    assert path.read_text(encoding="utf-8") == "{not json"


# ── settings.json that can't be read is never written over ─────────────────
# Before, a file with a byte-order mark (Notepad, Windows PowerShell 5.1) or
# one another program held for a moment (an antivirus scan) was read as empty
# and rewritten with defaults: every setting and key it held was lost.

_SAVED = {"general": {"default_permission_mode": "ask", "theme": "light", "display_name": "Alex"},
          "api_keys": {"openrouter": "sk-or-kept"}}


def test_a_settings_file_with_a_byte_order_mark_is_read(tmp_path):
    path = tmp_path / "settings.json"
    path.write_bytes(b"\xef\xbb\xbf" + json.dumps(_SAVED).encode("utf-8"))
    settings = SettingsManager(path)
    assert settings.load_error == ""
    assert (settings.get("general", "display_name"), settings.get("general", "theme")) == ("Alex", "light")
    assert settings.get("general", "default_permission_mode") == "ask"
    assert settings.get("api_keys", "openrouter") == "sk-or-kept"
    # Saved again without the mark, and with everything it held.
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["general"]["display_name"] == "Alex" and saved["api_keys"]["openrouter"] == "sk-or-kept"


@pytest.mark.parametrize("content", [b"{not json", b"[1, 2]", b"\xff\xfe{\x00}\x00", b""],
                         ids=["invalid JSON", "not an object", "not UTF-8", "empty"])
def test_a_settings_file_that_cant_be_parsed_is_kept_and_never_written_over(tmp_path, monkeypatch, content):
    from lumi.gui import settings as settings_module

    monkeypatch.setattr(settings_module, "_FILE_RETRY_SECONDS", 0)
    path = tmp_path / "settings.json"
    path.write_bytes(content)
    settings = SettingsManager(path)
    assert str(path) in settings.load_error and "won't save any change" in settings.load_error
    assert settings.get("general", "default_permission_mode") == "auto-edit"  # defaults meanwhile
    settings.set("general", "theme", "light")
    assert settings.get("general", "theme") == "light"  # for this run
    assert path.read_bytes() == content
    assert not settings.backup_path.exists()
    # The page and the terminal say so (Settings, the banner above the message box).
    assert settings.get_masked()["_meta"]["load_error"] == settings.load_error


def test_a_settings_file_held_for_a_moment_is_read_once_it_is_free(tmp_path, monkeypatch):
    from pathlib import Path

    from lumi.gui import settings as settings_module

    monkeypatch.setattr(settings_module, "_FILE_RETRY_SECONDS", 0)
    path = tmp_path / "settings.json"
    path.write_text(json.dumps(_SAVED), encoding="utf-8")
    real_read = Path.read_text
    attempts = []

    def held(self, *args, **kwargs):
        if self == path and len(attempts) < 2:
            attempts.append("held")
            raise PermissionError(13, "The process cannot access the file because it is being used by another process")
        return real_read(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", held)
    settings = SettingsManager(path)
    assert attempts == ["held", "held"] and settings.load_error == ""
    assert settings.get("general", "display_name") == "Alex"


def test_a_settings_file_that_stays_locked_is_kept(tmp_path, monkeypatch):
    from pathlib import Path

    from lumi.gui import settings as settings_module

    monkeypatch.setattr(settings_module, "_FILE_RETRY_SECONDS", 0)
    path = tmp_path / "settings.json"
    path.write_text(json.dumps(_SAVED), encoding="utf-8")
    real_read = Path.read_text

    def locked(self, *args, **kwargs):
        if self == path:
            raise PermissionError(13, "The process cannot access the file because it is being used by another process")
        return real_read(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", locked)
    settings = SettingsManager(path)
    monkeypatch.setattr(Path, "read_text", real_read)
    assert "being used by another process" in settings.load_error
    settings.set("general", "theme", "dark")
    assert json.loads(path.read_text(encoding="utf-8")) == _SAVED


def test_each_save_keeps_the_file_as_it_was_in_a_backup(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps(_SAVED), encoding="utf-8")
    settings = SettingsManager(path)
    before = json.loads(path.read_text(encoding="utf-8"))
    settings.set("general", "theme", "dark")
    assert settings.backup_path == tmp_path / "settings.json.bak"
    # The file as it was, without its API key (the store is off here, so it's plain text in the file).
    assert json.loads(settings.backup_path.read_text(encoding="utf-8")) == {
        **before, "api_keys": {name: "" for name in before["api_keys"]}}
    assert before["api_keys"]["openrouter"] == "sk-or-kept"
    assert json.loads(path.read_text(encoding="utf-8"))["general"]["theme"] == "dark"
    # One backup: the next save replaces it with the file before that save.
    settings.set("general", "theme", "light")
    assert json.loads(settings.backup_path.read_text(encoding="utf-8"))["general"]["theme"] == "dark"
    assert sorted(item.name for item in tmp_path.iterdir()) == ["settings.json", "settings.json.bak"]
    # A new install has nothing to back up.
    fresh = SettingsManager(tmp_path / "new" / "settings.json")
    assert fresh.load_error == "" and not fresh.backup_path.exists()


_CREDENTIALS = {"api_keys": {"openai": "sk-plain-openai-key-7f3a", "lumi_cloud_device_key": "ZGV2aWNlLWtleS1ieXRlcw=="},
                "mcp_servers": {"github": {"command": "gh-mcp", "env": {"GITHUB_TOKEN": "ghp_tokenvalue0123456789"},
                                           "headers": {"Authorization": "Bearer mcp-bearer-value-42"}}},
                "network": {"proxy_password": "proxy-secret-99"},
                "general": {"theme": "light", "default_permission_mode": "ask"}}
_CREDENTIAL_VALUES = ["sk-plain-openai-key-7f3a", "ZGV2aWNlLWtleS1ieXRlcw==", "ghp_tokenvalue0123456789",
                      "mcp-bearer-value-42", "proxy-secret-99"]


@pytest.mark.usefixtures("memory_keyring")
def test_the_backup_keeps_no_credential_the_store_just_took(tmp_path):
    # The credential store takes the keys out of settings.json on load; the
    # file as it was (plain-text keys) must not survive in the backup.
    path = tmp_path / ".lumi" / "settings.json"
    path.parent.mkdir()
    path.write_text(json.dumps(_CREDENTIALS), encoding="utf-8")
    settings = SettingsManager(path)
    assert settings.get("api_keys", "openai") == "sk-plain-openai-key-7f3a"  # from the store
    backup = settings.backup_path.read_text(encoding="utf-8")
    assert [value for value in _CREDENTIAL_VALUES if value in backup] == []
    kept = json.loads(backup)
    assert kept["general"] == _CREDENTIALS["general"] and kept["mcp_servers"]["github"]["command"] == "gh-mcp"
    # The next backup is of the file the store left: its placeholders say where the keys are.
    settings.set("general", "theme", "dark")
    backup = settings.backup_path.read_text(encoding="utf-8")
    assert [value for value in _CREDENTIAL_VALUES if value in backup] == []
    assert json.loads(backup)["api_keys"]["openai"] == PLACEHOLDER


def test_the_backup_keeps_no_credential_without_a_store(tmp_path):
    # LUMI_KEYCHAIN=off (conftest): keys stay in settings.json itself, never in the backup.
    path = tmp_path / "settings.json"
    path.write_text(json.dumps(_CREDENTIALS), encoding="utf-8")
    settings = SettingsManager(path)
    settings.set("api_keys", "anthropic", "sk-ant-saved-later-5521")
    settings.set("general", "theme", "dark")
    backup = settings.backup_path.read_text(encoding="utf-8")
    assert [value for value in [*_CREDENTIAL_VALUES, "sk-ant-saved-later-5521"] if value in backup] == []
    assert "sk-ant-saved-later-5521" in path.read_text(encoding="utf-8")


def test_a_save_windows_wont_replace_in_one_step_is_written_in_place(tmp_path, monkeypatch):
    # Windows won't replace a file another program has open; the file is then
    # written in place, as before this change, once the backup is made.
    from lumi.gui import settings as settings_module

    monkeypatch.setattr(settings_module, "_FILE_RETRY_SECONDS", 0)
    path = tmp_path / "settings.json"
    path.write_text(json.dumps(_SAVED), encoding="utf-8")
    settings = SettingsManager(path)
    real_replace = settings_module.os.replace

    def refuse(source, target):
        if str(target) == str(path):
            raise PermissionError(13, "Access is denied")
        return real_replace(source, target)

    monkeypatch.setattr(settings_module.os, "replace", refuse)
    settings.set("general", "theme", "dark")
    assert json.loads(path.read_text(encoding="utf-8"))["general"]["theme"] == "dark"
    assert settings.save_error == "" and settings.get_masked()["_meta"]["save_error"] == ""
    assert json.loads(settings.backup_path.read_text(encoding="utf-8"))["general"]["theme"] == "light"
    # No half-written copy is left beside it.
    assert sorted(item.name for item in tmp_path.iterdir()) == ["settings.json", "settings.json.bak"]


def test_a_save_that_fails_is_reported_and_leaves_the_file_whole(tmp_path, monkeypatch):
    from pathlib import Path

    from lumi.gui import settings as settings_module

    monkeypatch.setattr(settings_module, "_FILE_RETRY_SECONDS", 0)
    path = tmp_path / "settings.json"
    path.write_text(json.dumps(_SAVED), encoding="utf-8")
    settings = SettingsManager(path)
    before = path.read_text(encoding="utf-8")
    real_replace, real_write = settings_module.os.replace, Path.write_text

    def refuse(source, target):
        if str(target) == str(path):
            raise PermissionError(13, "Access is denied")
        return real_replace(source, target)

    def refuse_write(self, *args, **kwargs):
        if self == path:
            raise PermissionError(13, "The process cannot access the file because another process has locked it")
        return real_write(self, *args, **kwargs)

    monkeypatch.setattr(settings_module.os, "replace", refuse)
    monkeypatch.setattr(Path, "write_text", refuse_write)
    settings.set("general", "theme", "dark")
    assert path.read_text(encoding="utf-8") == before
    assert "couldn't save its settings" in settings.save_error and str(path) in settings.save_error
    # The page shows it (Settings and the banner above the message box).
    assert settings.get_masked()["_meta"]["save_error"] == settings.save_error
    assert settings.get("general", "theme") == "dark"  # for this run
    # The next save that reaches the file clears it.
    monkeypatch.setattr(settings_module.os, "replace", real_replace)
    monkeypatch.setattr(Path, "write_text", real_write)
    settings.set("general", "theme", "light")
    assert settings.save_error == "" and json.loads(path.read_text(encoding="utf-8"))["general"]["theme"] == "light"
    assert sorted(item.name for item in tmp_path.iterdir()) == ["settings.json", "settings.json.bak"]


def test_a_save_on_the_event_loop_never_waits_for_a_held_file(tmp_path, monkeypatch):
    from lumi.gui import settings as settings_module

    monkeypatch.setattr(settings_module, "_FILE_RETRY_SECONDS", 0)
    path = tmp_path / "settings.json"
    path.write_text(json.dumps(_SAVED), encoding="utf-8")
    settings = SettingsManager(path)
    real_replace = settings_module.os.replace
    tries = []

    def refuse(source, target):
        if str(target) == str(path):
            tries.append(target)
            raise PermissionError(13, "Access is denied")
        return real_replace(source, target)

    monkeypatch.setattr(settings_module.os, "replace", refuse)

    async def switch_model():  # as a WebSocket handler saves on the event loop
        settings.set("general", "default_model", "qwen3-coder:30b")

    asyncio.run(switch_model())
    assert len(tries) == 1  # one try, then written in place: the loop never sleeps on it
    assert json.loads(path.read_text(encoding="utf-8"))["general"]["default_model"] == "qwen3-coder:30b"
    tries.clear()
    settings.set("general", "default_model", "stub-model")  # off the loop it waits, as a moment's lock needs
    assert len(tries) == settings_module._FILE_ATTEMPTS


def test_the_app_is_told_when_saves_stop_or_start_reaching_the_file(tmp_path, monkeypatch):
    from pathlib import Path

    from lumi.gui import settings as settings_module

    monkeypatch.setattr(settings_module, "_FILE_RETRY_SECONDS", 0)
    path = tmp_path / "settings.json"
    path.write_text(json.dumps(_SAVED), encoding="utf-8")
    settings = SettingsManager(path)
    told = []

    def listener():
        assert not settings._lock.locked()  # told after the save: reading the settings here doesn't wait
        told.append(settings.get_masked()["_meta"]["save_error"])

    settings.on_save_error_changed = listener
    settings.set("general", "theme", "dark")
    assert told == []  # saved as before: nothing to tell
    real_replace, real_write = settings_module.os.replace, Path.write_text

    def refuse(source, target):
        if str(target) == str(path):
            raise PermissionError(13, "Access is denied")
        return real_replace(source, target)

    def refuse_write(self, *args, **kwargs):
        if self == path:
            raise PermissionError(13, "The process cannot access the file because another process has locked it")
        return real_write(self, *args, **kwargs)

    monkeypatch.setattr(settings_module.os, "replace", refuse)
    monkeypatch.setattr(Path, "write_text", refuse_write)
    settings.set("general", "theme", "light")
    settings.update_section("general", {"display_name": "Alex Morgan"})  # still failing: told once
    assert len(told) == 1 and "couldn't save its settings" in told[0]
    monkeypatch.setattr(settings_module.os, "replace", real_replace)
    monkeypatch.setattr(Path, "write_text", real_write)
    settings.set("general", "theme", "light")
    assert told[1:] == [""]
    # A listener that fails doesn't fail the save.
    settings.on_save_error_changed = lambda: 1 / 0
    monkeypatch.setattr(settings_module.os, "replace", refuse)
    monkeypatch.setattr(Path, "write_text", refuse_write)
    settings.set("general", "theme", "dark")
    assert "couldn't save its settings" in settings.save_error


def test_the_page_is_sent_the_settings_files_state_whatever_saved():
    from lumi.gui import app as gui_app

    # The app's settings tell it (a background save too) ...
    assert gui_app.state.settings.on_save_error_changed == gui_app.state._settings_file_changed
    # ... and it sends only the file's state, which leaves fields being edited alone.
    state = AppState.__new__(AppState)
    state.settings = SimpleNamespace(load_error="", save_error="Lumi couldn't save its settings to settings.json.")
    sent = []
    state._push_ws_event = sent.append
    state._settings_file_changed()
    assert sent == [{"event": "settings_file", "load_error": "",
                     "save_error": "Lumi couldn't save its settings to settings.json."}]


@pytest.fixture
def held(tmp_path):
    """Hold ``path`` open from another process with a share mode: ``held(path, share)``.

    As an antivirus scan, a sync client or an editor does (tests/fixtures/hold_open.py).
    """
    import subprocess
    from pathlib import Path

    holders = []
    script = Path(__file__).parent / "fixtures" / "hold_open.py"

    def hold(path, share):
        process = subprocess.Popen([sys.executable, str(script), str(path), str(share)], stdin=subprocess.PIPE,
                                   stdout=subprocess.PIPE, text=True)
        holders.append(process)
        assert process.stdout.readline().strip() == "open"
        return process

    yield hold
    for process in holders:
        process.stdin.close()
        process.wait(timeout=10)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows' sharing rules for open files")
@pytest.mark.parametrize("share", [7, 3], ids=["shared for reading, writing and deleting", "shared for reading and writing"])
def test_a_save_while_another_program_has_the_file_open_is_not_lost(tmp_path, monkeypatch, held, share):
    from lumi.gui import settings as settings_module

    monkeypatch.setattr(settings_module, "_FILE_RETRY_SECONDS", 0.001)
    path = tmp_path / "settings.json"
    path.write_text(json.dumps(_SAVED), encoding="utf-8")
    settings = SettingsManager(path)
    held(path, share)
    settings.set("general", "theme", "dark")
    assert settings.save_error == ""
    assert json.loads(path.read_text(encoding="utf-8"))["general"]["theme"] == "dark"
    assert json.loads(settings.backup_path.read_text(encoding="utf-8"))["general"]["theme"] == "light"


@pytest.mark.skipif(sys.platform != "win32", reason="Windows' sharing rules for open files")
@pytest.mark.parametrize("share", [1, 0], ids=["shared for reading only", "not shared"])
def test_a_save_another_program_keeps_out_is_reported_until_one_lands(tmp_path, monkeypatch, held, share):
    from lumi.gui import settings as settings_module

    monkeypatch.setattr(settings_module, "_FILE_RETRY_SECONDS", 0.001)
    path = tmp_path / "settings.json"
    path.write_text(json.dumps(_SAVED), encoding="utf-8")
    settings = SettingsManager(path)
    before = path.read_bytes()
    holder = held(path, share)
    settings.set("general", "theme", "dark")
    assert "couldn't save its settings" in settings.get_masked()["_meta"]["save_error"]
    holder.stdin.close()
    holder.wait(timeout=10)
    assert path.read_bytes() == before
    settings.set("general", "display_name", "Alex Morgan")
    assert settings.save_error == ""
    saved = json.loads(path.read_text(encoding="utf-8"))["general"]
    assert (saved["theme"], saved["display_name"]) == ("dark", "Alex Morgan")


def test_an_empty_mode_means_a_new_installs_default():
    assert AppState.normalize_permission_mode("") == "auto-edit"
    assert AppState.normalize_permission_mode("  ") == "auto-edit"
    assert AppState.normalize_permission_mode("sideways") == "ask"  # unknown: fails closed


# ── A refused message ends the running state ───────────────────────────────


def test_a_refused_turn_says_so_and_names_a_queued_follow_up():
    assert ws_commands.refused_turn({"text": "hi"}, "No model is running.") == {
        "event": "error", "message": "No model is running.", "refused": True}
    assert ws_commands.refused_turn({"message_id": "m-2"}, "Busy", code="oversight_notice") == {
        "event": "error", "message": "Busy", "refused": True, "code": "oversight_notice", "message_id": "m-2"}


def test_a_message_without_a_running_model_is_refused_as_such():
    state = SimpleNamespace(session=None, runtime_unavailable_reason=lambda: "No model is running: choose one.")
    sent = _run(ws_commands.HANDLERS["message"], _ctx(state, {"command": "message", "text": "Fix the bug"}))
    assert sent == [{"event": "error", "message": "No model is running: choose one.", "refused": True}]


# ── Unattended work needs Full-auto ────────────────────────────────────────


def _app_state(mode: str) -> AppState:
    state = AppState.__new__(AppState)  # only the mode matters here
    state.permission_mode = mode
    return state


@pytest.mark.parametrize(("work", "grant"), [
    ("plan", "run this plan"), ("roadmap", "build this roadmap"), ("autonomous", "run this session"),
    ("autonomous_resume", "resume this session"), ("team", "run this team"), ("team_continue", "continue this team"),
])
def test_unattended_work_outside_full_auto_asks_to_run_just_that_in_full_auto(work, grant):
    state = _app_state("auto-edit")
    needed = state.full_auto_needed(work)
    assert needed["code"] == "needs_full_auto" and needed["can_grant"] is True and needed["work"] == work
    assert needed["message"].endswith(f"This conversation is in Auto-edit, and stays in Auto-edit if you {grant} in Full-auto.")
    assert "stays in Ask" in _app_state("ask").full_auto_needed(work)["message"]
    assert _app_state("bypass").full_auto_needed(work) is None
    # The person's grant for this one run: it goes ahead, and the conversation keeps its mode.
    assert state.full_auto_needed(work, granted=True) is None
    assert state.permission_mode == "auto-edit"


def test_only_a_grant_the_page_sends_as_true_counts():
    assert ws_commands.full_auto_granted({"full_auto": True}) is True
    for msg in ({}, {"full_auto": "true"}, {"full_auto": 1}, None, "full_auto"):
        assert ws_commands.full_auto_granted(msg) is False


def test_where_the_organization_doesnt_allow_full_auto_its_own_refusal_applies():
    _without_full_auto()
    # policy.full_auto_refusal (and the team's mode_refusal) say it in the organization's words,
    # grant or not: there is no grant to offer.
    assert _app_state("auto-edit").full_auto_needed("plan") is None
    assert _app_state("auto-edit").full_auto_needed("team", granted=True) is None
    assert "doesn't allow Full-auto" in lumi_policy.full_auto_refusal()


def test_each_full_auto_grant_is_recorded_in_the_audit_log(tmp_path):
    from lumi import audit
    from lumi.audit import AuditLog

    log = AuditLog(tmp_path / "audit")
    audit.set_for_tests(log)  # conftest puts the default back afterwards

    def grants():
        rows = [json.loads(line) for path in log._files() for line in path.read_text(encoding="utf-8").splitlines()]
        return [(row["session"], row["project"], row["data"]) for row in rows if row["type"] == "permission.full_auto_grant"]

    state = _app_state("auto-edit")
    state.project = SimpleNamespace(current_session=SimpleNamespace(id="s1"), project_path=str(tmp_path / "shop"))
    assert state.full_auto_needed("plan") is not None  # offering the grant records nothing
    assert grants() == []
    assert state.full_auto_needed("plan", granted=True) is None
    # A resumed session and a team are recorded under the conversation they belong to.
    assert state.full_auto_needed("autonomous_resume", granted=True, session_id="s2") is None
    team = {"action": "start", "autonomy": {"rounds": 2}, "session_id": "s3", "full_auto": True}
    assert swarming._full_auto_needed(state, _Manager(False), object(), team) is None
    shop = str(tmp_path / "shop")
    assert grants() == [
        ("s1", shop, {"work": "plan", "mode": "auto-edit"}),
        ("s2", shop, {"work": "autonomous_resume", "mode": "auto-edit"}),
        ("s3", shop, {"work": "team", "mode": "auto-edit"}),
    ]
    assert state.permission_mode == "auto-edit" and log.verify()[0] is True
    # No grant to record in Full-auto, or where the organization doesn't allow Full-auto.
    assert _app_state("bypass").full_auto_needed("plan", granted=True) is None
    _without_full_auto()
    assert state.full_auto_needed("plan", granted=True) is None
    assert len(grants()) == 3


@pytest.mark.parametrize("apply", [False, True], ids=["reads and reports", "applies changes"])
def test_a_policy_without_full_auto_refuses_a_team_the_orchestrator_runs(tmp_path, apply):
    # The rule AppState.full_auto_needed defers to: an orchestrated team needs
    # Full-auto under a policy too, not only one whose orchestrator applies changes.
    from lumi.engine.swarming import organization

    _without_full_auto()
    refusal = organization.mode_refusal(writers=apply, applies=apply, orchestrated=True)
    assert refusal.startswith("Acme's policy doesn't allow Full-auto")
    assert organization.mode_refusal(writers=False, applies=False) == ""  # a team the owner reviews
    setup = {"model": {"provider": "ollama", "model": "m"}, "plan_mode": "coordinator",
             "autonomy": {"rounds": 2, **({"apply": True} if apply else {})},
             **({"write_roots": ["src"]} if apply else {})}
    governance = organization.TeamGovernance.from_setup(None, setup, run_id="run-1", project=str(tmp_path),
                                                        session="s", personal=True)
    assert governance.orchestrated is True and "doesn't allow Full-auto" in governance.refusal()
    reviewed = organization.TeamGovernance.from_setup(None, {**setup, "autonomy": {}}, run_id="run-2",
                                                      project=str(tmp_path), session="s", personal=True)
    assert reviewed.orchestrated is False


class _Intents:
    def __init__(self):
        self.started = []

    def start_intent(self, text, **kwargs):
        self.started.append(text)
        return "intent-1"

    def route_to(self, *args):
        pass


def _refusing_state(**extra):
    needed = _app_state("auto-edit")
    return SimpleNamespace(full_auto_needed=needed.full_auto_needed, **extra)


def test_a_plan_outside_full_auto_is_refused_before_it_starts():
    intents = _Intents()
    state = _refusing_state(backend=object(), get_intent_service=lambda on_event=None: intents)
    [event] = _run(ws_commands.HANDLERS["intent_start"], _ctx(state, {"command": "intent_start", "text": "Add a counter"}))
    # The /plan card matches the prefix (app.js _failStartingPlan); detail is the explanation alone.
    assert event["event"] == "error" and event["message"].startswith("intent_start failed: A plan runs its steps")
    assert event["detail"].startswith("A plan runs its steps") and event["code"] == "needs_full_auto"
    assert event["can_grant"] is True and event["work"] == "plan" and intents.started == []


def test_a_plan_granted_full_auto_runs_and_the_conversation_keeps_its_mode():
    intents = _Intents()
    conversation = _app_state("auto-edit")
    state = SimpleNamespace(full_auto_needed=conversation.full_auto_needed, backend=object(),
                            get_intent_service=lambda on_event=None: intents)
    msg = {"command": "intent_start", "text": "Add a counter", "full_auto": True}
    sent = _run(ws_commands.HANDLERS["intent_start"], _ctx(state, msg))
    assert sent == [{"event": "intent.accepted", "intent_id": "intent-1", "text": "Add a counter"}]
    assert intents.started == ["Add a counter"]
    # Nothing switched the conversation: its next plan asks again.
    assert conversation.permission_mode == "auto-edit"
    [again] = _run(ws_commands.HANDLERS["intent_start"], _ctx(state, {"command": "intent_start", "text": "More"}))
    assert again["code"] == "needs_full_auto" and intents.started == ["Add a counter"]


def test_a_granted_plan_is_still_refused_where_the_policy_doesnt_allow_full_auto(tmp_path):
    # The engine's own refusal: IntentService.start_intent checks the policy.
    from lumi.orchestration.intent_service import IntentService

    _without_full_auto()
    service = IntentService(project_path=str(tmp_path), backend=object(), all_tools=[], settings=None)
    state = SimpleNamespace(full_auto_needed=_app_state("auto-edit").full_auto_needed, backend=object(),
                            get_intent_service=lambda on_event=None: service)
    msg = {"command": "intent_start", "text": "Add a counter", "full_auto": True}
    [event] = _run(ws_commands.HANDLERS["intent_start"], _ctx(state, msg))
    assert event["event"] == "error" and "Acme's policy doesn't allow Full-auto" in event["message"]
    assert "code" not in event  # no grant to offer


def test_a_plan_in_full_auto_starts():
    intents = _Intents()
    state = SimpleNamespace(full_auto_needed=_app_state("bypass").full_auto_needed, backend=object(),
                            get_intent_service=lambda on_event=None: intents)
    sent = _run(ws_commands.HANDLERS["intent_start"], _ctx(state, {"command": "intent_start", "text": "Add a counter"}))
    assert sent == [{"event": "intent.accepted", "intent_id": "intent-1", "text": "Add a counter"}]


def test_build_this_roadmap_outside_full_auto_leaves_the_mission_in_drafting():
    advanced = []
    mission = SimpleNamespace(id="s1", mission_state={"phase": "drafting"},
                              advance_mission_phase=lambda *args, **kwargs: advanced.append(args), save=lambda: None)
    intents = _Intents()
    state = _refusing_state(project=SimpleNamespace(project_path="/p", current_session=mission),
                            get_intent_service=lambda on_event=None: intents)
    [event] = _run(ws_commands.HANDLERS["mission_dispatch_roadmap"], _ctx(state, {"spec_markdown": "## Final spec"}))
    # source puts the Build button back; the page's notice offers to build this roadmap in Full-auto.
    assert event["source"] == "mission_dispatch" and event["code"] == "needs_full_auto"
    assert event["message"].startswith("A roadmap is built in Full-auto") and event["can_grant"] is True
    assert advanced == [] and intents.started == []


def test_build_this_roadmap_in_full_auto_dispatches_it_and_keeps_the_mode():
    advanced = []
    mission = SimpleNamespace(id="s1", mission_state={"phase": "drafting"},
                              advance_mission_phase=lambda *args, **kwargs: advanced.append(args), save=lambda: None)
    intents = _Intents()
    conversation = _app_state("auto-edit")
    state = SimpleNamespace(full_auto_needed=conversation.full_auto_needed,
                            project=SimpleNamespace(project_path="/p", current_session=mission,
                                                    list_sessions=lambda: [], list_all_sessions=lambda: []),
                            get_intent_service=lambda on_event=None: intents)
    sent = _run(ws_commands.HANDLERS["mission_dispatch_roadmap"],
                _ctx(state, {"spec_markdown": "## Final spec", "full_auto": True}))
    assert intents.started == ["## Final spec"] and advanced == [("planning_dispatched",)]
    assert sent[0]["event"] == "mission_phase_changed" and conversation.permission_mode == "auto-edit"


def test_an_autonomous_session_outside_full_auto_neither_starts_nor_resumes(monkeypatch):
    from lumi.gui import app as gui_app
    from tests.gui_access import LocalClient

    started = []
    monkeypatch.setattr(gui_app, "_start_autonomous_mission", lambda **kwargs: started.append(kwargs))
    monkeypatch.setattr(gui_app, "_resume_autonomous_mission", lambda **kwargs: started.append(kwargs))
    mission = SimpleNamespace(id="s1", title="Counter", mission_state={"phase": "drafting"})
    monkeypatch.setattr(gui_app.state, "permission_mode", "auto-edit")
    monkeypatch.setattr(gui_app.state.project, "current_session", mission)
    def first_error(websocket):
        # Connecting can bring other events first (status, oversight).
        for _ in range(20):
            event = websocket.receive_json()
            if event.get("event") == "error":
                return event
        raise AssertionError("no error event")

    with LocalClient(gui_app.app) as client:
        with client.websocket_connect("/ws") as websocket:
            websocket.send_json({"command": "mission_dispatch_autonomous", "spec_markdown": "## Final spec",
                                 "time_budget": "4h"})
            dispatch = first_error(websocket)
            # Refused before the switch to the interrupted session's conversation.
            websocket.send_json({"command": "autonomous_mission_resume", "intent_id": "auto-1", "session_id": "s-other"})
            resume = first_error(websocket)
    assert dispatch["source"] == "mission_dispatch" and dispatch["code"] == "needs_full_auto"
    assert dispatch["message"].startswith("An autonomous session runs in Full-auto")
    assert (resume["source"], resume["intent_id"], resume["session_id"]) == ("autonomous_resume", "auto-1", "s-other")
    assert resume["code"] == "needs_full_auto" and resume["can_grant"] is True
    assert started == [] and gui_app.state.project.current_session is mission


def test_an_autonomous_session_granted_full_auto_starts_and_resumes_in_its_mode(monkeypatch):
    from lumi.gui import app as gui_app
    from tests.gui_access import LocalClient

    started = []

    def start(kind):
        def reached(**kwargs):
            started.append((kind, gui_app.state.permission_mode))
            raise ValueError("stopped here by the test")  # the handler reports it; nothing runs
        return reached

    monkeypatch.setattr(gui_app, "_start_autonomous_mission", start("start"))
    monkeypatch.setattr(gui_app, "_resume_autonomous_mission", start("resume"))
    mission = SimpleNamespace(id="s1", title="Counter", mission_state={"phase": "drafting"})
    monkeypatch.setattr(gui_app.state, "permission_mode", "auto-edit")
    monkeypatch.setattr(gui_app.state.project, "current_session", mission)

    def first_error(websocket):
        for _ in range(20):
            event = websocket.receive_json()
            if event.get("event") == "error":
                return event
        raise AssertionError("no error event")

    with LocalClient(gui_app.app) as client:
        with client.websocket_connect("/ws") as websocket:
            websocket.send_json({"command": "mission_dispatch_autonomous", "spec_markdown": "## Final spec",
                                 "time_budget": "4h", "full_auto": True})
            dispatch = first_error(websocket)
            websocket.send_json({"command": "autonomous_mission_resume", "intent_id": "auto-1",
                                 "session_id": "s1", "full_auto": True})
            resume = first_error(websocket)
    assert dispatch["message"] == "Autonomous dispatch failed: stopped here by the test"
    assert resume["message"] == "Resume failed: stopped here by the test"
    # Both reached their start in the conversation's own mode: only that run was granted Full-auto.
    assert started == [("start", "auto-edit"), ("resume", "auto-edit")]
    assert gui_app.state.permission_mode == "auto-edit"


class _Manager:
    def __init__(self, orchestrated):
        self._orchestrated = orchestrated

    def orchestrated(self, capture, run_id):
        return self._orchestrated


@pytest.mark.parametrize(("message", "orchestrated", "work"), [
    ({"action": "start", "autonomy": {"rounds": 2}}, False, "team"),
    ({"action": "start", "autonomy": {"rounds": 2, "apply": True}}, False, "team"),
    ({"action": "continue_recovered", "run_id": "run-1"}, True, "team_continue"),
    ({"action": "start"}, False, None),
    ({"action": "continue_recovered", "run_id": "run-1"}, False, None),
    ({"action": "resume", "run_id": "run-1"}, True, None),
], ids=["orchestrated", "applies changes", "continue orchestrated", "reviewed by the owner",
        "continue reviewed", "resume a running team"])
def test_a_team_the_orchestrator_runs_needs_full_auto(message, orchestrated, work):
    state = _app_state("auto-edit")
    needed = swarming._full_auto_needed(state, _Manager(orchestrated), object(), message)
    assert (needed is not None) == bool(work)
    if work:
        assert needed["code"] == "needs_full_auto" and needed["can_grant"] is True
        # The owner's grant for this one team: no refusal, and the conversation's mode stays.
        granted = swarming._full_auto_needed(state, _Manager(orchestrated), object(), {**message, "full_auto": True})
        assert granted is None and state.permission_mode == "auto-edit"


class _Operated:
    """A SwarmRuntime stand-in that records what reaches the team engine."""

    busy = False

    def __init__(self):
        self.operated = []

    def orchestrated(self, capture, run_id):
        return False

    def operate(self, capture, message):
        self.operated.append(message)
        return {"run": None}


def test_the_team_engine_never_sees_the_grant_and_the_mode_stays(monkeypatch):
    runtime = _Operated()
    conversation = _app_state("auto-edit")
    state = SimpleNamespace(full_auto_needed=conversation.full_auto_needed, _swarm_desktop=runtime)
    monkeypatch.setattr(swarming, "_capture", lambda *args: object())
    replies = []

    async def send(reply):
        replies.append(reply)

    start = {"command": "swarm", "action": "start", "request_id": "r1", "project": "p", "session_id": "s",
             "objective": "Check the CSV export", "plan_mode": "coordinator", "autonomy": {"rounds": 2}}
    asyncio.run(swarming.command(state, send, dict(start)))
    assert replies[-1]["code"] == "needs_full_auto" and replies[-1]["can_grant"] is True and runtime.operated == []
    asyncio.run(swarming.command(state, send, {**start, "full_auto": True}))
    [operated] = runtime.operated
    assert "full_auto" not in operated and operated["autonomy"] == {"rounds": 2}
    assert conversation.permission_mode == "auto-edit"


def test_the_runtime_knows_which_retained_teams_the_orchestrator_runs(tmp_path):
    from lumi.engine.swarming import Scope
    from lumi.engine.swarming.service import CapturedSession, SwarmRuntime
    from lumi.gui.runtime import BackendSpec
    from tests.streaming_stub import StreamingBackend, done, text_delta
    from tests.test_gui_swarming import enable, start_request
    from tests.test_swarm_autopilot import start as start_orchestrated

    def factory(spec):
        backend = StreamingBackend(events=[text_delta("Inspected."), done()])
        backend.name, backend.model = spec.backend_type, spec.model
        return backend

    def runtime(name):
        # One team per project: each kind gets its own.
        workspace = tmp_path / name
        workspace.mkdir()
        service = SwarmRuntime(SettingsManager(tmp_path / f"{name}.json"), backend_factory=factory,
                               state_root=lambda _: tmp_path / f"{name}-state")
        capture = CapturedSession(Scope.personal("fixture-owner", name, "session"),
                                  str(workspace), BackendSpec("ollama", "chosen"), "")
        enable(service, capture)
        return service, capture

    reviewed_service, reviewed_capture = runtime("reviewed")
    orchestrated_service, orchestrated_capture = runtime("orchestrated")
    try:
        reviewed = reviewed_service.operate(reviewed_capture, start_request())["run"]["run"]["id"]
        orchestrated = start_orchestrated(orchestrated_service, orchestrated_capture, rounds=1)
        assert reviewed_service.orchestrated(reviewed_capture, reviewed) is False
        assert orchestrated_service.orchestrated(orchestrated_capture, orchestrated) is True
        assert orchestrated_service.orchestrated(orchestrated_capture, "no-such-run") is False
    finally:
        reviewed_service.close()
        orchestrated_service.close()


# ── Settings > Connections: Ollama ─────────────────────────────────────────


class _Ollama(BaseHTTPRequestHandler):
    models: list[str] = []
    status = 200

    def do_GET(self):  # noqa: N802 (http.server's name)
        body = json.dumps({"models": [{"name": name} for name in self.models]}).encode()
        self.send_response(self.status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


@pytest.fixture
def ollama():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Ollama)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server, f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


def test_the_ollama_address_is_checked():
    assert ws_commands.ollama_address("") == ""
    assert ws_commands.ollama_address(" 10.0.0.5:11434/ ") == "http://10.0.0.5:11434"
    assert ws_commands.ollama_address("https://ollama.example.com") == "https://ollama.example.com"
    for bad in ["ftp://host", "http://", "http://user:pw@host:11434", "http://host:99999"]:
        with pytest.raises(ValueError):
            ws_commands.ollama_address(bad)
    with pytest.raises(ValueError, match="Enter Ollama"):
        ws_commands._socket_setting_value("network", "ollama_url", "ftp://host")


def test_probing_ollama_reports_its_chat_models_or_why_not(ollama, monkeypatch):
    server, url = ollama
    monkeypatch.setattr(_Ollama, "models", ["qwen3-coder:30b", "nomic-embed-text"])
    assert ws_commands.probe_ollama(url) == {"status": "ready", "url": url, "models": ["qwen3-coder:30b"], "model_count": 1}
    monkeypatch.setattr(_Ollama, "models", ["nomic-embed-text"])
    empty = ws_commands.probe_ollama(url)
    assert empty["status"] == "empty" and "ollama pull" in empty["error"]
    monkeypatch.setattr(_Ollama, "status", 404)
    assert "HTTP 404" in ws_commands.probe_ollama(url)["error"]
    closed = ws_commands.probe_ollama("http://127.0.0.1:9")
    assert closed["status"] == "unreachable" and "Nothing answered as Ollama" in closed["error"]


class _OllamaState:
    """The AppState parts Settings > Connections' Ollama card uses."""

    def __init__(self, tmp_path, url):
        self.settings = SettingsManager(tmp_path / "settings.json")
        self.ollama_url = url
        self.backend = None
        self.detected = 0
        self.started = 0

    def update_setting_value(self, section, key, value):
        self.settings.set(section, key, value)
        self.ollama_url = value or self.ollama_url
        self.detect_backends(force=True)  # as the app's apply_settings does after every change
        return {"network": {"ollama_url": value}}

    def detect_backends(self, force=False):
        self.detected += 1

    def ensure_default_runtime_session(self):
        self.started += 1
        self.backend = object()

    def get_init_data(self, refresh_only=False):
        return {"event": "init", "refresh_only": refresh_only}


def _connection(state, action, url=""):
    msg = {"command": "provider_connection", "provider": "ollama", "action": action, "url": url}
    return _run(ws_commands.HANDLERS["provider_connection"], _ctx(state, msg))


def test_testing_an_ollama_address_saves_nothing(tmp_path, ollama, monkeypatch):
    monkeypatch.delenv("OLLAMA_HOST", raising=False)
    server, url = ollama
    monkeypatch.setattr(_Ollama, "models", ["qwen3-coder:30b"])
    state = _OllamaState(tmp_path, "http://127.0.0.1:9")
    [event] = _connection(state, "test", url)
    assert event["data"] == {"status": "ready", "url": url, "models": ["qwen3-coder:30b"], "model_count": 1,
                             "saved": False, "action": "test",
                             "address": {"in_use": "http://127.0.0.1:9", "saved": "", "environment": ""}}
    assert state.settings.get("network", "ollama_url") == "" and state.detected == 0 and state.started == 0


def test_testing_an_empty_address_checks_this_computer(tmp_path, monkeypatch):
    # "Leave it empty for this computer": what saving it would mean, whatever OLLAMA_HOST says.
    monkeypatch.setenv("OLLAMA_HOST", "http://10.9.9.9:11434")
    probed = []
    monkeypatch.setattr(ws_commands, "probe_ollama", lambda url: probed.append(url) or {"status": "unreachable", "url": url})
    state = _OllamaState(tmp_path, "http://10.9.9.9:11434")
    _connection(state, "test", "")
    assert probed == ["http://127.0.0.1:11434"]


def test_saving_an_ollama_address_stores_it_and_starts_a_model(tmp_path, ollama, monkeypatch):
    monkeypatch.delenv("OLLAMA_HOST", raising=False)
    server, url = ollama
    monkeypatch.setattr(_Ollama, "models", ["qwen3-coder:30b"])
    state = _OllamaState(tmp_path, "http://127.0.0.1:9")
    settings_event, connection, init = _connection(state, "save", url + "/")
    assert settings_event == {"event": "settings", "data": {"network": {"ollama_url": url}}}
    assert connection["data"]["status"] == "ready" and connection["data"]["saved"] is True
    assert connection["data"]["action"] == "save"
    assert connection["data"]["address"] == {"in_use": url, "saved": url, "environment": ""}
    assert init == {"event": "init", "refresh_only": True}
    assert state.settings.get("network", "ollama_url") == url
    # Providers are probed once, by the save itself: a second probe made Save take seconds.
    assert state.detected == 1 and state.started == 1


def test_the_ollama_cards_status_check_probes_the_providers_again(tmp_path, ollama, monkeypatch):
    monkeypatch.delenv("OLLAMA_HOST", raising=False)
    server, url = ollama
    monkeypatch.setattr(_Ollama, "models", ["qwen3-coder:30b"])
    state = _OllamaState(tmp_path, url)
    [connection, *_] = _connection(state, "status")
    assert connection["data"]["status"] == "ready" and connection["data"]["action"] == "status"
    assert state.detected == 1 and state.settings.get("network", "ollama_url") == ""


class _ResolvingOllamaState(_OllamaState):
    """Resolves the address as the app does (network_defaults.resolve_ollama_url): OLLAMA_HOST first."""

    def update_setting_value(self, section, key, value):
        from lumi.network_defaults import resolve_ollama_url

        self.settings.set(section, key, value)
        self.ollama_url = resolve_ollama_url(settings_data=self.settings.get_all())
        self.detect_backends(force=True)
        return {"network": {"ollama_url": value}}


def test_ollama_host_takes_the_saved_address_place_and_the_card_is_told(tmp_path, ollama, monkeypatch):
    server, url = ollama
    monkeypatch.setattr(_Ollama, "models", ["qwen3-coder:30b"])
    monkeypatch.setenv("OLLAMA_HOST", "http://127.0.0.1:9")
    state = _ResolvingOllamaState(tmp_path, "http://127.0.0.1:9")
    settings_event, connection, init = _connection(state, "save", url)
    # Saved as asked, but the app goes on using OLLAMA_HOST's address, and says so.
    assert state.settings.get("network", "ollama_url") == url
    assert connection["data"]["address"] == {"in_use": "http://127.0.0.1:9", "saved": url, "environment": "OLLAMA_HOST"}
    # The check was of the address in use, never reported as the one saved.
    assert connection["data"]["url"] == "http://127.0.0.1:9" and connection["data"]["status"] == "unreachable"
    assert state.started == 0
    monkeypatch.delenv("OLLAMA_HOST")
    assert ws_commands.ollama_address_in_use(state, lambda: "unused")["environment"] == ""


def test_a_bad_ollama_address_is_refused_and_not_saved(tmp_path):
    state = _OllamaState(tmp_path, "http://127.0.0.1:9")
    [event] = _connection(state, "save", "ftp://nowhere")
    assert "Enter Ollama" in event["data"]["error"]
    assert state.settings.get("network", "ollama_url") == ""


def test_a_policy_that_locks_ollamas_address_keeps_it(tmp_path):
    lumi_policy.set_for_tests(lumi_policy.parse(
        {"schema": "lumi.policy/v1", "organization": "Acme", "settings": {"network.ollama_url": "http://gpu:11434"}},
        source="test"))
    state = _OllamaState(tmp_path, "http://gpu:11434")
    [event] = _connection(state, "save", "http://127.0.0.1:11434")
    assert "managed by Acme" in event["data"]["error"]


# ── ChatGPT sign-in without the Codex CLI ──────────────────────────────────


def test_chatgpt_sign_in_without_the_codex_cli_says_what_to_install(monkeypatch):
    from lumi import codex_account

    monkeypatch.setattr(codex_account, "resolve_codex_cli_path", lambda: "")
    monkeypatch.setattr(codex_account, "find_program", lambda name, **kwargs: None)
    account = codex_account.CodexAccount()
    with pytest.raises(codex_account.CodexCliMissing, match="needs Node.js") as missing:
        account.login()
    assert "npm install -g @openai/codex" in str(missing.value)
    monkeypatch.setattr(codex_account, "find_program", lambda name, **kwargs: f"C:/tools/{name}.exe")
    assert "needs Node.js" not in codex_account.missing_cli_message()

    monkeypatch.setattr(codex_account, "codex_account", account)
    state = SimpleNamespace(codex_connection=None, available_backends={})
    msg = {"command": "provider_connection", "provider": "codex", "action": "login"}
    [event] = _run(ws_commands.HANDLERS["provider_connection"],
                   ws_commands.CommandContext(ws=_StubWS(), state=state, msg=msg, runs=SimpleNamespace(busy=False)))
    assert event["data"]["missing_cli"] is True and "Codex CLI" in event["data"]["error"]


# ── Diagnostics ────────────────────────────────────────────────────────────


def _windows_folder() -> str:
    """The Windows folder, asked of Windows itself (as executables.windows_folder does)."""
    import ctypes

    buffer = ctypes.create_unicode_buffer(32768)
    assert ctypes.windll.kernel32.GetSystemWindowsDirectoryW(buffer, len(buffer))
    return buffer.value


def test_show_in_folder_reveals_only_the_saved_diagnostics(tmp_path, monkeypatch):
    opened = []
    monkeypatch.setattr(ws_commands.subprocess, "Popen", lambda args, **kwargs: opened.append((args, kwargs)))
    state = SimpleNamespace()
    [event] = _run(ws_commands.HANDLERS["reveal_diagnostics"], _ctx(state, {"path": str(tmp_path / "other.zip")}))
    assert "isn't there anymore" in event["message"] and opened == []

    saved = tmp_path / "lumi-diagnostics-1.zip"
    saved.write_bytes(b"PK")
    state._last_diagnostics_zip = str(saved)
    # The page names no path; one it sends anyway is ignored.
    [event] = _run(ws_commands.HANDLERS["reveal_diagnostics"], _ctx(state, {"path": "C:/Windows/System32"}))
    assert event["message"] == "Showing lumi-diagnostics-1.zip in its folder"
    [(args, kwargs)] = opened
    # A list of arguments, never a command line for a shell to read.
    assert isinstance(args, list) and args[-1] in {str(saved), str(tmp_path)}
    assert kwargs.get("shell") is not True
    if sys.platform == "win32":
        assert args[1:] == ["/select,", str(saved)]
        # The file manager's window shows: started with SW_HIDE (the hidden
        # start-up console tools get), Explorer opens the folder hidden.
        assert "startupinfo" not in kwargs
    elif sys.platform == "darwin":
        assert args == ["/usr/bin/open", "-R", str(saved)]


def test_show_in_folder_starts_the_file_manager_by_its_absolute_path(tmp_path, monkeypatch):
    # The app's working folder is the open project, and Windows looks for a
    # bare "explorer" there first: a repository's own explorer.exe must never run.
    project = tmp_path / "cloned-repo"
    project.mkdir()
    (project / "explorer.exe").write_bytes(b"MZ not the real one")
    (project / "explorer.bat").write_text("@echo off\r\necho hijacked\r\n", encoding="ascii")
    monkeypatch.chdir(project)
    saved = tmp_path / "lumi-diagnostics-1.zip"
    saved.write_bytes(b"PK")
    started = []
    monkeypatch.setattr(ws_commands.subprocess, "Popen", lambda args, **kwargs: started.append(args))
    state = SimpleNamespace(_last_diagnostics_zip=str(saved))
    _run(ws_commands.HANDLERS["reveal_diagnostics"], _ctx(state, {}))
    [args] = started
    program = args[0]
    assert os.path.isabs(program), program
    assert not os.path.normcase(program).startswith(os.path.normcase(str(project)))
    if sys.platform == "win32":
        # The Windows folder Windows reports, not the environment or PATH.
        assert os.path.normcase(program) == os.path.normcase(os.path.join(_windows_folder(), "explorer.exe"))
        assert os.path.isfile(program)
    # executables.show_in_folder (#110), not a command of the page's own.
    from lumi import executables

    assert executables.show_in_folder_command(str(saved))[0] == program
    with pytest.raises(ValueError):
        executables.show_in_folder_command("relative/lumi-diagnostics-1.zip")
