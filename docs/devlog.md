# Devlog

*Lab notebook. Entries are chronological; "(continued N)" marks further entries on the same day. Terminal output is shown as Markdown tables.*

## 2026-11-07

- Set up environment on O: SSD (Python 3.13, venv, portable Git on PATH).
- Defined task: predict trip duration at pickup time.
- Defined 4 rule types (R1-R4), 8 leaky scenarios, 4 negative controls (later extended to 10 leaky and 5 valid scenarios, see 2026-11-08).
- Decision: availability times are simulated with configurable lags,
  because NYC TLC data does not record when fields became available.

## 2026-11-07 (continued)

- Downloaded yellow_tripdata_2024-01.parquet and taxi zone lookup.
- Explored data in DuckDB (experiments/explore_data.py).
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

  | decision | n | pct |
  |---|---|---|
  | KEPT | 2,927,502 | 98.748 |
  | duration_too_short | 34,251 | 1.155 |
  | duration_too_long | 1,983 | 0.067 |
  | nonpositive_duration | 870 | 0.029 |
  | pickup_outside_window | 18 | 0.001 |

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
- Correlations with the target:

  | feature | correlation |
  |---|---|
  | pickup_hour | -0.012 |
  | passenger count | 0.011 |
  | leak_dropoff_hour | 0.001 |
  | leak_zone_avg_global | 0.535 |
  | leak_trip_distance | 0.774 |
  | leak_total_amount | 0.748 |

## 2026-11-07 (continued 5)

- Correlations with target (row features): pickup_hour -0.012, passengers 0.011, leak_dropoff_hour 0.001,
  leak_zone_avg_global 0.535, leak_trip_distance 0.774, leak_total_amount 0.748.
  Lesson: dropoff hour has ~0 standalone correlation but is still invalid -> audit availability, not correlation.
- Implemented temporal_join.py: ASOF LEFT JOIN on zone_cum for N2 (as-of t_pred, <=) and N3 (7-day lookback
  via difference of running totals, window (t_pred-7d, t_pred]). No history -> NULL, not 0.
- Implemented pipeline.py (one command, rebuilds from raw). Timings on 4 GB / 2-core laptop (O: SSD):

  | step | seconds |
  |---|---|
  | ingestion | 8.6 |
  | availability times | 4.0 |
  | prediction examples | 0.7 |
  | feature tables | 3.7 |
  | point-in-time features | 1.6 |
  | **total** | **18.6** |

- Safe vs leaky zone average, correlation with the target: safe as-of 0.534, safe 7-day 0.533,
  leaky global 0.535.
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
- Corrections to the scope document (spec errors found while implementing): R1 implies R2; L2/L7 trigger
  R1+R2; L1 needs metadata lying about both clocks to be R3-only (registry fixed); added L9 as the only
  R2-only scenario.

## 2026-11-07 (continued 7)

- Implemented auditor.py: R1 (event > t_pred), R2 (availability > t_pred, strict so == is valid),
  R3 (declared post-outcome) for row features, returning Finding objects with trip IDs and explanations.
- Tests: 8 hand-built fixture tests incl. boundary (== t_pred not flagged, +1s flagged),
  L1 pattern (metadata lies -> only R3), L3 pattern (R1+R2+R3), unknown clock rejected.
