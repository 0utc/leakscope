"""Sensitivity of the model scores to float-level noise in the aggregated features.

Rebuilding the database used to change sum-based features at the 1e-13 level and
moved offline MAEs by up to 0.016 minutes (see the devlog). The build is now
deterministic, but the same kind of noise could come from another platform or
library version. Here tiny multiplicative noise is added to the sum-based features,
every model is refitted, and the spread of its score is reported: a floor below
which differences between models are not interpretable.
"""
from pathlib import Path

import numpy as np
import pandas as pd

from leakscope.evaluation import (
    build_model_table, chronological_split, define_experiments, fit_and_score,
)
from leakscope.ingestion import connect, load_config
from leakscope.registry import load_registry

# features computed from sums of durations (the ones affected by the old defect);
# leak_snapshot_zone_avg is a row feature but is read from the same running totals
SUM_BASED = ["zone_avg_dur_asof", "zone_avg_dur_7d", "zone_avg_dur_60m",
             "leak_zone_avg_global", "leak_zone_avg_60m_self",
             "leak_zone_avg_60m_inprogress", "leak_snapshot_zone_avg"]


def perturb(df, columns, rel, seed):
    """Multiply the given columns by (1 + rel * N(0, 1)); other columns and NaNs are kept."""
    rng = np.random.default_rng(seed)
    out = df.copy()
    for col in columns:
        out[col] = out[col] * (1.0 + rel * rng.standard_normal(len(out)))
    return out


def summarize_runs(mae_runs, reference, labels, scenarios_):
    """One row per experiment from an (n_runs, n_experiments) array of MAEs.

    Column 0 must be the corrected model; differences are paired within a run."""
    mae_runs = np.asarray(mae_runs, float)
    n_runs = mae_runs.shape[0]
    diffs = mae_runs - mae_runs[:, [0]]
    rows = []
    for j, (label, scen) in enumerate(zip(labels, scenarios_)):
        col, d = mae_runs[:, j], diffs[:, j]
        rows.append(dict(
            experiment=label, scenario=scen, reference_mae=float(reference[j]),
            mean_mae=float(col.mean()), std_mae=float(col.std(ddof=1)) if n_runs > 1 else 0.0,
            min_mae=float(col.min()), max_mae=float(col.max()),
            d_mean=float(d.mean()), d_std=float(d.std(ddof=1)) if n_runs > 1 else 0.0,
            d_min=float(d.min()), d_max=float(d.max()),
            runs_lower=int((d < 0).sum()), n_runs=n_runs))
    return rows


def run_sensitivity(config_path="configs/taxi.yaml", out_dir="results", n_runs=20,
                    rel=1e-13):
    cfg = load_config(config_path)
    ml, seed = cfg["ml"], int(cfg["ml"]["seed"])
    feats = load_registry()
    con = connect(cfg)
    try:
        df = build_model_table(con, feats)
    finally:
        con.close()
    columns = [c for c in SUM_BASED if c in df.columns]
    exps = define_experiments(feats)
    labels = [e["label"] for e in exps]
    scens = [e["scenario"] for e in exps]

    def score_all(frame):
        train, test, _ = chronological_split(frame, ml["test_start"])
        return [fit_and_score(train, test, e["cols"], ml, seed)["mae"] for e in exps]

    reference = score_all(df)
    runs = np.array([score_all(perturb(df, columns, rel, seed=1000 + k))
                     for k in range(n_runs)])
    rows = summarize_runs(runs, reference, labels, scens)
    for r in rows:
        r["relative_noise"] = rel

    out = Path(out_dir)
    out.mkdir(exist_ok=True)
    pd.DataFrame(rows).to_csv(out / "ml_sensitivity.csv", index=False)
    print_sensitivity(rows, columns, rel, n_runs)
    return rows


def print_sensitivity(rows, columns, rel, n_runs):
    print(f"Perturbation: relative noise {rel:g} on {len(columns)} sum-based features; "
          f"{n_runs} refits of every model (model seed fixed)\n")
    print(f"{'experiment':40} {'reference':>9} {'mean':>7} {'std':>7} {'min':>7} {'max':>7}"
          f"   {'dMAE vs corrected: mean':>23} {'std':>6} {'runs lower':>10}")
    print("-" * 126)
    for i, r in enumerate(rows):
        if i == 0:
            tail = f"{'(reference)':>23} {'':>6} {'':>10}"
        else:
            tail = (f"{r['d_mean']:+23.4f} {r['d_std']:6.4f} "
                    f"{r['runs_lower']:>5}/{r['n_runs']:<4}")
        print(f"{r['experiment']:40} {r['reference_mae']:9.3f} {r['mean_mae']:7.3f} "
              f"{r['std_mae']:7.4f} {r['min_mae']:7.3f} {r['max_mae']:7.3f}   {tail}")
    widest = max(rows, key=lambda r: r["max_mae"] - r["min_mae"])
    print(f"\nLargest spread of one model's MAE across runs: {widest['max_mae'] - widest['min_mae']:.4f} "
          f"min ({widest['experiment'].strip()}). Differences between models of that order "
          f"are not interpretable.")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Score sensitivity to float-level noise")
    parser.add_argument("--config", default="configs/taxi.yaml")
    parser.add_argument("--runs", type=int, default=20)
    parser.add_argument("--rel", type=float, default=1e-13)
    args = parser.parse_args()
    run_sensitivity(args.config, "results", args.runs, args.rel)