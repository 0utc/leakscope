"""Tests for the float-noise sensitivity study."""
import contextlib
import io

import numpy as np
import pandas as pd
import pytest

from leakscope.pipeline import run_all
from leakscope.registry import load_registry
from leakscope.sensitivity import SUM_BASED, perturb, run_sensitivity, summarize_runs
from leakscope.synthetic import generate, make_demo_config


def _frame():
    return pd.DataFrame({"a": [10.0, 20.0, np.nan, 40.0], "b": [1.0, 2.0, 3.0, 4.0],
                         "keep": [5.0, 6.0, 7.0, 8.0]})


def test_perturbation_is_tiny_and_touches_only_the_chosen_columns():
    df = _frame()
    out = perturb(df, ["a", "b"], 1e-13, seed=1)
    assert out["keep"].equals(df["keep"])                       # other columns untouched
    assert np.isnan(out["a"].iloc[2])                           # missing values stay missing
    rel = (out[["a", "b"]] / df[["a", "b"]] - 1).abs().to_numpy()
    assert np.nanmax(rel) < 1e-11 and np.nanmax(rel) > 0       # tiny, but not zero
    assert df["a"].iloc[0] == 10.0                              # the input is not modified


def test_perturbation_is_deterministic_per_seed():
    a = perturb(_frame(), ["b"], 1e-13, seed=3)
    b = perturb(_frame(), ["b"], 1e-13, seed=3)
    c = perturb(_frame(), ["b"], 1e-13, seed=4)
    assert a["b"].equals(b["b"]) and not a["b"].equals(c["b"])


def test_every_registry_aggregate_is_covered():
    aggregates = {f["name"] for f in load_registry() if f["kind"] == "aggregate"}
    assert aggregates <= set(SUM_BASED)


def test_summary_statistics_are_paired_within_runs():
    runs = np.array([[6.0, 4.0], [6.2, 4.1], [5.8, 4.3]])      # column 0 = corrected model
    rows = summarize_runs(runs, reference=[6.0, 4.0], labels=["corrected", "leaky"],
                          scenarios_=["-", "L1"])
    leaky = rows[1]
    assert leaky["mean_mae"] == pytest.approx(np.mean([4.0, 4.1, 4.3]))
    assert leaky["std_mae"] == pytest.approx(np.std([4.0, 4.1, 4.3], ddof=1))
    assert leaky["d_mean"] == pytest.approx(np.mean([-2.0, -2.1, -1.5]))
    assert leaky["runs_lower"] == 3 and leaky["n_runs"] == 3
    assert rows[0]["d_mean"] == 0 and rows[0]["d_std"] == 0


def test_study_runs_end_to_end_on_a_demo_database(tmp_path):
    generate(tmp_path / "w", n_trips=10_000)
    cfg = make_demo_config(tmp_path / "w", "configs/taxi.yaml", n_examples=2500, n_boot=50)
    with contextlib.redirect_stdout(io.StringIO()):
        run_all(str(cfg))
        rows = run_sensitivity(str(cfg), str(tmp_path / "out"), n_runs=3, rel=1e-13)
    assert len(rows) == 11 and all(r["n_runs"] == 3 for r in rows)
    assert all(r["std_mae"] >= 0 and r["min_mae"] <= r["max_mae"] for r in rows)
    assert (tmp_path / "out" / "ml_sensitivity.csv").exists()