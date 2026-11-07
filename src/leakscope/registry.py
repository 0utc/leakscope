"""Feature registry: inspectable metadata describing every feature.

Each feature declares its source, the clocks that apply to it, and whether
it is declared to be known only after the outcome (post-outcome).
"""
import yaml

EVENT_CLOCKS = {"event_pickup", "event_dropoff"}
AVAIL_CLOCKS = {
    "avail_pickup", "avail_dropoff", "avail_distance",
    "avail_fare", "avail_fare_correction",
}
WINDOWS = {"whole_dataset", "up_to_t_pred", "lookback"}
REQUIRED = [
    "name", "kind", "scenario", "source", "transformation",
    "event_time", "availability_time", "declared_post_outcome",
]


def validate(features):
    problems, seen = [], set()
    for f in features:
        name = f.get("name", "<unnamed>")
        for key in REQUIRED:
            if key not in f:
                problems.append(f"{name}: missing '{key}'")
        if name in seen:
            problems.append(f"{name}: duplicate name")
        seen.add(name)
        if f.get("kind") not in {"row", "aggregate"}:
            problems.append(f"{name}: kind must be 'row' or 'aggregate'")
        if f.get("event_time") not in EVENT_CLOCKS:
            problems.append(f"{name}: unknown event_time {f.get('event_time')!r}")
        if f.get("availability_time") not in AVAIL_CLOCKS:
            problems.append(
                f"{name}: unknown availability_time {f.get('availability_time')!r}"
            )
        if not isinstance(f.get("declared_post_outcome"), bool):
            problems.append(f"{name}: declared_post_outcome must be true/false")
        if f.get("kind") == "aggregate":
            if f.get("window") not in WINDOWS:
                problems.append(f"{name}: aggregate needs a valid 'window'")
            if not isinstance(f.get("includes_current_row"), bool):
                problems.append(f"{name}: aggregate needs 'includes_current_row'")
            if "group_by" not in f:
                problems.append(f"{name}: aggregate needs 'group_by'")
            if f.get("window") == "lookback":
                days = f.get("lookback_days")
                if not isinstance(days, int) or days <= 0:
                    problems.append(f"{name}: lookback needs positive 'lookback_days'")
    return problems


def load_registry(path="configs/features.yaml"):
    with open(path, "r", encoding="utf-8") as fh:
        features = yaml.safe_load(fh)["features"]
    problems = validate(features)
    if problems:
        raise ValueError("Invalid feature registry:\n- " + "\n- ".join(problems))
    return features


if __name__ == "__main__":
    feats = load_registry()
    header = (f"{'name':24} {'kind':10} {'scen':5} "
              f"{'availability':22} {'post_outcome':13} window")
    print(header)
    print("-" * len(header))
    for f in feats:
        print(f"{f['name']:24} {f['kind']:10} {f['scenario']:5} "
              f"{f['availability_time']:22} {str(f['declared_post_outcome']):13} "
              f"{f.get('window', '-')}")
    print(f"\n{len(feats)} features, registry valid.")