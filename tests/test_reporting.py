"""Tests for the report generator (pure functions on small fixture data)."""
import json

import pandas as pd
import pytest

from leakscope import scenarios
from leakscope.audit_scoring import evaluate_features, score
from leakscope.reporting import (
    audit_to_json, load_ml_results, make_figures, md_table, render_markdown, sens_reading,
)

UNC_COLS = ["kind", "experiment", "scenario", "mae", "mae_lo", "mae_hi", "d_mae",
            "d_mae_lo", "d_mae_hi", "d_rmse", "d_rmse_lo", "d_rmse_hi",
            "mae_verdict", "rmse_verdict"]


def _unc(kind, rows):
    out = []
    for exp, scen, mae, d in rows:
        verdict = "no clear difference" if d == 0 else ("better" if d < 0 else "worse")
        out.append([kind, exp, scen, mae, mae - 0.2, mae + 0.2, d, d - 0.1, d + 0.1,
                    d, d - 0.1, d + 0.1, verdict, verdict])
    return pd.DataFrame(out, columns=UNC_COLS)


@pytest.fixture
def ml():
    corrected = "corrected (valid features only)"
    l1, allx = "+ L1 leak_trip_distance", "+ ALL leaky features"
    offline = _unc("offline", [(corrected, "-", 6.809, 0.0), (l1, "L1", 3.335, -3.474),
                               (allx, "ALL", 1.451, -5.358)])
    prod = _unc("production", [(l1, "L1", 8.643, 1.834), (allx, "ALL", 8.461, 1.652)])
    prod["offline_mae"] = [3.335, 1.451]
    comparison = pd.DataFrame({
        "experiment": [corrected, l1, allx], "scenario": ["-", "L1", "ALL"],
        "n_features": [8, 9, 17], "mae": [6.809, 3.335, 1.451], "rmse": [9.8, 5.5, 3.3],
        "d_mae_pct": [0.0, -51.0, -78.7], "d_rmse_pct": [0.0, -43.9, -66.0],
        "pct_rows_flagged": [None, 100.0, None], "fit_s": [1.6, 2.1, 2.5]})
    ablation = pd.DataFrame([dict(
        ablation="L5 without N5", scenario="L5", mae_without_leak=6.797,
        mae_with_leak=6.588, d_mae=-0.21, d_mae_lo=-0.25, d_mae_hi=-0.17,
        d_mae_pct=-3.1, main_d_mae_pct=-35.9)])
    info = dict(test_start="2024-01-25 00:00:00", n_train=69899, n_test=30066,
                n_purged=35, baseline_mae=7.787, model="HistGradientBoostingRegressor",
                seed=0, python="3.13", sklearn="1.x", duckdb="1.x", platform="test",
                cpu_count=2)
    return dict(comparison=comparison, uncertainty=offline, production=prod,
                ablation=ablation, info=info)


@pytest.fixture
def audit():
    con, feats, _ = scenarios.build_boundary_fixture()
    try:
        rows = evaluate_features(con, feats)
    finally:
        con.close()
    rows = sorted(rows, key=lambda r: (r["scenario"][0] == "N", int(r["scenario"][1:])))
    return dict(rows=rows, score=score(rows), conformance={"zone_avg_dur_7d": 0},
                limitation=dict(rules_fired=[], conformance_mismatches=5))


FIGS = dict(offline_vs_production="fig_offline_vs_production.png",
            flagged_vs_effect="fig_flagged_vs_effect.png", assets_dir="assets")


def test_md_table():
    assert md_table(["a", "b"], [[1, "x"]]) == "| a | b |\n|---|---|\n| 1 | x |"


def test_figures_are_written_as_png_files(ml, tmp_path):
    figs = make_figures(ml, tmp_path)
    for name in (figs["offline_vs_production"], figs["flagged_vs_effect"]):
        data = (tmp_path / name).read_bytes()
        assert data[:8] == b"\x89PNG\r\n\x1a\n" and len(data) > 5000


