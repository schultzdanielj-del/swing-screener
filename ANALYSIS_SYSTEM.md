# Setup Analysis System

**Purpose:** Automated methodology for maximizing expected value (EV) per trade for any swing trading setup type (3-4DB, DTSS, HTF, etc.) once PCF scan conditions and validated examples exist.

**The goal is EV in ATR units.** Every decision — scan conditions, filters, scoring, trade management — is measured by its impact on EV per trade. A setup with +0.47 ATR EV means that on a $50 stock with $2 ATR, the average trade nets $0.94. Over 100 trades that's $94 per share traded, regardless of win rate.

**Last updated:** 2026-02-20

---

## Overview

The system operates in two layers:

1. **Layer 1 — TC2000 PCF Scan:** Hard mathematical conditions that reduce 4,000+ tickers to 10-30 daily results. This is what runs nightly.
2. **Layer 2 — Backend Scoring:** Filters and scores that TC2000 cannot compute (AVWAP, cluster analysis, sector dedup, multi-factor scoring). This runs on top of Layer 1 results.

The optimization pipeline takes raw scan results through a repeatable process to identify which filters improve profit factor without killing validated winners.

---

## Step 1: Universe Preparation

### 1a. Tradable Universe

Starting universe: all tickers with 5-year OHLCV data in `universe_ohlcv` table (~11,000).

**Hard filters (applied once, rebuilt periodically):**

| Filter | Threshold | Rationale |
|--------|-----------|-----------|
| Price floor | Last close ≥ $1.00 | Eliminates penny stocks |
| Liquidity floor | 20-day avg dollar volume ≥ $5M | Ensures tradable size |

Result: ~4,100 tradable tickers stored in `tradable_universe` table.

Endpoint: `POST /api/tradable/rebuild`

### 1b. Sector Exclusions

Applied during analysis, not stored in tradable universe (sector lookup is slow for 4K tickers).

| Exclusion | Reason |
|-----------|--------|
| **Healthcare / Biotech** | Binary overnight risk (FDA, trial data). Never short overnight. |

Additional exclusions may be added per setup type.

### 1c. Leveraged ETF Deduplication

Many leveraged/inverse ETFs track the same underlying. When both fire, they represent one trade idea, not two.

**Maintained mapping:**

```
# Gold
JNUG → GDXJ, NUGT → GDX, GDXU → GDX, UGL → GLD, DGP → GLD, GLTR → GLD, ASMG → GDX

# Silver
SLVR → SLV, SLVP → SLV, SHNY → SLV, PSLV → SLV, SIVR → SLV, SILJ → GDXJ

# Single-stock leveraged
LLYX → LLY, MULL → MU, MUU → MU, INTW → INTC, RKLX → RKLB, SOXL → SOXX

# Inverse
MSTZ → MSTR, SMST → MSTR, KOLD → UNG
```

**Rule:** If the underlying OR another leveraged version fires on the same day, keep only the underlying (or the non-leveraged ETF if no underlying fires).

### 1d. Correlated Sector Grouping

Tickers in the same sector/theme firing on the same day = one trade idea.

**Groups:**

| Group | Tickers |
|-------|---------|
| gold_miners | CDE, EGO, EXK, GORO, LAR, MUX, NGD, TGB, USAS, USAU, SGML, ELE, HYMC, VGZ, WRN, ALM, SKE, TRX, GDX, GDXJ, GDMN |
| silver_miners | AG, ASM, SVM |
| gold | GLD |
| silver | SLV |
| uranium | DNN, UEC, UUUU, URNJ, URNM |

**Rule:** Per date, per group → pick one representative (prefer actual stock over ETF).

This mapping is setup-agnostic and applies to all scan types.

---

## Step 2: Run Scan & Collect Signals

For each setup type, run the PCF conditions against the tradable universe for the lookback period.

```
POST /api/scan/{setup_type}?days=77
GET /api/scan/{setup_type}/results
```

Output: list of signals with ticker, date, close, ATR, retracement, extension metrics.

---

