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

**Outcome data:** For every example (and later, every historical signal), precompute forward outcome data from entry using OHLCV data already in the system:
- Max Favorable Excursion (MFE) in ATR units at each bar from entry day 1 through day 30
- Max Adverse Excursion (MAE) in ATR units at each bar from entry day 1 through day 30
- Close-to-close P&L in ATR units at each bar
- Bar-by-bar high, low, close relative to entry price

This precomputed outcome matrix enables exhaustive management optimization in Step 7 — every stop/target/time combination is just a query against these numbers, not a simulation.

This gives a general starting point to understand what the setup looks like numerically. Ask questions if anything is unclear about the pattern mechanics or what distinguishes a good example from a bad one.

### ⚠️ CRITICAL: Scan Timing — The #1 Rule

**The scan runs AFTER market close the night BEFORE the entry.** The entry happens the next morning at the open. This means:

- **The scan candle = 1 trading day BEFORE the entry date.** If the entry date is Tuesday, the scan ran Monday night using Monday's completed bar.
- **ZERO entry candle data can be used in scan conditions.** The entry candle hasn't happened yet when the scan runs. Its Open, High, Low, Close, Volume — none of it exists at scan time.
- **When analyzing examples:** if the example has `entry_date = 2024-05-22`, all conditions must be tested against the bar for `2024-05-21` (or the prior trading day). The scan is looking for charts that look like the setup **1-2 days before the entry**, not on the entry day itself.

**Every time you write analysis code, verify you are using index `entry_idx - 1` (or the equivalent prior trading day) for all condition checks. Using `entry_idx` is WRONG and will produce conditions that can't work in real-time scanning.**

---

## Step 3: THE GRINDER — Automated Expression Discovery

**Goal:** Find which expression combinations best discriminate examples from the tradable universe. Get from 100% down to the mathematical ceiling — the tightest pass rate achievable through pure brute-force computation. Then eliminate historical noise until signals/day is manageable.

**This step is fully automated via THE GRINDER, a two-phase desktop search system.**

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

These are handled in Step 4 (Collaborative Analysis) where human discretion pushes past the grinder's ceiling toward zero daily pass rate.

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
- `scripts/backtest_runner.py` — **✅ BUILT (Step 6):** Parallel signal scan + chart generation for visual verification. Loads conditions from pyramid results, scans 5yr cache, generates dark-theme candlestick charts per signal (magenta entry marker, 8/21 EMA + 50/200 SMA). Charts organized by date folder. **Auto-uploads signals to Railway** via `POST /api/backtest/signals/upload` so frontend Historical tab updates automatically. Modes: full run, scan-only (`--no-charts`), charts-only (`--charts-only`). Outputs: `backtest_signals_{setup}.csv`, `backtest_summary_{setup}.txt`, `backtest_charts_{setup}/`, Railway upload.
- `scripts/lsp_detector.py` — Detects Local Structural Peak (highest structural high before scan bar)
- `scripts/classify_universe.py` — ETF classifier (quarterly, desktop-only, ~150 exclusions)
- `scripts/fetch_universe.py` — Universe OHLCV fetcher: full build + incremental `append_daily()` for nightly updates. Batch 40, 3s delay, INSERT OR REPLACE for dedup.
- `server.py` — Grinder API endpoints (jobs/status/progress/results/agent), nightly append-daily endpoint

### Expression library — 100% generic

The grinder uses one universal expression set for all setups. No setup-specific expressions — the pyramid grinder finds setup-specific discrimination by grinding the generic library against 5yr history.

- **Generic set** (`brute_expressions.json`) — 4,017 expressions across 29 categories: near_resistance (203), near_support (133), extension (98), extension_dynamics (91), extension_ceiling (40), extension_adr (6), MA slope (240), MA spread (46), spread_slope (64), slope_ratio (18), MA cross (72), MA stack (7), momentum (138), range (59), range_dynamics (13), retracement (26), swing_structure (48), gap (21), consecutive (4), candle_pattern (39), volume_character (49), volume_continuous (36), bollinger (25), macd (28), aroon (18), efficiency (9), vwap (36), percentile_rank (37), boolean (2,413 from 127 conditions). Used by all setups and the universe matrix.

