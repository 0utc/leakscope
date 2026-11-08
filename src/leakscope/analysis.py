"""Uncertainty, production-condition evaluation and ablations for the ML comparison.

1. Uncertainty: hour-block bootstrap over the test set. Rows in the same hour share
   traffic conditions, so whole hours are resampled together (resampling single rows
   would give intervals that are too narrow). Differences are PAIRED: both models are
   scored on the same resampled hours.
2. Production conditions: each leaky model is trained exactly as before, but at serving
   time the leaky value cannot be computed from what is available. It is replaced by
   what a point-in-time feature service would return: a missing value for the trip's own
   outcome fields, the correctly timed version of the feature otherwise.
3. Ablations: test two mechanism hypotheses recorded in the devlog.
The integrity check that the auditor agrees with the scenario labels is done by
leakscope.evaluation; run that first.
"""
import math
from pathlib import Path

import numpy as np
import pandas as pd

from leakscope.evaluation import (
    build_model_table, chronological_split, define_experiments, make_model,
)
from leakscope.ingestion import connect, load_config
from leakscope.registry import load_registry

# What a correct point-in-time feature service would return at prediction time.
# None = the value does not exist yet -> missing.
SERVING_SUBSTITUTE = {
    "L1": None, "L2": None, "L3": None, "L7": None,
    "L4": "zone_avg_dur_asof", "L8": "zone_avg_dur_asof",
    "L5": "zone_avg_dur_60m", "L6": "zone_avg_dur_60m",
    "L9": "last_fare_available",
}

# The correctly timed version of L9: latest zone trip whose FARE is available by t_pred.
LAST_FARE_AVAILABLE_SQL = """
    WITH src AS (
        SELECT tt.pu_zone, tt.avail_fare,
               arg_max(c.total_amount, tt.trip_id) AS value
        FROM trip_times tt JOIN clean_trips c USING (trip_id)
        GROUP BY tt.pu_zone, tt.avail_fare
    )
    SELECT ex.trip_id, s.value AS last_fare_available
    FROM row_features ex
    ASOF LEFT JOIN src s
      ON ex.pu_zone = s.pu_zone AND ex.t_pred >= s.avail_fare
"""

ABLATIONS = [
    dict(label="L5 without N5 (safe 60-min average removed)", main="L5",
         drop=["zone_avg_dur_60m"], add="leak_zone_avg_60m_self"),
    dict(label="L3 without pickup_hour", main="L3",
         drop=["pickup_hour"], add="leak_dropoff_hour"),
]


# --------------------------------------------------------------- bootstrap
def block_bootstrap(blocks, abs_err, sq_err, ref, n_boot, seed):
    """Paired block bootstrap.

    blocks  : block label per row (e.g. the hour of the prediction)
    abs_err, sq_err : (n_rows, n_models) arrays
    ref     : column index of the reference model
    Returns arrays with one entry per model: full-sample mae/rmse, full-sample
    difference to the reference, and 95% percentile intervals.
    """
    abs_err, sq_err = np.asarray(abs_err, float), np.asarray(sq_err, float)
    codes, uniques = pd.factorize(np.asarray(blocks))
    nb = len(uniques)
    n_b = np.bincount(codes, minlength=nb).astype(float)
    abs_b = pd.DataFrame(abs_err).groupby(codes).sum().to_numpy()
    sq_b = pd.DataFrame(sq_err).groupby(codes).sum().to_numpy()

    rng = np.random.default_rng(seed)
    counts = rng.multinomial(nb, np.full(nb, 1.0 / nb), size=n_boot).astype(float)
    tot = counts @ n_b
    mae_b = (counts @ abs_b) / tot[:, None]
    rmse_b = np.sqrt((counts @ sq_b) / tot[:, None])
    d_mae_b = mae_b - mae_b[:, [ref]]
    d_rmse_b = rmse_b - rmse_b[:, [ref]]

    def ci(x):
        return np.percentile(x, [2.5, 97.5], axis=0)

    mae_full, rmse_full = abs_err.mean(axis=0), np.sqrt(sq_err.mean(axis=0))
    out = dict(n_blocks=nb, mae=mae_full, rmse=rmse_full,
               d_mae=mae_full - mae_full[ref], d_rmse=rmse_full - rmse_full[ref])
    out["mae_lo"], out["mae_hi"] = ci(mae_b)
    out["d_mae_lo"], out["d_mae_hi"] = ci(d_mae_b)
    out["d_rmse_lo"], out["d_rmse_hi"] = ci(d_rmse_b)
    return out


