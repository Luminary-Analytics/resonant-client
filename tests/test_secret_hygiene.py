"""Keys in the OS credential store, and keys kept out of child processes.

The credential store is replaced by an in-memory keyring here; nothing in
these tests touches Windows Credential Manager or a real Keychain.
"""

from __future__ import annotations

import json
import sys

import keyring
import keyring.core
import pytest
from keyring.backend import KeyringBackend
from keyring.errors import PasswordDeleteError

from lumi import secrets_store
from lumi.gui.settings import SettingsManager
from lumi.secrets_store import PLACEHOLDER, PROVIDER_KEY_ENV, SecretStore, child_env


class MemoryKeyring(KeyringBackend):
    priority = 1

    def __init__(self):
        super().__init__()
        self.store: dict[tuple[str, str], str] = {}

    def get_password(self, service, username):
        return self.store.get((service, username))

    def set_password(self, service, username, password):
        self.store[(service, username)] = password

    def delete_password(self, service, username):
        if (service, username) not in self.store:
            raise PasswordDeleteError(username)
        del self.store[(service, username)]


@pytest.fixture
def memory_keyring(monkeypatch):
    backend = MemoryKeyring()
    # tests/conftest.py turns the store off for every test; these opt back in
    # with a backend that lives only in this process.
    monkeypatch.setattr(keyring.core, "_keyring_backend", backend)
    monkeypatch.setenv("LUMI_KEYCHAIN", "on")
    monkeypatch.setenv("LUMI_KEYCHAIN_SERVICE", "Lumi-test")
    return backend


def _settings_file(tmp_path, folder=".lumi", data=None):
    path = tmp_path / folder / "settings.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    if data is not None:
        path.write_text(json.dumps(data), encoding="utf-8")
    return path


class TestSecretStore:
    def test_turned_off_by_environment(self, monkeypatch):
        monkeypatch.setenv("LUMI_KEYCHAIN", "off")
        store = SecretStore()
        assert store.available is False
        assert "LUMI_KEYCHAIN=off" in store.reason
        assert store.get("openai") == ""
        assert store.set("openai", "value") is False

    def test_unusable_backend_is_reported(self, monkeypatch):
        from keyring.backends import fail

        monkeypatch.setattr(keyring.core, "_keyring_backend", fail.Keyring())
        monkeypatch.setenv("LUMI_KEYCHAIN", "on")
        store = SecretStore()
        assert store.available is False
        assert "No credential store" in store.reason

    def test_round_trip(self, memory_keyring):
        store = SecretStore()
        assert store.available and store.service == "Lumi-test"
        assert store.set("openai", "sk-live-value") is True
        assert store.get("openai") == "sk-live-value"
        store.delete("openai")
        store.delete("openai")  # already gone: no error
        assert store.get("openai") == ""


class TestSettingsKeychain:
    def test_plaintext_keys_move_into_the_store(self, tmp_path, memory_keyring):
        path = _settings_file(tmp_path, data={"api_keys": {"openai": "sk-plain-openai-key"}})
        settings = SettingsManager(path)

        on_disk = json.loads(path.read_text(encoding="utf-8"))
        assert on_disk["api_keys"]["openai"] == PLACEHOLDER
        assert memory_keyring.store[("Lumi-test", "openai")] == "sk-plain-openai-key"
        assert settings.get("api_keys", "openai") == "sk-plain-openai-key"
        assert "sk-plain-openai-key" not in path.read_text(encoding="utf-8")

    def test_saving_and_clearing_a_key(self, tmp_path, memory_keyring):
        path = _settings_file(tmp_path)
        settings = SettingsManager(path)
        settings.set("api_keys", "anthropic", "sk-ant-saved-key")
        settings.update_section("api_keys", {"conn_work": "work-key-value"})

        assert json.loads(path.read_text(encoding="utf-8"))["api_keys"]["anthropic"] == PLACEHOLDER
        assert settings.get("api_keys", "conn_work") == "work-key-value"
        masked = settings.get_masked()
        assert masked["api_keys"]["anthropic"] == ""
        assert masked["_meta"]["api_keys_present"]["anthropic"] is True
        assert masked["_meta"]["secret_storage"]["keychain"] is True
        assert masked["_meta"]["secret_storage"]["store"]

        settings.set("api_keys", "anthropic", "")
        assert ("Lumi-test", "anthropic") not in memory_keyring.store
        assert settings.get("api_keys", "anthropic", "") == ""

    def test_a_new_manager_reads_keys_back(self, tmp_path, memory_keyring):
        path = _settings_file(tmp_path)
        SettingsManager(path).set("api_keys", "kimi", "moonshot-key-value")
        assert SettingsManager(path).get("api_keys", "kimi") == "moonshot-key-value"

    def test_legacy_folder_keeps_plaintext(self, tmp_path, memory_keyring):
        # An older SONN Client may share ~/.resonant and would read the
        # placeholder as its key.
        path = _settings_file(tmp_path, ".resonant", {"api_keys": {"sonn": "sonn-plain-key"}})
        settings = SettingsManager(path)
        settings.set("api_keys", "kimi", "kimi-plain-key")

        keys = json.loads(path.read_text(encoding="utf-8"))["api_keys"]
        assert (keys["sonn"], keys["kimi"]) == ("sonn-plain-key", "kimi-plain-key")
        assert memory_keyring.store == {}
        storage = settings.get_masked()["_meta"]["secret_storage"]
        assert storage["keychain"] is False
        assert ".resonant" in storage["reason"]

    def test_store_off_keeps_plaintext(self, tmp_path):
        path = _settings_file(tmp_path)
        settings = SettingsManager(path)
        settings.set("api_keys", "openai", "sk-file-key")
        assert json.loads(path.read_text(encoding="utf-8"))["api_keys"]["openai"] == "sk-file-key"
        assert settings.get_masked()["_meta"]["secret_storage"]["keychain"] is False


