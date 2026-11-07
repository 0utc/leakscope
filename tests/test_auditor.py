"""Tests for R1-R3 on a tiny hand-built fixture.

Trips (t_pred = event_pickup of each):
  1: normal trip,              dropoff 20 min after pickup
  2: normal trip
  3: BOUNDARY: dropoff available at exactly t_pred (valid under <=)
  4: dropoff available 1 second after t_pred (a violation)
"""
import duckdb
import pytest

from leakscope.auditor import audit_feature


@pytest.fixture
def con():
    c = duckdb.connect()
    c.execute("""
        CREATE TABLE trip_times AS
        SELECT * FROM (VALUES
          (1, TIMESTAMP '2024-01-10 10:00:00', TIMESTAMP '2024-01-10 10:20:00',
              TIMESTAMP '2024-01-10 10:00:00', TIMESTAMP '2024-01-10 10:20:00'),
          (2, TIMESTAMP '2024-01-10 11:00:00', TIMESTAMP '2024-01-10 11:20:00',
              TIMESTAMP '2024-01-10 11:00:00', TIMESTAMP '2024-01-10 11:20:00'),
          (3, TIMESTAMP '2024-01-10 12:00:00', TIMESTAMP '2024-01-10 12:00:00',
              TIMESTAMP '2024-01-10 12:00:00', TIMESTAMP '2024-01-10 12:00:00'),
          (4, TIMESTAMP '2024-01-10 13:00:00', TIMESTAMP '2024-01-10 13:00:01',
              TIMESTAMP '2024-01-10 13:00:00', TIMESTAMP '2024-01-10 13:00:01')
        ) t(trip_id, event_pickup, event_dropoff, avail_pickup, avail_dropoff)
    """)
    c.execute("""
        CREATE TABLE row_features AS
        SELECT trip_id, event_pickup AS t_pred FROM trip_times
    """)
    yield c
    c.close()


def feat(**kw):
    base = dict(name="f", kind="row", scenario="X", source="s",
                transformation="t", event_time="event_pickup",
                availability_time="avail_pickup", declared_post_outcome=False)
    base.update(kw)
    return base


def rules(findings):
    return [f.rule for f in findings]


def test_valid_feature_not_flagged(con):                       # N1
    assert audit_feature(con, feat()) == []


def test_r2_flags_late_availability_and_respects_boundary(con):
    findings = audit_feature(con, feat(availability_time="avail_dropoff"))
    assert rules(findings) == ["R2"]
    f = findings[0]
    assert f.n_violations == 3 and f.n_checked == 4
    assert f.affected_trip_ids == [1, 2, 4]   # trip 3 (== t_pred) is NOT flagged


def test_one_second_late_is_flagged_exactly_at_t_pred_is_not(con):  # N4
    f = audit_feature(con, feat(availability_time="avail_dropoff"))[0]
    assert 4 in f.affected_trip_ids and 3 not in f.affected_trip_ids


def test_r1_fires_on_future_event_even_if_availability_lies(con):
    findings = audit_feature(
        con, feat(event_time="event_dropoff", availability_time="avail_pickup"))
    assert rules(findings) == ["R1"]
    assert findings[0].affected_trip_ids == [1, 2, 4]


def test_r3_catches_metadata_that_lies_about_both_clocks(con):  # L1
    findings = audit_feature(con, feat(declared_post_outcome=True))
    assert rules(findings) == ["R3"]
    assert findings[0].n_violations == 4


def test_honest_timestamps_and_declaration_fire_all_three(con):  # L3
    findings = audit_feature(con, feat(
        event_time="event_dropoff", availability_time="avail_dropoff",
        declared_post_outcome=True))
    assert rules(findings) == ["R1", "R2", "R3"]


def test_explanation_names_the_rule_and_an_example(con):
    f = audit_feature(con, feat(availability_time="avail_dropoff"))[0]
    assert "avail_dropoff" in f.explanation and "trip 1" in f.explanation


def test_unknown_clock_name_is_rejected(con):
    with pytest.raises(ValueError):
        audit_feature(con, feat(availability_time="avail_typo"))