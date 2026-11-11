"""Run the whole pipeline: raw Parquet -> clean trips -> availability times
-> prediction examples -> feature tables -> point-in-time features
-> scenario features. Bad input fails early with a readable PipelineError."""
import time

from leakscope import (
    feature_builder, ingestion, scenarios, temporal_join, temporal_model,
)
from leakscope.checks import PipelineError, check_raw_files
from leakscope.schema import validate_raw


def check_clean_trips(cfg):
    """After ingestion: at least one trip must survive the cleaning rules."""
    con = ingestion.connect(cfg)
    try:
        rows = con.execute("""
            SELECT coalesce(reject_reason, 'KEPT'), count(*) FROM trips
            GROUP BY 1 ORDER BY 2 DESC
        """).fetchall()
    finally:
        con.close()
    counts = dict(rows)
    if counts.get("KEPT", 0) == 0:
        detail = (", ".join(f"{k}: {v:,}" for k, v in counts.items())
                  or "the file has no rows")
        raise PipelineError(
            f"no usable trips after cleaning ({detail}). Check window.start/window.end "
            f"and the cleaning limits in the config, and that the file covers that period.")
    return counts["KEPT"]


def check_examples(cfg):
    """After sampling: there must be prediction examples to work with."""
    con = ingestion.connect(cfg)
    try:
        n = con.execute("SELECT count(*) FROM prediction_examples").fetchone()[0]
        lo, hi = con.execute(
            "SELECT min(event_pickup), max(event_pickup) FROM trip_times").fetchone()
    finally:
        con.close()
    if n == 0:
        raise PipelineError(
            f"no prediction examples: the clean trips run from {lo} to {hi}, but examples "
            f"need pickups on or after window.start + sampling.warmup_days "
            f"({cfg['window']['start']} + {cfg['sampling']['warmup_days']} days).")
    return n


def _input_checks(cfg):
    check_raw_files(cfg)
    validate_raw(cfg["data"]["raw_parquet"])


def _ingest(cfg):
    ingestion.ingest(cfg)
    check_clean_trips(cfg)


def _examples(cfg):
    temporal_model.build_examples(cfg)
    check_examples(cfg)


def run_all(config_path="configs/taxi.yaml"):
    cfg = ingestion.load_config(config_path)
    steps = [
        ("input checks", lambda: _input_checks(cfg)),
        ("ingestion", lambda: _ingest(cfg)),
        ("availability times", lambda: temporal_model.build_availability(cfg)),
        ("prediction examples", lambda: _examples(cfg)),
        ("feature tables", lambda: feature_builder.build_feature_tables(cfg)),
        ("point-in-time features", lambda: temporal_join.build_all(cfg)),
        ("scenario features", lambda: scenarios.build_all(cfg)),
    ]
    total_start = time.perf_counter()
    for name, step in steps:
        start = time.perf_counter()
        step()
        print(f"[ok] {name:24} {time.perf_counter() - start:7.1f}s")
    print(f"Total: {time.perf_counter() - total_start:.1f}s")
    return cfg


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Run the full pipeline")
    parser.add_argument("--config", default="configs/taxi.yaml")
    run_all(parser.parse_args().config)