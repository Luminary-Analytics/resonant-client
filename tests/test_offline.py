"""Offline mode (lumi/offline.py): the central outbound check and where it applies.

Mock transports and fakes only: nothing here reaches another computer.
"""

from __future__ import annotations

import json
import socket

import httpx
import pytest

from lumi import net, offline
from lumi.offline import OfflineBlocked, OfflineConfig


def _on(*hosts: str, **fields) -> OfflineConfig:
    return offline.set_for_tests(enabled=True, allowed_hosts=hosts, **fields)


def _ok(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={"ok": True})


class _Settings:
    """settings.get with optional policy locks, like SettingsManager."""

    def __init__(self, values: dict | None = None, locked: dict | None = None):
        self.values = values or {}
        self.locked = locked or {}

    def get(self, section, key=None, default=None):
        locked = self.locked.get(section, {})
        if key is not None and key in locked:
            return locked[key]
        section_values = self.values.get(section, {})
        return section_values if key is None else section_values.get(key, default)

    def locked_values(self, section):
        return dict(self.locked.get(section, {}))


# ── The central check ───────────────────────────────────────────────────────


class TestLocalHosts:
    @pytest.mark.parametrize("host", [
        "localhost", "LOCALHOST", "127.0.0.1", "127.0.0.2", "127.255.255.254", "::1", "[::1]",
        "0:0:0:0:0:0:0:1", "::ffff:127.0.0.1", "0.0.0.0", "::",
    ])
    def test_this_computer(self, host):
        assert offline.is_local_host(host)

    @pytest.mark.parametrize("host", [
        # Names that resolve to loopback somewhere, or look local, are still other computers.
        "localhost.evil.com", "localhost.", "127.0.0.1.nip.io", "evil.com", "localtest.me",
        "127.1", "2130706433", "0x7f000001", "0177.0.0.1", "::ffff:8.8.8.8", "fe80::1", "10.0.0.131",
        "",
    ])
    def test_elsewhere(self, host):
        assert not offline.is_local_host(host)

    def test_this_computers_own_name(self):
        assert offline.is_local_host(socket.gethostname())


class TestHostAllowed:
    def test_everything_while_off(self):
        offline.reset_for_tests()
        assert offline.host_allowed("api.openai.com")

    def test_local_blocked_and_allowed(self):
        _on("llm.corp.example", "*.models.example", "10.20.0.0/16", "192.168.1.7", "fd00::5")
        for host in ("localhost", "127.0.0.1", "::1", "llm.corp.example", "LLM.corp.example",
                     "llm.corp.example.", "a.models.example", "b.c.models.example", "10.20.3.4",
                     "192.168.1.7", "fd00::5", "[fd00::5]", "::ffff:10.20.0.9"):
            assert offline.host_allowed(host), host
        for host in ("api.openai.com", "corp.example", "x.llm.corp.example", "models.example",
                     "evilmodels.example", "models.example.evil.com", "10.21.0.1", "192.168.1.8",
                     "localhost.evil.com", "llm.corp.example.evil.com", "fd00::6"):
            assert not offline.host_allowed(host), host

    def test_idna_names_compare_encoded(self):
        _on("bücher.example")
        assert offline.host_allowed("xn--bcher-kva.example")
        assert offline.host_allowed("BÜCHER.example")


class TestAllowedHostsParsing:
    def test_normalizes(self):
        parsed = offline.parse_allowed_hosts(
            "LLM.corp.example, https://gpu.corp.example:8443/v1\n*.Models.Example  10.20.0.7/16 [FD00::1] "
            "10.0.0.131 llm.corp.example")
        assert parsed == ("llm.corp.example", "gpu.corp.example", "*.models.example", "10.20.0.0/16",
                          "fd00::1", "10.0.0.131")

    @pytest.mark.parametrize("value, fix", [
        ("*", "turns offline mode off"),
        ("0.0.0.0/0", "turns offline mode off"),
        ("::/0", "turns offline mode off"),
        ("llm.corp:8080", "Leave the port out"),
        ("*.com", "top-level domain"),
        ("bad!host", "isn't a host name"),
        ("10.0.0.0/33", "isn't a network"),
    ])
    def test_refused(self, value, fix):
        with pytest.raises(ValueError, match=fix):
            offline.parse_allowed_hosts([value])

    def test_not_a_list(self):
        with pytest.raises(ValueError, match="list of hosts"):
            offline.parse_allowed_hosts({"host": "x"})
        with pytest.raises(ValueError, match="list of hosts"):
            offline.parse_allowed_hosts([3])