**Note:** `expression_engine.py` still has LSP compute ops (`lsp_distance`, `lsp_bounce_recovery`, etc.) and `set_lsp_context()`. These are dormant — nothing calls them. They can be repurposed later if universal pivot detection is added to the expression library.

### Expression generation constraints
- Every expression must be valid TC2000 PCF
- ATR = `ATR14` (not AVGT14, AVG14, or AVGT)
- EMA = `XAVGC` (not EAVG or XAVG). e.g. EMA21 = `XAVGC21`
- All thresholds normalized to ATR, ADR, or % — no fixed dollar amounts
- Tradable universe only — `tradable_universe` table (4,167 tickers). Never Universe.txt. Never samples.

---

## Step 4: Collaborative Analysis — Push Past the Ceiling

**Goal:** Take the grinder's mathematical ceiling and, through human-AI collaboration, add qualitative/discretionary conditions that push selectivity toward zero.

**The grinder finds the floor of what pure math can do. This step adds what the human eye sees that numbers can't capture.**

**Process:**

1. **Review grinder results** — understand the best condition combo, what each expression captures, and where the ceiling sits (e.g., "grinder gets to 2.1%, adding more conditions doesn't help")
2. **Identify what's missing** — look at the tickers still passing. What do the false positives have in common? What distinguishes them from real setups? Common gaps:
   - Market regime (stage transitions, breadth, sector rotation)
   - AVWAP relationships (institutional flow, supply/demand zones)
   - Algo lines from high-volume candles
   - Multi-day price structure / pattern shape
   - Volume character (distribution vs accumulation)
   - Net gamma effects
3. **Add discretionary conditions** — translate the human observation into testable conditions. Some become PCF expressions, others become manual checklist items for the final vet.
4. **Test combined selectivity** — run grinder conditions + new conditions against examples and universe
5. **Iterate** — each round requires explicit user approval

**The goal is zero:** the scan should return nothing most days. When it fires, that's the signal. This is achieved through the combination of grinder conditions (mechanical) + collaborative conditions (discretionary).

**Output:** A complete condition set split into:
- **Scannable conditions** — PCF code for TC2000 automated scanning
- **Manual checklist** — qualitative checks for final human review of scan output

### PCF Output Rules
- Each condition is its own code block for single-click copy
- ATR = `ATR14` (not AVGT14, AVG14, or AVGT)
- EMA = `XAVGC` (not EAVG or XAVG). e.g. EMA21 = `XAVGC21`
- All thresholds normalized to ATR, ADR, or % — no fixed dollar amounts
- **NEVER present a condition backed by a hit rate unless actually tested and verified.**

---

## Step 5: Backtest — Historical Validation

**Goal:** Confirm the conditions identify the right pattern across history, not just on the known examples.

**Process:**

1. **Run the conditions across the full historical universe** — every trading day, every tradable ticker, over multiple years.

2. **Generate charts for a sample of signals** that are NOT in the original example set. Present them to the user.

3. **User reviews:** Do these look like the setup? Every signal should be visually recognizable as the pattern. If signals appear that don't look right:
   - Identify what's different about the bad signals
   - Add or tighten conditions to exclude them
   - Return to Step 5 and re-compose

4. **Check signal clustering:** Signals should cluster around specific dates/periods, not spread evenly. The setup depends on market conditions — if signals fire constantly, the conditions aren't capturing the right thing.

5. **Verify no false negatives on known examples:** All original examples must still pass. If condition changes in this step caused any to fail, resolve before proceeding.

6. **Mine negative examples:** Run the scan conditions but add a forward-looking filter that identifies signals where the pattern failed (e.g., price continued higher instead of breaking down). These are charts that looked like the setup on scan day but didn't work. Review the best-looking failures — they become negative examples. Profile the negatives the same way as Step 3-4 to discover what distinguishes real setups from lookalikes, then add conditions to exclude them.

7. **Grow the example set:** During review, the user may identify signals that are legitimate examples not in the original set. Add these to the example library. More examples = more reliable profiling = tighter conditions. Re-run Steps 3-5 periodically as the example set grows.

**Output:** A validated condition set with historical signal data stored in the system. Confidence that the scan identifies the pattern and not noise.

---

## Step 6: Behavioral Grinder — Confirm Runners

