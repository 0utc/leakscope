"""Controlled ML comparison: corrected vs leaky feature sets.

Same algorithm, same chronological split, same seed. Only the feature set
differs. The split is a purged chronological split:
  train : examples whose LABEL was already available at the cutoff
          (avail_dropoff <= test_start), so no training label comes from the future
  test  : examples predicted at or after the cutoff (t_pred >= test_start)
  purged: examples predicted before the cutoff whose trip was still in progress
          at the cutoff (label not yet known) -> excluded from both sides
"""
import json
import os
import platform
import time
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd
import sklearn
from sklearn.ensemble import HistGradientBoostingRegressor

from leakscope import scenarios
from leakscope.audit_scoring import evaluate_features
from leakscope.ingestion import connect, load_config
from leakscope.registry import load_registry

SOURCE_ALIAS = {"row_features": "r", "pit_features": "p", "scenario_features": "s"}


# ------------------------------------------------------------------- data
def feature_locations(con, names):
    """Which source table holds each feature's column."""
    loc = {}
    for table in SOURCE_ALIAS:
        cols = {row[0] for row in con.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name = ?",
            [table]).fetchall()}
        for n in names:
            if n in cols and n not in loc:
                loc[n] = table
    missing = [n for n in names if n not in loc]
    if missing:
        raise ValueError(f"features not found in any source table: {missing}")
    return loc


def build_model_table(con, features):
    """One row per prediction example: target, label availability, all features."""
    names = [f["name"] for f in features]
    loc = feature_locations(con, names)
    cols = ", ".join(f"{SOURCE_ALIAS[loc[n]]}.{n} AS {n}" for n in names)
    df = con.execute(f"""
        SELECT r.trip_id, r.t_pred, r.target_duration_min AS target,
               tt.avail_dropoff AS label_available_at, {cols}
        FROM row_features r
        JOIN pit_features p ON p.trip_id = r.trip_id
        JOIN scenario_features s ON s.trip_id = r.trip_id
        JOIN trip_times tt ON tt.trip_id = r.trip_id
        ORDER BY r.t_pred, r.trip_id
    """).df()
    df[names] = df[names].astype("float64")
    return df


def chronological_split(df, test_start):
    """Purged chronological split. Returns (train, test, n_purged)."""
    cutoff = pd.Timestamp(test_start)
    train = df[df["label_available_at"] <= cutoff]
    test = df[df["t_pred"] >= cutoff]
    return train, test, len(df) - len(train) - len(test)


# ------------------------------------------------------------ experiments
def scenario_order(sid):
    return (sid[0] == "N", int(sid[1:]))


def define_experiments(features):
    """Corrected model = every valid feature; each leaky model adds ONE leaky
    feature; the last one adds all of them. Valid/leaky comes from the labelled
    ground truth (scenarios.EXPECTED_RULES)."""
    safe = [f["name"] for f in features if not scenarios.EXPECTED_RULES[f["scenario"]]]
    leaky = sorted((f for f in features if scenarios.EXPECTED_RULES[f["scenario"]]),
                   key=lambda f: scenario_order(f["scenario"]))
    exps = [dict(label="corrected (valid features only)", scenario="-",
                 cols=list(safe), added=[])]
    for f in leaky:
        exps.append(dict(label=f"+ {f['scenario']} {f['name']}", scenario=f["scenario"],
                         cols=safe + [f["name"]], added=[f["name"]]))
    exps.append(dict(label="+ ALL leaky features", scenario="ALL",
                     cols=safe + [f["name"] for f in leaky],
                     added=[f["name"] for f in leaky]))
    return exps


def mae(y, p):
    return float(np.mean(np.abs(np.asarray(y) - np.asarray(p))))


def rmse(y, p):
    return float(np.sqrt(np.mean((np.asarray(y) - np.asarray(p)) ** 2)))


def make_model(ml, seed):
    m = ml["model"]
    return HistGradientBoostingRegressor(
        max_iter=int(m["max_iter"]), learning_rate=float(m["learning_rate"]),
        max_leaf_nodes=int(m["max_leaf_nodes"]),
        early_stopping=False,          # no random validation split inside training
        random_state=seed)


