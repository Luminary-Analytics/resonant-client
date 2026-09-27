"""Real disposable PostgreSQL fixture; no existing/default database is assumed."""

import json
import os
from pathlib import Path

import pytest

from sonn_governance.store import migrate


@pytest.fixture(scope="session")
def database_config():
    path = os.environ.get("SONN_GOVERNANCE_TEST_CONFIG")
    if not path:
        pytest.skip("set SONN_GOVERNANCE_TEST_CONFIG to a private disposable PostgreSQL config")
    config = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    required = {"owner_dsn", "application_dsn", "application_role"}
    if not required <= config.keys():
        pytest.fail("invalid disposable database configuration")
    migrate(config["owner_dsn"], application_role=config["application_role"])
    return config
