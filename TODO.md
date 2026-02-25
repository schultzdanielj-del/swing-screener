# TODO

## Current State (as of 2026-02-25)

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
- Matmul pre-screening: (beam × rows) @ (rows × candidates) estimates joint counts, only top candidates materialized
- OpenBLAS MAX_THREADS=24 parallelizes matmul across all cores
- Deterministic: row sorting by (date, ticker), tiebreakers by (peak, -total_signals, condition_indices)
- NaN handling: 70% example coverage threshold (maximizes candidate pool)
- Constraint: 100% of setup examples ALWAYS pass all conditions

**Production grind parameters (found 2026-02-24):**
- Beam width: 10000 (exhaustive — search self-terminates when no path improves, typically ~25-30 levels)
- Search depth: 100 (ceiling, never reached — search stops early)
- Peak targets: sweep 2-10, take best result
- Best result: peak-target=3 → 26 conditions, peak 6/day, 201 total signals across 5yr, avg 2.1/day (~3.4/month)
- Runtime: ~5 min per peak target, ~50 min full sweep (2-10)
- beam=10000 fully exhausts search space — more beam won't help. If results plateau, expand expression library.

**Nightly Pipeline: ✅ COMPLETE** — `python local_runner/nightly.py` chains 5 steps: Railway incremental append → daily cache → 5yr cache → expression cache append → matrix rebuild. Auto-triggered by agent at 4:30pm ET on trading days. ~15-20 min if new data, <1 min if already current.

**compute_series parity: ✅ COMPLETE** — All 88 ops available in backtest_conditions.compute_series(). Historical tiers (T2-T6) have full access to the 4,017 expression library.

**Expression library: ✅ EXPANDED** — 2,541 → 4,017 expressions. 127 boolean conditions (was 65), 88 ops (was 82). 29 categories.

**Expression series cache: ✅ COMPLETE** — 4,119 tickers × 4,017 expressions pre-cached on disk (~21 GB). Build: 37.5 min one-time. Nightly append: 5-8 min. Pyramid grinder auto-detects cache. 14x speedup at equivalent beam width.

**NaN handling: ✅ FIXED** — Expression ranges use 70% example coverage threshold (minimum 3 examples). Maximizes candidate pool — few NaN values (e.g., RSI slope on short-history examples) don't eliminate useful expressions. Previous 100% threshold was too aggressive and reduced candidate pool.

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
├── expression_engine.py     # Expression computation (point + series)
├── backtest_conditions.py   # Series computation for historical scoring
├── backtest_runner.py       # Step 5: parallel signal scan + chart generation for visual review
├── signal_distribution.py   # Parallel signal analyzer: daily counts + per-signal CSV
├── fetch_universe.py        # Universe OHLCV fetcher: full build + incremental append_daily()
├── lsp_detector.py          # Local Structural Peak detection (DTSS-specific)
├── classify_universe.py     # ETF classifier (quarterly, desktop-only)
├── fast_profiler.py         # FastProfiler for rapid example profiling
├── exit_grinder.py          # Step 6: TA exit management on examples (NEW)
├── outcome_grinder.py       # Step 7: outcome signal identification (NEW)
├── presignal_grinder.py     # Step 8: pre-signal refinement (NEW)
└── environment_scorer.py    # Step 9: environment clustering for EV (NEW)

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
| 2 Receive | ✅ Done | 23 examples with LSP data |
| 3 Signal Grind (Phase 1) | ✅ Done | 9 conditions, 0.00% single-day pass rate, 11s at L3 |
| 3 Signal Grind (Phase 2) | ✅ Done | beam=10000, PT=3 → 26 conditions, peak 6/day, 201 signals/5yr, avg 2.1/day |
| 4 Backtest verification | ✅ Done | Signal prevalence + SPY overlay in frontend Historical tab |
| 5 Backtest Runner | ✅ Done | `scripts/backtest_runner.py` — scan + charts + Railway upload |
| **6 Exit Management Grind** | **⬜ Not started** | **NEXT: Brute force TA exit conditions on 23 examples' post-entry bars** |
| **7 Outcome Grind** | **⬜ Not started** | Apply exit conditions to Step 3 signals → OUTCOME SIGNALS |
| **8 Pre-Signal Refinement** | **⬜ Not started** | Grind outcome vs non-outcome on pre-signal expressions → TOTAL SIGNALS |
| **9 Environment Clustering** | **⬜ Not started** | OUTCOME ÷ TOTAL by market regime → EV |

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

