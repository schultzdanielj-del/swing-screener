# TODO

## Current State (as of 2026-02-22)

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
| 3 Profile | ✅ Done | **THE GRINDER** — 1,338 expressions, spiderweb combo search, desktop compute |
| 4 Collaborate | **→ NEXT** | Take grinder ceiling, add discretionary/qualitative conditions together |
| 5 Backtest | Not started | |
| 6 Market Context | Not started | |
| 7 EV Optimize | Not started | |

---

## Immediate Next Steps

| # | Task | Description |
|---|------|-------------|
| 1 | **Run DTSS grind to completion** | First universe matrix build (~30 min one-time). Then run grinder at various levels to find the mathematical ceiling. |
| 2 | **Collaborative refinement (Step 4)** | Review grinder results together. Add market regime, AVWAP, algo line, and qualitative conditions that the math can't find. Goal: 0% daily pass rate (scan only fires when setup is present). |
| 3 | **3-4DB through grinder** | Already has 21 examples. Run through same pipeline — universe matrix is shared so no 30-min wait. |
| 4 | **Backtest validated conditions** | Run final conditions across 5 years of history, review signal quality |
| 5 | **Market regime analysis (Step 6)** | Build the "when to trade it" filter. 3-4DB showed 6-7x signal spikes during stage transitions. |
| 6 | **Daily scan automation** | Nightly job: run scan conditions against today's data, surface tomorrow's candidates. |
| 7 | **HTF setup examples** | Third setup type has zero examples. Need to collect and load. |

---

## Parallelize Matrix Build

The universe matrix build (`get_universe_matrix`) is single-threaded. Each ticker is fully independent — trivial to parallelize with `ProcessPoolExecutor`. On a 12600K (10 cores) this should cut build time from ~30 min down to 3-5 min.

**What to do:** Wrap the ticker loop in `local_runner/matrix_builder.py` line 173 with `ProcessPoolExecutor(max_workers=N)`. Progress reporting needs minor adjustment to work across processes.

---

## Expression Library Expansion: 2,271 → ~3,800

Target: ~3,800 expressions via cheap numerics (hold booleans at 1,235). Benchmarked: matrix build goes from 5.9 min → 6.8 min with 8 workers. Negligible cost increase because booleans are 81% of compute and stay constant.

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

---

## Other Priorities

| # | Task | Description |
|---|------|-------------|
| 1 | **3-4DB backtest → optimizer** | Run 800+ backtest signals through outcome precomputation + management optimizer. Get real EV numbers. |
| 2 | **EV optimization pipeline** | Build Step 7 — brute force management testing against MFE/MAE matrices. |
