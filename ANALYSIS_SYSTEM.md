# Setup Analysis System

**The repeatable process for building any setup type into a fully optimized trading playbook.**

**The formula:**

> Best setups × Best markets for those setups × Best management = Highest EV possible

---

## Step 1: Load Data & Knowledge

All OHLCV data already exists in the system. The `universe_ohlcv` table has 5 years of daily data for ~11,000 tickers, updated nightly with the most recent day. The `tradable_universe` subset (~4,100 tickers meeting minimum price and liquidity requirements) is what we scan against. Nothing needs to be fetched from Yahoo Finance — it's all local.

Before starting any setup analysis, load `ta_knowledge.md` for full TA context — extension structures, channel behavior, AVWAP mechanics, market stages, wave cycles. This informs what to look for and how to think about the data.

**Verify before proceeding:**
- Universe OHLCV is current (most recent trading day present)
- Tradable universe is built and populated
- `ta_knowledge.md` is read and understood

---

## Step 2: Receive Setup Examples & Context

The user presents:
- **Setup description** — what the pattern is, what it looks like, why it works
- **Validated examples** — tickers with confirmed entry dates
- **Any additional TA context** — LSP levels, channel structure, key relationships specific to this setup type

This gives a general starting point to understand what the setup looks like numerically. Ask questions if anything is unclear about the pattern mechanics or what distinguishes a good example from a bad one.

---

## Step 3: Find PCF Conditions That Match ALL Examples

**Goal:** Use PCF conditions to find charts that look close to the exact shape of all validated setups — nothing else. The scan should surface charts that visually match the setup pattern and filter out everything that doesn't.

**Don't care about stops at this stage.** Stop placement, risk sizing, and trade management come later in Steps 6-7. This step is purely about identifying the right chart shape.

Write PCF conditions that:
1. **Pass every single validated example** — zero false negatives
2. **Produce a small enough result set** when scanned against only the most recent day of the tradable universe (not historical — just today's data). This simulates what the nightly scan would return.

Rules:
- All conditions are **normalized for ATR or ADR** — no fixed dollar or percentage thresholds. This ensures the scan catches correctly shaped charts regardless of price level.
- **Eliminate duplicate ETFs** — use the underlying stock. Don't trade inverse ETFs.
- **Ask about biotech exclusions** — sometimes we exclude them (binary overnight risk), sometimes not. Depends on the setup type.
- Test each condition against ALL examples with real OHLCV data before proposing it.
- **NEVER present a PCF condition backed by a hit rate (e.g. "26/26" or "92%") unless you have actually tested it against the data and verified the number.** No guessing, no estimating, no rounding. If you haven't run the test, don't claim a hit rate.

---

## Step 4: Iterate Conditions to Reduce Results

Keep trying new conditions and combinations of conditions to produce tighter scans. Each iteration should reduce the result count.

**Target: fewer than 100 results for a single day scan.**

Present each iteration with:
- What the new/modified condition does
- How many examples still pass (must remain 100%)
- How many scan results it produces
- Which results got eliminated vs kept

---

## Step 5: Collaborate on Advanced Filtering

Beyond simple PCF filtering, use more sophisticated analysis techniques to narrow results further. This is collaborative — propose ideas, discuss what works, iterate together.

Techniques may include:
- Scoring and ranking signals by multiple factors
- AVWAP positioning analysis
- Cluster/correlation analysis (same sector firing = one idea, not five)
- Volume profile characteristics
- Extension structure positioning
- Any other edge identified in the data

**Goal: the tightest possible scan conditions with the least noise.**

---

## Step 6: Store Conditions & Run Historical Analysis

Once the condition list is tight enough:
1. **Store the final conditions** in the system for that setup type
2. **Backtest across history** — run the conditions against the full tradable universe over multiple years
3. **Analyze when and where** these setups show up — which market stages, which regimes, which sectors
4. **Identify the highest-success market conditions** for this setup type

This reveals the relationship between the setup and the broader market environment.

---

## Step 7: EV Optimization

Using the historical signals from Step 6, filtered to the highest-success market conditions:

1. **Test trade management variations** — different stop types, exit types, time stops
2. **Find the combination** of conditions + market filter + management that produces the best win rate AND profit per trade
3. **Measure EV per trade in ATR units** — the universal measure across all setups

This is where the playbook entry gets finalized:
- **Setup conditions** (what to scan for)
- **Market conditions** (when to trade it)
- **Management rules** (how to manage the trade)
- **Expected EV** (what to expect per trade)

---

## Summary

| Step | What Happens |
|------|-------------|
| 1 | Load data & TA knowledge — everything is already in the system |
| 2 | User presents examples, entry dates, and setup context |
| 3 | Find PCF conditions matching ALL examples, scan tradable universe |
| 4 | Iterate conditions to get under 100 results per day |
| 5 | Collaborate on advanced filtering to get the tightest scan possible |
| 6 | Store conditions, backtest history, find best market conditions |
| 7 | Optimize EV through management grid search in best conditions |

**The output is a complete playbook entry:** best setups × best markets × best management = highest EV possible.

---

## Reference: TC2000 PCF Syntax

- **ATR** = `ATR14` (NOT AVGT14, AVG14, or AVGT)
- **EMA** = `XAVGC` (NOT EAVG or XAVG). e.g. EMA21 = `XAVGC21`
- **Always present each PCF condition as its own individual code block** so the user can single-click copy each one. Never inline PCF in prose or combine multiple conditions in one block. Each condition = its own code block.

## Reference: ScanPerfect App

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
