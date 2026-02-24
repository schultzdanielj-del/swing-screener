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

This precomputed outcome matrix enables exhaustive management optimization in Step 8 — every stop/target/time combination is just a query against these numbers, not a simulation.

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

**This step is now automated via THE GRINDER, a two-phase desktop search system.**

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

**✅ BUILT & TESTED** — `local_runner/pyramid_grinder.py`

**Test results (DTSS):**
- **Run 1** (2,541 exprs, beam 50): 10 conditions, peak 69/day, avg 7.2/day, ~10 min. Hit expression ceiling.
- **Run 2** (4,017 exprs, beam 50): 20 conditions, peak 91/day, avg 9.7/day, ~40 min. More conditions locked but worse results — larger search space needs wider beam, and 127 boolean conditions cause 4x runtime increase. The 2021 Jul-Aug cluster remains hardest noise to eliminate.

**Expression parity achieved:** All 88 ops in `expression_engine.py` are now available in `backtest_conditions.compute_series()`, giving historical tiers (T2-T6) access to the full 4,017 expression library (127 boolean conditions, expanded from 2,541/65 in Step 5b).

**⚠️ Performance bottleneck:** With 4,017 expressions (2,413 boolean), the pyramid grinder takes ~40 min per run — too slow for iterative tuning. **Next step: pre-cache all expression series to disk** so grind becomes pure search (~2-3 min).

The pyramid progressively widens the historical window, each tier grinding until `peak_signals/day < threshold` before advancing to the next:

1. **D1 (today):** Spiderweb grind on today's snapshot → finds ceiling. ~11s. Lock conditions.
2. **1 week:** Build matrix from last 5 trading days. Grind until peak/day < threshold. ~10-30s. Lock.
3. **1 month:** Matrix from ~21 trading days. Same. ~30s. Lock.
4. **6 months:** Matrix from ~126 trading days. Same. ~30s. Lock.
5. **1 year:** Matrix from ~252 trading days. Same. ~30s. Lock.
6. **5 years:** Matrix from ~1,260 trading days. Same. ~30s. Lock.

**Why this is fast:** Each tier eliminates the cheap noise so the next tier only scores survivors. The expensive 5-year compute only runs against tickers/days that passed all previous tiers. Total: ~2 min instead of 28 min.

**Why peak-based:** Average 7.4/day sounds fine but hides days with 260 signals. Peak-based guarantee means no single day overwhelms manual review. Target: peak < 15 across all 5 years.

**Key implementation:** Same spiderweb code, just different matrix construction per tier. Matrix rows = ticker-day combos for the window. Scoring metric = max(daily_counts) instead of total pass rate.

**Constraint:** 100% of setup examples must ALWAYS pass all conditions at every tier (zero false negatives).

