"""Compare two ml_comparison.csv files (for example a local run and a CI run).

Usage: python experiments/compare_runs.py first.csv second.csv

Prints, for every numeric column except the timing column, the largest absolute
difference, and says whether the two runs agree to 1e-9. Timings always differ.
"""
import sys

import pandas as pd

TOLERANCE = 1e-9
SKIP = {"fit_s"}


def compare(first, second):
    a = pd.read_csv(first).set_index("experiment")
    b = pd.read_csv(second).set_index("experiment")
    if list(a.index) != list(b.index):
        raise SystemExit("the two files contain different experiments")
    rows = []
    for col in a.columns:
        if col in SKIP or not pd.api.types.is_numeric_dtype(a[col]):
            continue
        diff = (a[col] - b[col]).abs().max()
        rows.append((col, 0.0 if pd.isna(diff) else float(diff)))
    return rows


def main(argv):
    if len(argv) != 3:
        raise SystemExit(__doc__)
    rows = compare(argv[1], argv[2])
    print(f"{'column':20} {'largest absolute difference':>28}")
    for col, diff in rows:
        print(f"{col:20} {diff:28.3e}")
    same = all(d <= TOLERANCE for _, d in rows)
    print(f"\nagree to {TOLERANCE:g}: {'yes' if same else 'NO'}")
    return 0 if same else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))