- Real-data result:

  | feature | scenario | rules fired | max rows per rule |
  |---|---|---|---|
  | pickup_hour | N1 | - | 0 |
  | pickup_dow | N1 | - | 0 |
  | pu_zone | N1 | - | 0 |
  | passenger_count_filled | N1 | - | 0 |
  | passenger_missing | N1 | - | 0 |
  | zone_avg_dur_asof | N2 | - | 0 (aggregate: R4 pending) |
  | zone_avg_dur_7d | N3 | - | 0 (aggregate: R4 pending) |
  | leak_trip_distance | L1 | R3 | 100,000 |
  | leak_total_amount | L2 | R1, R2 | 100,000 |
  | leak_dropoff_hour | L3 | R1, R2, R3 | 100,000 |
  | leak_zone_avg_global | L4 | - | 0 (aggregate: R4 pending) |

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
- Real-data results, auditor:

  | feature | scenario | rules fired | rows flagged (max) |
  |---|---|---|---|
  | pickup_hour | N1 | - | 0 |
  | pickup_dow | N1 | - | 0 |
  | pu_zone | N1 | - | 0 |
  | passenger_count_filled | N1 | - | 0 |
  | passenger_missing | N1 | - | 0 |
  | zone_avg_dur_asof | N2 | - | 0 |
  | zone_avg_dur_7d | N3 | - | 0 |
  | leak_trip_distance | L1 | R3 | 100,000 |
  | leak_total_amount | L2 | R1, R2 | 100,000 |
  | leak_dropoff_hour | L3 | R1, R2, R3 | 100,000 |
  | leak_zone_avg_global | L4 | R4 | 100,000 |
  | zone_avg_dur_60m | N5 | - | 0 |
  | leak_zone_avg_60m_self | L5 | R4 | 100,000 |
  | leak_zone_avg_60m_inprogress | L6 | R4 | 98,211 |
  | leak_fare_correction | L7 | R1, R2 | 100,000 |
  | leak_snapshot_zone_avg | L8 | R1, R2 | 100,000 |
  | leak_last_fare_zone | L9 | R2 | 90,309 |

- Scenario feature tables, coverage:

  | metric | value |
  |---|---|
  | rows | 100,000 |
  | empty 60-minute window (zone_avg_dur_60m) | 0.82% |
  | empty window, in-progress feature (L6) | 0.75% |
  | no last fare (L9) | 0.002% |

- Scenario feature tables, correlation with the target:

  | feature | correlation |
  |---|---|
  | N5 safe 60-minute average | 0.541 |
  | L5 (60 min + own trip) | 0.617 |
  | L6 (in-progress trips) | 0.566 |
  | L7 (fare correction) | 0.771 |
  | L8 (snapshot) | 0.536 |
  | L9 (last fare by wrong clock) | 0.344 |

