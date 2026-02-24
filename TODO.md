# TODO

## Current State (as of 2026-02-24)

### The Grinder — Two-Phase Desktop Expression Discovery Engine

**Phase 1: Spiderweb Search (single-day ceiling)**
- Desktop agent (`local_runner/agent.py`) polls Railway for jobs
- Universe matrix (~4,017 tickers × 4,017 expressions) auto-rebuilds nightly at 4:30pm ET
- Spiderweb beam search explores branching condition combinations
- Finds mathematical ceiling on current day's snapshot (e.g., 0.07% = 3 tickers)
- Frontend slider controls grind depth (5 levels: 30s → 8 hours)

**Phase 2: Pyramidal Grinder (6-tier nested historical noise elimination)**
- After Phase 1 ceiling, pyramid progressively widens time horizon: D1 → 1wk → 1mo → 6mo → 1yr → 5yr
- Each tier grinds until peak_signals/day < threshold before advancing
- 5-year OHLCV cache (4,167 tickers × avg 1,108 bars, 214 MB) provides the history
- Constraint: 100% of setup examples ALWAYS pass all conditions

**Nightly Pipeline: ✅ COMPLETE** — `python local_runner/nightly.py` chains 5 steps: Railway incremental append → daily cache → 5yr cache → expression cache append → matrix rebuild. Auto-triggered by agent at 4:30pm ET on trading days. ~15-20 min if new data, <1 min if already current.

**compute_series parity: ✅ COMPLETE** — All 88 ops available in backtest_conditions.compute_series(). Historical tiers (T2-T6) have full access to the 4,017 expression library.

**Expression library: ✅ EXPANDED** — 2,541 → 4,017 expressions. 127 boolean conditions (was 65), 88 ops (was 82). 29 categories.

**Universal design:** Upload examples for any setup type → grinder handles the rest. Universe matrix is shared, only example matrix + grind is per-setup.

---

## Architecture

```
local_runner/
├── agent.py              # Polling agent with nightly auto-rebuild (4:30pm ET trigger)
├── nightly.py            # Single-command nightly pipeline (5 steps, gate logic)
├── grinder.py            # CLI interface
├── pyramid_grinder.py    # Pyramidal grinder: 6 nested tiers (D1→5yr), peak-based
├── spiderweb.py          # Phase 1: beam search tree exploration (used by D1 tier)
├── historical_scorer.py  # Phase 2: greedy historical signal elimination (legacy, replaced by pyramid)
├── matrix_builder.py     # Precomputes universe + example matrices
├── cache_builder.py      # OHLCV caches (300-bar daily + 1,260-bar 5yr)
├── expr_cache_builder.py # Pre-cached expression series for all tickers × 5yr
├── brute_expressions.py  # 4,017 expression generator (generic, universal)
└── cache/
    ├── universe_ohlcv.pkl      # Daily cache (~57 MB, 300 bars/ticker)
    ├── universe_ohlcv_5yr.pkl  # 5yr cache (~214 MB, avg 1,108 bars/ticker)
    ├── expr_series/             # Pre-cached expression series (~52 GB compressed)
    ├── grinder_results_{setup}.json     # Phase 1 output
    └── historical_results_{setup}.json  # Phase 2 output

scripts/
├── expression_engine.py     # Computes expressions against OHLCV (point + series)
├── backtest_conditions.py   # Series computation for historical scoring
├── fetch_universe.py        # Universe OHLCV fetcher: full build + incremental append_daily()
├── lsp_detector.py          # Local Structural Peak detection (DTSS-specific)
├── classify_universe.py     # ETF classifier (quarterly, desktop-only)
└── fast_profiler.py         # FastProfiler for rapid example profiling

server.py                    # Railway API: 14+ endpoints, universe rebuild, grinder jobs, nightly append
```

### Key data stores
- **Railway SQLite DB:** 11M+ rows OHLCV across ~4,167 tickers, examples, scan results, classifications
- **Local 5yr cache:** `universe_ohlcv_5yr.pkl` — 4,167 tickers × avg 1,108 bars (214 MB)
- **Local daily cache:** `universe_ohlcv.pkl` — 4,167 tickers × avg 292 bars (57 MB)
- **Tradable universe:** ~4,017 tickers after ETF exclusions (150 leveraged/inverse/derivative excluded)

