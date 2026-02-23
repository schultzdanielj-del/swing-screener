# TODO

## Current State (as of 2026-02-23)

### The Grinder — Desktop Expression Discovery Engine ✅ LIVE

The core analysis engine is now a desktop-based spiderweb search system:
- **Desktop agent** (`local_runner/agent.py`) runs 24/7, polls Railway for jobs
- **Universe matrix** (~4,167 tickers × 2,271 expressions) auto-rebuilds nightly at 4:30pm ET
- **Spiderweb search** explores branching condition combinations via beam search
- **Frontend slider** controls grind depth (5 levels: 30s → 8 hours)
- **Results display** shows pass rate progression + best condition combo

**Architecture:**
- `local_runner/matrix_builder.py` — Precomputes universe matrix (daily, shared) + example matrix (per-setup, fast)
- `local_runner/spiderweb.py` — Beam search tree exploration
- `local_runner/grinder.py` — CLI interface
- `local_runner/agent.py` — Polling agent with nightly auto-rebuild
- `local_runner/cache_builder.py` — OHLCV cache from Railway DB
- `local_runner/brute_expressions.py` — 2,271 expression generator (expanded from 1,338)
- `server.py` — 13 grinder API endpoints (jobs/status/progress/results/agent)

---

## DTSS Pipeline Status

| Step | Status | Notes |
|------|--------|-------|
| 1 Load | ✅ Done | Data + TA knowledge loaded |
| 2 Receive | ✅ Done | 26 examples with LSP data |
| 3 Profile | ✅ Done | **THE GRINDER** — 2,271 expressions, spiderweb combo search, desktop compute |
| 4 Backtest Grind | **→ NEXT** | Phase 1 grind works (0.07% today, 1.2s). Phase 2 historical scoring needed — today's 3 tickers = 340/day historically. Need to grind against 5yr history, not just today. |
| 5 Collaborate | Not started | Take grinder ceiling, add discretionary/qualitative conditions together |
| 6 Market Context | Not started | |
| 7 EV Optimize | Not started | |

---

## BUILD PLAN — Grinder v2: Historical Backtest Scoring

**Problem discovered 2026-02-23:** The grinder finds conditions that filter tightly on TODAY (0.07%, 3 tickers) but when backtested across 200 days: 68,065 signals, 340/day avg. The conditions describe "bull market uptrend" not "DTSS setup." The spiderweb stops at the single-day ceiling but there are still expressions that could eliminate massive historical noise without losing any examples.

**Solution:** After single-day ceiling, enter Phase 2 that scores candidate expressions by historical signal reduction. Keep stacking conditions until historical signals/day is manageable. Constraint: 100% of setup examples ALWAYS pass.

**This must work for any setup type — upload examples, describe the setup, grinder handles the rest.**

---