- L9: the looked-up fare was not yet available for 90.3% of examples (mean 3.72 min since that trip's dropoff).
- Pipeline timings on the laptop (with scenario features):

  | step | seconds |
  |---|---|
  | ingestion | 9.4 |
  | availability times | 4.6 |
  | prediction examples | 0.7 |
  | feature tables | 3.7 |
  | point-in-time features | 1.6 |
  | scenario features | 9.4 |
  | **total** | **29.4** |

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

  | scenario | what it tests | feature | expected | fired | ok | rows flagged |
  |---|---|---|---|---|---|---|
  | L1 | final distance, metadata lies | leak_trip_distance | R3 | R3 | yes | 100000/100000 |
  | L2 | final fare of own trip | leak_total_amount | R1,R2 | R1,R2 | yes | 100000/100000 |
  | L3 | own dropoff hour | leak_dropoff_hour | R1,R2,R3 | R1,R2,R3 | yes | 100000/100000 |
  | L4 | whole-dataset zone average | leak_zone_avg_global | R4 | R4 | yes | 100000/100000 |
  | L5 | rolling average incl. own trip | leak_zone_avg_60m_self | R4 | R4 | yes | 100000/100000 |
  | L6 | rolling avg incl. in-progress trips | leak_zone_avg_60m_inprogress | R4 | R4 | yes | 98211/100000 |
  | L7 | next-day fare correction | leak_fare_correction | R1,R2 | R1,R2 | yes | 100000/100000 |
  | L8 | end-of-day snapshot join | leak_snapshot_zone_avg | R1,R2 | R1,R2 | yes | 100000/100000 |
  | L9 | last fare by wrong clock | leak_last_fare_zone | R2 | R2 | yes | 90309/100000 |
  | L10 | available 1 second too late | boundary_late | R2 | R2 | yes | 3/3 |
  | N1 | pickup-time fields | pickup_hour, pickup_dow, pu_zone, passenger_count_filled, passenger_missing (5 features) | - | - | yes | 0/100000 each |
  | N2 | zone avg available by t_pred | zone_avg_dur_asof | - | - | yes | 0/100000 |
  | N3 | 7-day lookback average | zone_avg_dur_7d | - | - | yes | 0/100000 |
  | N4 | available at exactly t_pred | boundary_exact | - | - | yes | 0/3 |
  | N5 | 60-minute availability window | zone_avg_dur_60m | - | - | yes | 0/100000 |

  19 features evaluated (10 leaky, 9 valid); exact rule-set match 19/19.
  Feature level: TP=10 FP=0 FN=0 TN=9, precision 1.000, recall 1.000.
  Rule level: TP=15 FP=0 FN=0 TN=61, precision 1.000, recall 1.000.
  Conformance: all six aggregates conform (zone_avg_dur_asof, zone_avg_dur_7d, leak_zone_avg_global,
  zone_avg_dur_60m, leak_zone_avg_60m_self, leak_zone_avg_60m_inprogress).
  Known limitation M1 (aggregate whose declaration is false): rules R1-R4 fired none (missed);
  the conformance check found 5 mismatching examples (caught).
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

## 2026-11-08 (continued 4)

- Phase 4, step 1: report generator (reporting.py). Outputs results/report.md, results/audit_report.json,
  assets/fig_offline_vs_production.png, assets/fig_flagged_vs_effect.png. HTML skipped on purpose
  (Markdown renders on GitHub; JSON is for programs).
- Design: every number in the summary is computed from the data; no timestamps so reruns only change the
  file when results change; evaluate_features keeps findings; audit_scoring.run split into collect()
  (no printing) + run(); auditor explanations show seconds for gaps under one minute.
- Figure review on real results: numbers match the analysis tables. Figure 1: production bars of L1, L2,
  L7 and ALL lie entirely above the constant-median baseline line (7.79); L3's interval (about 7.7-8.5)
  contains it. This is a visual check against a point value, not a formal test (the baseline's own
  uncertainty is not drawn). Fixes: figure 2 labels now carry the effect size so L4 (-0.5%) and L8
  (-0.1%) are distinguishable; x-axis capped at 100%; y-axis states "negative = lower error"; figure 1
  caption warns that overlapping bars are not a paired test.
- Figure titles are deliberately neutral: an earlier draft claimed "leaky models are worse in
  production", which is false for L4, L5 and L9 on this data.
- Tests: 62 passed (47.8 s).


## 2026-11-11

### Defect: feature tables not reproducible across database rebuilds

| Item | Detail |
|---|---|
| Symptom | Rebuilding the DuckDB database changed some feature values at the 1e-13 level; the L6 offline MAE moved from 6.804 to 6.796 |
| Cause (diagnosis) | Float sums over rows with tied timestamps in DuckDB window functions are order-dependent. Supported by the fix and test; not traced inside DuckDB |
| Fix | Accumulate integer seconds in `feature_builder.create_zone_cum` and `create_zone_global`; new `tests/test_determinism.py` |
| Verification (Windows) | `pytest -q`: 98 passed. Two separate rebuilds compared with `compare_runs.py`: difference 0.000e+00 in every column |
| Verification (CI) | GitHub Actions green on all jobs. Ubuntu, Python 3.13: 89 passed, 9 skipped, coverage 86% (1341 statements, 190 missed) |

### Correction of an earlier claim

| Earlier statement | Correction |
|---|---|
| "Offline MAEs reproduce results to 1e-9" | That check only compared refits on one database, not rebuilds of the database |

### Old (commit 3693b25) vs new results, real data, 100k examples

| Experiment | Offline MAE old | Offline MAE new | Production MAE old | Production MAE new |
|---|---|---|---|---|
| Corrected | 6.809 | 6.817 | | |
| L1 | 3.335 | 3.332 | 8.643 | 8.626 |
| L2 | 2.690 | 2.706 | 9.055 | 9.075 |
| L3 | 6.368 | 6.368 | 8.079 | 7.912 |
| L4 | 6.776 | 6.766 | 6.768 | 6.762 |
| L5 | 4.362 | 4.372 | 6.802 | 6.806 |
| L6 | 6.804 | 6.801 | 6.835 | 6.842 |
| L7 | 2.146 | 2.153 | 8.998 | 9.035 |
| L8 | 6.801 | 6.797 | 6.833 | 6.822 |
| L9 | 6.806 | 6.811 | 6.813 | 6.809 |
| ALL | 1.451 | 1.451 | 8.461 | 8.471 |

Largest absolute shift in any offline MAE: 0.0157 (RMSE: 0.0316), from `compare_runs.py`.

### Observations (not explained)

| Observation | Status |
|---|---|
| The corrected model also moved (+0.008), so the fix changed more than the L6 feature | Which valid features changed is untested |
| Production L3 moved by 0.167 | Its 95% interval was about 0.8 wide, so the shift is inside the sampling noise; cause unknown |
| Production verdict for L8 changed from "worse" (interval lower end +0.0001) to "no clear difference" | A verdict flipped between builds; interpret verdicts near the interval edge with caution |
| 9 tests are skipped in CI | Reason not yet checked |

## 2026-11-11 (continued)

### Cross-platform check

| Item | Detail |
|---|---|
| Comparison | `ml_comparison.csv` from the demo: local Windows run vs GitHub Actions artifact (Ubuntu, Python 3.13) |
| Result | Largest absolute difference 0.000e+00 in every column; agree to 1e-09: yes |
| Scope | Synthetic demo data only; one CI configuration; other result files not compared |
| Open | 9 tests are skipped in CI (none locally); reason not yet checked |


## 2026-11-11 (continued 2)

### CI skip reasons and coverage

| Item | Detail |
|---|---|
| Change | `-rs` added to the pytest step in `.github/workflows/ci.yml` (commit 3f5ab66) |
| Skip reasons (CI) | 9 skipped, all "local database not built": 6 in `test_conformance.py`, 1 each in `test_evaluation.py`, `test_scorecard.py`, `test_temporal_join.py`. CI has no real data, so the tests that need the database skip |
| Coverage, CI (Ubuntu, Python 3.13) | 89 passed, 9 skipped; TOTAL 1341 statements, 190 missed, 86% |
| Coverage, local (Windows, Python 3.13.11) | 98 passed, 0 skipped; TOTAL 1341 statements, 190 missed, 86% |
| Test duration, local (2-core laptop) | 98 tests with coverage: 171 s; 103 tests: 181 s (70 tests earlier: 63 s). The end-to-end tests dominate |

### Corrections of earlier predictions

| Earlier statement | Correction |
|---|---|
| Local coverage with all 98 tests would be fuller than CI's | Falsified: both runs give identical per-module numbers, so the 9 skipped tests add no covered lines |
| Low coverage of `feature_builder`, `temporal_join` and `temporal_model` is due to the skipped tests | Not supported: same numbers with and without the skipped tests |
| Code run through CLI subprocesses is not measured by coverage | Falsified: no use of `subprocess` in `tests/` |

### Cause of the low coverage (resolved)

| Module (coverage) | Missed lines | What they are |
|---|---|---|
| `temporal_model` (51%) | 69-103, 107-115 | `verify()` printer and `__main__` block |
| `temporal_join` (56%) | 99-119, 123-130 | `verify()` printer and `__main__` block |
| `feature_builder` (56%) | 16, 75-98, 102-109 | one error branch, `verify()` printer, `__main__` block |
| `ingestion` (69%) | 83-110, 114-121 | `summarize()` printer and `__main__` block |
| `scenarios` (74%) | 117-148, 239-245 | `verify()` printer and `__main__` block |
| `registry` (67%) | 99-108 and 17 single lines | `__main__` block and validation-failure branches |
| `auditor` (81%), `audit_scoring` (92%) | 236-265, 134-144 | `print_report()` and `run()` printing wrappers, `__main__` blocks |
| `__main__.py` (0%) | 1-3 | module entry point (the CI demo step runs it) |

| Item | Detail |
|---|---|
| Conclusion | The gaps are diagnostic printing functions, `__main__` blocks and error branches that no test triggers; the core logic is covered |
| Evidence level | Line numbers match function boundaries exactly for `feature_builder` and `registry` (compared with the assistant's copies) and within 1-3 lines for `scenarios`, `auditor`, `audit_scoring`. For `temporal_model`, `temporal_join` and `ingestion` the ranges are consistent with the sizes of the printer functions; their source was not compared line by line |
| Wording allowed | "86% line coverage (pytest-cov); diagnostic print functions and `__main__` entry points are not executed by the tests" |

### Process notes

| Note | Detail |
|---|---|
| Devlog corrupted once | An entry was pasted into PowerShell instead of the editor; `docs/devlog.md` was restored with `git checkout` and the entry re-added in VS Code |
| Wrong command | A `compare_runs.py` call used a folder instead of a file path |

## 2026-11-13

### Float-noise sensitivity of the model scores (offline MAE, real data)

| Item | Detail |
|---|---|
| Question | How far do scores move when the sum-based features change at float-noise level (the size of the earlier rebuild defect)? |
| Method | Multiply the 7 sum-based features by (1 + rel x N(0,1)); refit all 11 models; model seed and split fixed |
| Runs | 20 refits at relative noise 1e-13 (`results/ml_sensitivity.csv`); 10 refits at 1e-9 (table below only, file not kept) |
| Code | `sensitivity.py`, `python -m leakscope sensitivity`, `tests/test_sensitivity.py`; suite: 103 passed in 181 s |

### Results, relative noise 1e-13 (20 runs)

| model | reference MAE | mean | SD | min | max | effect vs corrected (mean) | paired SD | runs lower |
|---|---|---|---|---|---|---|---|---|
| corrected | 6.817 | 6.798 | 0.0073 | 6.778 | 6.808 | (reference) | | |
| L1 | 3.332 | 3.329 | 0.0050 | 3.317 | 3.339 | -3.4690 | 0.0102 | 20/20 |
| L2 | 2.706 | 2.693 | 0.0139 | 2.673 | 2.716 | -4.1056 | 0.0177 | 20/20 |
| L3 | 6.368 | 6.372 | 0.0089 | 6.361 | 6.391 | -0.4261 | 0.0131 | 20/20 |
| L4 | 6.766 | 6.770 | 0.0098 | 6.755 | 6.791 | -0.0287 | 0.0128 | 20/20 |
| L5 | 4.372 | 4.361 | 0.0181 | 4.332 | 4.397 | -2.4378 | 0.0182 | 20/20 |
| L6 | 6.801 | 6.805 | 0.0071 | 6.789 | 6.822 | +0.0064 | 0.0120 | 5/20 |
| L7 | 2.153 | 2.138 | 0.0029 | 2.133 | 2.147 | -4.6606 | 0.0077 | 20/20 |
| L8 | 6.797 | 6.790 | 0.0100 | 6.773 | 6.807 | -0.0081 | 0.0129 | 13/20 |
| L9 | 6.811 | 6.818 | 0.0047 | 6.813 | 6.826 | +0.0195 | 0.0097 | 0/20 |
| ALL | 1.451 | 1.454 | 0.0053 | 1.443 | 1.463 | -5.3441 | 0.0086 | 20/20 |

Largest min-max spread of one model's MAE: 0.0657 min (L5).

### Results, relative noise 1e-9 (10 runs)

| model | reference MAE | mean | SD | min | max | effect vs corrected (mean) | paired SD | runs lower |
|---|---|---|---|---|---|---|---|---|
| corrected | 6.817 | 6.798 | 0.0063 | 6.787 | 6.808 | (reference) | | |
| L1 | 3.332 | 3.329 | 0.0051 | 3.317 | 3.335 | -3.4690 | 0.0081 | 10/10 |
| L2 | 2.706 | 2.691 | 0.0169 | 2.672 | 2.728 | -4.1066 | 0.0184 | 10/10 |
| L3 | 6.368 | 6.372 | 0.0086 | 6.360 | 6.382 | -0.4262 | 0.0100 | 10/10 |
| L4 | 6.766 | 6.772 | 0.0152 | 6.753 | 6.806 | -0.0256 | 0.0173 | 9/10 |
| L5 | 4.372 | 4.369 | 0.0220 | 4.336 | 4.396 | -2.4288 | 0.0231 | 10/10 |
| L6 | 6.801 | 6.802 | 0.0055 | 6.793 | 6.809 | +0.0036 | 0.0075 | 3/10 |
| L7 | 2.153 | 2.139 | 0.0031 | 2.133 | 2.142 | -4.6594 | 0.0073 | 10/10 |
| L8 | 6.797 | 6.793 | 0.0140 | 6.774 | 6.826 | -0.0049 | 0.0165 | 7/10 |
| L9 | 6.811 | 6.819 | 0.0048 | 6.815 | 6.829 | +0.0205 | 0.0089 | 0/10 |
| ALL | 1.451 | 1.453 | 0.0056 | 1.446 | 1.465 | -5.3445 | 0.0082 | 10/10 |

Largest min-max spread: 0.0597 min (L5).

### Old vs new build: effect relative to the corrected model

Computed from the rounded MAEs, so about +/-0.001.

| Leak | Effect, old build | Effect, new build | Moved by |
|---|---|---|---|
| L1 | -3.474 | -3.485 | 0.011 |
| L2 | -4.119 | -4.111 | 0.008 |
| L3 | -0.441 | -0.449 | 0.008 |
| L5 | -2.447 | -2.445 | 0.002 |
| L7 | -4.663 | -4.664 | 0.001 |
| ALL | -5.358 | -5.366 | 0.008 |
| L4 | -0.033 | -0.051 | 0.018 |
| L6 | -0.005 | -0.016 | 0.011 |
| L8 | -0.008 | -0.020 | 0.012 |
| L9 | -0.003 | -0.006 | 0.003 |

### Reading

| Observation | Detail |
|---|---|
| Size of the jitter | SD of one model's MAE under feature noise: 0.003 to 0.018 min. This matches the 0.0157 shift between two builds |
| Noise size | 1e-9 gives a similar spread to 1e-13 (SD 0.003-0.022 vs 0.003-0.018; only 10 runs at 1e-9) |
| Clear effects (sign in 30/30 runs, 30 or more times the paired SD for the smallest) | L1, L2, L3, L5, L7, ALL |
| L4 | Small gain, mean -0.029 (1e-13) and -0.026 (1e-9) min, 20/20 and 9/10 runs lower; about 1.5-2.2 times the paired SD: borderline |
| L9 | Consistently slightly worse: mean +0.020 min, 0/30 runs lower; about 2 times the paired SD |
| L6, L8 | Not distinguishable from zero (5/20, 3/10 and 13/20, 7/10 runs lower) |
| Scope | Offline scores only. Production conditions not covered: production verdicts near zero (L4, L5, L6, L8, L9) remain unresolved. Production effects of L1, L2, L3, L7, ALL (+1.1 to +2.3 min) are tens of times the jitter |
| Rule of thumb used | A difference is read as real if its sign is the same in nearly all runs and it is about twice the paired SD or more. Chosen by the author, not a statistical test |
| Reporting | Quote MAE with the jitter SD, e.g. corrected 6.82 min (SD 0.007). Three sources of uncertainty: test sampling (bootstrap), feature jitter (this study), and not studied: other splits, months, model seeds |

### Superseded interpretations

| Earlier statement | Now |
|---|---|
| "L4's MAE change is statistically detectable [-0.050, -0.015]" (single build, bootstrap only) | Very small (about 0.03 min, 2 s) and borderline relative to jitter; consistent sign in 29 of 30 refits |
| "L6, L8, L9 are indistinguishable from zero" | Confirmed for L6 and L8. L9 shows a consistent small increase in error (0/30 runs lower) that the bootstrap, which covers test sampling only, could not reveal |
| "L8 production verdict worse (borderline)" | Unresolved; production conditions not covered by the study |

### Hypotheses (untested)

| Hypothesis | Basis | Test |
|---|---|---|
| The sensitivity comes from tie-breaking: noise separates exactly equal feature values and moves histogram bin edges | The spread does not depend on the noise size (1e-13 vs 1e-9) | Round the sum-based features to 1e-6 and repeat the study |
| L9 slightly hurts the fit because it adds a weakly informative feature | Sign consistent in 30/30 refits | Not planned |
| The deterministic build is systematically different from the perturbed runs | Its MAE lies just outside the min-max of the 20 runs for 3 of 11 models (corrected, L7, L9); chance alone gives about 1 of 11 | Inconclusive, no test planned |

## 2026-11-14

### Benchmark design (runtime and peak memory)

| Item | Detail |
|---|---|
| Code | `src/leakscope/benchmark.py`, `python -m leakscope benchmark`, `tests/test_benchmark.py` |
| Size definition | Number of prediction examples (10,000, 50,000, 200,000); the synthetic data has 2 x as many trips. Chosen by the author; the real-data timings in earlier entries (2.9M trips, 100k examples) are a different setup and are not compared |
| Isolation | Every run is a fresh Python process, so interpreter, DuckDB and imports start cold; data generation runs in the parent and is not timed |
| Timed stages | Pipeline (`run_all`), audit (`collect`), ML (`evaluation` + `analysis`, 500 bootstrap resamples); per-step pipeline times parsed from the `[ok]` lines |
| Memory | Peak working set of the worker process (Windows API; `resource` elsewhere), read after each stage. Cumulative, so it never decreases within a run |
| Statistics | 3 runs per size, interleaved across sizes; median, min, max and the first run's total are reported |
| Hardware | Lenovo IdeaPad 1 14ADA05, AMD 3020e (2 cores), 4 GB RAM, Windows 11 Pro; DuckDB limited to 1 GB and 2 threads by `configs/taxi.yaml` |
| Limitations (known before running) | Synthetic data with 60 zones, so window sizes differ from the real data; one machine; the OS file cache is not cleared between runs, so "cold" means a cold process, not a cold disk |

### Benchmark results (synthetic data, 3 runs per size, median [min-max])

Hardware as stated in `results/benchmark_info.json`: Lenovo IdeaPad 1 14ADA05, AMD 3020e (2 cores), 4 GB RAM, Windows 11 Pro, work files on an external SSD. Files: `results/benchmark_runs.csv`, `benchmark_summary.csv`, `benchmark_info.json`.

| Examples | Clean trips | Pipeline s | Audit s | ML s | Total s | Peak MiB | First run total s |
|---|---|---|---|---|---|---|---|
| 10,000 | 19,928 | 1.6 [1.6-1.7] | 1.1 [1.1-1.2] | 21.9 [21.6-22.0] | 24.7 [24.4-24.7] | 226 [226-226] | 24.4 |
| 50,000 | 99,640 | 3.6 [3.6-3.6] | 2.1 [2.1-2.1] | 32.5 [32.2-35.3] | 38.2 [37.9-41.0] | 264 [264-265] | 37.9 |
| 200,000 | 398,557 | 6.8 [6.7-6.8] | 5.0 [5.0-5.3] | 73.9 [73.5-75.1] | 85.7 [85.5-86.8] | 434 [425-434] | 85.5 |

### Reading

| Observation | Detail |
|---|---|
| ML dominates the runtime | 89% of the total at 10k, 85% at 50k, 86% at 200k (ML median / total median) |
| Growth is sublinear in the total | 20x more examples gives 3.5x the total time (24.7 s to 85.7 s). Pipeline grows 4.3x (1.6 s to 6.8 s) and audit 4.5x (1.1 s to 5.0 s) |
| Large fixed ML cost | 21.9 s at 10k already. Untested explanation: the ML stage fits many models (the 11 experiments, the same 11 again for the production analysis, plus the ablation fits), each with 200 boosting iterations, and that cost depends only weakly on the number of rows. Not measured per model in this study |
| Memory | Peak working set 226, 264 and 434 MiB, far below the 4 GB of the machine. 20x more examples gives 1.9x the peak. Measured for the worker process only (whole run, cumulative); the parent process that generates the data is not included |
| Run-to-run spread | Total time within 1.2% (10k), 8.2% (50k) and 1.5% (200k) of the median. The 50k spread comes from one slower ML stage (35.3 s vs 32.2-32.5 s); cause unknown |
| Cold start | The first run was not slower than the median at any size (24.4 vs 24.7, 37.9 vs 38.2, 85.5 vs 85.7). So no cold-start penalty is visible. Caveat: "cold" means a fresh process; the OS file cache was not cleared, and the data files were written just before each run |
| Not comparable | The earlier real-data pipeline timing (29.4 s, 2.9M trips, 100k examples) is a different setup: real data with ~260 zones against 60 synthetic zones, and a different size definition |

### Limitations

| Limitation | Detail |
|---|---|
| Three runs per size | Min and max over 3 runs show spread, not a confidence interval |
| One machine, one power setting | Results describe this laptop only |
| Synthetic data | 60 zones, so lookback windows pool more trips than on the real data |
| Larger sizes untested | 200k examples was the maximum; no extrapolation beyond it |

## 2026-11-14 (continued)

### Report: benchmark, float-noise sensitivity and uncertainty wording

| Item | Detail |
|---|---|
| Change | `reporting.py` now adds a Precision bullet to the summary, section 2.4 "How precise are these numbers?" (three sources of uncertainty, plus the float-noise table), and a Performance section (benchmark table) |
| Optional inputs | `ml_sensitivity.csv`, `benchmark_summary.csv` + `benchmark_info.json`. Without them the report still builds (demo and CI); section 2.4 then says the noise study was not measured in that run, and section numbering stays 1-4 |
| Computed from data | Every number and every reading in the new sections comes from the result files: SD range, largest spread, the models whose reported MAE lies outside the refits' range, the "production differences smaller than the spread" list, the ML share of runtime, growth factors and the first-run difference |
| Reading rule | Sign in at least 90% of refits; clear if mean effect >= 10 x paired SD, borderline if 2 to 10 x, otherwise not distinguishable from 0. Chosen by the author, not a statistical test. L4 (ratio 2.25) and L9 (2.01) are near the borderline threshold |
| Tests | `tests/test_reporting.py`: reading rule edge cases, report without extras, report with extras (including determinism), optional file loading |
| Wording rule | Quote MAE with the jitter SD (corrected 6.817 min, SD 0.007); differences below the largest spread (0.066 min) are not interpretable; the intervals in 2.1-2.3 cover test sampling only |
| Open | The float-noise study is offline only; production verdicts near zero (L4, L5, L6, L8, L9) stay unresolved. 10 refits at 1e-9 (devlog 2026-11-13) are not in the report because that file was not kept |

### README

| Item | Detail |
|---|---|
| Content | Summary with the offline-vs-production figure, main results table, rule table, scorecard excerpt, quick start (`demo`), data download section, command table, performance table, limitations, repository layout, documentation links |
| Design choices | Two badges only (CI, Python version); no emoji or decorative icons; the scorecard is a text block, not a screenshot; the two existing report figures are reused |
| Numbers | Copied by hand from `results/report.md` and the benchmark and sensitivity results; they must be updated by hand if the experiments are re-run |
| Open | No LICENSE file yet; README sections for the LaTeX report and the final clean-install test are added when those items are done |

### Figure review and license

| Item | Detail |
|---|---|
| Figure review | Both report figures checked against the result files: every bar, line and label matches (`ml_uncertainty.csv`, `ml_production.csv`, `ml_comparison.csv`). No change to the figures |
| Caveat added to README | The four points near zero in the flagged-vs-effect figure (L4, L6, L8, L9; at most 0.051 min) are below the 0.066 min float-noise spread, so their order is not interpretable |
| Repository issue found | The two PNGs in `assets/` had never been committed, so the images referenced by `report.md` and the README would have been broken on GitHub. Fixed in the same commit as the README |
| License | MIT, code only; the TLC data is not redistributed. `pyproject.toml` license metadata left unchanged on purpose |
| Correction | The README entry above says "No LICENSE file yet": superseded by this entry |

### Screenshots

| Item | Detail |
|---|---|
| Files | `assets/screenshots/p4_benchmark_summary.png` (terminal summary of the benchmark run), `assets/screenshots/p4_report_uncertainty.png` (rendered section 2.4 of the report) |
| Purpose | Evidence of the actual runs on the laptop; not referenced in the README, which uses a text excerpt instead |