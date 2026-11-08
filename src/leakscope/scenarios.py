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
import duckdb

from leakscope.feature_builder import create_zone_cum
from leakscope.ingestion import connect, load_config
from leakscope.temporal_join import build_lookback

WINDOW_MIN = 60

# Ground truth: the rules each scenario MUST trigger (mirrors docs/scope.md, section 6).
# An empty set means the auditor must stay silent (negative control).
EXPECTED_RULES = {
    "L1": {"R3"}, "L2": {"R1", "R2"}, "L3": {"R1", "R2", "R3"},
    "L4": {"R4"}, "L5": {"R4"}, "L6": {"R4"},
    "L7": {"R1", "R2"}, "L8": {"R1", "R2"}, "L9": {"R2"}, "L10": {"R2"},
    "N1": set(), "N2": set(), "N3": set(), "N4": set(), "N5": set(),
}

SCENARIO_NAMES = {
    "L1": "final distance, metadata lies", "L2": "final fare of own trip",
    "L3": "own dropoff hour", "L4": "whole-dataset zone average",
    "L5": "rolling average incl. own trip", "L6": "rolling avg incl. in-progress trips",
    "L7": "next-day fare correction", "L8": "end-of-day snapshot join",
    "L9": "last fare by wrong clock", "L10": "available 1 second too late",
    "N1": "pickup-time fields", "N2": "zone avg available by t_pred",
    "N3": "7-day lookback average", "N4": "available at exactly t_pred",
    "N5": "60-minute availability window",
}

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

# ------------------------------------------------------------------ fixtures
def _feature(name, scenario, **kw):
    base = dict(name=name, kind="row", scenario=scenario, source="fixture",
                transformation="fixture", event_time="event_dropoff",
                availability_time="avail_fare", declared_post_outcome=False)
    base.update(kw)
    return base


def build_boundary_fixture():
    """In-memory micro dataset for the boundary scenarios (deterministic).

    N4  : the source record becomes available at EXACTLY t_pred -> valid (<=)
    L10 : the source record becomes available ONE SECOND after t_pred -> R2
    Returns (connection, features, n_examples).
    """
    con = duckdb.connect()
    con.execute("""
        CREATE TABLE row_features AS
        SELECT * FROM (VALUES
            (1, TIMESTAMP '2024-01-10 10:00:00'),
            (2, TIMESTAMP '2024-01-10 11:30:00'),
            (3, TIMESTAMP '2024-01-10 23:59:59')
        ) t(trip_id, t_pred)
    """)
    for table, shift in (("boundary_exact", "0 SECOND"), ("boundary_late", "1 SECOND")):
        con.execute(f"""
            CREATE TABLE lineage_{table} AS
            SELECT trip_id, 100 + trip_id AS src_trip_id,
                   t_pred - INTERVAL 2 MINUTE AS event_ts,
                   t_pred + INTERVAL {shift}  AS avail_ts
            FROM row_features
        """)
    features = [
        _feature("boundary_exact", "N4", source_record="lookup"),
        _feature("boundary_late", "L10", source_record="lookup"),
    ]
    return con, features, 3


def build_lying_aggregate_fixture():
    """Known limitation M1: an aggregate that DECLARES an availability-based
    window but whose stored values were built from a pickup-time window.

    Declaration-based rules see nothing wrong; only the conformance check can
    notice. Returns (connection, feature, table, column).
    """
    con = duckdb.connect()
    con.execute("""
        CREATE TABLE trip_times AS
        SELECT trip_id, pu_zone,
               CAST(date_diff('second', p, d) / 60.0 AS DOUBLE) AS duration_min,
               p AS event_pickup, d AS event_dropoff, d AS avail_dropoff
        FROM (VALUES
          (1, 1, TIMESTAMP '2024-01-10 10:00:00', TIMESTAMP '2024-01-10 10:30:00'),
          (2, 1, TIMESTAMP '2024-01-10 10:10:00', TIMESTAMP '2024-01-10 10:20:00'),
          (3, 1, TIMESTAMP '2024-01-10 10:25:00', TIMESTAMP '2024-01-10 11:15:00'),
          (4, 1, TIMESTAMP '2024-01-10 10:40:00', TIMESTAMP '2024-01-10 11:00:00'),
          (5, 1, TIMESTAMP '2024-01-10 11:00:00', TIMESTAMP '2024-01-10 11:10:00'),
          (6, 1, TIMESTAMP '2024-01-10 09:00:00', TIMESTAMP '2024-01-10 09:30:00'),
          (7, 2, TIMESTAMP '2024-01-10 10:30:00', TIMESTAMP '2024-01-10 10:40:00'),
          (8, 1, TIMESTAMP '2024-01-10 09:25:00', TIMESTAMP '2024-01-10 10:45:00')
        ) t(trip_id, pu_zone, p, d)
    """)
    create_zone_cum(con)
    con.execute("""
        CREATE TABLE row_features AS
        SELECT trip_id, pu_zone, event_pickup AS t_pred FROM trip_times
    """)
    # the "wrong" implementation: window on PICKUP time, excluding the own trip
    con.execute("""
        CREATE TABLE stored_lying AS
        SELECT ex.trip_id,
               (SELECT avg(s.duration_min) FROM trip_times s
                WHERE s.pu_zone = ex.pu_zone AND s.trip_id <> ex.trip_id
                  AND s.event_pickup >  ex.t_pred - INTERVAL 60 MINUTE
                  AND s.event_pickup <= ex.t_pred) AS hidden_avg
        FROM row_features ex
    """)
    feature = dict(
        name="hidden_inprogress_avg", kind="aggregate", scenario="M1",
        source="fixture", transformation="declared: availability-based 60 min window",
        event_time="event_dropoff", availability_time="avail_dropoff",
        declared_post_outcome=False, group_by="pu_zone", window="lookback",
        lookback_minutes=60, window_clock="avail_dropoff",
        includes_current_row=False)
    return con, feature, "stored_lying", "hidden_avg"

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Build scenario features")
    parser.add_argument("--config", default="configs/taxi.yaml")
    config = load_config(parser.parse_args().config)
    build_all(config)
    verify(config)