**Why this works:** An expression useless today (doesn't drop 3→2 tickers) might eliminate 300/day of historical noise. RSI 40-65 range might not help when all 3 current tickers are in range, but it kills thousands of historical signals with RSI 80+ or RSI 20-.

### Legacy Phase 2: Flat Historical Scorer (deprecated)

The original Phase 2 is still functional at `local_runner/historical_scorer.py` but will be replaced by the pyramid:
- Greedy forward selection with precomputed numpy masks across full 5yr in one shot
- Achieved avg 7.4/day but peak 260/day (Jul-Aug 2021 cluster)
- 20.6 min runtime on i5-12600K (15 workers, ProcessPool)
- Targeted average signals/day, not peak — fundamental design flaw

### Running the Grinder

**Phase 1:**
1. Load examples into Railway DB with entry dates
2. Set grind level via frontend slider (L1: 30s → L5: 8 hours)
3. Click Start Grind — desktop agent picks up job, runs spiderweb search
4. Review ceiling — the progression chart shows where adding conditions stops improving

**Phase 2 (Pyramid):**
```bash
python local_runner/pyramid_grinder.py --setup dtss --peak-target 15 --beam 50 --depth 10
```
Requires 5yr OHLCV cache + Railway API for examples. Runs all 6 tiers sequentially, outputs `pyramid_results_{setup}.json` + `historical_results_{setup}.json` (compatible with `signal_distribution.py`).

### What the Grinder Produces

**Phase 1 output** (`grinder_results_{setup}.json`):
- Best condition combo — ranked list of expressions with thresholds
- Pass rate progression at each depth level
- Ceiling identification

**Phase 2 output** (`historical_results_{setup}.json`):
- `phase1_conditions`: from spiderweb grind
- `phase2_additions`: expressions added by historical scorer
- `all_conditions`: combined final condition set
- Signals/day reduction at each round

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
- `local_runner/pyramid_grinder.py` — Pyramidal grinder: 6 nested tiers (D1 → 1wk → 1mo → 6mo → 1yr → 5yr). D1 uses SpiderwebSearch, historical tiers use PeakSpiderweb (peak-based scoring). Parallel matrix build per tier via ProcessPoolExecutor. Replaces historical_scorer.py.
- `local_runner/historical_scorer.py` — Legacy Phase 2: greedy historical signal elimination (replaced by pyramid_grinder.py, kept for reference).
- `local_runner/grinder.py` — CLI interface
- `local_runner/agent.py` — Desktop polling agent with nightly auto-rebuild (triggers at 4:30pm ET)
- `local_runner/nightly.py` — **✅ BUILT:** Single command nightly update pipeline. Chains: Railway append-daily → daily cache → 5yr cache → expression cache append → matrix rebuild. Stops early if DB is already current. Run manually or auto-triggered by agent.
- `local_runner/cache_builder.py` — OHLCV caches: daily (300 bars, 57 MB) + 5yr (1,260 bars, 214 MB)
- `local_runner/expr_cache_builder.py` — **✅ BUILT (Step 5c):** Pre-cached expression series for all tickers × 5yr. Stores compressed .npz per ticker in `cache/expr_series/`. Manifest tracks expression fingerprint for auto-invalidation. `--build` for first-time (~40 min, ~52 GB), `--append` for nightly (~5-8 min), `--status` to check. Pyramid grinder auto-detects and uses cache, falls back to compute_series() if missing.
- `local_runner/brute_expressions.py` — Expression generator: 4,017 generic expressions (same for all setups)
- `scripts/expression_engine.py` — Computes expressions against OHLCV
- `scripts/backtest_conditions.py` — Series computation for historical scoring. **88 ops** — full parity with expression_engine.py (excluding 8 LSP ops that require injected context). All generic expressions are available to all pyramid tiers.
- `scripts/signal_distribution.py` — Parallel signal analyzer: runs all conditions across 5yr cache, outputs daily signal counts + per-signal CSV. Used to verify peak/avg before advancing.
- `scripts/lsp_detector.py` — Detects Local Structural Peak (highest structural high before scan bar)
- `scripts/classify_universe.py` — ETF classifier (quarterly, desktop-only, ~150 exclusions)
- `scripts/fetch_universe.py` — Universe OHLCV fetcher: full build + incremental `append_daily()` for nightly updates. Batch 40, 3s delay, INSERT OR REPLACE for dedup.
- `server.py` — Grinder API endpoints (jobs/status/progress/results/agent), nightly append-daily endpoint

### Expression library — 100% generic

The grinder uses one universal expression set for all setups. No setup-specific expressions — the Phase 2 historical scorer finds setup-specific discrimination by grinding the generic library against 5yr history.

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

## Step 6: Market Context — When to Trade It

**Goal:** Identify which market conditions produce winning signals vs losing signals.

**Process:**

1. **Analyze when signals appear** — which market stages, which regimes, which sectors
2. **Correlate with outcomes** — of the historical signals, which periods had the highest success rates
3. **Build the market filter** — conditions or rules that identify "this is a good time to trade this setup"
4. **Test the filter** — does applying it improve win rate without eliminating too many good trades

**Output:** The "when to trade it" overlay — market conditions that must be present for this setup to have edge.

---

## Step 7: EV Optimization — Brute Force Trade Management

**Goal:** Find the absolute best trade management strategy by exhaustively testing every possible combination against precomputed outcome data.

Using the historical signals from Step 7, filtered to the highest-success market conditions:

**The management variable space:**
- **Stop distance:** Every 0.25 ATR increment from 0.25 to 5.0 ATR (20 values), plus MA-based stops (prior day low, entry candle low, each MA from 8 EMA through 50 SMA)
- **Target distance:** Every 0.25 ATR increment from 0.5 to 10.0 ATR (38 values), plus structure-based targets (prior swing low, MA levels)
- **Time stop:** Exit if nothing happened after N days, from 1 to 30 (30 values)
- **Trailing stop type:** Fixed ATR trail at every increment, trail at each MA, step-up trail (move stop to breakeven after 1R, etc.), no trail (~15 variations)
- **Partial exits:** Take half at various R levels and trail rest, full position to target, scale out in thirds (~10 variations)

**Process:**

1. **Test every combination** against the precomputed MFE/MAE outcome matrix for every signal. Each combination is just array math — no simulation needed. Hundreds of thousands of combinations, runs in seconds.

2. **Rank by EV per trade in ATR units.** The universal measure across all setups. Also measure win rate, average winner size, average loser size, max drawdown, profit factor.

3. **Identify the top cluster** — not just the single best, but the region of management parameters that consistently produces high EV. A strategy that's optimal at exactly 1.73 ATR stop but falls apart at 1.74 is fragile. Look for broad plateaus of good performance.

4. **Validate robustness:** The best management strategy should work across different time periods and market conditions, not just on the best-case signals.

**Output:** The complete playbook entry:
- **Setup conditions** (what to scan for — from Step 4)
- **Market conditions** (when to trade it — from Step 6)
- **Management rules** (exact stop, target, trail, time stop, partial rules — from this step)
- **Expected EV per trade** in ATR units
- **Win rate, profit factor, max drawdown** for the chosen management approach

---

## Summary

| Step | What | How |
|------|------|-----|
| 1 | **Load** | Data & TA knowledge — everything is already in the system |
| 2 | **Receive** | User presents examples, entry dates, and setup context |
| 3 | **Grind** | THE GRINDER — Phase 1: spiderweb beam search (4,017 generic expressions) finds single-day ceiling. Phase 2: historical scorer eliminates 5yr noise via greedy selection. |
| 4 | **Collaborate** | Human-AI iteration to push past the grinder ceiling with qualitative/discretionary conditions. Goal: zero daily pass rate. |
| 5 | **Backtest** | Run conditions across full history, review signals, validate and tighten |
| 6 | **Market Context** | Find which market conditions produce winners vs losers |
| 7 | **EV Optimize** | Test management variations, finalize the playbook entry |

**The output is a complete playbook entry:** best setups × best markets × best management = highest EV possible.

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
  - `GET /api/chart-image/{type}/{id}` — chart image
  - `GET /docs` — full Swagger API docs
  - **Nightly endpoints:**
  - `POST /api/universe/append-daily` — incremental OHLCV update (checks DB max date vs yfinance, fetches only missing days, rebuilds tradable_universe)
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
- **DB tables:** examples, ohlcv, extension, conditions, signal_analysis, universe_ohlcv, tradable_universe, scan_backtest, scan_backtest_clean, ticker_sectors, backtest_status, ticker_classification, universe_exclusions