### Expression library: 4,017 expressions across 29 categories
- near_resistance (203), near_support (133), extension (98), extension_dynamics (91)
- extension_ceiling (40), extension_adr (6), ma_slope (240), ma_spread (46)
- spread_slope (64), slope_ratio (18), ma_cross (72), ma_stack (7)
- momentum (138), range (59), range_dynamics (13), retracement (26)
- swing_structure (48), gap (21), consecutive (4), candle_pattern (39)
- volume_character (49), volume_continuous (36), bollinger (25), macd (28)
- aroon (18), efficiency (9), vwap (36), percentile_rank (37)
- boolean (2,413 from 127 conditions × 19 aggregations each)

---

## DTSS Pipeline Status

| Step | Status | Notes |
|------|--------|-------|
| 1 Load | ✅ Done | Data + TA knowledge loaded |
| 2 Receive | ✅ Done | 26 examples with LSP data |
| 3 Grind (Phase 1) | ✅ Done | 9 conditions, 0.00% single-day pass rate, 11s at L3 |
| 4 Grind (Phase 2) | ✅ Done | 12 conditions (9 P1 + 3 P2), avg 7.4 signals/day, 20.6 min |
| 4.7 Signal analysis | ✅ Done | Peak: 260/day (2021-08-11), clustered Jul-Aug 2021. Avg hides massive spikes. |
| 5 **Pyramid grinder** | **✅ Tested** | Run 1 (2,541 exprs): 10 conditions, peak 69/day, avg 7.2/day, ~10 min. Run 2 (4,017 exprs): 20 conditions, peak 91/day, avg 9.7/day, ~40 min. More conditions locked but worse results + 4x slower due to 127 booleans. |
| 5a **compute_series parity** | **✅ Done** | 82 → 88 total ops. Full parity with expression_engine (excluding 8 LSP ops). |
| 5b **Expression expansion** | **✅ Done** | 2,541 → 4,017 expressions. 65 → 127 booleans, 6 new ops. |
| 5c **Expression series cache** | **✅ Built** | `local_runner/expr_cache_builder.py` — pre-caches all 4,017 expression series for all tickers × 5yr. Build once (~40 min), nightly append (~5-8 min). Pyramid grinder auto-detects and uses cache. Estimated ~52 GB on disk (float32, compressed). |
| 6 Backtest | Not started | Visual verification of signal charts |
| 7 Market Context | Not started | |
| 8 EV Optimize | Not started | |

---

## BUILD PLAN — Completed Steps

### Step 1: 5-Year OHLCV Cache ✅ COMPLETE
- `python local_runner/cache_builder.py --5yr --force`
- 4,167 tickers × 1,260 bars max, 214 MB, avg 1,108 bars/ticker
- No bar minimum filter — short-history IPOs included (min 10 bars)
- 7-day freshness (vs 24h for daily cache)

### Step 2: Clean Tradable Universe ✅ COMPLETE
- `scripts/classify_universe.py` classifies all tickers via yfinance
- 150 excluded: leveraged, inverse, single-stock, derivative income, volatility ETFs
- `server.py` rebuild filters via `universe_exclusions` table
- Run quarterly with `--force` to catch new tickers

### Step 3: Expression Library Expansion ✅ COMPLETE
- 2,271 → 2,541 expressions (270 new)
- 11 new compute ops in both `expression_engine.py` and `backtest_conditions.py`:
  - `distance_to_minl` / `ratio_c_minl`: support proximity
  - `percentile_rank`: normalize any metric to 0-100 vs history
  - `spread_slope`: is MA spread widening or narrowing
  - `slope_ratio`: fast MA slope / slow MA slope
  - `rvol_continuous` / `cumulative_rvol`: rolling relative volume
  - `retrace_high` / `retrace_low`: separate H/L retracement
  - `vwap_slope`: rolling VWAP direction

### Step 4.5: Strip Bespoke System ✅ COMPLETE
- Removed `generate_dtss_lsp_expressions()` and `generate_dtss()` from `brute_expressions.py`
- Removed DTSS branch in `_load_expressions()` — always uses `generate_all()` now
- Removed `lsp_context` parameter from `_compute_ticker_values()`
- Removed LSP detector injection from `get_example_matrix()`
- Deleted `get_bespoke_candidate_matrix()` function entirely
- Removed Phase 2 bespoke re-filter block from `agent.py`
- Removed matrix alignment code from `agent.py` (matrices always match now)
- `expression_engine.py` LSP ops kept (harmless, nothing calls them)
- Example matrix now returns `expr_names` and `expr_categories` for consistency

