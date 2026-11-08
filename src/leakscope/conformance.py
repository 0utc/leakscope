"""Conformance check: do stored aggregate values follow their DECLARED definition?

The auditor's R4 judges the declaration in the registry. This module closes
the loop: for a deterministic sample of examples it recomputes the aggregate
by brute force from the declaration and compares it with the stored value.
A mismatch means the pipeline computed something other than what the
registry claims, which the declaration-based rules cannot see.
"""
import math

from leakscope.registry import NAME_RE, lookback_minutes

# where each aggregate's stored values live: feature -> (table, column)
STORED_AT = {
    "zone_avg_dur_asof": ("pit_features", "zone_avg_dur_asof"),
    "zone_avg_dur_7d": ("pit_features", "zone_avg_dur_7d"),
    "zone_avg_dur_60m": ("scenario_features", "zone_avg_dur_60m"),
    "leak_zone_avg_global": ("row_features", "leak_zone_avg_global"),
    "leak_zone_avg_60m_self": ("scenario_features", "leak_zone_avg_60m_self"),
    "leak_zone_avg_60m_inprogress": ("scenario_features", "leak_zone_avg_60m_inprogress"),
}


def reference_average(con, feat, trip_id, zone, t_pred, duration):
    """Average duration over the declared window, computed the slow, obvious way."""
    clock = feat.get("window_clock", "avail_dropoff")
    if clock not in ("avail_dropoff", "event_pickup"):
        raise ValueError(f"unsupported window clock {clock!r}")
    conds, params = ["pu_zone = ?", "trip_id <> ?"], [zone, trip_id]
    if feat["window"] in ("up_to_t_pred", "lookback"):
        conds.append(f"{clock} <= CAST(? AS TIMESTAMP)")
        params.append(t_pred)
    if feat["window"] == "lookback":
        conds.append(f"{clock} > CAST(? AS TIMESTAMP) - "
                     f"INTERVAL {lookback_minutes(feat)} MINUTE")
        params.append(t_pred)
    n, s = con.execute(
        f"SELECT count(*), sum(duration_min) FROM trip_times "
        f"WHERE {' AND '.join(conds)}", params).fetchone()
    s = s or 0.0
    if feat["includes_current_row"]:
        n, s = n + 1, s + duration
    return s / n if n else None


def check_conformance(con, feat, table, column, examples="row_features",
                      n_sample=30):
    """Return a list of (trip_id, stored, expected) mismatches (empty = conforms)."""
    for ident in (table, column, examples):
        if not NAME_RE.match(ident):
            raise ValueError(f"unsafe identifier {ident!r}")
    sample = con.execute(f"""
        SELECT ex.trip_id, ex.pu_zone, ex.t_pred, tt.duration_min
        FROM {examples} ex JOIN trip_times tt USING (trip_id)
        ORDER BY hash(ex.trip_id) LIMIT {int(n_sample)}
    """).fetchall()
    mismatches = []
    for trip_id, zone, t_pred, duration in sample:
        stored = con.execute(
            f"SELECT {column} FROM {table} WHERE trip_id = ?", [trip_id]).fetchone()[0]
        expected = reference_average(con, feat, trip_id, zone, t_pred, duration)
        if expected is None or stored is None:
            same = expected is None and stored is None
        else:
            same = math.isclose(stored, expected, rel_tol=1e-6, abs_tol=1e-9)
        if not same:
            mismatches.append((trip_id, stored, expected))
    return mismatches