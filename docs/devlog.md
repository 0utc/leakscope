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

## 2026-11-07 (continued 5)
- Correlations with target (row features): pickup_hour -0.012, passengers 0.011, leak_dropoff_hour 0.001,
  leak_zone_avg_global 0.535, leak_trip_distance 0.774, leak_total_amount 0.748.
  Lesson: dropoff hour has ~0 standalone correlation but is still invalid -> audit availability, not correlation.
- Implemented temporal_join.py: ASOF LEFT JOIN on zone_cum for N2 (as-of t_pred, <=) and N3 (7-day lookback
  via difference of running totals, window (t_pred-7d, t_pred]). No history -> NULL, not 0.
- Implemented pipeline.py (one command, rebuilds from raw). Timings on 4 GB / 2-core laptop (O: SSD):
    [ok] ingestion                    8.6s
    [ok] availability times           4.0s
    [ok] prediction examples          0.7s
    [ok] feature tables               3.7s
    [ok] point-in-time features       1.6s
    Total: 18.6s
- Safe vs leaky zone average correlation: safe_asof <x>, safe_7d <y>, leaky_global <0.535>.
- Tests: 8 synthetic hand-checked cases (boundaries: exactly at t_pred included, +1s excluded,
  window left edge excluded, 1s inside included, no history -> NULL, zones isolated) + brute-force
  cross-check on 100 real examples. All passing.
- PHASE 1 GATE PASSED: data loads, safe feature built, retrieved correctly for historical predictions.
- Moved the construction of L5-L8 wrong joins to Phase 2 (built alongside expected outcomes).

## 2026-11-07 (continued 6)
- PHASE 1 GATE PASSED. Pipeline from scratch: 18.6s total (ingestion 8.6, availability 4.0,
  examples 0.7, features 3.7, PIT 1.6) on 4 GB / 2-core laptop, warm run. pytest: 9 passed.
- Coverage: 0.002% of examples have no zone history (NULL, not 0). Mean trips pooled per zone average:
  47,452 (as-of), 18,055 (7-day).
- KEY FINDING: safe zone averages r=0.534 / 0.533 vs leaky global average r=0.535. Aggregates pooling
  ~10^4 trips barely change when one trip or a few future trips are included, so the L4 leak is real
  but nearly invisible to metrics; only an availability audit catches it. Leakage magnitude scales
  inversely with group size -> L5/L6 will use small windows (zone x 60 min).
- Corrections to my own scope: R1 implies R2; L2/L7 trigger R1+R2; L1 needs metadata lying about both
  clocks to be R3-only (registry fixed); added L9 as the only R2-only scenario.

## 2026-11-07 (continued 7)
- Implemented auditor.py: R1 (event > t_pred), R2 (availability > t_pred, strict so == is valid),
  R3 (declared post-outcome) for row features, returning Finding objects with trip IDs and explanations.
- Tests: 8 hand-built fixture tests incl. boundary (== t_pred not flagged, +1s flagged),
  L1 pattern (metadata lies -> only R3), L3 pattern (R1+R2+R3), unknown clock rejected.
- Real-data result:
feature                  scen  rules fired  max rows/rule
----------------------------------------------------------
pickup_hour              N1    -                        0
pickup_dow               N1    -                        0
pu_zone                  N1    -                        0
passenger_count_filled   N1    -                        0
passenger_missing        N1    -                        0
zone_avg_dur_asof        N2    -                        0  (aggregate: R4 pending)
zone_avg_dur_7d          N3    -                        0  (aggregate: R4 pending)
leak_trip_distance       L1    R3                  100000
leak_total_amount        L2    R1,R2               100000
leak_dropoff_hour        L3    R1,R2,R3            100000
leak_zone_avg_global     L4    -                        0  (aggregate: R4 pending)

- Known gap: aggregate features (L4) are not yet judged; R4 comes next.

## 2026-11-08
- Auditor R1-R3 on real data matched predictions exactly (N1 clean; L1 R3; L2 R1+R2; L3 R1+R2+R3).
- Implemented R4: audits the DECLARED aggregate definition. Whole-dataset window via running totals;
  availability-window aggregates only violated by the own trip; pickup-time windows via a bucket join
  (bucket width = window length -> at most 2 buckets) to avoid scanning ~47k trips per example.
- Lookup features (L9) carry a lineage table (src trip + clocks) so R1/R2 audit the clocks of the
  trip actually used, not the example's own trip.
- Added snapshot clocks (event/avail_snapshot_eod) for L8; added N5 (safe 60 min) as the corrected
  counterpart of L5/L6; scenarios.py builds N5, L5-L9.
- Conformance test: stored aggregates recomputed by brute force from the declaration (6 features x 30 examples).
- Honest limitation: if a registry declaration is false AND not covered by the conformance test,
  R4 cannot see it. R4 supports duration aggregates grouped by zone only (others raise NotImplementedError).
- Real-data results:
auditor table :
    feature                        scen  rules fired    rows flagged (max)
-----------------------------------------------------------------------
pickup_hour                    N1    -                               0
pickup_dow                     N1    -                               0
pu_zone                        N1    -                               0
passenger_count_filled         N1    -                               0
passenger_missing              N1    -                               0
zone_avg_dur_asof              N2    -                               0
zone_avg_dur_7d                N3    -                               0
leak_trip_distance             L1    R3                         100000
leak_total_amount              L2    R1,R2                      100000
leak_dropoff_hour              L3    R1,R2,R3                   100000
leak_zone_avg_global           L4    R4                         100000
zone_avg_dur_60m               N5    -                               0
leak_zone_avg_60m_self         L5    R4                         100000
leak_zone_avg_60m_inprogress   L6    R4                          98211
leak_fare_correction           L7    R1,R2                      100000
leak_snapshot_zone_avg         L8    R1,R2                      100000
leak_last_fare_zone            L9    R2                          90309

