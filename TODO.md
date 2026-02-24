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
- Expression series cache (4,119 tickers × 4,017 expressions, ~21 GB compressed) eliminates compute bottleneck
- Constraint: 100% of setup examples ALWAYS pass all conditions

**Optimal grind parameters (found 2026-02-24):**
- Beam width: 200
- Search depth: 30
- Peak target: 5 (produces peak 6/day, 184 total signals across 5yr, avg 2.2/day)
- Runtime: ~3 min with expression cache (was 40 min without)

**Nightly Pipeline: ✅ COMPLETE** — `python local_runner/nightly.py` chains 5 steps: Railway incremental append → daily cache → 5yr cache → expression cache append → matrix rebuild. Auto-triggered by agent at 4:30pm ET on trading days. ~15-20 min if new data, <1 min if already current.

**compute_series parity: ✅ COMPLETE** — All 88 ops available in backtest_conditions.compute_series(). Historical tiers (T2-T6) have full access to the 4,017 expression library.

**Expression library: ✅ EXPANDED** — 2,541 → 4,017 expressions. 127 boolean conditions (was 65), 88 ops (was 82). 29 categories.

**Expression series cache: ✅ COMPLETE** — 4,119 tickers × 4,017 expressions pre-cached on disk (~21 GB). Build: 37.5 min one-time. Nightly append: 5-8 min. Pyramid grinder auto-detects cache. 14x speedup at equivalent beam width.

**NaN validation fix: ✅ COMPLETE** — Expression ranges now require ALL examples with scan_idx to have non-NaN values (was 70% threshold). Guarantees zero false negatives by construction.

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
├── expr_cache_builder.py # Pre-cached expression series for all tickers × 5yr (~21 GB)
├── brute_expressions.py  # 4,017 expression generator (generic, universal)
└── cache/
    ├── universe_ohlcv.pkl      # Daily cache (~57 MB, 300 bars/ticker)
    ├── universe_ohlcv_5yr.pkl  # 5yr cache (~214 MB, avg 1,108 bars/ticker)
    ├── expr_series/             # Pre-cached expression series (~21 GB compressed)
    ├── universe_matrix.npz      # D1 universe matrix (auto-rebuilt nightly)
    ├── pyramid_results_{setup}.json     # Pyramid grinder output
    └── historical_results_{setup}.json  # Compat format for signal_distribution.py

scripts/
├── expression_engine.py     # Computes expressions against OHLCV (point + series)
├── backtest_conditions.py   # Series computation for historical scoring
├── backtest_runner.py       # Step 6: parallel signal scan + chart generation for visual review
├── signal_distribution.py   # Parallel signal analyzer: daily counts + per-signal CSV
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
- **Expression series cache:** `cache/expr_series/` — 4,119 tickers × 4,017 expressions (~21 GB)
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
| 4 Grind (Phase 2 legacy) | ✅ Done | 12 conditions (9 P1 + 3 P2), avg 7.4 signals/day, 20.6 min |
| 4.7 Signal analysis | ✅ Done | Peak: 260/day (2021-08-11), clustered Jul-Aug 2021. Avg hides massive spikes. |
| 5a compute_series parity | ✅ Done | 82 → 88 total ops. Full parity with expression_engine. |
| 5b Expression expansion | ✅ Done | 2,541 → 4,017 expressions. 127 booleans, 88 ops. |
| 5c Expression series cache | ✅ Done | 4,119 tickers × 4,017 exprs, ~21 GB, 37.5 min build, 5-8 min nightly append. |
| 5d **NaN validation fix** | **✅ Done** | 100% example coverage required for expression ranges (was 70%). Zero false negatives guaranteed. |
| 5e **Optimal grind params** | **✅ Done** | beam=200, depth=30, peak-target=5. Result: 30 conditions, peak 6/day, 184 signals/5yr, 3 min runtime. |
| **6 Backtest Runner** | **🔧 IN PROGRESS** | `scripts/backtest_runner.py` built. Scans 5yr cache, generates charts per signal. Run locally to verify. |
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
- 11 new compute ops in both `expression_engine.py` and `backtest_conditions.py`

