"""Generate the audit + experiment report from saved results.

Outputs (no timestamps, so files only change when results change):
  results/report.md          human-readable report (renders on GitHub)
  results/audit_report.json  machine-readable audit results
  assets/fig_offline_vs_production.png, assets/fig_flagged_vs_effect.png
Inputs: the DuckDB database (audit) and the CSV/JSON files written by
leakscope.evaluation and leakscope.analysis.
"""
import json
import os
from pathlib import Path

import matplotlib

matplotlib.use("Agg")  # no window needed; must be set before pyplot is imported
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from leakscope import scenarios  # noqa: E402
from leakscope.audit_scoring import collect, prf, score  # noqa: E402

ML_FILES = ["ml_comparison.csv", "ml_uncertainty.csv", "ml_production.csv",
            "ml_ablation.csv", "ml_run_info.json"]


# ------------------------------------------------------------------ inputs
def load_ml_results(results_dir="results"):
    d = Path(results_dir)
    missing = [n for n in ML_FILES if not (d / n).exists()]
    if missing:
        raise FileNotFoundError(
            f"missing {missing} in {d}; run python -m leakscope.evaluation and "
            f"python -m leakscope.analysis first")
    return dict(
        comparison=pd.read_csv(d / "ml_comparison.csv"),
        uncertainty=pd.read_csv(d / "ml_uncertainty.csv"),
        production=pd.read_csv(d / "ml_production.csv"),
        ablation=pd.read_csv(d / "ml_ablation.csv"),
        info=json.loads((d / "ml_run_info.json").read_text(encoding="utf-8")))


def _scenario_order(row):
    s = row["scenario"]
    return (s[0] == "N", int(s[1:]))


def collect_audit(config_path="configs/taxi.yaml"):
    rows, conformance, limitation = collect(config_path)
    rows = sorted(rows, key=_scenario_order)
    findings, mismatches = limitation
    return dict(
        rows=rows, score=score(rows),
        conformance={k: len(v) for k, v in conformance.items()},
        limitation=dict(rules_fired=sorted({x.rule for x in findings}),
                        conformance_mismatches=len(mismatches)))


def audit_to_json(audit):
    rows = []
    for r in audit["rows"]:
        rows.append(dict(
            scenario=r["scenario"], feature=r["feature"],
            expected_rules=sorted(r["expected"]), fired_rules=sorted(r["fired"]),
            matches=r["expected"] == r["fired"],
            n_flagged=r["n_flagged"], n_examples=r["n_examples"],
            findings=[dict(rule=f.rule, n_violations=f.n_violations,
                           n_checked=f.n_checked, pct=round(f.pct, 3),
                           sample_trip_ids=list(f.affected_trip_ids),
                           explanation=f.explanation) for f in r["findings"]]))
    return dict(scenarios=rows, metrics=audit["score"],
                conformance_mismatches=audit["conformance"],
                known_limitation_M1=audit["limitation"])


# ----------------------------------------------------------------- figures
def _label(scenario):
    return "ALL leaky features" if scenario == "ALL" else \
        f"{scenario} {scenarios.SCENARIO_NAMES[scenario]}"


