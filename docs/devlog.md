# Devlog

## 2026-11-07
- Set up environment on O: SSD (Python 3.13, venv, portable Git on PATH).
- Defined task: predict trip duration at pickup time.
- Defined 4 rule types (R1-R4), 8 leaky scenarios, 4 negative controls.
- Decision: availability times are simulated with configurable lags,
  because NYC TLC data does not record when fields became available.
  ## 2026-11-07 (continued)
- Downloaded yellow_tripdata_2024-01.parquet and taxi zone lookup.
- Explored data in DuckDB (experiments/explore_data.py): 2964624 rows ; 203403 suspicious rows : 870 dropoff_not_after_pickup , 18  outside_month , 60371 zero_distance , 140162 null_passengers , 1982 over_3_hours .
- Wrote docs/data_dictionary.md: classified every column as known-at-pickup or post-outcome.
- Finding: raw data has no trip ID and no availability timestamps, so both must be created/simulated.