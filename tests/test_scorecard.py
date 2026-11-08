"""Tests for the labelled scenarios, the scoring maths and the known limitation."""
from pathlib import Path

import duckdb
import pytest

from leakscope import scenarios
from leakscope.audit_scoring import evaluate_features, prf, score
from leakscope.auditor import audit_feature
from leakscope.conformance import check_conformance
from leakscope.registry import load_registry

DB = Path("data/processed/leakscope.duckdb")


def test_every_scenario_in_the_registry_has_ground_truth():
    used = {f["scenario"] for f in load_registry()}
    assert used <= set(scenarios.EXPECTED_RULES)
    # scenarios without a registry feature must be covered by a fixture
    fixture_ids = {"N4", "L10"}
    assert set(scenarios.EXPECTED_RULES) - used == fixture_ids
    assert set(scenarios.SCENARIO_NAMES) == set(scenarios.EXPECTED_RULES)


def test_requirements_on_scenario_counts():
    leaky = [s for s, r in scenarios.EXPECTED_RULES.items() if r]
    valid = [s for s, r in scenarios.EXPECTED_RULES.items() if not r]
    assert len(leaky) >= 8 and len(valid) >= 4


def test_boundary_fixture_exactly_at_t_pred_is_clean_one_second_late_is_not():
    con, feats, n = scenarios.build_boundary_fixture()
    try:
        rows = {r["scenario"]: r for r in evaluate_features(con, feats)}
    finally:
        con.close()
    assert rows["N4"]["fired"] == set() and rows["N4"]["n_flagged"] == 0
    assert rows["L10"]["fired"] == {"R2"} and rows["L10"]["n_flagged"] == n


def test_lying_aggregate_is_missed_by_the_rules_but_caught_by_conformance():
    con, feat, table, col = scenarios.build_lying_aggregate_fixture()
    try:
        assert audit_feature(con, feat) == []                  # declaration looks safe
        mismatches = check_conformance(con, feat, table, col)  # but values disagree
    finally:
        con.close()
    assert len(mismatches) > 0


def test_precision_recall_maths():
    assert prf(8, 2, 0) == (0.8, 1.0)
    assert prf(3, 0, 1) == (1.0, 0.75)
    assert prf(0, 0, 0) == (None, None)


def test_score_counts_a_missed_and_a_false_alarm():
    rows = [
        dict(expected={"R2"}, fired={"R2"}),        # correct
        dict(expected={"R3"}, fired=set()),         # missed leak
        dict(expected=set(), fired={"R1"}),         # false alarm
        dict(expected=set(), fired=set()),          # correct negative
    ]
    s = score(rows)
    assert s["feature"] == dict(tp=1, fp=1, fn=1, tn=1)
    assert s["rule"]["tp"] == 1 and s["rule"]["fp"] == 1 and s["rule"]["fn"] == 1
    assert s["exact"] == 2 and s["n"] == 4


def test_auditor_matches_ground_truth_on_real_database():
    if not DB.exists():
        pytest.skip("local database not built (run python -m leakscope.pipeline)")
    con = duckdb.connect(str(DB), read_only=True)
    con.execute("SET memory_limit='1GB'")
    try:
        rows = evaluate_features(con, load_registry())
    finally:
        con.close()
    wrong = [(r["feature"], r["expected"], r["fired"])
             for r in rows if r["expected"] != r["fired"]]
    assert wrong == []