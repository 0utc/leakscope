import duckdb

con = duckdb.connect()  # temporary in-memory database
con.execute("SET memory_limit='1GB'")
con.execute("SET threads=2")
con.execute("SET temp_directory='O:/Projects/leakscope/tmp'")

F = "data/raw/yellow_tripdata_2024-01.parquet"

print("--- Columns and types ---")
con.sql(f"DESCRIBE SELECT * FROM read_parquet('{F}')").show(max_rows=40)

print("--- Row count and pickup time range ---")
con.sql(f"""
    SELECT count(*) AS n_rows,
           min(tpep_pickup_datetime) AS first_pickup,
           max(tpep_pickup_datetime) AS last_pickup
    FROM read_parquet('{F}')
""").show()

print("--- Suspicious rows ---")
con.sql(f"""
    SELECT
      count(*) FILTER (WHERE tpep_dropoff_datetime <= tpep_pickup_datetime) AS dropoff_not_after_pickup,
      count(*) FILTER (WHERE tpep_pickup_datetime < '2024-01-01'
                          OR tpep_pickup_datetime >= '2024-02-01') AS outside_month,
      count(*) FILTER (WHERE trip_distance <= 0) AS zero_distance,
      count(*) FILTER (WHERE passenger_count IS NULL) AS null_passengers,
      count(*) FILTER (WHERE date_diff('minute', tpep_pickup_datetime,
                                       tpep_dropoff_datetime) > 180) AS over_3_hours
    FROM read_parquet('{F}')
""").show()

print("--- Trip duration in minutes ---")
con.sql(f"""
    SELECT round(quantile_cont(dur, 0.5), 1)  AS median_min,
           round(quantile_cont(dur, 0.99), 1) AS p99_min,
           round(max(dur), 1)                 AS max_min
    FROM (SELECT date_diff('second', tpep_pickup_datetime, tpep_dropoff_datetime) / 60.0 AS dur
          FROM read_parquet('{F}'))
""").show()