## Step 3: Optimize Trade Management (Exit Rules)

Trade management is tested BEFORE scoring filters, because the exit rules define what a "winner" and "loser" actually are. Different management changes EV dramatically — the same scan can range from -0.14 to +0.47 ATR per trade depending on stops and exits.

### 3a. Management Grid Search

For each setup type, test all combinations of:

**Stop loss types:**
| Type | Description |
|------|-------------|
| `entry_high` | Close above entry candle high, checked from day 1 |
| `entry_high_Nd` | Close above entry candle high, only checked after N-day grace period |
| `atr_stop_X` | Close above entry close + X × ATR |

**Exit types:**
| Type | Description |
|------|-------------|
| `ema8` | First close above 8 EMA |
| `ema12` | First close above 12 EMA |
| `ema21` | First close above 21 EMA |
| `atr_target_X` | Low touches entry close - X × ATR (for shorts) |
| `trail_Nd_high` | Close above N-day highest high |

**Time stops:** None, 5d, 10d, 15d

### 3b. P&L Calculation

For shorts:
- **Stop hit:** P&L = -(stop price - entry close) / ATR (negative)
- **Exit signal:** P&L = (entry high - exit close) / ATR (positive if exit < entry high)
- **Time stop:** P&L = (entry high - close on time stop day) / ATR

All P&L measured from entry candle high (the stop level) to exit close, in ATR units. This means P&L includes the risk from entry close to entry high — no free rides.

### 3c. Select Best Management

Rank all combinations by **EV per trade (avg P&L in ATR)**. Secondary sort by profit factor, then by fewer average hold days (capital efficiency).

**The winning management becomes the fixed exit ruleset for that setup type.** All subsequent filter analysis uses this management to compute outcomes.

### 3d. Key Finding: EMA Exits >> Fixed Targets

From 3-4DB analysis:
- EMA exits adapt to the stock's actual momentum shift
- Fixed ATR targets (1.5+) are negative EV — too few trades reach them
- Trail stops hold too long and give back gains
- Grace periods on stops improve EV by letting the trade "settle" before enforcing the stop

---

## Step 4: Score Forward Performance & Compute Signal Characteristics

Using the selected management from Step 3, score every signal with actual P&L (not theoretical max drop). Then compute signal attributes for filter analysis:

For each signal, compute attributes that might predict winners vs losers:

### Continuous metrics
- **Retracement %** — how much of the peak-to-trough move has been retraced
- **Bars from peak** — how many bars since the 15-day high
- **Volume ratio** — signal day volume / 20-day average
- **Extension (ATRs above SMA50)** — how far price is from the 50 SMA
- **% above SMA50** — same as above in percentage terms
- **Average range last 3 bars** — in ATR units (tightness measure)
- **Up days in last 3** — count of green candles
- **SMA50 slope (10-day %)** — trend strength

### Boolean metrics
- **Red candle** — close < open on signal day
- **Close above/below EMA8**
- **Close above/below EMA21**
- **EMA8 above/below EMA21** — MA crossover state
- **Below breakout AVWAP** — optimized AVWAP anchored near peak

### AVWAP computation
1. Find pivot high in 35-bar lookback
2. Test anchor points: peak - 2 to peak + 3 bars
3. Compute AVWAP for each anchor using (H+L+C)/3 * Volume cumsum
4. Select anchor producing highest AVWAP on signal day
5. Compare close vs AVWAP

### Meta-signals (not per-ticker)
- **Cluster size** — total raw signals on the same date
- **Deduped group count** — unique trade ideas on the same date

---

## Step 5: Analyze for Edge

Using actual P&L from Step 3's management rules (not theoretical max drop), find which signal characteristics predict higher EV.

### 5a. Top 30% vs Bottom 70% (by profit)

Sort all signals by `drop5` descending. Compare means of all characteristics between top 30% (best shorts) and bottom 70%.

**Look for:** Large deltas in any metric. These indicate what distinguishes the most profitable setups.

### 5b. Winners vs Losers

Split by `win` (drop ≥ 1 ATR in 5 days). Compare means and rates.

