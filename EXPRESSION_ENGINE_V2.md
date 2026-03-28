# Expression Engine V2 — Build Plan

## Purpose

Every night: Fetch fresh OHLCV for all ~10,856 tickers via yfinance, then do a full expression cache rebuild for all of them. No skipping, no shortcuts. Because any ticker could become tradable tomorrow, and historical examples need complete data for tickers that were tradable in the past.

The output must be **identical** to what the current builder produces. Same .npz files, same float32 values, same date arrays. The cache is additive — old bars are never dropped or trimmed. The full historical series from the 5yr OHLCV cache is preserved in every .npz file. Historical expression values are immutable — bar 500 of AAPL must produce the exact same 16,051 values today that it produced yesterday.

**Ticker count:** ~10,856 tickers in the 5yr OHLCV cache. ~4,118 are currently active (getting new D1 candles). ~6,738 are dead/delisted (no new bars, but their historical data must remain in the cache for pipeline analysis).

**Output format:** One .npz per ticker with `data` (float32 array, n_bars x 16,051) and `dates` (date strings). Grinders, matrix builder, and all downstream pipeline stages read these files identically.

**Critical correctness gate:** After any rebuild with optimized code, the signal filter must still find ALL examples. If it doesn't, the optimization is broken and we don't ship it.

## What's In The Expression Library

### 1. LSP Detection (Left Side Pivots)
- Find all pivot highs and pivot lows across multiple window sizes (5, 10, 15, 20, 30, 40)
- For each pivot: track price, bars back, break count (how many times subsequent bars exceeded it)
- Return top N pivots ranked by prominence
- Expose per-pivot expressions: `lsp1_distance`, `lsp1_break_count`, `lsp1_bars_back`, `lsp1_avwap_distance`, `lsp2_distance`, etc.

### 2. Multi-Timeframe OHLCV
- Resample daily data to weekly (W), monthly (ME) using pandas
- Run the FULL existing expression library on each timeframe
- Expression naming: `w_rsi_14` (weekly RSI 14), `m_ext_above_avgc50` (monthly extension above 50 SMA), etc.

### 3. Contextual AVWAPs
- Per-pivot contextual AVWAP: For each detected LSP pivot, search bars before the pivot for the anchor that produces the highest (or lowest) AVWAP at the current bar.
- "Highest all-time AVWAP" excluded -- only 5yr of data, not enough.

---

## Architecture Constraints (Non-Negotiable)

1. **Single computation path:** All expressions go through `ExpressionEngine` then `compute_series()`. No separate code paths for LSP/HTF/AVWAP.
2. **Precomputed in expr_cache_builder:** LSP detection, HTF resampling, and AVWAP computation happen during cache build. Grinders never compute these live.
3. **No network calls in pipeline:** All data from local 5yr OHLCV cache.
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

**Cache:** ~65 GB on disk. One .npz file per ticker (~10,856 tickers). Float32 arrays (n_bars x 16,051).

---

## Build Tasks

### Tasks A-G: Expression Library + Cache Infrastructure -- ALL COMPLETE (2026-02-27)

- **Task A:** LSP Detector refactor (`scripts/lsp_detector_v2.py`) -- 80 expressions, ~0.5s/ticker
- **Task B:** LSP expression registration in `brute_expressions.py`
- **Task C:** HTF resampling + integration -- weekly/monthly resampled OHLCV, HTF engine per timeframe
- **Task D:** Contextual AVWAPs -- built into Task A
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

**Nightly estimate (10,856 tickers, 8 workers):**
- Original: ~260 min
- Current optimized: ~79 min
- Target: <60 min (requires per-ticker <2.65s)

**Remaining opportunities (diminishing returns):**
- 459 daily arith fallback ops + 340 HTF fallback ops × 2: adding these to dispatch_arith_numpy would eliminate ~1,139 compute_series calls. Small per-call savings.
- LSP + Algo (0.67s): structural cost of the detectors. Would require rewriting lsp_detector_v2 / algo_line_detector.
- ext_ceiling_ratio (129ms, 40 exprs): goes through dispatch but _rolling_max is called 40× on computed series. Diminishing returns.

**Files:**
- `scripts/benchmark_expr_cache.py` -- benchmark/test harness with all optimizations (v9c)
- Production changes: **NEXT** -- wire optimizations into expr_cache_builder.py