def verdict(lo, hi):
    if lo > 0:
        return "worse"
    if hi < 0:
        return "better"
    return "no clear difference"


# -------------------------------------------------------------------- data
def load_data(cfg, feats):
    con = connect(cfg)
    try:
        df = build_model_table(con, feats)
        extra = con.execute(LAST_FARE_AVAILABLE_SQL).df()
    finally:
        con.close()
    df = df.merge(extra, on="trip_id", how="left")
    df["last_fare_available"] = df["last_fare_available"].astype("float64")
    return df


def _errors(pred, y):
    e = np.asarray(pred) - y
    return np.abs(e), e ** 2


# ------------------------------------------------------------------ runner
def run_analysis(config_path="configs/taxi.yaml", out_dir="results"):
    cfg = load_config(config_path)
    ml, seed = cfg["ml"], int(cfg["ml"]["seed"])
    n_boot, b_seed = int(cfg["analysis"]["n_boot"]), int(cfg["analysis"]["seed"])
    feats = load_registry()
    scen_of = {f["name"]: f["scenario"] for f in feats}
    df = load_data(cfg, feats)
    train, test, _ = chronological_split(df, ml["test_start"])
    y = test["target"].to_numpy()
    blocks = test["t_pred"].dt.floor("h").to_numpy()
    safe_cols = define_experiments(feats)[0]["cols"]

    # ---- main models: offline test score and production-condition score
    keys, abs_cols, sq_cols = [], [], []
    for exp in define_experiments(feats):
        model = make_model(ml, seed).fit(train[exp["cols"]], train["target"])
        variants = [("offline", model.predict(test[exp["cols"]]))]
        if exp["added"]:
            prod = test.copy()
            for name in exp["added"]:
                sub = SERVING_SUBSTITUTE[scen_of[name]]
                prod[name] = np.nan if sub is None else test[sub].to_numpy()
            variants.append(("production", model.predict(prod[exp["cols"]])))
        for kind, pred in variants:
            a, s = _errors(pred, y)
            keys.append((kind, exp["label"], exp["scenario"]))
            abs_cols.append(a)
            sq_cols.append(s)
    boot = block_bootstrap(blocks, np.column_stack(abs_cols), np.column_stack(sq_cols),
                           ref=0, n_boot=n_boot, seed=b_seed)

    # consistency with the table saved by leakscope.evaluation
    saved_path = Path(out_dir) / "ml_comparison.csv"
    consistent = None
    if saved_path.exists():
        saved = pd.read_csv(saved_path).set_index("experiment")["mae"]
        consistent = all(
            math.isclose(saved[label], boot["mae"][i], rel_tol=1e-9)
            for i, (kind, label, _) in enumerate(keys) if kind == "offline")

    def row(i):
        kind, label, scen = keys[i]
        return dict(kind=kind, experiment=label, scenario=scen,
                    mae=boot["mae"][i], mae_lo=boot["mae_lo"][i], mae_hi=boot["mae_hi"][i],
                    d_mae=boot["d_mae"][i], d_mae_lo=boot["d_mae_lo"][i],
                    d_mae_hi=boot["d_mae_hi"][i], d_rmse=boot["d_rmse"][i],
                    d_rmse_lo=boot["d_rmse_lo"][i], d_rmse_hi=boot["d_rmse_hi"][i],
                    mae_verdict=verdict(boot["d_mae_lo"][i], boot["d_mae_hi"][i]),
                    rmse_verdict=verdict(boot["d_rmse_lo"][i], boot["d_rmse_hi"][i]))

    offline = [row(i) for i, k in enumerate(keys) if k[0] == "offline"]
    production = [row(i) for i, k in enumerate(keys) if k[0] == "production"]
    off_mae = {r["scenario"]: r["mae"] for r in offline}
    for r in production:
        r["offline_mae"] = off_mae[r["scenario"]]

    # ---- ablations (each is a paired comparison with its own reference model)
    main_pct = {}
    ref_mae = offline[0]["mae"]
    for r in offline[1:]:
        main_pct[r["scenario"]] = 100 * (r["mae"] - ref_mae) / ref_mae
    ablation = []
    for ab in ABLATIONS:
        ref_cols = [c for c in safe_cols if c not in ab["drop"]]
        cols_list = [ref_cols, ref_cols + [ab["add"]]]
        a_cols, s_cols = [], []
        for cols in cols_list:
            m = make_model(ml, seed).fit(train[cols], train["target"])
            a, s = _errors(m.predict(test[cols]), y)
            a_cols.append(a)
            s_cols.append(s)
        b = block_bootstrap(blocks, np.column_stack(a_cols), np.column_stack(s_cols),
                            ref=0, n_boot=n_boot, seed=b_seed)
        ablation.append(dict(
            ablation=ab["label"], scenario=ab["main"],
            mae_without_leak=b["mae"][0], mae_with_leak=b["mae"][1],
            d_mae=b["d_mae"][1], d_mae_lo=b["d_mae_lo"][1], d_mae_hi=b["d_mae_hi"][1],
            d_mae_pct=100 * b["d_mae"][1] / b["mae"][0],
            main_d_mae_pct=main_pct[ab["main"]]))

    out = Path(out_dir)
    out.mkdir(exist_ok=True)
    pd.DataFrame(offline).to_csv(out / "ml_uncertainty.csv", index=False)
    pd.DataFrame(production).to_csv(out / "ml_production.csv", index=False)
    pd.DataFrame(ablation).to_csv(out / "ml_ablation.csv", index=False)
    print_analysis(boot["n_blocks"], n_boot, consistent, offline, production, ablation)
    return offline, production, ablation


