"""Score the auditor against the labelled scenarios.

Ground truth lives in scenarios.EXPECTED_RULES (written from docs/scope.md
before the auditor was built). Two views are reported:
  * feature level: is a leaky feature flagged / a valid feature left alone?
  * rule level   : does each rule fire exactly where it should?
The scenarios and the auditor were designed together, so a perfect score shows
that the auditor implements the specification, not that it catches every kind
of leakage. Known limitations are listed separately.
"""
from leakscope import scenarios
from leakscope.auditor import audit_feature
from leakscope.conformance import STORED_AT, check_conformance
from leakscope.ingestion import connect, load_config
from leakscope.registry import load_registry

RULES = ("R1", "R2", "R3", "R4")


def _scenario_order(row):
    s = row["scenario"]
    return (s[0] == "N", int(s[1:]))


def evaluate_features(con, features, examples="row_features"):
    n_examples = con.execute(f"SELECT count(*) FROM {examples}").fetchone()[0]
    rows = []
    for f in features:
        findings = audit_feature(con, f, examples)
        rows.append(dict(
            feature=f["name"], scenario=f["scenario"],
            expected=set(scenarios.EXPECTED_RULES[f["scenario"]]),
            fired={x.rule for x in findings},
            n_flagged=max((x.n_violations for x in findings), default=0),
            n_examples=n_examples))
    return rows


def prf(tp, fp, fn):
    """Precision and recall (None when undefined)."""
    precision = tp / (tp + fp) if tp + fp else None
    recall = tp / (tp + fn) if tp + fn else None
    return precision, recall


def score(rows):
    feat = dict(tp=0, fp=0, fn=0, tn=0)
    rule = dict(tp=0, fp=0, fn=0, tn=0)
    exact = 0
    for r in rows:
        should, did = bool(r["expected"]), bool(r["fired"])
        key = ("tp" if did else "fn") if should else ("fp" if did else "tn")
        feat[key] += 1
        exact += r["expected"] == r["fired"]
        for rl in RULES:
            should, did = rl in r["expected"], rl in r["fired"]
            key = ("tp" if did else "fn") if should else ("fp" if did else "tn")
            rule[key] += 1
    return dict(feature=feat, rule=rule, exact=exact, n=len(rows))


def _fmt(x):
    return "n/a" if x is None else f"{x:.3f}"


def print_scorecard(rows, conformance, limitation):
    rows = sorted(rows, key=_scenario_order)
    print(f"{'scen':5} {'what it tests':36} {'feature':30} "
          f"{'expected':11} {'fired':11} {'ok':3} {'rows flagged':>12}")
    print("-" * 114)
    for r in rows:
        exp = ",".join(sorted(r["expected"])) or "-"
        fired = ",".join(sorted(r["fired"])) or "-"
        ok = "yes" if r["expected"] == r["fired"] else "NO"
        print(f"{r['scenario']:5} {scenarios.SCENARIO_NAMES[r['scenario']]:36} "
              f"{r['feature']:30} {exp:11} {fired:11} {ok:3} "
              f"{r['n_flagged']:>5}/{r['n_examples']:<6}")

    s = score(rows)
    fc, rc = s["feature"], s["rule"]
    fp_, fr_ = prf(fc["tp"], fc["fp"], fc["fn"])
    rp_, rr_ = prf(rc["tp"], rc["fp"], rc["fn"])
    n_leaky = sum(1 for r in rows if r["expected"])
    print(f"\n{s['n']} features evaluated ({n_leaky} leaky, {s['n'] - n_leaky} valid); "
          f"exact rule-set match: {s['exact']}/{s['n']}")
    print(f"Feature level: TP={fc['tp']} FP={fc['fp']} FN={fc['fn']} TN={fc['tn']}  "
          f"precision={_fmt(fp_)} recall={_fmt(fr_)}")
    print(f"Rule level   : TP={rc['tp']} FP={rc['fp']} FN={rc['fn']} TN={rc['tn']}  "
          f"precision={_fmt(rp_)} recall={_fmt(rr_)}")

    print("\nConformance (stored aggregate values vs their declared definition)")
    for name, mism in conformance.items():
        print(f"  {name:30} {'conforms' if not mism else f'{len(mism)} MISMATCHES'}")

    findings, mism = limitation
    print("\nKnown limitation M1: aggregate whose declaration is false")
    print(f"  rules R1-R4 fired : {','.join(sorted({x.rule for x in findings})) or 'none (missed)'}")
    print(f"  conformance check : {len(mism)} mismatching examples "
          f"({'caught' if mism else 'missed'})")


def run(config_path="configs/taxi.yaml", n_sample=30):
    cfg = load_config(config_path)
    feats = load_registry()
    con = connect(cfg)
    try:
        rows = evaluate_features(con, feats)
        conformance = {}
        for f in feats:
            if f["name"] in STORED_AT:
                table, col = STORED_AT[f["name"]]
                conformance[f["name"]] = check_conformance(con, f, table, col,
                                                           n_sample=n_sample)
    finally:
        con.close()

    fcon, ffeats, _ = scenarios.build_boundary_fixture()
    try:
        rows += evaluate_features(fcon, ffeats)
    finally:
        fcon.close()

    lcon, lfeat, ltable, lcol = scenarios.build_lying_aggregate_fixture()
    try:
        limitation = (audit_feature(lcon, lfeat),
                      check_conformance(lcon, lfeat, ltable, lcol))
    finally:
        lcon.close()
    print_scorecard(rows, conformance, limitation)
    return rows, conformance, limitation


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Score the auditor on labelled scenarios")
    parser.add_argument("--config", default="configs/taxi.yaml")
    run(parser.parse_args().config)