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

## 2026-11-08 (continued 2)
- Phase 2 gate passed: scorecard 19/19, feature-level TP=10 FP=0 FN=0 TN=9, rule-level TP=15 FP=0 FN=0 TN=61,
  M1 missed by R1-R4 but caught by conformance (5 mismatches). 41 tests passed in 15.6s.
- Phase 3 design: purged chronological split (train = labels available by cutoff, test = predicted at/after
  cutoff, in-progress trips purged). Test = last 7 days (Jan 25-31), train = Jan 8-24.
  HistGradientBoostingRegressor (no early stopping: its random validation split would leak inside training;
  pu_zone numeric because >255 categories). Leaky models = corrected set + ONE leaky feature; plus ALL.
  The run refuses to start if the auditor disagrees with the scenario labels.
- Results: train 69,899 | test 30,066 | purged 35 (trips in progress at the cutoff).
  Constant-median baseline (11.70 min): MAE 7.787, RMSE 12.399.

  | experiment | MAE | RMSE | dMAE% | dRMSE% | rows flagged |
  |---|---|---|---|---|---|
  | corrected (valid features only) | 6.809 | 9.848 | 0.0 | 0.0 | - |
  | + L1 leak_trip_distance | 3.335 | 5.526 | -51.0 | -43.9 | 100.0% |
  | + L2 leak_total_amount | 2.690 | 5.128 | -60.5 | -47.9 | 100.0% |
  | + L3 leak_dropoff_hour | 6.368 | 9.073 | -6.5 | -7.9 | 100.0% |
  | + L4 leak_zone_avg_global | 6.776 | 9.845 | -0.5 | -0.0 | 100.0% |
  | + L5 leak_zone_avg_60m_self | 4.362 | 6.911 | -35.9 | -29.8 | 100.0% |
  | + L6 leak_zone_avg_60m_inprogress | 6.804 | 9.852 | -0.1 | +0.0 | 98.2% |
  | + L7 leak_fare_correction | 2.146 | 4.688 | -68.5 | -52.4 | 100.0% |
  | + L8 leak_snapshot_zone_avg | 6.801 | 9.867 | -0.1 | +0.2 | 100.0% |
  | + L9 leak_last_fare_zone | 6.806 | 9.845 | -0.0 | -0.0 | 90.3% |
  | + ALL leaky features | 1.451 | 3.349 | -78.7 | -66.0 | - |
- Observation to discuss:
  - Honest skill is modest: the corrected model beats the baseline by 12.6% (MAE) and 20.6% (RMSE).
    The ALL-leaky model reports MAE 1.451, 78.7% lower than the honest model: apparent skill is far
    above real skill.
  - The share of rows flagged does NOT predict the damage. 7 of 9 leaky features are flagged on 100% of
    rows, yet their MAE change ranges from -0.1% (L8) to -68.5% (L7); L6 (98.2%) and L9 (90.3%) also
    change almost nothing. The flagged share measures how often a rule is violated, not how informative
    the leaked value is.
  - Leaks invisible to the metrics: L4, L6, L8 and L9 changed MAE by 0.5% or less (L8's RMSE even rose
    0.2%). A score-based check would never notice them, but the auditor flags all four (R4, R4, R1+R2,
    R2). Caveat: no uncertainty estimates yet, so changes of a few tenths of a percent cannot be told
    apart from noise either way.
  - Low correlation does not mean safe: L3 (dropoff hour) had r = 0.001 with the target alone but
    lowers MAE by 6.5% combined with pickup hour (the two together reveal trip length at hour resolution).
  - L5 gives -35.9% although its window pools few trips. Hypothesis (untested): the corrected set already
    contains the safe 60-minute average (N5), and L5 differs from it only by the trip's own duration,
    so the model can recover the target from the two. Test: rerun L5 without N5.
  - Leaks combine: ALL (MAE 1.451) is better than the best single leak (L7, 2.146).
  - Single-feature correlation overstated L6's effect (r 0.566 vs 0.541 for N5); in the full model it
    added nothing measurable.
- Timings (4 GB / 2-core laptop): 1.6 s to 2.5 s per model, measured as fit + predict with 200 boosting
  iterations on 69,899 training rows. 1.6 s for the corrected model (8 features), 1.6 to 2.2 s for the
  9-feature models, 2.5 s for ALL (17 features). About 21 s for all 11 models (sum of the rounded values;
  excludes loading the table and the audit). Test suite: 47 passed in 22.2 s.

## 2026-11-08 (continued 3)
- Phase 3 gate passed: analysis re-fit every model and reproduced results/ml_comparison.csv exactly
  (MAE equal to 1e-9). 55 tests passed in 25.7s.
- Method: hour-block bootstrap on the test set (168 hourly blocks, 2000 resamples, paired differences,
  95% percentile intervals). Intervals cover test-set sampling only; training randomness is fixed by the
  seed; one split, one week. Production conditions = leaky value replaced by what a correct point-in-time
  service returns (missing for L1/L2/L3/L7; zone_avg_dur_asof for L4/L8; zone_avg_dur_60m for L5/L6;
  last fare by availability clock for L9). This is a simulation under simulated availability times.