### Step 5d: NaN Handling Fix ✅ COMPLETE
- Expression ranges use 70% example coverage threshold (minimum 3 examples)
- Previous 100% threshold was too aggressive — excluded useful expressions where a few examples had NaN
- 70% threshold maximizes candidate pool for the grinder to work with

### Step 5e: Production Grind Parameters ✅ FOUND
- **beam=10000, depth=100, sweep peak-target 2-10** — the production approach
- beam=10000 fully exhausts search space (search self-terminates at ~25-30 levels when no path improves)
- Different peak-target values explore genuinely different paths (different conditions locked at earlier tiers)
- Best result: peak-target=3 → 26 conditions, peak 6/day, 201 signals/5yr, avg 2.1/day, ~5 min runtime
- If all peak targets plateau at similar totals, the expression library is the bottleneck → expand it

### Step 5f: Determinism + Vectorization ✅ COMPLETE
- **Deterministic row sorting:** Rows sorted by (date, ticker) to eliminate process-pool scheduling variance
- **Deterministic tiebreakers:** Beam nodes sorted by (peak, -total_signals, condition_indices)
- **Matmul vectorization:** Pre-screening via (beam × rows) @ (rows × candidates) matmul. OpenBLAS MAX_THREADS=24 parallelizes across all cores. Only top beam_width*10 candidates materialized with exact peak scores.
- **No expansion cap:** Removed `beam_width * 8` cap that caused non-determinism and limited search

### Nightly Automation ✅ COMPLETE
- `python local_runner/nightly.py` — 5-step chain with gate logic
- Agent triggers at 4:30pm ET on weekdays, ~15-20 min total

---

## IMMEDIATE NEXT STEP

**Step 6: Exit Management Grind**

Build `scripts/exit_grinder.py` — brute force ~4,000 post-signal expressions against the DTSS examples' forward paths:

