# Setup Analysis System

**The repeatable process for building any setup type into a fully optimized trading playbook.**

**The formula:**

> Best setups × Best markets for those setups × Best management = Highest EV possible

---

## Step 1: Load Data & Knowledge

All OHLCV data already exists in the system. The `universe_ohlcv` table has 5 years of daily data for ~11,000 tickers, updated nightly via `POST /api/universe/append-daily` (incremental — fetches only missing days). The `tradable_universe` subset (~4,100 tickers meeting minimum price and liquidity requirements) is what we scan against. The full nightly pipeline (`python local_runner/nightly.py`) chains: Railway append → daily cache → 5yr cache → expression cache append → matrix rebuild. Runs after 4:30pm ET, ~15-20 min total. Agent auto-triggers if running.

Before starting any setup analysis, load `ta_knowledge.md` for full TA context — extension structures, channel behavior, AVWAP mechanics, market stages, wave cycles. This informs what to look for and how to think about the data. Load `pcf.md` for the complete PCF language reference — every function, operator, and syntax pattern available in TC2000.

**Verify before proceeding:**
- Universe OHLCV is current (most recent trading day present)
- Tradable universe is built and populated
- `ta_knowledge.md` is read and understood
- `pcf.md` is read and understood

---

## Step 2: Receive Setup Examples & Context

The user presents:
- **Setup description** — what the pattern is, what it looks like, why it works
- **Validated examples** — tickers with confirmed entry dates. **Remember: entry date = the day the trade is entered at the open. The scan candle is the trading day BEFORE the entry date.**
- **Any additional TA context** — LSP levels, channel structure, key relationships specific to this setup type

This gives a general starting point to understand what the setup looks like numerically. Ask questions if anything is unclear about the pattern mechanics or what distinguishes a good example from a bad one.

### ⚠️ CRITICAL: Scan Timing — The #1 Rule

**The scan runs AFTER market close the night BEFORE the entry.** The entry happens the next morning at the open. This means:

- **The scan candle = 1 trading day BEFORE the entry date.** If the entry date is Tuesday, the scan ran Monday night using Monday's completed bar.
- **ZERO entry candle data can be used in scan conditions.** The entry candle hasn't happened yet when the scan runs. Its Open, High, Low, Close, Volume — none of it exists at scan time.
- **When analyzing examples:** if the example has `entry_date = 2024-05-22`, all conditions must be tested against the bar for `2024-05-21` (or the prior trading day). The scan is looking for charts that look like the setup **1-2 days before the entry**, not on the entry day itself.

**Every time you write analysis code, verify you are using index `entry_idx - 1` (or the equivalent prior trading day) for all condition checks. Using `entry_idx` is WRONG and will produce conditions that can't work in real-time scanning.**

---

## Step 3: THE GRINDER — Signal Discovery (Pre-Signal Conditions)

**Goal:** Find which expression combinations best discriminate examples from the tradable universe. Get from 100% down to the mathematical ceiling — the tightest pass rate achievable through pure brute-force computation. Then eliminate historical noise until signals/day is manageable.

**This step is fully automated via THE GRINDER, a two-phase desktop search system.**

**Output: SIGNALS** — the set of all ticker-date pairs across 5yr history that pass all conditions. These are the raw signals before any outcome filtering.

**Note:** Step 8 (Pre-Signal Refinement) may add additional conditions back into this step after outcome analysis reveals pre-signal tells that were invisible when comparing examples vs the full universe. When that happens, the signal set is re-run and becomes TOTAL SIGNALS (tighter than the original, but all outcome signals still pass by definition).

### Phase 1: Spiderweb Search (single-day ceiling)

The grinder precomputes two matrices:
1. **Universe matrix** — every tradable ticker (~4,017 after ETF exclusions) × every expression (4,017), evaluated at the most recent bar. Shared across all setups. Auto-rebuilds nightly at 4:30pm ET. Parallelized across 8 CPU cores (~2.8 min on i5-12600K).
2. **Example matrix** — every setup example × every expression, evaluated at the scan candle (day before entry). Per-setup, fast (~5s per example).

