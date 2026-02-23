# TODO

## Current State (as of 2026-02-23)

### The Grinder — Two-Phase Desktop Expression Discovery Engine

**Phase 1: Spiderweb Search (single-day ceiling)**
- Desktop agent (`local_runner/agent.py`) polls Railway for jobs
- Universe matrix (~4,017 tickers × 2,541 expressions) auto-rebuilds nightly at 4:30pm ET
- Spiderweb beam search explores branching condition combinations
- Finds mathematical ceiling on current day's snapshot (e.g., 0.07% = 3 tickers)
- Frontend slider controls grind depth (5 levels: 30s → 8 hours)

**Phase 2: Historical Scorer (5-year noise elimination)**
- After Phase 1 ceiling, greedily adds expressions that eliminate the most historical signals
- 5-year OHLCV cache (4,167 tickers × avg 1,108 bars, 214 MB) provides the history
- Precomputes all candidate boolean masks once, then scoring is pure numpy AND (sub-second/round)
- Stops when avg signals/day < target (default 10)
- Constraint: 100% of setup examples ALWAYS pass all conditions

**compute_series parity: ✅ COMPLETE** — All 82 generic ops from expression_engine.py are now available in backtest_conditions.compute_series(). Historical tiers (T2-T6) have full access to the 2,541 expression library.

**Universal design:** Upload examples for any setup type → grinder handles the rest. Universe matrix is shared, only example matrix + grind is per-setup.

---

## Architecture

```
local_runner/
├── agent.py              # Polling agent with nightly auto-rebuild
├── grinder.py            # CLI interface
├── pyramid_grinder.py    # Pyramidal grinder: 6 nested tiers (D1→5yr), peak-based
├── spiderweb.py          # Phase 1: beam search tree exploration (used by D1 tier)
├── historical_scorer.py  # Phase 2: greedy historical signal elimination (legacy, replaced by pyramid)
├── matrix_builder.py     # Precomputes universe + example matrices
├── cache_builder.py      # OHLCV caches (300-bar daily + 1,260-bar 5yr)
├── brute_expressions.py  # 2,541 expression generator (generic + per-setup bespoke)
└── cache/
    ├── universe_ohlcv.pkl      # Daily cache (~57 MB, 300 bars/ticker)
    ├── universe_ohlcv_5yr.pkl  # 5yr cache (~214 MB, avg 1,108 bars/ticker)
    ├── grinder_results_{setup}.json     # Phase 1 output
    └── historical_results_{setup}.json  # Phase 2 output

scripts/
├── expression_engine.py     # Computes expressions against OHLCV (point + series)
├── backtest_conditions.py   # Series computation for historical scoring
├── lsp_detector.py          # Local Structural Peak detection (DTSS-specific)
├── classify_universe.py     # ETF classifier (quarterly, desktop-only)
└── fast_profiler.py         # FastProfiler for rapid example profiling

server.py                    # Railway API: 14+ endpoints, universe rebuild, grinder jobs
```

### Key data stores
- **Railway SQLite DB:** 11M+ rows OHLCV across ~4,167 tickers, examples, scan results, classifications
- **Local 5yr cache:** `universe_ohlcv_5yr.pkl` — 4,167 tickers × avg 1,108 bars (214 MB)
- **Local daily cache:** `universe_ohlcv.pkl` — 4,167 tickers × avg 292 bars (57 MB)
- **Tradable universe:** ~4,017 tickers after ETF exclusions (150 leveraged/inverse/derivative excluded)

### Expression library: 2,541 expressions across 29 categories
- near_resistance (196), near_support (119), extension (78), extension_dynamics (65)
- extension_ceiling (32), extension_adr (6), ma_slope (176), ma_spread (40)
- spread_slope (48), slope_ratio (12), ma_cross (60), ma_stack (5)
- momentum (115), range (49), range_dynamics (11), retracement (24)
- swing_structure (42), gap (21), consecutive (4), candle_pattern (26)
- volume_character (36), volume_continuous (20), bollinger (20), macd (21)
- aroon (12), efficiency (7), vwap (36), percentile_rank (25), boolean (1,235)

---

## DTSS Pipeline Status

| Step | Status | Notes |
|------|--------|-------|
| 1 Load | ✅ Done | Data + TA knowledge loaded |
| 2 Receive | ✅ Done | 26 examples with LSP data |
| 3 Grind (Phase 1) | ✅ Done | 9 conditions, 0.00% single-day pass rate, 11s at L3 |
| 4 Grind (Phase 2) | ✅ Done | 12 conditions (9 P1 + 3 P2), avg 7.4 signals/day, 20.6 min |
| 4.7 Signal analysis | ✅ Done | Peak: 260/day (2021-08-11), clustered Jul-Aug 2021. Avg hides massive spikes. |
| 5 **Pyramid grinder** | **✅ Tested** | 10 conditions, peak 69/day (down from 260), avg 7.2/day. Hit ceiling — ran out of expressions. 5yr matrix build ~10 min. |
| 5a **compute_series parity** | **✅ Done** | 39 ops ported. 82 total ops, full parity with expression_engine (excluding 8 LSP ops). All 2,541 expressions now available to all pyramid tiers. |
| 5b **Expression expansion** | **⬜ NEXT** | Expand library from 2,541 → ~3,500+. More param combos + new concepts from ta_knowledge.md. |
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

---

## IMMEDIATE NEXT STEP: Expression Library Expansion

### ✅ COMPLETED: compute_series Parity (Job 1)

39 generic ops ported from `expression_engine.compute()` to `backtest_conditions.compute_series()`. All historical pyramid tiers now have access to the full 2,541 expression library. 8 LSP ops intentionally excluded (require injected context). Validated 38/39 exact match with point-value implementations.

