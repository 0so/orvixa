# M3 Validation Report (Final) — Symbol Manager Audit

Status: **audit complete, 1 latent bug fixed**. No architecture or schema
changes made. Milestone 4 not started.

This is an independent re-audit of the M3 deliverable (`SymbolManager` and
its supporting `symbols/` modules), beyond the original
`M3_DELIVERY_REPORT.md`. It re-runs the full quality-gate suite, re-validates
end-to-end behavior against the live local Postgres, and specifically probes
for architectural/scaling issues, race conditions, database-consistency
issues, subscription leaks, and performance bottlenecks.

## 1. Test / lint / type-check results

```
PYTHONPATH=src python -m pytest -q   -> 69 passed, 1 skipped
PYTHONPATH=src ruff check src tests  -> 2 pre-existing M1 ASYNC110 findings
                                         (test_feed_contract.py, test_reconnect.py),
                                         unrelated to M3, unchanged since M2
PYTHONPATH=src mypy src              -> Success: no issues found in 28 source files
```

## 2. Functional validation (live Postgres + SimFeed)

Re-ran the full discovery -> tiering -> watchlist -> breadth -> feed-sync ->
persistence flow against the live local Postgres
(`postgresql://orvixa:orvixa@localhost:5432/orvixa`) with a scripted fake
market client:

- **Discovery**: new pairs correctly added to `_states` (`"symbol
  discovered"`), tiered, and persisted.
- **Tier assignment**: BTC/ETH/SOL -> Tier 0 (`core`); DOGE -> Tier 1
  (`meme`); LINK/AVAX -> Tier 1 (`alt`, top-N by volume).
- **Watchlist**: `get_watchlist(sort_by="volume")` returned exactly Tier 0+1,
  sorted by `quote_volume` descending — `[BTC, ETH, SOL, LINK, AVAX, DOGE]`.
- **Breadth**: `BreadthEngine` correctly aggregated the sim feed's live
  snapshot stream (`total=11, advancers=4, decliners=7, ad_ratio=0.57`).
- **Feed sync**: feed ended up subscribed to exactly the Tier 0+1 pairs.
- **Persistence**: `symbols` rows show correct `class`/`tier`/`status`/
  `tags`/`rank`/`metrics` (jsonb)/`last_synced`.

This matches the original M3 delivery validation; no regressions.

## 3. Findings

### 3.1 FIXED — Race condition: concurrent `refresh_universe()` mutates `_states` mid-iteration

**Severity: medium (latent, not currently reachable from any call site, but a
real bug if triggered).**

`refresh_universe()` iterates `self._states.values()` while calling `await
self._persist(state)` for each entry. `_sync_listings()` (called earlier in
the same method, on a *different* invocation) inserts new keys into
`_states` when new pairs are discovered. If two `refresh_universe()` calls
ever overlapped — e.g. a future "force refresh" API trigger racing with the
periodic `_loop()` — and the second call discovered a new listing while the
first was still in its persistence loop, Python raises:

```
RuntimeError: dictionary changed size during iteration
```

Reproduced this exactly with a scripted client that returns a larger universe
on its second call and `asyncio.gather`-ing two `refresh_universe()` calls
(see audit transcript: fails without the fix, both calls complete cleanly
with it).

**Currently not reachable**: `_loop()` is strictly sequential
(`sleep` → `await refresh_universe()`), so no existing code path triggers
this today. It's a latent landmine for any future caller (API-triggered
manual refresh, multiple manager instances sharing state, etc.).

**Fix applied** (`src/orvixa/symbols/manager.py`): added
`self._refresh_lock = asyncio.Lock()` in `__init__`, and wrapped the body of
`refresh_universe()` in `async with self._refresh_lock:`. This serializes
overlapping calls without changing the method's signature, return value, or
any other architecture/contract. Verified: 69/69 tests still pass, and the
`asyncio.gather` reproduction now completes without error.

### 3.2 Reported, not fixed — duplicate `base` collision on discovery (pre-existing M1 normalization assumption)

**Severity: low (no realistic trigger on current Binance listings).**

`_sync_listings` keys `_states` (and ultimately `symbols.base`, which is
`UNIQUE`) by `normalize_symbol(pair)`. If two *different* active exchange
pairs ever normalized to the same base — e.g. a hypothetical world where both
`"PEPEUSDT"` and `"1000PEPEUSDT"` are simultaneously `TRADING` (both
normalize to `"PEPE"` because `normalize_symbol` strips the `1000` prefix) —
the second one processed in `active.items()` silently overwrites the first's
`_SymbolState` in the dict. The first pair's data is dropped with no error,
no log, and is never persisted that cycle.

Reproduced with a synthetic two-pair fixture: only one of the two pairs ends
up in `_states`/`symbols`.

