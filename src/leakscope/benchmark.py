"""Runtime and peak-memory benchmark of the pipeline, the audit and the ML steps.

"Size" is the number of prediction examples; the synthetic data has
`trips_per_example` times as many trips. Every run is a fresh Python process
(cold interpreter, cold DuckDB), so its peak memory is its own. Data generation
happens in the parent process and is not timed. Runs are interleaved across sizes
(10k, 50k, 200k, 10k, ...) so slow drifts such as a warming laptop hit all sizes
alike. The summary reports the median of the repeated runs, plus min, max and
the first run, which shows the cold-start cost.
"""
import contextlib
import io
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

import duckdb
import pandas as pd
import sklearn

from leakscope.ingestion import connect, load_config

SIZES = (10_000, 50_000, 200_000)
METRICS = ("pipeline_s", "audit_s", "ml_s", "total_s", "peak_mb")
MARK = "BENCH_JSON:"
HARDWARE = ("Lenovo IdeaPad 1 14ADA05, AMD 3020e (2 cores), 4 GB RAM, Windows 11 Pro; "
            "work files on an external SSD")


def peak_memory_mb():
    """Peak resident memory of this process so far, in MiB."""
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        class Counters(ctypes.Structure):
            _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
                        ("PeakWorkingSetSize", ctypes.c_size_t),
                        ("WorkingSetSize", ctypes.c_size_t),
                        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                        ("PagefileUsage", ctypes.c_size_t),
                        ("PeakPagefileUsage", ctypes.c_size_t)]

        kernel32, psapi = ctypes.windll.kernel32, ctypes.windll.psapi
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        psapi.GetProcessMemoryInfo.argtypes = [
            wintypes.HANDLE, ctypes.POINTER(Counters), wintypes.DWORD]
        c = Counters()
        c.cb = ctypes.sizeof(Counters)
        psapi.GetProcessMemoryInfo(kernel32.GetCurrentProcess(), ctypes.byref(c), c.cb)
        return c.PeakWorkingSetSize / 2 ** 20
    import resource
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return peak / 2 ** 20 if sys.platform == "darwin" else peak / 1024


# ------------------------------------------------------------------ worker
def worker(config_path):
    """One complete run in this process; prints one JSON line with the measurements."""
    from leakscope import analysis, evaluation
    from leakscope.audit_scoring import collect
    from leakscope.pipeline import run_all

    cfg = load_config(config_path)
    out_dir = Path(cfg["data"]["database"]).parent / "results"
    out_dir.mkdir(exist_ok=True)
    res, sink = {}, io.StringIO()
    start = time.perf_counter()
    with contextlib.redirect_stdout(sink):          # keep stdout clean for the JSON line
        t = time.perf_counter()
        run_all(config_path)
        res["pipeline_s"] = time.perf_counter() - t
        res["peak_after_pipeline_mb"] = peak_memory_mb()

        t = time.perf_counter()
        collect(config_path)
        res["audit_s"] = time.perf_counter() - t
        res["peak_after_audit_mb"] = peak_memory_mb()

        t = time.perf_counter()
        evaluation.run_experiments(config_path, str(out_dir))
        analysis.run_analysis(config_path, str(out_dir))
        res["ml_s"] = time.perf_counter() - t
    res["total_s"] = time.perf_counter() - start
    res["peak_mb"] = peak_memory_mb()
    res["steps"] = {name.strip(): float(sec) for name, sec in
                    re.findall(r"\[ok\] (.+?)\s+([\d.]+)s", sink.getvalue())}
    con = connect(cfg)
    try:
        res["n_examples_actual"] = con.execute(
            "SELECT count(*) FROM prediction_examples").fetchone()[0]
        res["n_clean_trips"] = con.execute("SELECT count(*) FROM clean_trips").fetchone()[0]
    finally:
        con.close()
    print(MARK + json.dumps(res))


# ------------------------------------------------------------------ parent
def one_run(n_examples, run, work_dir, trips_per_example, n_boot):
    from leakscope.synthetic import generate, make_demo_config

    work = Path(work_dir) / f"n{n_examples}"
    shutil.rmtree(work, ignore_errors=True)
    generate(work, n_trips=trips_per_example * n_examples)
    cfg = make_demo_config(work, "configs/taxi.yaml", n_examples=n_examples, n_boot=n_boot)
    proc = subprocess.run(
        [sys.executable, "-m", "leakscope.benchmark", "--worker", str(cfg)],
        capture_output=True, text=True)
    line = next((x for x in proc.stdout.splitlines() if x.startswith(MARK)), None)
    if proc.returncode != 0 or line is None:
        raise RuntimeError(f"benchmark worker failed (exit {proc.returncode}):\n"
                           f"{proc.stderr[-2000:]}")
    res = json.loads(line[len(MARK):])
    steps = res.pop("steps")
    res.update({"step_" + re.sub(r"\W+", "_", k): v for k, v in steps.items()})
    return dict(n_examples=n_examples, run=run, **res)


