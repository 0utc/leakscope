"""Temporal leakage auditor.

Rules (see docs/scope.md):
  R1 future event timestamp : the feature's event time is after t_pred
  R2 late availability      : the feature's availability time is after t_pred
  R3 declared post-outcome  : the feature is declared known only after the outcome
  R4 incorrect aggregation  : the aggregate uses source rows that are not yet
                              available at t_pred (including the example's own row)

R1 and R2 are evaluated per example against the clocks of the record the
feature was built from: the example's own trip (source_record: own_trip) or a
looked-up trip recorded in a lineage table (source_record: lookup).
R4 audits the DECLARED definition of an aggregate (window, window clock,
includes_current_row); tests/test_conformance.py checks that stored values
really follow that declaration. The auditor reports explicit rule violations
only; it cannot guarantee finding every form of leakage.
"""
from dataclasses import dataclass, field

from leakscope.ingestion import connect, load_config
from leakscope.registry import (
    AVAIL_CLOCKS, EVENT_CLOCKS, NAME_RE, load_registry, lookback_minutes,
)


@dataclass
class Finding:
    feature: str
    rule: str
    n_violations: int
    n_checked: int
    affected_trip_ids: list = field(default_factory=list)  # first few only
    explanation: str = ""

    @property
    def pct(self):
        return 100.0 * self.n_violations / self.n_checked if self.n_checked else 0.0


# ---------------------------------------------------------------- R1 / R2
def _clock_relation(feat, clock_key):
    """SQL giving (trip_id, src_trip_id, clock_value) for every example."""
    clock = feat[clock_key]
    allowed = EVENT_CLOCKS if clock_key == "event_time" else AVAIL_CLOCKS
    if clock not in allowed:  # also protects the f-string SQL below
        raise ValueError(f"{feat['name']}: unknown clock {clock!r}")
    if not NAME_RE.match(feat["name"]):
        raise ValueError(f"unsafe feature name {feat['name']!r}")
    if feat.get("source_record", "own_trip") == "lookup":
        col = "event_ts" if clock_key == "event_time" else "avail_ts"
        return clock, (f"SELECT trip_id, src_trip_id, {col} AS clock_value "
                       f"FROM lineage_{feat['name']} WHERE src_trip_id IS NOT NULL")
    return clock, (f"SELECT trip_id, trip_id AS src_trip_id, "
                   f"{clock} AS clock_value FROM trip_times")


def _clock_check(con, feat, clock_key, rule, examples, sample_n):
    clock, rel = _clock_relation(feat, clock_key)
    n_checked, n_bad = con.execute(f"""
        SELECT count(*), count(*) FILTER (WHERE cr.clock_value > ex.t_pred)
        FROM {examples} ex JOIN ({rel}) cr USING (trip_id)
    """).fetchone()
    if n_bad == 0:
        return None

    rows = con.execute(f"""
        SELECT ex.trip_id, ex.t_pred, cr.clock_value, cr.src_trip_id
        FROM {examples} ex JOIN ({rel}) cr USING (trip_id)
        WHERE cr.clock_value > ex.t_pred
        ORDER BY ex.trip_id LIMIT {int(sample_n)}
    """).fetchall()
    trip_id, t_pred, clock_value, src_trip_id = rows[0]
    gap_s = (clock_value - t_pred).total_seconds()
    gap = f"{gap_s:.0f} s" if gap_s < 60 else f"{gap_s / 60.0:.1f} min"
    pct = 100.0 * n_bad / n_checked
    if rule == "R1":
        what = f"describes something that happens at {clock}, after the prediction time"
    else:
        what = "only becomes available at " + clock + ", after the prediction time"
    via = f" (source trip {src_trip_id})" if src_trip_id != trip_id else ""
    explanation = (
        f"'{feat['name']}' {what}, for {n_bad} of {n_checked} examples "
        f"({pct:.1f}%). Example: trip {trip_id} is predicted at {t_pred} but "
        f"the value{via} is tied to {clock_value} ({gap} later)."
    )
    return Finding(feat["name"], rule, n_bad, n_checked,
                   [r[0] for r in rows], explanation)