class TestMessage:
    def test_blocked_message_names_feature_and_host(self):
        _on()
        with pytest.raises(OfflineBlocked) as caught:
            offline.check_url("https://cloud.example.com/api", "Lumi Cloud")
        assert str(caught.value) == (
            "Offline mode: Lumi Cloud needs cloud.example.com; allow it or turn offline mode off.")
        assert caught.value.host == "cloud.example.com"

    def test_managed_message_says_who(self):
        offline.set_for_tests(enabled=True, managed_by="Acme", locked=("enabled",))
        text = offline.refusal("https://api.openai.com/v1", "OpenAI")
        assert text.startswith("Offline mode: OpenAI needs api.openai.com; allow it or turn offline mode off.")
        assert "Acme's policy manages offline mode" in text

    @pytest.mark.parametrize("url", [
        "https://evil.com\\@localhost/", "https://localhost@evil.com/", "https://user:pw@localhost/",
        "https://evil.com /", "https://evil.com\t/", "https://evil.com:99999/", "https:///nohost",
    ])
    def test_ambiguous_addresses_are_refused(self, url):
        _on()
        assert offline.refusal(url, "browsing").startswith("Offline mode: browsing needs an address Lumi can't read")

    def test_message_for_follows_causes(self):
        blocked = OfflineBlocked("GitHub", "api.github.com")
        try:
            try:
                raise blocked
            except OfflineBlocked as inner:
                raise RuntimeError("wrapped") from inner
        except RuntimeError as outer:
            assert offline.message_for(outer) == str(blocked)
        assert offline.message_for(ValueError("x")) == ""


# ── Settings and the policy ─────────────────────────────────────────────────


