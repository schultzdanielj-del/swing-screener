# Nightly Refresh

**Goal:** Nightly refresh of the OHLCV + intermediate cache + market cache + fundamentals, producing a fresh live-scan watchlist. Zero Railway dependency for data; Railway is seed-vault backup only.

**Entry point:** `local_runner/nightly.py` — triggered manually or via Windows Task Scheduler at 7:00am ET, daily. Schedule moved from 4:30pm because EODHD bulk data isn't reliably ready by then; running the next morning is fine since scans are fast.

Refer to the authoritative step list in `DEPENDENCY_MAP.md` under `nightly.py`. This file documents the intent, known issues, and non-obvious design decisions.

---

## Pipeline steps

| Step | Component | Purpose |
|------|-----------|---------|
| 1 | `cache_builder.check_freshness()` | yfinance gate — abort if market day's data isn't available yet |
| 2 | `cache_builder.append_daily_cache()` | Append new daily bars to `universe_ohlcv_daily.pkl`, sync universe (IPOs + delistings) against the EODHD exchange symbol list |
| 3 | `cache_builder.append_weekly()` | Append to `universe_ohlcv_weekly.pkl` |
| 4 | `cache_builder.append_monthly()` | Append to `universe_ohlcv_monthly.pkl` |
| 5 | `intermediate_cache_builder.build_full()` | Rebuild `.im` files — 196 numeric intermediates per ticker (SMA, ATR, RSI, etc). Pure-numpy compute, no ExpressionEngine, ~1.7 min for all tickers at 14 workers. |
| 6 | `matrix_builder.get_universe_matrix()` | Rebuild D1 universe matrix for the grinder (graceful skip if expression cache missing). |
| 7 | Earnings refresh | Currently **BROKEN** — Railway endpoints don't exist. Needs a local Yahoo Finance scraper. |
| 8 | `market_cache_builder.append_new_bars()` | Append market context instruments (US ETFs + EODHD indices/crypto/breadth + yfinance futures + FRED macro). |
| 9 | `fetch_fundamentals` | Refresh per-ticker sector, shares outstanding, float. Expensive on Mondays only. |
| 10 | `seed_vault.backup()` | Push SQLite tables + grind result JSONs to Railway for backup. Intentional Railway dependency. |

**The nightly pipeline does NOT update the expression cache `.npz` files.** The grinder's expression cache is a separate artifact, rebuilt manually via `expr_cache_builder.py --build` when needed for a consensus pipeline run. Step 5 uses the new intermediate-cache architecture (`.im` files) which is independent of the grinder's `.npz` cache — they have different purposes and different column sets. See `DATA_CONTRACT.md` for the format details and `DEPENDENCY_MAP.md` Chain 5 for the dependency graph.

**The nightly pipeline does NOT scan or generate signals.** Nightly is infrastructure-only: cache builds, matrix rebuild, market context, fundamentals, seed vault. Live scan / watchlist generation runs separately via `scan_engine.py`'s own CLI (`--setup` / `--all`).

**Killed:** Old step 2 (300-bar daily OHLCV cache rebuild from Railway). Nothing reads this cache — everything uses the daily pickle. Removed 2026-03-25.

### Legacy Railway examples API (`/api/examples/`) — ✅ DONE (2026-04-01)

All 8 scripts that loaded examples from Railway have been switched to local SQLite (`data/scanperfect.db`). The seed vault still syncs setups + examples to Railway nightly as a backup.

| Script | Status |
|--------|--------|
| `local_runner/matrix_builder.py` | ✅ Local SQLite + local OHLCV pickle |
| `scripts/exit_grinder.py` | ✅ Local SQLite |
| `scripts/cycle_health.py` | ✅ Local SQLite (keeps Railway for cycles/signals) |
| `scripts/entry_candle_weight_diagnostic.py` | ✅ Local SQLite |
| `scripts/entry_candle_sanity_check.py` | ✅ Local SQLite |
| `scripts/debug_example_conditions.py` | ✅ Local SQLite |
| `scripts/signal_filter.py` | ✅ Already local (pre-existing) |
| `scripts/signal_exit_grinder.py` | ✅ Already local (pre-existing) |