**Goal:** Take the raw grinder signals (e.g., 368) and filter to only the ones that actually produced moves like the validated examples. Uses the same grinder logic but with post-signal expressions and a different universe.

**The system does NOT try to identify exact entry candles or entry prices.** Entry is discretionary — the trader handles that. The system identifies which signals produced big moves worth trading.

### How It Works

- **Universe:** The raw grinder signal set (e.g., 368 ticker-date pairs) — NOT the 4,000 ticker tradable universe
- **Examples:** The validated examples with known entry dates (all confirmed runners with great MFE)
- **Expressions:** A NEW post-signal expression library (see below) — NOT the standard 4,017
- **Method:** Same pyramid grinder logic. Find conditions all examples share, filter the signals to only those matching.

### Post-Signal Expression Library

Expressions measured relative to the signal bar, looking FORWARD. Designed to be **delay-insensitive** — they don't care whether the move started on bar 1 or bar 6 after the signal.

**"Anytime within N bars" expressions** (robust to delayed entry):
- Min close within bars 1-30 relative to signal bar close, in ADR multiples
- Max extension below 20 EMA reached within 30/60 bars, in ADR multiples
- Did price close below 50 SMA at any point within 30 bars? (yes/no)
- Did price close below 200 SMA at any point within 60 bars?
- Max drawdown from signal bar high within N bars, in ADR

**Cumulative/rolling metrics** (naturally delay-insensitive):
- % of bars with close below signal bar close within 30 bars
- % of red (down) candles within 20 bars
- Number of consecutive closes below 8 EMA within 30 bars
- Down-bar volume sum vs up-bar volume sum within 20 bars
- Count of lower-lows in bars 1-30

**Structural destination metrics:**
- Bars to first close below 8 EMA / 21 EMA / 50 SMA / 200 SMA
- Did it make lower-high AND lower-low within 10 bars?
- Extension velocity: ADR per bar of move in the first 20 bars

**Distance metrics (the key ones for confirming runners):**
- Total move from signal bar high to lowest low within 30/60 bars, in ADR
- Total move in % terms
- Move captured below each MA (close at 50 SMA minus close at signal bar, etc.)

### Why Delay-Insensitive Design

A signal might fire Monday night but the move doesn't start until Thursday. The grinder might even flag the same ticker on multiple consecutive nights. "Anytime within N bars" and cumulative expressions don't care — they capture the outcome regardless of exactly when it started. If the move happened, the expressions see it. If it didn't, they don't.

### Output

A filtered signal set — only signals whose post-signal behavior matches the examples. These are **confirmed runners**: signals where the setup triggered AND produced a move with great MFE, just like the examples.

The survivors are NOT just "confirmed entries." They're confirmed entries that RAN.

### Script: `scripts/post_signal_grinder.py` (NEW)

Adapts the pyramid grinder logic:
- Builds example matrix: 23 examples × post-signal expressions, evaluated at the post-signal window
- Builds universe matrix: 368 signals × same expressions, evaluated at same windows
- Runs beam search to find discriminating conditions
- Outputs confirmed_runners.csv

---

## Step 7: Environment Grinder — When Do They Run Biggest?

**Goal:** Cluster confirmed runners against market context to find which environments produce the biggest moves.

**Input:** Confirmed runners from Step 6 + market context at each signal (SPY regime, breadth, signal clustering, etc.)

**Process:**

1. **Distance profiling** — For each confirmed runner, measure total move in ADR, structural destinations reached (50 SMA, 200 SMA), extension levels hit, time to max extension
2. **Factor analysis** — For each market context factor at signal time, split confirmed runners into quantile groups, compare distance outcomes. Which factors predict bigger moves?
3. **Scoring model** — Each factor gets a weight. Nightly signals get scored: expected_distance = base + Σ(weight × factor_value)

**Output:** Scoring model — given tonight's market context, how much runway should we expect?

### Script: `scripts/environment_scorer.py` (NEW)

---

## Step 8: Exit Grinder — When Is the Move Done?

**Goal:** Brute force the optimal technical exit strategy that captures the most of the available move.

**Input:** Confirmed runners from Step 6 + their full forward price paths

