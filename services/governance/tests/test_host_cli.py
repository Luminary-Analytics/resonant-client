"""The host listener needs explicit protected configuration and real TLS files."""
# ruff: noqa: F811 -- imported pytest fixture is intentionally used by name.

import json
import base64

from sonn_governance.host_main import main, load_configuration
from test_host_http import certificates  # noqa: F401


def test_host_cli_rejects_public_test_listener_without_logging_credentials(tmp_path, certificates, monkeypatch, capsys):
    secret = "synthetic-host-database-secret"
    config = {"database_dsn": secret, "transport": {"host": "0.0.0.0", "port": 8444, "profile": "test",
        "certificate_file": certificates.server.certificate_file,
        "private_key_file": certificates.server.private_key_file, "client_ca_file": certificates.ca.certificate_file}}
    path = tmp_path / "private.json"
    path.write_text(json.dumps(config), encoding="utf-8")
    launched = []
    monkeypatch.setattr("sonn_governance.host_main.HostHTTPServer", lambda *args: launched.append(args))
    assert main(["--config-file", str(path)]) == 2
    assert not launched
    output = capsys.readouterr()
    assert secret not in output.out + output.err


def test_host_sharing_keyring_is_separate_and_explicit(tmp_path, certificates):
    config = {"database_dsn": "synthetic-host-database-secret", "transport": {"host": "127.0.0.1", "port": 8444, "profile": "test",
        "certificate_file": certificates.server.certificate_file,
        "private_key_file": certificates.server.private_key_file, "client_ca_file": certificates.ca.certificate_file}}
    path = tmp_path / "private-host.json"
    path.write_text(json.dumps(config))
    assert load_configuration(path)[2] is None
    ring = tmp_path / "private-sharing-keys.json"
    ring.write_text(json.dumps({"active_id": "fixture", "keys": {"fixture": base64.b64encode(b"x" * 32).decode()}}))
    ring.chmod(0o600)
    config["sharing"] = {"key_ring_file": str(ring)}
    path.write_text(json.dumps(config))
    assert load_configuration(path)[2].active_id == "fixture"