The spiderweb search then explores branching combinations of conditions:
- Each node = a set of conditions applied together (AND logic)
- Thresholds derived from example ranges (all examples must pass)
- Score = % of universe that passes (lower = tighter = better)
- Beam search prunes weak paths, explores promising ones deeper

**The ceiling problem:** Phase 1 finds conditions that filter tightly on TODAY (e.g., 0.07% = 3 tickers) but when backtested across 5 years: 340 signals/day. The conditions describe "bull market uptrend" not the specific setup. The spiderweb stops at the single-day ceiling but there are still expressions that could eliminate massive historical noise without losing any examples.

### Phase 2: Pyramidal Grinder (nested time horizon noise elimination)

**✅ COMPLETE — PRODUCTION READY**

`local_runner/pyramid_grinder.py`

**How to run:**
```bash
# Standard run — sweep peak targets 2-10, take best result (~50 min total)
for /L %p in (2,1,10) do python local_runner/pyramid_grinder.py --setup dtss --peak-target %p --beam 10000 --depth 100

# Quick single run (~5 min)
python local_runner/pyramid_grinder.py --setup dtss --peak-target 3 --beam 10000 --depth 100
```

**Best result (2026-02-24, peak-target=3, beam=10000):**
- 26 conditions, peak 6/day, 201 total signals across 5yr, avg 2.1/day (~3.4/month to vet)
- Runtime: ~5 min with expression cache + matmul vectorization

**Key parameters:**
- **beam=10000** — explores 10K paths per level (exhaustive — search terminates when no path improves)
- **depth=100** — up to 100 conditions per tier (search self-terminates when exhausted, typically ~25-30)
- **peak-target=N** — target ≤N signals on any single day. Different values lock different conditions at earlier tiers → different 5yr starting points → explore different solution paths. **Always sweep multiple values and take the best.**

**How it works:** The pyramid progressively widens the historical window, each tier grinding until `peak_signals/day < threshold` before advancing:

1. **D1 (today):** Spiderweb grind on today's snapshot → finds ceiling. Lock conditions.
2. **1 week:** Build matrix from last 5 trading days. Grind until peak/day < threshold. Lock.
3. **1 month:** Matrix from ~21 trading days. Same. Lock.
4. **6 months:** Matrix from ~126 trading days. Same. Lock.
5. **1 year:** Matrix from ~252 trading days. Same. Lock.
6. **5 years:** Matrix from all trading days. Same. Lock.

**Why this is fast:** Expression series cache pre-computes all 4,017 expressions for all 4,119 tickers × 5yr on disk (~21 GB). Tier matrix builds load pre-computed arrays instead of calling compute_series() thousands of times. Matmul pre-screening (beam × rows @ rows × candidates) estimates joint signal counts without materializing all combinations — only top candidates get exact peak scoring. OpenBLAS with MAX_THREADS=24 parallelizes matmul across all cores.

**Why peak-based:** Average 7.4/day sounds fine but hides days with 260 signals. Peak-based guarantee means no single day overwhelms manual review.

**Why sweep peak targets:** Different peak-target values lock different conditions at the 6mo/1yr tiers (which have fewer rows to discriminate), creating different starting points into the 5yr tier. The 5yr tier is where most conditions get added and where the expression library's discriminating power is tested. peak-target=1 is usually too aggressive (prunes everything), peak-target=2-5 tend to produce the best results.

**NaN handling:** Expression ranges require 70% of examples to have non-NaN values (minimum 3). This maximizes the candidate pool — a few NaN values (e.g., RSI slope on short-history examples) don't eliminate useful expressions.

**Determinism:** Row ordering is sorted by (date, ticker) to eliminate process-pool scheduling variance. Beam search tiebreakers use (peak, -total_signals, condition_indices) for reproducible results.

**Constraint:** 100% of setup examples must ALWAYS pass all conditions at every tier (zero false negatives).

**DTSS grind progression (peak-target=3, beam=10000):**
- D1: 4 conditions → 0 tickers passing today
- 1wk-1yr: ~3 conditions added across these tiers
- 5yr: ~19 added → peak 6/day (ceiling at level 26)
- Total: 26 conditions, 201 signals, ~5 min

