"""Tests for the benchmark: summary statistics, memory probe, and one tiny real run."""
import pandas as pd

from leakscope.benchmark import peak_memory_mb, run_benchmark, summarize


def _run(i, pipeline):
    return dict(n_examples=10, run=i, n_examples_actual=10, n_clean_trips=20,
                pipeline_s=pipeline, audit_s=1.0, ml_s=1.0, total_s=pipeline + 2.0,
                peak_mb=100.0 + pipeline)


def test_summary_uses_the_median_and_keeps_the_first_run():
    row = summarize([_run(0, 5.0), _run(1, 3.0), _run(2, 100.0)])[0]
    assert row["n_runs"] == 3
    assert row["pipeline_s_median"] == 5.0 and row["total_s_median"] == 7.0
    assert row["pipeline_s_min"] == 3.0 and row["pipeline_s_max"] == 100.0
    assert row["total_s_first_run"] == 7.0


def test_summary_has_one_row_per_size():
    runs = [_run(0, 1.0), dict(_run(0, 2.0), n_examples=20)]
    assert [r["n_examples"] for r in summarize(runs)] == [10, 20]


def test_peak_memory_is_positive():
    assert peak_memory_mb() > 10


def test_tiny_end_to_end_benchmark_writes_its_files(tmp_path):
    out = tmp_path / "out"
    rows, summary = run_benchmark(sizes=(2500,), runs=1, out_dir=out,
                                  work_dir=tmp_path / "w", trips_per_example=4, n_boot=50)
    r = rows[0]
    assert r["n_examples_actual"] == 2500
    assert r["pipeline_s"] > 0 and r["audit_s"] > 0 and r["ml_s"] > 0
    assert r["pipeline_s"] + r["audit_s"] + r["ml_s"] <= r["total_s"] + 0.5
    assert r["peak_after_pipeline_mb"] <= r["peak_after_audit_mb"] <= r["peak_mb"]
    assert summary[0]["n_runs"] == 1
    for name in ("benchmark_runs.csv", "benchmark_summary.csv", "benchmark_info.json"):
        assert (out / name).exists(), name
    assert len(pd.read_csv(out / "benchmark_runs.csv")) == 1