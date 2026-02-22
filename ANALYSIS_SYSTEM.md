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

## Step 3: Profile — Build the Numerical Fingerprint

**Goal:** Compute thousands of measurements for every example on its scan bar. Build a complete numerical profile of what the setup looks like at the moment it's ripe.

**What gets computed:** Every indicator the PCF language can express, at every reasonable period, in every meaningful combination. This includes:

**Layer 1 — Raw indicators at swept periods:**
- Price primitives: `C`, `O`, `H`, `L`, `V` and offsets (`C1` through `C10`, etc.)
- Moving averages: SMA, EMA, FWMA, HMA of C/H/L/O — periods 5 through 200, every integer
- Rolling MAX/MIN: `MAXH`, `MAXC`, `MINL`, `MINC` — periods 2 through 200
- ATR: periods 2 through 50
- RSI / WRSI: periods 5 through 50
- MACD: common fast/slow combos
- Stochastics: period sweeps
- CCI, ADX, Bollinger Bands, Aroon: period sweeps
- Volume indicators: OBV, BOP with smoothing sweeps
- SUM(V, x) for various periods

**Layer 2 — Derived measurements that encode history into scan-bar values:**
- Distance from MA: `(C - AVGCx) / ATRy`, `(C - XAVGCx) / ATRy`
- Extension: `(MAXHx - AVGCy) / C`, `(MAXHx - AVGCy) / ATRz`
- Pullback depth: `(MAXHx - C) / ATRy`
- Pullback as % of move: `(MAXHx - C) / (MAXHx - MINLy)`
- MA relationships: `AVGCx - AVGCy`, `XAVGCx / XAVGCy`
- MA slope: `AVGCx - AVGCx.y` (SMA now vs y bars ago)
- Range ratios: `MAXHx / MINLy`, `(MAXHx - MINLy) / ATRz`
- Candle shape: `(H - C) / (H - L)`, `(C - L) / (H - L)`, `(H - L) / ATRx`
- Volume ratios: `AVG(V,x) / AVG(V,y)`, `V / AVG(V,x)`
- Price position in range: `(C - MINLx) / (MAXHx - MINLx)`
- Bollinger %b: `(C - BBBOTx) / (BBTOPx - BBBOTx)`
- CountTrue patterns: `CountTrue(C > C1, x)`, `CountTrue(C > XAVGCy, x)`
- SinceTrue patterns: `SinceTrue(C > MAXHx.1, y)` (bars since new high)
- Consecutive patterns: `TrueInRow(C < C1, x)` (consecutive down days)

**Layer 3 — Market context (SPY/QQQ from universe_ohlcv):**
- All Layer 1 and Layer 2 measurements computed for SPY and QQQ on the same scan bar
- Stock-vs-market relative measurements: stock RSI minus SPY RSI, stock extension vs SPY extension, stock pullback depth vs market pullback depth
- Market regime indicators: SPY distance from 50/200 SMA, SPY MA slope, QQQ above/below key MAs
- Correlation: is the stock pulling back WITH the market or AGAINST it

**Layer 4 — Offset comparisons (rate of change of indicators):**
- Indicator now vs N bars ago for key indicators
- Acceleration: second derivative of MAs and key indicators

All measurements are computed for every example on the scan bar. The same measurements are computed for a sample of the tradable universe on the most recent bar (or a representative date).

**Output:** A wide matrix — one row per ticker-date, one column per measurement. Examples and universe samples side by side.

**NEVER present a measurement count or claim the profiling is complete unless you have actually run the computation. No guessing at numbers.**

---

## Step 4: Discover — Find What's Consistent AND Selective

**Goal:** Find which measurements make the examples look the same as each other AND different from everything else.

**Process:**

1. **Consistency check:** For each measurement, look at the range of values across all examples. Compute the spread (max - min) relative to the full universe distribution. Tight clustering across examples = consistent feature.

2. **Selectivity check:** For each consistent feature, find what percentage of the tradable universe falls within the example range. Low percentage = highly selective. A feature where all examples cluster between 2.1 and 3.4 but only 2% of the universe falls in that range is gold.

3. **Combined score:** Rank every measurement by consistency × selectivity. The best features are tight across examples AND rare in the universe.

4. **Threshold extraction:** For each top feature, derive the tightest threshold that passes 100% of examples. This comes directly from the example min/max for that measurement, with a small buffer.

**Output:** A ranked list of the top 50-100 most discerning features, each with:
- What it measures (human-readable description)
- The range across examples
- What % of the universe it filters out
- The derived threshold and direction (> or <)

Present this to the user. This is the "here's what your examples have in common that's unique" report.

---

## Step 5: Compose — TA-Validated Condition Building

**Goal:** Using TA knowledge, interpret the top features from Step 4 and compose them into a coherent, meaningful set of PCF scan conditions.

**Process:**

1. **Group by concept:** Cluster the top features by what they're actually describing in TA terms. Many measurements will be capturing the same underlying characteristic from different angles. Groups might include:
   - Prior extension (how far price ran before pulling back)
   - Pullback depth and character
   - Bounce weakness (failed recovery)
   - Trend context (MA structure, slope)
   - Volume behavior
   - Candle/bar characteristics

2. **Select best representative per group:** Within each group, pick the single most selective measurement. Multiple conditions capturing the same concept add complexity without adding filtering power.

