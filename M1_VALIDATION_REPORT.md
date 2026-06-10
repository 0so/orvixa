# ORVIXA — Milestone 1 Validation Report

Scope: validate the M1 deliverable (`orvixa/` — BinanceFeed & local dev
environment) against the approved Phase 2 architecture and implementation
plan, before proceeding to M2 (persistence / TimescaleDB). No M2 work has
been started.

## Go / No-Go: **GO** (after fixes applied below)

M1 was **NO-GO as generated** — two bugs broke both documented entry points
(`make dev` / `make feedcheck` crashed on startup, and `make test` had a
failing test). Both have been fixed in this pass, are minimal and
architecture-neutral, and the suite now passes end-to-end. See "Fixes
applied" below for the diff summary.

---

## 1. Bugs found & fixed

### 1.1 CRITICAL — `config.py` crashed on the documented `.env` (blocked `make dev` / `make feedcheck`)

`Settings.core_symbols` / `seed_symbols` were `list[str]` fields with a
`mode="before"` validator (`_split_csv`) intended to turn
`"BTCUSDT,ETHUSDT,SOLUSDT"` into `["BTCUSDT", "ETHUSDT", "SOLUSDT"]`.

pydantic-settings v2 attempts to **JSON-decode** any env value bound to a
`list`/`set`/`tuple` field *before* field validators run. A CSV string isn't
valid JSON, so loading `.env.example` (exactly as the README's "Quickstart"
instructs: `cp .env.example .env`) raised immediately:

```
pydantic_settings.exceptions.SettingsError: error parsing value for field "core_symbols" from source "EnvSettingsSource"
```

Reproduced before the fix; every documented entry point (`make dev`,
`FEED=sim|binance make feedcheck`) crashed at the first line of
`get_settings()`. It only "worked" when no `.env` was present (defaults
bypass env decoding) — which is why `make test` never caught it (no
`test_config.py`).

**Fix applied** (`src/orvixa/config.py`): annotated both fields as
`Annotated[list[str], NoDecode]` (from `pydantic_settings`) so the raw CSV
string reaches `_parse_symbol_lists` / `_split_csv` unmodified. Verified:
loading `.env.example` now yields the expected lists and `all_symbols`.

### 1.2 HIGH — `make test` was red: `test_backoff_then_reconnect` failed

`tests/test_reconnect.py::_FakeWS._gen` had no `yield`, so it was a plain
coroutine, not an async generator. `ws.__aiter__()` returned an unawaited
coroutine; `async for raw in ws` raised `TypeError`, which `BinanceFeed._run`
caught as "connection lost" and treated as a failed connection — even on the
"successful" socket. Result: 6 backoff attempts instead of 2, hit
`max_reconnects`, and the test's `assert len(feed.backoff_history) == 2`
failed (`6 == 2`).

The other two tests in the same file reused this broken fixture and passed
"by accident" — their assertions didn't depend on the connection staying up,
so the spurious reconnect storm didn't fail them, but they weren't testing a
stable connected session either.

**Fix applied** (`tests/test_reconnect.py`): made `_gen` a real (empty) async
generator (`return` followed by an unreachable `yield`), matching the
correct pattern already used in `tests/test_feed_contract.py`.

**Result**: `pytest -q` → `20 passed` (was `19 passed, 1 failed`).

### 1.3 LOW — lint/mypy nits (fixed)

- `feeds/binance.py`: removed unused `TickerRow` import (F401).
- `feeds/binance.py`: `asyncio.TimeoutError` → builtin `TimeoutError`
  (UP041); added `# noqa: ASYNC109` on `wait_connected`'s `timeout` param
  (kept the simple `asyncio.wait_for` shape rather than refactor to
  `asyncio.timeout`, to stay minimal for M1).
- `feeds/binance.py`: removed now-unused `# type: ignore[union-attr]` on the
  `async for raw in ws:` line (was failing `mypy --warn-unused-ignores`).
- `logging.py`: `datetime.timezone.utc` → `datetime.UTC` (UP017).
- `tests/test_feed_contract.py`: removed unused `import pytest` (F401).

`ruff check` now reports only **2 remaining ASYNC110** findings (busy-wait
`while not self.closed: await asyncio.sleep(...)` in both fake-socket test
fixtures). These are cosmetic, test-only, and not worth restructuring for M1
— flagged for a future cleanup pass, not blocking.

`mypy src` → **clean** (was 1 error).

---

## 2. M1 Validation Checklist

Mapped to the M1 "Definition of Done" and task groups (A–F) in
`ORVIXA Phase 2 - Implementation Plan.html`.