def _ci(d, lo, hi):
    return f"{d:+.3f} [{lo:+.3f}, {hi:+.3f}]"


def print_analysis(n_blocks, n_boot, consistent, offline, production, ablation):
    print(f"Hour-block bootstrap: {n_blocks} blocks, {n_boot} resamples, 95% percentile intervals")
    if consistent is not None:
        print(f"Offline MAEs reproduce results/ml_comparison.csv: {'yes' if consistent else 'NO'}")

    print("\n1) OFFLINE (leaky test set): difference vs the corrected model, in minutes")
    print(f"{'experiment':40} {'MAE [95% CI]':24} {'dMAE [95% CI]':26} "
          f"{'dRMSE [95% CI]':26} {'MAE':>5} {'RMSE':>5}")
    print("-" * 134)
    for i, r in enumerate(offline):
        mae_txt = f"{r['mae']:.3f} [{r['mae_lo']:.3f}, {r['mae_hi']:.3f}]"
        if i == 0:
            d1 = d2 = "(reference)"
            v1 = v2 = "-"
        else:
            d1 = _ci(r["d_mae"], r["d_mae_lo"], r["d_mae_hi"])
            d2 = _ci(r["d_rmse"], r["d_rmse_lo"], r["d_rmse_hi"])
            v1, v2 = r["mae_verdict"], r["rmse_verdict"]
        flag = lambda v: {"better": "less", "worse": "more", "no clear difference": "n.s.",
                          "-": "-"}[v]
        print(f"{r['experiment']:40} {mae_txt:24} {d1:26} {d2:26} {flag(v1):>5} {flag(v2):>5}")
    print("less/more = error clearly lower/higher than the corrected model; n.s. = interval includes 0")

    print("\n2) PRODUCTION CONDITIONS: leaky value replaced by what is available at prediction time")
    print(f"{'experiment':40} {'offline MAE':>11} {'production MAE':>14} "
          f"{'production - corrected [95% CI]':32} verdict")
    print("-" * 118)
    for r in production:
        print(f"{r['experiment']:40} {r['offline_mae']:11.3f} {r['mae']:14.3f} "
              f"{_ci(r['d_mae'], r['d_mae_lo'], r['d_mae_hi']):32} {r['mae_verdict']}")
    print(f"(corrected model: MAE {offline[0]['mae']:.3f}; 'worse' = leaky model clearly "
          f"worse than the corrected model once the leak is gone)")

    print("\n3) ABLATIONS")
    print(f"{'ablation':50} {'MAE without':>11} {'MAE with':>9} {'dMAE [95% CI]':26} "
          f"{'dMAE%':>7} {'main dMAE%':>10}")
    print("-" * 120)
    for a in ablation:
        print(f"{a['ablation']:50} {a['mae_without_leak']:11.3f} {a['mae_with_leak']:9.3f} "
              f"{_ci(a['d_mae'], a['d_mae_lo'], a['d_mae_hi']):26} "
              f"{a['d_mae_pct']:+7.1f} {a['main_d_mae_pct']:+10.1f}")
    print("main dMAE% = the same leak's effect in the full experiment (previous table)")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Uncertainty, production and ablations")
    parser.add_argument("--config", default="configs/taxi.yaml")
    run_analysis(parser.parse_args().config)