class TestSettingsAndPolicy:
    def test_from_settings(self):
        config = offline.from_settings(_Settings({"offline": {"enabled": True, "allowed_hosts": ["llm.corp"]}}))
        assert config.enabled and config.allowed_hosts == ("llm.corp",) and not config.managed_by

    def test_off_by_default(self):
        assert offline.from_settings(_Settings()).enabled is False

    def test_policy_lock_wins_over_user_setting(self):
        from lumi import policy

        policy.set_for_tests(policy.parse({"schema": policy.SCHEMA, "organization": "Acme",
                                           "settings": {"offline.enabled": True}}, source="test"))
        settings = _Settings({"offline": {"enabled": False, "allowed_hosts": ["api.openai.com"]}},
                             locked={"offline": {"enabled": True}})
        config = offline.from_settings(settings)
        assert config.enabled and config.managed_by == "Acme" and config.locked == ("enabled",)
        # The organization turned it on, so a person's own hosts don't widen it.
        assert config.allowed_hosts == ()
        assert "only the hosts it allows" in config.problems[0]

    def test_policy_hosts_apply_when_locked(self):
        settings = _Settings({"offline": {"enabled": True, "allowed_hosts": ["mine.example"]}},
                             locked={"offline": {"enabled": True, "allowed_hosts": ["llm.corp"]}})
        assert offline.from_settings(settings).allowed_hosts == ("llm.corp",)

    def test_policy_can_keep_offline_mode_off(self):
        settings = _Settings({"offline": {"enabled": True}}, locked={"offline": {"enabled": False}})
        assert offline.from_settings(settings).enabled is False

    def test_hand_edited_bad_entries_allow_less_not_more(self):
        config = offline.from_settings(_Settings({"offline": {"enabled": True,
                                                              "allowed_hosts": ["llm.corp", "*", "bad!host"]}}))
        assert config.allowed_hosts == ("llm.corp",)
        assert len(config.problems) == 2

    @pytest.mark.parametrize("settings, fix", [
        ({"offline.enabled": "yes"}, "true or false"),
        ({"offline.allowed_hosts": "*"}, "turns offline mode off"),
        ({"offline.allowed_hosts": {"a": 1}}, "list of hosts"),
        ({"offline.everything": True}, "isn't an offline setting"),
    ])
    def test_policy_values_lumi_cant_apply_make_it_invalid(self, settings, fix):
        from lumi import policy

        with pytest.raises(policy.PolicyError, match=fix):
            policy.parse({"schema": policy.SCHEMA, "settings": settings}, source="test")

    @pytest.mark.parametrize("hosts, valid", [(["llm.corp.example", "10.20.0.0/16"], True), (["*"], False)])
    def test_the_standard_library_profile_maker_checks_offline_settings(self, tmp_path, hosts, valid):
        """Policy validation, offline.* included, runs where httpx isn't installed (make_mobileconfig.py)."""
        import subprocess
        import sys
        from pathlib import Path

        from lumi import policy

        root = Path(__file__).resolve().parents[1]
        source = tmp_path / "policy.json"
        source.write_text(json.dumps({"schema": policy.SCHEMA, "organization": "Acme",
                                      "settings": {"offline.enabled": True, "offline.allowed_hosts": hosts}}),
                          encoding="utf-8")
        result = subprocess.run([sys.executable, "-I", "-S", str(root / "packaging/policy/make_mobileconfig.py"),
                                 str(source), "--out", str(tmp_path / "policy.mobileconfig")],
                                capture_output=True, text=True, timeout=30)
        if valid:
            assert result.returncode == 0, result.stderr
        else:
            assert result.returncode == 1 and "turns offline mode off" in result.stderr, result.stderr

    def test_settings_manager_applies_the_lock(self, tmp_path):
        from lumi import policy
        from lumi.gui.settings import SettingsManager

        manager = SettingsManager(tmp_path / "settings.json")
        manager.set("offline", "enabled", False)
        policy.set_for_tests(policy.parse({"schema": policy.SCHEMA, "organization": "Acme",
                                           "settings": {"offline.enabled": True,
                                                        "offline.allowed_hosts": ["10.0.0.131"]}}, source="t"))
        config = net.configure(manager)
        assert config["offline"] is True
        assert offline.current().allowed_hosts == ("10.0.0.131",)
        assert offline.current().managed_by == "Acme"

    @pytest.mark.parametrize("settings", [
        {"offline.enabled": True, "offline.allowed_hosts": ["llm.corp.example", "*.com"]},
        {"offline.enabled": "true"},
    ], ids=["a host pattern Lumi refuses", "enabled as text"])
    def test_a_policy_lumi_cant_use_keeps_offline_mode_on(self, tmp_path, monkeypatch, settings):
        from lumi import policy

        path = tmp_path / "policy.json"
        path.write_text(json.dumps({"schema": policy.SCHEMA, "organization": "Acme", "settings": settings}),
                        encoding="utf-8")
        monkeypatch.setattr(policy, "_registry_policy", lambda: None)
        monkeypatch.setattr(policy, "_macos_managed_policy", lambda: None)
        monkeypatch.setattr(policy, "machine_keys", lambda: {})
        monkeypatch.setattr(policy, "machine_policy_file", lambda: path)
        state = policy.load(force=True)
        assert state.policy is None and "is invalid" in state.error
        # The person's settings say off; the organization meant on. A mistake in its policy mustn't turn it off.
        config = offline.configure(_Settings({"offline": {"enabled": False, "allowed_hosts": ["api.openai.com"]}}))
        assert config.enabled and config.allowed_hosts == () and config.policy_error == state.error
        assert config.managed_by == "your organization" and "offline mode stays on" in config.problems[0]
        assert offline.host_allowed("127.0.0.1") and not offline.host_allowed("llm.corp.example")
        assert offline.refusal("https://api.openai.com/v1", "OpenAI") == (
            "Offline mode: OpenAI needs api.openai.com. Your organization's policy can't be used, so offline mode "
            "stays on until your administrator fixes it.")
        # Startup code that reads settings.json itself (the updater) sees the same.
        stored = tmp_path / "settings.json"
        stored.write_text(json.dumps({"offline": {"enabled": False}}), encoding="utf-8")
        assert offline.read(stored).enabled is True
        assert offline.read(stored, policy.PolicyState(error="The policy file couldn't be read.")).enabled is True

    def test_read_without_settings_manager(self, tmp_path):
        from lumi import policy

        path = tmp_path / "settings.json"
        path.write_text(json.dumps({"offline": {"enabled": True, "allowed_hosts": ["llm.corp"]}}), encoding="utf-8")
        assert offline.read(path, policy.PolicyState()).allowed_hosts == ("llm.corp",)
        locked = policy.PolicyState(policy=policy.parse({"schema": policy.SCHEMA, "organization": "Acme",
                                                         "settings": {"offline.enabled": False}}, source="t"))
        assert offline.read(path, locked).enabled is False


