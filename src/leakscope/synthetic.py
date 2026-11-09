"""Deterministic synthetic taxi data with the NYC TLC yellow-taxi schema.

Used by the one-command demo and by CI so that nobody has to download the real
files. The data is NOT real: it has the same columns and a plausible structure
(longer trips take longer, rush hours are slower, fares grow with distance and
time) plus a little dirt (broken timestamps, extreme durations) so that the
cleaning rules are exercised. Same seed -> same data.
"""
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

from leakscope.ingestion import load_config

TLC_COLUMNS = [
    "VendorID", "tpep_pickup_datetime", "tpep_dropoff_datetime", "passenger_count",
    "trip_distance", "RatecodeID", "store_and_fwd_flag", "PULocationID",
    "DOLocationID", "payment_type", "fare_amount", "extra", "mta_tax", "tip_amount",
    "tolls_amount", "improvement_surcharge", "total_amount", "congestion_surcharge",
    "Airport_fee",
]

# relative pickup frequency per hour of day (quiet nights, morning and evening peaks)
HOUR_WEIGHTS = np.array([
    1.5, 1.0, 0.8, 0.6, 0.6, 0.9, 1.8, 3.2, 4.2, 3.8, 3.4, 3.5,
    3.7, 3.6, 3.8, 4.0, 4.4, 5.2, 5.4, 4.8, 4.2, 3.8, 3.2, 2.4])


def generate(out_dir, n_trips=120_000, seed=7, n_zones=60):
    """Write yellow_tripdata_synthetic.parquet and taxi_zone_lookup.csv to out_dir."""
    rng = np.random.default_rng(seed)
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)

    zones = np.arange(1, n_zones + 1)
    popularity = 1.0 / zones ** 1.0
    popularity /= popularity.sum()
    zone_len = rng.lognormal(mean=0.8, sigma=0.4, size=n_zones)   # typical miles per zone

    pu = rng.choice(zones, n_trips, p=popularity)
    day = rng.integers(0, 31, n_trips)
    hour = rng.choice(24, n_trips, p=HOUR_WEIGHTS / HOUR_WEIGHTS.sum())
    second = rng.integers(0, 3600, n_trips)
    pickup = (np.datetime64("2024-01-01T00:00:00", "us")
              + (day * 86400 + hour * 3600 + second).astype("timedelta64[s]"))
    weekday = (pickup.astype("datetime64[D]").astype(np.int64) + 4) % 7   # 1970-01-01 = Thu
    weekend = weekday >= 5

    dist = zone_len[pu - 1] * rng.lognormal(0.0, 0.55, n_trips)            # miles
    rush = ((hour >= 7) & (hour <= 9)) | ((hour >= 16) & (hour <= 19))
    speed = (14.0 * np.where(rush, 0.75, 1.0) * np.where(weekend, 1.1, 1.0)
             * rng.lognormal(0.0, 0.18, n_trips))                          # mph
    duration_min = dist / speed * 60.0 + 2.0 + rng.exponential(1.5, n_trips)
    dropoff = pickup + (duration_min * 60).astype("timedelta64[s]")

    fare = np.maximum(3.0, 3.0 + 2.8 * dist + 0.35 * duration_min
                      + rng.normal(0, 0.8, n_trips))
    tip = np.round(rng.gamma(2.0, 1.0, n_trips) * (rng.random(n_trips) < 0.6), 2)
    tolls = np.where(rng.random(n_trips) < 0.03, 6.94, 0.0)
    extra = np.where(rush, 2.5, 0.5)
    total = fare + extra + 0.5 + tip + tolls + 1.0

    passengers = pd.array(rng.integers(1, 5, n_trips), dtype="Int64")
    passengers[rng.random(n_trips) < 0.04] = pd.NA

    df = pd.DataFrame({
        "VendorID": rng.integers(1, 3, n_trips).astype("int32"),
        "tpep_pickup_datetime": pickup, "tpep_dropoff_datetime": dropoff,
        "passenger_count": passengers,
        "trip_distance": np.round(dist, 2),
        "RatecodeID": pd.array(np.ones(n_trips, dtype="int64"), dtype="Int64"),
        "store_and_fwd_flag": "N",
        "PULocationID": pu.astype("int32"),
        "DOLocationID": rng.integers(1, n_zones + 1, n_trips).astype("int32"),
        "payment_type": pd.array(rng.integers(1, 3, n_trips), dtype="Int64"),
        "fare_amount": np.round(fare, 2), "extra": extra, "mta_tax": 0.5,
        "tip_amount": tip, "tolls_amount": tolls, "improvement_surcharge": 1.0,
        "total_amount": np.round(total, 2), "congestion_surcharge": 2.5,
        "Airport_fee": 0.0,
    })[TLC_COLUMNS]

    # a little dirt, so the cleaning rules have something to do
    idx = rng.permutation(n_trips)
    n_bad = max(3, int(0.003 * n_trips))
    df.loc[idx[:n_bad], "tpep_dropoff_datetime"] = (
        df.loc[idx[:n_bad], "tpep_pickup_datetime"] - pd.Timedelta(minutes=1))
    n_long = max(3, int(0.0005 * n_trips))
    sel = idx[n_bad:n_bad + n_long]
    df.loc[sel, "tpep_dropoff_datetime"] = (
        df.loc[sel, "tpep_pickup_datetime"] + pd.Timedelta(minutes=500))
    n_old = max(2, int(0.0001 * n_trips))
    sel = idx[n_bad + n_long:n_bad + n_long + n_old]
    df.loc[sel, "tpep_pickup_datetime"] = pd.Timestamp("2002-12-31")

    parquet = out / "yellow_tripdata_synthetic.parquet"
    zones_csv = out / "taxi_zone_lookup.csv"
    df.to_parquet(parquet, index=False)
    pd.DataFrame({"LocationID": zones, "Borough": "Synthetic",
                  "Zone": [f"Zone {z}" for z in zones],
                  "service_zone": "Yellow Zone"}).to_csv(zones_csv, index=False)
    return dict(parquet=str(parquet), zones=str(zones_csv))


def make_demo_config(work_dir, base_config="configs/taxi.yaml", n_examples=20_000,
                     n_boot=500):
    """Copy the real config but point it at the synthetic data and a private
    database, so the demo never touches the real database or results."""
    work = Path(work_dir).resolve()
    work.mkdir(parents=True, exist_ok=True)
    cfg = load_config(base_config)
    cfg["data"].update(
        raw_parquet=(work / "yellow_tripdata_synthetic.parquet").as_posix(),
        zone_lookup=(work / "taxi_zone_lookup.csv").as_posix(),
        database=(work / "leakscope.duckdb").as_posix(),
        temp_dir=(work / "tmp").as_posix())
    cfg["sampling"]["n_examples"] = int(n_examples)
    cfg["analysis"]["n_boot"] = int(n_boot)
    path = work / "demo.yaml"
    path.write_text(yaml.safe_dump(cfg, sort_keys=False), encoding="utf-8")
    return path