**Total current runtime: ~2-2.5 hours.** Target: under 30 minutes. With forward-prop engine (step 5: ~19 min vs ~80-90 min), projected total drops to **~50-60 min**. Remaining bottleneck: market context cache (step 8: ~12 min) and fundamentals (step 9: ~10 min Mon).

### Railway dependencies — status:

1. **`cache_builder.py` → `get_tradable_tickers()`** (line 35): ~~Calls Railway.~~ **DONE (2026-03-27).** Replaced with `get_tradable_tickers_local()` reading from local SQLite.
2. **`cache_builder.py` → `fetch_one_ticker_daily()`** (line 180): ~~Fetches from Railway.~~ **DONE (2026-03-27).** Replaced with `_yf_download_daily()` using yfinance.
3. **`cache_builder.py` → `_fetch_ticker_after_date()`** (line 303): ~~Fetches from Railway.~~ **DONE (2026-03-27).** Replaced with `_yf_append_after_date()` using yfinance.
4. **`build_tradable.py`** (line 56): Only runs on Railway's SQLite DB. Needs a local equivalent. **DEPRIORITIZED.**
5. **`nightly.py` step 1**: ~~Calls Railway to trigger yfinance fetch.~~ **DONE (2026-03-27).** Replaced with local `check_yfinance_freshness()`.
6. **`nightly.py` step 7 (earnings)**: Calls non-existent Railway endpoints. **BROKEN** — replace with local Yahoo Finance scraper.
7. **`nightly.py` step 9 (fundamentals)**: Mirrors fundamentals cache to Railway (redundant). **TODO** — remove dead mirror call.

**Railway removal DONE for OHLCV (2026-03-28).** `cache_builder.py` fully rewired to yfinance. HTF caches (weekly + monthly, 10yr lookback) merged into `cache_builder.py` (`--htf` to build, `--htf-status` to check). `nightly.py` step 1 uses local freshness check. `expr_cache_builder.py` uses HTF pickles instead of resampling from daily. **Market cache EODHD migration DONE (2026-04-01).** Railway is now only used for earnings (broken, needs local scraper) and seed vault backup (intentional).

### What's been done:

- **Railway OHLCV removal** (2026-03-28): `cache_builder.py` fully rewired — all Railway HTTP calls replaced with yfinance. HTF caches (weekly + monthly, 10yr) merged into `cache_builder.py`. `expr_cache_builder.py` uses HTF pickles instead of resampling. `nightly.py` expanded to 10 steps. Railway removed from OHLCV path entirely.
- **Expr cache Task H Phase 2** (2026-03-27): SLOW_OPS numpy, numpy bools, ext struct vectorization, HTF intermediates dispatch, fast compression (compresslevel=1), worker-side saves. 1.8 tickers/s for full rebuild.
- **Seed vault gate fix** (2026-03-26): Step 10 (was 8) now runs every night even when step 1 gates early.
- **Old step 2 killed** (2026-03-25): 300-bar daily OHLCV cache rebuild. Nothing used it.
- **Expr cache vectorization** (v2-consensus): `trendline_deviation`, `channel_position`, `CCI` vectorized.
- **Expr cache I/O fix** (v2-consensus): Eliminated serial load/concat/save bottleneck.

### What does NOT need changing:

- Steps 5, 6, 8 (expr cache, matrix, market cache) — work locally already. Market cache fully migrated to EODHD/pickle/FRED (2026-04-01).
- Step 10 (seed vault) — Railway dependency is intentional
- All downstream consumers (scanperfect.py, grinders, EV grinder, profit grinder, consensus pipeline) — read same pickle/cache format

---

