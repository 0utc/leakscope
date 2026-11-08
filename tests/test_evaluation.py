"""Tests for the ML comparison harness: split rule, experiment definition,
metrics, determinism and integrity of the model table."""
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd
import pytest

from leakscope import scenarios
from leakscope.evaluation import (
    build_model_table, chronological_split, define_experiments, fit_and_score,
    mae, rmse,
)
from leakscope.ingestion import load_config
from leakscope.registry import load_registry

DB = Path("data/processed/leakscope.duckdb")
ML = {"model": {"max_iter": 50, "learning_rate": 0.1, "max_leaf_nodes": 15}}


def test_split_is_purged_and_respects_both_boundaries():
    df = pd.DataFrame({
        "name": list("abcde"),
        "t_pred": pd.to_datetime([
            "2024-01-24 10:00:00", "2024-01-24 23:50:00", "2024-01-24 23:30:00",
            "2024-01-25 00:00:00", "2024-01-26 12:00:00"]),
        "label_available_at": pd.to_datetime([
            "2024-01-24 10:30:00", "2024-01-25 00:10:00", "2024-01-25 00:00:00",
            "2024-01-25 00:20:00", "2024-01-26 12:10:00"]),
    })
    train, test, purged = chronological_split(df, "2024-01-25")
    assert list(train["name"]) == ["a", "c"]    # c: label available at exactly the cutoff
    assert list(test["name"]) == ["d", "e"]     # d: predicted at exactly the cutoff
    assert purged == 1                          # b: still in progress at the cutoff
    assert set(train["name"]).isdisjoint(test["name"])
    assert train["label_available_at"].max() <= pd.Timestamp("2024-01-25")
    assert test["t_pred"].min() >= pd.Timestamp("2024-01-25")


def test_experiments_follow_the_labelled_ground_truth():
    feats = load_registry()
    exps = define_experiments(feats)
    safe = exps[0]["cols"]
    assert exps[0]["scenario"] == "-" and exps[0]["added"] == []
    assert all(not scenarios.EXPECTED_RULES[f["scenario"]]
               for f in feats if f["name"] in safe)
    singles = exps[1:-1]
    assert [e["scenario"] for e in singles] == [f"L{i}" for i in range(1, 10)]
    for e in singles:
        assert len(e["added"]) == 1 and e["cols"] == safe + e["added"]
    assert exps[-1]["scenario"] == "ALL" and len(exps[-1]["added"]) == 9
    assert len(exps) == 11


def test_metrics():
    y, p = [1.0, 2.0, 3.0, 4.0], [1.0, 3.0, 3.0, 6.0]
    assert mae(y, p) == pytest.approx(0.75)
    assert rmse(y, p) == pytest.approx(np.sqrt(5 / 4))


def _toy(n=600, seed=1):
    rng = np.random.default_rng(seed)
    x1 = rng.normal(size=n)
    target = 3 * x1 + rng.normal(scale=1.0, size=n)
    df = pd.DataFrame({"x1": x1, "leak": target + rng.normal(scale=0.05, size=n),
                       "target": target})
    return df.iloc[:400], df.iloc[400:]


def test_same_seed_gives_identical_results():
    train, test = _toy()
    a = fit_and_score(train, test, ["x1"], ML, seed=0)
    b = fit_and_score(train, test, ["x1"], ML, seed=0)
    assert a["mae"] == b["mae"] and a["rmse"] == b["rmse"]


def test_harness_rewards_a_leaky_feature_on_a_toy_problem():
    train, test = _toy()
    safe = fit_and_score(train, test, ["x1"], ML, seed=0)
    leaky = fit_and_score(train, test, ["x1", "leak"], ML, seed=0)
    assert leaky["mae"] < 0.5 * safe["mae"]       # the leak looks like a big win


def test_model_table_integrity_on_real_database():
    if not DB.exists():
        pytest.skip("local database not built (run python -m leakscope.pipeline)")
    con = duckdb.connect(str(DB), read_only=True)
    con.execute("SET memory_limit='1GB'")
    try:
        feats = load_registry()
        df = build_model_table(con, feats)
        n_examples = con.execute("SELECT count(*) FROM row_features").fetchone()[0]
    finally:
        con.close()
    names = [f["name"] for f in feats]
    assert len(df) == n_examples and df["trip_id"].is_unique
    assert (df["label_available_at"] > df["t_pred"]).all()
    assert set(names) <= set(df.columns)
    assert not np.isinf(df[names].to_numpy()).any()
    train, test, purged = chronological_split(
        df, load_config("configs/taxi.yaml")["ml"]["test_start"])
    assert len(train) > 0 and len(test) > 0
    assert len(train) + len(test) + purged == len(df)