### Step 4.5: Strip Bespoke System ✅ COMPLETE
- Removed DTSS-specific expression generation and LSP injection
- Universal expression library only

### Step 4: Phase 2 — Historical Scorer ✅ COMPLETE (legacy, replaced by pyramid)
- Greedy forward selection with precomputed numpy masks
- Achieved avg 7.4/day but peak 260/day — fundamental design flaw (targeted average, not peak)

### Step 4.6: Phase 1→2 Integration Fixes ✅ COMPLETE
- Compute spec enrichment, missing ops, key name mismatches, ProcessPool parallelization

### Step 5a: compute_series Parity ✅ COMPLETE
- 82 → 88 total ops. Full parity with expression_engine (excluding 8 LSP ops).

### Step 5b: Expression Library Expansion ✅ COMPLETE
- 2,541 → 4,017 expressions. 127 boolean conditions, 88 ops, 6 new ops.

### Step 5c: Expression Series Cache ✅ COMPLETE
- `local_runner/expr_cache_builder.py` — pre-caches all 4,017 expression series
- 4,119 tickers × 4,017 expressions, ~21 GB compressed (.npz per ticker)
- Build: 37.5 min one-time. Append: 5-8 min nightly. Manifest tracks expression fingerprint.
- Pyramid grinder auto-detects cache, 14x speedup (40 min → 2.9 min at equivalent beam)

### Step 5d: NaN Validation Fix ✅ COMPLETE
- Expression ranges now require ALL examples with scan_idx to have non-NaN values
- Previous 70% threshold allowed expressions where 6/23 examples had NaN, causing validation failures
- Fix guarantees zero false negatives by construction — fewer candidate expressions but every one is safe

### Step 5e: Optimal Grind Parameters ✅ FOUND
- **beam=200, depth=30, peak-target=5** — the production parameters
- Results: 30 conditions, peak 6/day, 184 total signals across 5yr, avg 2.2/day, 3 min runtime
- All 21 examples pass validation (zero false negatives confirmed)
- Comparison: beam=200/depth=30/peak-target=15 → 33 conditions, peak 14/day (looser alternative)

### Nightly Automation ✅ COMPLETE
- `python local_runner/nightly.py` — 5-step chain with gate logic
- Agent triggers at 4:30pm ET on weekdays, ~15-20 min total

---

## IMMEDIATE NEXT STEP: Backtest Runner (Step 6)

The grinder is done. 184 signals across 5 years, peak 6/day. Now we need to see what they look like.

**What to build:**
1. Load conditions from `pyramid_results_dtss.json`
2. Run all 30 conditions across 5yr OHLCV cache for all tradable tickers
3. Generate candlestick charts for each signal (dark theme, entry candle marked, MAs overlaid)
4. Present signals for visual review — are they real DTSS setups?
5. Feedback loop: identify noise signals → tighten conditions or add qualitative filters

**Why this matters:**
- The grinder eliminates noise mathematically but can't tell if remaining signals *look* like the setup
- 184 signals is small enough to review every one
- This is where the conditions get refined from "mathematically tight" to "visually correct"

---

## BUILD PLAN — Remaining Steps

### Step 6: Backtest Runner 🔧 IN PROGRESS
**What:** Generate charts for all historical signals, visual verification.
**Script:** `scripts/backtest_runner.py`
**How:**
- Read conditions from `pyramid_results_dtss.json` (or `historical_results_dtss.json`)
- Parallel scan against 5yr OHLCV cache (ProcessPoolExecutor, same pattern as signal_distribution.py)
- Generate mplfinance charts: dark theme, entry candle marked with magenta triangle, 8/21 EMA + 50/200 SMA
- Charts organized by date folder: `cache/backtest_charts_{setup}/{date}/{TICKER}_{date}.png`
- Output: signals CSV + summary stats + chart images

**Usage:**
```bash
# Full run: scan + charts
python scripts/backtest_runner.py --setup dtss

# Scan only, no charts
python scripts/backtest_runner.py --setup dtss --no-charts

# Regenerate charts from existing CSV
python scripts/backtest_runner.py --setup dtss --charts-only
```

**Status:** Script written, needs local run to generate actual signals + charts for review.

