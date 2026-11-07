# Progress tracker (started 2026-11-07)

## Phase 0: Setup and scope
- [x] Environment on O: (Python 3.13, venv, packages verified)
- [x] Project skeleton created
- [x] docs/scope.md written
- [x] First commit pushed to GitHub

## Phase 1: Data and pipeline  (GATE: load data, build one safe feature, retrieve it correctly)
- [x] Download NYC TLC data, data dictionary
- [x] DuckDB ingestion + cleaning
- [x] Temporal data model (simulated availability times)
- [x] Feature registry (configs/features.yaml + registry.py validation)
- [x] Feature tables (safe + intentionally leaky); aggregates N2/N3 computed in the point-in-time join step
- [x] Point-in-time SQL join
- [x] First end-to-end pipeline + correctness tests

## Phase 2: Auditor  (GATE: catches all leaky scenarios, ignores all negative controls)
- [ ] Scenario L9 (R2-only: last completed trip's fare looked up by dropoff time)
- [ ] Rules R1-R4 implemented
- [ ] Provenance / feature-dependency checks
- [ ] Deliberately wrong joins for L5-L8 (self-inclusion, in-progress trips, fare correction, post-t_pred snapshot)
- [ ] 12 scenario fixtures (8 leaky, 4 negative)
- [ ] Positive/negative test suite

## Phase 3: ML experiments  (GATE: reproducible leaky vs corrected comparison)
- [ ] Small-window aggregates (zone x last 60 min) so L5/L6 leakage is measurable
- [ ] Chronological split + baseline
- [ ] Leaky vs corrected models
- [ ] MAE/RMSE analysis

## Phase 4: Packaging
- [ ] Reports (Markdown/HTML)
- [ ] CLI
- [ ] CI + robustness tests
- [ ] Benchmarks (10k/50k/200k)
- [ ] README, docs, charts, demo
- [ ] LaTeX report
- [ ] Final clean-install test + release