"""Conformance on the real local database: stored aggregates must follow the
definitions declared in the registry."""
from pathlib import Path

import duckdb
import pytest

from leakscope.conformance import STORED_AT, check_conformance
from leakscope.registry import load_registry

DB = Path("data/processed/leakscope.duckdb")


@pytest.fixture(scope="module")
def con():
    if not DB.exists():
        pytest.skip("local database not built (run python -m leakscope.pipeline)")
    c = duckdb.connect(str(DB), read_only=True)
    c.execute("SET memory_limit='1GB'")
    try:
        c.execute("SELECT 1 FROM scenario_features LIMIT 1")
        c.execute("SELECT 1 FROM pit_features LIMIT 1")
    except duckdb.Error:
        c.close()
        pytest.skip("feature tables not built (run python -m leakscope.pipeline)")
    yield c
    c.close()


@pytest.mark.parametrize("name", sorted(STORED_AT))
def test_stored_values_follow_the_declared_definition(con, name):
    feat = next(f for f in load_registry() if f["name"] == name)
    table, col = STORED_AT[name]
    assert check_conformance(con, feat, table, col) == []