# --------------------------------------------------------------------- R3
def _declared_check(con, feat, examples, sample_n):
    if not feat["declared_post_outcome"]:
        return None
    n = con.execute(f"SELECT count(*) FROM {examples}").fetchone()[0]
    ids = [r[0] for r in con.execute(
        f"SELECT trip_id FROM {examples} ORDER BY trip_id LIMIT {int(sample_n)}"
    ).fetchall()]
    explanation = (
        f"'{feat['name']}' is declared to be known only after the outcome, so "
        f"it can never be used for a prediction, whatever its timestamps say. "
        f"Affects all {n} examples."
    )
    return Finding(feat["name"], "R3", n, n, ids, explanation)


# --------------------------------------------------------------------- R4
def r4_unavailable_counts(con, feat, examples="row_features"):
    """For every example: how many source trips does the aggregate use that
    were NOT yet available at t_pred? Returns {trip_id: count}.

    A source trip is unavailable when avail_dropoff > t_pred (strict, so a
    trip available at exactly t_pred is fine). The example's own trip counts
    when includes_current_row is true.
    """
    if feat["availability_time"] != "avail_dropoff":
        raise NotImplementedError("R4 supports duration aggregates (avail_dropoff)")
    if feat.get("group_by") != "pu_zone":
        raise NotImplementedError("R4 supports group_by: pu_zone")
    window = feat["window"]
    clock = feat.get("window_clock", "avail_dropoff")
    incl = 1 if feat["includes_current_row"] else 0
    self_late = "(tt.avail_dropoff > ex.t_pred)::INT"

    if window == "whole_dataset":
        # every zone trip is in the window; unavailable = total - available
        sql = f"""
            WITH zt AS (SELECT pu_zone, max(cum_cnt) AS total
                        FROM zone_cum GROUP BY pu_zone),
            n_av AS (
                SELECT ex.trip_id, coalesce(c.cum_cnt, 0) AS n_avail
                FROM {examples} ex ASOF LEFT JOIN zone_cum c
                  ON ex.pu_zone = c.pu_zone AND ex.t_pred >= c.avail_dropoff
            )
            SELECT ex.trip_id,
                   zt.total - n_av.n_avail - (1 - {incl}) * {self_late}
            FROM {examples} ex
            JOIN trip_times tt ON tt.trip_id = ex.trip_id
            JOIN zt ON zt.pu_zone = ex.pu_zone
            JOIN n_av ON n_av.trip_id = ex.trip_id
        """
    elif clock == "avail_dropoff" and window in ("up_to_t_pred", "lookback"):
        # the window itself only contains available trips; only the
        # example's own trip can be unavailable
        sql = f"""
            SELECT ex.trip_id, {incl} * {self_late}
            FROM {examples} ex JOIN trip_times tt ON tt.trip_id = ex.trip_id
        """
    elif clock == "event_pickup" and window == "lookback":
        minutes = lookback_minutes(feat)
        secs = minutes * 60
        # bucket width == window length, so a window spans at most 2 buckets
        sql = f"""
            WITH src AS (
                SELECT trip_id AS src_id, pu_zone, event_pickup, avail_dropoff,
                       CAST(floor(epoch(event_pickup) / {secs}) AS BIGINT) AS b
                FROM trip_times
            ),
            exb AS (
                SELECT trip_id, pu_zone, t_pred,
                       CAST(floor(epoch(t_pred) / {secs}) AS BIGINT) AS b
                FROM {examples}
            ),
            cnt AS (
                SELECT exb.trip_id, count(s.src_id) AS n_win_unavail
                FROM exb CROSS JOIN (VALUES (0), (1)) k(d)
                LEFT JOIN src s
                  ON s.pu_zone = exb.pu_zone AND s.b = exb.b - k.d
                 AND s.src_id <> exb.trip_id
                 AND s.event_pickup >  exb.t_pred - INTERVAL {int(minutes)} MINUTE
                 AND s.event_pickup <= exb.t_pred
                 AND s.avail_dropoff > exb.t_pred
                GROUP BY exb.trip_id
            )
            SELECT ex.trip_id, cnt.n_win_unavail + {incl} * {self_late}
            FROM {examples} ex
            JOIN trip_times tt ON tt.trip_id = ex.trip_id
            JOIN cnt ON cnt.trip_id = ex.trip_id
        """
    else:
        raise NotImplementedError(f"R4: unsupported window {window}/{clock}")
    return {tid: int(n) for tid, n in con.execute(sql).fetchall()}