**Look for:** The sharpest statistical separators — metrics where winners and losers diverge most.

### 5c. Bucketed Win Rates

For each continuous metric, bucket into ranges and compute win rate per bucket.

**Look for:**
- Buckets with win rate significantly above or below baseline
- "Kill zones" — buckets where win rate drops below 35% (hard avoid)
- "Sweet spots" — buckets where win rate exceeds 60%

### 5d. Combined Filter Testing

Test combinations of promising filters. Compute for each combination:
- Win rate
- Signal count
- Average drop (profit)
- Average bounce (risk)
- Profit factor = total gains / total adverse moves

---

## Step 6: Validate Against Known Winners

**CRITICAL STEP — do not skip.**

Before adopting any filter, test it against all validated example setups for that setup type.

```
GET /api/examples/{setup_type}
```

For each proposed filter, compute: **what % of validated winners pass this filter?**

| Threshold | Action |
|-----------|--------|
| ≥ 90% pass | Safe to add as hard PCF condition |
| 70-90% pass | Use as soft scoring factor, not hard filter |
| < 70% pass | **REJECT** — filter is a false positive from sample bias |

### Known false positive example (3-4DB):

"Close below EMA21" showed strong edge in scan data (100% of losers above EMA21) but only 19% of validated winners passed it. The statistical edge was driven by one large sector cluster, not a universal pattern.

---

## Step 7: Implement Changes

### Hard filters (Layer 1 — PCF)

Only add conditions that:
- Pass ≥ 90% of validated winners
- Eliminate a bucket with ≤ 35% win rate
- Can be expressed in TC2000 PCF syntax

Update scan script and generate PCF code block for TC2000.

### Soft scoring (Layer 2 — Backend)

Factors that show edge but don't pass the 90% threshold become scoring inputs.

Each setup type has a scoring rubric:

```
Score 0-4:
  +1 if [factor A]
  +1 if [factor B]
  +1 if [factor C]
  +1 if [factor D]
```

Recommended thresholds:
- **Score ≥ 3:** High conviction, take on any cluster day
- **Score ≥ 2:** Medium conviction, prefer large cluster days
- **Score < 2:** Low conviction, skip unless chart is exceptional

---

## Step 8: Measure EV Impact of Each Change

Every modification to the system (new hard filter, scoring change, management tweak) must be measured by its impact on EV per trade.

### 8a. Before/After Comparison

For any proposed change, compute:

| Metric | Before | After | Delta |
|--------|--------|-------|-------|
| EV per trade (ATR) | | | |
| Total P&L (ATR) | | | |
| Signal count | | | |
| Win rate | | | |
| Profit factor | | | |
| Avg hold (days) | | | |

**Accept if:** EV increases OR stays flat while reducing signal count (better capital efficiency).
**Reject if:** EV decreases, even if win rate improves (high win rate with tiny wins is worse than moderate win rate with real gains).

### 8b. Capital Efficiency

Two setups with the same EV but different hold times are not equal:
- **EV per day** = EV per trade / avg hold days
- Higher EV/day means faster capital recycling
- Prefer shorter holds when EV is comparable

---

## Step 9: Forward Testing & Iteration

Track live performance of the scan + scoring system:
- Record every signal, its score, and whether you took the trade
- Record actual entry, exit, and P&L
- Compare: scan baseline win rate vs your actual win rate (measures intraday TA value-add)
- Re-run this analysis quarterly with new data to check for drift

---

## TO DO

- [x] Define realistic exit rules per setup type and compute EV (done for 3-4DB)
- [ ] Re-score hard filters and soft scoring combos using real management rules (not theoretical max drop)
- [ ] Provide market stage data (stage 3 periods) for last 5 years → enables backtesting across multiple regimes
- [ ] Automate the full pipeline: scan → dedup → score → management → output ranked short list with EV
- [ ] Build nightly workflow endpoint that runs everything and returns the actionable list
- [ ] Expand to DTSS and HTF setup types once examples and conditions exist
- [ ] Run management grid search for each new setup type to find optimal stops/exits

