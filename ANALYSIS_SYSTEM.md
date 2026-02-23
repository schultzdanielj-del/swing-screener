# Setup Analysis System

**The repeatable process for building any setup type into a fully optimized trading playbook.**

**The formula:**

> Best setups × Best markets for those setups × Best management = Highest EV possible

---

## Step 1: Load Data & Knowledge

All OHLCV data already exists in the system. The `universe_ohlcv` table has 5 years of daily data for ~11,000 tickers, updated nightly with the most recent day. The `tradable_universe` subset (~4,100 tickers meeting minimum price and liquidity requirements) is what we scan against. Nothing needs to be fetched from Yahoo Finance — it's all local.

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

**Goal:** Find which expression combinations best discriminate examples from the tradable universe. Get from 100% down to the mathematical ceiling — the tightest pass rate achievable through pure brute-force computation.

**This step is now automated via THE GRINDER, a desktop-based spiderweb search system.**

### How It Works

The grinder precomputes two matrices:
1. **Universe matrix** — every tradable ticker × every expression, evaluated at the most recent bar. Shared across all setups. Auto-rebuilds nightly at 4:30pm ET. Parallelized across 8 CPU cores (~5 min on i5-12600K, cached daily after that.)
2. **Example matrix** — every setup example × every expression, evaluated at the scan candle (day before entry). Per-setup, fast (~5s per example).

The spiderweb search then explores branching combinations of conditions:
- Each node = a set of conditions applied together (AND logic)
- Thresholds derived from example ranges (all examples must pass)
- Score = % of universe that passes (lower = tighter = better)
- Beam search prunes weak paths, explores promising ones deeper

### Running the Grinder

1. **Load examples** — must exist in Railway DB with entry dates
2. **Set grind level** via frontend slider:
   - Level 1: Quick scan (beam=10, depth=5, ~30s)
   - Level 2: Light grind (beam=25, depth=8, ~2 min)
   - Level 3: Medium grind (beam=50, depth=10, ~10 min)
   - Level 4: Heavy grind (beam=100, depth=12, ~30 min)
   - Level 5: Overnight (beam=250, depth=15, ~2-8 hours)
3. **Click Start Grind** — desktop agent picks up job, runs search, posts results
4. **Review ceiling** — the level progression chart shows when adding conditions stops improving. That ceiling is the mathematical limit of brute-force expression stacking.

### What the Grinder Produces

- **Best condition combo** — ranked list of expressions with thresholds
- **Pass rate progression** — how selectivity improves at each depth level
- **Ceiling identification** — the point where adding conditions stops helping

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
- `local_runner/spiderweb.py` — Beam search tree exploration. Inner loop vectorized with numpy broadcasting (batch AND + sum per beam node instead of per-expression Python loop).
- `local_runner/grinder.py` — CLI interface
- `local_runner/agent.py` — Desktop polling agent with nightly auto-rebuild
- `local_runner/cache_builder.py` — OHLCV cache from Railway DB
- `local_runner/brute_expressions.py` — Expression generator (generic + per-setup bespoke blocks)
- `scripts/expression_engine.py` — Computes expressions against OHLCV; supports LSP context injection
- `scripts/lsp_detector.py` — Detects Local Structural Peak (highest structural high before scan bar)
- `server.py` — Grinder API endpoints (jobs/status/progress/results/agent)

### Expression sets — generic vs bespoke

The grinder supports **setup-specific expression sets**. Each setup can extend the generic library with bespoke expressions that only make sense for that pattern. The expression loader is setup-aware:

- **Generic set** (`brute_expressions.json`) — 2,271 expressions across: near_resistance, extension, extension_ceiling, extension_adr, extension_dynamics, MA slope, MA spread, MA cross, MA stack, momentum, range, range_dynamics, retracement, swing_structure, gap, consecutive, candle_pattern, volume_character, bollinger, macd, aroon, efficiency, vwap, boolean. Used by all setups and the universe matrix.
- **DTSS set** (`dtss_expressions.json`) — generic + 19 bespoke `dtss_lsp` expressions (2,290 total). Requires LSP context injected per example. See DTSS bespoke block below.

