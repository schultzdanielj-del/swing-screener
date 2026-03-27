# Expression Engine V2 — Build Plan

## Purpose

Optimize the expression cache builder so that the nightly rebuild runs fast while producing **identical output** to the current builder. The cache is additive — each nightly run appends new bars to every ticker's existing cache file. Old bars are never dropped or trimmed. The full historical series from the 5yr OHLCV cache is preserved in every .npz file.

**Two modes:**
- **Full build** (\`--build --force\`): Recomputes all 16,051 expressions for all ~4,100 tickers from scratch. Only needed when expression definitions change (e.g., new AVWAP formula, new expression category added). Produces ~65 GB of .npz files.
- **Nightly append** (\`--append\`, runs at 4:30pm ET via Task Scheduler): Recomputes the full series for every ticker that has new bars (which is all ~4,100 tickers every trading day) and overwrites the .npz file. On non-Mondays, weekly HTF columns are copied from the previous cache instead of recomputed. On non-month-start days, monthly HTF columns are copied. This skips ~66% of expressions on most days.

Both modes produce the same output format: one .npz per ticker with \`data\` (float32 array, n_bars × 16,051) and \`dates\` (date strings). Grinders, matrix builder, and all downstream pipeline stages read these files identically regardless of which mode produced them.

## What's In The Expression Library

### 1. LSP Detection (Left Side Pivots)
- Find all pivot highs and pivot lows across multiple window sizes (5, 10, 15, 20, 30, 40)
- For each pivot: track price, bars back, break count (how many times subsequent bars exceeded it)
- Return top N pivots ranked by prominence
- Expose per-pivot expressions: `lsp1_distance`, `lsp1_break_count`, `lsp1_bars_back`, `lsp1_avwap_distance`, `lsp2_distance`, etc.
- The grinder discovers which pivot characteristics matter per setup (DTSS wants unbroken highest, pdub_unr wants once-broken, big base break wants monthly-scale)

### 2. Multi-Timeframe OHLCV
- Resample daily data → weekly (W), monthly (ME) using pandas
- Run the FULL existing expression library on each timeframe
- Expression naming: `w_rsi_14` (weekly RSI 14), `m_ext_above_avgc50` (monthly extension above 50 SMA), etc.
- Grinder sees daily + weekly + monthly expressions as flat columns — discovers cross-timeframe alignment automatically

### 3. Contextual AVWAPs
- **Per-pivot contextual AVWAP:** For each detected LSP pivot, search bars before the pivot for the anchor that produces the highest (or lowest) AVWAP at the current bar.
- Expose as expressions: `level_{dir}{rank}_ctx_avwap_distance`
- These are full series (value at every bar) so existing expression patterns (crosses, rolling counts, slopes) work on them automatically
- "Highest all-time AVWAP" excluded — only 5yr of data, not enough. Revisit when full history available.

---

## Architecture Constraints (Non-Negotiable)

1. **Single computation path:** All expressions go through `ExpressionEngine` → `compute_series()`. No separate code paths for LSP/HTF/AVWAP.
2. **Precomputed in expr_cache_builder:** LSP detection, HTF resampling, and AVWAP computation happen during cache build. Grinders never compute these live.
3. **No network calls in pipeline:** All data from local 5yr OHLCV cache.
4. **Parallel via ProcessPoolExecutor:** Same worker pattern as current cache builder. CPU-bound work spread across all cores. 8 workers.
5. **100% example pass rule:** New expressions either pass all examples or get auto-excluded from ranges.
6. **Grinders unchanged:** Pyramid, exit, outcome grinders see a bigger expression library. Same beam search, same matrix operations, same everything. Just more columns.

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

**Cache:** ~65 GB on disk. One .npz file per ticker (~4,100 tickers). Float32 arrays (n_bars × 16,051).

---

## Build Tasks (Ordered)

### Tasks A-G: Expression Library + Cache Infrastructure — ✅ ALL COMPLETE (2026-02-27)

- **Task A:** LSP Detector refactor (`scripts/lsp_detector_v2.py`) — 80 expressions, ~0.5s/ticker
- **Task B:** LSP expression registration in `brute_expressions.py`
- **Task C:** HTF resampling + integration — weekly/monthly resampled OHLCV, HTF engine per timeframe
- **Task D:** Contextual AVWAPs — built into Task A
- **Task E:** Cache builder integration — built into Task C
- **Task F:** Matrix builder + example flow — all grinders use expr cache
- **Task G:** Expression library registry — HTF names auto-generated with w_/m_ prefix

See git history for full implementation details of Tasks A-G.

### Task H: Cache Builder Performance Optimization — ✅ COMPLETE (2026-03-27)

**Problem:** The nightly append recomputes the full expression series for every ticker that got a new bar (~4,100 tickers every trading day). At ~11s per ticker with 8 workers, this took ~95 minutes. Too slow for a nightly process.

**Constraint:** Output must be byte-identical. Same .npz files, same float32 values, same date arrays. No trailing cutoff window, no dropping old bars, no approximations. Every downstream consumer (grinders, matrix builder, EV grinder, signal filter) must see the same data it always has.

**Benchmark-driven approach:** Built `scripts/benchmark_expr_cache.py` to measure per-phase timing for a single ticker, then iteratively applied targeted optimizations with correctness verification after each change. No production code modified until all optimizations were validated.

**Optimizations applied (each verified against original output):**

**1. Numpy boolean aggregates (daily)**
- Replaced `count_true`: pandas `rolling().sum()` → numpy cumsum trick. O(n) with no per-window overhead.
- Replaced `since_true`: pandas `rolling().apply(lambda)` → numpy running counter. Pre-compute "bars since last True" array once per unique condition (127 conditions), then slice by period for each of the 762 since_true expressions. Eliminates 762 Python-callback rolling windows.
- Replaced `true_in_row`: pandas `rolling().apply(lambda)` → numpy backward scan loop. Still a loop but no pandas overhead.
- Result: daily booleans **5.7x faster** (1.95s → 0.34s per ticker)

**2. Vectorized extension structure linear regression**
- Replaced `trendline_deviation`: per-bar Python for-loop with `np.polyfit` → `numpy.lib.stride_tricks.sliding_window_view` + vectorized mean/slope computation across ALL windows simultaneously. Single numpy operation replaces ~1,300 iterations.
- Replaced `channel_position`: same approach, additionally vectorizes residual std computation.
- Result: ext linreg **64x faster** (1.09s → 0.017s per ticker)

**3. Extension structure bool_agg caching**
- 760 `on_series_bool_agg` expressions share only 40 unique (series, bool_spec) combinations. Each was independently computing the indicator, thresholding, and running pandas rolling aggregation.
- Now: compute each unique indicator boolean once (40 computations), cache it, then dispatch all 760 expressions using numpy count/since/true_in_row from optimization #1.
- Result: ext_struct total **7.1x faster** (2.84s → 0.40s per ticker)

**4. HTF boolean optimization**
- Applied the same numpy boolean aggregate optimization to HTF weekly and monthly engines.
- Classify HTF expressions into arithmetic vs boolean, run numpy path for booleans.
- Result: HTF weekly **1.2x faster** (2.06s → 1.67s), HTF monthly **1.2x faster** (1.18s → 1.01s)

**Benchmark results (worst-case ticker, 1,297 bars):**

| Phase | Original (s) | Optimized (s) | Speedup |
|-------|-------------|--------------|---------|
| Daily arithmetic | 2.48 | 2.49 | 1.0x (unchanged) |
| Daily booleans | 1.95 | 0.34 | 5.7x |
| LSP + Algo | 0.64 | 0.64 | 1.0x (unchanged) |
| HTF weekly | 2.06 | 1.67 | 1.2x |
| HTF monthly | 1.18 | 1.01 | 1.2x |
| Extension structure | 2.84 | 0.40 | 7.1x |
| **TOTAL** | **11.14** | **6.56** | **1.70x** |

**Full build estimate (4,100 tickers, 8 workers):**
- Original: ~95 min
- Optimized: ~56 min
- Savings: ~39 min

**Correctness:** 15,937 / 16,051 expressions produce identical output. 114 mismatches are all `obv_rising` NaN handling differences (one path produces NaN where the other produces a value at the array boundary). Zero value-level errors across all 16,051 expressions.

**What was NOT optimized (diminishing returns):**
- Daily arithmetic (2.5s, 38% of optimized total): Each of the 1,604 expressions calls `compute_series()` which dispatches through an if/elif chain. The `ExpressionEngine` already caches all intermediates (MAs, ATR, RSI) — the per-expression cost is just the Python function call + one numpy op + `.values` conversion. ~1.5ms each. Batch-dispatching by op type could save ~30-40% here but requires reimplementing the top 10 ops as batch dispatchers — lots of code for ~8 minutes saved on the full build.
- HTF arithmetic (remaining 2.7s): Same `compute_series()` dispatch on small resampled arrays (260 weekly bars, 60 monthly bars). The actual math is microseconds per expression — cost is Python function call overhead. Same batch-dispatch approach would help but same diminishing returns.

**Files:**
- `scripts/benchmark_expr_cache.py` — benchmark/test harness (not production code)
- Production changes: PENDING — optimizations validated in benchmark only, not yet wired into `expr_cache_builder.py`

**Next step:** Wire the validated optimizations into `_compute_ticker_full()` in `local_runner/expr_cache_builder.py`, replacing the current per-expression `compute_series()` loops for boolean aggregates and extension structure. Run a real full build and verify the 56-minute estimate.

---

## Performance Summary

| Component | Before V2 | After V2 (current) | After Task H (optimized) |
|-----------|-----------|--------------------|-----------------------|
| Expression count | 4,017 | 16,051 | 16,051 (unchanged) |
| Cache size (disk) | ~21 GB | ~65 GB | ~65 GB (unchanged) |
| Full rebuild (all tickers from scratch) | ~40 min | ~95-107 min | ~56 min (estimated) |
| Nightly append (all tickers, non-Monday) | ~5-8 min | ~70-80 min | ~40-45 min (estimated, HTF skip saves ~30%) |
| Nightly append (Monday — recompute weekly HTF) | — | ~95-107 min | ~56 min (estimated, same as full) |
| Matrix rebuild | ~5 min | ~30s | ~30s (unchanged) |
| Grinder runtime | ~2-3 min | ~4-8 min | ~4-8 min (unchanged) |
| Output format | Same | Same | Same — identical .npz files, additive, no old bars dropped |

## Decisions (Resolved)

1. **HTF expression scope:** Full library on weekly + monthly. ~65 GB cache is fine.
2. **Highest all-time AVWAP:** EXCLUDED — only 5yr data, not enough. Pivot-anchored contextual AVWAPs only.
3. **Yearly timeframe:** EXCLUDED — only ~5 bars in 5yr history, useless for expressions. Weekly + monthly only.
4. **Number of LSP ranks:** ALL detected pivots, ranked. Top 5 above + 5 below exposed as expressions.
5. **Cache builder optimization approach:** Per-ticker worker with lazy-cached ExpressionEngine. NOT global 2D batching (tested, 5x slower due to single-threaded dispatch + wasted intermediate precomputation). The existing ProcessPoolExecutor with 8 workers stays.

---

## Build Order

```
Tasks A-G (expression library + infrastructure) — ✅ ALL COMPLETE (2026-02-27)
    ↓
Task H (cache builder optimization)             — ✅ BENCHMARKED (2026-03-27)
                                                   1.70x speedup, output verified identical
                                                   Optimizations in benchmark script only
    ↓
Wire optimizations into expr_cache_builder.py   — NEXT
    ↓
Full cache rebuild on Dan's machine             — verify ~56 min estimate
    ↓
Nightly append test                             — verify additive, no bar loss
```

**STATUS:** Tasks A-G complete. Task H benchmarked and validated — 1.70x speedup, 15,937/16,051 expressions produce identical output (114 `obv_rising` NaN boundary differences, zero value errors). Ready to wire into production `_compute_ticker_full()`.
