# Data Dictionary (NYC TLC Yellow Taxi, 2024-01)

Official dictionary: https://www.nyc.gov/assets/tlc/downloads/pdf/data_dictionary_trip_records_yellow.pdf

Note: the raw file has NO trip ID and NO availability timestamps.
We create our own trip_id, and availability times are SIMULATED (see scope.md).

| Column                 | Meaning                         | Classification                    |
|------------------------|---------------------------------|-----------------------------------|
| VendorID               | Data provider code              | known at pickup                   |
| tpep_pickup_datetime   | Pickup time                     | known at pickup (this is t_pred)  |
| tpep_dropoff_datetime  | Dropoff time                    | POST-OUTCOME                      |
| passenger_count        | Passengers (driver-entered)     | known at pickup (assumption)      |
| trip_distance          | Final trip distance             | POST-OUTCOME                      |
| RatecodeID             | Rate code (final one in effect) | POST-OUTCOME (conservative)       |
| store_and_fwd_flag     | Stored-and-forwarded record     | ambiguous, excluded from features |
| PULocationID           | Pickup zone                     | known at pickup                   |
| DOLocationID           | Dropoff zone                    | POST-OUTCOME                      |
| payment_type           | How the trip was paid           | POST-OUTCOME                      |
| fare_amount, extra, mta_tax, tip_amount, tolls_amount, improvement_surcharge, total_amount, congestion_surcharge, Airport_fee | Fare components | POST-OUTCOME |

Target: duration_min = (dropoff - pickup) in minutes.