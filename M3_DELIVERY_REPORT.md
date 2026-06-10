# M3 Delivery Report — Symbol Manager

Status: **complete**, scoped strictly to the approved M3 plan ("automatic
discovery / tiering / ranking / breadth / promotion-demotion"). M1's feed
layer/architecture and M2's persistence pipeline are unmodified except for
one additive migration and one new repository method needed for the M3
persistence requirement. Milestone 4 has not been started.

## 1. Architecture summary

The Symbol Manager (`src/orvixa/symbols/manager.py`) is a periodic service
that discovers the live Binance USDT spot universe, ranks it, assigns/updates
tiers, persists the result, and keeps the existing feed's subscription set in
sync — all without touching the feed architecture.

```
                 ┌────────────────────┐
                 │ BinanceMarketClient │  /exchangeInfo, /ticker/24hr
                 └─────────┬──────────┘
                            │ ExchangeSymbol[], TickerStats{}
                            ▼
┌───────────────────────────────────────────────────────────┐
│                      SymbolManager                          │
│                                                              │
│  refresh_universe():                                        │
│    1. _rank_universe   -> global rank_of, alt-only top_n    │
│    2. _mark_delistings -> freeze symbols no longer active   │
│    3. _sync_listings   -> discover new pairs, update stats  │
│    4. _assign_tiers    -> tier 0/1/2 + promotion/demotion    │
│    5. _persist(*)      -> SymbolRepository.upsert +          │
│                            update_ranking (per symbol)       │
│    6. _sync_feed       -> feed.subscribe/unsubscribe diff    │
│                                                              │
│  handle_snapshot(rows) -> BreadthEngine.update -> breadth   │
│  get_watchlist(sort_by) -> build_watchlist + sort_watchlist │
└───────────┬───────────────────────────────┬────────────────┘
            │                                │
            ▼                                ▼
   ┌──────────────────┐           ┌─────────────────────┐
   │ SymbolRepository  │           │   MarketFeed (M1)    │
   │  .upsert          │           │   .subscribe         │
   │  .update_ranking  │           │   .unsubscribe       │
   │  .list_all        │           │   .on_market_snapshot│
   └──────────────────┘           └─────────────────────┘
```

### Tiering

- **Tier 0 (core)**: `settings.core_symbols` (default BTC/ETH/SOL) — fixed.
- **Tier 1 (watch)**: curated meme set (`settings.meme_symbols`) + the top
  `tier1_size` *alts* by volume/activity score (core and meme symbols don't
  consume top-N slots), plus any Tier-2 symbol currently spike-promoted.
- **Tier 2 (rest)**: everything else tradable.

Ranking is computed twice per cycle:
- a **global rank** over the whole active universe (incl. core), persisted
  as `symbols.rank` / `metrics.rank` for display/diagnostics;
- an **alt-only top-N** (excluding core/meme bases) used to decide Tier-1
  membership, since core/meme already guarantee Tier ≥ 1 regardless of rank.

### Promotion / demotion

A Tier-2 symbol is "spiking" if its 24h quote volume or trade count grows by
`promotion_volume_multiplier` (default 3x) vs. the previous cycle, or its 24h
change exceeds `promotion_volatility_pct` (default 8%). Spiking symbols are
promoted to Tier 1 and tagged `"spike"`. Once calm, a spike-promoted symbol
gets `demotion_grace_cycles` (default 3) consecutive calm cycles before
falling back to Tier 2 — avoiding tier flapping. The `"spike"` tag is
persisted, so a restart correctly reconstructs `_spike_promoted` via
`_load_existing()`.

### Delisting / relisting

A symbol whose pair disappears from `/exchangeInfo`'s active set is marked
`status="frozen"` (tier/class/tags untouched) and a `"delisted"` `TierChange`
is recorded. If it reappears, status flips back to `"trading"` ("relisted").

### Breadth

`BreadthEngine` consumes the feed's existing `on_market_snapshot` stream
(whole-market `TickerRow[]`, already part of the M1 `MarketFeed` contract —
no feed changes needed). Per refresh it tracks a rolling per-symbol price
history (`breadth_trend_window`, default 20) to compute advancers/decliners/
unchanged, advance/decline ratio, % above rolling-average trend, and new
highs/lows.

### Watchlist

`get_watchlist(sort_by="volume"|"volatility"|"change")` returns Tier 0+1
symbols (`build_watchlist` filters, `sort_watchlist` sorts — `volatility` is
absolute change, `change` is signed, `volume` is quote volume; `ValueError`
on an unknown `sort_by`).

### Feed integration

`SymbolManager` never touches `BinanceFeed`/`SimFeed` internals — it only
calls the existing `subscribe()`/`unsubscribe()`/`on_market_snapshot()`
methods from `MarketFeed` (M1). On construction, `_subscribed` is seeded from
`settings.all_symbols` (the same set `build_feed()` initializes the feed
with), so the very first `_sync_feed()` correctly reconciles the feed's
starting subscription set against the freshly computed Tier 0/1 watchlist —
not just future tier changes.

### Persistence

One additive migration, `alembic/versions/0002_symbol_ranking.py`:
adds `symbols.rank smallint`, `symbols.metrics jsonb NOT NULL DEFAULT '{}'`,
`symbols.last_synced timestamptz`. Applied and verified against the live
local Postgres from the M2 validation environment.

`SymbolRepository.update_ranking(base, rank, metrics)` (new method) does the
`UPDATE symbols SET rank = $1, metrics = $2::jsonb, last_synced = now() WHERE
base = $3`, JSON-encoding `metrics` (asyncpg requires JSON-encoded strings
for `jsonb` parameters absent a custom codec — confirmed live). Each refresh
cycle calls `SymbolRepository.upsert` (tier/class/status/tags) and
`update_ranking` (rank + `{quote_volume, price_change_pct, last_price,
count}`) for every tracked symbol.

### Configuration (new `Settings` fields)

| Field | Default | Purpose |
|---|---|---|
| `meme_symbols` | DOGE,SHIB,PEPE,WIF,BONK,FLOKI | Curated Tier-1 meme set |
| `symbol_refresh_interval_seconds` | 300.0 | Refresh-cycle period |
| `tier1_size` | 15 | Top-N alts by score -> Tier 1 |
| `breadth_trend_window` | 20 | Rolling snapshots for breadth trend/highs/lows |
| `promotion_volume_multiplier` | 3.0 | Spike threshold (volume/count) |
| `promotion_volatility_pct` | 8.0 | Spike threshold (24h change) |
| `demotion_grace_cycles` | 3 | Calm cycles before demotion |

`persistence/registry.py`'s meme classification now reads
`settings.meme_symbols` instead of a hardcoded set (defaults unchanged).

## 2. File tree (new/changed for M3)

```
alembic/versions/0002_symbol_ranking.py     (new) rank/metrics/last_synced migration
src/orvixa/config.py                        (mod) M3 settings block
src/orvixa/persistence/registry.py          (mod) meme set from settings
src/orvixa/db/repository.py                 (mod) + import json, SymbolRepository.update_ranking
src/orvixa/symbols/__init__.py              (new) public exports
src/orvixa/symbols/models.py                (new) ExchangeSymbol, TickerStats, RankedSymbol,
                                                    BreadthSnapshot, TierChange
src/orvixa/symbols/client.py                (new) BinanceMarketClient (/exchangeInfo, /ticker/24hr)
src/orvixa/symbols/ranking.py               (new) compute_score, rank_by_score
src/orvixa/symbols/breadth.py               (new) BreadthEngine
src/orvixa/symbols/watchlist.py             (new) build_watchlist, sort_watchlist
src/orvixa/symbols/manager.py               (new) SymbolManager (core deliverable)
src/orvixa/runners/symbols.py               (new) orvixa-symbols runner
pyproject.toml                              (mod) + orvixa-symbols script entry

tests/test_symbol_client.py                 (new)
tests/test_ranking.py                       (new)
tests/test_breadth.py                       (new)
tests/test_watchlist.py                     (new)
tests/test_symbol_manager.py                (new)
```

## 3. Test coverage summary

`PYTHONPATH=src python -m pytest -q` -> **69 passed, 1 skipped** (the skip is
the pre-existing `RUN_DB_TESTS=1`-gated M2 integration test, unrelated to M3).

- **`test_symbol_client.py`** — `/exchangeInfo` filtered to tradable USDT
  spot pairs (excludes non-USDT and non-spot-tradable, includes BREAK status);
  `/ticker/24hr` filtered to USDT pairs, malformed entries skipped. No live
  network — fake `httpx`-shaped client.
- **`test_ranking.py`** — `compute_score` ordering (volume dominates,
  activity/volatility break ties); `rank_by_score` ordering and empty input.
- **`test_breadth.py`** — advancers/decliners/unchanged/ad_ratio (incl.
  no-decliners edge case); first snapshot has no trend/highs/lows; pct above
  trend and new highs/lows after rolling history builds up.
- **`test_watchlist.py`** — Tier 0/1 filter; sort by volume/volatility
  (absolute)/change (signed); ascending order; unknown `sort_by` raises.
- **`test_symbol_manager.py`** — the core suite, fully fake-driven
  (`_FakeMarketClient` scripted multi-cycle exchange/ticker data,
  `_FakeFeed` records subscribe/unsubscribe, `FakePool` for persistence):
  - core/meme/top-N tiering on first discovery.
  - new listing discovered as Tier 2.
  - delisted symbol frozen; relisting recovers `status="trading"`.
  - volume-spike and volatility-spike promotion to Tier 1 with `"spike"` tag.
  - spike-promoted symbol demotes back to Tier 2 after `demotion_grace_cycles`
    calm cycles.
  - watchlist contains only Tier 0/1, sorted by volume.
  - `handle_snapshot` updates breadth via `BreadthEngine`.
  - feed subscribe on promotion / unsubscribe on demotion (incl. seeding
    `_subscribed` from `settings.all_symbols` so pre-existing feed
    subscriptions outside the watchlist are unsubscribed on the first cycle).
  - refresh persists tier/class/status/tags and rank/metrics (JSON-encoded
    jsonb) via `SymbolRepository`.
  - restart reconstructs `_spike_promoted`/tier/rank/tags from
    `_load_existing()`.

`ruff check src tests` — clean except the 2 pre-existing M1 `ASYNC110`
findings in `tests/test_feed_contract.py`/`tests/test_reconnect.py` (noted in
the M2 validation report, unrelated to M3, left as-is).

`mypy src` — `Success: no issues found in 28 source files`.

### Live-DB validation

Ran an end-to-end smoke test against the real local Postgres
(`postgresql://orvixa:orvixa@localhost:5432/orvixa`, M2's instance) +
`SimFeed`, with a scripted two-cycle fake market client (new listing,
delisting, volume spike):

- Cycle 1: discovered BTC/ETH/SOL as core, LINK/AVAX as top-volume alts
  (Tier 1), DOGE as meme (Tier 1), a brand-new `NEWCOINUSDT` listing as Tier 2
  initially (then Tier 1 here since `tier1_size` covered it). Feed subscribed
  to exactly the Tier 0/1 watchlist; pre-existing M2-seeded symbols (BNB, XRP,
  PEPE, SHIB, WIF — not in the fake universe) were correctly **unsubscribed**
  on the very first cycle and persisted as `frozen`.
- Cycle 2: LINK delisted -> `status="frozen"`, `TierChange(..., "delisted")`,
  unsubscribed from the feed. AVAX's 5x volume spike kept it in Tier 1.
- Breadth snapshot computed from the sim feed's live ticker stream
  (`advancers=7, decliners=4, ad_ratio=1.75`).
- `symbols` table verified post-run: `rank`/`metrics` (jsonb)/`tags`/`tier`/
  `class`/`status` all correctly persisted for every symbol.

## 4. Definition-of-done validation

| # | Requirement | Status |
|---|---|---|
| 1 | Automatic symbol discovery via `/exchangeInfo` + `/ticker/24hr`, detects new listings/delistings, updates status | ✅ `BinanceMarketClient`, `_sync_listings`/`_mark_delistings` |
| 2 | Tier system (0=BTC/ETH/SOL, 1=top volume + curated memes, 2=rest), recalculated automatically | ✅ `_assign_tiers`/`_base_tier`, every `refresh_universe()` |
| 3 | Volume ranking engine (quoteVolume, liquidity/activity, refreshed periodically) | ✅ `symbols/ranking.py` (`compute_score`/`rank_by_score`), refreshed each cycle |
| 4 | Dynamic watchlist (Tier 0+1), sortable by volume/volatility/daily change | ✅ `get_watchlist`, `symbols/watchlist.py` |
| 5 | Market breadth engine (advancers/decliners/AD ratio/% above trend/new highs/lows) from market-wide ticker data | ✅ `BreadthEngine` via `on_market_snapshot` |
| 6 | Promotion/demotion on volume/volatility/activity spikes, demote when activity fades | ✅ `_apply_spike_logic`/`_is_spiking`, grace-cycle demotion |
| 7 | Feed integration: subscribe/unsubscribe without restart | ✅ `_sync_feed` via existing `MarketFeed.subscribe/unsubscribe`, no feed changes |
| 8 | Persist tier/class/status/tags/rankings in Postgres | ✅ migration `0002`, `SymbolRepository.upsert` + `update_ranking` |
| 9 | Unit/ranking/promotion/breadth tests, fully testable without live Binance | ✅ 69 passed, 1 skipped (pre-existing, unrelated); fake client/feed/pool throughout |
| 10 | Deliverables: symbol_manager.py, ranking/breadth/watchlist engines, scheduler integration, tests | ✅ all present (file tree above); `start()`/`stop()`/`_loop()` scheduler, `orvixa-symbols` runner |

**Constraints honored**: feed architecture (`feeds/`) untouched; only
additive schema change (`0002_symbol_ranking.py`); Milestone 4 not started.

M3 is complete and ready for review.