### Legacy Phase 2: Flat Historical Scorer (deprecated)

The original Phase 2 at `local_runner/historical_scorer.py` targeted average signals/day instead of peak — fundamental design flaw that allowed 260 signals/day spikes. Replaced by pyramid grinder.

### Running the Grinder

**Phase 1:**
1. Load examples into Railway DB with entry dates
2. Set grind level via frontend slider (L1: 30s → L5: 8 hours)
3. Click Start Grind — desktop agent picks up job, runs spiderweb search
4. Review ceiling — the progression chart shows where adding conditions stops improving

**Phase 2 (Pyramid):**
```bash
python local_runner/pyramid_grinder.py --setup dtss --peak-target 5 --beam 200 --depth 30
```
Requires 5yr OHLCV cache + expression series cache + Railway API for examples. Runs all 6 tiers sequentially, outputs `pyramid_results_{setup}.json` + `historical_results_{setup}.json` (compatible with `signal_distribution.py`).

### What the Grinder Produces

**Pyramid output** (`pyramid_results_{setup}.json`):
- All locked conditions with tiers, expression names, categories, and ranges
- Per-tier condition counts and peak/avg stats
- Total signal count and peak/day across full history

**Compat output** (`historical_results_{setup}.json`):
- Same conditions in format compatible with `signal_distribution.py`
- Enables signal chart generation and visual verification

### What the Grinder CAN'T Do

The grinder finds the ceiling of mechanical, single-threshold conditions. It cannot capture:
- Market regime / stage transitions
- AVWAP relationships (institutional flow)
- Algo lines from high-volume candles
- Multi-day pattern shape (it sees one bar at a time)
- Net gamma effects
- Qualitative "this chart looks right" patterns

### Architecture

- `local_runner/matrix_builder.py` — Precomputes universe + example matrices. Universe build parallelized via `ProcessPoolExecutor` (8 workers, `MATRIX_WORKERS` env var configurable).
- `local_runner/spiderweb.py` — Phase 1: beam search tree exploration. Inner loop vectorized with numpy broadcasting + float32 matmul.
- `local_runner/pyramid_grinder.py` — Pyramidal grinder: 6 nested tiers (D1 → 1wk → 1mo → 6mo → 1yr → 5yr). D1 uses SpiderwebSearch, historical tiers use PeakSpiderweb (peak-based scoring). Parallel matrix build per tier via ProcessPoolExecutor. Auto-detects expression series cache for 14x speedup.
- `local_runner/historical_scorer.py` — Legacy Phase 2: greedy historical signal elimination (deprecated, kept for reference).
- `local_runner/grinder.py` — CLI interface
- `local_runner/agent.py` — Desktop polling agent with nightly auto-rebuild (triggers at 4:30pm ET)
- `local_runner/nightly.py` — **✅ BUILT:** Single command nightly update pipeline. Chains: Railway append-daily → daily cache → 5yr cache → expression cache append → matrix rebuild. Stops early if DB is already current. Run manually or auto-triggered by agent.
- `local_runner/cache_builder.py` — OHLCV caches: daily (300 bars, 57 MB) + 5yr (1,260 bars, 214 MB)
- `local_runner/expr_cache_builder.py` — **✅ BUILT:** Pre-cached expression series for all tickers × 5yr. 4,119 tickers × 4,017 expressions, ~21 GB compressed .npz per ticker. `--build` (first-time, ~37.5 min), `--append` (nightly, ~5-8 min), `--status`. Manifest tracks expression fingerprint for auto-invalidation.
- `local_runner/brute_expressions.py` — Expression generator: 4,017 generic expressions (same for all setups)
- `scripts/expression_engine.py` — Computes expressions against OHLCV
- `scripts/backtest_conditions.py` — Series computation for historical scoring. **88 ops** — full parity with expression_engine.py (excluding 8 LSP ops that require injected context). All generic expressions are available to all pyramid tiers.
- `scripts/signal_distribution.py` — Parallel signal analyzer: runs all conditions across 5yr cache, outputs daily signal counts + per-signal CSV. Used to verify peak/avg before advancing.
- `scripts/backtest_runner.py` — **✅ BUILT:** Parallel signal scan + chart generation for visual verification. Loads conditions from pyramid results, scans 5yr cache, generates dark-theme candlestick charts per signal (magenta entry marker, 8/21 EMA + 50/200 SMA). Charts organized by date folder. **Auto-uploads signals to Railway** via `POST /api/backtest/signals/upload` so frontend Historical tab updates automatically. Modes: full run, scan-only (`--no-charts`), charts-only (`--charts-only`). Outputs: `backtest_signals_{setup}.csv`, `backtest_summary_{setup}.txt`, `backtest_charts_{setup}/`, Railway upload.
- `scripts/lsp_detector.py` — Detects Local Structural Peak (highest structural high before scan bar)
- `scripts/classify_universe.py` — ETF classifier (quarterly, desktop-only, ~150 exclusions)
- `scripts/fetch_universe.py` — Universe OHLCV fetcher: full build + incremental `append_daily()` for nightly updates. Batch 40, 3s delay, INSERT OR REPLACE for dedup.
- `server.py` — Grinder API endpoints (jobs/status/progress/results/agent), nightly append-daily endpoint