**The system does NOT optimize entry or stops.** Entry is discretionary. Stop is the trader's risk management (HOD, LSP level, etc.). The exit grinder only answers: **"When is this move statistically exhausted?"**

### Exit Parameter Space (all technical, no R-multiples)

| Parameter | Values |
|-----------|--------|
| **MA reclaim exit** | Close above 8 EMA, 12 EMA, 21 EMA, 50 SMA |
| **Extension exhaustion** | Extension below 20 EMA reaches -X ADR then starts contracting |
| **Structural target** | First touch of 50 SMA, 200 SMA, prior swing low |
| **Time stop** | If hasn't reached destination in N bars (10, 15, 20, 30) |
| **Trail** | Highest close above entry MA, then close below 8 EMA = done |
| **Partial combos** | Take 50% at 50 SMA, trail rest to 200 SMA or MA reclaim |

### Process

1. For each confirmed runner's forward path, simulate every exit combination
2. Measure: what % of the total available move did each exit strategy capture?
3. Rank by captured distance in ADR (not R-multiples — these are technical exits)
4. Find robust plateaus — exit strategies that consistently capture 60-80%+ of the move
5. Test if optimal exit varies by environment (from Step 7) — e.g., trail to 200 SMA in bear markets, take profit at 50 SMA in neutral markets

### Output

Recommended exit strategy per setup, potentially varying by market environment. Nightly output includes: "This signal has ~8 ADR of runway. Historical optimal exit: trail below 8 EMA after price reaches 50 SMA. Expect to capture ~6 ADR."

### Script: `scripts/exit_grinder.py` (NEW)

---

## Step 9: Nightly Priority Queue

**Goal:** Combine all intelligence into a single ranked output each night.

For each signal the grinder produces tonight:
1. **Score environment** — apply Step 7 scoring model with current market context
2. **Estimate runway** — expected distance in ADR
3. **Attach exit strategy** — from Step 8, which exit rules to use given current environment
4. **Rank by expected runway** — biggest expected moves get priority

**Output format:**
```
Tonight's Signals — DTSS (2026-02-25)
Market: SPY -2.1 ADR below 50 SMA, 4 signals clustered

Rank | Ticker | Expected Move | Exit Strategy           | Confidence
1    | NVDA   | ~9 ADR        | Trail 8EMA after 50 SMA | High
2    | TSLA   | ~7 ADR        | Trail 8EMA after 50 SMA | High  
3    | META   | ~5 ADR        | Take profit at 50 SMA   | Medium
4    | XYZ    | ~4 ADR        | Take profit at 21 EMA   | Low
```

**Capital allocation:** Biggest expected moves get biggest positions. Worth re-entering after stop-outs if runway is large (e.g., 6 paper cuts at 0.5 ADR then catch a 20 ADR runner = +17 ADR net).

### Script: `scripts/priority_scorer.py` (NEW)

---

## Summary

| Step | What | How |
|------|------|-----|
| 1 | **Load** | Data & TA knowledge — everything is already in the system |
| 2 | **Receive** | User presents examples, entry dates, and setup context |
| 3 | **Grind** | THE GRINDER — Phase 1: spiderweb beam search (4,017 generic expressions) finds single-day ceiling. Phase 2: pyramidal grinder eliminates 5yr noise via 6-tier nested search (beam=10000, depth=100, sweep peak-target 2-10). ~5 min/run, ~50 min full sweep. |
| 4 | **Collaborate** | Human-AI iteration to push past the grinder ceiling with qualitative/discretionary conditions. Goal: zero daily pass rate. |
| 5 | **Backtest** | Run conditions across full history, review signals, validate and tighten |
| 6 | **Behavioral Grinder** | Post-signal expressions on examples vs raw signals. Confirms which signals actually ran. Delay-insensitive design. |
| 7 | **Environment Grinder** | Cluster confirmed runners against market context. Score factors that predict bigger moves. |
| 8 | **Exit Grinder** | Brute force optimal technical exits. When is the move statistically done? |
| 9 | **Priority Queue** | Nightly ranked output: expected runway + exit strategy + confidence per signal |

**The output is a complete nightly playbook:** what to watch × did it run × how far will it go × when to get out × how confident to be.

**What the system does NOT do:** Entry. That's discretionary TA — the trader's skill and edge.

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