### Step 4: Phase 2 — Historical Scorer ✅ COMPLETE
- `python local_runner/historical_scorer.py --setup dtss --target 10`
- Greedy forward selection with precomputed numpy masks
- Requires: Phase 1 grinder results + 5yr OHLCV cache
- Output: `local_runner/cache/historical_results_{setup}.json`

### Step 4.6: Phase 1→2 Integration Fixes ✅ COMPLETE
- **Compute spec enrichment:** Phase 1 saves `{expr, category, low, high}` but Phase 2 needs `{compute}`. `load_phase1_results()` now rebuilds compute specs from expression library.
- **Missing backtest ops:** Added `bars_since_ma_cross` and `gap_count` to `backtest_conditions.py` (existed in expression_engine but not in series computation).
- **Key name mismatch:** Phase 1 uses `"expr"`, Phase 2 expected `"name"`. Fixed with `.get()` fallbacks.
- **Parallelization:** Both `compute_base_signals` and candidate precompute switched from `ThreadPoolExecutor` to `ProcessPoolExecutor` for true CPU parallelism (GIL was limiting threads to 10% CPU usage).

### Nightly Automation ✅ COMPLETE
**Railway endpoint:** `POST /api/universe/append-daily` — checks DB max date vs yfinance latest trading day (uses SPY as reference). If behind, fetches missing days for all tradable tickers (batch 40, 3s delay, INSERT OR REPLACE). Rebuilds tradable_universe after successful append. Returns stats.
**Desktop orchestrator:** `python local_runner/nightly.py` — 5-step chain with gate logic:
1. Railway append-daily (30-min timeout) — if already current, stops here
2. Daily OHLCV cache refresh (300 bars)
3. 5yr OHLCV cache refresh (1,260 bars)
4. Expression series cache append (new bars only)
5. D1 universe matrix rebuild
**Agent integration:** `local_runner/agent.py` triggers at 4:30pm ET on weekdays (30-min buffer after close for data availability).
**Total time:** ~15-20 min if new data, <1 min if already current.

---

## IMMEDIATE NEXT STEP: Run Pyramid Grinder with Expression Cache

All infrastructure is built. Nightly pipeline is complete. Expression cache is built. Next grind run should be ~2-3 min instead of 40 min.

1. Run `python local_runner/nightly.py` to ensure all data is current
2. Run pyramid grinder with wider beam (100+) now that runtime is fast
3. Iterate: tune peak targets, try expression subsets, experiment with tier thresholds
4. Move to Step 5 (backtest visual verification) once peak/day is manageable

---

## BUILD PLAN — Remaining Steps

### Step 5a: compute_series Parity ✅ COMPLETE
**What:** Ported 39 missing ops from `expression_engine.py` to `backtest_conditions.compute_series()`.
**Result:** 82 total ops in compute_series (full parity with expression_engine, excluding 8 LSP ops). All 2,541 expressions now accessible to all pyramid tiers (T2-T6), not just D1. Validated 38/39 exact match.

### Step 5b: Expression Library Expansion ✅ COMPLETE
**What:** Expanded expression library from 2,541 → 4,017
**Result:** 127 boolean conditions (was 65), 88 ops (was 82), 6 new ops added to both expression_engine.py and backtest_conditions.py. New categories: close_position_in_bar, volume_price_divergence, low/high_vs_ma, roc_acceleration, roc_percentile_rank. Expanded parameter combos across extension, MA slope, Bollinger, MACD, range, retracement.
**Grind result:** 20 conditions locked (was 10), but peak 91/day and avg 9.7/day — worse than pre-expansion. Larger search space + same beam width = suboptimal path selection. Also 40 min runtime (was ~10 min) due to 127 boolean series computation.

### Step 5c: Expression Series Cache ✅ BUILT
**What:** Pre-cache all 4,017 expression series for all 4,167 tickers × 5yr on disk.
**Result:** `local_runner/expr_cache_builder.py` — standalone builder with --build (full, ~40 min first time), --append (nightly, ~5-8 min), --status. Stores compressed .npz per ticker in `local_runner/cache/expr_series/`. Manifest tracks expression fingerprint for auto-invalidation when library changes. `pyramid_grinder.py` modified to auto-detect and use cache — falls back to compute_series() for uncached tickers. Estimated ~52 GB disk usage (float32, np.savez_compressed).
**Impact:** Grind iterations: 40 min → ~2-3 min. Enables rapid experimentation with beam width, peak targets, expression subsets.