class TestChildEnvironment:
    def test_provider_keys_are_removed(self):
        base = {name: "secret" for name in PROVIDER_KEY_ENV}
        base.update(PATH="/bin", GITHUB_TOKEN="kept")
        env = child_env(base)
        assert env == {"PATH": "/bin", "GITHUB_TOKEN": "kept"}

    def test_names_match_without_case(self):
        assert child_env({"openai_api_key": "x", "Home": "/h"}) == {"Home": "/h"}

    def test_agent_shell_does_not_see_keys(self, monkeypatch, tmp_path):
        from lumi.engine.tools import _run_subprocess_with_cancel

        monkeypatch.setenv("OPENAI_API_KEY", "sk-parent-process-key")
        monkeypatch.setenv("LUMI_HYGIENE_MARKER", "still-here")
        code, stdout, _, timed_out = _run_subprocess_with_cancel(
            [sys.executable, "-c",
             "import os; print(os.environ.get('OPENAI_API_KEY', '<none>'), os.environ.get('LUMI_HYGIENE_MARKER'))"],
            timeout=60, shell=False, text=True, cwd=str(tmp_path),
        )
        assert (code, timed_out) == (0, False)
        assert stdout.split() == ["<none>", "still-here"]

    def test_mcp_server_keeps_only_its_own_keys(self, monkeypatch):
        from lumi.engine import mcp

        captured = {}

        def fake_popen(argv, **kwargs):
            captured.update(kwargs["env"])
            raise OSError("not started in tests")

        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-parent")
        monkeypatch.setenv("OPENAI_API_KEY", "sk-parent")
        monkeypatch.setattr(mcp.subprocess, "Popen", fake_popen)
        config = mcp.MCPServerConfig(
            name="docs", command="docs-server",
            env={"OPENAI_API_KEY": "sk-configured-for-server", "DOCS_TOKEN": "t"},
        )
        assert mcp.MCPConnection(config).connect() is False
        assert "ANTHROPIC_API_KEY" not in captured
        assert captured["OPENAI_API_KEY"] == "sk-configured-for-server"
        assert captured["DOCS_TOKEN"] == "t"

    def test_hooks_run_without_keys(self, monkeypatch, tmp_path):
        from lumi.engine.hooks import HookDefinition, HookRunner, HookType

        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-parent")
        out = tmp_path / "hook-env.txt"
        script = tmp_path / "hook.py"
        script.write_text(
            "import os, pathlib\n"
            f"pathlib.Path({str(out)!r}).write_text(os.environ.get('ANTHROPIC_API_KEY', '<none>'))\n",
            encoding="utf-8",
        )
        runner = HookRunner().scoped([HookDefinition(
            hook_type=HookType.PRE_TOOL_USE,
            command=f'"{sys.executable}" "{script}"',
            timeout_seconds=60,
        )])
        result = runner.run_hooks(HookType.PRE_TOOL_USE, {"project_path": str(tmp_path)}, tool_name="bash")
        assert result.allowed, result.error
        assert out.read_text(encoding="utf-8") == "<none>"


def test_credential_store_name_is_platform_specific(monkeypatch):
    monkeypatch.setattr(secrets_store.sys, "platform", "win32")
    assert secrets_store.credential_store_name() == "Windows Credential Manager"
    monkeypatch.setattr(secrets_store.sys, "platform", "darwin")
    assert "Keychain" in secrets_store.credential_store_name()
