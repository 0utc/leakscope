"""Tests for R4 (aggregates) and for lookup features (lineage) on a tiny
hand-computed fixture.

Zone 1 trips (pickup -> dropoff, duration):
  1: 10:00 -> 10:30  (30)     5: 11:00 -> 11:10  (10)
  2: 10:10 -> 10:20  (10)     6: 09:00 -> 09:30  (30)
  3: 10:25 -> 11:15  (50)     8: 09:25 -> 10:45  (80)
  4: 10:40 -> 11:00  (20)
Zone 2: trip 7: 10:30 -> 10:40 (10).
Every trip is also an example, with t_pred = its pickup time. Availability
lag is 0, so avail_dropoff = dropoff. Boundary cases built in:
  * trip 8 was picked up exactly 60 min before example 3 (window left edge)
  * trip 4 is available at exactly 11:00:00 = example 5's t_pred
"""
import duckdb
import pytest

from leakscope.auditor import audit_feature, r4_unavailable_counts
from leakscope.feature_builder import create_zone_cum


@pytest.fixture
def con():
    c = duckdb.connect()
    c.execute("""
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
    create_zone_cum(c)
    c.execute("""
        CREATE TABLE row_features AS
        SELECT trip_id, pu_zone, event_pickup AS t_pred FROM trip_times
    """)
    yield c
    c.close()


def agg(**kw):
    base = dict(name="a", kind="aggregate", scenario="X", source="s",
                transformation="t", event_time="event_dropoff",
                availability_time="avail_dropoff", declared_post_outcome=False,
                group_by="pu_zone", window="lookback", lookback_minutes=60,
                window_clock="avail_dropoff", includes_current_row=False)
    base.update(kw)
    return base


def test_safe_availability_window_is_not_flagged(con):            # N5 / N3
    assert audit_feature(con, agg()) == []
    assert audit_feature(con, agg(window="up_to_t_pred")) == []    # N2


def test_including_the_current_trip_is_flagged_for_every_example(con):  # L5
    counts = r4_unavailable_counts(con, agg(includes_current_row=True))
    assert counts == {t: 1 for t in range(1, 9)}
    f = audit_feature(con, agg(includes_current_row=True))
    assert [x.rule for x in f] == ["R4"] and f[0].n_violations == 8


def test_pickup_window_counts_in_progress_trips(con):             # L6
    expected = {1: 1, 2: 2, 3: 1, 4: 1, 5: 1, 6: 0, 7: 0, 8: 1}
    got = r4_unavailable_counts(con, agg(window_clock="event_pickup"))
    assert got == expected
    # example 3: trip 8 sits exactly on the window's left edge (excluded);
    # example 5: trip 4 is available at exactly t_pred (not unavailable)
    f = audit_feature(con, agg(window_clock="event_pickup"))
    assert [x.rule for x in f] == ["R4"]
    assert f[0].n_violations == 6 and f[0].n_checked == 8
    assert f[0].affected_trip_ids == [1, 2, 3, 4, 5]               # first 5 only


def test_whole_dataset_window(con):                                # L4
    f = agg(window="whole_dataset", includes_current_row=True)
    f.pop("lookback_minutes")
    got = r4_unavailable_counts(con, f)
    # zone 1 has 7 trips; available at t_pred: ex1 ->1, ex3 ->2, ex5 ->5 (incl. boundary)
    assert got[1] == 6 and got[3] == 5 and got[5] == 2
    assert got[7] == 1                          # zone 2: only its own trip, still unavailable


def test_whole_dataset_leave_one_out_removes_only_the_own_trip(con):
    f = agg(window="whole_dataset", includes_current_row=False)
    f.pop("lookback_minutes")
    got = r4_unavailable_counts(con, f)
    assert got[3] == 4 and got[5] == 1 and got[7] == 0


def test_unsupported_definitions_are_rejected_loudly(con):
    with pytest.raises(NotImplementedError):
        r4_unavailable_counts(con, agg(group_by="pickup_hour"))
    with pytest.raises(NotImplementedError):
        r4_unavailable_counts(con, agg(availability_time="avail_fare"))


# -------------------- lookup features (lineage table) ---------------------
@pytest.fixture
def con_lookup():
    c = duckdb.connect()
    c.execute("""
        CREATE TABLE row_features AS
        SELECT * FROM (VALUES
          (1, TIMESTAMP '2024-01-10 10:00:00'),
          (2, TIMESTAMP '2024-01-10 10:00:00'),
          (3, TIMESTAMP '2024-01-10 10:00:00'),
          (4, TIMESTAMP '2024-01-10 10:00:00')
        ) t(trip_id, t_pred)
    """)
    c.execute("""
        CREATE TABLE lineage_leak_last_fare_zone AS
        SELECT * FROM (VALUES
          (1, 101, TIMESTAMP '2024-01-10 09:59:00', TIMESTAMP '2024-01-10 10:01:00'),
          (2, 102, TIMESTAMP '2024-01-10 09:50:00', TIMESTAMP '2024-01-10 09:52:00'),
          (3, 103, TIMESTAMP '2024-01-10 09:58:00', TIMESTAMP '2024-01-10 10:00:00'),
          (4, NULL, NULL, NULL)
        ) t(trip_id, src_trip_id, event_ts, avail_ts)
    """)
    yield c
    c.close()


def lookup_feat():
    return dict(name="leak_last_fare_zone", kind="row", scenario="L9",
                source="s", transformation="t", event_time="event_dropoff",
                availability_time="avail_fare", declared_post_outcome=False,
                source_record="lookup")


def test_lookup_feature_fires_r2_only_and_respects_boundary(con_lookup):  # L9
    findings = audit_feature(con_lookup, lookup_feat())
    assert [f.rule for f in findings] == ["R2"]
    f = findings[0]
    assert f.n_violations == 1 and f.n_checked == 3     # NULL source excluded
    assert f.affected_trip_ids == [1]                    # trip 3: avail == t_pred is fine
    assert "source trip 101" in f.explanation