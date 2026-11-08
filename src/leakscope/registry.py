"""Feature registry: inspectable metadata describing every feature.

Each feature declares its source, the clocks that apply to it, and whether
it is declared to be known only after the outcome (post-outcome).
Aggregates also declare exactly which source rows they cover.
"""
import re

import yaml

EVENT_CLOCKS = {"event_pickup", "event_dropoff", "event_snapshot_eod"}
AVAIL_CLOCKS = {
    "avail_pickup", "avail_dropoff", "avail_distance",
    "avail_fare", "avail_fare_correction", "avail_snapshot_eod",
}
WINDOWS = {"whole_dataset", "up_to_t_pred", "lookback"}
WINDOW_CLOCKS = {"avail_dropoff", "event_pickup"}
SOURCE_RECORDS = {"own_trip", "lookup"}
NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")
REQUIRED = [
    "name", "kind", "scenario", "source", "transformation",
    "event_time", "availability_time", "declared_post_outcome",
]


def lookback_minutes(feat):
    """Lookback length in minutes (from lookback_minutes or lookback_days)."""
    if feat.get("lookback_minutes") is not None:
        return int(feat["lookback_minutes"])
    if feat.get("lookback_days") is not None:
        return int(feat["lookback_days"]) * 1440
    return None


def _positive_int(value):
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def validate(features):
    problems, seen = [], set()
    for f in features:
        name = f.get("name", "<unnamed>")
        for key in REQUIRED:
            if key not in f:
                problems.append(f"{name}: missing '{key}'")
        if not NAME_RE.match(str(name)):
            problems.append(f"{name}: name must match [a-z][a-z0-9_]*")
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
        if f.get("source_record", "own_trip") not in SOURCE_RECORDS:
            problems.append(f"{name}: source_record must be own_trip or lookup")
        if f.get("source_record") == "lookup" and f.get("kind") != "row":
            problems.append(f"{name}: source_record 'lookup' is for row features")
        if f.get("kind") == "aggregate":
            if f.get("window") not in WINDOWS:
                problems.append(f"{name}: aggregate needs a valid 'window'")
            if not isinstance(f.get("includes_current_row"), bool):
                problems.append(f"{name}: aggregate needs 'includes_current_row'")
            if "group_by" not in f:
                problems.append(f"{name}: aggregate needs 'group_by'")
            clock = f.get("window_clock", "avail_dropoff")
            if clock not in WINDOW_CLOCKS:
                problems.append(f"{name}: unknown window_clock {clock!r}")
            if clock == "event_pickup" and f.get("window") != "lookback":
                problems.append(
                    f"{name}: window_clock event_pickup requires window 'lookback'")
            if f.get("window") == "lookback":
                days, mins = f.get("lookback_days"), f.get("lookback_minutes")
                if (days is None) == (mins is None):
                    problems.append(
                        f"{name}: lookback needs exactly one of "
                        f"lookback_days / lookback_minutes")
                elif not _positive_int(days if days is not None else mins):
                    problems.append(f"{name}: lookback length must be a positive integer")
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
    header = (f"{'name':30} {'kind':10} {'scen':5} "
              f"{'availability':22} {'post_outcome':13} window")
    print(header)
    print("-" * len(header))
    for f in feats:
        print(f"{f['name']:30} {f['kind']:10} {f['scenario']:5} "
              f"{f['availability_time']:22} {str(f['declared_post_outcome']):13} "
              f"{f.get('window', '-')}")
    print(f"\n{len(feats)} features, registry valid.")