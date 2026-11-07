"""Build feature source tables and row-level features.

zone_cum    : running totals per zone ordered by availability time (the
              'bank statement' used for point-in-time lookups)
zone_global : whole-dataset zone averages (used ONLY as a leaky feature)
row_features: one row per prediction example with safe and leaky row-level
              features, plus the target.
"""
from leakscope.ingestion import connect, load_config

def create_zone_cum(con):
    """Running totals per zone ordered by availability time."""
    con.execute("DROP TABLE IF EXISTS zone_cum")
    con.execute("""
        CREATE TABLE zone_cum AS
        SELECT pu_zone,
               avail_dropoff,
               sum(duration_min) OVER w AS cum_sum,
               count(*)          OVER w AS cum_cnt
        FROM trip_times
        WINDOW w AS (PARTITION BY pu_zone ORDER BY avail_dropoff)
    """)

def build_feature_tables(cfg):
    con = connect(cfg)
    try:
        create_zone_cum(con)

        con.execute("DROP TABLE IF EXISTS zone_global")
        con.execute("""
            CREATE TABLE zone_global AS
            SELECT pu_zone,
                   avg(duration_min) AS zone_avg_global,
                   count(*)          AS n_trips
            FROM trip_times
            GROUP BY pu_zone
        """)

        con.execute("DROP TABLE IF EXISTS row_features")
        con.execute("""
            CREATE TABLE row_features AS
            SELECT
                p.trip_id,
                p.t_pred,
                p.target_duration_min,
                -- valid row-level features (N1)
                hour(p.t_pred)                     AS pickup_hour,
                dayofweek(p.t_pred)                AS pickup_dow,
                c.PULocationID                     AS pu_zone,
                coalesce(c.passenger_count, 1)     AS passenger_count_filled,
                (c.passenger_count IS NULL)::INT   AS passenger_missing,
                -- intentionally leaky features
                c.trip_distance                    AS leak_trip_distance,
                c.total_amount                     AS leak_total_amount,
                hour(c.tpep_dropoff_datetime)      AS leak_dropoff_hour,
                g.zone_avg_global                  AS leak_zone_avg_global
            FROM prediction_examples p
            JOIN clean_trips c ON c.trip_id = p.trip_id
            LEFT JOIN zone_global g ON g.pu_zone = c.PULocationID
            ORDER BY p.t_pred, p.trip_id
        """)
    finally:
        con.close()


def verify(cfg):
    con = connect(cfg)
    try:
        print("--- Integrity (first two numbers must match) ---")
        con.sql("""
            SELECT
              (SELECT sum(m) FROM (SELECT max(cum_cnt) AS m
                                   FROM zone_cum GROUP BY pu_zone)) AS zone_cum_total,
              (SELECT count(*) FROM clean_trips)                    AS clean_trips,
              (SELECT count(*) FROM row_features)                   AS feature_rows
        """).show()

        print("--- Correlation of each feature with the target ---")
        con.sql("""
            SELECT
              round(corr(pickup_hour, target_duration_min), 3)            AS r_pickup_hour,
              round(corr(passenger_count_filled, target_duration_min), 3) AS r_passengers,
              round(corr(leak_dropoff_hour, target_duration_min), 3)      AS r_leak_dropoff_hr,
              round(corr(leak_zone_avg_global, target_duration_min), 3)   AS r_leak_zone_glob,
              round(corr(leak_trip_distance, target_duration_min), 3)     AS r_leak_distance,
              round(corr(leak_total_amount, target_duration_min), 3)      AS r_leak_amount
            FROM row_features
        """).show()
    finally:
        con.close()


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Build feature tables")
    parser.add_argument("--config", default="configs/taxi.yaml")
    args = parser.parse_args()
    config = load_config(args.config)
    build_feature_tables(config)
    verify(config)