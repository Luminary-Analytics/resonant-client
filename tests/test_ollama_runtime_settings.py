"""Settings › Ollama runtime: the context window and keep-alive every Ollama request carries.

They were saved and never read, keep-alive came only from
LUMI_OLLAMA_KEEP_ALIVE, and a second "Ollama host" field did nothing. Now
each Ollama backend takes them when it's made (backends.ollama_runtime), the
Settings value before the environment, and Ollama's address has one setting:
network.ollama_url, the Connections card's.
"""

from __future__ import annotations

from unittest.mock import patch

import httpx
import pytest

from lumi import backends
from lumi.backends import OllamaBackend, configure_ollama_runtime, ollama_keep_alive_setting, ollama_num_ctx_setting
from lumi.gui import ws_commands
from lumi.gui.settings import SettingsManager

MODEL = "qwen3-coder:30b"


@pytest.fixture
def settings(tmp_path, monkeypatch):
    monkeypatch.delenv("LUMI_OLLAMA_NUM_CTX", raising=False)
    monkeypatch.delenv("LUMI_OLLAMA_KEEP_ALIVE", raising=False)
    manager = SettingsManager(tmp_path / "settings.json")
    configure_ollama_runtime(manager)
    yield manager
    configure_ollama_runtime(None)


@pytest.mark.parametrize("value, kept", [(None, None), ("", None), (0, None), (32768, 32768), ("65536", 65536),
                                         (131072.0, 131072), (4096, 4096), (1_048_576, 1_048_576)])
def test_a_context_window_settings_keeps(value, kept):
    assert ollama_num_ctx_setting(value) == kept


@pytest.mark.parametrize("value", [2048, 2_000_000, 8192.5, "lots", True, -1, [8192]])
def test_a_context_window_settings_refuses(value):
    with pytest.raises(ValueError, match="4,096 to 1,048,576 tokens"):
        ollama_num_ctx_setting(value)


@pytest.mark.parametrize("value, kept", [(None, ""), ("", ""), (" 30m ", "30m"), ("2h", "2h"), ("1h30m", "1h30m"),
                                         ("0", "0"), ("-1m", "-1m"), ("300", "300s"), ("-1", "-1s"), ("1.5h", "1.5h")])
def test_a_keep_alive_is_a_duration_ollama_takes(value, kept):
    assert ollama_keep_alive_setting(value) == kept


@pytest.mark.parametrize("value", ["forever", "10 minutes", "5d", "m", "1h 30m", "x" * 40])
def test_a_keep_alive_ollama_wouldnt_take_is_refused(value):
    with pytest.raises(ValueError, match="30m or 2h"):
        ollama_keep_alive_setting(value)


def _sent(backend: OllamaBackend, monkeypatch) -> dict:
    """The /api/chat request ``backend`` makes, caught before it leaves."""
    monkeypatch.setitem(OllamaBackend._vision_support_cache, MODEL, False)
    monkeypatch.setattr(OllamaBackend, "_circuit", {})
    backend._use_native_tools = True
    captured: dict = {}

    def stream(method, url, json=None, **kwargs):
        captured.update(json or {})
        raise httpx.ConnectError("caught before it left")

    with patch("httpx.Client") as client_class:
        client_class.return_value.stream = stream
        list(backend.stream("Hi", [], "Be helpful", tools=[]))
    return captured


def test_each_backend_asks_ollama_for_what_settings_say(settings, monkeypatch):
    default = OllamaBackend("http://127.0.0.1:11434", MODEL)
    assert default._ollama_keep_alive == "120m"
    settings.set("local_backends", "ollama_num_ctx", 16384)
    settings.set("local_backends", "ollama_keep_alive", "30m")
    tuned = OllamaBackend("http://127.0.0.1:11434", MODEL)
    assert tuned.effective_context_tokens == 16384 and tuned._ollama_keep_alive == "30m"
    # The request itself carries them.
    request = _sent(tuned, monkeypatch)
    assert request["options"]["num_ctx"] == 16384 and request["keep_alive"] == "30m"
    # Settings come before the environment (and the Large-context profile, which sets it).
    monkeypatch.setenv("LUMI_OLLAMA_NUM_CTX", "131072")
    monkeypatch.setenv("LUMI_OLLAMA_KEEP_ALIVE", "5m")
    assert OllamaBackend("http://127.0.0.1:11434", MODEL).effective_context_tokens == 16384
    settings.set("local_backends", "ollama_num_ctx", None)
    settings.set("local_backends", "ollama_keep_alive", "")
    from_environment = OllamaBackend("http://127.0.0.1:11434", MODEL)
    assert from_environment.effective_context_tokens == 131072 and from_environment._ollama_keep_alive == "5m"
    # Never more than the model reports it takes.
    tuned._apply_reported_context_length({"llama.context_length": 8192})
    assert tuned.effective_context_tokens == 8192


def test_a_value_edited_into_settings_json_that_ollama_wouldnt_take_is_ignored(settings, caplog):
    settings.set("local_backends", "ollama_num_ctx", "a lot")
    settings.set("local_backends", "ollama_keep_alive", "forever")
    backend = OllamaBackend("http://127.0.0.1:11434", MODEL)
    assert backend._ollama_keep_alive == "120m"
    assert backend.effective_context_tokens == max(4096, backends.default_context_window(MODEL))
    assert "ollama_num_ctx" in caplog.text and "ollama_keep_alive" in caplog.text


def test_settings_saves_them_checked_and_ollamas_address_has_one_setting():
    assert ws_commands._socket_setting_value("local_backends", "ollama_num_ctx", 32768) == 32768
    assert ws_commands._socket_setting_value("local_backends", "ollama_num_ctx", None) is None
    assert ws_commands._socket_setting_value("local_backends", "ollama_keep_alive", "45m") == "45m"
    with pytest.raises(ValueError, match="4,096"):
        ws_commands._socket_setting_value("local_backends", "ollama_num_ctx", 1024)
    with pytest.raises(ValueError, match="30m or 2h"):
        ws_commands._socket_setting_value("local_backends", "ollama_keep_alive", "a while")
    # The old "Ollama host" field wrote a setting nothing read: the address is network.ollama_url alone.
    with pytest.raises(ValueError, match="can't be changed from the app"):
        ws_commands._socket_setting_value("local_backends", "ollama_host", "http://10.0.0.5:11434")
    assert ws_commands._socket_setting_value("network", "ollama_url", "10.0.0.5:11434") == "http://10.0.0.5:11434"
