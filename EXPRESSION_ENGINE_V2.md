# Expression Engine V2 — Build Plan

## Purpose

Every night: Fetch fresh OHLCV for all ~8,962 tickers via EODHD, then do a full expression cache rebuild for all of them. No skipping, no shortcuts. Because any ticker could become tradable tomorrow, and historical examples need complete data for tickers that were tradable in the past.

The output must be **identical** to what the current builder produces. Same .npz files, same date arrays. The cache is additive — old bars are never dropped or trimmed. The full historical series from EXPR_CACHE_START (2020-01-02) onward is preserved in every .npz file. Historical expression values are immutable — bar 500 of AAPL must produce the exact same 16,051 values today that it produced yesterday.

**Ticker count:** ~11,523 tickers in the daily OHLCV cache (from EODHD). Weekly cache matches daily. Monthly at ~11,239.

**Output format:** One .npz per ticker with `data` (float16 array on disk, n_bars x ~15,805) and `dates` (date strings). Data is cast to float32 on load via `load_ticker_cache()` — all consumers see float32 transparently. Storage dtype is float16 to halve disk usage (~111 GB total).

**History window:** EXPR_CACHE_START = 2020-01-02. OHLCV data before this date is truncated before computing expressions. ~6 years of history. This keeps cache size manageable and grinder scan times reasonable for the consensus pipeline (10-15 passes overnight).

**HTF look-ahead bias — FIXED (2026-04-01):** The partial candle engine (`local_runner/partial_candle_engine.py`) computes HTF expression values using only data available on each day. Monday of a week sees only Monday's partial weekly candle; Friday sees the full closed week. All prior completed periods use final closed values. Fallback to closed-candle mapping retained for unhandled ops. Requires full expression cache rebuild to take effect.

**Critical correctness gate:** After any rebuild with optimized code, the signal filter must still find ALL examples. If it doesn't, the optimization is broken and we don't ship it.

## What's In The Expression Library

### 1. LSP Detection (Left Side Pivots)
- Find all pivot highs and pivot lows across multiple window sizes (5, 10, 15, 20, 30, 40)
- For each pivot: track price, bars back, break count (how many times subsequent bars exceeded it)
- Return top N pivots ranked by prominence
- Expose per-level expressions: `level_above1_distance`, `level_above1_break_count`, `level_above1_bars_back_nearest`, etc.

### 2. Multi-Timeframe OHLCV
- Resample daily data to weekly (W), monthly (ME) using pandas
- Run the FULL existing expression library on each timeframe
- Expression naming: `w_rsi_14` (weekly RSI 14), `m_ext_above_avgc50` (monthly extension above 50 SMA), etc.

### 3. Contextual AVWAPs — REMOVED
- AVWAP computation has been removed from the project. Dan handles AVWAP manually at trade entry.
- All AVWAP code was removed from lsp_detector_v2.py, algo_line_detector.py, exit_compute.py, exit_expressions.py, and expression_engine.py (2026-04-02).

---

## Architecture Constraints (Non-Negotiable)

1. **Single computation path:** All expressions go through `ExpressionEngine` then `compute_series()`. No separate code paths for LSP/HTF.
2. **Precomputed in expr_cache_builder:** LSP detection, HTF resampling, and algo line detection happen during cache build. They are independent systems. Grinders never compute these live.
3. **No network calls in pipeline:** All data from local daily OHLCV cache.
4. **Parallel via ProcessPoolExecutor:** Same worker pattern as current cache builder. CPU-bound work spread across all cores. 8 workers.
5. **100% example pass rule:** New expressions either pass all examples or get auto-excluded from ranges.
6. **Grinders unchanged:** Pyramid, exit, outcome grinders see a bigger expression library. Same beam search, same matrix operations. Just more columns.
7. **Historical immutability:** Old bars' expression values never change between rebuilds. Only new bars get appended.

---

## Current State (2026-03-27)

**Expression library:** 16,051 expressions total
- 1,850 daily arithmetic (ma_slope, extension, rsi, adx, distance_to_maxh, etc.)
- 2,413 daily boolean aggregates (count_true, since_true, true_in_row on 127 unique conditions)
- 1,198 extension structure (on_series + on_series_bool_agg operating on extension series)
- 80 LSP precomputed
- 44 algo line precomputed
- 246 generic exit expressions
- 5,233 HTF weekly (w_ prefix on all daily expressions)
- 5,233 HTF monthly (m_ prefix on all daily expressions)

**Cache:** ~111 GB on disk. One .npz file per ticker (~11,201 tickers). Float16 arrays on disk (n_bars x ~15,805), cast to float32 on load. 6-year history window from 2020-01-02. Full rebuild: 124 min, 0 failures.

---

## Build Tasks

### Tasks A-G: Expression Library + Cache Infrastructure -- ALL COMPLETE (2026-02-27)

- **Task A:** LSP Detector refactor (`scripts/lsp_detector_v2.py`) -- 80 expressions, ~0.5s/ticker
- **Task B:** LSP expression registration in `brute_expressions.py`
- **Task C:** HTF resampling + integration -- weekly/monthly resampled OHLCV, HTF engine per timeframe
- **Task D:** Contextual AVWAPs -- REMOVED from project (2026-04-02)
- **Task E:** Cache builder integration -- built into Task C
- **Task F:** Matrix builder + example flow -- all grinders use expr cache
- **Task G:** Expression library registry -- HTF names auto-generated with w_/m_ prefix

### Task H: Cache Builder Performance Optimization -- IN PROGRESS (2026-03-27)

**Problem:** Nightly must rebuild expression cache for all ~10,856 tickers. At ~12s per ticker with 8 workers, that's ~275 minutes. Way too slow.

**Constraint:** Output must be identical. Historical expression values immutable. Signal filter must find all examples after rebuild.

**Benchmark-driven approach:** Built `scripts/benchmark_expr_cache.py` to measure per-phase timing for a single ticker, then iteratively applied targeted optimizations with correctness verification after each change. No production code modified until all optimizations are validated.

**Optimizations applied and benchmarked:**

**1. Numpy boolean aggregates (daily + HTF)**
- Replaced count_true: pandas rolling sum to numpy cumsum trick.
- Replaced since_true: pandas rolling apply lambda to numpy running counter. Pre-compute bars_since array once per unique condition (127 conditions), then slice by period.
- Replaced true_in_row: pandas rolling apply lambda to numpy backward scan.
- Applied same optimization to HTF weekly and monthly engines.
- Result: daily booleans 5.9x faster (2.09s to 0.35s per ticker)

**2. Vectorized extension structure linear regression**
- Replaced trendline_deviation and channel_position: per-bar Python for-loop to numpy sliding_window_view + vectorized mean/slope/std computation across ALL windows simultaneously.
- Result: ext linreg 64x faster (1.09s to 0.017s per ticker)

**3. Extension structure bool_agg caching**
- 760 on_series_bool_agg expressions share only 40 unique (series, bool_spec) combinations. Now: compute each unique indicator boolean once, cache it, dispatch all 760 expressions using numpy aggregation.
- Result: ext_struct total 7.1x faster (2.90s to 0.40s per ticker)

**4. Targeted slow-op numpy replacements (daily + HTF)**
- percentile_rank (25 expressions, 53ms each to <1ms each): pandas rolling apply to sliding_window_view + vectorized comparison. Saved 1.3s per ticker.
- swing_high/low_count (16 expressions): precompute swing boolean array once, reuse for all periods.
- higher/lower_high/low_count (32 expressions): same precomputed swing arrays.
- roc_percentile_rank (12 expressions): vectorized with sliding_window_view.
- bars_since_ma_cross (6 expressions): numpy loop replacement for nested Python loop.
- Applied same interceptions to HTF weekly and monthly.
- Result: daily_arith 3.0x faster (2.55s to 0.85s per ticker)

