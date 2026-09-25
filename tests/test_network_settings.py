"""Settings > Network: proxy, bypass list and the system certificate store."""

from __future__ import annotations

import os

import pytest

from lumi import net


class _Settings:
    def __init__(self, **network):
        self.network = network

    def get(self, section, key=None, default=None):
        if section != "network":
            return default
        return self.network.get(key, default)


@pytest.fixture
def clean_network(monkeypatch):
    """Fresh module state and proxy variables; truststore calls are recorded, not made."""
    for name in ("HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY"):
        # setenv first so undo also removes what configure() exports; delenv
        # alone records nothing for a variable that was never set.
        monkeypatch.setenv(name, "")
        monkeypatch.delenv(name)
    monkeypatch.setattr(net, "_state", {"trust_injected": False, "saved_env": None})
    calls = []
    truststore = pytest.importorskip("truststore")
    monkeypatch.setattr(truststore, "inject_into_ssl", lambda: calls.append("inject"))
    monkeypatch.setattr(truststore, "extract_from_ssl", lambda: calls.append("extract"))
    return calls


class TestProxyValidation:
    @pytest.mark.parametrize("value, expected", [
        ("", ""),
        ("  http://proxy.corp:8080/ ", "http://proxy.corp:8080"),
        ("https://proxy.corp", "https://proxy.corp"),
        ("socks5://127.0.0.1:1080", "socks5://127.0.0.1:1080"),
    ])
    def test_accepted(self, value, expected):
        assert net.validate_proxy_url(value) == expected

    @pytest.mark.parametrize("value", ["proxy.corp:8080", "ftp://proxy.corp", "http://"])
    def test_rejected(self, value):
        with pytest.raises(ValueError, match="http://host:port"):
            net.validate_proxy_url(value)

    def test_credentials_are_refused(self):
        with pytest.raises(ValueError, match="sign-in"):
            net.validate_proxy_url("http://user:secret@proxy.corp:8080")


class TestConfigure:
    def test_proxy_and_bypass_are_exported(self, clean_network):
        state = net.configure(_Settings(proxy_url="http://proxy.corp:8080", no_proxy="git.corp, .internal"))
        assert state == {"system_certificates": True, "proxy": True}
        assert os.environ["HTTPS_PROXY"] == os.environ["HTTP_PROXY"] == "http://proxy.corp:8080"
        bypass = os.environ["NO_PROXY"].split(",")
        assert bypass[:2] == ["git.corp", ".internal"]
        assert {"localhost", "127.0.0.1", "::1"} <= set(bypass)

    def test_clearing_restores_the_users_own_variables(self, clean_network, monkeypatch):
        monkeypatch.setenv("HTTPS_PROXY", "http://from-shell:3128")
        monkeypatch.setenv("NO_PROXY", "shell.host")
        net.configure(_Settings(proxy_url="http://proxy.corp:8080"))
        assert os.environ["HTTPS_PROXY"] == "http://proxy.corp:8080"
        net.configure(_Settings(proxy_url=""))
        assert os.environ["HTTPS_PROXY"] == "http://from-shell:3128"
        assert "HTTP_PROXY" not in os.environ
        assert os.environ["NO_PROXY"].split(",")[0] == "shell.host"

    def test_invalid_saved_proxy_is_ignored(self, clean_network):
        state = net.configure(_Settings(proxy_url="http://user:pw@proxy.corp:8080"))
        assert state["proxy"] is False
        assert "HTTPS_PROXY" not in os.environ

    def test_system_certificates_toggle(self, clean_network):
        assert net.configure(_Settings())["system_certificates"] is True
        assert net.configure(_Settings(system_certificates=True))["system_certificates"] is True
        assert net.configure(_Settings(system_certificates=False))["system_certificates"] is False
        assert clean_network == ["inject", "extract"]


class TestSocketValidation:
    def test_network_fields(self):
        from lumi.gui.ws_commands import _socket_setting_value

        assert _socket_setting_value("network", "proxy_url", "http://proxy.corp:8080/") == "http://proxy.corp:8080"
        assert _socket_setting_value("network", "no_proxy", " a.corp,,b.corp\n") == "a.corp, b.corp"
        assert _socket_setting_value("network", "system_certificates", False) is False
        assert _socket_setting_value("privacy", "secret_scan", True) is True
        with pytest.raises(ValueError, match="sign-in"):
            _socket_setting_value("network", "proxy_url", "http://u:p@proxy.corp")
        with pytest.raises(ValueError, match="commas"):
            _socket_setting_value("network", "no_proxy", "a.corp b.corp")
        with pytest.raises(ValueError, match="on or off"):
            _socket_setting_value("privacy", "secret_scan", "yes")
