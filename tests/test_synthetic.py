"""Tests for the synthetic data generator and the demo config."""
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

from leakscope.ingestion import load_config
from leakscope.synthetic import TLC_COLUMNS, generate, make_demo_config


def test_schema_matches_the_tlc_files(tmp_path):
    paths = generate(tmp_path, n_trips=2000)
    desc = duckdb.sql(
        f"DESCRIBE SELECT * FROM read_parquet('{Path(paths['parquet']).as_posix()}')"
    ).fetchall()
    assert [r[0] for r in desc] == TLC_COLUMNS and len(TLC_COLUMNS) == 19
    types = {r[0]: r[1] for r in desc}
    assert types["tpep_pickup_datetime"].startswith("TIMESTAMP")
    assert types["PULocationID"] == "INTEGER"


def test_same_seed_same_data_different_seed_different_data(tmp_path):
    a = generate(tmp_path / "a", n_trips=1500, seed=3)
    b = generate(tmp_path / "b", n_trips=1500, seed=3)
    c = generate(tmp_path / "c", n_trips=1500, seed=4)
    da, db, dc = (pd.read_parquet(x["parquet"]) for x in (a, b, c))
    pd.testing.assert_frame_equal(da, db)
    assert not da.equals(dc)


def test_data_has_the_structure_the_demo_relies_on(tmp_path):
    df = pd.read_parquet(generate(tmp_path, n_trips=20000)["parquet"])
    dur = (df["tpep_dropoff_datetime"] - df["tpep_pickup_datetime"]).dt.total_seconds() / 60
    ok = (dur >= 1) & (dur <= 180) & (df["tpep_pickup_datetime"] >= "2024-01-01")
    assert ok.mean() > 0.98 and (~ok).sum() > 0          # a little dirt, not much
    good, d = df[ok], dur[ok]
    assert np.corrcoef(good["trip_distance"], d)[0, 1] > 0.6
    assert np.corrcoef(good["fare_amount"], d)[0, 1] > 0.6
    assert good["passenger_count"].isna().mean() > 0.01


def test_demo_config_reuses_the_real_settings_but_isolates_paths(tmp_path):
    base = load_config("configs/taxi.yaml")
    path = make_demo_config(tmp_path / "w", "configs/taxi.yaml", n_examples=123, n_boot=45)
    cfg = load_config(path)
    for section in ("availability", "cleaning", "window", "ml"):
        assert cfg[section] == base[section]
    assert cfg["sampling"]["n_examples"] == 123 and cfg["analysis"]["n_boot"] == 45
    root = (tmp_path / "w").resolve()
    for key in ("raw_parquet", "zone_lookup", "database", "temp_dir"):
        assert Path(cfg["data"][key]).resolve().is_relative_to(root)
    assert cfg["data"]["database"] != base["data"]["database"]