### Step 1: 5-Year OHLCV Cache ⬜
**What:** Extend local cache from 300 bars (~1yr) to 1,250 bars (~5yr) per ticker.
**Why:** Phase 2 scoring needs 5 years of history. Can't hit Railway API during the grind.
**How:**
- One-time build: fetch 1,250 bars per ticker from Railway DB (~10-15 min with 20 threads)
- Store as `local_runner/cache/universe_ohlcv_5yr.pkl` (est ~200MB)
- Daily append: add today's bar for each ticker (~2-3 min)
- Keep existing 300-bar cache for matrix build (doesn't need 5yr)
**Files:** `local_runner/cache_builder.py`

### Step 2: Clean Tradable Universe ⬜ (script ready, needs desktop run)
**What:** Remove inverse/leveraged/single-stock/derivative ETFs from tradable_universe.
**Why:** TQQQ, NVDL, AAPU etc. fire duplicate signals on the same move as the underlying, inflating signal counts and masking true selectivity.
**How:**
- `scripts/classify_universe.py` — classifies all 4,167 tickers via yfinance (quoteType + category + fund family)
- Stores results in `ticker_classification` table (full details) + `universe_exclusions` table (permanent exclude list)
- Categories: equity, etf_plain (keep), etf_leveraged/etf_inverse/etf_single_stock/etf_derivative_income/etf_volatility (exclude)
- `server.py` rebuild_tradable_universe() now checks `universe_exclusions` — excluded tickers never come back
- **Run quarterly** to catch new tickers/ETF launches
- **Must run on desktop** — yfinance rate limits prevent running from sandbox
**Usage:** `python scripts/classify_universe.py` (first run), `python scripts/classify_universe.py --force` (quarterly re-classify)
**Files:** `scripts/classify_universe.py` (new), `server.py` (modified rebuild)

### Step 3: Expression Library Expansion ⬜
**What:** 2,271 → ~3,800 expressions via cheap numeric ops.
**Why:** Only 18 valid expressions survived filtering in the DTSS grind. More expressions = more tools for the spiderweb to find setup-specific conditions (not just broad market regime).
**How:** See detailed expansion plan in "Expression Library Expansion" section below.
**Files:** `local_runner/brute_expressions.py`, `scripts/expression_engine.py`
**Constraint:** Matrix rebuild must stay under 7 min. Benchmarked at 6.8 min pre-optimization, should be ~4 min with bool lazy eval.

### Step 4: Phase 2 — Historical Backtest Scoring ⬜ (code ready, needs 5yr cache)
**What:** After single-day ceiling, greedily add expressions that eliminate the most historical signals.
**Why:** This is the core fix. A condition useless today (doesn't drop 3→2 tickers) might eliminate 300/day of historical noise.
**How:** `local_runner/historical_scorer.py`
1. Loads Phase 1 results + 5yr OHLCV cache + example data
2. Computes base signal mask: Phase 1 conditions × all tickers × all bars
3. Pre-computes example ranges for ~2,000 candidate expressions
4. Pre-computes ALL candidate boolean masks per signal-bearing ticker (one-time, ~2-5 min)
5. Greedy rounds: score all candidates via numpy AND (sub-second/round), add best eliminator
6. Stops when avg signals/day < target (default 10)
**Performance:** Precompute = O(tickers × candidates × bars) done once. Rounds = pure vectorized numpy.
**Usage:** `python local_runner/historical_scorer.py --setup dtss --target 10`
**Requires:** Phase 1 grinder results + 5yr OHLCV cache (Step 1)
**Key constraint:** 100% of setup examples must ALWAYS pass all conditions. Zero false negatives.

### Step 5: Backtest Runner Integration ⬜
**What:** Make `scripts/backtest_conditions.py` pull conditions from grinder results automatically instead of hardcoded. Output signal charts.
**Why:** Need to visually verify signals. Currently hardcoded from one grind run.
**How:**
- Read conditions from grinder result JSON (API endpoint or local file)
- Generate candlestick charts for each signal (entry candle marked)
- Summary stats: signals/day distribution, worst day, ticker frequency
**Files:** `scripts/backtest_conditions.py`

### Step 6: End-to-End Pipeline Test ⬜
**What:** Run full pipeline on DTSS: grind → historical score → backtest → review signals.
**Why:** Validate the whole system produces tight, setup-specific conditions.
**Expected outcome:** Conditions that fire <10/day avg across 5 years, with clear DTSS setups in the results.

### Step 7: Second Setup — 3-4DB ⬜
**What:** Run 3-4DB examples (21 already loaded) through the same pipeline.
**Why:** Validates the system is setup-agnostic. Universe matrix is shared, only example matrix + grind is new.

---

## Future (not blocking current work)

| # | Task | Description |
|---|------|-------------|
| 1 | **Nightly automation** | Chain: append cache → rebuild matrix → grind per setup → output candidates. All before 7pm ET. |
| 2 | **Dynamic re-grind** | System re-grinds nightly and adjusts conditions as market evolves. |
| 3 | **HTF setup** | Third setup type, zero examples. Collect and load. |
| 4 | **EV optimization** | Management optimizer against MFE/MAE matrices for position sizing. |
| 5 | **Market regime filter** | "When to trade it" on/off switch per setup. 3-4DB showed 6-7x signal spikes during stage transitions. |

---

## Performance Optimizations — ✅ ALL IMPLEMENTED & TESTED (2026-02-23)

**Desktop test results:** Matrix build: 2.8 min (down from 30+ min). Spiderweb grind: 1.2s for full DTSS. All working.

### 1. `_bool_series` lazy eval — ✅ DONE
**File:** `scripts/expression_engine.py`
**Change:** Replaced dict-literal (eagerly evaluates ALL ~55 booleans) with if/elif dispatch (only computes the one requested).
**Expected:** ~37% matrix build speedup (5.9 min → ~3.7 min).
**Verified:** All 65 boolean conditions pass in sandbox.

### 2. Matmul spiderweb — ✅ DONE
**File:** `local_runner/spiderweb.py`
**Change:** Replaced Python-level beam×candidate loop with float32 matmul for all joint pass rates + numpy vectorized filtering + sorted early exit.
**Measured:** 78x faster in sandbox (100K → 7.8M nodes/sec). Real-scale test (1000 exprs, 4000 universe, beam 250) completes in 0.16s.
**Verified:** Same results as old implementation on simulated data.

### 3. Numpy array serialization — ✅ DONE
**File:** `local_runner/matrix_builder.py`
**Change:** Send raw numpy arrays to workers instead of `df.to_dict(orient="list")`. Eliminates `pd.to_numeric` coercion in worker.
**Measured:** 8x faster reconstruct (0.76ms → 0.09ms), 9% smaller pickle.
**Verified:** Worker produces identical output.

### 4. Example matrix parallelization — ✅ DONE
**File:** `local_runner/matrix_builder.py`
**Change:** ThreadPoolExecutor (10 threads) for concurrent OHLCV fetch + LSP detection + compute across all examples.
**Expected:** ~4-5x faster example build (~13s → ~2-3s for 26 DTSS examples).
**Verified:** Imports clean, logic preserved. Needs real API test.

---

## Data Cleanup: Remove Inverse/Leveraged ETFs from Tradable Universe

Inverse and leveraged ETFs (AIPO, HOOZ, etc.) are polluting grinder results — they have synthetic price patterns that match setup criteria but are not tradable swing candidates. These need to be permanently excluded.

**Task:**
- Identify all inverse, leveraged, and synthetic ETFs in the tradable_universe table (~hundreds of tickers)
- Remove them from the database
- Add a filter to the universe rebuild process so they never get re-added
- Common patterns: tickers from ProShares, Direxion, GraniteShares, leveraged/inverse fund names, ETF suffixes

---

## Expression Library Expansion: 2,271 → ~3,800

Target: ~3,800 expressions via cheap numerics (hold booleans at 1,235). Benchmarked: matrix build goes from 5.9 min → 6.8 min with 8 workers. Negligible cost increase because booleans are 81% of compute and stay constant.

**Note:** The `_bool_series` lazy eval optimization (#1 above) should reduce all these times by ~37%. Actual numbers need re-benchmarking on desktop after testing.

**Timing benchmarks (from real per-ticker measurement):**
| Config | ms/ticker | 8-worker build |
|--------|-----------|----------------|
| 1,338 original (sequential) | 399 | 28 min (1 worker) |
| 2,271 current | 677 | 5.9 min |
| 3,800 cheap numerics | 781 | 6.8 min |
| 3,800 via booleans | 1,151 | 10.0 min |

**Per-category cost (ms/expression):** boolean 0.44, bollinger 0.78, swing_structure 0.44, momentum 0.33, aroon 0.33, efficiency 0.34, vwap 0.21, gap 0.22 — vs cheap ops: ma_slope 0.01, extension 0.04, ma_spread 0.01, retracement 0.07.

**What to add (~960 new cheap expressions):**
- Extension: add HMA, EMA8/EMA21/EMA100 (78 → 130)
- MA slope: HMA slopes, more offsets (176 → 260)
- MA spread: more pairs + slope of spread (40 → 80)
- Extension dynamics: acceleration (slope of slope), more MAs (65 → 100)
- Extension ADR multiples: all MAs (6 → 16)
- Extension ceiling: add EMA8, EMA21 (32 → 48)
- Near resistance: add distance_to_minl (196 → 240)
- Retracement: separate high/low retracement (10 → 20)
- VWAP: more periods + VWAP slope (12 → 24)
- Percentile rank: close/vol/range × lookbacks (0 → 48, new op)
- Slope ratios: fast/slow MA slope ratios (0 → 30, new op)
- Momentum: WRSI, more stoch/CCI periods, continuous RVOL (107 → 170)
- Volume character: cumulative RVOL, more OBV (36 → 55)
- Remaining misc expansions across candle_pattern, range, gap, etc.

**New compute ops needed in expression_engine.py:** `percentile_rank`, `rvol_continuous`, `cumulative_rvol`, `slope_ratio`, `wrsi`, `extension_accel`, `distance_to_minl`

**Spiderweb impact:** More valid expressions survive 95% filter (~400 vs ~250). Search goes deeper before ceiling. Old L5 starved at depth 4 in 8s; new L5 should reach depth 6-8 in 1-3 min with tighter final pass rate.

**Constraint:** Rebuild must finish before 7pm ET. At 6.8 min this is well within budget.

---

## Future Investigation: Universal Pivot/LSP Expressions

Currently LSP expressions are in a bespoke Phase 2 block per setup type. The limitation: they don't participate in the spiderweb's combinatorial search (Phase 1 only).

**Insight:** LSPs are just prominent D1 pivot highs. Different setups use pivots differently — DTSS uses the most recent structural high, PDub UNR uses a support level further back with multiple pivot touches. But they're all points on the same pivot spectrum.

**Proposed approach:** Detect ALL D1 pivots (highs and lows) with prominence scores across the universe. Generate expressions against multiple pivot ranks, proximity clusters, and prominence thresholds. Let the spiderweb figure out which pivot measurements matter per setup.

**Expressions would include:** `nearest_pivot_high_dist_atr`, `pivot_high_count_within_Natr`, `prominent_pivot_dist`, `highest_pivot_60bars_dist`, etc. across multiple ranks and prominence cutoffs.

**Blockers:**
- LSP detector currently requires Railway API calls per ticker — needs refactoring to work off in-memory DataFrames
- Detection robustness across 4,167 random tickers (not just setup examples) is unknown
- Expression count multiplies fast (5 ranks × 19 expressions × 3 normalizers × prominence thresholds)
- Need to validate what "significant D1 pivot" means universally vs just for DTSS

**Decision:** Parked for now. Focus on generic expression expansion first (immediate grinder improvement), revisit after that's working.


