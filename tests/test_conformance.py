"""Conformance: stored aggregate values must equal a slow recomputation from
the DECLARED definition in the registry. This links what the registry says
(and what the auditor audits) to what the pipeline actually computed."""
from pathlib import Path

import duckdb
import pytest

from leakscope.registry import load_registry, lookback_minutes

DB = Path("data/processed/leakscope.duckdb")
LOCATION = {
    "zone_avg_dur_asof": ("pit_features", "zone_avg_dur_asof"),
    "zone_avg_dur_7d": ("pit_features", "zone_avg_dur_7d"),
    "zone_avg_dur_60m": ("scenario_features", "zone_avg_dur_60m"),
    "leak_zone_avg_global": ("row_features", "leak_zone_avg_global"),
    "leak_zone_avg_60m_self": ("scenario_features", "leak_zone_avg_60m_self"),
    "leak_zone_avg_60m_inprogress": ("scenario_features", "leak_zone_avg_60m_inprogress"),
}
N_SAMPLE = 30


def reference_average(con, feat, trip_id, zone, t_pred, duration):
    clock = feat.get("window_clock", "avail_dropoff")
    assert clock in ("avail_dropoff", "event_pickup")
    conds, params = ["pu_zone = ?", "trip_id <> ?"], [zone, trip_id]
    if feat["window"] in ("up_to_t_pred", "lookback"):
        conds.append(f"{clock} <= CAST(? AS TIMESTAMP)")
        params.append(t_pred)
    if feat["window"] == "lookback":
        conds.append(f"{clock} > CAST(? AS TIMESTAMP) - "
                     f"INTERVAL {lookback_minutes(feat)} MINUTE")
        params.append(t_pred)
    n, s = con.execute(
        f"SELECT count(*), sum(duration_min) FROM trip_times "
        f"WHERE {' AND '.join(conds)}", params).fetchone()
    s = s or 0.0
    if feat["includes_current_row"]:
        n, s = n + 1, s + duration
    return s / n if n else None


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


@pytest.mark.parametrize("name", sorted(LOCATION))
def test_stored_values_follow_the_declared_definition(con, name):
    feat = next(f for f in load_registry() if f["name"] == name)
    table, col = LOCATION[name]
    sample = con.execute(f"""
        SELECT trip_id, pu_zone, t_pred, target_duration_min FROM row_features
        ORDER BY hash(trip_id) LIMIT {N_SAMPLE}
    """).fetchall()
    for trip_id, zone, t_pred, duration in sample:
        stored = con.execute(
            f"SELECT {col} FROM {table} WHERE trip_id = ?", [trip_id]).fetchone()[0]
        expected = reference_average(con, feat, trip_id, zone, t_pred, duration)
        if expected is None:
            assert stored is None, (name, trip_id)
        else:
            assert stored == pytest.approx(expected, rel=1e-6, abs=1e-9), (name, trip_id)