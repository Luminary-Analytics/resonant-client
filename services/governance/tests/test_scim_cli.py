"""SCIM listener requires explicit transport and never consumes human identity."""

import json

import pytest

from sonn_governance.scim_main import load_configuration, main


def configuration():
    return {"database_dsn": "synthetic-private-runtime-dsn", "listen": {"host": "127.0.0.1", "port": 19999},
            "transport": {"profile": "test", "allow_test_loopback": True}, "tls": None}


def test_explicit_scim_launch_has_no_proxy_accesslogs_or_inline_token(tmp_path, monkeypatch):
    path = tmp_path / "protected-scim.json"
    value = configuration()
    path.write_text(json.dumps(value))
    assert load_configuration(path)["transport"].profile == "test"
    monkeypatch.setattr("sonn_governance.scim_main.GovernanceStore", lambda _: object())
    launches = []
    monkeypatch.setattr("sonn_governance.scim_main.uvicorn.run", lambda *args, **kwargs: launches.append(kwargs))
    assert main(["--config-file", str(path)]) == 0
    assert launches[0]["access_log"] is False and launches[0]["proxy_headers"] is False


@pytest.mark.parametrize("fault", ["production_plaintext", "public_test", "one_switch", "inline_bearer"])
def test_invalid_scim_launch_never_opens_listener_or_discloses_config(tmp_path, monkeypatch, capsys, fault):
    value = configuration()
    if fault == "production_plaintext":
        value["transport"] = {"profile": "production"}
    elif fault == "public_test":
        value["listen"]["host"] = "0.0.0.0"
    elif fault == "one_switch":
        value["transport"]["allow_test_loopback"] = False
    else:
        value["token"] = "synthetic-private-provisioner"
    path = tmp_path / "protected-scim.json"
    path.write_text(json.dumps(value))
    monkeypatch.setattr("sonn_governance.scim_main.uvicorn.run", lambda *args, **kwargs: pytest.fail("Unsafe listener started"))
    assert main(["--config-file", str(path)]) == 2
    output = capsys.readouterr()
    assert "synthetic-private" not in output.out + output.err
