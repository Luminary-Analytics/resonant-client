"""Production cannot inherit an unencrypted or test-only listener configuration."""

import json
import base64

import pytest

from sonn_governance.__main__ import load_configuration, main


def configuration():
    return {"database_dsn": "synthetic-private-database-credential", "identity": {
        "issuer": "http://127.0.0.1:10001", "audience": "fixture-resource", "jwks_url": "http://127.0.0.1:10001/keys",
        "profile": "test", "allow_test_loopback": True}, "listen": {"host": "127.0.0.1", "port": 10002}, "tls": None}


@pytest.mark.parametrize("fault", ["production_test", "public_test", "production_http", "unexpected", "huge"])
def test_invalid_service_config_never_starts_listener_or_logs_credentials(tmp_path, monkeypatch, capsys, fault):
    value = configuration()
    if fault == "production_test":
        value["identity"]["profile"] = "production"
    elif fault == "public_test":
        value["listen"]["host"] = "0.0.0.0"
    elif fault == "production_http":
        value["identity"].update(issuer="https://idp.example.com", jwks_url="https://idp.example.com/keys",
                                 profile="production", allow_test_loopback=False)
    elif fault == "unexpected":
        value["trusted_forwarded_identity"] = True
    else:
        value["database_dsn"] *= 10000
    path = tmp_path / "protected.json"
    path.write_text(json.dumps(value), encoding="utf-8")
    monkeypatch.setattr("sonn_governance.__main__.uvicorn.run", lambda *args, **kwargs: pytest.fail("Unsafe config started a listener"))
    assert main(["--config-file", str(path)]) == 2
    output = capsys.readouterr()
    assert "synthetic-private" not in output.err and "synthetic-private" not in output.out


def test_explicit_test_config_stays_loopback_without_proxy_or_access_logging(tmp_path, monkeypatch):
    path = tmp_path / "protected.json"
    value = configuration()
    path.write_text(json.dumps(value), encoding="utf-8")
    assert load_configuration(path)["identity"].profile == "test"
    stores, launch = [], []
    monkeypatch.setattr("sonn_governance.__main__.GovernanceStore", lambda dsn: stores.append(dsn) or object())
    monkeypatch.setattr("sonn_governance.__main__.uvicorn.run", lambda *args, **kwargs: launch.append(kwargs))
    assert main(["--config-file", str(path)]) == 0
    assert stores == [value["database_dsn"]]
    assert launch[0]["host"] == "127.0.0.1"
    assert launch[0]["proxy_headers"] is False and launch[0]["access_log"] is False
    assert launch[0]["ws"] == "none" and launch[0]["server_header"] is False


def test_optional_content_keys_load_only_from_separate_absolute_file(tmp_path):
    ring = tmp_path / "private-keys.json"
    ring.write_text(json.dumps({"active_id": "first", "keys": {"first": base64.b64encode(b"s" * 32).decode()}}))
    ring.chmod(0o600)
    value = {**configuration(), "content": {"key_ring_file": str(ring)}}
    path = tmp_path / "service.json"
    path.write_text(json.dumps(value))
    assert load_configuration(path)["content"].active_id == "first"
    assert load_configuration(path)["sharing"] is None
    for invalid in ({"key_ring_file": "relative.json"}, {"keys": {"first": "inline-secret"}},
                    {"key_ring_file": str(ring), "active_id": "override"}):
        path.write_text(json.dumps({**value, "content": invalid}))
        with pytest.raises(ValueError):
            load_configuration(path)


def test_sharing_listener_capability_requires_its_own_explicit_configuration(tmp_path):
    ring = tmp_path / "sharing-keys.json"
    ring.write_text(json.dumps({"active_id": "sharing", "keys": {"sharing": base64.b64encode(b"x" * 32).decode()}}))
    ring.chmod(0o600)
    path = tmp_path / "service.json"
    path.write_text(json.dumps({**configuration(), "sharing": {"key_ring_file": str(ring)}}))
    configured = load_configuration(path)
    assert configured["sharing"].active_id == "sharing" and configured["content"] is None
    for invalid in (True, {"key_ring_file": "relative.json"}, {"keys": {"sharing": "inline-secret"}}):
        path.write_text(json.dumps({**configuration(), "sharing": invalid}))
        with pytest.raises(ValueError):
            load_configuration(path)


def test_invalid_content_key_never_reaches_listener_or_diagnostics(tmp_path, monkeypatch, capsys):
    ring = tmp_path / "private-keys.json"
    secret = "synthetic-private-key-material"
    ring.write_text(json.dumps({"active_id": "first", "keys": {"first": secret}}))
    ring.chmod(0o600)
    path = tmp_path / "service.json"
    path.write_text(json.dumps({**configuration(), "content": {"key_ring_file": str(ring)}}))
    monkeypatch.setattr("sonn_governance.__main__.uvicorn.run", lambda *args, **kwargs: pytest.fail("Invalid keys opened listener"))
    assert main(["--config-file", str(path)]) == 2
    output = capsys.readouterr()
    assert secret not in output.out + output.err