**5. Failed approach: precompute-all intermediates on DAILY data**
- Tried precomputing ALL intermediates upfront as numpy arrays on daily data, then dispatching all expressions from those intermediates.
- Result: 5x SLOWER than original. Upfront precompute cost exceeded savings because most intermediates are never used. The ExpressionEngine's lazy caching is the correct pattern.
- Lesson: only replace specific slow ops on daily, don't replace the dispatch pattern.
- BUT: this approach works perfectly on HTF data (260 weekly / 60 monthly bars) where precompute is cheap.

**6. HTF intermediates dispatch (v9)**
- On HTF data (260/60 bars), build_numpy_intermediates costs ~50ms (vs hundreds of ms on daily).
- Dispatch ALL HTF arith ops through dispatch_arith_numpy (direct numpy from precomputed dict), fallback to compute_series only for ops dispatch doesn't handle.
- 1,264 ops go through numpy dispatch, 340 fall back to compute_series per timeframe.
- Result: HTF weekly 2.5x faster (2.15s to 0.86s), HTF monthly 2.9x (1.24s to 0.43s)

**7. HTF extension structure optimization (v9b)**
- 1,216 on_series/on_series_bool_agg HTF expressions per timeframe were going through individual compute_series calls.
- Compute the 2 base extension series (ext_avgc50_adr14, ext_avgc200_adr14) at HTF resolution directly from the intermediates dict.
- Run vectorized linreg + cached bool_agg at HTF resolution, then map results to daily.
- Eliminates ~2,432 compute_series() calls total.

**8. Daily arith two-phase dispatch (v9c)**
- Phase A: SLOW_OPS with custom numpy implementations (percentile_rank, swing counts, bars_since_ma_cross). Warms the engine cache as side effect.
- Phase B: build_numpy_intermediates from the now-warm engine cache (~117ms), then dispatch remaining ~1,300 daily arith ops through dispatch_arith_numpy with compute_series fallback for ~459 unhandled ops.
- Result: daily_arith 3.3x faster (2.55s to 0.78s per ticker)

**9. Rolling max/min fix**
- _rolling_max and _rolling_min helpers in dispatch_arith_numpy replaced from Python for-loops to pandas rolling (14x faster).

**Latest benchmark results (worst-case ticker AFG, 1,297 bars):**

| Phase | Original (s) | Optimized (s) | Speedup |
|-------|-------------|--------------|---------|
| Daily arithmetic | 2.55 | 0.78 | 3.3x |
| Daily booleans | 2.01 | 0.35 | 5.7x |
| LSP + Algo | 0.67 | 0.67 | 1.0x (unchanged) |
| HTF weekly | 2.15 | 0.86 | 2.5x |
| HTF monthly | 1.24 | 0.43 | 2.9x |
| Extension structure | 2.90 | 0.41 | 7.1x |
| **TOTAL** | **11.51** | **3.50** | **3.29x** |

**Correctness:** 15,210 / 16,051 expressions produce identical output. 841 mismatches are NaN boundary differences at start of series (warmup bars only) across pctrank and ext_peak expressions. Zero value-level errors. Max diff: 0.000000.

**Files:**
- `scripts/benchmark_expr_cache.py` -- benchmark/test harness with all optimizations (v9c)
- `local_runner/expr_cache_builder.py` -- production file with wired optimizations

### Task H Phase 2: Production Wiring (2026-03-27)

Wired benchmark optimizations into production `expr_cache_builder.py`. Multiple iterations required — the benchmark ran in a single process and hid several production-only issues.

**CRITICAL CONSTRAINT: Every ticker must be fully computed. No skipping, no incremental appends, no reusing old cache. All 10,542 valid tickers × 16,051 expressions from scratch every time. This is non-negotiable.**

**What shipped (currently in production):**

1. **SLOW_OPS custom numpy (daily):** percentile_rank, roc_percentile_rank, bars_since_ma_cross, swing_high/low_count, higher/lower_high/low_count. These go through custom numpy implementations instead of compute_series. Saves ~1.7s/ticker on worst case.

2. **Numpy boolean aggregates (daily):** count_true, since_true, true_in_row all use numpy (cumsum trick, running counter, backward scan) instead of pandas rolling apply. 127 unique conditions computed once and cached. Saves ~1.7s/ticker.

3. **Extension structure (daily):** Vectorized linreg (trendline_deviation, channel_position via sliding_window_view) + cached bool_agg (40 unique indicator booleans computed once, dispatched to 760 expressions). Saves ~2.5s/ticker.

4. **HTF intermediates dispatch (weekly + monthly):** build_numpy_intermediates on HTF engine (cheap on 260/60 bar arrays), dispatch_arith_numpy for ~1,264 arith ops with compute_series fallback for ~340 unhandled. Numpy bools at HTF resolution. HTF ext struct at HTF resolution (compute base extension series from intermediates, vectorized linreg, cached bool_agg, then map to daily). Saves ~2.1s/ticker.

5. **Fast compression (zipfile compresslevel=1):** Replaced np.savez_compressed (default zlib) with zipfile.ZipFile at compresslevel=1. Benchmarked on realistic expression data: 6.75s → 1.62s per ticker (4.2x faster saves), files only 5% larger. This was the single biggest production win — compression was eating ~5s per ticker out of ~7s total.

6. **Worker-side saves (no IPC bottleneck):** build_full was using _compute_ticker_full which returned 83MB numpy arrays through IPC pipes to the main thread for serial save. Switched to _compute_and_save_ticker which saves .npz inside the worker. Eliminated 83MB serialize/deserialize per ticker + serial I/O blocking.

7. **Sorted work items:** Tickers sorted by bar count descending. Big tickers go first, short ones fill gaps at the end for better load balancing.

8. **Bounds checks on numpy helpers:** np_count_true, np_since_true, np_true_in_row, np_swing_count_rolling, np_trend_swing_count all need period <= n_bars guards. HTF monthly arrays can be as short as 20 bars but periods go up to 50. Without guards, IndexError crashes the entire ticker.

9. **RuntimeWarning suppression:** Added warnings.filterwarnings("ignore", category=RuntimeWarning) to match benchmark. Prevents numpy division warnings from being raised thousands of times per ticker.

**What was tried and FAILED / REVERTED:**

1. **Daily arith two-phase dispatch (Phase B — build_numpy_intermediates + dispatch_arith_numpy on daily data):** The benchmark showed this saving 0.78s vs 0.85s on daily arith. In production, it made things SLOWER. Reason: build_numpy_intermediates forces every engine method (dozens of MAs, RSIs, ADX, stoch, cci, bop, aroon, cmf, kaufman, bollinger, macd, obv...) to compute upfront, whether needed or not. The benchmark's single-ticker test had the engine warm from Phase A. In production workers processing cold engines across thousands of tickers, the upfront cost exceeded the dispatch savings. The ExpressionEngine's lazy caching is the correct pattern for daily data. REVERTED — daily arith (non-SLOW_OPS) goes through original compute_series. HTF dispatch kept because HTF arrays are small (260/60 bars) so precompute is genuinely cheap.

2. **15 workers (cpu_count - 1):** On i5-12600K (6P+4E cores, 16 logical), 15 workers caused severe contention. Per-ticker time went from 7.6s (8 workers) to 20s (15 workers). Throughput dropped from 1.1 to 1.0 tickers/s. CPU stayed at 43%. Workers fighting over shared CPU caches.

3. **Uncompressed saves (np.savez):** Would save ~6s/ticker in compression but cache grows from ~112GB to ~250-330GB. Dan's disk can't absorb that — only 480GB total with room needed for growing historical data. REVERTED.

**Current production performance (2026-03-27, build in progress):**

- 10,542 tickers × 16,051 expressions
- 14 workers on i5-12600K (10 cores / 16 logical)
- ~80% CPU utilization, ~14GB RAM (32GB available)
- 1.8 tickers/s throughput, ETA dropping (started at 137 min, trending toward ~80-90 min)
- Per-ticker time: ~7.8s at 125 tickers completed (biggest tickers first, will drop)
- Zero failures after bounds check fix
- Previous build (old code, ~4K tickers only): 6,107s (~102 min)

