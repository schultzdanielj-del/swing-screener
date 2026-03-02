# Expression Engine V2 — Build Plan

## What's Being Added

Three new TA capabilities, all precomputed into the expression cache so grinders see them as regular expressions:

### 1. LSP Detection (Left Side Pivots)
- Find all pivot highs and pivot lows across multiple window sizes (5, 10, 15, 20, 30, 40)
- For each pivot: track price, bars back, break count (how many times subsequent bars exceeded it)
- Return top N pivots ranked by prominence
- Expose per-pivot expressions: `lsp1_distance`, `lsp1_break_count`, `lsp1_bars_back`, `lsp1_avwap_distance`, `lsp2_distance`, etc.
- The grinder discovers which pivot characteristics matter per setup (DTSS wants unbroken highest, pdub_unr wants once-broken, big base break wants monthly-scale)

### 2. Multi-Timeframe OHLCV
- Resample daily data → weekly (W), monthly (ME), yearly (YE) using pandas
- Run the FULL existing expression library on each timeframe
- Expression naming: `w_rsi_14` (weekly RSI 14), `m_ext_above_avgc50` (monthly extension above 50 SMA), etc.
- Grinder sees daily + weekly + monthly + yearly expressions as flat columns — discovers cross-timeframe alignment automatically

### 3. Contextual AVWAPs
- **Highest AVWAP of all time:** For each bar, brute-search all prior bars as anchor points, find which anchor produces the highest AVWAP value at the current bar. Also lowest.
- **Per-pivot contextual AVWAP:** For each detected LSP pivot, search bars before the pivot for the anchor that produces the highest (or lowest) AVWAP at the current bar.
- Expose as expressions: `highest_avwap_distance`, `lowest_avwap_distance`, `lsp1_ctx_avwap_distance`, etc.
- These are full series (value at every bar) so existing expression patterns (crosses, rolling counts, slopes) work on them automatically

---

## Architecture Constraints (Non-Negotiable)

1. **Single computation path:** All expressions go through `ExpressionEngine` → `compute_series()`. No separate code paths for LSP/HTF/AVWAP.
2. **Precomputed in expr_cache_builder:** LSP detection, HTF resampling, and AVWAP computation happen during cache build. Grinders never compute these live.
3. **No network calls in pipeline:** All data from local 5yr OHLCV cache. LSP detector refactored to accept DataFrame, not fetch from API.
4. **Parallel via ProcessPoolExecutor:** Same worker pattern as current cache builder. CPU-bound work spread across all cores.
5. **100% example pass rule:** New expressions either pass all examples or get auto-excluded from ranges. Existing grinder logic handles this — no grinder code changes.
6. **Grinders unchanged:** Pyramid, exit, outcome grinders see a bigger expression library. Same beam search, same matrix operations, same everything. Just more columns.

---

## Build Tasks (Ordered)

### Task A: LSP Detector Refactor — ✅ COMPLETE (2026-02-27)
**File:** `scripts/lsp_detector_v2.py` (1,004 lines, new file — old `lsp_detector.py` preserved)

**What was built:**
```python
class LSPDetectorV2:
    def __init__(self, daily_df, weekly_df, monthly_df):
        # Detects ALL pivot highs + lows across all 3 timeframes
        # Precomputes cumulative break count arrays per pivot
        # No API calls — pure DataFrame/numpy
    
    def get_levels_at_bar(self, bar_idx, n_above=5, n_below=5) -> dict:
        # Clusters pivots within 1 ATR into unified levels
        # Returns proximity-ordered: nearest above/below first
        # Each level: center_price, pivot_count, timeframe_count, break_count,
        #             max_window, bars_back_nearest, volume_ratio, distance
    
    def compute_all_series(self, cum_tpv, cum_v, n_above=5, n_below=5) -> dict:
        # Single-pass bar-by-bar computation of ALL 80 expressions
        # Returns dict of expression_name → float64 array(n_bars)

# Top-level entry point for cache builder:
def compute_all_lsp_series(daily_df, weekly_df=None, monthly_df=None) -> dict:
    # Resample if needed, detect pivots, compute all 80 series
    # ~0.5s per ticker for 1,260 bars

def get_lsp_expression_names() -> list[str]:
    # Ordered list of all 80 expression names for cache column registration
```

**80 expressions produced:**
- 7 metrics × 5 ranks × 2 directions = 70 level expressions
- 1 ctx_avwap_distance × 5 ranks × 2 directions = 10 AVWAP expressions
- Naming: `level_{above|below}{1-5}_{metric}`