def _r4_check(con, feat, examples, sample_n):
    if feat["kind"] != "aggregate":
        return None
    counts = r4_unavailable_counts(con, feat, examples)
    bad = sorted(t for t, n in counts.items() if n > 0)
    if not bad:
        return None
    n_checked = len(counts)
    vals = [counts[t] for t in bad]
    first = bad[0]
    t_pred = con.execute(
        f"SELECT t_pred FROM {examples} WHERE trip_id = ?", [first]).fetchone()[0]
    reasons = []
    if feat["window"] == "whole_dataset":
        reasons.append("its window is the whole dataset, so it includes trips "
                       "that happen after the prediction time")
    if feat.get("window_clock") == "event_pickup":
        reasons.append("its window is defined on pickup time, so it includes "
                       "trips still in progress at the prediction time")
    if feat["includes_current_row"]:
        reasons.append("it includes the example's own trip")
    pct = 100.0 * len(bad) / n_checked
    explanation = (
        f"'{feat['name']}' aggregates over source trips that were not yet "
        f"available at prediction time, for {len(bad)} of {n_checked} examples "
        f"({pct:.1f}%): {'; '.join(reasons)}. On average {sum(vals) / len(vals):.1f} "
        f"unavailable source trips per affected example (max {max(vals)}). "
        f"Example: trip {first} is predicted at {t_pred} and its aggregate uses "
        f"{counts[first]} unavailable source trip(s)."
    )
    return Finding(feat["name"], "R4", len(bad), n_checked,
                   bad[:int(sample_n)], explanation)


# -------------------------------------------------------------- orchestration
def audit_feature(con, feat, examples="row_features", sample_n=5):
    findings = []
    if feat["kind"] == "row":
        for key, rule in (("event_time", "R1"), ("availability_time", "R2")):
            f = _clock_check(con, feat, key, rule, examples, sample_n)
            if f:
                findings.append(f)
    f = _declared_check(con, feat, examples, sample_n)
    if f:
        findings.append(f)
    f = _r4_check(con, feat, examples, sample_n)
    if f:
        findings.append(f)
    return findings


def audit_all(con, features, examples="row_features"):
    return {f["name"]: audit_feature(con, f, examples) for f in features}


def print_report(features, results):
    print(f"{'feature':30} {'scen':5} {'rules fired':14} {'rows flagged (max)':>18}")
    print("-" * 71)
    for f in features:
        fs = results[f["name"]]
        rules = ",".join(x.rule for x in fs) or "-"
        flagged = max((x.n_violations for x in fs), default=0)
        print(f"{f['name']:30} {f['scenario']:5} {rules:14} {flagged:>18}")
    print("\nExplanations")
    for f in features:
        for x in results[f["name"]]:
            print(f"- [{x.rule}] {x.explanation}")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Audit the feature registry")
    parser.add_argument("--config", default="configs/taxi.yaml")
    args = parser.parse_args()
    cfg = load_config(args.config)
    feats = load_registry()
    con = connect(cfg)
    try:
        print_report(feats, audit_all(con, feats))
    finally:
        con.close()