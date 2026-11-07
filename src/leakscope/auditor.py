"""Temporal leakage auditor.

Rules (see docs/scope.md):
  R1 future event timestamp : the feature's event time is after t_pred
  R2 late availability      : the feature's availability time is after t_pred
  R3 declared post-outcome  : the feature is declared known only after the outcome
  R4 incorrect aggregation  : (implemented in the next step)

R1 and R2 are evaluated per example against the example's own source record,
so they apply to row-level features. The auditor reports explicit rule
violations only; it cannot guarantee finding every form of leakage.
"""
from dataclasses import dataclass, field

from leakscope.ingestion import connect, load_config
from leakscope.registry import AVAIL_CLOCKS, EVENT_CLOCKS, load_registry


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


def _clock_check(con, feat, clock_key, rule, examples, sample_n):
    clock = feat[clock_key]
    allowed = EVENT_CLOCKS if clock_key == "event_time" else AVAIL_CLOCKS
    if clock not in allowed:  # also protects the f-string SQL below
        raise ValueError(f"{feat['name']}: unknown clock {clock!r}")

    n_checked, n_bad = con.execute(f"""
        SELECT count(*), count(*) FILTER (WHERE tt.{clock} > ex.t_pred)
        FROM {examples} ex JOIN trip_times tt USING (trip_id)
    """).fetchone()
    if n_bad == 0:
        return None

    rows = con.execute(f"""
        SELECT ex.trip_id, ex.t_pred, tt.{clock}
        FROM {examples} ex JOIN trip_times tt USING (trip_id)
        WHERE tt.{clock} > ex.t_pred
        ORDER BY ex.trip_id LIMIT {int(sample_n)}
    """).fetchall()
    trip_id, t_pred, clock_value = rows[0]
    gap_min = round((clock_value - t_pred).total_seconds() / 60.0, 1)
    pct = 100.0 * n_bad / n_checked
    if rule == "R1":
        what = (f"describes something that happens at {clock}, after the "
                f"prediction time")
    else:
        what = (f"only becomes available at {clock}, after the prediction time")
    explanation = (
        f"'{feat['name']}' {what}, for {n_bad} of {n_checked} examples "
        f"({pct:.1f}%). Example: trip {trip_id} is predicted at {t_pred} but "
        f"the value is tied to {clock_value} ({gap_min} min later)."
    )
    return Finding(feat["name"], rule, n_bad, n_checked,
                   [r[0] for r in rows], explanation)


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
    return findings


def audit_all(con, features, examples="row_features"):
    return {f["name"]: audit_feature(con, f, examples) for f in features}


def print_report(features, results):
    print(f"{'feature':24} {'scen':5} {'rules fired':12} {'max rows/rule':>13}")
    print("-" * 58)
    for f in features:
        fs = results[f["name"]]
        rules = ",".join(x.rule for x in fs) or "-"
        flagged = max((x.n_violations for x in fs), default=0)
        note = "  (aggregate: R4 pending)" if f["kind"] == "aggregate" else ""
        print(f"{f['name']:24} {f['scenario']:5} {rules:12} {flagged:>13}{note}")
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