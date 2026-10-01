"""Configuration validation never opens a socket or inherits external addresses."""

import json
from uuid import uuid4

from psycopg.conninfo import conninfo_to_dict
import pytest

from sonn_governance.archive_main import load_configuration


@pytest.mark.parametrize("host", ["127.0.0.1", "::1"])
@pytest.mark.parametrize("override", ["dsn", "environment", "service"])
def test_loopback_profile_pins_effective_address_before_any_connection(tmp_path, monkeypatch, host, override):
    def unexpected_connection(*args, **kwargs):
        pytest.fail("Configuration validation opened a database connection")

    monkeypatch.setattr("psycopg.connect", unexpected_connection)
    monkeypatch.setenv("PGHOSTADDR", "198.51.100.9")
    dsn = f"host={host} dbname=fixture user=fixture password=synthetic-only"
    if override == "dsn":
        dsn += " hostaddr=198.51.100.10"
    elif override == "service":
        service = tmp_path / "pg_service.conf"
        service.write_text("[fixture]\nhostaddr=198.51.100.11\n", encoding="utf-8")
        monkeypatch.setenv("PGSERVICEFILE", str(service))
        dsn += " service=fixture"
    config = {"source_dsn": dsn, "archive_dsn": dsn, "source_id": str(uuid4()),
        "archive_id": str(uuid4()), "tenant_ids": [str(uuid4())], "profile": "test", "interval_seconds": 1}
    path = tmp_path / "relay.json"
    path.write_text(json.dumps(config), encoding="utf-8")
    loaded = load_configuration(str(path))
    for field in ("source_dsn", "archive_dsn"):
        parameters = conninfo_to_dict(loaded[field])
        assert parameters["host"] == host
        assert parameters["hostaddr"] == host
        assert parameters["password"] == "synthetic-only"


def test_external_test_host_still_refused_without_connecting(tmp_path, monkeypatch):
    def unexpected_connection(*args, **kwargs):
        pytest.fail("Invalid configuration opened a database connection")

    monkeypatch.setattr("psycopg.connect", unexpected_connection)
    path = tmp_path / "relay.json"
    path.write_text(json.dumps({"source_dsn": "host=198.51.100.9 hostaddr=127.0.0.1",
        "archive_dsn": "host=127.0.0.1", "source_id": str(uuid4()), "archive_id": str(uuid4()),
        "tenant_ids": [str(uuid4())], "profile": "test", "interval_seconds": 1}), encoding="utf-8")
    with pytest.raises(ValueError, match="loopback"):
        load_configuration(str(path))