### Step 5: Backtest Runner Integration ⬜
**What:** Make `scripts/backtest_conditions.py` pull conditions from grinder results automatically. Output signal charts.
**Why:** Need to visually verify signals. Currently hardcoded from one grind run.
**How:**
- Read conditions from Phase 1 + Phase 2 result JSONs
- Generate candlestick charts for each signal (entry candle marked)
- Summary stats: signals/day distribution, worst day, ticker frequency

### Step 6: End-to-End Pipeline Test ⬜
**What:** Run full pipeline on DTSS: grind → historical score → backtest → review signals.
**Expected outcome:** Conditions that fire <10/day avg across 5 years, with clear DTSS setups.

### Step 7: Second Setup — 3-4DB ⬜
**What:** Run 3-4DB examples (21 already loaded) through the same pipeline.
**Why:** Validates the system is setup-agnostic.

---

## Future (not blocking current work)

| # | Task | Description |
|---|------|-------------|
| 1 | **Dynamic re-grind** | System re-grinds nightly and adjusts conditions as market evolves. |
| 2 | **HTF setup** | Third setup type. Collect and load examples. |
| 3 | **IPO break setup** | CRWV-style IPO breakout of highest AVWAP. Short history stocks. |
| 4 | **EV optimization** | Management optimizer against MFE/MAE matrices for position sizing. |
| 5 | **Market regime filter** | "When to trade it" on/off switch per setup. 3-4DB showed 6-7x signal spikes during stage transitions. |
| 6 | **Universal pivot expressions** | Detect all D1 pivots with prominence, generate expressions for spiderweb. Currently parked — LSP detector needs refactoring. |
| 7 | **Frontend Phase 2 control** | Run pyramid grinder from the frontend (like Phase 1 grind). Detailed progress per tier. |

---

## Performance — Desktop Benchmarks (i5-12600K)

| Component | Time | Notes |
|-----------|------|-------|
| Daily matrix build | ~5 min (est) | 4,017 tickers × 4,017 expressions, 8 workers |
| Spiderweb grind (L3) | ~11s | beam=50, depth=10, 550K nodes |
| Spiderweb grind (L5) | 1-8 hours | beam=250, depth=15 |
| **Pyramid grinder (2,541 exprs)** | **~12 min** | **10 conditions, peak 69/day, avg 7.2/day** |
| **Pyramid grinder (4,017 exprs)** | **~40 min** | **20 conditions, peak 91/day, avg 9.7/day. Boolean explosion (127 conditions) is the bottleneck.** |
| Pyramid 5yr matrix build | ~30+ min | Bottleneck. 2,413 boolean expressions dominate compute time. |
| Phase 2 legacy total | 20.6 min | Down from 28.2 min after ProcessPool switch (deprecated by pyramid) |
| Signal distribution | ~15s | 4,167 tickers × 12 conditions, parallel (ProcessPool) |
| Daily cache build | 4.4 min | 4,167 tickers × 300 bars |
| 5yr cache build | 4.6 min | 4,167 tickers × 1,260 bars |
| ETF classifier | ~10 min | Quarterly, yfinance API |
| **Nightly pipeline (full)** | **~15-20 min** | **append + daily cache + 5yr cache + expr cache + matrix** |
| **Nightly pipeline (current)** | **<1 min** | **Gate check only — Railway returns up_to_date** |

**Note:** Phase 2 uses ProcessPoolExecutor with cache-via-initializer pattern (serialized once per worker at startup, not per task). Batched ticker submission eliminates per-task DataFrame pickling overhead. 99% CPU utilization on i5-12600K.

---

## Key Optimizations Implemented

1. **`_bool_series` lazy eval** — if/elif dispatch instead of dict-literal. ~37% matrix build speedup.
2. **Matmul spiderweb** — float32 matmul replaces Python beam×candidate loop. 78x faster.
3. **Numpy array serialization** — raw numpy to workers, skip `pd.to_numeric`. 8x faster reconstruct.
4. **Example matrix parallelization** — ThreadPoolExecutor (10 threads) for concurrent example builds.
5. **No bar minimum in cache** — every tradable ticker included regardless of history length.
6. **ProcessPoolExecutor for Phase 2** — true CPU parallelism for base signal + candidate precompute (ThreadPool was GIL-bound at 10% CPU).