| # | Item | Status | Notes |
|---|---|---|---|
| A1 | Repo scaffold (`pyproject.toml`, ruff/mypy/pytest config) | ✅ | matches blueprint layout |
| A2 | Makefile targets (`dev`, `feedcheck`, `test`, `fmt`, `lint`, `down`) | ✅ | all present, tab-indented correctly |
| B1 | `config.py` — pydantic-settings, `.env`, symbol list parsing | ✅ (fixed) | CSV env vars now parse correctly |
| B2 | `logging.py` — structured JSON logs, level from env | ✅ (fixed) | `datetime.UTC` cleanup |
| C1 | `feeds/base.py` — `MarketFeed` ABC + `Candle` / `TickerRow` | ✅ | clean contract, fan-out isolates consumer exceptions |
| C2 | `feeds/sim.py` — ported simulator, deterministic with seed | ✅ | `test_simfeed_is_deterministic_with_seed` passes |
| D1 | `feeds/binance.py` — combined WS (kline + miniTicker) | ✅ | |
| D2 | Candle-close gating on `k.x` | ✅ | `test_kline_open_flag_when_not_closed` |
| D3 | Reconnect: backoff + jitter, resubscribe, gap-fill | ✅ (fixed) | `test_reconnect.py` now green and meaningful |
| D4 | `feeds/normalize.py` — symbol normalization, `1000x` meme mapping | ✅ | locked by `kline_1m.json` fixture |
| E1 | `runners/feedcheck.py` — candle log + breadth summary | ✅ | manually verified with `FEED=sim` |
| E2 | `docker-compose.dev.yml` — postgres (Timescale) + redis + app, healthchecks | ✅ | Postgres/Redis idle by design (M2) |
| F1 | Unit tests — normalization fixture | ✅ | 8/8 pass |
| F2 | Contract tests — SimFeed & BinanceFeed via shared assertions | ✅ | 4/4 pass |
| F3 | Reconnect tests — backoff/resubscribe/gap-fill, offline | ✅ (fixed) | 3/3 pass |
| F4 | Integration tests (opt-in, `RUN_NET_TESTS=1`) | ⚠️ N/A | none written — acceptable per plan (opt-in, network-gated, not required for M1 DoD) |
| DoD1 | `make dev` brings up stack; live candles within 60s | ⚠️ Partial | code path verified correct; **live Binance reachability from this sandbox returns HTTP 403** (geo/WAF) — see Risks. `FEED=sim` path verified working end-to-end |
| DoD2 | Dropping network → backoff + gap-fill, no missing minutes | ✅ | proven by `test_reconnect.py` (offline, deterministic) |
| DoD3 | `FEED=sim` / `FEED=binance` pure config swap | ✅ | `factory.py` is the single switch point |
| DoD4 | Unit + contract + reconnect tests pass | ✅ (fixed) | `20 passed` |
| DoD5 | No DB writes, no HTTP server, no Binance types past `feeds/` | ✅ | confirmed by reading `factory.py`, `feedcheck.py`; Postgres/Redis untouched |

---

## 3. Architecture consistency

Cross-checked against `ORVIXA Phase 2 - Architecture.html` and
`ORVIXA Phase 2 - Implementation Plan.html`:

- Repo layout, `MarketFeed`/`Candle`/`TickerRow` contract, `SimFeed`/
  `BinanceFeed` split, `feedcheck` runner, compose services, `.env` keys, and
  task groups A–F all match the approved blueprint.
- `core_symbols` defaults (BTC/ETH/SOL = Tier 0) and `seed_symbols`
  (Tier-1 seed incl. memecoins) align with the Phase 2 tiering model.
- **Naming note for M4 (engine port)**: the blueprint's pseudocode names
  candle fields `o/h/l/c/v/quote_v`, while the real `Candle` dataclass uses
  `open/high/low/close/volume/quote_volume`. This is more readable and not a
  defect, but the M4 parity tests against `engine/orvixa-engine.js` (which
  uses `o/h/l/c/v`) will need an explicit field-name mapping — flag this when
  scoping M4, not an M1 blocker.
- Postgres/Redis are provisioned in `docker-compose.dev.yml` but
  intentionally idle, exactly as specified ("provisioned now, used from
  M2/M5").

---

## 4. Risks / things to watch when running `make dev` or `make test`

1. **`FEED=binance` default + outbound network.** `.env.example` ships with
   `FEED=binance`. From this container, `https://api.binance.com` returns
   `403` (geo/WAF block) — `make dev` with the shipped `.env` will sit in the
   reconnect-backoff loop rather than show candles. Not a code defect (the
   architecture explicitly designs for this — `FEED=sim` fallback), but
   worth a one-line callout in the README's quickstart so a first run on a
   blocked network isn't mistaken for a bug. On an unblocked VPS this should
   work as documented.
2. **`docker-compose.dev.yml` requires `.env` to exist** (`env_file: .env`).
   `docker compose up` fails with a missing-file error if the documented
   `cp .env.example .env` step is skipped. Expected, just confirming.
3. **No `test_config.py`.** The config bug above had zero test coverage.
   Recommend adding a small settings test (env-var CSV parsing, `all_symbols`
   dedup, invalid `FEED` value) before/while doing M2 — cheap insurance
   against regressions in `config.py`, which M2 will extend.
4. Two residual `ASYNC110` lint findings in test fixtures (busy-wait
   `while not closed: sleep(0.01)`); cosmetic, doesn't affect `make test`/
   `make dev`.

---

## 5. Verification performed

```
pip install -e ".[dev]"
ruff check src tests        # 2 cosmetic ASYNC110 (test-only), down from 7
mypy src                     # clean (was 1 error)
pytest -q                    # 20 passed (was 19 passed, 1 failed)

cp .env.example .env (env vars sourced) → get_settings()  # now succeeds,
  core_symbols/seed_symbols/all_symbols correct

FEED=sim make feedcheck      # candles + breadth lines emit correctly
```

`make dev` (docker compose with live Binance) was not run end-to-end in this
sandbox due to the network block noted above — the code path (connector,
backfill, reconnect, normalize) is fully covered by the offline contract and
reconnect tests instead.

---

## 6. Recommendation

**Proceed to M2 (persistence / TimescaleDB).** M1's `MarketFeed` contract
(`on_candle_close` / `on_market_snapshot`, `Candle` / `TickerRow`) is stable,
tested, and the only seam M2's batched writers need to attach to. The two
blocking bugs are fixed and verified; remaining items (config test coverage,
README network-fallback note, two cosmetic lint findings) are non-blocking
and can be picked up incidentally during M2 without re-litigating M1 scope.