**Key optimizations:**
- Precomputed cumulative break arrays → O(1) break count at any bar
- Single-pass `compute_all_series()` eliminates redundant `get_levels_at_bar()` calls
- Vectorized AVWAP computation (numpy batch vs Python loop over 25 anchors)
- HTF pivot indices mapped to daily bar indices via date lookup

**Performance:** ~0.5s/ticker (1,260 bars), ~4 min for 4,100 tickers on 8 cores

**Validation:** CLI mode `python scripts/lsp_detector_v2.py validate` runs against labeled DTSS data.
Needs validation on Dan's machine against real 5yr cache (synthetic data tested in sandbox).

**Design constraints met:**
- ✅ No API/network calls — pure DataFrame/numpy
- ✅ Precomputed in cache builder — grinders never compute live
- ✅ ProcessPoolExecutor compatible — stateless function, no shared state
- ✅ Output is flat expression columns — grinders see them identically to existing 4,017 daily
- ✅ 100% example pass rule — enforced by existing range computation (NaN auto-excluded)

### Task B: LSP Expressions in Expression Engine — ✅ COMPLETE (2026-02-27)
**File:** `local_runner/brute_expressions.py` (44 lines added)

**What was done:**
- Imported `get_lsp_expression_names()` from `scripts/lsp_detector_v2.py` (with fallback if import fails)
- Registered all 80 LSP expressions with `category: "lsp"` and `compute: {"op": "precomputed", "source": "lsp", "column": "<name>"}`
- The `op: "precomputed"` marker tells the cache builder (Task E) to grab these from the LSP precompute dict rather than running through `compute_series()`
- Updated compute estimate to separate precomputed expressions from arithmetic/boolean
- Total expression count: 4,017 → 4,097 (+80 LSP) → 4,141 (+44 algo lines)
- Names 100% match `get_lsp_expression_names()` — verified programmatically

**Compute spec pattern:**
```json
{"op": "precomputed", "source": "lsp", "column": "level_above1_distance"}
```

The cache builder worker (Task E) will:
1. Call `compute_all_lsp_series(daily_df)` → dict of 80 series
2. For each expression with `op == "precomputed"` and `source == "lsp"`: grab `column` from that dict
3. Write to the appropriate column index in the output array

### Task C: Higher Timeframe Resampling — ✅ COMPLETE (2026-02-27)
**Files modified:** `local_runner/expr_cache_builder.py`, `local_runner/brute_expressions.py`

**What was built:**

1. **HTF expression registration in `brute_expressions.py`:**
   - After generating all 4,017 daily expressions (arithmetic + boolean), generates `w_` and `m_` copies of every one
   - Each HTF expression has `op: "precomputed", source: "htf", timeframe: "w"/"m"`, plus `base_compute` carrying the original daily compute spec
   - Total: 4,017 weekly + 4,017 monthly = 8,034 new HTF expressions
   - Grand total: 12,175 expressions (4,017 daily + 80 LSP + 44 algo + 4,017 weekly + 4,017 monthly)

2. **HTF helpers in `expr_cache_builder.py`:**
   ```python
   def resample_ohlcv(daily_df, freq='W'):
       # Resamples daily→weekly/monthly using pandas resample
       # Returns None if too few bars (<10 daily or <5 resampled)
   
   def build_htf_to_daily_map(daily_dates, htf_df, freq):
       # Maps each daily bar index → HTF bar index via searchsorted
       # Returns int32 array; -1 for unmapped bars
   
   def map_htf_series_to_daily(htf_series, htf_to_daily_map):
       # Applies the mapping: step function from HTF values to daily grid
   ```

3. **Updated `_init_worker()`:**
   - Pre-classifies all 12,175 expressions into 5 buckets at startup (once per worker process):
     - `_w_daily_indices` (4,017) — computed via `compute_series()` on daily engine
     - `_w_lsp_indices` (80) — computed via `compute_all_lsp_series()`
     - `_w_algo_indices` (44) — computed via `compute_all_algo_series()` (daily-only)
     - `_w_htf_weekly_indices` (4,017) + `_w_htf_weekly_base` — computed on weekly engine
     - `_w_htf_monthly_indices` (4,017) + `_w_htf_monthly_base` — computed on monthly engine

4. **Updated `_compute_ticker_full()` — 4-phase computation:**
   - Phase 1: Daily expressions via `compute_series(engine, spec)` (same as before)
   - Phase 2: LSP expressions via `compute_all_lsp_series(df)` → dict lookup
   - Phase 2b: Algo line expressions via `compute_all_algo_series(df)` → dict lookup (daily-only)
   - Phase 3: For each HTF timeframe: resample → build mapping → create HTF engine → run `compute_series()` with `base_compute` spec → map back to daily

5. **Updated `_append_ticker()`:** Same 3-phase structure for nightly appends.