### Expression library — 100% generic

The grinder uses one universal expression set for all setups. No setup-specific expressions — the pyramid grinder finds setup-specific discrimination by grinding the generic library against 5yr history.

- **Generic set** (`brute_expressions.json`) — 4,017 expressions across 29 categories: near_resistance (203), near_support (133), extension (98), extension_dynamics (91), extension_ceiling (40), extension_adr (6), MA slope (240), MA spread (46), spread_slope (64), slope_ratio (18), MA cross (72), MA stack (7), momentum (138), range (59), range_dynamics (13), retracement (26), swing_structure (48), gap (21), consecutive (4), candle_pattern (39), volume_character (49), volume_continuous (36), bollinger (25), macd (28), aroon (18), efficiency (9), vwap (36), percentile_rank (37), boolean (2,413 from 127 conditions × 19 aggregations each). Used by all setups and the universe matrix.

**Note:** `expression_engine.py` still has LSP compute ops (`lsp_distance`, `lsp_bounce_recovery`, etc.) and `set_lsp_context()`. These are dormant — nothing calls them. They can be repurposed later if universal pivot detection is added to the expression library.

### Expression generation constraints
- Every expression must be valid TC2000 PCF
- ATR = `ATR14` (not AVGT14, AVG14, or AVGT)
- EMA = `XAVGC` (not EAVG or XAVG). e.g. EMA21 = `XAVGC21`
- All thresholds normalized to ATR, ADR, or % — no fixed dollar amounts
- Tradable universe only — `tradable_universe` table (4,167 tickers). Never Universe.txt. Never samples.

---

## Step 4: Backtest & Visual Verification

**Goal:** Confirm the signal grind conditions identify the right pattern across history, not just on the known examples.

**✅ COMPLETE** — `scripts/backtest_runner.py` + Historical tab in frontend.

**Process:**

1. Run conditions across full 5yr history — every trading day, every tradable ticker
2. Generate charts for signals, visual verification that they look like the setup
3. Check signal clustering — signals should cluster around specific dates/periods, not spread evenly
4. Verify all original examples still pass (zero false negatives)
5. User reviews and identifies any signals that are legitimate new examples → add to example library

**Output:** Validated signal set with historical data stored in Railway. Signal prevalence + SPY overlay visualization in frontend Historical tab.

---

## Step 5: Backtest Runner

**✅ COMPLETE**

**Script:** `scripts/backtest_runner.py`

Scans 5yr cache with grind conditions, generates charts per signal, auto-uploads to Railway. Frontend Historical tab shows signal prevalence bar chart + SPY candlestick bubble overlay.

**Usage:**
```bash
# Full run: scan + charts + upload to Railway
python scripts/backtest_runner.py --setup dtss

# Scan + upload only, no charts
python scripts/backtest_runner.py --setup dtss --no-charts

# Regenerate charts from existing CSV
python scripts/backtest_runner.py --setup dtss --charts-only
```

---