To add a new setup's bespoke block: add `generate_{setup}_expressions()` and `generate_{setup}()` to `brute_expressions.py`, then add a `setup_type` branch to `_load_expressions()` in `matrix_builder.py`.

### DTSS bespoke expression block — `dtss_lsp` category

DTSS is built around the **Local Structural Peak (LSP)** — the structural high the stock is approaching for the second time (the "left peak" of the double top). All bespoke expressions require LSP context injected via `engine.set_lsp_context(lsp)` before the matrix builder calls `engine.compute()`.

**What LSP context provides:**
- `price` — the price of the structural high
- `bars_lookback` — how many bars back from the scan bar the LSP occurred
- `prominence_score` — how dominant the high is relative to surrounding price action
- `pullback_depth_atr` — how deep the selloff was after the LSP (in ATR units)

**The 19 bespoke expressions:**

| Expression | Op | What it captures |
|---|---|---|
| `lsp_dist_c_atr14` / `adr14` / `pct` | `lsp_distance` (C) | Distance from scan close to LSP price — positive = approaching from below, negative = poked through (breakout failure variant) |
| `lsp_dist_h_atr14` / `adr14` / `pct` | `lsp_distance` (H) | Same but using scan bar high |
| `lsp_bounce_recovery` | `lsp_bounce_recovery` | `(close - post_lsp_low) / (lsp_price - post_lsp_low)` — 0.0 at trough, 1.0 back at LSP. Captures "second rally into same zone." |
| `lsp_right_peak_ratio` | `lsp_right_peak_ratio` | `scan_high / lsp_price` — 1.0 = exactly at LSP, >1.0 = breakout failure, <1.0 = lower high |
| `lsp_vol_ratio_{3,5,10,15,20}d` | `lsp_volume_ratio` | Recent avg volume / volume at LSP bar. Low ratio = volume drying up on approach = bearish confirmation |
| `avwap_lsp_atr14` / `adr14` / `pct` | `avwap_lsp_distance` | AVWAP anchored at LSP bar vs scan close. Negative = price below AVWAP = trapped buyers from the prior rally = short fuel |
| `lsp_bars_back` | `lsp_bars_back` | Raw bar count since LSP |
| `lsp_prominence` | `lsp_prominence` | Structural dominance of the LSP |
| `lsp_pullback_depth_atr` | `lsp_pullback_depth` | Depth of selloff after LSP in ATR — deeper = more convincing structural high |

**How the matrix builder injects LSP for DTSS:**

For each DTSS example, `get_example_matrix()` runs the LSP detector at the scan bar date, constructs an `lsp_context` dict, and passes it to `_compute_ticker_values(lsp_context=...)`, which calls `engine.set_lsp_context(lsp_context)` before evaluating expressions. The universe matrix does NOT use LSP injection — LSP is a post-filter at scan time, not a precomputed universe attribute.

**Validated ranges from 23 DTSS examples (scan bar):**

| Expression | Valid | Median | Range |
|---|---|---|---|
| `lsp_dist_c_atr14` | 21/23 | 0.87 | -1.47 to 7.0 |
| `lsp_bounce_recovery` | 16/23 | 0.856 | 0.17 to 1.37 |
| `lsp_right_peak_ratio` | 21/23 | 0.962 | 0.48 to 1.20 |
| `lsp_vol_ratio_5d` | 16/23 | 0.624 | 0.15 to 1.63 |
| `avwap_lsp_atr14` | 16/23 | +1.97 | -1.89 to 4.48 |
| `lsp_pullback_depth_atr` | 21/23 | 6.01 | 2.55 to 9.38 |

**LSP detector validation:** 22/23 examples returned correct LSP at rank 1. The one apparent failure (BTBT) is a ground truth labeling issue — detector behavior is correct.

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
| 3 | **Grind** | THE GRINDER — 2,271 generic + setup-specific bespoke expressions (DTSS: 2,290 total), spiderweb beam search, desktop compute. Finds the mathematical ceiling of brute-force condition stacking. |
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
- **DB tables:** examples, ohlcv, extension, conditions, signal_analysis, universe_ohlcv, tradable_universe, scan_backtest, scan_backtest_clean, ticker_sectors, backtest_status