**Test results (synthetic 1,260-bar ticker):**
- Output shape: (1,260, 12,175) ✅
- Time per ticker: ~8.5s (vs ~3-4s previously)
- Daily NaN: 3.9%, Weekly NaN: 12.5%, Monthly NaN: 37.5% (warmup expected)

**Performance estimates (4,167 tickers, 7 cores):**
- Full cache build: ~84 min (vs ~40 min previously)
- Cache size: ~70 GB (vs ~21 GB previously)

**Design constraints met:**
- ✅ Same `compute_series()` path for HTF expressions — just different ExpressionEngine instance
- ✅ Precomputed in cache builder — grinders never see HTF computation
- ✅ ProcessPoolExecutor compatible — all state in worker globals, initialized once
- ✅ Step function mapping verified — daily bars within same week/month get identical values
- ✅ Grinders unchanged — they see 12,175 flat columns, search works identically

### Task D: Contextual AVWAP Computation
**What:** Precompute pivot-anchored contextual AVWAP series per level.

**Per-Level Contextual AVWAP:**
For each clustered level, take the most prominent pivot in the cluster. Search the ~20-30 bars before that pivot for the anchor bar that produces the highest AVWAP at the current bar. Also find the anchor that produces the lowest. This captures the "average buyer/seller cost basis" relative to each structural level.

**Optimization:** Precompute cumulative TP×V and cumulative V arrays once per ticker. AVWAP from any anchor to any bar is then just `(cum_tpv[bar] - cum_tpv[anchor-1]) / (cum_v[bar] - cum_v[anchor-1])`. Two array lookups and a division. For ~20-30 candidate anchors per level × ~10 levels per bar, this is very fast.