## Step 6: Exit Management Grind — How Do the Examples Resolve?

**Goal:** Find the optimal TA-driven exit conditions on the validated examples' post-entry bars. No fixed bar counts, no arbitrary targets — the TA tells us when the move is done.

**This step MUST come before the outcome grind (Step 7) because it defines what "the move played out" looks like.**

### How It Works

- **Input:** The validated examples (e.g., 23 DTSS) with entry dates
- **Data:** Post-entry OHLCV bars for each example, pulled from the 5yr cache. Open-ended — span enough bars to encompass all behavior (let the data tell us, not a predefined number).
- **Method:** Brute force every TA exit condition against the forward price paths of the examples. Find which exit conditions consistently capture the most runway.

### Exit Condition Parameter Space (all technical, open-ended)

| Parameter | Values |
|-----------|--------|
| **MA reclaim exit** | Close above 8 EMA, 12 EMA, 21 EMA, 50 SMA |
| **Extension exhaustion** | Extension below 20 EMA reaches -X ADR then starts contracting |
| **Structural target** | First touch of 50 SMA, 200 SMA, prior swing low |
| **Trail** | Highest close above entry MA, then close below 8 EMA = done |
| **Partial combos** | Take 50% at 50 SMA, trail rest to 200 SMA or MA reclaim |

**No time stops. No fixed R-multiples. No bar count limits.** The exit triggers when the TA condition triggers. If it takes 5 bars, it takes 5. If it takes 50, it takes 50.

### Process

1. For each example's forward path, simulate every TA exit condition
2. Measure: how much of the available move did each exit capture (in ADR)?
3. Rank by captured distance — which exit conditions consistently capture the most runway across all examples?
4. Find robust plateaus — exit strategies that work across the full example set, not just a few outliers

### Output

**Exit conditions** — the TA rules that mark "this move is done." These become the base filter for Step 7: a Step 3 signal only counts as an outcome signal if these exit conditions eventually triggered on its post-signal bars (meaning the move played out the same way as the examples).

### Script: `scripts/exit_grinder.py` (NEW)

---

## Step 7: Outcome Grind — Which Signals Actually Ran?