def fit_and_score(train, test, cols, ml, seed):
    start = time.perf_counter()
    model = make_model(ml, seed).fit(train[cols], train["target"])
    pred = model.predict(test[cols])
    y = test["target"].to_numpy()
    return dict(mae=mae(y, pred), rmse=rmse(y, pred),
                fit_s=time.perf_counter() - start)


# ------------------------------------------------------------------ runner
def run_experiments(config_path="configs/taxi.yaml", out_dir="results"):
    cfg = load_config(config_path)
    ml, seed = cfg["ml"], int(cfg["ml"]["seed"])
    feats = load_registry()
    con = connect(cfg)
    try:
        df = build_model_table(con, feats)
        audit = {r["feature"]: r for r in evaluate_features(con, feats)}
    finally:
        con.close()

    # integrity: the labels that define the experiments must agree with the auditor
    for f in feats:
        a = audit[f["name"]]
        if a["expected"] != a["fired"]:
            raise RuntimeError(
                f"auditor disagrees with the label of {f['name']} "
                f"(expected {sorted(a['expected'])}, fired {sorted(a['fired'])}); "
                f"run python -m leakscope.audit_scoring")

    train, test, n_purged = chronological_split(df, ml["test_start"])
    y_test = test["target"].to_numpy()
    median = float(train["target"].median())
    base = dict(mae=mae(y_test, np.full(len(y_test), median)),
                rmse=rmse(y_test, np.full(len(y_test), median)))

    n_examples = len(df)
    rows, ref = [], None
    for exp in define_experiments(feats):
        s = fit_and_score(train, test, exp["cols"], ml, seed)
        ref = ref or s                      # the first experiment is the corrected model
        flagged = (audit[exp["added"][0]]["n_flagged"] / n_examples * 100
                   if len(exp["added"]) == 1 else None)
        rows.append(dict(
            experiment=exp["label"], scenario=exp["scenario"],
            n_features=len(exp["cols"]), mae=s["mae"], rmse=s["rmse"],
            d_mae_pct=100 * (s["mae"] - ref["mae"]) / ref["mae"],
            d_rmse_pct=100 * (s["rmse"] - ref["rmse"]) / ref["rmse"],
            pct_rows_flagged=flagged, fit_s=s["fit_s"]))

    info = dict(
        test_start=str(pd.Timestamp(ml["test_start"])), n_total=n_examples,
        n_train=len(train), n_test=len(test), n_purged=n_purged,
        train_median_min=median, baseline_mae=base["mae"], baseline_rmse=base["rmse"],
        seed=seed, model="HistGradientBoostingRegressor", model_params=ml["model"],
        python=platform.python_version(), sklearn=sklearn.__version__,
        duckdb=duckdb.__version__, platform=platform.platform(),
        cpu_count=os.cpu_count())
    out = Path(out_dir)
    out.mkdir(exist_ok=True)
    pd.DataFrame(rows).to_csv(out / "ml_comparison.csv", index=False)
    (out / "ml_run_info.json").write_text(json.dumps(info, indent=2), encoding="utf-8")
    print_results(info, rows)
    return rows, info


def print_results(info, rows):
    print(f"Chronological split at {info['test_start']}: train {info['n_train']:,} | "
          f"test {info['n_test']:,} | purged {info['n_purged']:,} "
          f"(trips still in progress at the cutoff)")
    print(f"Baseline, constant train median ({info['train_median_min']:.2f} min): "
          f"MAE {info['baseline_mae']:.3f}  RMSE {info['baseline_rmse']:.3f}\n")
    print(f"{'experiment':40} {'scen':4} {'MAE':>7} {'RMSE':>7} {'dMAE%':>8} "
          f"{'dRMSE%':>8} {'rows flagged':>13} {'fit s':>6}")
    print("-" * 100)
    for r in rows:
        flagged = "-" if r["pct_rows_flagged"] is None else f"{r['pct_rows_flagged']:.1f}%"
        print(f"{r['experiment']:40} {r['scenario']:4} {r['mae']:7.3f} {r['rmse']:7.3f} "
              f"{r['d_mae_pct']:+8.1f} {r['d_rmse_pct']:+8.1f} {flagged:>13} "
              f"{r['fit_s']:6.1f}")
    print("\nd% columns: change vs the corrected model (negative = lower error on this test set)")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Leaky vs corrected ML comparison")
    parser.add_argument("--config", default="configs/taxi.yaml")
    run_experiments(parser.parse_args().config)