## THE EXPR CACHE BOTTLENECK

### The problem

The expression cache append (`expr_cache_builder.py --append`) takes ~91 minutes on the i5-12600k with 15 workers. This is the single biggest blocker for the 30-minute target.

**Why it's slow:** To append 1 new bar per ticker, the code must recompute ALL 15,805 expressions across the FULL history (~1,200 bars) for each ticker. Indicators need lookback history — you can't compute a 21-period moving average from just 1 bar. So "appending 1 bar" is computationally identical to a full rebuild.

4,114 tickers × 15,805 expressions × full history recompute = ~91 minutes.

### Expression breakdown (15,805 total):

| Category | Count | Compute pattern |
|----------|-------|----------------|
| HTF weekly | 5,233 | Resample to weekly (~260 bars), compute_series per expression |
| HTF monthly | 5,233 | Resample to monthly (~60 bars), compute_series per expression |
| Daily | 4,017 | compute_series per expression on full daily bars |
| Extension structure | 1,198 | Second pass — depends on already-computed ext_avgc50/200 columns |
| LSP | 80 | Single batch call (compute_all_lsp_series) |
| Algo lines | 44 | Single batch call (compute_all_algo_series) |

Each ticker makes ~15,681 individual `compute_series()` Python function calls, each doing pandas Series rolling operations.

### What was tried (2026-03-25 session):

**Fix 1: Eliminate serial I/O bottleneck (commit `1f54e5e`)** ✅ PUSHED TO v2-consensus
- Old append path: worker computes full series → returns only new rows → main thread serially loads old .npz, concatenates, saves. The serial load/concat/save was 3.2s × 4,100 tickers = 219 min of main-thread I/O.
- Fix: Both append and new-ticker work use `_compute_ticker_full` with the same chunked submission pattern as `build_full()`. Worker does full recompute, returns complete result, main thread saves directly. No load, no concat.
- Result: Reduced from 2+ hours to ~109 minutes (318s for first 200 tickers at 15 workers). The I/O bottleneck was real but compute is still ~91 min.

**Fix 2: Vectorize Python for-loop bottlenecks (commit `427d3c1`)** ✅ PUSHED TO v2-consensus
- `trendline_deviation`: Python for-loop over every bar → numpy convolution. 215x faster per call (benchmarked in sandbox).
- `channel_position`: Same vectorization. Similar speedup.
- `CCI` (both `compute_on_series` and `profiling_engine`): `.apply(lambda)` → `sliding_window_view`. 19x faster per call (benchmarked in sandbox).
- These 3 ops affect 404 expressions per ticker (60 trendline/channel + 284 CCI).
- Result on Dan's machine: 279s for first 200 tickers (vs 318s before). ~12% faster. **Not enough.**
- Projected total: ~91 minutes at 15 workers. Still way over 30-minute target.
- All outputs verified numerically identical to originals (max diff < 1e-10).
- Produces numpy RuntimeWarnings (divide by zero, invalid value in subtract) in terminal — these are cosmetic, not errors. 0 tickers failed. The warnings come from NaN/zero edge cases in OHLCV data that were always present but previously swallowed silently.

**What was tried on v2 (not v2-consensus) and failed:**
- Commit `9eadbcd` (2026-03-25): "chunked submission for expr cache append to prevent memory blowup" — buggy, never ran successfully.
- Commit `42d997d` (2026-03-26): "full recompute + parallel save" — same idea as fix 1 above but buggy, never ran successfully. Dan asked for revert but it wasn't reverted.

### Why 91 minutes is still too slow

The remaining time is spread across ~15,277 non-vectorized `compute_series()` calls per ticker. Each call is fast individually (pandas rolling ops on 60-1,200 bars), but the sheer volume adds up. There is no single bottleneck left to fix — it's death by 15,000 cuts.

### What's been done:

- **HTF skip on non-rebalance days (2026-03-26).** Weekly HTF (5,233 expressions) only recomputed on Mondays. Monthly HTF (5,233) only on first trading day of the month. Other days copy HTF columns from previous cache. Skips ~66% of expressions on Tue-Fri. Expected: ~91 min → ~30-35 min on most days. Mondays stay full.

### Directions NOT yet explored (future optimization):

1. **Vectorize `since_true` and `true_in_row`.** These 1,397 boolean expressions use `.apply(lambda)` — Python callbacks on every rolling window. Replacing with numpy loops gives 4-10x speedup per call. Benchmarked at ~6 min savings. Low risk, low effort, same pattern as the CCI/trendline vectorization that already shipped.

2. **Numpy-ify `profiling_engine.py` base indicators.** Rewrite the ~15 core indicator functions (sma, ema, atr, rsi, etc.) to accept/return numpy arrays instead of pandas Series. Smaller surface area than rewriting all 90 ops in `compute_series()` — everything upstream benefits automatically. Estimated 2-3× speedup across the board. Medium risk, medium effort.

3. **Batch batchable expression families.** 1,323 daily expressions fall into families sharing the same formula with different parameters (240 ma_slope, 174 distance_to_maxh, 114 distance_to_minl, etc.). Could compute each family as one matrix operation instead of hundreds of individual `compute_series()` calls. Estimated 5-8 min savings. Medium risk, high effort.

4. **Incremental append for simple ops.** Instead of full-series recompute, derive the new bar's value from the previous bar's value + the new data point. Works for SMA, EMA, rolling max/min. Tricky for RSI, ADX. Eliminates full recompute entirely for supported ops. High risk (numerical drift), high effort.

5. **C extension / Cython / Numba.** Compile the hot loop. 10-50x potential. Major rework.

6. **Reduce expression count.** If HTF expressions don't contribute to EV scoring, drop them entirely. Design decision, not code optimization.

7. **Accept the time and start earlier.** Run nightly at 3:00pm ET. Market cache already handles partial-day data.

---

## PROPOSED NIGHTLY OVERHAUL (Railway removal)

This is separate from the expr cache speed problem. Even if the cache takes 91 minutes, removing Railway from steps 1-3 and 6 eliminates failure modes and saves ~25 minutes.

### New step order (6 steps):

| Step | What | Notes |
|------|------|-------|
| 1 | Local yfinance freshness check + direct fetch + tradable universe rebuild | Replaces old steps 1+2+3 |
| 2 | Expression cache append | Keep as-is (the bottleneck) |
| 3 | Universe matrix rebuild | Keep as-is |
| 4 | Market context cache append | Keep as-is |
| 5 | Fundamentals cache refresh | Remove Railway mirror at end |
| 6 | Seed vault backup to Railway | Keep as-is — only Railway touch |

### Step 1 details (not yet implemented):

**Freshness check:** Load daily pickle → find latest date across all tickers → compare to yfinance SPY latest bar → if same, "up to date", stop.

**yfinance fetch:** Batch tickers into groups of ~500 using `yf.download(ticker_list, start=last_date+1day, auto_adjust=True)`. Sleep 5-10s between batches. Append new bars directly to daily pickle. Recompute `dvol_20d` on combined data.

**Tradable universe rebuild:** New function in `cache_builder.py`. Reads the daily pickle, applies same 3 filters as `build_tradable.py`:
- Last close ≥ $1.00
- 20-day avg APTR ≥ 1.5%
- 20-day avg dollar volume ≥ $5,000,000
Saves as `local_runner/cache/tradable_tickers.json`. `get_tradable_tickers()` reads from this file.

**Risk areas:**
- yfinance rate limiting at 4,167 tickers (market_cache_builder handles 266 fine — 4,167 is 15x more)
- yfinance DataFrame format quirks (MultiIndex columns in newer versions, timezone-aware dates, auto_adjust behavior)
- Ticker list divergence if local tradable universe filtering differs from Railway's version (boundary tickers)
- **Output pickle format must be identical** — column names, dtypes, date types, dvol_20d computation. The UI, all grinders, expr cache builder, matrix builder, consensus pipeline, EV grinder, and profit grinder all read from these pickles.