- 1) Offline difference vs corrected (corrected MAE 6.809 [6.613, 6.999]), minutes, 95% CI:
  | model | dMAE | dRMSE |
  |---|---|---|
  | L1 distance | -3.474 [-3.585, -3.361] | -4.323 [-4.546, -4.091] |
  | L2 total amount | -4.119 [-4.246, -3.985] | -4.720 [-4.915, -4.509] |
  | L3 dropoff hour | -0.441 [-0.493, -0.392] | -0.775 [-0.870, -0.680] |
  | L4 global zone avg | -0.033 [-0.050, -0.015] | -0.003 [-0.036, +0.032] (n.s.) |
  | L5 60m self | -2.447 [-2.540, -2.352] | -2.937 [-3.094, -2.778] |
  | L6 in-progress | -0.005 [-0.030, +0.019] (n.s.) | +0.004 [-0.035, +0.047] (n.s.) |
  | L7 fare correction | -4.663 [-4.807, -4.515] | -5.160 [-5.396, -4.910] |
  | L8 snapshot | -0.008 [-0.025, +0.011] (n.s.) | +0.019 [-0.010, +0.047] (n.s.) |
  | L9 last fare | -0.002 [-0.014, +0.009] (n.s.) | -0.003 [-0.025, +0.018] (n.s.) |
  | ALL leaky | -5.357 [-5.520, -5.189] | -6.499 [-6.759, -6.227] |
- 2) Production conditions (production MAE, difference vs corrected 6.809, 95% CI):
  | model | offline MAE | production MAE | production - corrected |
  |---|---|---|---|
  | L1 | 3.335 | 8.643 | +1.835 [+1.709, +1.959] worse |
  | L2 | 2.690 | 9.055 | +2.246 [+2.095, +2.394] worse |
  | L3 | 6.368 | 8.079 | +1.270 [+0.926, +1.689] worse |
  | L4 | 6.776 | 6.768 | -0.041 [-0.056, -0.024] better |
  | L5 | 4.362 | 6.802 | -0.007 [-0.048, +0.034] no clear difference |
  | L6 | 6.804 | 6.835 | +0.026 [+0.005, +0.048] worse (tiny) |
  | L7 | 2.146 | 8.998 | +2.189 [+2.050, +2.327] worse |
  | L8 | 6.801 | 6.833 | +0.024 [+0.000, +0.050] worse (borderline) |
  | L9 | 6.806 | 6.813 | +0.004 [-0.009, +0.017] no clear difference |
  | ALL | 1.451 | 8.461 | +1.652 [+1.509, +1.787] worse |
- 3) Ablations (MAE without -> with the leak; main = effect in the full experiment):
  - L5 without N5: 6.797 -> 6.588, dMAE -0.210 [-0.253, -0.167] = -3.1% (main -35.9%).
  - L3 without pickup_hour: 6.949 -> 6.840, dMAE -0.109 [-0.146, -0.073] = -1.6% (main -6.5%).
- Findings:
  - Offline the leaky models look far better (ALL: MAE 1.451, -78.7%). In production the models that rely
    on a value that does not yet exist (L1, L2, L7, ALL) are 24-33% WORSE than the corrected model
    (MAE 8.46-9.06 vs 6.809), i.e. 2.6x to 5.8x their offline error, and above the constant-median
    baseline (7.787). Baseline comparison not tested with intervals; L3's margin over it is only 0.29.
  - L5's offline gain (-35.9%) vanishes in production (-0.007, n.s.): it was inflation only.
  - Hypothesis from the previous entry supported: L5's gain needs N5 (effect shrinks ~12x without it);
    L3's gain mostly needs pickup_hour (effect shrinks ~4x; the remaining -1.6% is plausibly dropoff hour
    acting as a proxy for the missing pickup hour, untested). Removing N5 from the corrected model changed
    MAE from 6.809 to 6.797 (not tested): N5 seems redundant for the honest model.
  - CORRECTION to the previous entry: L4, L6, L8, L9 are practically invisible (< 3 seconds of MAE), but
    only L6, L8, L9 are statistically indistinguishable from zero. L4's MAE change (-0.033 min) is
    detectable [-0.050, -0.015]; its RMSE change is not.
  - L4 under production conditions was slightly better than the corrected model (-0.041 min, ~2.5 s).
    Untested explanation: the whole-month zone average is a steadier zone-level training feature than the
    as-of one. This does NOT make L4 acceptable: it still uses information from after t_pred.
  - Limits: one month, one chronological split, one model class, simulated availability lags (2 min fare
    lag, next-day 06:00 corrections), serving substitutes chosen by us. Bootstrap by hour does not capture
    day-level variation (only 7 test days).