# ── The client factory ──────────────────────────────────────────────────────


class TestClientFactory:
    def _client(self, feature="Lumi Cloud"):
        return httpx.Client(**net.client_options(timeout=5.0, transport=httpx.MockTransport(_ok), feature=feature))

    def test_blocked_before_connecting(self):
        _on()
        sent = []

        def transport(request):
            sent.append(request)
            return httpx.Response(200)

        with httpx.Client(**net.client_options(timeout=5.0, transport=httpx.MockTransport(transport),
                                               feature="Lumi Cloud")) as client:
            with pytest.raises(OfflineBlocked, match="Offline mode: Lumi Cloud needs cloud.example.com"):
                client.get("https://cloud.example.com/api/v1/me")
        assert sent == []

    def test_local_and_allowed_go_through(self):
        _on("llm.corp.example")
        with self._client() as client:
            assert client.get("http://127.0.0.1:11434/api/tags").status_code == 200
            assert client.get("http://[::1]:8080/").status_code == 200
            assert client.get("https://llm.corp.example/v1/models").status_code == 200

    def test_redirects_are_checked(self):
        _on("llm.corp.example")

        def redirect(request):
            if request.url.host == "llm.corp.example":
                return httpx.Response(302, headers={"Location": "https://exfil.example.com/x"})
            return httpx.Response(200)

        with httpx.Client(**net.client_options(timeout=5.0, transport=httpx.MockTransport(redirect),
                                               feature="GitHub"), follow_redirects=True) as client:
            with pytest.raises(OfflineBlocked, match="GitHub needs exfil.example.com"):
                client.get("https://llm.corp.example/")

    def test_hook_reads_offline_mode_at_each_request(self):
        offline.reset_for_tests()
        with self._client() as client:
            assert client.get("https://cloud.example.com/").status_code == 200
            _on()
            with pytest.raises(OfflineBlocked):
                client.get("https://cloud.example.com/")

    def test_is_a_connect_error(self):
        _on()
        with self._client() as client, pytest.raises(httpx.ConnectError) as caught:
            client.get("https://cloud.example.com/")
        assert net.error_text(caught.value).startswith("Offline mode: Lumi Cloud needs")
        assert net.error_text(httpx.ReadTimeout("slow")) == "ReadTimeout"


# ── The process-wide backstop ───────────────────────────────────────────────


