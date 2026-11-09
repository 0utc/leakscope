"""Command-line interface: python -m leakscope <command>

  demo        one command: synthetic data -> pipeline -> audit -> ML comparison -> report
  synthetic   write the synthetic demo data only
  pipeline    build the database from the raw data in the config
  audit       audit the feature registry (findings with explanations + scorecard)
  experiment  leaky-vs-corrected ML comparison, uncertainty, production conditions
  report      build results/report.md, results/audit_report.json and the figures

Run from the project root (the feature registry is read from configs/features.yaml).
"""
import time
from pathlib import Path
from typing import Annotated

import typer

app = typer.Typer(
    add_completion=False, no_args_is_help=True,
    help="LeakScope: auditing what information was actually available at prediction time.")

Config = Annotated[Path, typer.Option("--config", "-c", help="YAML config file.")]
DEFAULT_CONFIG = Path("configs/taxi.yaml")


def _need(path):
    if not Path(path).exists():
        typer.secho(f"config not found: {path} (run from the project root)",
                    err=True, fg=typer.colors.RED)
        raise typer.Exit(2)


def _scenario_key(row):
    return (row["scenario"][0] == "N", int(row["scenario"][1:]))


def _print_findings(rows):
    print("Findings (what the auditor reports)")
    print("-" * 36)
    any_found = False
    for r in sorted(rows, key=_scenario_key):
        for f in r["findings"]:
            any_found = True
            ids = ", ".join(str(i) for i in f.affected_trip_ids)
            print(f"- [{f.rule}] {f.explanation} First affected trip IDs: {ids}.")
    if not any_found:
        print("(none)")
    print()


def _run_audit(config, explain):
    from leakscope.audit_scoring import collect, print_scorecard
    rows, conformance, limitation = collect(str(config))
    if explain:
        _print_findings(rows)
    print_scorecard(rows, conformance, limitation)
    bad = [r["feature"] for r in rows if r["expected"] != r["fired"]]
    if bad:
        typer.secho(f"\nSCORECARD MISMATCH for: {', '.join(bad)}", err=True,
                    fg=typer.colors.RED)
        raise typer.Exit(1)


@app.command()
def synthetic(
    out: Annotated[Path, typer.Option("--out", help="Output folder.")] = Path("demo_work"),
    n_trips: Annotated[int, typer.Option(help="Number of synthetic trips.")] = 120_000,
    seed: Annotated[int, typer.Option(help="Random seed.")] = 7,
):
    """Write deterministic synthetic taxi data (same schema as the NYC TLC files)."""
    from leakscope.synthetic import generate
    paths = generate(out, n_trips=n_trips, seed=seed)
    typer.echo(f"wrote {paths['parquet']}")
    typer.echo(f"wrote {paths['zones']}")


@app.command()
def pipeline(config: Config = DEFAULT_CONFIG):
    """Build the DuckDB database: cleaning, availability times, features."""
    _need(config)
    from leakscope.pipeline import run_all
    run_all(str(config))


@app.command()
def audit(
    config: Config = DEFAULT_CONFIG,
    explain: Annotated[bool, typer.Option("--explain/--no-explain",
                                          help="Print every finding with its explanation.")] = True,
):
    """Audit the feature registry; exits with code 1 if a scenario is mis-detected."""
    _need(config)
    _run_audit(config, explain)


@app.command()
def experiment(
    config: Config = DEFAULT_CONFIG,
    out: Annotated[Path, typer.Option("--out", help="Results folder.")] = Path("results"),
    skip_analysis: Annotated[bool, typer.Option(
        help="Only the leaky-vs-corrected table, no bootstrap/production/ablations.")] = False,
):
    """Leaky vs corrected ML comparison (plus uncertainty, production, ablations)."""
    _need(config)
    from leakscope import analysis, evaluation
    evaluation.run_experiments(str(config), str(out))
    if not skip_analysis:
        print()
        analysis.run_analysis(str(config), str(out))


@app.command()
def report(
    config: Config = DEFAULT_CONFIG,
    out: Annotated[Path, typer.Option("--out", help="Results folder.")] = Path("results"),
    assets: Annotated[Path, typer.Option(help="Folder for the figures.")] = Path("assets"),
):
    """Build the Markdown report, the JSON audit and the figures."""
    _need(config)
    from leakscope.reporting import build_report
    build_report(str(config), str(out), str(assets))


@app.command()
def demo(
    work_dir: Annotated[Path, typer.Option(help="Private folder for data and database.")]
    = Path("demo_work"),
    out: Annotated[Path, typer.Option(help="Folder for the demo results.")] = Path("demo_results"),
    n_trips: Annotated[int, typer.Option(help="Synthetic trips to generate.")] = 120_000,
    n_examples: Annotated[int, typer.Option(help="Prediction examples to sample.")] = 20_000,
    n_boot: Annotated[int, typer.Option(help="Bootstrap resamples.")] = 500,
    base_config: Annotated[Path, typer.Option(help="Config whose settings are reused.")]
    = DEFAULT_CONFIG,
):
    """One command: synthetic data, pipeline, audit, ML comparison and report.

    Everything is written to demo_work/ and demo_results/; your real database
    and results are never touched.
    """
    _need(base_config)
    from leakscope import analysis, evaluation
    from leakscope.pipeline import run_all
    from leakscope.reporting import build_report
    from leakscope.synthetic import generate, make_demo_config

    start = time.perf_counter()

    def stage(i, title):
        print(f"\n=== {i}/6 {title} " + "=" * max(4, 70 - len(title)))

    stage(1, "Synthetic taxi data (not real data)")
    generate(work_dir, n_trips=n_trips)
    cfg = make_demo_config(work_dir, str(base_config), n_examples, n_boot)
    print(f"{n_trips:,} synthetic trips written to {work_dir}")

    stage(2, "Pipeline: cleaning, availability times, features")
    run_all(str(cfg))

    stage(3, "Audit: leaky features are flagged, valid ones are not")
    _run_audit(cfg, explain=True)

    stage(4, "ML comparison: corrected vs leaky models")
    evaluation.run_experiments(str(cfg), str(out))

    stage(5, "Uncertainty, production conditions and ablations")
    analysis.run_analysis(str(cfg), str(out))

    stage(6, "Report")
    build_report(str(cfg), str(out), str(out / "figures"))
    print(f"\nDone in {time.perf_counter() - start:.0f}s. Open {out / 'report.md'}")


if __name__ == "__main__":
    app()