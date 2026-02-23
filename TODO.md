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

**Universal design:** Upload examples for any setup type → grinder handles the rest. Universe matrix is shared, only example matrix + grind is per-setup.

---

## Architecture

```
local_runner/
├── agent.py              # Polling agent with nightly auto-rebuild
├── grinder.py            # CLI interface
├── spiderweb.py          # Phase 1: beam search tree exploration
├── historical_scorer.py  # Phase 2: greedy historical signal elimination
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
| 5 **Pyramid grinder** | **⬜ NEXT** | Replace Phase 1+2 with nested pyramid. Target: peak signals/day < 15 across 5yr. |
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

## IMMEDIATE NEXT STEP: Build Pyramidal Grinder

**Problem:** Current Phase 2 achieves avg 7.4/day but peak is 260/day (Jul-Aug 2021 cluster). Takes 20+ min and targets average, not peak.

**Solution:** Pyramidal grinder — nested time horizons, each tier grinds until `peak_signals/day < threshold` before advancing:
1. **D1 (today):** Grind to ceiling (spiderweb on today's snapshot). ~11s. Lock conditions.
2. **1 week:** Grind until peak/day < threshold. ~10-30s. Lock.
3. **1 month:** Same. ~30s. Lock.
4. **6 months → 1 year → 5 years:** Same. ~30s each. Lock.

Each tier is cheap because the previous one already eliminated the easy noise. Total: ~2 min instead of 28 min. Peak-based guarantee means no single day overwhelms manual review.

**Key:** Same spiderweb code, just different matrix construction per tier. Scoring metric = max(daily_counts) instead of sum.

**Analysis tools:**
- Signal distribution: `python scripts/signal_distribution.py` (parallel, all cores)
- Outputs: `cache/signals_daily_dtss.csv`, `cache/signals_dtss.csv`

---

## BUILD PLAN — Remaining Steps

### Step 5a: Expression Library Expansion ⬜
**What:** Expand from 2,541 → ~3,800+ expressions. The grinder's L5 setting finished in 8 seconds because it ran out of useful expressions at depth 4. It needs massively more search space.
**Missing from ta_knowledge.md:**
- Extension at statistical ceiling (how close to historical max extension)
- MOC/RVOL-anchored levels
- AVWAP from pivots
- Extension compression (narrowing extension)
- 50 SMA cross frequency (chop detection)
- Swing retracement levels
**New compute ops needed in expression_engine.py + backtest_conditions.py:**
- `percentile_rank` — normalize any metric to 0-100 vs history
- `rvol_continuous` / `cumulative_rvol` — rolling relative volume
- `slope_ratio` — fast MA slope / slow MA slope
- `spread_slope` — is MA spread widening or narrowing
- `distance_to_minl` / `ratio_c_minl` — support proximity
- `retrace_high` / `retrace_low` — separate H/L retracement
- `vwap_slope` — rolling VWAP direction
**Per-category expansion:** near_support (119→196), percentile_rank (0→48), slope_ratio (0→30), momentum (107→170), volume_character (36→55), plus misc across remaining categories.
**Constraint:** Matrix rebuild must finish before 7pm ET. Benchmarked at 6.8 min — well within budget.

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
| 1 | **Nightly automation** | Chain: append cache → rebuild matrix → grind per setup → output candidates. All before 7pm ET. |
| 2 | **Dynamic re-grind** | System re-grinds nightly and adjusts conditions as market evolves. |
| 3 | **HTF setup** | Third setup type. Collect and load examples. |
| 4 | **IPO break setup** | CRWV-style IPO breakout of highest AVWAP. Short history stocks. |
| 5 | **EV optimization** | Management optimizer against MFE/MAE matrices for position sizing. |
| 6 | **Market regime filter** | "When to trade it" on/off switch per setup. 3-4DB showed 6-7x signal spikes during stage transitions. |
| 7 | **Universal pivot expressions** | Detect all D1 pivots with prominence, generate expressions for spiderweb. Currently parked — LSP detector needs refactoring. |
| 8 | **Frontend Phase 2 control** | Run Phase 2 historical scorer from the frontend (like Phase 1 grind). Detailed progress: base signals, precompute %, greedy rounds, signals/day reduction, ETA. |
| 9 | **Pyramidal grinder (Phase 1/2 replacement)** | Replace the current 2-phase system with a nested pyramid that progressively widens the historical window. Each tier locks in conditions from the previous tier and grinds on the next time horizon. **D1 (today):** Grind to ceiling (can't go below # of tickers that pass). **Historical tiers (1wk → 1mo → 6mo → 1yr → 5yr):** Each grinds until `peak_signals/day < threshold` (e.g. 15) before advancing. Each tier is fast (~30s) because the previous one already eliminated the cheap noise. Total pipeline ~2 min instead of 28 min. Peak-based target guarantees no single day overwhelms manual review. |

---

## Performance — Desktop Benchmarks (i5-12600K)

| Component | Time | Notes |
|-----------|------|-------|
| Daily matrix build | 2.8 min | 4,017 tickers × 2,541 expressions, 8 workers |
| Spiderweb grind (L3) | ~11s | beam=50, depth=10, 550K nodes |
| Spiderweb grind (L5) | 1-8 hours | beam=250, depth=15 |
| Phase 2 base signals | ~66s | 4,167 tickers × 9 conditions → 192K signals (ProcessPool, 15 workers) |
| Phase 2 candidate precompute | ~1,100s | 2,106 candidates × 3,373 tickers (ProcessPool, 15 workers) |
| Phase 2 greedy rounds | ~12s | 3 rounds, sub-second each (pure numpy AND) |
| Phase 2 total | 20.6 min | Down from 28.2 min after ProcessPool switch |
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