**What to try next to go faster:**

- The per-ticker time is dominated by: (a) ~1.6s compression at level 1, (b) ~1-2s HTF compute (already optimized), (c) ~3-4s daily compute_series calls through pandas ExpressionEngine. The daily compute_series path is the remaining target. The engine itself uses pandas Series everywhere — every MA, RSI, ATR call returns a pd.Series. A ground-up numpy-only engine would be faster but that's a large rewrite.
- LSP + Algo detectors (~0.67s) are untouched. Would require rewriting lsp_detector_v2 and algo_line_detector.
- Worker count sweet spot: 14 workers at 80% CPU is current best. 12 workers was 67% CPU / 1.5 tickers/s. 15 workers collapsed to 43% CPU due to contention when IPC was the bottleneck — may work better now with worker-side saves, but untested.
- Disk I/O: SSD at 3-4% utilization. Not a bottleneck.
- RAM: 18GB free. Could potentially be used for caching but each ticker has unique OHLCV so cross-ticker caching doesn't apply.

**Remaining opportunities (diminishing returns):**
- 459 daily arith fallback ops + 340 HTF fallback ops × 2: adding these to dispatch_arith_numpy would eliminate ~1,139 compute_series calls. Small per-call savings.
- LSP + Algo (0.67s): structural cost of the detectors. Would require rewriting lsp_detector_v2 / algo_line_detector.
- ext_ceiling_ratio (129ms, 40 exprs): goes through dispatch but _rolling_max is called 40× on computed series. Diminishing returns.

---

## Performance Summary

**Ticker count:** 10,542 valid tickers (≥50 bars) out of 10,856 in daily OHLCV cache. ALL get rebuilt every time. No skipping, no incremental, no shortcuts. Non-negotiable.

| Component | Before V2 | After V2 (pre-Task H) | After Task H (production) | Target |
|-----------|-----------|----------------------|--------------------------|--------|
| Expression count | 4,017 | 16,051 | 16,051 | -- |
| Tickers (build) | ~4,100 | ~10,542 | ~10,542 | 10,542 |
| Cache size (disk) | ~21 GB | ~112 GB | ~112 GB (5% larger with level 1) | -- |
| Build time | ~102 min (4K) | ~260 min est. (10.5K) | ~80-90 min est. (10.5K, in progress) | <60 min |
| Throughput | -- | ~0.7 tickers/s | ~1.8 tickers/s | >3 tickers/s |
| Workers | 8 | 8 | 14 | -- |
| CPU utilization | -- | ~39% | ~80% | >90% |
| RAM usage | -- | ~13 GB | ~14 GB | -- |
| Output | -- | -- | Identical format, .npz files, np.load compatible | -- |

## RAM Management (Critical)

The current expr_cache_builder.py deliberately loads the daily OHLCV pickle, prepares work items as dicts, then does `del universe_cache` + `gc.collect()` BEFORE spawning ProcessPoolExecutor workers. This is intentional -- ProcessPoolExecutor copies data to each worker process. Without freeing the pickle first, workers × ~250MB pickle = multi-GB wasted RAM on top of each worker's own allocations. On Dan's 32GB machine this has crashed before.

Any optimized worker must respect this pattern:
- The worker receives one ticker's OHLCV as a dict (not the full pickle)
- All numpy caches created inside the worker (swing arrays, bool caches, bars_since, sliding_window_view, intermediates dict) must be per-ticker only
- Worker must not hold references to large cross-ticker data
- The `del + gc.collect()` between phases in the main process is INTENTIONAL and must never be removed

RAM budget per worker: ~83MB output array (n_bars x 16,051 x 4 bytes) + intermediates (~3.5MB for daily intermediates dict + engine pandas cache) + HTF intermediates (~0.8MB for weekly + monthly) + OHLCV dict (~50KB). Total ~88MB per worker x 14 workers = ~1.2 GB. Actual observed: ~14 GB total process with 14 workers on 32 GB machine (18 GB free).

## Decisions (Resolved)

1. **HTF expression scope:** Full library on weekly + monthly. ~112 GB cache is fine.
2. **Contextual AVWAPs:** REMOVED from project (2026-04-02). Dan handles AVWAP manually at trade entry.
3. **Yearly timeframe:** EXCLUDED -- only ~5 bars in full history, useless for expressions. Weekly + monthly only.
4. **Number of LSP ranks:** ALL detected pivots, ranked. Top 5 above + 5 below exposed as expressions.
5. **Cache builder optimization approach:** Two modes — full rebuild (current, ~80-90 min) and incremental append (NEXT, target 2-5 min). Full rebuild uses per-ticker worker with lazy-cached ExpressionEngine + targeted numpy replacements. Incremental loads existing .npz, computes only the new bar's values, appends.
6. **Nightly ticker count:** All ~10,542 valid tickers (≥50 bars). Every ticker must go through the pipeline every night. For incremental mode, every ticker still gets processed — it's just computing 1 new bar instead of all bars.
7. **Precompute-all on daily data is net negative.** Tested in benchmark AND in production — both confirmed slower. Precompute on HTF is net positive (small arrays, trivial cost). Daily uses SLOW_OPS numpy + compute_series for everything else.
8. **Compression:** zipfile compresslevel=1 instead of np.savez_compressed default. 4.2x faster saves, 5% larger files.
9. **Worker count:** 14 on i5-12600K for full rebuild. Incremental may need different tuning.
10. **IPC:** Workers save .npz in-process, return only small metadata. NEVER return 83MB numpy arrays through IPC.
11. **HTF on incremental:** Cannot just copy forward on non-boundary days. Weekly/monthly candles are partial (current week/month in progress) — today's close, this week's high/low so far, etc. Must recompute the current HTF period's expressions every day, but only 1 HTF bar of computation. Full HTF history stays cached.

---

## Build Order

