# Phase 1 Observational Audit — BTC_REAL / ETH_REAL (May 2026)

**Status: Pipeline operational. Zero signals expected and explained. No code defects found.**

## 1. Scope

- Data: `data/real/BTC_REAL.csv`, `data/real/ETH_REAL.csv` (1m OHLCV, 2026-05-01 → 2026-05-31, 44,640 candles each)
- Pipeline: ingestion → backfill → `run_signal_validation` (`edge_evaluation`) → `run_regime_validation` → `run_policy_validation`
- Stack: frozen (no code, threshold, schema, or CSV changes)

## 2. Pipeline Execution Status

| Stage | Result |
|---|---|
| DB / migrations | OK — Alembic at head (`0002`) |
| Symbol registration | OK — `BTC_REAL`, `ETH_REAL` registered, untagged |
| Backfill | OK — 44,640 candles/symbol, matches CSV row counts, no gaps |
| `classify_dataset` | `REAL` (correct) |
| `run_signal_validation(edge_evaluation)` | Ran without error |
| `run_regime_validation` / `run_policy_validation` | Ran without error |
| Indicator warmup | OK — 44,620/44,640 candles produce valid trend/RSI/vol_rel |

No exceptions, gaps, NaNs, or misalignments detected at any stage.

## 3. Confidence Ceiling: Observed vs Theoretical

Confidence formula (unchanged):
```
confidence = 0.6 * trend.strength + 0.2 * rsi_component + 0.2 * vol_component
rsi_component = min(|rsi - 50| * 2, 100)
vol_component = min(max((vol_rel - 1) * 50, 0), 100)
```

| Symbol | Observed max confidence | Theoretical max confidence (best-case observed components) | Threshold | Gap to threshold |
|---|---|---|---|---|
| BTC_REAL | 30 (eligible candles) / 41 (all) | 43.6 | 60 | 16.4 |
| ETH_REAL | 35 (eligible candles) / 43 (all) | 48.4 | 60 | 11.6 |

Theoretical max assumes RSI and vol_rel components both saturate at their observed historical maxima *simultaneously* with the highest observed `trend.strength` — a best-case, not realistic, combination. Even under this generous assumption, neither symbol crosses 60.

## 4. Why Signals Cannot Fire (Root Cause)

`trend.strength` carries 60% of the confidence weight. Required value to reach 60 (assuming RSI/vol components both at formula maximum of 100):

```
0.6 * strength + 40 = 60  →  strength ≥ 33.33
```

| Symbol | Observed mean trend.strength | Observed max trend.strength | % of required (33.33) |
|---|---|---|---|
| BTC_REAL | 0.62 | 8.13 | 24.4% |
| ETH_REAL | 0.78 | 15.20 | 45.6% |

`trend.strength` never approached the required floor in 44,620 evaluated candles per symbol (a full month of 1m data). RSI and vol_rel components are already near-saturated in this data (RSI swings 5–99, vol_rel spikes to 100+×); they are not the limiting factor. The structural ceiling is set by `trend.strength`, which is a function of the EMA-fast/EMA-slow percentage spread (`diff_pct`) — this spread simply does not reach the magnitude the formula's 60%-weight term requires, for this asset/period.

## 5. Theoretical Scenarios Where Signals Could Fire (No Code Changes)

Without modifying code/thresholds, signals would fire only if the **input data** itself produced:
- A sustained EMA-fast/EMA-slow divergence large enough to push `trend.strength` ≥ ~33 (requires materially higher trend persistence/volatility than seen in May 2026), **and**
- RSI simultaneously deep in trend-confirming territory (not overbought on an uptrend / not oversold on a downtrend), **and**
- `vol_rel` ≥ 3 (to fully saturate the vol component).

These conditions describe a strongly trending, high-volatility regime (e.g., a sharp breakout/breakdown period) — not present in the ingested May-2026 window. No combination of analysis on the *current* dataset can produce this; it would require a different real data window with materially different volatility/trend characteristics. This is an observational note only — no data was substituted or altered.

## 6. Confirmation: Zero Signals Are Expected

Given:
- The formula and threshold are fixed (frozen),
- The theoretical confidence ceiling for this exact dataset (43.6 BTC / 48.4 ETH) is below 60,
- All upstream stages (ingestion, indicators, trend, RSI, vol_rel) compute correctly and without error,

**zero signals is the mathematically correct, expected output for this data window — not an error, gap, or silent failure.**

## 7. Team Summary

> **Phase 1 pipeline status: OPERATIONAL.** Ingestion (44,640 candles/symbol, BTC_REAL & ETH_REAL, May 2026), classification (REAL), and the full signal → regime → policy validation chain executed end-to-end without errors. Zero signals were emitted for both symbols; this is **expected and explained**: the data's `trend.strength` (mean 0.62 BTC / 0.78 ETH, max 8.1 / 15.2) caps theoretical confidence at 43.6 (BTC) / 48.4 (ETH), below the `signal_min_confidence=60` threshold by 16.4 / 11.6 points. RSI and volatility components are already near their formula maxima in this data and are not the limiting factor. **No code, threshold, schema, or data defects identified.**

## 8. Recommendations (Observational Only — No Changes Made)

1. Repeat this same diagnostic on additional real data windows (different months / higher-volatility periods) to determine whether `trend.strength` ever reaches the ~33 floor on real market data, purely as an observational data-gathering exercise.
2. If a synthetic baseline dataset becomes available, run the identical read-only diagnostic against it to compare `trend.strength` distributions (none was accessible during this audit — no synthetic symbols/data exist in the current DB).
3. Treat the `signal_min_confidence=60` vs. observed `trend.strength` scale gap as a **calibration question** for a future, separately-scoped review — out of scope for this frozen-stack Phase 1 execution.
4. Continue to log confidence-score distributions for each future ingestion as a standing observational metric, without altering the production formula or threshold.
