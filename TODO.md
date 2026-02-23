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
| 3 Grind (Phase 1) | **⚠️ BLOCKED** | Matrix builds OK (2,541 exprs) but crashes on bespoke LSP mismatch. Must strip bespoke first. |
| 4 Grind (Phase 2) | ⬜ Waiting | Historical scorer code complete, 5yr cache built. Needs fresh Phase 1 results. |
| 5 Collaborate | Not started | Take grinder ceiling, add discretionary/qualitative conditions |
| 6 Backtest | Not started | Run full conditions across history, review signal charts |
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

### Step 4: Phase 2 — Historical Scorer ✅ CODE COMPLETE (needs execution)
- `python local_runner/historical_scorer.py --setup dtss --target 10`
- Greedy forward selection with precomputed numpy masks
- Requires: Phase 1 grinder results + 5yr OHLCV cache
- Output: `local_runner/cache/historical_results_{setup}.json`

---

## IMMEDIATE NEXT STEP: Strip Bespoke System

**What:** Remove ALL bespoke/LSP-specific code from the grinder pipeline. The system should be 100% generic — same expressions for all setups.

**Why:** Bespoke LSP expressions (19 DTSS-specific) make the example matrix wider than the universe matrix, crashing the spiderweb. More importantly, the Phase 2 historical scorer makes bespoke unnecessary — it grinds the full generic library against 5yr history to find setup-specific discrimination. No hand-crafted expressions needed.

**Files to modify:**
- `local_runner/matrix_builder.py` — Remove DTSS branch in `_load_expressions()`, remove `lsp_context` injection in `get_example_matrix()`, remove `get_bespoke_candidate_matrix()` function entirely
- `local_runner/brute_expressions.py` — Remove `generate_dtss_lsp_expressions()` and `generate_dtss()` functions (keep `generate_all()`)
- `local_runner/grinder.py` — Remove bespoke post-filter logic from results output
- `scripts/expression_engine.py` — Can keep LSP ops in engine (no harm), just nothing calls them
- Delete `local_runner/cache/dtss_expressions.json` if present

**After stripping:**
1. `python local_runner/grinder.py --setup dtss --level 3` should work cleanly
2. Example matrix and universe matrix both have exactly 2,541 columns
3. Phase 1 produces results → Phase 2 grinds historical noise → done

---

## BUILD PLAN — Remaining Steps

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

---

## Performance — Desktop Benchmarks (i5-12600K)

| Component | Time | Notes |
|-----------|------|-------|
| Daily matrix build | 2.8 min | 4,017 tickers × 2,541 expressions, 8 workers |
| Spiderweb grind (L1) | ~30s | beam=10, depth=5 |
| Spiderweb grind (L5) | 1-8 hours | beam=250, depth=15 |
| Daily cache build | 4.4 min | 4,167 tickers × 300 bars |
| 5yr cache build | 4.6 min | 4,167 tickers × 1,260 bars |
| Historical scorer | TBD | Precompute ~2-5 min, rounds sub-second each |
| ETF classifier | ~10 min | Quarterly, yfinance API |

---

## Key Optimizations Implemented

1. **`_bool_series` lazy eval** — if/elif dispatch instead of dict-literal. ~37% matrix build speedup.
2. **Matmul spiderweb** — float32 matmul replaces Python beam×candidate loop. 78x faster.
3. **Numpy array serialization** — raw numpy to workers, skip `pd.to_numeric`. 8x faster reconstruct.
4. **Example matrix parallelization** — ThreadPoolExecutor (10 threads) for concurrent example builds.
5. **No bar minimum in cache** — every tradable ticker included regardless of history length.