**Why not fixed**: this is an inherited consequence of M1's
`normalize_symbol` (1000x-prefix stripping) and M2's `symbols.base UNIQUE`
schema constraint — both explicitly out of scope ("do not modify the existing
feed architecture" / "do not modify the database schema unless strictly
necessary"). A correct fix would require either keying the registry by `pair`
instead of `base`, or relaxing the `base` uniqueness constraint, both of
which are architecture/schema changes. In practice Binance does not currently
list both a `1000X` and bare `X` USDT pair simultaneously, so this is a
documented edge case rather than an active bug.

### 3.3 Reported, not fixed — per-symbol persistence is two sequential round trips

**Severity: low/medium (scaling consideration for the full Binance universe).**

`_persist()` issues `SymbolRepository.upsert` (a `fetchrow`) and
`update_ranking` (an `execute`) sequentially per symbol, and
`refresh_universe()` awaits this in a plain `for` loop over every tracked
symbol — including `frozen` ones, which are never pruned from `_states`.
Measured locally: ~3 ms/symbol (2 queries) against a loopback Postgres for
204 symbols (~0.6 s total). For the full Binance USDT spot universe
(roughly 400-600 pairs) against a non-local DB (10-30 ms RTT), this
extrapolates to **roughly 15-35 seconds per refresh cycle** spent purely on
sequential persistence — still well under the default 300 s
`symbol_refresh_interval_seconds`, but a meaningful and growing fraction of
the budget as the tracked-symbol set grows (it is monotonically non-shrinking
since delisted symbols are kept as `frozen` rows forever).

**Why not fixed**: batching this (e.g. `executemany`/`UPDATE ... FROM (VALUES
...)`) would change the `SymbolRepository` persistence pattern, which is an
architectural change beyond "fix bugs if found". Flagging for M4+ planning:
if `tier1_size`/universe size grows significantly, consider a single batched
upsert + batched ranking update per refresh cycle.

### 3.4 Reported, not fixed — `last_synced` updates even for frozen/delisted symbols

**Severity: cosmetic.**

`refresh_universe()` calls `_persist()` (and therefore `update_ranking`,
which sets `last_synced = now()`) for *every* tracked symbol each cycle,
including ones marked `status="frozen"` this cycle or earlier, with their
stale (frequently zeroed) `metrics`. `last_synced` therefore reflects "last
time the manager's refresh loop ran", not "last time this symbol had live
market data". Not a correctness bug — `tier`/`class`/`status`/`tags` for
frozen symbols are correctly preserved (only `metrics`/`rank`/`last_synced`
are touched) — but could be confusing for an operator inspecting the table.
No fix applied; purely informational.

### 3.5 No issues found

- **Symbol discovery**: new-listing/delisting/relisting transitions verified
  correct, including status flips and `TierChange` records.
- **Tier assignment**: global rank vs. alt-only top-N split verified correct;
  core/meme bases never consume top-N slots; frozen symbols correctly skipped
  by `_assign_tiers`.
- **Watchlist generation**: Tier 0/1 filter and all three sort modes
  (`volume`/`volatility`/`change`, ascending/descending) verified; unknown
  `sort_by` raises `ValueError` as documented.
- **Breadth calculations**: advancers/decliners/unchanged, AD ratio (incl.
  zero-decliner edge case), pct-above-trend, new-highs/lows, and the
  empty-history first-snapshot case all verified.
- **Promotion/demotion**: volume-multiplier, trade-count-multiplier, and
  volatility-threshold spike triggers all verified; grace-cycle counter
  correctly resets on re-spike and correctly demotes after
  `demotion_grace_cycles` calm cycles; `"spike"` tag correctly persisted and
  reconstructed via `_load_existing()` after a simulated restart.
- **Persistence**: jsonb `metrics` round-trips correctly (`json.dumps` +
  `::jsonb` cast, fixed in the prior session); `tags` (`text[]`) round-trips
  natively; `upsert` correctly refreshes `class`/`tier`/`status`/`tags`
  without disturbing `first_seen`.
- **Feed synchronization**: `_subscribed` is seeded from
  `settings.all_symbols` (matching what `build_feed()` initially subscribes),
  so the first `_sync_feed()` correctly unsubscribes any initial feed symbols
  outside the Tier 0/1 watchlist — verified live, no leaked subscriptions
  across multiple cycles (new listings subscribed, delisted/demoted symbols
  unsubscribed, re-verified with the lock fix in place).
- **Subscription leaks**: none found — `BinanceFeed.subscribe`/`unsubscribe`
  are idempotent set-diffs, so even a partial failure mid-`_sync_feed` is
  self-healing on the next cycle (though `self._subscribed` would remain
  stale until the next successful cycle — acceptable given idempotency).

## 4. Summary of changes made during this audit

- `src/orvixa/symbols/manager.py`: added `self._refresh_lock = asyncio.Lock()`
  and wrapped `refresh_universe()`'s body in `async with self._refresh_lock:`
  to fix the reproducible dict-mutation race (3.1). No signature, return
  type, or external-behavior changes.

All other findings (3.2-3.4) are documented for awareness/future planning and
were intentionally **not** fixed, as doing so would require architecture or
schema changes excluded by the audit constraints.

## 5. Final gate status

| Gate | Result |
|---|---|
| Unit/integration tests | 69 passed, 1 skipped (pre-existing, unrelated) |
| ruff | clean (2 pre-existing M1 findings, unrelated) |
| mypy | clean (28 source files) |
| Live-DB end-to-end validation | pass |
| Race-condition fix verified | pass (reproduced before fix, clean after) |

M3 remains complete and is now hardened against the one reproducible latent
bug found. Milestone 4 has not been started.
