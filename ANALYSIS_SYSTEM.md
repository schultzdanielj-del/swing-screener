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

## Step 3: Profiler — PCF Expression Discovery

**Goal:** Find which PCF expressions best discriminate examples from the tradable universe. Get from 100% down to single-digit selectivity with ranked, ready-to-use PCF conditions.

**This is a 10-step process. Steps 9-10 loop with user approval.**

### 3.1 Read the setup description and examples
Understand what the pattern looks like the day before entry. What's the TA thesis? What should be true on the scan candle?

### 3.2 Read TA knowledge
Load `ta_knowledge.md`. Map the setup to TA concepts — extension structures, market stages, channel behavior, MA relationships, volume patterns. These concepts drive what expressions to generate.

### 3.3 Collect setup-specific data
Load any metadata unique to this setup type. For DTSS: LSP data (`data/dtss_lsp_data.json`). For other setups: whatever anchoring data exists. This data informs rule design and validates that generated expressions capture the right thing.

### 3.4 Define generation rules grounded in TA concepts
**Rules are NOT random feature combinations.** Each rule captures a specific TA concept relevant to the setup. Example rules for DTSS:
- **Near resistance** — price close to prior highs (MAXH at various periods proxies for LSP)
- **Extended above MAs** — distance from 50 SMA, 200 SMA, EMAs in ADR multiples
- **MA structure** — stacking, slopes, spreads confirming the uptrend
- **Momentum stalling** — ROC declining, RSI extreme, volume drying up, ADX rolling
- **Range position** — where price sits in its channel

Rules are specific to each setup type. The user reviews the rules, not the individual expressions.

### 3.5 Map rules to PCF primitives
Each rule generates expressions automatically: base indicators × periods × normalizers (ATR, ADR, %). Every expression must be valid TC2000 PCF syntax. No Python-only calculations.

### 3.6 Budget the compute
**Hard constraint: 5 minutes for full tradable universe (4,167 tickers).**
- Benchmark base indicator computation per ticker
- Benchmark expression generation per ticker
- If under budget, add more rules/periods/normalizers
- If over budget, trim the least valuable (by TA relevance, not randomly)
- **Every number presented must come from actual benchmarks, not estimates.**

### 3.7 Save the expression config per setup type
Expression rules are saved as a config file specific to this setup (e.g., `data/dtss_expression_rules.json`). Different setups generate different expressions. The profiler reads this config and generates accordingly.

### 3.8 Run the profiler
Compute all expressions for:
- Every example on its scan candle (day before entry)
- Every tradable universe ticker on its most recent bar

Rank expressions by discrimination power: example pass rate vs universe pass rate. Output top discriminators in PCF syntax.

### 3.9 Present results. STOP. Wait for user go-ahead.
Show the ranked discriminators with:
- PCF expression
- What it captures in TA terms
- Example pass rate
- Universe pass rate (selectivity)
- Direction and threshold

**Do not proceed without explicit user approval.**

### 3.10 Layer and iterate
Take top discriminators, combine into multi-condition logic. Test combined selectivity. Present results. **STOP. Wait for user go-ahead.** Repeat this step as many times as the user chooses. Each individual attempt requires explicit approval.

### Expression generation constraints
- Every expression must be valid TC2000 PCF
- ATR = `ATR14` (not AVGT14, AVG14, or AVGT)
- EMA = `XAVGC` (not EAVG or XAVG). e.g. EMA21 = `XAVGC21`
- All thresholds normalized to ATR, ADR, or % — no fixed dollar amounts
- **NEVER present a number (expression count, selectivity, pass rate) unless actually computed from data.**

### Tools
- **FastProfiler** (`scripts/fast_profiler.py`) — cached OHLCV, concurrent fetch, fast computation
- **Tradable universe only** — `tradable_universe` table (4,167 tickers). Never Universe.txt. Never samples.
- **LSP data** — `data/{setup}_lsp_data.json` for setup-specific metadata
- **PCF reference** — `pcf.md` for syntax validation

---

## Step 4: Collaborative Analysis — Refine to Scan-Ready Conditions

**Goal:** Take the profiler's ranked discriminators and, through iterative human-AI collaboration, build the tightest possible multi-condition scan.

**Process:**

1. **Review profiler output together** — user evaluates which discriminators make TA sense vs which are noise or overfitting
2. **Compose conditions** — combine selected discriminators into multi-condition PCF logic
3. **Test combined selectivity** — run all conditions together against examples and universe
4. **User decides next move** — tighten thresholds, add conditions, remove conditions, try different combinations
5. **Repeat** — each iteration requires explicit user approval. Continue until user is satisfied with selectivity vs false negative tradeoff.

**Output:** A set of PCF conditions, each copy-paste ready for TC2000, with tested selectivity numbers.

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
| 3 | **Profile** | Generate PCF expressions from TA-grounded rules, rank by discrimination power (10 sub-steps, loops with user approval) |
| 4 | **Collaborate** | Human-AI iteration to compose, test, and tighten PCF scan conditions |
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
- **Infrastructure:** SQLite on Railway persistent volume (/app/data)
- **DB tables:** examples, ohlcv, extension, conditions, signal_analysis, universe_ohlcv, tradable_universe, scan_backtest, scan_backtest_clean, ticker_sectors, backtest_status