**Note (2026-02-24):** Consider building a frontend component to streamline the visual review workflow — browsing charts by date, tagging signals as valid/noise, tracking progress. Running CLI + manually browsing folders is error-prone when focus is split.

### Step 7: Market Context ⬜
**What:** Identify which market conditions produce winners vs losers.
**How:** Correlate signal outcomes with market regime (stage transitions, breadth, VIX).

### Step 8: EV Optimization ⬜
**What:** Exhaustive management parameter search against MFE/MAE outcome matrix.
**How:** Every stop/target/trail/time combination, ranked by EV per trade in ATR units.

### Step 9: Second Setup — 3-4DB ⬜
**What:** Run 3-4DB examples (21 already loaded) through the same pipeline.
**Why:** Validates the system is setup-agnostic.

---

## Future (not blocking current work)

| # | Task | Description |
|---|------|-------------|
| 1 | **Dynamic re-grind** | System re-grinds nightly and adjusts conditions as market evolves. |
| 2 | **HTF setup** | Third setup type. Collect and load examples. |
| 3 | **IPO break setup** | CRWV-style IPO breakout of highest AVWAP. Short history stocks. |
| 4 | **Frontend Phase 2 control** | Run pyramid grinder from the frontend (like Phase 1 grind). Detailed progress per tier. |
| 5 | **Universal pivot expressions** | Detect all D1 pivots with prominence, generate expressions for spiderweb. |
| 6 | **Intermediate pyramid tiers** | Add 2yr/3yr tiers between 1yr and 5yr for earlier noise elimination. |

---

## Performance — Desktop Benchmarks (i5-12600K)

| Component | Time | Notes |
|-----------|------|-------|
| Daily matrix build | ~5 min (est) | 4,017 tickers × 4,017 expressions, 8 workers |
| Spiderweb grind (L3) | ~11s | beam=50, depth=10, 550K nodes |
| Spiderweb grind (L5) | 1-8 hours | beam=250, depth=15 |
| **Pyramid grinder (with cache)** | **~3 min** | **beam=200, depth=30, peak-target=5. 30 conditions, peak 6/day, 184 signals/5yr** |
| **Pyramid grinder (with cache, target 15)** | **~2.9 min** | **beam=200, depth=30, peak-target=15. 33 conditions, peak 14/day** |
| Pyramid grinder (no cache, 4,017 exprs) | ~40 min | beam=50, depth=10. Boolean series computation is bottleneck. |
| Expression cache build | 37.5 min | One-time. 4,119 tickers × 4,017 expressions, ~21 GB compressed. |
| Expression cache append | 5-8 min (est) | Nightly, new bars only. |
| Phase 2 legacy total | 20.6 min | Deprecated by pyramid grinder. |
| Signal distribution | ~15s | 4,167 tickers × 12 conditions, parallel (ProcessPool) |
| Daily cache build | 4.4 min | 4,167 tickers × 300 bars |
| 5yr cache build | 4.6 min | 4,167 tickers × 1,260 bars |
| ETF classifier | ~10 min | Quarterly, yfinance API |
| **Nightly pipeline (full)** | **~15-20 min** | **append + daily cache + 5yr cache + expr cache + matrix** |
| **Nightly pipeline (current)** | **<1 min** | **Gate check only — Railway returns up_to_date** |

---

## Key Optimizations Implemented

1. **Expression series cache** — Pre-compute all 4,017 expressions for all tickers × 5yr. Eliminates compute bottleneck. 14x grinder speedup.
2. **NaN validation fix** — 100% example coverage required for expression ranges. Zero false negatives by construction.
3. **`_bool_series` lazy eval** — if/elif dispatch instead of dict-literal. ~37% matrix build speedup.
4. **Matmul spiderweb** — float32 matmul replaces Python beam×candidate loop. 78x faster.
5. **Numpy array serialization** — raw numpy to workers, skip `pd.to_numeric`. 8x faster reconstruct.
6. **Example matrix parallelization** — ThreadPoolExecutor (10 threads) for concurrent example builds.
7. **No bar minimum in cache** — every tradable ticker included regardless of history length.
8. **ProcessPoolExecutor for Phase 2** — true CPU parallelism (ThreadPool was GIL-bound at 10% CPU).
