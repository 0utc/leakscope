"""Robustness: bad input and bad configuration must fail early with a clear
message, and awkward-but-valid data (duplicates, sparse zones) must not crash
the pipeline or cause false alarms."""
import copy
import os

import numpy as np
import pandas as pd
import pytest
from typer.testing import CliRunner

from leakscope.audit_scoring import collect
from leakscope.checks import PipelineError, validate_config
from leakscope.cli import app
from leakscope.evaluation import chronological_split
from leakscope.ingestion import connect, load_config
from leakscope.pipeline import run_all
from leakscope.schema import validate_model_table, validate_raw
from leakscope.synthetic import generate, make_demo_config

runner = CliRunner()


def setup_case(tmp_path, mutate=None, n_trips=4000, n_examples=1000):
    """Synthetic data, optionally damaged by `mutate`, plus a demo config for it."""
    work = tmp_path / "w"
    paths = generate(work, n_trips=n_trips)
    if mutate is not None:
        df = mutate(pd.read_parquet(paths["parquet"]))
        df.to_parquet(paths["parquet"], index=False)
    return str(make_demo_config(work, "configs/taxi.yaml", n_examples=n_examples,
                                n_boot=50)), paths


# ------------------------------------------------------- bad input data
def test_empty_file_gives_a_clear_error(tmp_path):
    cfg, _ = setup_case(tmp_path, mutate=lambda d: d.iloc[0:0])
    with pytest.raises(PipelineError, match="no usable trips.*no rows"):
        run_all(cfg)


def test_data_from_another_period_names_the_reason(tmp_path):
    def shift(d):
        d = d.copy()
        d["tpep_pickup_datetime"] -= pd.DateOffset(years=1)
        d["tpep_dropoff_datetime"] -= pd.DateOffset(years=1)
        return d
    cfg, _ = setup_case(tmp_path, mutate=shift)
    with pytest.raises(PipelineError, match="pickup_outside_window"):
        run_all(cfg)


def test_trips_only_inside_the_warmup_give_a_clear_error(tmp_path):
    cfg, _ = setup_case(tmp_path, mutate=lambda d: d[
        (d["tpep_pickup_datetime"] >= "2024-01-01") & (d["tpep_pickup_datetime"] < "2024-01-07")])
    with pytest.raises(PipelineError, match="no prediction examples.*warmup_days"):
        run_all(cfg)


def test_missing_column_is_named(tmp_path):
    cfg, _ = setup_case(tmp_path, mutate=lambda d: d.drop(columns=["PULocationID"]))
    with pytest.raises(PipelineError, match="missing required column.*PULocationID"):
        run_all(cfg)


def test_wrong_column_type_is_named(tmp_path):
    cfg, _ = setup_case(tmp_path, mutate=lambda d: d.assign(
        tpep_pickup_datetime=d["tpep_pickup_datetime"].astype(str)))
    with pytest.raises(PipelineError, match="tpep_pickup_datetime"):
        run_all(cfg)


def test_missing_file_explains_how_to_get_data(tmp_path):
    cfg, paths = setup_case(tmp_path)
    os.remove(paths["parquet"])
    with pytest.raises(PipelineError, match="(?s)file not found.*demo"):
        run_all(cfg)


def test_a_file_that_is_not_parquet_is_reported(tmp_path):
    cfg, paths = setup_case(tmp_path)
    with open(paths["parquet"], "w") as f:
        f.write("this is not a parquet file")
    with pytest.raises(PipelineError, match="cannot read"):
        validate_raw(paths["parquet"])


def test_raw_contract_accepts_the_synthetic_file(tmp_path):
    paths = generate(tmp_path, n_trips=1500)
    assert validate_raw(paths["parquet"]) == 1500


# ------------------------------------------- awkward but valid data
def test_duplicated_trips_do_not_crash_or_cause_false_alarms(tmp_path):
    cfg, _ = setup_case(tmp_path, mutate=lambda d: pd.concat(
        [d, d.sample(frac=0.05, random_state=1)], ignore_index=True))
    run_all(cfg)
    con = connect(load_config(cfg))
    try:
        unique = con.execute("SELECT count(*) = count(DISTINCT trip_id) FROM trips").fetchone()[0]
    finally:
        con.close()
    assert unique                                   # every row still has its own trip_id
    rows, _, _ = collect(cfg)
    assert [r["feature"] for r in rows if r["expected"] != r["fired"]] == []


