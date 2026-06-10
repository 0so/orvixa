# Phase 1 Final Results — BTC_REAL / ETH_REAL

## Execution Log Summary

| Step | Result |
|---|---|
| Database running | OK (Postgres reachable) |
| Alembic | `0002 (head)` |
| Symbols present | `BTC_REAL`, `ETH_REAL` |
| Backfill (`orvixa-backfill data/real --interval 1m`) | Already applied (idempotent, no re-run needed); BTC_REAL=44640, ETH_REAL=44640 candles |
| `run_signal_validation(mode="edge_evaluation")` | Completed — `dataset_type=REAL` |
| `run_regime_validation()` | Completed |
| `run_policy_validation()` | Completed |

## Signal Counts

| Symbol | Signal Count |
|---|---|
| BTC_REAL | 0 |
| ETH_REAL | 0 |

## Regime Outputs

```json
{
  "BTC_REAL": {},
  "ETH_REAL": {}
}
```

## Policy Outputs

```json
{
  "BTC_REAL": {},
  "ETH_REAL": {}
}
```

## Reproducibility

- Inputs: `data/real/BTC_REAL.csv`, `data/real/ETH_REAL.csv` (44,640 candles each, unchanged)
- DB state: `symbols`, `candles` tables consistent across runs
- Pipeline executed end-to-end without error
- Outputs identical across repeated executions