### Step 5 (earnings) — replace with local scraper:

The Railway endpoints don't exist. The vetting UI reads earnings dates from local SQLite (`earnings_dates` table) and draws them as chart markers. Data has been going stale every night since these endpoints were never built.

**Fix:** Replace with local Yahoo Finance scraper using the same `quoteSummary` API as `fetch_fundamentals.py`. ~4,167 requests at 0.15s = ~10 minutes. Write directly to local SQLite `earnings_dates` table.

---

## CRITICAL FORMAT CONSTRAINTS

The daily pickle (`universe_ohlcv_daily.pkl`) must maintain identical format:

```python
# Dict of {ticker_str: pd.DataFrame}
# Each DataFrame columns: date, open, high, low, close, volume, dvol_20d
# date: datetime64[ns], timezone-naive
# open/high/low/close/volume: float64
# dvol_20d: float64, = (close * volume).rolling(20).mean()
# Sorted by date ascending, reset_index(drop=True)
```

**Consumers:** scanperfect.py (line 161), pyramid_grinder.py, expr_cache_builder.py, matrix_builder.py, signal_filter.py, ev_grinder.py, profit_grinder.py, backtest_runner.py, signal_distribution.py.

**Who reads the tradable ticker list:**
- `cache_builder.py` `get_tradable_tickers()` — to know which tickers to fetch/cache
- `fetch_fundamentals.py` `load_universe_tickers()` — reads from pickle keys directly, does NOT call `get_tradable_tickers()`

---

## CONSENSUS PIPELINE INTERACTION

The consensus pipeline orchestrator (`scripts/run_consensus_pipeline.py`) checks for nightly completion before starting:

```python
# Looks for "Nightly refresh complete" with today's date in
# local_runner/cache/nightly_log.txt
```

The bat file (`nightly_refresh.bat`) writes this line after `nightly.py` finishes. The nightly needs to complete before the consensus pipeline's 8-10 hour overnight run starts.

**Also noted:** `pyramid_grinder.py` line 109 still loads examples from Railway API (`requests.get(f"{API_BASE}/api/examples/{setup_type}")`). This is NOT a nightly issue — it's a grinder dependency. The refinement grinder already loads from local SQLite. Separate fix needed.

**Cannot run consensus pipeline while nightly is running.** The consensus pipeline checks for "Nightly refresh complete" in the log, AND the grinders read from the expr cache .npz files while the nightly append is actively writing them. Running both simultaneously = corrupted reads.

---

## FILES INVOLVED

| File | Role | Railway dependency? |
|------|------|-------------------|
| `local_runner/nightly.py` | Orchestrator (10 steps) | Steps 7, 10 call Railway |
| `local_runner/cache_builder.py` | Daily + Weekly + Monthly OHLCV cache management | **NO** — fully rewired to yfinance (2026-03-28). HTF caches merged in. |
| `local_runner/expr_cache_builder.py` | Expression cache | **NO** — uses HTF pickles, no Railway. THE BOTTLENECK. |
| `local_runner/matrix_builder.py` | Universe matrix | No Railway dependency |
| `local_runner/market_cache_builder.py` | Market context cache | No Railway dependency. Migrated to EODHD/pickle/yfinance/FRED (2026-04-01) |
| `scripts/build_tradable.py` | Tradable universe filter | Runs on Railway SQLite only (needs local equivalent) |
| `scripts/fetch_fundamentals.py` | Fundamentals cache | No Railway dependency. Has dead Railway mirror at end. |
| `scripts/seed_vault.py` | Backup to Railway | Intentional Railway dependency (file mirror only). |
| `nightly_refresh.bat` | Windows Task Scheduler trigger | No changes needed |