class TestBackstop:
    def test_lookup_of_a_blocked_host_fails_at_once(self):
        _on("llm.corp.example")
        with pytest.raises(offline.BlockedLookup, match="Offline mode: a network connection needs "
                                                        "blocked.invalid"):
            socket.getaddrinfo("blocked.invalid", 443)

    def test_local_and_allowed_lookups_proceed(self):
        _on("llm.corp.invalid")
        assert socket.getaddrinfo("127.0.0.1", 80)  # an address: nothing is looked up
        # Names go to the hook directly, so no lookup leaves this computer.
        for host in ("localhost", "llm.corp.invalid", "LLM.corp.invalid.", b"llm.corp.invalid", socket.gethostname()):
            assert offline._audit("socket.getaddrinfo", (host, 80, 0, 0, 0)) is None

    def test_every_kind_of_lookup_is_checked(self):
        _on("llm.corp.example", "10.20.0.0/16")
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            refused = [("socket.gethostbyname", ("blocked.example",)),  # gethostbyname_ex raises it too
                       ("socket.gethostbyaddr", ("blocked.example",)),
                       ("socket.gethostbyaddr", ("192.0.2.1",)),  # a reverse lookup asks the DNS server
                       ("socket.getnameinfo", (("192.0.2.1", 80),)),
                       ("socket.sendto", (sock, ("blocked.example", 53))),
                       ("socket.sendmsg", (sock, ("blocked.example", 53)))]
            allowed = [("socket.gethostbyname", ("llm.corp.example",)),
                       ("socket.gethostbyaddr", ("127.0.0.1",)),
                       ("socket.gethostbyaddr", (socket.gethostname(),)),  # socket.getfqdn()
                       ("socket.getnameinfo", (("10.20.1.2", 80),)),
                       ("socket.sendto", (sock, ("10.9.9.9", 53))),  # an address: the clients' check
                       ("socket.sendmsg", (sock, None)),
                       ("socket.getaddrinfo", (None, 80, 0, 0, 0)),
                       ("socket.bind", (sock, ("blocked.example", 0)))]
            for event, args in refused:
                with pytest.raises(offline.BlockedLookup, match="Offline mode: a network connection needs "):
                    offline._audit(event, args)
            for event, args in allowed:
                assert offline._audit(event, args) is None, event
        # The hook is installed: a real call is refused before anything is looked up.
        with pytest.raises(offline.BlockedLookup, match="needs blocked.invalid"):
            socket.gethostbyname("blocked.invalid")

    @pytest.mark.parametrize("host", [
        "evil.com\x00.corp.example", b"evil.com\x00.corp.example", "evil.com\n.corp.example",
        "evil.com%2f.corp.example", "evil.com/.corp.example", "evil.com@x.corp.example", "evil com.corp.example",
    ])
    def test_names_no_resolver_should_see_are_refused(self, host):
        # Each ends in an allowed domain, but a resolver, or a program that
        # decodes it, could reach evil.com: only plain host names match.
        _on("*.corp.example")
        assert not offline.host_allowed(host)
        with pytest.raises(offline.BlockedLookup) as caught:
            offline._audit("socket.getaddrinfo", (host, 443, 0, 0, 0))
        assert not any(ord(ch) < 0x20 for ch in str(caught.value))
        assert offline.host_allowed("x.corp.example")

    def test_raw_httpx_calls_fail_fast_with_the_message(self):
        _on()
        with pytest.raises(httpx.ConnectError) as caught:
            httpx.get("http://blocked.invalid:9/", timeout=30)
        assert offline.message_for(caught.value).startswith("Offline mode: a network connection needs blocked.invalid")

    def test_connect_by_name_is_checked(self):
        # Python resolves a name inside connect() before its audit event, so
        # the hook is called directly with what a connect by name reports.
        _on()
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            with pytest.raises(offline.BlockedLookup):
                offline._audit("socket.connect", (sock, ("blocked.example", 80)))
            offline._audit("socket.connect", (sock, ("localhost", 80)))
            offline._audit("socket.connect", (sock, ("10.9.9.9", 80)))  # addresses: the clients' check
            offline._audit("socket.connect", (sock, "/tmp/unix.sock"))

    def test_nothing_is_checked_while_off(self):
        _on()
        offline.reset_for_tests()
        # The hook stays installed and lets everything through (called directly: no lookup leaves).
        for event, args in (("socket.getaddrinfo", ("blocked.invalid", 443, 0, 0, 0)),
                            ("socket.gethostbyname", ("blocked.invalid",)),
                            ("socket.getnameinfo", (("192.0.2.1", 80),))):
            assert offline._audit(event, args) is None