def make_figures(ml, assets_dir="assets"):
    out = Path(assets_dir)
    out.mkdir(parents=True, exist_ok=True)
    unc, prod, info = ml["uncertainty"], ml["production"], ml["info"]
    offline = unc[unc["kind"] == "offline"]
    corrected = float(offline.iloc[0]["mae"])
    off = offline.iloc[1:].reset_index(drop=True)
    prod = prod.set_index("experiment").loc[off["experiment"]].reset_index()

    # Figure 1: offline vs production error, with intervals
    y, h = np.arange(len(off)), 0.38
    fig, ax = plt.subplots(figsize=(9, 6.4))
    for pos, df, name in ((y - h / 2, off, "offline test set (leak present)"),
                          (y + h / 2, prod, "production conditions (leak unavailable)")):
        ax.barh(pos, df["mae"], h, label=name, capsize=2,
                xerr=[df["mae"] - df["mae_lo"], df["mae_hi"] - df["mae"]])
    ax.axvline(corrected, ls="--", color="black", lw=1,
               label=f"corrected model ({corrected:.2f} min)")
    ax.axvline(info["baseline_mae"], ls=":", color="gray", lw=1.4,
               label=f"constant-median baseline ({info['baseline_mae']:.2f} min)")
    ax.set_yticks(y)
    ax.set_yticklabels([_label(s) for s in off["scenario"]])
    ax.invert_yaxis()
    ax.set_xlabel("MAE on the test week (minutes); bars show 95% intervals (hour-block bootstrap)")
    ax.set_title("Error of each model: leak present (offline) vs leak unavailable (production)",
                 fontsize=10)
    ax.legend(loc="upper center", bbox_to_anchor=(0.5, -0.13), ncol=2, fontsize=8,
              frameon=False)
    fig.tight_layout()
    f1 = out / "fig_offline_vs_production.png"
    fig.savefig(f1, dpi=150)
    plt.close(fig)

    # Figure 2: share of invalid rows vs effect on the score
    cmp_ = ml["comparison"]
    pts = cmp_[cmp_["pct_rows_flagged"].notna()].reset_index(drop=True)
    x = pts["pct_rows_flagged"].to_numpy(float)
    yv = pts["d_mae_pct"].to_numpy(float)
    fig, ax = plt.subplots(figsize=(7, 4.8))
    ax.scatter(x, yv, s=40, zorder=3)
    ax.axhline(0, color="gray", lw=0.8)
    span = max(max(yv.max(), 0) - min(yv.min(), 0), 1.0)
    ax.set_ylim(min(yv.min(), 0) - 0.08 * span, max(yv.max(), 0) + 0.08 * span)
    ax.set_xlim(x.min() - 4, 101)
    ax.set_xticks([t for t in ax.get_xticks() if t <= 100])
    ax.set_xlabel("% of examples flagged by the auditor")
    ax.set_ylabel("change in offline MAE vs corrected model (%)\n(negative = lower error)")
    ax.set_title("Share of flagged rows vs effect of the leak on the offline score",
                 fontsize=10)
    fig.tight_layout()

    # Labels for the crowded right-hand cluster are stacked to the left of the points
    # (at least 14 px apart) and joined by thin lines; the others sit beside their point.
    pix = ax.transData.transform(np.column_stack([x, yv]))
    to_pt = 72.0 / fig.dpi
    right = sorted((i for i in range(len(x)) if x[i] >= x.max() - 5),
                   key=lambda i: -pix[i, 1])
    offs, last = {}, None
    for i in right:
        target = pix[i, 1]
        if last is not None and last - target < 14:
            target = last - 14
        offs[i] = (target - pix[i, 1]) * to_pt
        last = target
    for i, r in pts.iterrows():
        pct = round(float(r["d_mae_pct"]), 1) + 0.0     # avoids printing "-0.0"
        tag = f"{r['scenario']} ({pct:+.1f}%)"
        if i in offs:
            ax.annotate(tag, (x[i], yv[i]), xytext=(-18, offs[i]),
                        textcoords="offset points", ha="right", va="center", fontsize=9,
                        arrowprops=dict(arrowstyle="-", lw=0.5, color="gray"))
        else:
            ax.annotate(tag, (x[i], yv[i]), xytext=(6, 5),
                        textcoords="offset points", fontsize=9)
    f2 = out / "fig_flagged_vs_effect.png"
    fig.savefig(f2, dpi=150)
    plt.close(fig)
    return dict(offline_vs_production=f1.name, flagged_vs_effect=f2.name,
                assets_dir=str(out))


# ---------------------------------------------------------------- markdown
def md_table(headers, rows):
    lines = ["| " + " | ".join(headers) + " |",
             "|" + "|".join("---" for _ in headers) + "|"]
    lines += ["| " + " | ".join(str(c) for c in r) + " |" for r in rows]
    return "\n".join(lines)


def _ci(d, lo, hi):
    return f"{d:+.3f} [{lo:+.3f}, {hi:+.3f}]"


def _fmt(x):
    return "n/a" if x is None else f"{x:.3f}"


OFFLINE_WORDS = {"better": "clearly lower", "worse": "clearly higher",
                 "no clear difference": "not distinguishable from 0"}


