"""Load the raw TLC Parquet file into DuckDB.

Adds a trip_id and a duration, and labels every row with a cleaning
decision (reject_reason) instead of deleting it.
"""
from pathlib import Path

import duckdb
import yaml


def load_config(path):
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def connect(cfg):
    db_path = Path(cfg["data"]["database"])
    db_path.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(db_path))
    con.execute(f"SET memory_limit='{cfg['duckdb']['memory_limit']}'")
    con.execute(f"SET threads={int(cfg['duckdb']['threads'])}")
    temp_dir = Path(cfg["data"]["temp_dir"]).resolve().as_posix()
    con.execute(f"SET temp_directory='{temp_dir}'")
    return con


def ingest(cfg):
    con = connect(cfg)
    try:
        raw = Path(cfg["data"]["raw_parquet"]).as_posix()
        zones = Path(cfg["data"]["zone_lookup"]).as_posix()
        start, end = cfg["window"]["start"], cfg["window"]["end"]
        lo = cfg["cleaning"]["min_duration_min"]
        hi = cfg["cleaning"]["max_duration_min"]

        con.execute("DROP VIEW IF EXISTS clean_trips")
        con.execute("DROP TABLE IF EXISTS trips")
        con.execute(f"""
            CREATE TABLE trips AS
            WITH src AS (
                SELECT * FROM read_parquet('{raw}', file_row_number = true)
            ),
            timed AS (
                SELECT
                    file_row_number::BIGINT AS trip_id,
                    * EXCLUDE (file_row_number),
                    date_diff('second', tpep_pickup_datetime,
                              tpep_dropoff_datetime) / 60.0 AS duration_min
                FROM src
            )
            SELECT *,
                CASE
                    WHEN tpep_pickup_datetime IS NULL
                      OR tpep_dropoff_datetime IS NULL THEN 'missing_timestamp'
                    WHEN tpep_pickup_datetime < TIMESTAMP '{start}'
                      OR tpep_pickup_datetime >= TIMESTAMP '{end}'
                        THEN 'pickup_outside_window'
                    WHEN duration_min <= 0 THEN 'nonpositive_duration'
                    WHEN duration_min < {lo} THEN 'duration_too_short'
                    WHEN duration_min > {hi} THEN 'duration_too_long'
                    ELSE NULL
                END AS reject_reason
            FROM timed
        """)
        con.execute(
            "CREATE VIEW clean_trips AS "
            "SELECT * FROM trips WHERE reject_reason IS NULL"
        )
        con.execute("DROP TABLE IF EXISTS zones")
        con.execute(
            f"CREATE TABLE zones AS SELECT * FROM read_csv('{zones}', header = true)"
        )
    finally:
        con.close()


def summarize(cfg):
    con = connect(cfg)
    try:
        print("--- Rows by cleaning decision (first failing rule wins) ---")
        con.sql("""
            SELECT coalesce(reject_reason, 'KEPT') AS decision,
                   count(*) AS n,
                   round(100.0 * count(*) / sum(count(*)) OVER (), 3) AS pct
            FROM trips GROUP BY 1 ORDER BY n DESC
        """).show()

        print("--- Integrity checks ---")
        con.sql("""
            SELECT count(*) AS n_rows,
                   count(DISTINCT trip_id) AS n_unique_ids,
                   count(*) FILTER (WHERE reject_reason IS NULL) AS n_kept
            FROM trips
        """).show()

        print("--- Kept trips: pickup range and duration ---")
        con.sql("""
            SELECT min(tpep_pickup_datetime) AS first_pickup,
                   max(tpep_pickup_datetime) AS last_pickup,
                   round(quantile_cont(duration_min, 0.5), 1) AS median_min,
                   round(max(duration_min), 1) AS max_min
            FROM clean_trips
        """).show()
    finally:
        con.close()


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Ingest and clean TLC data")
    parser.add_argument("--config", default="configs/taxi.yaml")
    args = parser.parse_args()
    config = load_config(args.config)
    ingest(config)
    summarize(config)