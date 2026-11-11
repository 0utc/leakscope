"""Reproducibility: rebuilding must give identical features.

Regression tests for a defect found by comparing two rebuilds of the real database:
summing decimal minutes in DuckDB windows is order-dependent for rows with tied
timestamps, which changed feature values at the 1e-13 level and, through the
model's histogram bins, changed a score in the third decimal. Durations are now
accumulated as integer seconds."""
import contextlib
import io

import duckdb
import numpy as np
import pandas as pd
import pytest

from leakscope.feature_builder import create_zone_cum, create_zone_global
from leakscope.ingestion import load_config
from leakscope.pipeline import run_all
from leakscope.synthetic import generate, make_demo_config


def _trips(order_seed, n=3000):
    """Same trips every time, but in a different insertion order; only 60 distinct
    timestamps, so many rows are tied."""
    rng = np.random.default_rng(5)
    zones = rng.integers(1, 4, n)
    times = (np.datetime64("2024-01-10T10:00:00", "s")
             + rng.integers(0, 60, n).astype("timedelta64[s]"))
    seconds = rng.integers(60, 10800, n)
    df = pd.DataFrame({
        "trip_id": np.arange(n), "pu_zone": zones, "event_pickup": times,
        "avail_dropoff": times + seconds.astype("timedelta64[s]"),
        "duration_min": seconds / 60.0})
    return df.sample(frac=1.0, random_state=order_seed).reset_index(drop=True)


def _con(df):
    con = duckdb.connect()
    con.execute("SET threads=2")
    con.execute("CREATE TABLE trip_times AS SELECT * FROM df")
    return con


@pytest.mark.parametrize("order_col, table", [("avail_dropoff", "zone_cum"),
                                              ("event_pickup", "zone_cum_pickup")])
def test_running_totals_do_not_depend_on_the_order_of_tied_rows(order_col, table):
    results = []
    for seed in (1, 2, 3):
        con = _con(_trips(seed))
        create_zone_cum(con, order_col, table)
        results.append(con.execute(
            f"SELECT DISTINCT pu_zone, {order_col}, cum_sum, cum_cnt FROM {table} "
            f"ORDER BY 1, 2, 3, 4").fetchall())
        con.close()
    assert results[0] == results[1] == results[2]


def test_zone_global_average_does_not_depend_on_row_order():
    results = []
    for seed in (1, 2, 3):
        con = _con(_trips(seed))
        create_zone_global(con)
        results.append(con.execute(
            "SELECT pu_zone, zone_avg_global, n_trips FROM zone_global ORDER BY 1").fetchall())
        con.close()
    assert results[0] == results[1] == results[2]


def test_running_totals_are_the_exact_minutes_of_whole_seconds():
    con = duckdb.connect()
    con.execute("""
        CREATE TABLE trip_times AS
        SELECT * FROM (VALUES
            (1, 1, TIMESTAMP '2024-01-10 10:00:00', TIMESTAMP '2024-01-10 10:10:00', 10.0 / 60),
            (2, 1, TIMESTAMP '2024-01-10 10:00:00', TIMESTAMP '2024-01-10 10:10:00', 20.0 / 60),
            (3, 1, TIMESTAMP '2024-01-10 10:05:00', TIMESTAMP '2024-01-10 10:20:00', 30.0 / 60)
        ) t(trip_id, pu_zone, event_pickup, avail_dropoff, duration_min)
    """)
    create_zone_cum(con)
    rows = con.execute("SELECT avail_dropoff, cum_sum, cum_cnt FROM zone_cum "
                       "ORDER BY avail_dropoff, cum_cnt").fetchall()
    con.close()
    assert [(r[1], r[2]) for r in rows] == [(30.0 / 60, 2), (30.0 / 60, 2), (60.0 / 60, 3)]


def test_rebuilding_the_pipeline_gives_identical_feature_tables(tmp_path):
    builds = []
    for name in ("a", "b"):
        work = tmp_path / name
        generate(work, n_trips=30_000)
        cfg = make_demo_config(work, "configs/taxi.yaml", n_examples=8000, n_boot=50)
        with contextlib.redirect_stdout(io.StringIO()):
            run_all(str(cfg))
        builds.append(load_config(cfg)["data"]["database"])
    con = duckdb.connect()
    con.execute(f"ATTACH '{builds[0]}' AS a (READ_ONLY)")
    con.execute(f"ATTACH '{builds[1]}' AS b (READ_ONLY)")
    columns = {"scenario_features": ["zone_avg_dur_60m", "leak_zone_avg_60m_self",
                                     "leak_zone_avg_60m_inprogress", "leak_fare_correction",
                                     "leak_snapshot_zone_avg", "leak_last_fare_zone"],
               "pit_features": ["zone_avg_dur_asof", "zone_avg_dur_7d"],
               "row_features": ["leak_zone_avg_global"]}
    for table, cols in columns.items():
        for col in cols:
            n_diff = con.execute(
                f"SELECT count(*) FILTER (WHERE x.{col} IS DISTINCT FROM y.{col}) "
                f"FROM a.{table} x JOIN b.{table} y USING (trip_id)").fetchone()[0]
            assert n_diff == 0, f"{table}.{col}: {n_diff} values differ between rebuilds"
    con.close()