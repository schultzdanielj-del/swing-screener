# Nightly Refresh — Overhaul Spec

**Created:** 2026-03-25
**Branch:** `v2-consensus`
**Goal:** Nightly refresh finishes in under 30 minutes with zero Railway dependency for data. Railway stays for seed vault backup only.

---

## WHAT'S BROKEN NOW

### The nightly pipeline (`local_runner/nightly.py`) has 9 steps:

| Step | What it does | Time | Problem |
|------|-------------|------|---------|
| 1 | Calls Railway `POST /api/universe/append-daily` | 5-15 min | **Railway dependency.** If Railway is down, nothing runs. Railway fetches yfinance, writes to its SQLite, then acts as gate for the whole pipeline. |
| 2 | Rebuilds 300-bar daily OHLCV cache from Railway | ~10 min | **Dead weight.** Nothing uses this cache. Everything reads from the 5yr pickle. |
| 3 | Appends new bars to 5yr OHLCV pickle from Railway | ~10 min | **Railway dependency.** Calls `_fetch_ticker_after_date()` per ticker via Railway's `/api/query/bulk`. |
| 4 | Appends expression cache | **~91 min** | **THE BOTTLENECK.** See section below. |
| 5 | Rebuilds universe matrix | ~30s | Fine as-is. |
| 6 | Refreshes earnings dates via Railway | Fails silently | **Endpoints don't exist.** `POST /api/universe/refresh-earnings` and `GET /api/universe/earnings-status` are not in `server.py`. Has been failing silently every night. |
| 7 | Appends market context cache (266 instruments) | ~2-3 min | Fine as-is. Uses yfinance directly. |
| 8 | Refreshes fundamentals cache | ~10 min (Mon) / <1 min | Fine, but has a dead Railway mirror call at the end. |
| 9 | Seed vault backup to Railway | ~1-2 min | Fine. Keep as-is. |

**Total current runtime: ~2.5-3 hours.** Target: under 30 minutes.

### Railway dependencies that need removing:

1. **`cache_builder.py` → `get_tradable_tickers()`** (line 35): Calls Railway's `/api/query/bulk` to `SELECT ticker FROM tradable_universe`. No local equivalent exists.
2. **`cache_builder.py` → `fetch_one_ticker_5yr()`** (line 180): Fetches ALL OHLCV per ticker from Railway.
3. **`cache_builder.py` → `_fetch_ticker_after_date()`** (line 303): Fetches new bars per ticker from Railway.
4. **`build_tradable.py`** (line 56): Only runs on Railway's SQLite DB. Filters: close ≥ $1, APTR ≥ 1.5%, avg dollar volume ≥ $5M over 20 days. Needs a local equivalent.
5. **`nightly.py` step 1**: Calls Railway to trigger yfinance fetch.
6. **`nightly.py` step 6**: Calls non-existent Railway endpoints.
7. **`nightly.py` step 8**: Mirrors fundamentals cache to Railway (redundant — seed vault handles backup).

### What does NOT need changing:

- Steps 4, 5, 7 (expr cache, matrix, market cache) — work locally already
- Step 9 (seed vault) — Railway dependency is intentional
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

### Directions NOT yet explored:

1. **Skip unchanged tickers.** On a normal trading day, virtually all tickers get 1 new bar, so all ~4,114 need recomputing. But on weekends/holidays, 0 tickers need updating. The code already handles this (checks bar counts against manifest). This doesn't help for normal days.

2. **Reduce expression count.** 10,466 of 15,805 are HTF (weekly + monthly). If HTF expressions don't contribute meaningfully to EV grinder scoring, dropping them cuts compute by 66%. This is a pipeline design question, not a code optimization.

3. **Cache intermediate indicators.** `ExpressionEngine._cache` already caches MAs, ATR, RSI etc. within one ticker. But the cache is rebuilt per ticker. If many expressions share the same base indicator (e.g., hundreds use `adr14`), it's already cached. The overhead is in the per-expression Python function dispatch + pandas Series creation.

4. **Numpy-only computation.** Replace pandas Series operations with raw numpy arrays throughout `compute_series()` and `ExpressionEngine`. Pandas rolling operations have significant per-call overhead (index alignment, NaN handling, Series construction). Numpy equivalents would be faster but would require rewriting ~90 ops in `backtest_conditions.py` and all of `ExpressionEngine`. Major effort.

5. **Batch HTF computation.** Instead of calling `compute_series()` 5,233 times for weekly, batch similar ops. E.g., compute all `extension` ops at once since they share the same MA computation. The `ExpressionEngine` cache partially handles this, but the Python for-loop dispatch is still 5,233 iterations.

6. **C extension / Cython / Numba.** Compile the hot loop. Would require significant rework but could get 10-50x speedup on the per-expression computation.

7. **Incremental computation for simple ops.** Some expressions (simple ratios, single-bar calculations, slopes) could theoretically be computed from just the last N bars of history rather than the full series. But the `ExpressionEngine` is designed around full-series computation with pandas, and the HTF resampling step needs full history. Partial solution at best.

8. **Parallel within-ticker.** Currently each worker handles one ticker sequentially (all 15,805 expressions). Could split expressions across workers for a single ticker. But the `ExpressionEngine` cache is per-ticker, and cross-process sharing of the cache would be complex.

9. **Accept the time and start earlier.** If the nightly runs at 3:00pm ET instead of 4:30pm, a 91-minute expr cache step finishes by ~4:30pm, leaving time for the consensus pipeline overnight. Doesn't fix the problem but unblocks the workflow. The market_cache_builder already fetches from yfinance before market close with no issues — partial-day data gets overwritten the next day.

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

### Step 6 (earnings) — kill or replace:

The Railway endpoints don't exist. Two options:
- **Kill it.** Earnings dates are in local SQLite, backed up via seed vault. Nobody populates them nightly.
- **Replace with local Yahoo Finance scraper.** Use the same quoteSummary API as `fetch_fundamentals.py`. ~4,167 requests at 0.15s = ~10 minutes.

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
| `local_runner/nightly.py` | Orchestrator | Steps 1, 6 call Railway |
| `local_runner/cache_builder.py` | OHLCV cache management | `get_tradable_tickers()`, `fetch_one_ticker_5yr()`, `_fetch_ticker_after_date()` all call Railway |
| `local_runner/expr_cache_builder.py` | Expression cache | No Railway dependency. THE BOTTLENECK. |
| `local_runner/matrix_builder.py` | Universe matrix | No Railway dependency |
| `local_runner/market_cache_builder.py` | Market context cache | No Railway dependency (uses yfinance directly) |
| `scripts/build_tradable.py` | Tradable universe filter | Runs on Railway SQLite only |
| `scripts/fetch_fundamentals.py` | Fundamentals cache | No Railway dependency (uses Yahoo Finance API). Has dead Railway mirror at end. |
| `scripts/seed_vault.py` | Backup to Railway | Intentional Railway dependency. Keep. |
| `nightly_refresh.bat` | Windows Task Scheduler trigger | No changes needed |