**Both directions:**
- Highest contextual AVWAP per level (sellers' break-even — relevant for longs)
- Lowest contextual AVWAP per level (buyers' break-even — relevant for shorts)

**Expressions (already included in Task B's per-level list):**
- `level_{dir}{rank}_ctx_avwap_distance` — close vs contextual AVWAP for this level

**Note:** "Highest all-time AVWAP" excluded — only 5yr of data, not enough. Revisit when full history is available.

### Task E: Integration into Cache Builder — ✅ COMPLETE (2026-02-27, built as part of Task C)
The cache builder integration was done directly as part of Task C rather than as a separate step.
Both `_compute_ticker_full()` and `_append_ticker()` now handle all 3 expression types (daily + LSP + HTF).
See Task C implementation details above.

### Task F: Matrix Builder Update — ✅ COMPLETE (2026-02-27)
**Files modified:** `local_runner/matrix_builder.py`, `local_runner/pyramid_grinder.py`

**Problem solved:** The matrix builder and example range computation were computing expressions **live** via `ExpressionEngine.compute()` and `compute_series()`. These functions don't know about `op: "precomputed"` expressions (LSP, algo, weekly, monthly), causing 8,158 of 12,175 expressions to silently return NaN. The pyramid grinder's validation also used live computation, creating an inconsistent path.

**What was done:**

1. **`matrix_builder.py` — rewritten `get_universe_matrix()`:**
   - Primary path now loads ALL expression values from the expr series cache (last bar per ticker)
   - Uses `ProcessPoolExecutor` for parallel file I/O across all cores
   - ~30s to load 4,167 tickers from cache vs ~30 min live computation
   - Falls back to live computation only if no expr cache exists (with warning that LSP/HTF will be NaN)

2. **`pyramid_grinder.py` — rewritten `compute_example_ranges()`:**
   - Now accepts `expr_cache` parameter
   - When cache available: loads scan_idx row from each example's cached .npz file
   - All 12,175 expressions get valid values, so all can participate as grinder candidates
   - Falls back to `compute_series()` if no cache

3. **`pyramid_grinder.py` — rewritten `validate_examples()`:**
   - Now accepts `expr_cache` parameter
   - Uses same cached data path as range computation and historical tiers
   - Consistent values across all grinder stages

4. **`pyramid_grinder.py` — rewritten final validation block:**
   - Same cache-based lookup for the save/no-save gate
   - Ensures 100% example pass rule uses identical computation path

5. **`run_pyramid()` orchestrator:**
   - Moved `ExprSeriesCache` detection **before** `compute_example_ranges()` (was after)
   - Passes `expr_cache` to all range computation and validation calls

**Design constraints met:**
- ✅ All grinders use the exact same computation path (expr cache files)
- ✅ All grinders optimized for max speed (parallel file I/O, no live computation)
- ✅ All grinders use all CPU cores (ProcessPoolExecutor)
- ✅ All conditional results pass all setup examples (validated via same cache path)
- ✅ LSP, HTF, and daily expressions all treated identically
- ✅ Fallback to live computation preserved for bootstrapping (before first cache build)

### Task G: Expression Library Update — ✅ COMPLETE (2026-02-27, built as part of Task C)
HTF expression names are auto-generated programmatically in `brute_expressions.py` by prefixing all daily expressions with `w_`/`m_`. No manual curation needed — the grinder discovers which HTF expressions matter per setup. LSP expressions were already registered in Task B.

---

## Performance Estimates (Updated with Measured Values)

| Component | Previous | After V2 (Measured) |
|-----------|----------|---------------------|
| Expression count | 4,017 daily | 12,175 (4,017 daily + 80 LSP + 44 algo + 4,017 weekly + 4,017 monthly) |
| Cache size (disk) | ~21 GB | ~255 GB (61 MB/ticker × 4,167 tickers, float32) |
| Full cache build | ~40 min | ~84 min estimated (8.5s/ticker × 4,167 tickers ÷ 7 cores) |
| Nightly append | ~5-8 min | ~15-20 min (same ratio increase) |
| Matrix rebuild | ~5 min | ~30s (reads from expr cache, no live computation) |
| Grinder runtime | ~2-3 min | ~4-8 min (3x more expressions to search) |

## Decisions (Resolved)

1. **HTF expression scope:** Full 4,017 on weekly + monthly. ~255 GB cache is fine (548 GB free).
2. **Highest all-time AVWAP:** EXCLUDED — only 5yr data, not enough for "all time." Pivot-anchored contextual AVWAPs only. Revisit when full history available (~1 TB cache).
3. **Yearly timeframe:** EXCLUDED — only ~5 bars in 5yr history, useless for expressions. Weekly + monthly only.
4. **Number of LSP ranks:** ALL detected pivots, ranked. Expression engine exposes top N as ranked expressions.
5. **LSP on HTF:** Yes — run pivot detection natively on weekly + monthly resampled data. Deduplicate overlaps (daily pivot at same price as weekly pivot = keep weekly version).

---

## Build Order

```
Task A (LSP detector refactor)          — ✅ COMPLETE (2026-02-27)
    ↓
Task B (LSP level expressions)          — ✅ COMPLETE (2026-02-27)
    ↓
Task C (HTF resampling + integration)   — ✅ COMPLETE (2026-02-27): 8,034 HTF expressions + cache builder updated
    ↓
Task D (Contextual AVWAPs)              — ✅ BUILT INTO Task A
    ↓
Task E (Cache builder integration)      — ✅ BUILT INTO Task C
    ↓
Task F (Matrix builder + example flow)  — ✅ COMPLETE (2026-02-27): all grinders use expr cache
    ↓
Task G (Expression library registry)    — ✅ BUILT INTO Task C
    ↓
Full cache rebuild + validation         — NEXT: run on Dan's machine
    ↓
Re-grind DTSS with expanded library
```

### What's Left

**Cache rebuilt (2026-03-02):** Expression cache now includes generic exit expressions (27 new ops in backtest_conditions.py). `_load_expressions()` merges signal + exit libraries, deduplicates by name. Cache built successfully overnight.

**Task 4 (Signal Filter) — blocked items resolved, needs re-run:**
- Signal filter uses expression cache as single computation path (no compute_series, no ExpressionEngine)
- Loads correct grinder result: `pyramid_dtss_mp_sig264_pk3_20260228_163923.json` (264 signals, 41 conditions)
- 20/23 examples pass all conditions (3 skipped: BRK-B, SMMT, VUZI not in 5yr cache)
- Exit expression `avg_range_atr_10b` should now be in rebuilt cache
- **Next:** restart agent, trigger step 4 from pipeline dashboard, verify exit phase works

**Changes made 2026-03-02 session:**
- `scripts/backtest_conditions.py`: 88 → 115 ops (added 27 generic exit ops from exit_compute.py: avg_bar_range_rolling, avg_body_ratio_rolling, consecutive_green/red, distance_from_ma, vol ratios, ext_accel, gap_from_prior, etc.)
- `local_runner/expr_cache_builder.py`: `_load_expressions()` merges generic exit expressions (filters out entry-relative and context-dependent ops that can't be precomputed)
- `scripts/signal_filter.py`: Complete rewrite to use expression cache as single computation path. Loads pyramid results from `local_runner/cache/` (picks latest by mtime). No fallbacks — missing expressions cause clear error with rebuild command.
- `local_runner/agent.py`: Now polls both grinder job queue AND pipeline job queue
- Suppressed All-NaN slice warnings in backtest_conditions.py (ext_ceiling_ratio warmup bars)