def test_report_has_all_sections_and_links_the_figures(ml, audit):
    md = render_markdown(audit, ml, FIGS, results_dir="results")
    for heading in ("## Summary", "## 1. Audit scorecard", "### Findings",
                    "## 2. ML experiment", "### 2.2 Production conditions",
                    "### 2.3 Ablations", "## 3. Limitations and assumptions",
                    "## 4. Reproducibility"):
        assert heading in md
    assert "(../assets/fig_offline_vs_production.png)" in md
    assert "(../assets/fig_flagged_vs_effect.png)" in md
    assert "boundary_late" in md and "1 s later" in md


def test_summary_numbers_come_from_the_data(ml, audit):
    md = render_markdown(audit, ml, FIGS)
    assert "MAE 6.809 min" in md and "7.787" in md
    assert "(-78.7% vs the honest model)" in md
    assert "(+24.3% vs the honest model)" in md
    assert "1 of 1 leaky features were flagged and 1 of 1 valid" in md


def test_report_is_deterministic(ml, audit):
    assert render_markdown(audit, ml, FIGS) == render_markdown(audit, ml, FIGS)


def test_json_export_is_serialisable_and_complete(audit):
    data = json.loads(json.dumps(audit_to_json(audit)))
    assert [r["scenario"] for r in data["scenarios"]] == ["L10", "N4"]
    first = data["scenarios"][0]
    assert first["expected_rules"] == ["R2"] and first["matches"] is True
    assert first["findings"][0]["sample_trip_ids"] == [1, 2, 3]
    assert data["known_limitation_M1"]["conformance_mismatches"] == 5


def test_missing_ml_files_give_a_helpful_error(tmp_path):
    with pytest.raises(FileNotFoundError, match="leakscope.evaluation"):
        load_ml_results(tmp_path)

# ----------------------- optional sections: sensitivity, benchmark, precision
SENS_COLS = ["experiment", "scenario", "reference_mae", "mean_mae", "std_mae", "min_mae",
             "max_mae", "d_mean", "d_std", "d_min", "d_max", "runs_lower", "n_runs",
             "relative_noise"]


def _sens_row(exp, scen, ref, mean, sd, lo, hi, d, dsd, low):
    return [exp, scen, ref, mean, sd, lo, hi, d, dsd, d - dsd, d + dsd, low, 20, 1e-13]


@pytest.fixture
def ml_extra(ml):
    sens = pd.DataFrame([
        _sens_row("corrected (valid features only)", "-", 6.817, 6.798, 0.0073, 6.778, 6.808, 0.0, 0.0, 0),
        _sens_row("+ L1 leak_trip_distance", "L1", 3.332, 3.329, 0.0050, 3.317, 3.339, -3.469, 0.0102, 20),
        _sens_row("+ L6 leak_zone_avg_60m_inprogress", "L6", 6.801, 6.805, 0.0071, 6.789, 6.822, 0.0064, 0.0120, 5),
        _sens_row("+ L9 leak_last_fare_zone", "L9", 6.811, 6.818, 0.0047, 6.813, 6.826, 0.0195, 0.0097, 0),
    ], columns=SENS_COLS)
    cols = ["n_examples", "n_runs", "n_examples_actual", "n_clean_trips"]
    row = lambda n, trips, p, a, m, t, mem, first: {
        "n_examples": n, "n_runs": 3, "n_examples_actual": n, "n_clean_trips": trips,
        "pipeline_s_median": p, "pipeline_s_min": p - 0.1, "pipeline_s_max": p + 0.1,
        "audit_s_median": a, "audit_s_min": a - 0.1, "audit_s_max": a + 0.1,
        "ml_s_median": m, "ml_s_min": m - 0.3, "ml_s_max": m + 0.1,
        "total_s_median": t, "total_s_min": t - 0.3, "total_s_max": t + 0.0,
        "peak_mb_median": mem, "peak_mb_min": mem, "peak_mb_max": mem,
        "total_s_first_run": first}
    bench = pd.DataFrame([row(10_000, 19_928, 1.6, 1.1, 21.9, 24.7, 226.0, 24.4),
                          row(50_000, 99_640, 3.6, 2.1, 32.5, 38.2, 264.0, 37.9)])
    prod = ml["production"]
    small = prod.iloc[[0]].copy()
    small["scenario"], small["experiment"], small["d_mae"] = "L9", "+ L9 leak_last_fare_zone", 0.01
    out = dict(ml)
    out["production"] = pd.concat([prod, small], ignore_index=True)
    out["sensitivity"] = sens
    out["benchmark"] = bench
    out["benchmark_info"] = dict(
        hardware="Test laptop, 4 GB RAM", runs=3, trips_per_example=2, n_boot=500,
        duckdb_memory_limit="1GB", duckdb_threads=2)
    return out


