"""Point-in-time feature retrieval.

For every prediction example, look up zone statistics using ONLY trips whose
(simulated) availability time is <= t_pred.

Technique: zone_cum holds running totals per zone ordered by availability
time. An ASOF join finds, for each example, the last running-total row at or
before t_pred. A lookback average is the difference between the running
totals now and at (t_pred - lookback), i.e. over (t_pred - lookback, t_pred].
"""
from leakscope.ingestion import connect, load_config
from leakscope.registry import load_registry


def build_pit_features(con, examples="row_features", cum="zone_cum",
                       lookback_days=7, out="pit_features"):
    con.execute(f"DROP TABLE IF EXISTS {out}")
    con.execute(f"""
        CREATE TABLE {out} AS
        WITH ex AS (
            SELECT trip_id, pu_zone, t_pred,
                   t_pred - INTERVAL {int(lookback_days)} DAY AS t_prev
            FROM {examples}
        ),
        now_totals AS (
            SELECT ex.trip_id, c.cum_sum AS s_now, c.cum_cnt AS n_now
            FROM ex ASOF LEFT JOIN {cum} c
              ON ex.pu_zone = c.pu_zone AND ex.t_pred >= c.avail_dropoff
        ),
        prev_totals AS (
            SELECT ex.trip_id, c.cum_sum AS s_prev, c.cum_cnt AS n_prev
            FROM ex ASOF LEFT JOIN {cum} c
              ON ex.pu_zone = c.pu_zone AND ex.t_prev >= c.avail_dropoff
        )
        SELECT
            n.trip_id,
            coalesce(n.n_now, 0)                          AS n_asof,
            n.s_now / n.n_now                             AS zone_avg_dur_asof,
            coalesce(n.n_now, 0) - coalesce(p.n_prev, 0)  AS n_lookback,
            (coalesce(n.s_now, 0) - coalesce(p.s_prev, 0))
              / nullif(coalesce(n.n_now, 0) - coalesce(p.n_prev, 0), 0)
                                                          AS zone_avg_dur_7d
        FROM now_totals n
        JOIN prev_totals p USING (trip_id)
    """)


def build_all(cfg):
    lookback = next(
        f["lookback_days"] for f in load_registry()
        if f["name"] == "zone_avg_dur_7d"
    )
    con = connect(cfg)
    try:
        build_pit_features(con, lookback_days=lookback)
    finally:
        con.close()


def verify(cfg):
    con = connect(cfg)
    try:
        print("--- Point-in-time features: coverage ---")
        con.sql("""
            SELECT count(*) AS n_rows,
                   round(100.0 * avg((zone_avg_dur_asof IS NULL)::INT), 3) AS pct_null_asof,
                   round(100.0 * avg((zone_avg_dur_7d  IS NULL)::INT), 3) AS pct_null_7d,
                   round(avg(n_asof), 0)     AS mean_trips_asof,
                   round(avg(n_lookback), 0) AS mean_trips_7d
            FROM pit_features
        """).show()

        print("--- Safe vs leaky zone average: correlation with target ---")
        con.sql("""
            SELECT round(corr(p.zone_avg_dur_asof, r.target_duration_min), 3) AS r_safe_asof,
                   round(corr(p.zone_avg_dur_7d,  r.target_duration_min), 3) AS r_safe_7d,
                   round(corr(r.leak_zone_avg_global, r.target_duration_min), 3) AS r_leak_global
            FROM pit_features p JOIN row_features r USING (trip_id)
        """).show()
    finally:
        con.close()


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Build point-in-time features")
    parser.add_argument("--config", default="configs/taxi.yaml")
    args = parser.parse_args()
    config = load_config(args.config)
    build_all(config)
    verify(config)