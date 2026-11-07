# LeakScope: Scope Definition

## 1. Prediction task
At the moment a taxi trip begins (pickup), predict how long the trip will
take (target: duration_min = dropoff time - pickup time, in minutes).

- Prediction time (t_pred) = the trip's pickup timestamp.
- Dataset: NYC TLC Yellow Taxi trips (Parquet), 50k-200k rows.

## 2. Core definitions
- Event time: when something happened in the real world.
- Availability time: when the information becomes usable by a prediction
  service. NYC TLC data does NOT record this, so availability times are
  SIMULATED using the lag rules in section 4. They are assumptions, not
  observed facts.
- Core rule: a feature value may be used for a prediction at t_pred only if
  availability_time <= t_pred.

## 3. What is allowed at prediction time
Known at pickup: pickup time, pickup zone, day of week, hour,
passenger count (assumed entered at pickup).
Historical statistics: computed ONLY from trips whose availability time
is <= t_pred, within a lookback window.

## 4. Simulated availability rules (all assumptions, configurable)
| Field / feature               | Event time | Availability time                       |
|-------------------------------|------------|-----------------------------------------|
| pickup time, zone, passengers | pickup     | pickup (lag 0)                          |
| dropoff time, duration        | dropoff    | dropoff + 0 min                         |
| final trip distance           | dropoff    | dropoff + 0 min                         |
| final fare / total amount     | dropoff    | dropoff + 2 min                         |
| corrections to fare           | dropoff    | next day 06:00 after dropoff            |
| zone average duration         | derived    | max(availability of its source trips)   |

## 5. Mandatory rule types the auditor must detect
- R1 Future event timestamp: a feature's event time is after t_pred.
- R2 Late availability: availability time is after t_pred.
- R3 Declared post-outcome field: a field marked as only known after
  the outcome is used as a feature.
- R4 Incorrect aggregation: an aggregate includes rows that are
  unavailable at t_pred (including the row being predicted).

Rule notes: R1 implies R2 whenever metadata is consistent (availability >= event),
so R1 is the stronger diagnosis (the feature describes something that had not yet
happened) and R2 alone means it happened but was not yet usable.

## 6. Test scenarios (known expected outcomes)
A scenario may trigger several rules. Each scenario's expected outcome is its
full set of rules.

Leaky scenarios (the auditor MUST flag these):
| ID | Scenario | Expected rules |
|----|----------|----------------|
| L1 | Final trip_distance used as a feature; the registry wrongly claims it was recorded at pickup (metadata lies) | R3 |
| L2 | Final total fare of the trip itself | R1, R2 |
| L3 | Dropoff hour of the trip itself | R1, R2, R3 |
| L4 | Zone average duration over the whole dataset (includes the future and the current trip) | R4 |
| L5 | Zone rolling average that includes the current trip itself | R4 |
| L6 | Zone rolling average (last 60 min by pickup time) that includes trips still in progress at t_pred (the 10:00 vs 10:25 case) | R4 |
| L7 | Next-day fare correction of the trip itself | R1, R2 |
| L8 | Feature snapshot taken after t_pred (non-point-in-time join) | R1, R2 |
| L9 | Fare of the last completed trip in the zone, looked up by dropoff time instead of fare availability time | R2 |

Negative controls (the auditor MUST NOT flag these):
| ID | Scenario |
|----|----------|
| N1 | Pickup hour, day of week, zone, passenger count |
| N2 | Zone average using only trips with availability time <= t_pred |
| N3 | 7-day lookback average over trips with availability time in (t_pred - 7 days, t_pred] |
| N4 | Boundary case: a source trip available at exactly t_pred (valid under <=) |

## 7. Explicit limitations
- Availability times are simulated, not observed.
- The auditor detects explicit rule violations only and cannot guarantee
  finding every kind of leakage.
- Out of scope for the MVP: cross-validation leakage, target encoding
  leakage, Feast comparison, streaming, cloud deployment.