def test_sensitivity_reading_rule():
    base = dict(n_runs=20, runs_lower=20, d_mean=-0.30, d_std=0.01)
    assert sens_reading(pd.Series(base)) == "clear, lower error"
    assert sens_reading(pd.Series(dict(base, d_mean=-0.03))) == "borderline, lower error"
    assert sens_reading(pd.Series(dict(base, d_mean=0.03, runs_lower=0))) == "borderline, higher error"
    assert sens_reading(pd.Series(dict(base, d_mean=-0.01))) == "not distinguishable from 0"
    assert sens_reading(pd.Series(dict(base, runs_lower=3))) == "not distinguishable from 0"  # 3/20 mixed
    assert sens_reading(pd.Series(dict(base, runs_lower=2))) == "clear, lower error"           # 2/20 is the edge


def test_report_without_extras_still_explains_the_uncertainty(ml, audit):
    md = render_markdown(audit, ml, FIGS)
    assert "### 2.4 How precise are these numbers?" in md
    assert "not measured in this run" in md
    assert "Test sampling (measured)" in md and "Not studied" in md
    assert "## Performance" not in md and "Performance" not in md.split("## 3.")[1]
    assert "## 4. Reproducibility" in md


def test_report_with_extras_has_sensitivity_benchmark_and_precision(ml_extra, audit):
    md = render_markdown(audit, ml_extra, FIGS)
    assert "- **Precision:**" in md and "jitter SD 0.007" in md
    assert "borderline, higher error" in md           # L9
    assert "clear, lower error" in md                 # L1
    assert "not distinguishable from 0" in md         # L6
    assert "outside the refits' min-to-max range for 2 of 4 models" in md
    assert "Production conditions were not covered" in md and "for L9" in md
    assert "## 4. Performance" in md and "## 5. Reproducibility" in md
    assert "Test laptop, 4 GB RAM" in md and "24.7 (24.4 to 24.7)" in md
    assert "10,000" in md and "50,000" in md
    assert "python -m leakscope.sensitivity" in md and "python -m leakscope.benchmark" in md
    assert render_markdown(audit, ml_extra, FIGS) == md     # still deterministic


def test_optional_result_files_are_loaded_only_when_present(ml_extra, tmp_path):
    for key, name in (("comparison", "ml_comparison.csv"), ("uncertainty", "ml_uncertainty.csv"),
                      ("production", "ml_production.csv"), ("ablation", "ml_ablation.csv")):
        ml_extra[key].to_csv(tmp_path / name, index=False)
    (tmp_path / "ml_run_info.json").write_text(json.dumps(ml_extra["info"]), encoding="utf-8")
    got = load_ml_results(tmp_path)
    assert "sensitivity" not in got and "benchmark" not in got
    ml_extra["sensitivity"].to_csv(tmp_path / "ml_sensitivity.csv", index=False)
    ml_extra["benchmark"].to_csv(tmp_path / "benchmark_summary.csv", index=False)
    assert "benchmark" not in load_ml_results(tmp_path)        # needs the info file too
    (tmp_path / "benchmark_info.json").write_text(
        json.dumps(ml_extra["benchmark_info"]), encoding="utf-8")
    got = load_ml_results(tmp_path)
    assert len(got["sensitivity"]) == 4 and len(got["benchmark"]) == 2