### Problem

The pyramid grinder hit a ceiling at peak 69/day (down from 260) with only 10 conditions. It ran out of useful expressions. Now that compute_series has full parity, the remaining bottleneck is the expression library itself — more parameter combinations and new concepts are needed.

### Plan: Job 2 — Expand expression library

**Target: 2,541 → ~3,500+ expressions**
- More parameter combinations for existing categories
- New concepts from ta_knowledge.md that aren't covered yet

### Why this matters
Even 1-2 new conditions locking at early tiers (D1, 1wk, 1mo) would dramatically reduce the number of surviving rows hitting the expensive 5yr tier. The expressions both improve grind quality AND reduce grind time.

### After expression expansion
Re-run the pyramid: `python local_runner/pyramid_grinder.py --setup dtss --peak-target 15`
Then verify with signal distribution: `python scripts/signal_distribution.py --setup dtss`
With full compute_series parity + expanded library, expect significantly more conditions locking and lower peak/day.

### Future optimization: Full-history series cache
Once the expression library stabilizes, pre-cache all expression series for all tickers (47 GB on disk, stream per-ticker). Would reduce the 10-min 5yr matrix build to ~1 min. Build once, append 1 bar/ticker nightly. Not blocking — do this after expressions prove the pyramid works at target.

---

## BUILD PLAN — Remaining Steps

### Step 5a: compute_series Parity ✅ COMPLETE
**What:** Ported 39 missing ops from `expression_engine.py` to `backtest_conditions.compute_series()`.
**Result:** 82 total ops in compute_series (full parity with expression_engine, excluding 8 LSP ops). All 2,541 expressions now accessible to all pyramid tiers (T2-T6), not just D1. Validated 38/39 exact match.

### Step 5b: Expression Library Expansion ⬜ NEXT
**What:** Expand expression library from 2,541 → ~3,500+

**Why:** Pyramid grinder hit ceiling at peak 69/day with only 10 conditions. Now that compute_series has full parity, the remaining bottleneck is expression library size. More expressions = more conditions locking earlier = faster grind + lower peak.

**Constraint:** Matrix rebuild must finish before 7pm ET. Current 2,541 builds in 2.8 min — budget allows ~3,500-4,000.

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
| 1 | **Full-history series cache** | Pre-cache all expression series for all tickers on disk (~47 GB). Stream per-ticker to avoid RAM limits. Build once, append 1 bar/ticker nightly. Reduces 10-min 5yr matrix build to ~1 min. Do after expression library stabilizes. |
| 2 | **Nightly automation** | Chain: append cache → rebuild matrix → grind per setup → output candidates. All before 7pm ET. |
| 3 | **Dynamic re-grind** | System re-grinds nightly and adjusts conditions as market evolves. |
| 4 | **HTF setup** | Third setup type. Collect and load examples. |
| 5 | **IPO break setup** | CRWV-style IPO breakout of highest AVWAP. Short history stocks. |
| 6 | **EV optimization** | Management optimizer against MFE/MAE matrices for position sizing. |
| 7 | **Market regime filter** | "When to trade it" on/off switch per setup. 3-4DB showed 6-7x signal spikes during stage transitions. |
| 8 | **Universal pivot expressions** | Detect all D1 pivots with prominence, generate expressions for spiderweb. Currently parked — LSP detector needs refactoring. |
| 9 | **Frontend Phase 2 control** | Run pyramid grinder from the frontend (like Phase 1 grind). Detailed progress per tier. |

---

## Performance — Desktop Benchmarks (i5-12600K)

| Component | Time | Notes |
|-----------|------|-------|
| Daily matrix build | 2.8 min | 4,017 tickers × 2,541 expressions, 8 workers |
| Spiderweb grind (L3) | ~11s | beam=50, depth=10, 550K nodes |
| Spiderweb grind (L5) | 1-8 hours | beam=250, depth=15 |
| **Pyramid grinder (full)** | **~12 min** | **D1 (cached: <1s) + tiers 2-5 (~2 min) + 5yr matrix build (~10 min). 10 conditions, peak 69/day.** |
| Pyramid 5yr matrix build | ~10 min | Bottleneck. compute_series() for all candidates × surviving tickers. Will improve with more conditions locking earlier. |
| Phase 2 legacy total | 20.6 min | Down from 28.2 min after ProcessPool switch (deprecated by pyramid) |
| Signal distribution | ~15s | 4,167 tickers × 12 conditions, parallel (ProcessPool) |
| Daily cache build | 4.4 min | 4,167 tickers × 300 bars |
| 5yr cache build | 4.6 min | 4,167 tickers × 1,260 bars |
| ETF classifier | ~10 min | Quarterly, yfinance API |

**Note:** Phase 2 uses ProcessPoolExecutor with cache-via-initializer pattern (serialized once per worker at startup, not per task). Batched ticker submission eliminates per-task DataFrame pickling overhead. 99% CPU utilization on i5-12600K.

---

## Key Optimizations Implemented

1. **`_bool_series` lazy eval** — if/elif dispatch instead of dict-literal. ~37% matrix build speedup.
2. **Matmul spiderweb** — float32 matmul replaces Python beam×candidate loop. 78x faster.
3. **Numpy array serialization** — raw numpy to workers, skip `pd.to_numeric`. 8x faster reconstruct.
4. **Example matrix parallelization** — ThreadPoolExecutor (10 threads) for concurrent example builds.
5. **No bar minimum in cache** — every tradable ticker included regardless of history length.
6. **ProcessPoolExecutor for Phase 2** — true CPU parallelism for base signal + candidate precompute (ThreadPool was GIL-bound at 10% CPU).
