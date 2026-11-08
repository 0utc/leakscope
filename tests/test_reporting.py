"""Tests for the report generator (pure functions on small fixture data)."""
import json

import pandas as pd
import pytest

from leakscope import scenarios
from leakscope.audit_scoring import evaluate_features, score
from leakscope.reporting import (
    audit_to_json, load_ml_results, make_figures, md_table, render_markdown,
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