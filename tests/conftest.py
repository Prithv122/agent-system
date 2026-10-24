"""Shared fixtures. Nothing here calls a model or the network.

DuckDB cannot open an in-memory database read-only, so the warehouse is built once per test
session as a file and every test that needs the agent's view of it opens that file read-only.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import duckdb
import pytest

from analyst._vendor import warehouse

# A key stored on the developer's machine must never turn a test into a live call.
os.environ["SECRETS_NO_KEYRING"] = "1"


@pytest.fixture(scope="session")
def warehouse_path(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The seeded warehouse as a file. Treat it as read-only: it is shared by every test."""
    path = tmp_path_factory.mktemp("warehouse") / "warehouse.duckdb"
    warehouse.build(path)
    return path


@pytest.fixture
def ro_con(warehouse_path: Path) -> Iterator[duckdb.DuckDBPyConnection]:
    """The agent's view: read-only, no file or network access from SQL."""
    con = duckdb.connect(
        str(warehouse_path), read_only=True, config={"enable_external_access": False}
    )
    yield con
    con.close()
