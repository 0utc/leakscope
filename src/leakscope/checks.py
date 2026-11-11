"""Early, readable failures for bad input and bad configuration.

PipelineError means "something about the data or the config is wrong, and here is
what and how to fix it". The command line prints it as a short message instead
of a traceback.
"""
from datetime import datetime
from pathlib import Path


class PipelineError(ValueError):
    """A problem with the input data or the configuration, explained for the user."""


REQUIRED_KEYS = {
    "data": ["raw_parquet", "zone_lookup", "database", "temp_dir"],
    "window": ["start", "end"],
    "cleaning": ["min_duration_min", "max_duration_min"],
    "duckdb": ["memory_limit", "threads"],
    "availability": ["pickup_fields_lag_min", "dropoff_fields_lag_min",
                     "distance_lag_min", "fare_lag_min",
                     "fare_correction_next_day_hour"],
    "sampling": ["n_examples", "seed", "warmup_days"],
    "ml": ["test_start", "seed", "model"],
    "analysis": ["n_boot", "seed"],
}
MODEL_KEYS = ["max_iter", "learning_rate", "max_leaf_nodes"]


def _is_int(x, minimum=None, maximum=None):
    ok = isinstance(x, int) and not isinstance(x, bool)
    if ok and minimum is not None:
        ok = x >= minimum
    if ok and maximum is not None:
        ok = x <= maximum
    return ok


def _is_number(x):
    return isinstance(x, (int, float)) and not isinstance(x, bool)


def _parse_time(value):
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


def validate_config(cfg):
    """Raise PipelineError listing EVERY problem found in the configuration."""
    if not isinstance(cfg, dict):
        raise PipelineError("Invalid configuration: the file is empty or is not a "
                            "YAML mapping (section: key: value).")
    problems = []
    for section, keys in REQUIRED_KEYS.items():
        if not isinstance(cfg.get(section), dict):
            problems.append(f"missing section '{section}'")
            continue
        for key in keys:
            if key not in cfg[section]:
                problems.append(f"missing '{section}.{key}'")
    if problems:
        raise PipelineError("Invalid configuration:\n- " + "\n- ".join(problems))

    start = _parse_time(cfg["window"]["start"])
    end = _parse_time(cfg["window"]["end"])
    if start is None:
        problems.append(f"window.start is not a date: {cfg['window']['start']!r}")
    if end is None:
        problems.append(f"window.end is not a date: {cfg['window']['end']!r}")
    if start and end and start >= end:
        problems.append("window.start must be before window.end")

    lo, hi = cfg["cleaning"]["min_duration_min"], cfg["cleaning"]["max_duration_min"]
    if not (_is_number(lo) and _is_number(hi)):
        problems.append("cleaning.min_duration_min and max_duration_min must be numbers")
    elif not 0 <= lo < hi:
        problems.append("cleaning limits must satisfy 0 <= min_duration_min < max_duration_min")

    if not _is_int(cfg["duckdb"]["threads"], 1):
        problems.append("duckdb.threads must be an integer >= 1")
    if not (isinstance(cfg["duckdb"]["memory_limit"], str) and cfg["duckdb"]["memory_limit"]):
        problems.append("duckdb.memory_limit must be text such as 1GB")

    for key in ("pickup_fields_lag_min", "dropoff_fields_lag_min", "distance_lag_min",
                "fare_lag_min"):
        if not _is_int(cfg["availability"][key], 0):
            problems.append(f"availability.{key} must be an integer >= 0 (minutes)")
    if not _is_int(cfg["availability"]["fare_correction_next_day_hour"], 0, 23):
        problems.append("availability.fare_correction_next_day_hour must be 0-23")

    s = cfg["sampling"]
    if not _is_int(s["n_examples"], 1):
        problems.append("sampling.n_examples must be an integer >= 1")
    if not _is_int(s["warmup_days"], 0):
        problems.append("sampling.warmup_days must be an integer >= 0")
    if not _is_int(s["seed"]):
        problems.append("sampling.seed must be an integer")

    ml = cfg["ml"]
    test_start = _parse_time(ml["test_start"])
    if test_start is None:
        problems.append(f"ml.test_start is not a date: {ml['test_start']!r}")
    elif start and end and _is_int(s["warmup_days"], 0):
        first_example = start.timestamp() + s["warmup_days"] * 86400
        if not first_example < test_start.timestamp() < end.timestamp():
            problems.append(
                "ml.test_start must lie after window.start + sampling.warmup_days "
                "and before window.end")
    if not _is_int(ml["seed"]):
        problems.append("ml.seed must be an integer")
    model = ml["model"]
    if not isinstance(model, dict) or any(k not in model for k in MODEL_KEYS):
        problems.append(f"ml.model needs {', '.join(MODEL_KEYS)}")
    else:
        if not _is_int(model["max_iter"], 1):
            problems.append("ml.model.max_iter must be an integer >= 1")
        if not (_is_number(model["learning_rate"]) and model["learning_rate"] > 0):
            problems.append("ml.model.learning_rate must be a number > 0")
        if not _is_int(model["max_leaf_nodes"], 2):
            problems.append("ml.model.max_leaf_nodes must be an integer >= 2")

    if not _is_int(cfg["analysis"]["n_boot"], 1):
        problems.append("analysis.n_boot must be an integer >= 1")
    if not _is_int(cfg["analysis"]["seed"]):
        problems.append("analysis.seed must be an integer")

    if problems:
        raise PipelineError("Invalid configuration:\n- " + "\n- ".join(problems))


def check_raw_files(cfg):
    """The raw Parquet file and the zone lookup must exist."""
    for key in ("raw_parquet", "zone_lookup"):
        path = Path(cfg["data"][key])
        if not path.exists():
            raise PipelineError(
                f"data.{key}: file not found: {path}\n"
                f"Download the NYC TLC file (see the README, section Data) or run "
                f"`python -m leakscope demo` to use synthetic data.")