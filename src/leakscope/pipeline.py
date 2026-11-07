"""Run the whole pipeline: raw Parquet -> clean trips -> availability times
-> prediction examples -> feature tables -> point-in-time features."""
import time

from leakscope import feature_builder, ingestion, temporal_join, temporal_model


def run_all(config_path="configs/taxi.yaml"):
    cfg = ingestion.load_config(config_path)
    steps = [
        ("ingestion", lambda: ingestion.ingest(cfg)),
        ("availability times", lambda: temporal_model.build_availability(cfg)),
        ("prediction examples", lambda: temporal_model.build_examples(cfg)),
        ("feature tables", lambda: feature_builder.build_feature_tables(cfg)),
        ("point-in-time features", lambda: temporal_join.build_all(cfg)),
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