def test_sparse_zones_give_missing_values_but_no_false_alarms(tmp_path):
    cfg, _ = setup_case(tmp_path, mutate=lambda d: d.assign(
        PULocationID=(np.arange(len(d)) % 1500 + 1).astype("int32")))
    run_all(cfg)
    rows, _, _ = collect(cfg)
    valid = [r for r in rows if not r["expected"]]
    assert valid and all(not r["fired"] for r in valid)       # no false alarms
    con = connect(load_config(cfg))
    try:
        n_null = con.execute(
            "SELECT count(*) FROM pit_features WHERE zone_avg_dur_7d IS NULL").fetchone()[0]
    finally:
        con.close()
    assert n_null > 0                                         # and the gaps are real NULLs


# --------------------------------------------------------- configuration
@pytest.fixture
def base_cfg():
    return load_config("configs/taxi.yaml")


def test_the_real_config_is_valid(base_cfg):
    validate_config(base_cfg)


def test_empty_or_non_mapping_config_is_rejected():
    for bad in (None, [], "text"):
        with pytest.raises(PipelineError, match="empty or is not a YAML mapping"):
            validate_config(bad)


def test_missing_sections_and_keys_are_listed(base_cfg):
    cfg = copy.deepcopy(base_cfg)
    del cfg["sampling"]
    del cfg["cleaning"]["max_duration_min"]
    with pytest.raises(PipelineError) as e:
        validate_config(cfg)
    assert "missing section 'sampling'" in str(e.value)
    assert "missing 'cleaning.max_duration_min'" in str(e.value)


def test_all_value_problems_are_reported_at_once(base_cfg):
    cfg = copy.deepcopy(base_cfg)
    cfg["window"]["start"], cfg["window"]["end"] = "2024-02-01", "2024-01-01"
    cfg["cleaning"]["min_duration_min"] = 500
    cfg["sampling"]["n_examples"] = 0
    cfg["availability"]["fare_correction_next_day_hour"] = 30
    cfg["analysis"]["n_boot"] = "many"
    with pytest.raises(PipelineError) as e:
        validate_config(cfg)
    text = str(e.value)
    for fragment in ("window.start must be before window.end", "cleaning limits",
                     "sampling.n_examples", "fare_correction_next_day_hour", "analysis.n_boot"):
        assert fragment in text


def test_test_start_must_leave_room_for_warmup_and_testing(base_cfg):
    for bad in ("2024-01-03", "2024-03-01"):      # inside the warm-up / after the window
        cfg = copy.deepcopy(base_cfg)
        cfg["ml"]["test_start"] = bad
        with pytest.raises(PipelineError, match="ml.test_start"):
            validate_config(cfg)


# --------------------------------------------------- split and contracts
def test_a_split_with_no_test_rows_explains_itself():
    df = pd.DataFrame({"t_pred": pd.to_datetime(["2024-01-10", "2024-01-11"]),
                       "label_available_at": pd.to_datetime(["2024-01-10 01:00", "2024-01-11 01:00"])})
    with pytest.raises(PipelineError, match="ml.test_start"):
        chronological_split(df, "2024-02-01")


def _model_table(n=5):
    t = pd.date_range("2024-01-10", periods=n, freq="h")
    return pd.DataFrame({"trip_id": np.arange(n), "t_pred": t,
                         "label_available_at": t + pd.Timedelta(minutes=10),
                         "target": np.full(n, 12.0), "f1": np.arange(n, dtype=float)})


def test_model_table_contract_accepts_a_valid_table():
    assert len(validate_model_table(_model_table(), ["f1"])) == 5


@pytest.mark.parametrize("damage, fragment", [
    (lambda d: d.assign(trip_id=[1, 1, 2, 3, 4]), "trip_id"),
    (lambda d: d.assign(f1=[0.0, np.inf, 1.0, 2.0, 3.0]), "infinite"),
    (lambda d: d.assign(target=[12.0, 0.0, 5.0, 5.0, 5.0]), "target"),
    (lambda d: d.assign(label_available_at=d["t_pred"] - pd.Timedelta(minutes=1)), "after the prediction time"),
])
def test_model_table_contract_catches_damage(damage, fragment):
    with pytest.raises(PipelineError, match=fragment):
        validate_model_table(damage(_model_table()), ["f1"])


def test_empty_model_table_is_rejected():
    with pytest.raises(PipelineError, match="empty"):
        validate_model_table(_model_table().iloc[0:0], ["f1"])


# ------------------------------------------------------ command line
def test_cli_shows_a_short_message_instead_of_a_traceback(tmp_path):
    cfg, paths = setup_case(tmp_path)
    os.remove(paths["parquet"])
    r = runner.invoke(app, ["pipeline", "--config", cfg])
    assert r.exit_code == 2
    assert "ERROR:" in r.output and "file not found" in r.output
    assert "Traceback" not in r.output