```
Tasks A-G (expression library + infrastructure)   -- ALL COMPLETE (2026-02-27)
    |
Task H Phase 1 (benchmark optimization)           -- COMPLETE (2026-03-27)
                                                      3.29x speedup on single ticker
                                                      Zero value errors
    |
Task H Phase 2 (production wiring — full rebuild) -- DONE (2026-03-27)
                                                      Wired: SLOW_OPS, numpy bools, ext struct,
                                                      HTF dispatch, fast compression, worker saves
                                                      Reverted: daily intermediates dispatch (slower)
                                                      Result: 1.8 tickers/s, ~80-90 min for 10.5K tickers
    |
Task H Phase 3 (incremental append)               -- IN PROGRESS (2026-03-29)
                                                      Increment 1: OHLCV infrastructure — DONE
                                                        - Rewire cache_builder.py: Railway → yfinance
                                                        - HTF caches merged into cache_builder.py
                                                          (weekly + monthly OHLCV, 10yr from yfinance)
                                                        - Wire HTF pickles into expr_cache_builder.py
                                                          (full rebuild uses HTF pickles, not resample)
                                                        - Rewire nightly.py: yfinance freshness check,
                                                          new weekly/monthly cache steps (10 steps total)
                                                      Increment 1b: Fill HTF gaps — DONE (2026-03-29)
                                                        - Ran --htf, filled to 10,822 weekly / 10,837 monthly
                                                        - ~34 weekly / ~19 monthly permanently unavailable (delisted)
                                                        - Found 2,152 weekly tickers with only 5 bars (bad prior fetch)
                                                        - Purged and refetched
                                                      Increment 1c: Data quality + harmonization — DONE (2026-03-30)
                                                        - HISTORY_START = "2016-01-01" — all caches use same start
                                                        - _yf_download switched from period= to start= (no silent truncation)
                                                        - Renamed 5yr→daily across 32 files + 5 docs
                                                        - File: universe_ohlcv_daily.pkl (fallback to 5yr.pkl)
                                                        - --all flag: daily + weekly + monthly in one command
                                                        - _batched_fetch(): adaptive rate limiting (scales workers
                                                          + sleep based on failure rate) with retry sweeps
                                                        - Full --all --force rebuild — DONE (2026-03-30)
                                                      Increment 2: True incremental expr cache append
                                                        - See detailed design below
    |
OHLCV --all --force rebuild                        -- DONE (2026-03-30) — but 3,081 tickers truncated
                                                      by yfinance rate limiting (silent partial data)
    |
Data validation infrastructure                     -- DONE (2026-03-30)
                                                      - ticker_reference.json: firstTradeDateMilliseconds
                                                        for all tickers (one-time, from yfinance .info)
                                                      - SPY fetched first on every run — date array is
                                                        ground truth for expected bar counts
                                                      - Exact match validation: ticker bars == SPY bars
                                                        from max(firstTradeDate, HISTORY_START). No tolerance.
                                                      - Mismatch = failed fetch → retry until pass or None
                                                      - Don't save cache until all tickers validated
                                                      - Split detection: tickers that split get full refetch
                                                        (historical prices changed by yfinance adjustment)
    |
EODHD migration (cache_builder.py)              -- DONE (2026-03-31)
                                                      - Replaced yfinance entirely with EODHD API
                                                      - OHLCV adjustment: ratio = adjusted_close / close
                                                        applied to O/H/L/C (split + dividend adjusted)
                                                      - EODHD_API_TOKEN from environment variable
                                                      - Ticker reference built from first-bar dates
                                                      - Split detection via adjusted_close comparison
                                                      - Adaptive rate limiting (~80 workers, backoff
                                                        settles at ~64w/4s under EODHD 1000/min limit)
                                                      - --force flag consistent: discard + rebuild
                                                        for daily, weekly, monthly, htf, all
                                                      - HTF full_sweep loads existing + fetches stale;
                                                        force_rebuild discards and starts fresh
                                                      - Daily: 8,962 tickers (down from 10,856 yfinance)
                                                        ~1,900 tickers EODHD doesn't cover — INVESTIGATING
                                                      - Weekly: 8,962 tickers (matches daily)
                                                      - Monthly: 8,935 tickers (27 too new for 3 bars)
                                                      - Still pending: nightly.py alignment,
                                                        market_cache_builder.py EODHD switch
    |
Full expr cache rebuild on EODHD data            -- DONE (2026-04-01)
                                                      11,201 tickers, 0 failures, 111 GB, 124 min
    |
Universe matrix rebuild                            -- DONE (2026-04-01)
                                                      11,201 tickers × 15,805 expressions
                                                      1.35 GB, 148s (parallel .npz file reads)
    |
Market cache EODHD migration                       -- NOT STARTED (separate task)
                                                      ~200 US ETFs can read from OHLCV daily pickle
                                                      ~60 non-equity instruments need original sources
                                                      (futures, ^VIX, Stooq breadth, FRED, BTC)
    |
Pyramid grind on DTSS                              -- verify baseline cache produces correct data
    |
Increment 2: True incremental expr cache append    -- next code task after baseline is verified
    |
Signal filter recheck                              -- confirm incremental results match baseline
    |
Switch nightly to incremental append               -- after recheck passes
```

### Task H Phase 3, Increment 1: OHLCV Infrastructure — DONE (2026-03-28)

**Problem:** The expr cache builder resampled daily OHLCV to weekly/monthly inside each worker via `resample_ohlcv()`. This blocked true incremental (need full daily history to resample). Additionally, `cache_builder.py` pulled OHLCV from Railway (fragile HTTP dependency).

**Solution:** Four changes, all shipped:

**1. Rewire `cache_builder.py` — Railway → yfinance**
- All Railway HTTP calls removed. `requests` and `API_BASE` deleted.
- `_yf_download(ticker, start, interval)` — unified download function using explicit start date (HISTORY_START). Never uses yfinance period= parameter.
- `_yf_append_after_date(ticker, after_date)` — fetches only new bars via yfinance `start=` param
- `get_tradable_tickers_local()` — reads from local SQLite `data/scanperfect.db`
- `append_daily_cache()` reads ticker list from existing pickle keys
- `check_yfinance_freshness()` — downloads 1 SPY bar, compares to cache
- Output format identical — same pickle, same DataFrame structure

**2. HTF caches merged into `cache_builder.py` (no separate file)**
- `universe_ohlcv_weekly.pkl` and `universe_ohlcv_monthly.pkl`
- Same dict-of-DataFrames format as daily cache
- Pulled from yfinance (`interval='1wk'`, `interval='1mo'`), NOT resampled from daily
- **10yr lookback** (10yr from HISTORY_START) — weekly expressions need ~4yr of bars for 200-period lookbacks plus warmup. 10yr gives 522 weekly bars, 120 monthly bars.
- Full build: `cache_builder.py --htf` or `--all`. Uses `_batched_fetch()` with adaptive rate limiting.
- Nightly append: `_merge_htf_bars()` — overwrite partial bar if same date, append if new date, freeze history
- Status: `cache_builder.py --htf-status`

**3. Wire HTF pickles into `expr_cache_builder.py`**
- `_compute_ticker_full()` now receives `(ticker, df_dict, weekly_df_dict, monthly_df_dict)`
- Uses HTF pickle data instead of `resample_ohlcv()` (fallback to resample if None)
- Removed HTF skip logic entirely (Monday/month-start checks, copy from previous cache — no longer needed with pre-fetched HTF data)
- Removed `_w_skip_htf_weekly`, `_w_skip_htf_monthly` globals
- `_init_worker()` simplified — no skip params
- `build_full()` and `append_new_bars()` load HTF pickles, pass per-ticker data to workers
- Added `_load_htf_cache(timeframe)` and `_df_to_dict(df)` helpers
- RAM: HTF dicts ~15KB per ticker (small). Freed after work item prep alongside daily cache.

**4. Rewire `nightly.py` — 10 steps (was 8)**
- Step 1: yfinance freshness check (replaces Railway `POST /api/universe/append-daily`)
- Step 2: daily append (yfinance)
- Steps 3-4: NEW — weekly + monthly cache appends
- Steps 5-10: expr cache, matrix, earnings, market, fundamentals, seed vault
- Railway only for earnings (step 7, broken) and seed vault (step 10)

**STATUS:** Increment 1 DONE.

**HTF cache initial build results (2026-03-28):**
- Weekly: 9,621 ok, 1,235 failed
- Monthly: 8,541 ok, 2,315 failed
- Failures are tickers yfinance can't return weekly/monthly data for (delisted, thin, etc.)