1. Pull post-entry OHLCV bars for each example from 5yr cache (open-ended forward)
2. Build post-signal expression library (~4,032 expressions across 12 categories: move_captured, extension_from_ma, extension_dynamics, ma_reclaim, momentum_reversal, candle_character, volume_character, structural, range_compression, retracement, time, relative_strength)
3. Compute MFE per example (theoretical max — lowest low for shorts)
4. At each forward bar, compute all expressions → build exit candidate matrix
5. For each expression+threshold: find exit bar (first trigger), measure captured move (entry high → exit close in ADR)
6. Score by floor capture efficiency (worst example's captured/MFE), break ties with median
7. Plateau detection for robust parameter regions
8. Output: exit conditions + per-example scoring report

---

## BUILD PLAN — Remaining Steps

### Step 6: Exit Management Grind ⬜
**What:** Brute force ~4,000 post-signal expressions against examples' forward paths. Find exit conditions that reliably capture the most move.
**Input:** Validated examples with entry dates + 5yr OHLCV cache
**Expression library:** ~4,032 post-signal expressions (12 categories: move_captured, extension_from_ma, extension_dynamics, ma_reclaim, momentum_reversal, candle_character, volume_character, structural, range_compression, retracement, time, relative_strength). 256 per-bar expressions × 7 forward windows + boolean aggregations.
**Benchmark:** Entry bar high → exit bar close in ADR.
**Scoring:** Floor capture efficiency (worst example's captured ADR / MFE ADR) as primary. Median as secondary. Hard constraint: every example must capture > 0 ADR. Plateau detection for robustness.
**Re-run:** Designed to re-run as examples grow. More examples → floor metric gains resolution → more aggressive exits become statistically justified.
**Output:** Exit conditions — the TA expression states marking "this move is done." Exit scoring report with per-example breakdown.
**Script:** `scripts/exit_grinder.py` (NEW)

### Step 7: Outcome Grind ⬜
**What:** Split Step 3 signals into OUTCOME signals (move played out like examples) and non-outcome signals.
**Input:** Step 3 signals (~201) + Step 6 exit conditions + validated examples
**How:** Phase 1: Apply exit conditions to all signal post-signal bars. Where they trigger = candidate outcomes. Phase 2: Grind post-signal expression library (delay-insensitive) — examples as positives, all signals as universe. Exit conditions are the starting filter, grinder finds additional shared behavior.
**Output:** OUTCOME SIGNALS — confirmed runners whose post-signal behavior matches the examples.
**Script:** `scripts/outcome_grinder.py` (NEW)

### Step 8: Pre-Signal Refinement Grind ⬜
**What:** Find pre-signal conditions that distinguish outcome signals from non-outcome signals — things Step 3 missed because it compared examples vs the full universe instead of within the signal set.
**Input:** OUTCOME SIGNALS + all Step 3 signals + existing 4,017 expression library
**How:** Standard pyramid grinder. Outcome signals as examples, all signals as universe, pre-signal expressions. Any new conditions found get added to Step 3's condition set.
**Output:** TOTAL SIGNALS (Step 3 conditions + Step 8 conditions). Tighter than original signals. Outcome signals unchanged (they pass by definition). Win rate improves because losers are eliminated without losing winners.
**Script:** `scripts/presignal_grinder.py` (NEW)

### Step 9: Environment Clustering ⬜
**What:** OUTCOME SIGNALS ÷ TOTAL SIGNALS by market environment = win rate per regime = EV.
**Input:** OUTCOME SIGNALS + TOTAL SIGNALS + market context at each signal (SPY regime, breadth, clustering, VIX, etc.)
**How:** Compute market context at each signal. Bucket by environment. Win rate per bucket. Find high-EV environments where big runners cluster.
**Output:** Environment scoring model — which market conditions produce the highest probability of quality moves. Combined with Step 6 exit management, this gives full EV.
**Script:** `scripts/environment_scorer.py` (NEW)

### Future: Loss Reduction (Step 10, optional) ⬜
**What:** Analyze non-outcome signals for early post-signal tells that predict failure.
**Input:** Non-outcome signals from Step 7
**How:** Profile first 1-5 bars after each non-outcome signal. Look for common patterns (gap ups, immediate reclaim, volume failure). Add near-entry management rules.
**Output:** Early management rules that reduce average loss without affecting winners. Improves loss side of EV.

### Second Setup — 3-4DB ⬜
**What:** Run 3-4DB examples (21 already loaded) through the same pipeline.
**Why:** Validates the system is setup-agnostic.

---

## Future (not blocking current work)

| # | Task | Description |
|---|------|-------------|
| 1 | **PRESENTATION_SYSTEM** | Separate system: nightly data updates, signal detection, rank-ordered EV presentation. Consumes outputs of this analysis system. |
| 2 | **Frontend grinder control** | Full grinder workflow from the frontend: set peak-target/beam/depth params, start grind, see results, choose to backtest — all from the UI. Desktop agent just listens for commands. No more CLI. |
| 3 | **Dynamic re-grind** | System re-grinds nightly and adjusts conditions as market evolves. |
| 4 | **HTF setup** | Third setup type. Collect and load examples. |
| 5 | **IPO break setup** | CRWV-style IPO breakout of highest AVWAP. Short history stocks. |
| 6 | **Universal pivot expressions** | Detect all D1 pivots with prominence, generate expressions for spiderweb. |
| 7 | **Intermediate pyramid tiers** | Add 2yr/3yr tiers between 1yr and 5yr for earlier noise elimination. |

---

## Performance — Desktop Benchmarks (i5-12600K)

| Component | Time | Notes |
|-----------|------|-------|
| Daily matrix build | ~5 min (est) | 4,017 tickers × 4,017 expressions, 8 workers |
| Spiderweb grind (L3) | ~11s | beam=50, depth=10, 550K nodes |
| Spiderweb grind (L5) | 1-8 hours | beam=250, depth=15 |
| **Pyramid grinder (beam=10000, cached+matmul)** | **~5 min** | **beam=10000, depth=100. Exhausts search at ~25-30 levels. Best: PT=3 → 26 conds, peak 6/day, 201 signals/5yr** |
| **Pyramid grinder sweep (PT 2-10)** | **~50 min** | **9 runs × ~5 min. Full exploration of parameter space.** |
| Pyramid grinder (beam=200, cached) | ~3 min | beam=200, depth=30. Previous "optimal" — too narrow to exhaust search. |
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
2. **Matmul pre-screening** — (beam × rows) @ (rows × candidates) estimates joint signal counts. OpenBLAS MAX_THREADS=24 across all cores. Only top candidates materialized. Makes beam=10000 feasible in 5 min.
3. **Deterministic search** — Row sorting by (date, ticker), tiebreakers by (peak, -total_signals, condition_indices). Reproducible results across runs.
4. **NaN handling** — 70% example coverage threshold maximizes candidate pool without false negatives.
5. **`_bool_series` lazy eval** — if/elif dispatch instead of dict-literal. ~37% matrix build speedup.
6. **Numpy array serialization** — raw numpy to workers, skip `pd.to_numeric`. 8x faster reconstruct.
7. **Example matrix parallelization** — ThreadPoolExecutor (10 threads) for concurrent example builds.
8. **No bar minimum in cache** — every tradable ticker included regardless of history length.
9. **ProcessPoolExecutor for tier builds** — true CPU parallelism (ThreadPool was GIL-bound at 10% CPU).