**Goal:** Split the Step 3 signals into OUTCOME signals (the move played out like the examples) and non-outcome signals (it didn't). Then grind for any additional shared behavior.

### How It Works

**Phase 1: Apply exit conditions as base filter**
- Take all Step 3 signals (e.g., 201 across 5yr)
- Run the Step 6 exit conditions on each signal's post-signal bars
- Signals where the exit conditions trigger = the move happened = **candidate outcome signals**
- Signals where exit conditions never trigger = the move didn't happen = **non-outcome signals**

**Phase 2: Grind for additional post-signal behavior**
- **Universe:** All Step 3 signals (both candidate outcome and non-outcome)
- **Examples:** The validated examples (post-entry bar data)
- **Expressions:** Post-signal expression library — measured relative to the signal bar, looking forward. Delay-insensitive design (see below).
- **Method:** The exit conditions from Step 6 are the starting filter. The grinder then searches for additional post-signal conditions that the examples share, further separating outcome signals from the rest.

**Output: OUTCOME SIGNALS** — the subset of Step 3 signals where the move played out like the examples AND the post-signal behavior matches. These are confirmed runners.

### Post-Signal Expression Library

Expressions measured relative to the signal bar, looking FORWARD. Designed to be **delay-insensitive** — they don't care whether the move started on bar 1 or bar 6 after the signal.

**"Anytime within N bars" expressions** (robust to delayed entry):
- Min close within bars 1-N relative to signal bar close, in ADR multiples
- Max extension below 20 EMA reached within N bars, in ADR multiples
- Did price close below key MAs at any point within N bars? (yes/no)
- Max drawdown from signal bar high within N bars, in ADR

**Cumulative/rolling metrics** (naturally delay-insensitive):
- % of bars with close below signal bar close within N bars
- % of red (down) candles within N bars
- Number of consecutive closes below 8 EMA within N bars
- Down-bar volume sum vs up-bar volume sum within N bars
- Count of lower-lows in bars 1-N

**Structural destination metrics:**
- Bars to first close below 8 EMA / 21 EMA / 50 SMA / 200 SMA
- Did it make lower-high AND lower-low within N bars?
- Extension velocity: ADR per bar of move

**Distance metrics:**
- Total move from signal bar high to lowest low within N bars, in ADR
- Total move in % terms
- Move captured below each MA

### Why Delay-Insensitive Design

A signal might fire Monday night but the move doesn't start until Thursday. "Anytime within N bars" and cumulative expressions don't care — they capture the outcome regardless of exactly when it started. If the move happened, the expressions see it. If it didn't, they don't.

### Script: `scripts/outcome_grinder.py` (NEW)

---

## Step 8: Pre-Signal Refinement Grind — Did Step 3 Miss Anything?

**Goal:** Check if there are pre-signal (scan bar) conditions that distinguish outcome signals from non-outcome signals — conditions that Step 3 couldn't find because it was comparing examples vs the entire 4,000 ticker universe instead of comparing within the signal set.

### How It Works

- **Universe:** All Step 3 signals (the full signal set)
- **Examples:** OUTCOME signals from Step 7
- **Expressions:** The existing 4,017 pre-signal expression library (same as Step 3)
- **Method:** Standard pyramid grinder. Outcome signals as examples, all signals as universe. Find any conditions that predict winners *before the move even starts*.

### Why This Can Find Things Step 3 Missed

Step 3 compared 23 examples vs ~4,000 tickers. That's a very different discrimination problem than comparing outcome signals vs non-outcome signals within an already-filtered set of ~200 signals. A subtle pre-signal tell that's invisible when comparing against the full market may become obvious when comparing within the signal set.

### What Happens With Results

Any new conditions found get **added to Step 3's condition set**. Rerun Step 3 with the expanded conditions to produce **TOTAL SIGNALS**.

**Critical property: TOTAL SIGNALS ≤ original signals, but OUTCOME SIGNALS don't change.** The new conditions only eliminate signals that were non-outcome anyway — the outcome signals pass by definition because they share those conditions with the examples. So the win rate goes up (fewer losers, same winners) without needing to re-run Steps 6-7.

### Output

- **TOTAL SIGNALS** — the tightened signal set (Step 3 conditions + Step 8 conditions)
- **OUTCOME SIGNALS** — unchanged from Step 7
- These two sets are the inputs to Step 9

### Script: `scripts/presignal_grinder.py` (NEW)

---

## Step 9: Environment Clustering — When Does This Setup Work Best?

**Goal:** Find the market environments where the setup produces the highest win rate on quality moves.

### The Math

- **Win rate** = OUTCOME SIGNALS ÷ TOTAL SIGNALS, computed per market environment
- This is win rate on **quality moves only** — signals that ran like the examples. Small wins and scratches are NOT counted as wins. So the win rate will be lower than a traditional backtest but represents real, tradeable runner probability.
- **EV** = (win rate × average captured distance under optimal exit) − (loss rate × average loss)
  - Win rate comes from this step
  - Average captured distance comes from Step 6 exit management
  - Average loss comes from analyzing non-outcome signals (potential Step 10 refinement)

### Process

1. **Compute market context at each signal** — SPY regime (above/below key MAs, extension, trend), breadth metrics, signal clustering density, VIX level, sector rotation state, etc.
2. **Split signals by environment** — for each market context factor, bucket TOTAL SIGNALS into quantile groups
3. **Compute win rate per bucket** — OUTCOME SIGNALS in bucket ÷ TOTAL SIGNALS in bucket
4. **Find the high-EV environments** — which market conditions produce the highest concentration of outcome signals?
5. **Optionally: check if optimal exit varies by environment** — from Step 6, does the best exit strategy differ in bear vs neutral vs bull markets?

### Output

**Environment scoring model** — given tonight's market context, what's the expected win rate and EV? This feeds into the presentation system for nightly signal ranking.

### Script: `scripts/environment_scorer.py` (NEW)

---

## Future: Loss Reduction (Step 10, optional)

**Goal:** Analyze non-outcome signals for common early post-signal behavior that predicts failure. If there's a consistent tell in the first bar or two after the signal fires, add near-entry management rules to cut losses faster and improve the loss side of EV.

**Input:** Non-outcome signals from Step 7 (signals where the move didn't play out)
**Method:** Profile the first 1-5 bars after each non-outcome signal. Look for common patterns — gap ups, immediate reclaim of a key level, volume failure, etc.
**Output:** Early management rules that reduce average loss size without affecting winners.

This is optional and may not be needed if Steps 6-9 already produce strong EV.

---

## Summary

| Step | What | Output |
|------|------|--------|
| 1 | **Load** | Data & TA knowledge — everything is already in the system |
| 2 | **Receive** | User presents examples, entry dates, and setup context |
| 3 | **Signal Grind** | THE GRINDER — pyramid grinder finds pre-signal conditions. Output: **SIGNALS** (~201/5yr for DTSS) |
| 4 | **Backtest** | Visual verification of signals across history |
| 5 | **Backtest Runner** | Charts + Railway upload + Historical tab |
| 6 | **Exit Management Grind** | Brute force optimal TA exits on examples. Output: **EXIT CONDITIONS** |
| 7 | **Outcome Grind** | Apply exit conditions + post-signal behavior matching to signals. Output: **OUTCOME SIGNALS** |
| 8 | **Pre-Signal Refinement** | Grind outcome vs non-outcome on pre-signal expressions. New conditions added to Step 3. Output: **TOTAL SIGNALS** (tighter, same outcome signals) |
| 9 | **Environment Clustering** | OUTCOME SIGNALS ÷ TOTAL SIGNALS by market regime. Output: **EV per environment** |

**The output:** For any setup, the system produces: signal conditions (when to watch) × exit conditions (how the move resolves) × environment scoring (when it works best) = **EV**.

**What the system does NOT do:** Entry. That's discretionary TA — the trader's skill and edge.

**Presentation:** A separate PRESENTATION_SYSTEM handles nightly data updates, signal detection, and rank-ordered EV presentation. That system consumes the outputs of this analysis system.

---

## Reference

- **URL:** web-production-e3025.up.railway.app
- **Key API endpoints:**
  - `GET /api/setups` — list setup types
  - `GET /api/examples/{type}` — list examples for a setup
  - `GET /api/conditions/{type}` — get conditions for a setup
  - `GET /api/ohlcv/local/{type}/{id}` — OHLCV data for an example
  - `GET /api/extension-data/{type}/{id}` — extension analysis
  - `GET /api/tradable` — tradable universe
  - `GET /api/universe/status` — universe data status
  - `GET /api/backtest/summary` — backtest results summary
  - `POST /api/backtest/run` — run backtest
  - `POST /api/backtest/signals/upload` — desktop runner uploads signals per setup_type (replaces existing)
  - `GET /api/backtest/signals/{setup_type}` — get signals for Historical tab (per-setup)
  - `GET /api/chart-image/{type}/{id}` — chart image
  - `GET /docs` — full Swagger API docs
  - **Nightly endpoints:**
  - `POST /api/universe/append-daily` — incremental OHLCV update (checks DB max date vs yfinance latest trading day, fetches only missing days, rebuilds tradable_universe)
  - **Grinder endpoints:**
  - `POST /api/grinder/start` — submit a grind job (setup_type, grind_level)
  - `GET /api/grinder/jobs/pending` — pending jobs for agent pickup
  - `POST /api/grinder/status` — agent posts job status updates
  - `POST /api/grinder/progress` — agent posts progress updates
  - `GET /api/grinder/progress/{job_id}` — frontend polls progress
  - `GET /api/grinder/results/{setup_type}` — get latest results
  - `GET /api/grinder/agent/status` — check if desktop agent is online
  - `POST /api/grinder/agent/heartbeat` — agent heartbeat
- **Infrastructure:** SQLite on Railway persistent volume (/app/data)
- **DB tables:** examples, ohlcv, extension, conditions, signal_analysis, universe_ohlcv, tradable_universe, scan_backtest, scan_backtest_clean, ticker_sectors, backtest_status, ticker_classification, universe_exclusions, backtest_signals
