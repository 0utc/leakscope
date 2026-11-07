# Devlog

## 2026-11-07
- Set up environment on O: SSD (Python 3.13, venv, portable Git on PATH).
- Defined task: predict trip duration at pickup time.
- Defined 4 rule types (R1-R4), 8 leaky scenarios, 4 negative controls.
- Decision: availability times are simulated with configurable lags,
  because NYC TLC data does not record when fields became available.