3. **Validate with TA knowledge:** For each selected condition, confirm it makes sense in the context of `ta_knowledge.md` and the setup description. If a condition is highly selective but makes no TA sense, flag it as potential overfitting — discuss with user.

4. **Test the combination:** Run all selected conditions together against the examples (must pass 100%) and the universe. Check the result count.

5. **Iterate:** If results are too many, add more conditions from the ranked list. If zero examples fail, tighten thresholds. If examples start failing, back off.

**Output:** A set of 8-15 PCF conditions, each with:
- The PCF code (copy-paste ready)
- What it captures in TA terms
- Its individual selectivity (what % of universe it eliminates)
- The combined result count when all conditions are applied

### PCF Output Rules
- Each condition is its own code block for single-click copy
- ATR = `ATR14` (not AVGT14, AVG14, or AVGT)
- EMA = `XAVGC` (not EAVG or XAVG). e.g. EMA21 = `XAVGC21`
- All thresholds normalized to ATR or price — no fixed dollar amounts
- **NEVER present a condition backed by a hit rate (e.g. "26/26" or "92%") unless you have actually tested it against the data and verified the number.**

---

## Step 6: Validate — Historical Stress Test

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

## Step 7: Market Context — When to Trade It

**Goal:** Identify which market conditions produce winning signals vs losing signals.

**Process:**

1. **Analyze when signals appear** — which market stages, which regimes, which sectors
2. **Correlate with outcomes** — of the historical signals, which periods had the highest success rates
3. **Build the market filter** — conditions or rules that identify "this is a good time to trade this setup"
4. **Test the filter** — does applying it improve win rate without eliminating too many good trades

**Output:** The "when to trade it" overlay — market conditions that must be present for this setup to have edge.

---

## Step 8: EV Optimization — Brute Force Trade Management

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
- **Setup conditions** (what to scan for — from Step 5)
- **Market conditions** (when to trade it — from Step 7)
- **Management rules** (exact stop, target, trail, time stop, partial rules — from this step)
- **Expected EV per trade** in ATR units
- **Win rate, profit factor, max drawdown** for the chosen management approach

---

## Summary

| Step | What | How |
|------|------|-----|
| 1 | **Load** | Data & TA knowledge — everything is already in the system |
| 2 | **Receive** | User presents examples, entry dates, and setup context |
| 3 | **Profile** | Compute thousands of measurements for every example — build the numerical fingerprint |
| 4 | **Discover** | Find which measurements are consistent across examples AND rare in the universe |
| 5 | **Compose** | Use TA knowledge to interpret top features, group by concept, build PCF conditions |
| 6 | **Validate** | Run conditions across history, review non-example signals, tighten if needed |
| 7 | **Market Context** | Find which market conditions produce winners vs losers |
| 8 | **EV Optimize** | Test management variations, finalize the playbook entry |

**The output is a complete playbook entry:** best setups × best markets × best management = highest EV possible.

---

## Build TODO

The system described above requires these components to be built. Work them in order — each one depends on the previous.

| # | Component | Status | Description |
|---|-----------|--------|-------------|
| 1 | **Profiling Engine** | ✅ DONE | `scripts/profiling_engine.py` — computes 506 numeric measurements per ticker-date across 4 layers. Layer 1: raw indicators (MA/EMA/FWMA/HMA sweeps, ATR, RSI, MACD, Stoch, CCI, ADX, BB, Aroon, BOP, OBV). Layer 2: derived (extension, pullback, MA slopes/spreads, vol ratios, candle shape, CountTrue/SinceTrue/TrueInRow, ROC, CMF, Kaufman, VWAP dist, Elder, PPO/PVO, Williams %R, hist vol). Layer 3: SPY/QQQ context + relative measures. Layer 4: indicator rate of change. Uses `/api/ohlcv/bulk` with chunked fallback. ~1.6s per ticker. |
| 2 | **Discovery Engine** | ✅ DONE | `scripts/discovery_engine.py` — scores every numeric feature for consistency (example spread / universe IQR) × selectivity (% of universe in example range). Product-of-ranks scoring. Extracts thresholds (direction + value) that pass 100% of examples. Features auto-classified into TA concept groups (extension, pullback, MA distance, volume, momentum, etc.). Output: `DiscoveryReport` with ranked features, grouped view, JSON export, and human-readable summary. Can run standalone or use pre-computed DataFrames. |
| 3 | **Outcome Precomputation** | ✅ DONE | `scripts/outcome_engine.py` — computes forward outcome matrix for any signal. Per bar: MFE, MAE, close P&L, H/L/C vs entry, running best/worst — all in ATR units, sign-adjusted for direction. Configurable forward window (default 60 bars). Batch modes for examples and backtest signals. `outcomes_to_matrix()` converts to numpy arrays for fast management optimization. DB storage via `signal_outcomes` table. CLI: `python -m scripts.outcome_engine {setup} {examples|backtest|single}`. |
| 4 | **Management Optimizer** | NOT STARTED | Exhaustive sweep of all stop/target/trail/time/partial combinations against precomputed outcome data. Ranks by EV, identifies robust parameter plateaus. |
| 5 | **API & DB Integration** | NOT STARTED | Wire engines into Railway app. Store profiling results, discovery results, outcome data in DB. Endpoints to re-run each step. |

**Next step: Build the Management Optimizer (#4).**

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
