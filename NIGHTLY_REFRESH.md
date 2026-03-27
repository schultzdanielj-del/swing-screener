# Nightly Refresh — Overhaul Spec

**Created:** 2026-03-25
**Updated:** 2026-03-26
**Branch:** `v2`
**Goal:** Nightly refresh finishes in under 30 minutes with zero Railway dependency for data. Railway stays for seed vault backup only.

---

## CURRENT STATE (8 steps, was 9)

### The nightly pipeline (`local_runner/nightly.py`):

| Step | What it does | Time | Status |
|------|-------------|------|--------|
| 1 | Calls Railway `POST /api/universe/append-daily` | 5-15 min | **NEEDS FIX.** Railway dependency — if Railway is down, nothing runs. Replace with local yfinance fetch. |
| 2 | Appends new bars to 5yr OHLCV pickle from Railway | ~10 min | **NEEDS FIX.** Railway dependency. Calls `_fetch_ticker_after_date()` per ticker via Railway's `/api/query/bulk`. Replace with local yfinance fetch (merge with step 1). |
| 3 | Appends expression cache | **~91 min** | **THE BOTTLENECK.** See section below. Two vectorization fixes pushed (v2-consensus), brought it from 2+ hrs to ~91 min. Still way over target. |
| 4 | Rebuilds universe matrix | ~30s | **OK.** No changes needed. |
| 5 | Refreshes earnings dates via Railway | Fails silently | **BROKEN.** `POST /api/universe/refresh-earnings` and `GET /api/universe/earnings-status` don't exist in `server.py`. Has been failing silently every night. Vetting UI reads from local SQLite `earnings_dates` table — data is stale. Replace with local Yahoo Finance scraper. |
| 6 | Appends market context cache (266 instruments) | ~2-3 min | **OK.** Uses yfinance directly, no Railway dependency. |
| 7 | Refreshes fundamentals cache | ~10 min (Mon) / <1 min | **OK** but has a dead Railway mirror call at the end. Remove it. |
| 8 | Seed vault backup to Railway | ~1-2 min | **FIXED 2026-03-26.** Now runs every night regardless of step 1 gate. Previously skipped when step 1 said "already up to date", causing stale backups. Removed legacy `sync_examples_to_railway()` — seed vault JSON files on file mirror are the only backup. Railway's actual SQLite DB is legacy (several scripts still read from it — see below). |

**Killed:** Old step 2 (300-bar daily OHLCV cache rebuild from Railway). Nothing reads this cache — everything uses the 5yr pickle. Removed 2026-03-25.

### Legacy Railway examples API (`/api/examples/`) — cleanup needed

Railway's SQLite DB has an examples table that several active scripts still read from instead of local SQLite. The seed vault no longer syncs to it (removed 2026-03-26), so it will go stale. These scripts need switching to local SQLite (`data/scanperfect.db`):

| Script | Line | What it does |
|--------|------|-------------|
| `local_runner/matrix_builder.py` | 412 | Checks Railway examples for cache freshness |
| `scripts/exit_grinder.py` | 118 | Loads examples from Railway |
| `scripts/cycle_health.py` | 73 | Loads examples from Railway |
| `scripts/entry_candle_weight_diagnostic.py` | 53 | Diagnostic — loads examples from Railway |
| `scripts/entry_candle_sanity_check.py` | 38 | Diagnostic — loads examples from Railway |
| `scripts/debug_example_conditions.py` | 26 | Debug — hardcoded Railway URL |
| `scripts/signal_filter.py` | 180 | Loads examples from Railway (v2-consensus) |
| `scripts/signal_exit_grinder.py` | 150 | Loads examples from Railway (v2-consensus) |

**Not urgent** — these scripts still work because Railway's DB happens to have data. But as new examples are added locally (e.g. BRKO's 51), Railway won't have them. Fix by replacing `requests.get(f"{API_BASE}/api/examples/{setup_type}")` with a local SQLite query in each script.

**Total current runtime: ~2-2.5 hours.** Target: under 30 minutes.

### Railway dependencies that need removing:

1. **`cache_builder.py` → `get_tradable_tickers()`** (line 35): Calls Railway's `/api/query/bulk` to `SELECT ticker FROM tradable_universe`. No local equivalent exists. **DEPRIORITIZED.**
2. **`cache_builder.py` → `fetch_one_ticker_5yr()`** (line 180): Fetches ALL OHLCV per ticker from Railway. **DEPRIORITIZED.**
3. **`cache_builder.py` → `_fetch_ticker_after_date()`** (line 303): Fetches new bars per ticker from Railway. **DEPRIORITIZED.**
4. **`build_tradable.py`** (line 56): Only runs on Railway's SQLite DB. Filters: close ≥ $1, APTR ≥ 1.5%, avg dollar volume ≥ $5M over 20 days. Needs a local equivalent. **DEPRIORITIZED.**
5. **`nightly.py` step 1**: Calls Railway to trigger yfinance fetch. **DEPRIORITIZED.**
6. **`nightly.py` step 5 (was 6)**: Calls non-existent Railway endpoints. **BROKEN** — replace with local Yahoo Finance scraper.
7. **`nightly.py` step 7 (was 8)**: Mirrors fundamentals cache to Railway (redundant — seed vault handles backup). **TODO** — remove dead mirror call.

**Railway removal deprioritized (2026-03-25).** Steps 1+2 account for ~15-25 min combined, but the expr cache bottleneck (step 3, ~91 min) is 75% of total runtime. Removing Railway only matters once the expr cache is under control. A previous attempt to remove Railway dependencies resulted in cascading errors across cache_builder.py and had to be fully reverted (commit `d16d3a3` on v2). The risk areas are real: yfinance rate limiting at 4,167 tickers, DataFrame format quirks (MultiIndex columns, timezone-aware dates), and the pickle format must be byte-compatible since ~12 scripts read it. Do not reattempt without isolated per-function testing.

### What's been done:

- **Seed vault gate fix** (2026-03-26): Step 8 now runs every night even when step 1 gates early ("no updates needed"). Removed `sync_examples_to_railway()` — Railway DB is legacy, seed vault JSON on file mirror is the only backup copy. Documented legacy scripts that still hit Railway's examples API.
- **Old step 2 killed** (2026-03-25): 300-bar daily OHLCV cache rebuild. Nothing used it. Pipeline now 8 steps.
- **Expr cache vectorization** (v2-consensus): `trendline_deviation`, `channel_position`, `CCI` vectorized. Brought append from 2+ hrs to ~91 min.
- **Expr cache I/O fix** (v2-consensus): Eliminated serial load/concat/save bottleneck. Workers do full recompute + direct save.

### What does NOT need changing:

- Steps 3, 4, 6 (expr cache, matrix, market cache) — work locally already
- Step 8 (seed vault) — Railway dependency is intentional
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

**Freshness check:** Load 5yr pickle → find latest date across all tickers → compare to yfinance SPY latest bar → if same, "up to date", stop.

**yfinance fetch:** Batch tickers into groups of ~500 using `yf.download(ticker_list, start=last_date+1day, auto_adjust=True)`. Sleep 5-10s between batches. Append new bars directly to 5yr pickle. Recompute `dvol_20d` on combined data.

**Tradable universe rebuild:** New function in `cache_builder.py`. Reads the 5yr pickle, applies same 3 filters as `build_tradable.py`:
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

The 5yr pickle (`universe_ohlcv_5yr.pkl`) must maintain identical format:

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
| `local_runner/nightly.py` | Orchestrator (8 steps, was 9) | Steps 1, 5 call Railway |
| `local_runner/cache_builder.py` | OHLCV cache management | `get_tradable_tickers()`, `fetch_one_ticker_5yr()`, `_fetch_ticker_after_date()` all call Railway |
| `local_runner/expr_cache_builder.py` | Expression cache | No Railway dependency. THE BOTTLENECK. |
| `local_runner/matrix_builder.py` | Universe matrix | No Railway dependency |
| `local_runner/market_cache_builder.py` | Market context cache | No Railway dependency (uses yfinance directly) |
| `scripts/build_tradable.py` | Tradable universe filter | Runs on Railway SQLite only |
| `scripts/fetch_fundamentals.py` | Fundamentals cache | No Railway dependency (uses Yahoo Finance API). Has dead Railway mirror at end. |
| `scripts/seed_vault.py` | Backup to Railway | Intentional Railway dependency (file mirror only). No longer syncs to Railway DB. |
| `nightly_refresh.bat` | Windows Task Scheduler trigger | No changes needed |