secenarios tables :
--- Scenario features: coverage ---
┌────────┬──────────────┬─────────────────┬───────────────────┐
│ n_rows │ pct_null_60m │ pct_null_inprog │ pct_null_lastfare │
│ int64  │    double    │     double      │      double       │
├────────┼──────────────┼─────────────────┼───────────────────┤
│ 100000 │         0.82 │            0.75 │             0.002 │
└────────┴──────────────┴─────────────────┴───────────────────┘

--- Correlation with the target ---
┌───────────────┬───────────┬─────────────┬────────────────┬───────────────┬────────────────┐
│ r_N5_safe_60m │ r_L5_self │ r_L6_inprog │ r_L7_fare_corr │ r_L8_snapshot │ r_L9_last_fare │
│    double     │  double   │   double    │     double     │    double     │     double     │
├───────────────┼───────────┼─────────────┼────────────────┼───────────────┼────────────────┤
│         0.541 │     0.617 │       0.566 │          0.771 │         0.536 │          0.344 │
└───────────────┴───────────┴─────────────┴────────────────┴───────────────┴────────────────┘

--- L9: how often was the looked-up fare not yet available? ---
┌───────────────────────┬────────────────────────┐
│ pct_not_yet_available │ mean_min_since_dropoff │
│        double         │         double         │
├───────────────────────┼────────────────────────┤
│                  90.3 │                   3.72 │
└───────────────────────┴────────────────────────┘
- Timings on laptop:
[ok] ingestion                    9.4s
[ok] availability times           4.6s
[ok] prediction examples          0.7s
[ok] feature tables               3.7s
[ok] point-in-time features       1.6s
[ok] scenario features            9.4s
Total: 29.4s

## 2026-11-08 (continued)
- Real-data auditor run matched all predictions; pytest 34 passed; pipeline from scratch 29.4s on laptop.
- Correlations with target: N5 0.541, L5 0.617, L6 0.566, L7 0.771, L8 0.536, L9 0.344 (N2 0.534 vs L4 0.535).
  Leakage magnitude grows as the aggregated group shrinks; big-window leaks (L4, L8) are invisible to metrics.
- 0.82% of examples have an empty 60-minute window -> L5 equals the target exactly there (by construction).
- L9: 90.3% flagged; depends on the SIMULATED 2-min fare lag and zone traffic density (at lag 0 it would
  flag nothing) -> plan lag-sensitivity analysis.
- Added ground truth (EXPECTED_RULES), boundary fixtures N4/L10, scorecard (audit_scoring.py),
  conformance.py (promoted from tests), known limitation M1.
- Scorecard:
scen  what it tests                        feature                        expected    fired       ok  rows flagged
------------------------------------------------------------------------------------------------------------------
L1    final distance, metadata lies        leak_trip_distance             R3          R3          yes 100000/100000
L2    final fare of own trip               leak_total_amount              R1,R2       R1,R2       yes 100000/100000
L3    own dropoff hour                     leak_dropoff_hour              R1,R2,R3    R1,R2,R3    yes 100000/100000
L4    whole-dataset zone average           leak_zone_avg_global           R4          R4          yes 100000/100000
L5    rolling average incl. own trip       leak_zone_avg_60m_self         R4          R4          yes 100000/100000
L6    rolling avg incl. in-progress trips  leak_zone_avg_60m_inprogress   R4          R4          yes 98211/100000
L7    next-day fare correction             leak_fare_correction           R1,R2       R1,R2       yes 100000/100000
L8    end-of-day snapshot join             leak_snapshot_zone_avg         R1,R2       R1,R2       yes 100000/100000
L9    last fare by wrong clock             leak_last_fare_zone            R2          R2          yes 90309/100000
L10   available 1 second too late          boundary_late                  R2          R2          yes     3/3
N1    pickup-time fields                   pickup_hour                    -           -           yes     0/100000
N1    pickup-time fields                   pickup_dow                     -           -           yes     0/100000
N1    pickup-time fields                   pu_zone                        -           -           yes     0/100000
N1    pickup-time fields                   passenger_count_filled         -           -           yes     0/100000
N1    pickup-time fields                   passenger_missing              -           -           yes     0/100000
N2    zone avg available by t_pred         zone_avg_dur_asof              -           -           yes     0/100000
N3    7-day lookback average               zone_avg_dur_7d                -           -           yes     0/100000
N4    available at exactly t_pred          boundary_exact                 -           -           yes     0/3
N5    60-minute availability window        zone_avg_dur_60m               -           -           yes     0/100000

19 features evaluated (10 leaky, 9 valid); exact rule-set match: 19/19
Feature level: TP=10 FP=0 FN=0 TN=9  precision=1.000 recall=1.000
Rule level   : TP=15 FP=0 FN=0 TN=61  precision=1.000 recall=1.000

Conformance (stored aggregate values vs their declared definition)
  zone_avg_dur_asof              conforms
  zone_avg_dur_7d                conforms
  leak_zone_avg_global           conforms
  zone_avg_dur_60m               conforms
  leak_zone_avg_60m_self         conforms
  leak_zone_avg_60m_inprogress   conforms

Known limitation M1: aggregate whose declaration is false
  rules R1-R4 fired : none (missed)
  conformance check : 5 mismatching examples (caught)
  
- Caveat for the report: scenarios and auditor were co-designed -> scores show spec conformance,
  not general leakage detection.
- PHASE 2 GATE PASSED.