def summarize(runs):
    """One row per size: median, min and max of every metric, plus the first run's total."""
    df = pd.DataFrame(runs)
    rows = []
    for n, d in df.groupby("n_examples", sort=True):
        row = dict(n_examples=int(n), n_runs=len(d),
                   n_examples_actual=int(d["n_examples_actual"].iloc[0]),
                   n_clean_trips=int(d["n_clean_trips"].iloc[0]))
        for m in METRICS:
            row[f"{m}_median"] = float(d[m].median())
            row[f"{m}_min"] = float(d[m].min())
            row[f"{m}_max"] = float(d[m].max())
        row["total_s_first_run"] = float(d.sort_values("run")["total_s"].iloc[0])
        rows.append(row)
    return rows


def run_benchmark(sizes=SIZES, runs=3, out_dir="results", work_dir="bench_work",
                  trips_per_example=2, n_boot=500, hardware=HARDWARE):
    out = Path(out_dir)
    out.mkdir(exist_ok=True)
    rows = []
    for r in range(runs):
        for n in sizes:
            print(f"run {r + 1}/{runs}, {n:,} examples ...", flush=True)
            row = one_run(n, r, work_dir, trips_per_example, n_boot)
            print(f"    total {row['total_s']:.1f} s, peak {row['peak_mb']:.0f} MiB", flush=True)
            rows.append(row)
            pd.DataFrame(rows).to_csv(out / "benchmark_runs.csv", index=False)  # keep partial results
    summary = summarize(rows)
    pd.DataFrame(summary).to_csv(out / "benchmark_summary.csv", index=False)
    duck = load_config("configs/taxi.yaml")["duckdb"]
    info = dict(hardware=hardware, sizes=list(sizes), runs=runs,
                trips_per_example=trips_per_example, n_boot=n_boot,
                duckdb_memory_limit=duck["memory_limit"], duckdb_threads=duck["threads"],
                python=platform.python_version(), sklearn=sklearn.__version__,
                duckdb=duckdb.__version__, platform=platform.platform(),
                cpu_count=os.cpu_count())
    (out / "benchmark_info.json").write_text(json.dumps(info, indent=2), encoding="utf-8")
    print_summary(summary, info)
    return rows, summary


def _span(row, m, digits=1):
    return (f"{row[m + '_median']:.{digits}f} "
            f"[{row[m + '_min']:.{digits}f}-{row[m + '_max']:.{digits}f}]")


def print_summary(summary, info):
    print(f"\nHardware: {info['hardware']}")
    print(f"{info['runs']} runs per size, fresh process each; median [min-max]; "
          f"trips = {info['trips_per_example']} x examples (synthetic data)\n")
    print(f"{'examples':>9} {'trips':>8} {'pipeline s':>18} {'audit s':>16} "
          f"{'ML s':>18} {'total s':>18} {'peak MiB':>18} {'1st run s':>10}")
    print("-" * 125)
    for r in summary:
        print(f"{r['n_examples']:>9,} {r['n_clean_trips']:>8,} {_span(r, 'pipeline_s'):>18} "
              f"{_span(r, 'audit_s'):>16} {_span(r, 'ml_s'):>18} {_span(r, 'total_s'):>18} "
              f"{_span(r, 'peak_mb', 0):>18} {r['total_s_first_run']:>10.1f}")
    short = [r for r in summary if r["n_examples_actual"] < r["n_examples"]]
    for r in short:
        print(f"WARNING: only {r['n_examples_actual']:,} of {r['n_examples']:,} examples "
              f"were available at this size")
    print("\nPeak MiB = peak working set of the process (whole run, cumulative). "
          "1st run = total time of the first repetition (cold caches).")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Runtime and peak-memory benchmark")
    parser.add_argument("--worker", metavar="CONFIG", help="internal: run one measured run")
    parser.add_argument("--sizes", default=",".join(str(s) for s in SIZES))
    parser.add_argument("--runs", type=int, default=3)
    args = parser.parse_args()
    if args.worker:
        worker(args.worker)
    else:
        run_benchmark(sizes=tuple(int(s) for s in args.sizes.split(",")), runs=args.runs)