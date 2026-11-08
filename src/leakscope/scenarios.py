"""Leakage scenario builders.

Builds the scenario features that the safe pipeline does NOT contain:
  N5 zone_avg_dur_60m             safe 60-minute zone average (availability-based)
  L5 leak_zone_avg_60m_self       the safe 60-minute average plus the trip itself
  L6 leak_zone_avg_60m_inprogress window on PICKUP time: includes in-progress trips
  L7 leak_fare_correction         next-day corrected fare of the trip itself
  L8 leak_snapshot_zone_avg       snapshot taken at the end of the day (joined on date)
  L9 leak_last_fare_zone          last completed trip's fare, looked up by dropoff
                                  time instead of fare availability time
L9 also writes a lineage table (which source trip was used and its clocks),
because its source record is not the example's own trip.
"""
from leakscope.ingestion import connect, load_config
from leakscope.temporal_join import build_lookback

WINDOW_MIN = 60


def build_scenario_features(con, examples="row_features", window_min=WINDOW_MIN):
    # running totals ordered by PICKUP time (used by the faulty L6 window)
    con.execute("DROP TABLE IF EXISTS zone_cum_pickup")
    con.execute("""
        CREATE TABLE zone_cum_pickup AS
        SELECT pu_zone, event_pickup,
               sum(duration_min) OVER w AS cum_sum,
               count(*)          OVER w AS cum_cnt
        FROM trip_times
        WINDOW w AS (PARTITION BY pu_zone ORDER BY event_pickup)
    """)
    build_lookback(con, window_min, "win_avail", examples,
                   cum="zone_cum", time_col="avail_dropoff")
    build_lookback(con, window_min, "win_pickup", examples,
                   cum="zone_cum_pickup", time_col="event_pickup")

    # L8: snapshot at the END of the prediction day
    con.execute("DROP TABLE IF EXISTS snapshot_avg")
    con.execute(f"""
        CREATE TABLE snapshot_avg AS
        SELECT ex.trip_id, c.cum_sum / c.cum_cnt AS snap_avg
        FROM (SELECT trip_id, pu_zone,
                     date_trunc('day', t_pred) + INTERVAL 1 DAY AS t_snap
              FROM {examples}) ex
        ASOF LEFT JOIN zone_cum c
          ON ex.pu_zone = c.pu_zone AND ex.t_snap >= c.avail_dropoff
    """)

    # L9: last COMPLETED trip in the zone by dropoff time (wrong clock).
    # One row per (zone, dropoff time) keeps the lookup deterministic.
    con.execute("DROP TABLE IF EXISTS lineage_leak_last_fare_zone")
    con.execute(f"""
        CREATE TABLE lineage_leak_last_fare_zone AS
        WITH src AS (
            SELECT tt.pu_zone,
                   tt.event_dropoff,
                   max(tt.trip_id)                         AS src_trip_id,
                   arg_max(c.total_amount, tt.trip_id)     AS value,
                   min(tt.avail_fare)                      AS avail_fare
            FROM trip_times tt JOIN clean_trips c USING (trip_id)
            GROUP BY tt.pu_zone, tt.event_dropoff
        )
        SELECT ex.trip_id,
               s.src_trip_id,
               s.value,
               s.event_dropoff AS event_ts,
               s.avail_fare    AS avail_ts
        FROM {examples} ex
        ASOF LEFT JOIN src s
          ON ex.pu_zone = s.pu_zone AND ex.t_pred >= s.event_dropoff
    """)

    con.execute("DROP TABLE IF EXISTS scenario_features")
    con.execute(f"""
        CREATE TABLE scenario_features AS
        SELECT r.trip_id,
               wa.avg_win AS zone_avg_dur_60m,
               (wa.sum_win + r.target_duration_min) / (wa.n_win + 1)
                   AS leak_zone_avg_60m_self,
               (wp.sum_win - r.target_duration_min) / nullif(wp.n_win - 1, 0)
                   AS leak_zone_avg_60m_inprogress,
               c.fare_amount AS leak_fare_correction,
               s.snap_avg    AS leak_snapshot_zone_avg,
               l.value       AS leak_last_fare_zone
        FROM {examples} r
        JOIN win_avail wa ON wa.trip_id = r.trip_id
        JOIN win_pickup wp ON wp.trip_id = r.trip_id
        JOIN snapshot_avg s ON s.trip_id = r.trip_id
        JOIN lineage_leak_last_fare_zone l ON l.trip_id = r.trip_id
        JOIN clean_trips c ON c.trip_id = r.trip_id
        ORDER BY r.trip_id
    """)


def build_all(cfg):
    con = connect(cfg)
    try:
        build_scenario_features(con)
    finally:
        con.close()


def verify(cfg):
    con = connect(cfg)
    try:
        print("--- Scenario features: coverage ---")
        con.sql("""
            SELECT count(*) AS n_rows,
                   round(100.0 * avg((zone_avg_dur_60m IS NULL)::INT), 2)            AS pct_null_60m,
                   round(100.0 * avg((leak_zone_avg_60m_inprogress IS NULL)::INT), 2) AS pct_null_inprog,
                   round(100.0 * avg((leak_last_fare_zone IS NULL)::INT), 3)          AS pct_null_lastfare
            FROM scenario_features
        """).show()

        print("--- Correlation with the target ---")
        con.sql("""
            SELECT
              round(corr(s.zone_avg_dur_60m, r.target_duration_min), 3)            AS r_N5_safe_60m,
              round(corr(s.leak_zone_avg_60m_self, r.target_duration_min), 3)      AS r_L5_self,
              round(corr(s.leak_zone_avg_60m_inprogress, r.target_duration_min), 3) AS r_L6_inprog,
              round(corr(s.leak_fare_correction, r.target_duration_min), 3)        AS r_L7_fare_corr,
              round(corr(s.leak_snapshot_zone_avg, r.target_duration_min), 3)      AS r_L8_snapshot,
              round(corr(s.leak_last_fare_zone, r.target_duration_min), 3)         AS r_L9_last_fare
            FROM scenario_features s JOIN row_features r USING (trip_id)
        """).show()

        print("--- L9: how often was the looked-up fare not yet available? ---")
        con.sql("""
            SELECT round(100.0 * avg((l.avail_ts > r.t_pred)::INT), 1) AS pct_not_yet_available,
                   round(avg(date_diff('second', l.event_ts, r.t_pred)) / 60.0, 2) AS mean_min_since_dropoff
            FROM lineage_leak_last_fare_zone l JOIN row_features r USING (trip_id)
            WHERE l.src_trip_id IS NOT NULL
        """).show()
    finally:
        con.close()


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Build scenario features")
    parser.add_argument("--config", default="configs/taxi.yaml")
    config = load_config(parser.parse_args().config)
    build_all(config)
    verify(config)