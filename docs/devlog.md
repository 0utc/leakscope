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