**HTF cache refactor (2026-03-28):**
Unified `_build_htf_cache` + `_append_htf_cache` into single `_sync_htf_cache(full_sweep)`.
- `full_sweep=True` (CLI `--htf`): updates stale tickers AND fetches missing ones (10yr)
- `full_sweep=False` (nightly steps 3-4): only updates existing tickers with recent bars
- Skips already-current tickers (compares last date to SPY's last date)
- Eliminates the old three-mode problem (build/append/retry were separate code paths)
- `build_htf_caches()` → `full_sweep=True`. `append_weekly()`/`append_monthly()` → `full_sweep=False`.

**NEXT:** Increment 2 (true incremental append) and market cache EODHD migration (separate task).

**Known issues (deferred):**
- ETA display in `_batched_fetch` inflates over time — rate calculation includes sleep time, making ETA grow even at constant per-batch speed. Cosmetic only.
- Empty cache save guard needed — `build_daily_cache` can save a 0-ticker pickle if ticker list is empty, which then blocks the fallback chain on next run. Needs a guard: don't save if universe is empty.
- Failed counter in `_sync_htf_cache` summary overcounts — reports first-attempt failures even when retry sweep succeeds. Cosmetic only.

### Task H Phase 3, Increment 2: True Incremental Append — DESIGN (2026-04-01)

**Problem:** Current `append_new_bars()` does a full recompute of every ticker with new bars — same as `build_full`, just scoped. Takes ~124 min. Nightly only adds 1 new bar per ticker. Every ticker in the daily OHLCV cache gets a new bar on each trading day.

**Critical constraint — HTF source consistency:** Every ticker must use the SAME HTF data source (pickle vs resample) in both full rebuild and incremental. If the full rebuild used pickle data for ticker X, incremental must also use pickle data. If pickle wasn't available and full rebuild resampled, incremental must also resample. Mixing sources = different HTF values for the same bar = broken immutability. This is why HTF gaps must be filled BEFORE the baseline full rebuild.

#### Expression Dependency Audit (2026-04-01)

Ran `scripts/validate_incremental_append.py` — classifies all 16,051 expressions by what each needs to compute a single new bar's value.

**Results (16,051 total):**

| Category | Count | Description |
|----------|-------|-------------|
| state_only | 1,177 | Prev expression row + today's OHLCV. Scalar math: EMA updates, slope diffs, ratios from today's bar. |
| lookback | 4,284 | Needs historical window of prior expression values. Rolling max, percentile rank, boolean scans, aroon argmax, CCI mean deviation, trendline regression. Max depth: 1,260 bars. |
| htf | 10,466 | Needs HTF OHLCV pickles (weekly + monthly). Partial candle engine builds intermediates on closed HTF series, extends to today's partial candle. |
| precomputed_lsp | 80 | LSP detector — runs `compute_all_lsp_series(df)` on full daily OHLCV. |
| precomputed_algo | 44 | Algo line detector — runs `compute_all_algo_series(df)` on full daily OHLCV. |

**Lookback depth distribution:**

| Depth | Count |
|-------|-------|
| 1-10 bars | 1,895 |
| 11-50 bars | 2,177 |
| 51-126 bars | 143 |
| 127-252 bars | 35 |
| 253-504 bars | 24 |
| 505-1260 bars | 10 |

The 10 deepest are all `ext_ceiling_ratio` from exit expressions with lookback=1260 (5 years).

**Immutability gate — PASSED:** Running `_compute_ticker_full` on N bars vs N-1 bars produces identical values for bar N-1. Zero mismatches across all 16,051 expressions. The computation is deterministic — same data, same bar, identical output regardless of how many bars follow.

**Incremental feasibility:** 15,927 expressions (99.2%) are incrementally computable without full OHLCV scan. Only 124 (0.8%) need full daily DataFrame (LSP + algo detectors).

#### Design — Per-Ticker Incremental Worker

**Phase 0: Load existing data**
1. Load existing .npz — get prev row (last row of cached data) and lookback buffer (last 1,260 rows)
2. Load today's OHLCV from daily pickle (one row)
3. Load weekly/monthly HTF DataFrames from HTF pickles (already updated in nightly steps 3-4)
4. Load full daily OHLCV DataFrame (needed only for LSP + algo)

**Phase 1: State-only expressions (1,177)**
5. Scalar math: prev row values + today's OHLCV → new values. EMA updates, MA slope diffs, RSI from prev avg_gain/avg_loss, extension ratios, candle ratios. No loops, no windows.

**Phase 2: Lookback expressions (4,284)**
6. Window operations on the lookback buffer (last 1,260 rows of expression values from cache). Rolling max, percentile rank, boolean count/scan, aroon argmax, CCI mean deviation, trendline regression. Each expression reads its own column from the buffer.

**Phase 3: HTF expressions (10,466)**
7. Build ExpressionEngine on closed HTF series (weekly ~300 bars, monthly ~75 bars — trivially fast)
8. `extract_closed_state()` — get intermediates + raw arrays from closed HTF
9. Build partial candle for today from daily OHLCV (one bar accumulated into current week/month)
10. `build_partial_intermediates()` — extend closed intermediates with today's partial candle
11. Dispatch all HTF arith/bool/ext_struct expressions from partial intermediates
12. Same partial candle engine code path as full rebuild — identical values guaranteed

**Phase 4: LSP + Algo (124)**
13. Run `compute_all_lsp_series(df)` on full daily OHLCV → extract last-bar values (80 expressions)
14. Run `compute_all_algo_series(df)` on full daily OHLCV → extract last-bar values (44 expressions)
15. These are the expensive phases (~0.64s combined per ticker) but only 124 expressions

**Phase 5: Save**
16. Write one row as raw binary float16 to .append file (~31 KB). NOT rewriting the full .npz — that's 1.6s/ticker saved.

#### Storage Layout

| File | Per ticker | Total (~11,201) | Description |
|------|-----------|-----------------|-------------|
| .npz | ~10 MB avg | ~111 GB | Frozen from last full rebuild. Never modified by append. |
| .append | ~31 KB/night | grows ~31 KB/night | Raw binary float16. One row per nightly append. |

**`load_ticker_cache()` change:** Read base .npz, then if .append file exists, read raw binary rows and vstack. Return combined array. Consumers see the same (n_bars, 16,051) float32 array — no API change.

**Full rebuild:** Deletes all .append files and regenerates .npz from scratch (same as today).

#### Benchmark Results (2026-04-01)

Ran `scripts/benchmark_incremental_append.py` on 100 randomly sampled tickers. HTF and LSP/algo phases are real computation. State and lookback phases are simulated with representative ops (actual forward-propagation engine not yet built — measured cost is a lower bound but these phases are <0.04s combined, so even 5x underestimate doesn't change the projection).

**Per-ticker cost breakdown (100-ticker average):**

| Phase | Mean (s) | Description |
|-------|----------|-------------|
| Load .npz | 0.07 | Read + decompress existing cache |
| State-only | <0.001 | 1,177 scalar math ops |
| Lookback | 0.03 | 4,284 window ops on buffer |
| HTF | 0.13 | Partial candle engine on weekly + monthly |
| LSP + Algo | 0.64 | Full detectors on daily OHLCV |
| Save | <0.001 | Write 31 KB raw binary |
| **TOTAL** | **0.87** | |

**Projected wall time (14 workers, 11,201 tickers):**

| Metric | Time |
|--------|------|
| Mean projection | 11.6 min |
| Median projection | 13.3 min |
| Without LSP+algo | 3.1 min |
| **Current full rebuild** | **~124 min** |

**Speedup: ~10x over full rebuild.**

LSP + algo is 73% of per-ticker cost. The actual incremental work (state + lookback + HTF + save) is 0.23s/ticker → 3.1 min projected. If LSP/algo detectors are ever optimized, the append drops toward 3 min.

#### Decisions

1. **Full rebuild stays as-is.** Incremental is a NEW code path in `append_new_bars()`, not a modification of `build_full()`.
2. **The resample fallback for HTF stays** in the incremental worker for tickers without HTF pickle data — same as full rebuild, preserving consistency.
3. **.npz files are never modified by append.** Raw binary .append files grow nightly. Full rebuild clears them.
4. **LSP + algo run on full daily OHLCV per ticker.** No incremental shortcut exists for pivot/algo detection — they scan the entire price history. This is the performance floor.
5. **Lookback buffer is 1,260 rows** (not 504 as originally estimated). 10 exit expressions have `ext_ceiling_ratio` with lookback=1260. Buffer size per ticker: 1,260 × 16,051 × 2 bytes (float16) ≈ 38 MB on disk, ~76 MB in float32 memory. Loaded from existing .npz tail.
6. **New tickers** (in daily cache but not in expr cache) get full compute via existing `_compute_and_save_ticker`. Rare after baseline build (only new IPOs/listings).
7. **No increase in grind times.** `load_ticker_cache()` returns the same (n_bars, 16,051) float32 array. Grinders don't know whether a row came from full rebuild or append.

#### What's Next

1. ~~Build `_append_one_ticker()` worker~~ — **DONE (2026-04-01).** Infrastructure shipped and validated (50/50 tickers, zero mismatches). Currently runs `_compute_ticker_full` internally — save-phase savings only (~1.6s/ticker).
2. ~~Correctness gate~~ — **DONE (2026-04-01).** `scripts/validate_append_infra.py` — fakes new bar by pretending last .npz row doesn't exist, verifies appended row matches fresh `_compute_ticker_full` output after float16 round-trip. Also tests `load_ticker_cache` vstack, `signal_filter._load_ticker_npz`, file sizes, cleanup.
3. Replace `_compute_ticker_full` inside `_append_one_ticker` with real forward-propagation using the four-file design below. Target: ~10ms/ticker → under 5 minutes total.
4. One-time setup: generate .lookback and .state files from existing cache (~33 min).
5. Integrate into nightly pipeline (step 5) — already wired, just needs real nightly run to confirm end-to-end.

#### Forward-Propagation Design — Four Files Per Ticker

*(Restored verbatim from original INCREMENTAL_APPEND_SPEC.md, commit a06f088)*

## Core Principle

**Zero lookback.** Each new bar is computed from:
1. Today's daily OHLCV candle (1 bar: O, H, L, C, V)
2. The previous bar's expression values (already in the cache)
3. A small state file per ticker (~3 KB) with intermediate values needed for forward computation

No ExpressionEngine. No full indicator series. No pandas. Pure numpy scalar math.

## Storage Format

### Base file: `{TICKER}.npz` (read-only after full rebuild)
- Existing compressed file from last full rebuild
- Contains `data` (float16, n_bars × 15,805) and `dates` (string array)
- **Never modified by nightly append.** Frozen until next full rebuild or manual consolidation.

### Append file: `{TICKER}.append` (raw binary, grows nightly)
- Raw float16 array, no compression, no headers
- Each night: 1 new row of 15,805 values appended (31 KB)
- File grows by 31 KB per trading day
- After 250 trading days: ~7.7 MB per ticker, ~86 GB total across all tickers
- To read: `np.frombuffer(data, dtype=np.float16).reshape(-1, 15805)`

### State file: `{TICKER}.state` (overwritten nightly)
- Raw float64 array, ~3 KB per ticker
- Contains intermediate values needed to compute the next bar
- Overwritten (not appended) each night with current state
- Total across all tickers: ~34 MB (negligible)

### Dates file: `{TICKER}.dates` (appended nightly)
- One date string per line, appended nightly
- Or raw binary of date ordinals — TBD simplest approach

## Consumer Impact

### `load_ticker_cache(ticker)` — updated
```
1. Load base .npz → dates_base, data_base (float16 → float32)
2. If .append file exists:
   a. Read raw binary → data_append (float16 → float32)
   b. Read .dates file → dates_append
   c. Concatenate: dates = concat(dates_base, dates_append)
   d. Concatenate: data = vstack(data_base, data_append)
3. Return (dates, data)  — shape (n_bars_total, 15805), float32
```
Consumers see the same (dates, data) tuple as before. The extra 212 state
variables are NOT in the expression columns — they're only in the .state file.
Column indices 0-15,804 are unchanged. No consumer code changes needed.

### `signal_filter._load_ticker_npz(ticker)` — updated same way
This function bypasses load_ticker_cache but does the same np.load.
Update to also read .append + .dates and concatenate.

### All other consumers
Use `ExprSeriesCache.get_ticker()` which calls `load_ticker_cache()`.
Transparent — no changes needed.

### Performance impact on grinds
- Base .npz decompression: ~0.3s per ticker (unchanged)
- Append file read: under 1ms (raw binary, small file)
- vstack: microseconds
- Net impact: negligible. Consensus runs (11+ hours) see no meaningful slowdown.

## State File Contents (~212 float64 values)

### Cumulative sums (for SMA-based indicators)
- `cumsum_close` — running sum of all closes from bar 0
- `cumsum_volume` — running sum of all volumes
- `cumsum_hl` — running sum of (high - low), for ADR
- `cumsum_tr` — running sum of true range, for ATR
- `cumsum_bop_raw` — for BOP SMA
- `cumsum_mfv` — money flow volume, for CMF
- `cumsum_abs_diff` — for Kaufman efficiency
- `cumsum_tp` — typical price, for CCI
- `cumsum_c2` — close squared, for Bollinger stddev

### EMA states
- `xavgc{p}` for p in [5,8,9,10,12,13,20,21,30,50,65,100,150,200] — 14 values
  (These are also daily intermediates but stored at float64 precision for forward computation)

### RSI internals
- `rsi_avg_gain_{p}`, `rsi_avg_loss_{p}` for p in [5,7,9,14,21,28] — 12 values
  RSI uses Wilder smoothing: avg_gain[i] = (avg_gain[i-1]*(p-1) + gain) / p
  Cannot be recovered from the RSI value alone without precision loss.

### ADX chain
- `ema_dmp_{p}`, `ema_dmm_{p}`, `ema_dx_{p}` for p in [7,10,14,20] — 12 values

### MACD signal line
- `macd_signal_{fast}_{slow}` for 5 MACD pairs — 5 values

### Stochastic raw_k
- `raw_k_{p}` for p in [3,5,7,9,10,14,21,28,50] — 9 values
  Stochastic uses 3-bar SMA of raw_k. Need previous 2 raw_k values.
  Actually need raw_k[i-1] and raw_k[i-2] — so 18 values (2 per period).

### Rolling max/min tracking
- `maxh_idx_{p}` for 29 maxH periods — bar index where current max occurred
- `minl_idx_{p}` for 19 minL periods — bar index where current min occurred
- `maxc_idx_{p}` for 3 maxC periods — bar index where current max close occurred
  Total: 51 values
  
  Update rule: if new bar's value >= current max, update index to current bar.
  If current max's index is falling out of the window (idx < current_bar - period),
  need to rescan the window. The window data is in the base .npz + .append file,
  which is already loaded by load_ticker_cache(). So the append worker loads the
  last N values for the affected period and finds the new max. This is rare
  (only when the old max drops off) and the scan is tiny (max 200 values).

### Aroon tracking
- `aroon_maxh_idx_{p}`, `aroon_minl_idx_{p}` for 7 periods — 14 values
  Same pattern as rolling max/min.

### OBV
- `obv` — cumulative, just previous value needed (already in intermediates)

### HTF partial candle state (per timeframe: weekly + monthly)
- `htf_{w,m}_partial_{open,high,low,close,volume}` — 10 values
- `htf_{w,m}_period_id` — 2 values (which week/month number we're in)
- `htf_{w,m}_xavgc{p}` — EMA states, 14 × 2 = 28 values
- `htf_{w,m}_ema_dmp_{p}`, `ema_dmm_{p}`, `ema_dx_{p}` — 12 × 2 = 24 values
- `htf_{w,m}_obv` — 2 values
- `htf_{w,m}_cumsum_*` — 11 cumsums × 2 = 22 values
- `htf_{w,m}_macd_signal_*` — 5 × 2 = 10 values
  Total HTF state: ~98 values

### LSP state
- Serialized pivot data: active pivot prices, break counts, bar indices
- Variable length — stored as a separate small binary blob or JSON
- Updated each bar: check if new pivot formed, update break counts

### Algo line state
- Serialized trendline data: slope, intercept, volume, bar index
- Similar to LSP — small variable-length blob

## 1-Bar Forward Computation — By Expression Type

### Daily Arithmetic Expressions (~1,850)

**extension(close, ma, normalizer):**
- SMA: `avgc50[i] = (cumsum_close[i] - cumsum_close[i-50]) / 50`
  where `cumsum_close[i] = cumsum_close[i-1] + close[today]` (from state)
  and `cumsum_close[i-50]` is row (current_bar - 50) in the cached data.
  For this we need to read that ONE historical value from the .append or base .npz.
  But wait — we said 1 bar lookback only. The cumsum at i-50 was the cumsum 
  state value 50 days ago. We don't have that in the state file.
  
  RESOLUTION: Store the cumsum in the STATE file. Store it also as a column
  in the .append file (as an intermediate column). Then cumsum[i-50] is 
  just column value at row (current_bar - 50) in the combined data.
  
  BUT — that means the .append file needs intermediate columns too, 
  which changes the column count from 15,805 to 15,805 + intermediates.
  And consumers only expect 15,805 columns.
  
  RESOLUTION: The .append file stores 15,805 + N_intermediates columns.
  load_ticker_cache() reads the .append file and returns ONLY the first
  15,805 columns to consumers. The append worker reads ALL columns 
  (including intermediates) for its forward computation.
  
  This way:
  - Consumers see 15,805 columns (unchanged)
  - Append worker has access to historical intermediate values via the .append file
  - No .state file needed for cumsums — they're in the .append columns
  - The .state file reduces to just the values that AREN'T stored historically
    (like rolling max/min indices, HTF partial candle state, LSP/algo state)

- EMA: `xavgc20[i] = alpha * close[today] + (1-alpha) * xavgc20[i-1]`
  Previous EMA value is in the intermediate column of the previous .append row.

- Result: `(close[today] - avgc50[i]) / atr14[i]`

**ma_slope(ma, offset, normalizer):**
- `(ma[i] - ma[i - offset]) / normalizer[i]`
- ma[i] computed as above. ma[i-offset] is the intermediate column value 
  at row (current_bar - offset) in the cached data.

**rsi(period):**
- `avg_gain[i] = (avg_gain[i-1] * (p-1) + max(0, close[today] - close[yesterday])) / p`
- `avg_loss[i] = (avg_loss[i-1] * (p-1) + max(0, close[yesterday] - close[today])) / p`
- `rsi[i] = 100 - 100 / (1 + avg_gain[i] / avg_loss[i])`
- avg_gain/avg_loss at i-1 stored as intermediate columns in previous .append row.
- close[yesterday] is the OHLCV close from the previous bar (in daily pickle or 
  stored in state).

**adx(period):**
- Needs smoothed DM+, DM-, then DI+, DI-, then DX, then smoothed DX.
- All EMA-based: `ema[i] = alpha * value + (1-alpha) * ema[i-1]`
- Previous EMA states in intermediate columns.

**rolling max/min (maxh, minl, maxc):**
- If new bar's high >= maxh[i-1]: maxh[i] = new high, update index
- Else if the bar at maxh_idx is still in window: maxh[i] = maxh[i-1]
- Else: rescan window from historical data (rare, max 200 values)
- For rescan: read the high column from intermediate columns in cached data

**percentile_rank(source, period):**
- Needs to know how many of the last N values are <= current value.
- This requires access to the last N values of the source.
- Source is one of: close, volume, range, atr14, rsi14.
- These are all available as intermediate columns in the cached data.
- Read last N values from the column, count <= current value. Simple.
- Max period is ~252. Reading 252 float16 values from a file = trivial.
- NOTE: This is a "lookback into cached data" but NOT into raw OHLCV.
  We're reading from our own intermediate columns in the .append file.

**bars_since_ma_cross(ma, max_lookback):**
- Scan backwards from current bar: is close > MA? Find where it flips.
- Previous bar's bars_since value helps: if sign didn't change, increment by 1.
  If sign DID change, reset to 1.
- Stored as expression column — just check: did close cross MA today?
  If same side: previous_value + 1. If crossed: 1.

**All other arithmetic ops** follow similar patterns:
- candle_range_ratio: (H-L) / ATR14 — pure OHLCV + intermediate
- body_range_ratio: abs(C-O) / (H-L) — pure OHLCV
- volume_ratio: V / avgv{p} — OHLCV + intermediate
- bollinger: need SMA + stddev. stddev from cumsum_c2 and cumsum_c intermediates.
- aroon: need position of max/min in window — tracked in intermediates
- cmf: need cumsum of MFV — intermediate column
- kaufman: need cumsum of abs_diff — intermediate column
- obv: obv[i-1] + sign(close_change) * volume — intermediate + OHLCV

### Daily Boolean Aggregates (~2,413)

**count_true(condition, period):**
- The condition (e.g., "rsi14_gt_50") evaluates to true/false at the new bar.
- `count[i] = count[i-1] - bool_value[i-period] + bool_value[i]`
- bool_value[i-period] is the boolean at the bar dropping out of the window.
- The boolean depends on an expression value (rsi14 > 50), which IS in the 
  cached data at row (i-period).
- So: read expression value at (i-period), evaluate condition, subtract.
  Evaluate condition at current bar, add.

**since_true(condition, period):**
- If condition is true today: 0
- Else: since_true[i-1] + 1 (capped at period, then -1)
- Previous since_true is in the expression column of previous row. Done.

**true_in_row(condition, period):**
- If condition is true today: true_in_row[i-1] + 1
- Else: 0
- Previous value is in the expression column. Done.

### Extension Structure (~1,198 on_series / on_series_bool_agg)

These operate on the 2 base extension series: ext_avgc50_adr14, ext_avgc200_adr14.
Both are regular daily arithmetic expressions — computed as part of step above.

**on_series inner ops (trendline_deviation, channel_position, etc.):**
- These are computed on the extension series as if it were price data.
- Same 1-bar-forward logic as daily arithmetic, but the "price" is the 
  extension series value instead of close.
- The extension series history is in the expression cache (it's an expression column).
- trendline_deviation needs a window of extension values + linear regression.
  Read the window from cached data, compute regression, get deviation at current bar.
- channel_position: same — window + regression + stddev normalization.

**on_series_bool_agg:**
- Same as daily boolean aggregates but on an indicator computed from the extension series.
- The indicator (e.g., RSI of extension series) needs its own forward state.
- Store these as additional intermediate columns.

### LSP Expressions (80 precomputed)

LSP detects pivot highs/lows and computes: distance, pivot_count, timeframe_count, break_count, 
max_window, bars_back_nearest, volume_ratio for top 5 levels above and below.

**1-bar forward update:**
- Did today's bar create a new pivot? Check if H[today] > H[yesterday] and 
  H[yesterday] > H[two days ago] (for pivot high at yesterday). Need H[yesterday-1]
  which is just the previous bar's high — available from state or intermediate.
- Actually, pivot detection needs N bars on each side (window sizes 5,10,15,20,30,40).
  A pivot at bar X with window 40 is only confirmed 40 bars LATER. So pivots are 
  detected with a lag. On each new bar, check if bar (today - window) is a pivot.
- Update break counts: for each active level, did today's bar break it?
  Compare today's H/L against level prices.
- Update distances: (level_price - close[today]) / ATR14[today]
- Update bars_back: increment by 1 for all levels.

LSP state is variable-length (depends on how many pivots are active).
Stored as a serialized blob in the .state file.

### Algo Line Expressions (44 precomputed)

Similar to LSP — detect trendlines from high-volume bars, track distance/slope/touch_count.

**1-bar forward update:**
- Check if today forms a new trendline anchor (high volume bar).
- Update distances to existing trendlines.
- Track touches and breaks.

Algo state is also variable-length, stored in .state file.

### HTF Expressions (~10,466: 5,233 weekly + 5,233 monthly)

**Partial candle update:**
- Load HTF partial candle state from state file (or previous intermediate columns)
- Is today in the same period as yesterday?
  - Same period: update partial candle
    - high = max(partial_high, today_high)
    - low = min(partial_low, today_low)  
    - close = today_close
    - volume += today_volume
    - open stays the same
  - New period: the previous partial becomes a closed candle
    - Update all HTF closed intermediates (EMA states roll forward, cumsums increment)
    - Start new partial: open=today_open, high=today_high, low=today_low, 
      close=today_close, volume=today_volume
- Compute all HTF expression values using the same 1-bar-forward formulas
  as daily, but operating on HTF intermediates + partial candle values.
- The partial candle engine already does exactly this logic.

**Period boundary detection:**
- Weekly: is today Monday? (or first trading day of the week)
- Monthly: is today the first trading day of the month?
- Compare today's date vs previous bar's date to detect boundary.

## One-Time Setup (~33 minutes)

Transform existing cache to add intermediate columns and generate state files:

For each of 11,200 tickers:
1. Load .npz (dates + data)
2. Load ticker's OHLCV from daily pickle
3. Build ExpressionEngine on full OHLCV
4. Run build_numpy_intermediates() → get all intermediate arrays
5. Extract the cumulative sums and EMA states needed for forward computation
6. Save .state file with last-bar values of all state variables
7. Compute intermediate columns for all historical bars
8. Save .append file with intermediate columns (so future lookbacks work)
   — Actually NO. The .append file only stores rows AFTER the base .npz.
   Historical intermediate values for rows IN the base .npz are NOT accessible
   during append. This is a problem for SMA (need cumsum[i-50]) and 
   percentile_rank (need last MAX_LOOKBACK values) and count_true (need bool[i-period]).

   RESOLUTION: During setup, also write a `.history` file containing the 
   intermediate columns for the LAST N rows of the base .npz (where N = max 
   lookback period, ~252). This is ~252 × intermediates × 2 bytes per ticker.
   That's 252 × 178 × 2 = ~179 KB per ticker, ~2 GB total. Negligible.
   
   On append: the worker reads the .history file to get intermediate values
   for rows that are in the base .npz. As new rows accumulate in .append,
   the .history window slides forward and old rows in .history become unused.
   Eventually all lookback rows are in .append and .history is no longer needed.

   Actually simpler: the .history file just stores the last MAX_LOOKBACK rows of 
   intermediate columns from the base .npz. Fixed size, written once during setup.
   The append worker reads from .history OR .append depending on which row it needs.

Wait — this is getting complicated. Let me simplify.

## REVISED: Append File Stores Everything

The .append file stores 15,805 expression columns + N intermediate columns per row.

During the one-time setup:
1. Load .npz
2. Compute intermediates for ALL historical bars  
3. Write the LAST MAX_LOOKBACK rows (expressions + intermediates) to a .lookback file
4. Write .state file (last-bar values only)

Nightly append:
1. Read .state file (last-bar intermediates + HTF state + LSP/algo state)
2. Read today's OHLCV bar
3. For expressions needing lookback (SMA, count_true, percentile_rank, rolling max/min):
   Read the specific historical value from .lookback or .append file
4. Compute new intermediates + new expression values
5. Append full row (expressions + intermediates) to .append file
6. Update .lookback: drop oldest row, add new row (sliding window)
7. Overwrite .state file

load_ticker_cache():
1. Read base .npz → data_base (n_base × 15,805)
2. Read .append → data_append, take ONLY first 15,805 columns
3. Concatenate and return

This means:
- .append file is wider than base .npz (has intermediate columns)
- load_ticker_cache strips the extra columns — consumers see 15,805
- .lookback file provides the trailing window of intermediates for 
  lookback ops (cumsums, rolling max indices, boolean values from N bars ago)
- .state file provides the current state for non-historical intermediates
  (HTF partial candle, LSP/algo state)

## File Summary

| File | Size per ticker | Total | Written when | Purpose |
|------|----------------|-------|-------------|---------|
| `{T}.npz` | ~10 MB | 111 GB | Full rebuild only | Base historical data (15,805 cols) |
| `{T}.append` | 31 KB/day | ~0.3 GB/month | Nightly | New expression rows (15,805 + intermediates) |
| `{T}.lookback` | ~179 KB | ~2 GB | Setup + nightly | Last MAX_LOOKBACK rows of intermediates (sliding window) |
| `{T}.state` | ~3 KB | ~34 MB | Nightly | Forward computation state (HTF, LSP, algo, EMA, cumsums) |
| `{T}.dates` | ~10 bytes/day | tiny | Nightly | Date strings for appended rows |

Total disk after setup: 111 + 1 + 0.034 = ~112 GB
Growth: ~0.3 GB per month (from .append files)
After 1 year: 111 + 3.6 + 1 = ~116 GB

## Consolidation (Optional, Quarterly)

Merge base .npz + .append into new base .npz. Regenerate .lookback from new base.
Delete old .append. Takes ~33 minutes. Resets growth to zero.

## Nightly Append Timing Estimate

Per ticker:
- Read .state: < 1ms (3 KB)
- Read lookback values: < 1ms (sequential read from .lookback)
- Compute intermediates: < 1ms (scalar math)
- Compute 15,805 expressions: < 1ms (scalar math from intermediates)
- Compute HTF expressions: < 1ms (same scalar math on HTF intermediates)
- Compute LSP/algo updates: < 5ms (pivot detection, break checking)
- Write .append row: < 1ms (seek + 32 KB write)
- Write .state: < 1ms (3 KB overwrite)
- Update .lookback: < 1ms (shift + write)

Total per ticker: ~10ms

11,200 tickers / 14 workers: 11,200 × 10ms / 14 = ~8 seconds

Add overhead (pickle load, process spawning, manifest update): ~2-3 minutes

**Total nightly append: under 5 minutes.**


## MAX_LOOKBACK Computation

The lookback window is computed from the expression library at setup time:

```
MAX_LOOKBACK = max across all expressions of their window/lookback parameter
```

Current value: **504 bars** (from extension_ceiling_ratio lookback=504).

Other large lookbacks:
- extension_ceiling_ratio: 504
- extension_peak_ratio: 252
- percentile_rank: 252
- roc_percentile_rank: 252 (+ roc_period 50 = 302 total)
- bollinger_bandwidth_rank: 252
- SMA close (avgc200): 200
- maxH (126-bar rolling max): 126

This value is stored in the manifest at setup time. If the expression library
changes (fingerprint mismatch), a full rebuild is triggered anyway, which
recalculates MAX_LOOKBACK and regenerates all .lookback files.

The .lookback file stores MAX_LOOKBACK rows of intermediate columns from the
end of the base .npz. Size per ticker: 504 × 178 intermediates × 2 bytes = ~179 KB.
Total: ~2 GB across all tickers.

As nightly appends accumulate, the .append file grows and eventually contains
enough rows that the lookback window is entirely within .append data (after
504 trading days ≈ 2 years). At that point .lookback is redundant but harmless.
Consolidation before then keeps .lookback relevant.

##### Clarification: 504 vs 1,260 Lookback

**Two distinct lookback requirements:**

1. **Intermediate lookback (for .lookback file): 504 bars.** This is how far back the forward-propagation formulas need to reach into intermediate columns (cumsums, raw OHLCV). Driven by extension_ceiling_ratio (504), percentile_rank (252), roc_percentile_rank (302), avgc200 (200).

2. **Expression-value lookback: 1,260 bars.** Some expressions (10 exit `ext_ceiling_ratio` variants with lookback=1260) need rolling max over 1,260 prior EXPRESSION values. These read directly from the loaded .npz tail + .append data — NOT from .lookback. The append worker loads the .npz tail (last 1,260 rows of expression columns) for these ops.

#### Bugs Found and Fixed During Increment 2 Build (2026-04-01)

1. **Missing HTF truncation in `append_new_bars()`:** `build_full()` truncates weekly/monthly pickles to `EXPR_CACHE_START` (2020-01-02) before passing to workers. `append_new_bars()` did not — passed untruncated HTF data back to `HISTORY_START` (2016-01-01). Extra historical HTF bars shifted the partial candle engine's `lci` (last closed index) mapping, producing different expression values for the same daily bar. Immutability violation. Fixed.
2. **Missing OHLCV truncation in `append_new_bars()`:** Same issue for daily OHLCV — existing tickers were not truncated to `EXPR_CACHE_START`, getting ~2,500 bars instead of ~1,500. Caused ~2x compute slowdown. Fixed.
3. **Missing work item sorting in `append_new_bars()`:** `build_full()` sorts by bar count descending for load balancing. `append_new_bars()` did not. Fixed.