---

## 3-4DB Specific Results

### Optimal Trade Management

Tested 128 combinations (4 stops × 8 exits × 4 time stops). Best EV:

| Rank | Stop | Exit | Time | EV/trade | PF | Win% | Avg Hold |
|------|------|------|------|----------|-----|------|----------|
| 1 | Entry high (3d grace) | Close > 12 EMA | 10d | **+0.47 ATR** | 4.28 | 77% | 3.0d |
| 2 | Entry high (3d grace) | Close > 8 EMA | 10d | +0.45 ATR | 4.06 | 73% | 3.0d |
| 3 | Entry + 1 ATR | Close > 12 EMA | 10d | +0.45 ATR | 3.96 | 77% | 3.0d |
| 4 | Entry high (3d grace) | Close > 21 EMA | 10d | +0.44 ATR | 4.11 | 77% | 2.4d |

**Selected: Stop at entry high (3d grace) + Close > 12 EMA + 10d time stop**

Management rules:
1. **Entry:** Short at close on signal day
2. **Stop:** Entry candle high — but only enforced after day 3 (3-day grace). If close > entry high on days 1-3, hold through.
3. **Exit:** First close above the 12 EMA
4. **Time stop:** Exit at close on day 10 if neither stop nor exit triggered
5. **Winner:** Exit close < entry candle low (+0.79 ATR avg)
6. **Loser:** Stop triggered after day 3 (-0.61 ATR avg)

Key insights:
- **EMA exits >> fixed targets.** 1.5+ ATR targets are breakeven or negative. These are 3-4 day moves — take what momentum gives you.
- **3-day grace adds +0.06-0.08 EV** vs immediate stop. Lets the trade settle.
- **12 EMA slightly edges 8 EMA** — more patience without giving back much.
- **10-day time stop adds +0.02-0.05 EV** vs no time stop. Prevents dead money.

### Hard PCF conditions (18 → 20)

**Existing conditions 1-18:** See `scripts/scan_3_4db.py`

**New conditions from this analysis:**
- **Condition 13 (tightened):** Retracement cap from `< 0.70` → `< 0.55`
  - PCF: `(C - MINL10) / (MAXH30 - MINL10) < 0.55`
  - 94% of validated winners pass, eliminates 33% win rate bucket
- **Condition 19 (new):** Bars from peak ≥ 5
  - PCF: `MAXH15 > MAXH4`
  - 94% of validated winners pass, eliminates 0% win rate bucket

### Soft scoring rubric (3-4DB)

```
Score 0-4:
  +1 if red candle (close < open)           — 88% of winners, +14pp edge
  +1 if below optimized AVWAP               — 81% of winners, +16pp edge
  +1 if volume < 0.8x 20-day avg            — 69% of winners, +36pp win rate diff
  +1 if up days ≤ 2 in last 3 bars          — 94% of winners, avoids 27% kill zone
```

### Performance summary (77-day backtest, deduped, no biotech, 12 EMA exit)

| Configuration | Signals | Win Rate | EV/Trade | Total P&L | PF |
|--------------|---------|----------|----------|-----------|-----|
| Current scan | 56 | 77% | **+0.47 ATR** | +26.1 ATR | 4.28 |
| + New hard filters | 44 | TBD | TBD | TBD | TBD |
| + Hard + Score ≥ 2 | 38 | TBD | TBD | TBD | TBD |
| + Hard + Score ≥ 3 | 28 | TBD | TBD | TBD | TBD |

*All numbers use the selected management: entry high stop (3d grace), 12 EMA exit, 10d time stop.*
*Filter combos need re-scoring with this management — previous PF numbers used theoretical max drop.*

### Cluster effect

| Cluster Size | Win Rate | Note |
|-------------|----------|------|
| 1-3 signals | 34% | Losing strategy |
| 8-14 signals | 60% | Good |
| 15+ signals | 74% | Best signal, but often one sector theme |

Cluster size is a meta-signal — check it after running the scan, not a PCF condition.
