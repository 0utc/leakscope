"""Tests for the command-line interface, including a full end-to-end demo run."""
from typer.testing import CliRunner

from leakscope.cli import app

runner = CliRunner()


def test_help_lists_all_commands():
    r = runner.invoke(app, ["--help"])
    assert r.exit_code == 0
    for command in ("demo", "synthetic", "pipeline", "audit", "experiment", "report"):
        assert command in r.output


def test_missing_config_gives_a_clear_error(tmp_path):
    r = runner.invoke(app, ["audit", "--config", str(tmp_path / "nope.yaml")])
    assert r.exit_code == 2 and "config not found" in r.output


def test_synthetic_command_writes_the_files(tmp_path):
    r = runner.invoke(app, ["synthetic", "--out", str(tmp_path), "--n-trips", "1500"])
    assert r.exit_code == 0
    assert (tmp_path / "yellow_tripdata_synthetic.parquet").exists()
    assert (tmp_path / "taxi_zone_lookup.csv").exists()


def test_demo_runs_end_to_end_and_the_audit_command_agrees(tmp_path):
    work, out = tmp_path / "work", tmp_path / "out"
    r = runner.invoke(app, [
        "demo", "--work-dir", str(work), "--out", str(out),
        "--n-trips", "10000", "--n-examples", "2500", "--n-boot", "100"])
    assert r.exit_code == 0, r.output
    assert "exact rule-set match: 19/19" in r.output
    for name in ("report.md", "audit_report.json", "ml_comparison.csv",
                 "ml_uncertainty.csv", "ml_production.csv", "ml_ablation.csv",
                 "ml_run_info.json"):
        assert (out / name).exists(), name
    assert (out / "figures" / "fig_offline_vs_production.png").exists()
    assert "## Summary" in (out / "report.md").read_text(encoding="utf-8")

    # the standalone audit command on the demo database reaches the same verdict
    r2 = runner.invoke(app, ["audit", "--config", str(work / "demo.yaml"), "--no-explain"])
    assert r2.exit_code == 0 and "19/19" in r2.output