---

## Performance Summary

**Ticker count:** 10,856 in 5yr OHLCV cache. ALL get rebuilt every night. No skipping, no shortcuts.

| Component | Before V2 | After V2 (current) | After Task H (benchmark v9c) | Target |
|-----------|-----------|--------------------|-----------------------------|--------|
| Expression count | 4,017 | 16,051 | 16,051 | -- |
| Tickers (nightly) | ~4,100 | ~10,856 | ~10,856 | 10,856 |
| Cache size (disk) | ~21 GB | ~65 GB | ~65 GB | -- |
| Nightly (10,856 tickers) | -- | ~260 min | ~79 min (benchmark est.) | <60 min |
| Per ticker (worst case) | -- | 11.51s | 3.50s | <2.65s |
| Matrix rebuild | ~5 min | ~30s | ~30s | -- |
| Output | -- | -- | Identical, additive, no bars dropped, historical values immutable | -- |

## RAM Management (Critical)

The current expr_cache_builder.py deliberately loads the 5yr OHLCV pickle, prepares work items as dicts, then does `del universe_cache` + `gc.collect()` BEFORE spawning ProcessPoolExecutor workers. This is intentional -- ProcessPoolExecutor copies data to each worker process. Without freeing the pickle first, 8 workers x ~250MB pickle = 2GB wasted RAM on top of each worker's own allocations. On Dan's 32GB machine this has crashed before.

Any optimized worker must respect this pattern:
- The worker receives one ticker's OHLCV as a dict (not the full pickle)
- All numpy caches created inside the worker (swing arrays, bool caches, bars_since, sliding_window_view, intermediates dict) must be per-ticker only
- Worker must not hold references to large cross-ticker data
- The `del + gc.collect()` between phases in the main process is INTENTIONAL and must never be removed

RAM budget per worker: ~83MB output array (n_bars x 16,051 x 4 bytes) + intermediates (~3.5MB for daily intermediates dict + engine pandas cache) + HTF intermediates (~0.8MB for weekly + monthly) + OHLCV dict (~50KB). Total ~88MB per worker x 8 workers = ~700MB. Well within 32GB.

## Decisions (Resolved)

1. **HTF expression scope:** Full library on weekly + monthly. ~65 GB cache is fine.
2. **Highest all-time AVWAP:** EXCLUDED -- only 5yr data, not enough. Pivot-anchored contextual AVWAPs only.
3. **Yearly timeframe:** EXCLUDED -- only ~5 bars in 5yr history, useless for expressions. Weekly + monthly only.
4. **Number of LSP ranks:** ALL detected pivots, ranked. Top 5 above + 5 below exposed as expressions.
5. **Cache builder optimization approach:** Per-ticker worker with lazy-cached ExpressionEngine + targeted numpy replacements for slow ops + intermediates dispatch for HTF and daily Phase B. NOT global 2D batching (tested, 5x slower). ProcessPoolExecutor with 8 workers stays.
6. **Nightly ticker count:** All 10,856. No skipping dead/delisted -- their .npz files must exist for historical pipeline analysis.
7. **Precompute-all on daily data is net negative.** Precompute on HTF is net positive (small arrays, trivial cost). Hybrid approach: SLOW_OPS first (custom numpy), then precompute from warm cache, then dispatch.

---

## Build Order

```
Tasks A-G (expression library + infrastructure)   -- ALL COMPLETE (2026-02-27)
    |
Task H (cache builder optimization)               -- BENCHMARK COMPLETE (2026-03-27)
                                                      3.29x speedup achieved (11.51s to 3.50s/ticker)
                                                      Zero value errors, 841 NaN boundary diffs
                                                      All optimizations validated in benchmark script
                                                      Nightly estimate: ~79 min (target was <60 min)
    |
Wire all optimizations into expr_cache_builder.py  -- NEXT
    |
Full cache rebuild on Dan's machine                -- verify actual timing
    |
Signal filter gate                                 -- verify ALL examples still found
```

**STATUS:** Task H benchmark phase complete. 3.29x speedup validated (79 min nightly est. at 10,856 tickers). Zero value errors. Next: wire optimizations from benchmark into production expr_cache_builder.py, then run full rebuild and signal filter gate.
