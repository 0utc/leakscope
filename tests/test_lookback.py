"""Tests for the generic short-window lookup used by the 60-minute features."""
import duckdb
import pytest

from leakscope.feature_builder import create_zone_cum
from leakscope.temporal_join import build_lookback


@pytest.fixture
def con():
    c = duckdb.connect()
    c.execute("""
        CREATE TABLE trip_times AS
        SELECT pu_zone, avail_dropoff, duration_min::DOUBLE AS duration_min
        FROM (VALUES
            (1, TIMESTAMP '2024-01-09 09:00:00', 10),
            (1, TIMESTAMP '2024-01-09 09:30:00', 20),
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
            (2, 2, TIMESTAMP '2024-01-09 09:00:00'),
            (3, 3, TIMESTAMP '2024-01-09 10:00:00')
        ) t(trip_id, pu_zone, t_pred)
    """)
    build_lookback(c, 60, "lb", examples="examples")
    yield c
    c.close()


def row(con, trip_id):
    return con.execute(
        "SELECT n_win, sum_win, avg_win FROM lb WHERE trip_id = ?", [trip_id]
    ).fetchone()


def test_window_is_left_open_right_closed(con):
    # example 1: window (09:00:00, 10:00:00] -> trips at 09:30 and 10:00:00;
    # the 09:00:00 trip sits on the left edge (excluded), 10:00:01 is late
    n, s, a = row(con, 1)
    assert n == 2 and s == pytest.approx(60.0) and a == pytest.approx(30.0)


def test_trip_exactly_at_t_pred_is_included_in_its_own_zone_only(con):
    n, s, a = row(con, 2)          # zone 2, window (08:00, 09:00] -> the 09:00:00 trip
    assert n == 1 and a == pytest.approx(60.0)


def test_unknown_zone_gives_null_average(con):
    n, s, a = row(con, 3)
    assert n == 0 and s == 0 and a is None


def test_unsupported_time_column_is_rejected(con):
    with pytest.raises(ValueError):
        build_lookback(con, 60, "lb2", examples="examples", time_col="avail_typo")