# Devlog

## 2026-11-07
- Set up environment on O: SSD (Python 3.13, venv, portable Git on PATH).
- Defined task: predict trip duration at pickup time.
- Defined 4 rule types (R1-R4), 8 leaky scenarios, 4 negative controls.
- Decision: availability times are simulated with configurable lags,
  because NYC TLC data does not record when fields became available.

## 2026-11-07 (continued)
- Downloaded yellow_tripdata_2024-01.parquet and taxi zone lookup.
- Explored data in DuckDB (experiments/explore_data.py)
- Wrote docs/data_dictionary.md: classified every column as known-at-pickup or post-outcome.
- Finding: raw data has no trip ID and no availability timestamps, so both must be created/simulated.

## 2026-11-07 (continued 2)
- Raw data findings: 2,964,624 rows; pickups from 2002 to Feb 2024 (18 outside Jan 2024);
  870 dropoff<=pickup; 1,982 trips >3h (max 9,455 min); 60,371 zero-distance; 140,162 null passenger_count.
- Cleaning principle: only reject for timestamp integrity or target sanity, never for other
  post-outcome fields (would be hidden selection on the future). Limitation: results apply to 1-180 min trips.
- Implemented ingestion.py: trip_id from file_row_number, duration_min, reject_reason labels, clean_trips view.
- Thresholds (1 and 180 min) are judgment calls, stored in configs/taxi.yaml.
- Cleaning results: 
┌───────────────────────┬─────────┬────────┐
│       decision        │    n    │  pct   │
│        varchar        │  int64  │ double │
├───────────────────────┼─────────┼────────┤
│ KEPT                  │ 2927502 │ 98.748 │
│ duration_too_short    │   34251 │  1.155 │
│ duration_too_long     │    1983 │  0.067 │
│ nonpositive_duration  │     870 │  0.029 │
│ pickup_outside_window │      18 │  0.001 │
└───────────────────────┴─────────┴────────┘

## 2026-11-07 (continued 3)
- Cleaning result: 2,927,502 kept; rejected 37,122 (1.25%): 34,251 too short (<1 min),
  1,983 too long (>180 min), 870 nonpositive, 18 outside window. Median duration 11.8 min after cleaning.
- Biggest rejection category is too-short trips (1.155%), likely cancellations or meter glitches.
- Implemented temporal_model.py: trip_times (event + simulated availability) and prediction_examples.
- Design: 100k examples sampled deterministically (hash of trip_id + seed 42) from trips on/after Jan 8
  (7-day warm-up for lookback features); history for aggregates uses ALL clean trips, as a live system would.
- Verified: clock ordering holds; a trip's own outcome fields are 100% unavailable at its t_pred;
  pickup fields are usable at the boundary (<=), unusable with strict <.

## 2026-11-07 (continued 4)
- Temporal model verified: clock ordering OK; own outcome fields 100% unavailable at t_pred.
- Spec fixes in scope.md: L1 now uses a mislabeled (optimistic) timestamp so only R3 catches it;
  L3 expects R2+R3; L6 reclassified R2 -> R4 (aggregate over in-progress trips); N3 window is
  (t_pred - 7d, t_pred] to be consistent with <=. Scenarios can trigger multiple rules.
- Built configs/features.yaml (feature registry, 11 features) and registry.py with validation.
- Built zone_cum (running totals per zone by availability time) for fast point-in-time lookups,
  zone_global and row_features (valid + leaky row-level features).
- Correlations with target:
┌───────────────┬──────────────┬───────────────────┬──────────────────┬─────────────────┬───────────────┐
│ r_pickup_hour │ r_passengers │ r_leak_dropoff_hr │ r_leak_zone_glob │ r_leak_distance │ r_leak_amount │
│    double     │    double    │      double       │      double      │     double      │    double     │
├───────────────┼──────────────┼───────────────────┼──────────────────┼─────────────────┼───────────────┤
│        -0.012 │        0.011 │             0.001 │            0.535 │           0.774 │         0.748 │
└───────────────┴──────────────┴───────────────────┴──────────────────┴─────────────────┴───────────────┘

