"""Tests for the bootstrap, the production-condition substitutes and the
availability-clock lookup used for L9's serving value."""
import duckdb
import numpy as np
import pandas as pd
import pytest

from leakscope import scenarios
from leakscope.analysis import (
    LAST_FARE_AVAILABLE_SQL, SERVING_SUBSTITUTE, block_bootstrap, verdict,
)
from leakscope.evaluation import make_model
from leakscope.registry import load_registry

ML = {"model": {"max_iter": 50, "learning_rate": 0.1, "max_leaf_nodes": 15}}


def _errors(seed=0, n_blocks=24, per_block=10, scale=(1.0, 1.0)):
    rng = np.random.default_rng(seed)
    n = n_blocks * per_block
    a = np.column_stack([np.abs(rng.normal(size=n)) * s for s in scale])
    return np.repeat(np.arange(n_blocks), per_block), a, a ** 2


def test_point_estimates_are_the_plain_full_sample_values():
    blocks, a, s = _errors(scale=(1.0, 2.0))
    b = block_bootstrap(blocks, a, s, ref=0, n_boot=200, seed=0)
    assert b["mae"] == pytest.approx(a.mean(axis=0))
    assert b["rmse"] == pytest.approx(np.sqrt(s.mean(axis=0)))
    assert b["d_mae"][0] == 0
    assert b["d_mae"][1] == pytest.approx(a[:, 1].mean() - a[:, 0].mean())
    assert b["n_blocks"] == 24


def test_interval_excludes_zero_for_a_clearly_better_model():
    blocks, a, s = _errors(n_blocks=50, scale=(1.0, 0.5))
    b = block_bootstrap(blocks, a, s, ref=0, n_boot=500, seed=0)
    assert b["d_mae_hi"][1] < 0
    assert verdict(b["d_mae_lo"][1], b["d_mae_hi"][1]) == "better"


def test_interval_includes_zero_for_two_equivalent_models():
    rng = np.random.default_rng(1)
    n_blocks, per_block = 100, 20
    a = np.abs(rng.normal(size=(n_blocks * per_block, 2)))
    blocks = np.repeat(np.arange(n_blocks), per_block)
    b = block_bootstrap(blocks, a, a ** 2, ref=0, n_boot=500, seed=0)
    assert b["d_mae_lo"][1] < 0 < b["d_mae_hi"][1]
    assert verdict(b["d_mae_lo"][1], b["d_mae_hi"][1]) == "no clear difference"


def test_same_seed_same_interval_different_seed_different_interval():
    blocks, a, s = _errors(scale=(1.0, 1.3))
    b1 = block_bootstrap(blocks, a, s, ref=0, n_boot=300, seed=5)
    b2 = block_bootstrap(blocks, a, s, ref=0, n_boot=300, seed=5)
    b3 = block_bootstrap(blocks, a, s, ref=0, n_boot=300, seed=6)
    assert np.array_equal(b1["d_mae_lo"], b2["d_mae_lo"])
    assert not np.array_equal(b1["d_mae_lo"], b3["d_mae_lo"])


def test_block_resampling_is_wider_than_row_resampling_when_errors_cluster():
    rng = np.random.default_rng(3)
    n_blocks, per_block = 20, 50
    block_effect = np.repeat(rng.normal(loc=5, scale=2, size=n_blocks), per_block)
    a = np.abs(block_effect + rng.normal(scale=0.2, size=n_blocks * per_block))[:, None]
    by_block = np.repeat(np.arange(n_blocks), per_block)
    by_row = np.arange(n_blocks * per_block)
    wb = block_bootstrap(by_block, a, a ** 2, ref=0, n_boot=500, seed=0)
    wr = block_bootstrap(by_row, a, a ** 2, ref=0, n_boot=500, seed=0)
    assert (wb["mae_hi"][0] - wb["mae_lo"][0]) > 3 * (wr["mae_hi"][0] - wr["mae_lo"][0])


def test_every_leaky_scenario_has_a_valid_serving_substitute():
    leaky = {s for s, r in scenarios.EXPECTED_RULES.items() if r and s != "L10"}
    assert set(SERVING_SUBSTITUTE) == leaky
    valid_features = {f["name"] for f in load_registry()
                      if not scenarios.EXPECTED_RULES[f["scenario"]]}
    for scen, sub in SERVING_SUBSTITUTE.items():
        assert sub is None or sub == "last_fare_available" or sub in valid_features, scen


def test_leaky_model_collapses_when_the_leak_is_unavailable_at_serving_time():
    rng = np.random.default_rng(1)
    n = 800
    x1 = rng.normal(size=n)
    target = 3 * x1 + rng.normal(size=n)
    df = pd.DataFrame({"x1": x1, "leak": target + rng.normal(scale=0.05, size=n),
                       "target": target})
    train, test = df.iloc[:600], df.iloc[600:]
    y = test["target"].to_numpy()
    safe = make_model(ML, 0).fit(train[["x1"]], train["target"])
    leaky = make_model(ML, 0).fit(train[["x1", "leak"]], train["target"])
    mae_safe = np.abs(safe.predict(test[["x1"]]) - y).mean()
    mae_offline = np.abs(leaky.predict(test[["x1", "leak"]]) - y).mean()
    served = test.copy()
    served["leak"] = np.nan
    mae_prod = np.abs(leaky.predict(served[["x1", "leak"]]) - y).mean()
    assert mae_offline < 0.5 * mae_safe          # looks great offline
    assert mae_prod > mae_safe                   # but is worse than the honest model


def test_last_fare_available_uses_the_availability_clock():
    con = duckdb.connect()
    con.execute("""
        CREATE TABLE trip_times AS
        SELECT * FROM (VALUES
            (1, 1, TIMESTAMP '2024-01-10 10:00:00'),
            (2, 1, TIMESTAMP '2024-01-10 10:00:01'),
            (3, 1, TIMESTAMP '2024-01-10 09:00:00')
        ) t(trip_id, pu_zone, avail_fare)
    """)
    con.execute("""
        CREATE TABLE clean_trips AS
        SELECT * FROM (VALUES (1, 11.0), (2, 22.0), (3, 33.0)) t(trip_id, total_amount)
    """)
    con.execute("""
        CREATE TABLE row_features AS
        SELECT * FROM (VALUES
            (101, 1, TIMESTAMP '2024-01-10 10:00:00'),
            (102, 1, TIMESTAMP '2024-01-10 09:59:59'),
            (103, 1, TIMESTAMP '2024-01-10 08:00:00'),
            (104, 2, TIMESTAMP '2024-01-10 10:00:00')
        ) t(trip_id, pu_zone, t_pred)
    """)
    got = dict(con.execute(LAST_FARE_AVAILABLE_SQL).fetchall())
    con.close()
    assert got[101] == 11.0     # fare available at exactly t_pred is usable, +1 s is not
    assert got[102] == 33.0
    assert got[103] is None     # nothing available yet
    assert got[104] is None     # other zone