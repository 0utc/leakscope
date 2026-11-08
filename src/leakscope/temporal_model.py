"""Temporal data model: simulated availability times and prediction examples.

Availability times are SIMULATED from the lag rules in the config. The raw
TLC data does not record when fields became available to a live system.
"""
from leakscope.ingestion import connect, load_config


def build_availability(cfg):
    a = cfg["availability"]
    con = connect(cfg)
    try:
        con.execute("DROP TABLE IF EXISTS trip_times")
        con.execute(f"""
            CREATE TABLE trip_times AS
            SELECT
                trip_id,
                PULocationID          AS pu_zone,
                duration_min,
                -- event times (observed in the raw data)
                tpep_pickup_datetime  AS event_pickup,
                tpep_dropoff_datetime AS event_dropoff,
                -- availability times (SIMULATED)
                tpep_pickup_datetime
                    + INTERVAL {int(a['pickup_fields_lag_min'])} MINUTE  AS avail_pickup,
                tpep_dropoff_datetime
                    + INTERVAL {int(a['dropoff_fields_lag_min'])} MINUTE AS avail_dropoff,
                tpep_dropoff_datetime
                    + INTERVAL {int(a['distance_lag_min'])} MINUTE       AS avail_distance,
                tpep_dropoff_datetime
                    + INTERVAL {int(a['fare_lag_min'])} MINUTE           AS avail_fare,
                date_trunc('day', tpep_dropoff_datetime) + INTERVAL 1 DAY
                    + INTERVAL {int(a['fare_correction_next_day_hour'])} HOUR
                                                                         AS avail_fare_correction,
                -- end-of-day snapshot clocks (used by scenario L8)
                date_trunc('day', tpep_pickup_datetime) + INTERVAL 1 DAY AS event_snapshot_eod,
                date_trunc('day', tpep_pickup_datetime) + INTERVAL 1 DAY AS avail_snapshot_eod
            FROM clean_trips
        """)
    finally:
        con.close()


def build_examples(cfg):
    s = cfg["sampling"]
    start = cfg["window"]["start"]
    con = connect(cfg)
    try:
        con.execute("DROP TABLE IF EXISTS prediction_examples")
        con.execute(f"""
            CREATE TABLE prediction_examples AS
            WITH sampled AS (
                SELECT trip_id,
                       event_pickup AS t_pred,
                       duration_min AS target_duration_min
                FROM trip_times
                WHERE event_pickup >= TIMESTAMP '{start}'
                                      + INTERVAL {int(s['warmup_days'])} DAY
                ORDER BY hash(trip_id + {int(s['seed'])})
                LIMIT {int(s['n_examples'])}
            )
            SELECT * FROM sampled ORDER BY t_pred, trip_id
        """)
    finally:
        con.close()


def verify(cfg):
    con = connect(cfg)
    try:
        print("--- Clock ordering checks (all three must be 0) ---")
        con.sql("""
            SELECT
              count(*) FILTER (WHERE avail_dropoff <= event_pickup)       AS dropoff_not_after_pickup,
              count(*) FILTER (WHERE avail_fare < avail_dropoff)          AS fare_before_dropoff,
              count(*) FILTER (WHERE avail_fare_correction <= avail_fare) AS correction_not_after_fare
            FROM trip_times
        """).show()

        print("--- Prediction examples ---")
        con.sql("""
            SELECT count(*) AS n_examples,
                   count(DISTINCT trip_id) AS n_unique,
                   min(t_pred) AS first_t_pred,
                   max(t_pred) AS last_t_pred,
                   round(avg(target_duration_min), 2) AS mean_target_min
            FROM prediction_examples
        """).show()

        print("--- Each example's OWN fields vs its prediction time ---")
        con.sql("""
            SELECT
              round(100.0 * avg((tt.avail_pickup <= p.t_pred)::INT), 2)  AS pct_pickup_usable_le,
              round(100.0 * avg((tt.avail_pickup <  p.t_pred)::INT), 2)  AS pct_pickup_usable_lt,
              round(100.0 * avg((tt.avail_dropoff  > p.t_pred)::INT), 2) AS pct_dropoff_late,
              round(100.0 * avg((tt.avail_distance > p.t_pred)::INT), 2) AS pct_distance_late,
              round(100.0 * avg((tt.avail_fare     > p.t_pred)::INT), 2) AS pct_fare_late,
              round(100.0 * avg((tt.avail_fare_correction > p.t_pred)::INT), 2) AS pct_correction_late
            FROM prediction_examples p
            JOIN trip_times tt USING (trip_id)
        """).show()
    finally:
        con.close()


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Build availability times and examples")
    parser.add_argument("--config", default="configs/taxi.yaml")
    args = parser.parse_args()
    config = load_config(args.config)
    build_availability(config)
    build_examples(config)
    verify(config)