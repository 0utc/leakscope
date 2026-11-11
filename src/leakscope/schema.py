"""Data contracts with Pandera.

validate_raw        : the raw TLC file has the columns and types the pipeline needs
                      (checked on a sample of rows)
validate_model_table: the table handed to the ML step is internally consistent
Both raise PipelineError with a readable list of what is wrong.
"""
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

try:
    import pandera.pandas as pa
except ImportError:                  # older Pandera versions
    import pandera as pa
from pandera.errors import SchemaErrors

from leakscope.checks import PipelineError

# columns the pipeline actually reads from the raw file
REQUIRED_RAW = ["tpep_pickup_datetime", "tpep_dropoff_datetime", "PULocationID",
                "passenger_count", "trip_distance", "fare_amount", "total_amount"]


def _is_datetime(s):
    return bool(pd.api.types.is_datetime64_any_dtype(s))


def _is_numeric(s):
    return bool(pd.api.types.is_numeric_dtype(s)) and not pd.api.types.is_bool_dtype(s)


def _datetime_col():
    return pa.Column(checks=pa.Check(_is_datetime, error="must be a datetime column"),
                     nullable=True)


def _numeric_col():
    return pa.Column(checks=pa.Check(_is_numeric, error="must be numeric"), nullable=True)


RAW_SCHEMA = pa.DataFrameSchema(
    {"tpep_pickup_datetime": _datetime_col(), "tpep_dropoff_datetime": _datetime_col(),
     **{c: _numeric_col() for c in REQUIRED_RAW[2:]}},
    strict=False)          # extra columns are fine


def _explain(error, title):
    cases = error.failure_cases
    keys = cases[["column", "check"]].drop_duplicates().head(8)
    lines = [f"{row.column}: {row.check}" for row in keys.itertuples()]
    return PipelineError(f"{title}:\n- " + "\n- ".join(lines))


def validate_raw(parquet_path, n_sample=20_000):
    """Check the raw file on its first n_sample rows. Returns the sample size."""
    path = Path(parquet_path)
    try:
        df = duckdb.sql(
            f"SELECT * FROM read_parquet('{path.as_posix()}') LIMIT {int(n_sample)}").df()
    except duckdb.Error as e:
        raise PipelineError(f"cannot read {path} as a Parquet file: {e}") from e
    missing = [c for c in REQUIRED_RAW if c not in df.columns]
    if missing:
        raise PipelineError(
            f"{path.name} is missing required column(s): {', '.join(missing)}. "
            f"The pipeline expects the NYC TLC yellow-taxi schema.")
    try:
        RAW_SCHEMA.validate(df, lazy=True)
    except SchemaErrors as e:
        raise _explain(e, f"{path.name} does not match the expected schema") from e
    return len(df)


def validate_model_table(df, feature_names):
    """Contract for the table used by the ML experiments."""
    if len(df) == 0:
        raise PipelineError("the model table is empty (no prediction examples)")
    finite = pa.Check(lambda s: bool(np.isfinite(s.dropna()).all()),
                      error="contains infinite values")
    schema = pa.DataFrameSchema(
        {"trip_id": pa.Column(unique=True, nullable=False),
         "t_pred": pa.Column(checks=pa.Check(_is_datetime, error="must be a datetime column"),
                             nullable=False),
         "label_available_at": pa.Column(
             checks=pa.Check(_is_datetime, error="must be a datetime column"),
             nullable=False),
         "target": pa.Column(float, checks=[pa.Check.gt(0), finite], nullable=False),
         **{n: pa.Column(float, checks=finite, nullable=True) for n in feature_names}},
        checks=pa.Check(lambda d: bool((d["label_available_at"] > d["t_pred"]).all()),
                        error="a label must become available after the prediction time"),
        strict=False)
    try:
        schema.validate(df, lazy=True)
    except SchemaErrors as e:
        raise _explain(e, "the model table violates its contract") from e
    return df