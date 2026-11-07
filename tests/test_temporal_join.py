"""Correctness tests for point-in-time retrieval.

Synthetic fixture (zone 1 trips, availability -> duration):
  Jan 1 10:00:00 -> 10
  Jan 2 10:00:00 -> 20   (exactly 7 days before Jan 9 10:00:00)
  Jan 5 12:00:00 -> 30
  Jan 9 10:00:00 -> 40   (exactly at example 1's t_pred)
  Jan 9 10:00:01 -> 50   (one second after example 1's t_pred)
Zone 2: Jan 9 09:00:00 -> 60.   Zone 3: no trips at all.
"""
from pathlib import Path

import duckdb
import pytest

from leakscope.feature_builder import create_zone_cum
from leakscope.temporal_join import build_pit_features


@pytest.fixture
def con():
    c = duckdb.connect()
    c.execute("""
        CREATE TABLE trip_times AS
        SELECT pu_zone, avail_dropoff, duration_min::DOUBLE AS duration_min
        FROM (VALUES
            (1, TIMESTAMP '2024-01-01 10:00:00', 10),
            (1, TIMESTAMP '2024-01-02 10:00:00', 20),
            (1, TIMESTAMP '2024-01-05 12:00:00', 30),
            (1, TIMESTAMP '2024-01-09 10:00:00', 40),
            (1, TIMESTAMP '2024-01-09 10:00:01', 50),
            (2, TIMESTAMP '2024-01-09 09:00:00', 60)
        ) t(pu_zone, avail_dropoff, duration_min)
    """)
    create_zone_cum(c)
    c.execute("""
        CREATE TABLE examples AS
        SELECT * FROM (VALUES
            (1, 1, TIMESTAMP '2024-01-09 10:00:00'),
            (2, 1, TIMESTAMP '2024-01-09 09:59:59'),
            (3, 1, TIMESTAMP '2024-01-01 09:00:00'),
            (4, 2, TIMESTAMP '2024-01-09 09:00:00'),
            (5, 3, TIMESTAMP '2024-01-09 09:00:00')
        ) t(trip_id, pu_zone, t_pred)
    """)
    build_pit_features(c, examples="examples", lookback_days=7)
    yield c
    c.close()


def get(con, trip_id):
    return con.execute("""
        SELECT n_asof, zone_avg_dur_asof, n_lookback, zone_avg_dur_7d
        FROM pit_features WHERE trip_id = ?
    """, [trip_id]).fetchone()


def test_one_output_row_per_example(con):
    n_out = con.execute("SELECT count(*) FROM pit_features").fetchone()[0]
    n_ids = con.execute("SELECT count(DISTINCT trip_id) FROM pit_features").fetchone()[0]
    assert n_out == 5 and n_ids == 5


def test_trip_available_exactly_at_t_pred_is_included(con):  # N4
    n, avg, _, _ = get(con, 1)
    assert n == 4                      # 10, 20, 30, 40 (the 10:00:00 trip counts)
    assert avg == pytest.approx(25.0)


def test_trip_available_one_second_after_t_pred_is_excluded(con):
    n, avg, _, _ = get(con, 2)         # t_pred 09:59:59, the 10:00:00 trip is late
    assert n == 3
    assert avg == pytest.approx(20.0)


def test_lookback_window_edges(con):
    # (Jan 2 10:00:00, Jan 9 10:00:00]: left edge excluded, right edge included
    _, _, n, avg = get(con, 1)
    assert n == 2                      # 30 and 40; the 20 sits exactly on the left edge
    assert avg == pytest.approx(35.0)


def test_lookback_includes_trip_one_second_inside_window(con):
    _, _, n, avg = get(con, 2)         # window (Jan 2 09:59:59, Jan 9 09:59:59]
    assert n == 2                      # 20 (one second inside) and 30
    assert avg == pytest.approx(25.0)


def test_no_history_gives_null_not_zero(con):
    n, avg, n_lb, avg_lb = get(con, 3)
    assert n == 0 and avg is None
    assert n_lb == 0 and avg_lb is None


def test_zones_do_not_mix(con):
    n, avg, n_lb, avg_lb = get(con, 4)
    assert n == 1 and avg == pytest.approx(60.0)
    assert n_lb == 1 and avg_lb == pytest.approx(60.0)


def test_unknown_zone_gives_null(con):
    n, avg, n_lb, avg_lb = get(con, 5)
    assert n == 0 and avg is None
    assert n_lb == 0 and avg_lb is None


# ---------- Cross-check against a slow, obviously correct scan ----------
DB = Path("data/processed/leakscope.duckdb")


def _open_real_db():
    if not DB.exists():
        pytest.skip("local database not built (run python -m leakscope.pipeline)")
    c = duckdb.connect(str(DB), read_only=True)
    c.execute("SET memory_limit='1GB'")
    try:
        c.execute("SELECT 1 FROM pit_features LIMIT 1")
    except duckdb.Error:
        c.close()
        pytest.skip("pit_features not built")
    return c


def test_matches_brute_force_scan_on_real_data():
    c = _open_real_db()
    try:
        sample = c.execute("""
            SELECT trip_id, pu_zone, t_pred FROM row_features
            ORDER BY hash(trip_id) LIMIT 100
        """).fetchall()
        for trip_id, zone, t_pred in sample:
            n_all, avg_all = c.execute("""
                SELECT count(*), avg(duration_min) FROM trip_times
                WHERE pu_zone = ? AND avail_dropoff <= CAST(? AS TIMESTAMP)
            """, [zone, t_pred]).fetchone()
            n_win, avg_win = c.execute("""
                SELECT count(*), avg(duration_min) FROM trip_times
                WHERE pu_zone = ?
                  AND avail_dropoff <= CAST(? AS TIMESTAMP)
                  AND avail_dropoff >  CAST(? AS TIMESTAMP) - INTERVAL 7 DAY
            """, [zone, t_pred, t_pred]).fetchone()
            got = c.execute("""
                SELECT n_asof, zone_avg_dur_asof, n_lookback, zone_avg_dur_7d
                FROM pit_features WHERE trip_id = ?
            """, [trip_id]).fetchone()
            assert got[0] == n_all and got[2] == n_win
            for expected, actual in ((avg_all, got[1]), (avg_win, got[3])):
                if expected is None:
                    assert actual is None
                else:
                    assert actual == pytest.approx(expected, rel=1e-6)
    finally:
        c.close()