def render_markdown(audit, ml, figures, results_dir="results"):
    s, rows = audit["score"], audit["rows"]
    fc, rc = s["feature"], s["rule"]
    fp_, fr_ = prf(fc["tp"], fc["fp"], fc["fn"])
    rp_, rr_ = prf(rc["tp"], rc["fp"], rc["fn"])
    n_leaky, n_valid = fc["tp"] + fc["fn"], fc["tn"] + fc["fp"]
    info = ml["info"]
    unc, prod, abl, cmp_ = (ml["uncertainty"], ml["production"], ml["ablation"],
                            ml["comparison"])
    offline = unc[unc["kind"] == "offline"].reset_index(drop=True)
    corrected = offline.iloc[0]
    all_off = offline[offline["scenario"] == "ALL"].iloc[0]
    all_prod = prod[prod["scenario"] == "ALL"].iloc[0]
    flagged = cmp_.set_index("experiment")["pct_rows_flagged"]

    def img(name):
        rel = os.path.relpath(Path(figures["assets_dir"]) / name, results_dir)
        return f"![{name}]({Path(rel).as_posix()})"

    out = []
    out.append("# LeakScope report\n")
    out.append("*Generated by `python -m leakscope.reporting` from the database and the "
               "files in `results/`. No timestamps are embedded, so this file only changes "
               "when the results change.*\n")

    out.append("## Summary\n")
    out.append(
        f"- **Audit:** {fc['tp']} of {n_leaky} leaky features were flagged and {fc['tn']} "
        f"of {n_valid} valid features were left alone (feature level: precision "
        f"{_fmt(fp_)}, recall {_fmt(fr_)}; rule level: precision {_fmt(rp_)}, recall "
        f"{_fmt(rr_)}). The scenarios were designed together with the auditor, so this "
        f"shows that the implementation matches its specification, not that all leakage "
        f"is caught (see section 3).")
    out.append(
        f"- **Honest model:** MAE {corrected['mae']:.3f} min on the test week, against "
        f"{info['baseline_mae']:.3f} for a constant-median baseline.")
    out.append(
        f"- **Offline vs production:** with all leaky features the offline MAE is "
        f"{all_off['mae']:.3f} min ({100 * (all_off['mae'] - corrected['mae']) / corrected['mae']:+.1f}% "
        f"vs the honest model). When the leaked values are not available at prediction "
        f"time the same model scores {all_prod['mae']:.3f} min "
        f"({100 * (all_prod['mae'] - corrected['mae']) / corrected['mae']:+.1f}% vs the honest model).\n")
    out.append(img(figures["offline_vs_production"]) + "\n")
    out.append("*Bars: MAE on the test week with 95% intervals (hour-block bootstrap). "
               "Dashed line: the corrected model. Dotted line: the constant-median "
               "baseline. The lines are single values without intervals, and the bar "
               "intervals describe each model on its own: to compare two models use the "
               "paired differences in the tables below, which are tighter than "
               "overlapping bars suggest.*\n")

    out.append("## 1. Audit scorecard\n")
    out.append(md_table(
        ["Scenario", "What it tests", "Feature", "Expected rules", "Fired rules", "OK",
         "Rows flagged"],
        [[r["scenario"], scenarios.SCENARIO_NAMES[r["scenario"]], f"`{r['feature']}`",
          ", ".join(sorted(r["expected"])) or "-", ", ".join(sorted(r["fired"])) or "-",
          "yes" if r["expected"] == r["fired"] else "**NO**",
          f"{r['n_flagged']:,} / {r['n_examples']:,}"] for r in rows]) + "\n")
    out.append(
        f"{s['n']} features evaluated ({n_leaky} leaky, {n_valid} valid); exact rule-set "
        f"match: {s['exact']}/{s['n']}.\n")
    out.append(md_table(
        ["Level", "TP", "FP", "FN", "TN", "Precision", "Recall"],
        [["Feature", fc["tp"], fc["fp"], fc["fn"], fc["tn"], _fmt(fp_), _fmt(fr_)],
         ["Rule", rc["tp"], rc["fp"], rc["fn"], rc["tn"], _fmt(rp_), _fmt(rr_)]]) + "\n")

    out.append("### Findings\n")
    for r in rows:
        for f in r["findings"]:
            ids = ", ".join(str(i) for i in f.affected_trip_ids)
            out.append(f"- **[{f.rule}] `{r['feature']}`** ({r['scenario']}): "
                       f"{f.explanation} First affected trip IDs: {ids}.")
    out.append("")

    lim = audit["limitation"]
    out.append("### Conformance and a known limitation\n")
    out.append(
        "Stored aggregate values were recomputed from their declared definitions "
        "(30 examples each):\n")
    out.append(md_table(["Feature", "Mismatches"],
                        [[f"`{k}`", v] for k, v in audit["conformance"].items()]) + "\n")
    out.append(
        f"Known limitation M1 (an aggregate whose declaration is false): rules R1 to R4 "
        f"fired: {', '.join(lim['rules_fired']) or 'none (missed)'}; the conformance "
        f"check found {lim['conformance_mismatches']} mismatching examples.\n")

    out.append("## 2. ML experiment\n")
    out.append(
        f"Same algorithm ({info['model']}), same seed ({info['seed']}), purged "
        f"chronological split at {info['test_start']}: train {info['n_train']:,}, test "
        f"{info['n_test']:,}, purged {info['n_purged']:,} (trips still in progress at the "
        f"cutoff). The corrected model uses only valid features; each leaky model adds one "
        f"leaky feature; the last adds all of them. Errors are in minutes.\n")
    out.append("### 2.1 Offline (the test set contains the leak too)\n")
    body = []
    for i, r in offline.iterrows():
        if i == 0:
            d1 = d2 = "(reference)"
            verdict = "-"
        else:
            d1 = _ci(r["d_mae"], r["d_mae_lo"], r["d_mae_hi"])
            d2 = _ci(r["d_rmse"], r["d_rmse_lo"], r["d_rmse_hi"])
            verdict = OFFLINE_WORDS[r["mae_verdict"]]
        fl = flagged.get(r["experiment"])
        body.append([r["experiment"], f"{r['mae']:.3f} [{r['mae_lo']:.3f}, {r['mae_hi']:.3f}]",
                     d1, d2, verdict,
                     "-" if fl is None or pd.isna(fl) else f"{fl:.1f}%"])
    out.append(md_table(["Experiment", "MAE [95% CI]", "dMAE vs corrected", "dRMSE vs corrected",
                         "MAE difference", "Rows flagged"], body) + "\n")
    out.append("Intervals: 95% percentile, paired hour-block bootstrap (resampling whole "
               "hours of the test week).\n")
    out.append(img(figures["flagged_vs_effect"]) + "\n")
    out.append("*Each point is one leaky feature (scenario ID). Horizontal axis: share of "
               "examples where the auditor flagged it. Vertical axis: change in offline "
               "MAE vs the corrected model.*\n")

    out.append("### 2.2 Production conditions (the leaked value is not available)\n")
    out.append(md_table(
        ["Experiment", "Offline MAE", "Production MAE", "Production - corrected [95% CI]",
         "Verdict"],
        [[r["experiment"], f"{r['offline_mae']:.3f}", f"{r['mae']:.3f}",
          _ci(r["d_mae"], r["d_mae_lo"], r["d_mae_hi"]), r["mae_verdict"]]
         for _, r in prod.iterrows()]) + "\n")
    out.append(
        "Serving values are simulated: missing for the trip's own outcome fields, the "
        "correctly timed version of the feature otherwise.\n")

    out.append("### 2.3 Ablations\n")
    out.append(md_table(
        ["Ablation", "MAE without leak", "MAE with leak", "dMAE [95% CI]", "dMAE %",
         "Effect in the full experiment"],
        [[r["ablation"], f"{r['mae_without_leak']:.3f}", f"{r['mae_with_leak']:.3f}",
          _ci(r["d_mae"], r["d_mae_lo"], r["d_mae_hi"]), f"{r['d_mae_pct']:+.1f}",
          f"{r['main_d_mae_pct']:+.1f}%"] for _, r in abl.iterrows()]) + "\n")

    out.append("## 3. Limitations and assumptions\n")
    for item in [
        "Availability times are simulated from configurable lags (`configs/taxi.yaml`), "
        "not observed. Results such as the share of rows flagged for L9 depend on them.",
        "The auditor reports explicit rule violations only. R4 audits the declared "
        "definition of an aggregate and supports duration aggregates grouped by zone; "
        "the conformance check covers the six aggregates listed above.",
        "Scenarios and auditor were designed together; perfect scores show agreement "
        "with the specification, not general leakage detection.",
        "One month of data, one chronological split (last 7 days) and one model class. "
        "The bootstrap covers test-set sampling, not training randomness or day-level "
        "variation.",
        "Production conditions are a simulation with serving values chosen by us.",
        "Cleaning keeps trips of 1 to 180 minutes, so conclusions apply to that range."]:
        out.append(f"- {item}")
    out.append("")

    out.append("## 4. Reproducibility\n")
    out.append(md_table(
        ["Item", "Value"],
        [["Python", info["python"]], ["scikit-learn", info["sklearn"]],
         ["DuckDB", info["duckdb"]], ["Platform", info["platform"]],
         ["CPU count", info["cpu_count"]], ["Seed", info["seed"]]]) + "\n")
    out.append("```\npython -m leakscope.pipeline\npython -m leakscope.audit_scoring\n"
               "python -m leakscope.evaluation\npython -m leakscope.analysis\n"
               "python -m leakscope.reporting\n```\n")
    return "\n".join(out)


# ------------------------------------------------------------------ runner
def build_report(config_path="configs/taxi.yaml", results_dir="results",
                 assets_dir="assets"):
    ml = load_ml_results(results_dir)       # fail early with a clear message
    audit = collect_audit(config_path)
    figures = make_figures(ml, assets_dir)
    md = render_markdown(audit, ml, figures, results_dir)
    out = Path(results_dir)
    (out / "report.md").write_text(md, encoding="utf-8")
    (out / "audit_report.json").write_text(
        json.dumps(audit_to_json(audit), indent=2), encoding="utf-8")
    print(f"wrote {out / 'report.md'}")
    print(f"wrote {out / 'audit_report.json'}")
    print(f"wrote {Path(assets_dir) / figures['offline_vs_production']}")
    print(f"wrote {Path(assets_dir) / figures['flagged_vs_effect']}")
    return md


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Build the audit + experiment report")
    parser.add_argument("--config", default="configs/taxi.yaml")
    parser.add_argument("--results", default="results")
    parser.add_argument("--assets", default="assets")
    args = parser.parse_args()
    build_report(args.config, args.results, args.assets)