# Setup Analysis System

**Purpose:** Automated methodology for optimizing any swing trading setup type (3-4DB, DTSS, HTF, etc.) once PCF scan conditions and validated examples exist.

**Last updated:** 2026-02-19

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

## Step 3: Score Forward Performance

For each signal, fetch forward price data and compute:

| Metric | Definition | Use |
|--------|-----------|-----|
| `drop3` | Max % drop in 3 days (short profit) | Quick wins |
| `drop5` | Max % drop in 5 days | Primary win metric |
| `drop10` | Max % drop in 10 days | Extended holds |
| `drop_atr5` | Drop in ATR units over 5 days | Normalized win metric |
| `bounce5` | Max % rise in 5 days (adverse move) | Risk metric |
| `win` | drop_atr5 ≥ 1.0 | Binary win/loss |

**Critical note on profit factor:** These metrics assume perfect exits (catching the exact low). Real profit factor requires defined exit rules — see "Exit Rules" section below.

---

## Step 4: Compute Signal Characteristics

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

## Step 8: Measure Profit Factor (Properly)

### Current limitation

The backtester uses theoretical max profit/loss within a time window. This inflates profit factor because it assumes perfect entry at close and perfect exit at the low.

### Realistic exit rules (TO DO — implement per setup type)

For shorts:

| Exit Type | Condition | Purpose |
|-----------|-----------|---------|
| **Stop loss** | Close above entry + X ATR | Capital preservation |
| **Profit target** | Low touches entry - Y ATR | Lock in gains |
| **Trail stop** | Close above N-day high after in profit | Let winners run |
| **Time stop** | No target/stop hit in Z days | Prevent dead money |

Parameters X, Y, Z, N should be optimized per setup type using the same backtest data.

### Profit factor formula

```
PF = sum(realized gains on winning trades) / sum(realized losses on losing trades)
```

Where gains/losses use actual exit prices from the rules above, not theoretical max drop/bounce.

---

## Step 9: Forward Testing & Iteration

Track live performance of the scan + scoring system:
- Record every signal, its score, and whether you took the trade
- Record actual entry, exit, and P&L
- Compare: scan baseline win rate vs your actual win rate (measures intraday TA value-add)
- Re-run this analysis quarterly with new data to check for drift

---

## TO DO

- [ ] Define realistic exit rules per setup type and re-compute profit factor
- [ ] Provide market stage data (stage 3 periods) for last 5 years → enables backtesting across multiple regimes instead of one 77-day window
- [ ] Automate the full pipeline: scan → dedup → score → output ranked short list
- [ ] Build nightly workflow endpoint that runs everything and returns the actionable list
- [ ] Expand to DTSS and HTF setup types once examples and conditions exist

---

## 3-4DB Specific Results

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

### Performance summary (77-day backtest, deduped, no biotech)

| Configuration | Signals | Win Rate | Profit Factor* |
|--------------|---------|----------|---------------|
| Current scan | 58 | 48% | 1.84 |
| + New hard filters | 46 | 52% | 2.27 |
| + Hard + Score ≥ 2 | 39 | 59% | 3.01 |
| + Hard + Score ≥ 3 | 29 | 62% | 4.53 |

*Profit factor uses theoretical max drop/bounce, not realistic exits. Actual PF will be lower.

### Cluster effect

| Cluster Size | Win Rate | Note |
|-------------|----------|------|
| 1-3 signals | 34% | Losing strategy |
| 8-14 signals | 60% | Good |
| 15+ signals | 74% | Best signal, but often one sector theme |

Cluster size is a meta-signal — check it after running the scan, not a PCF condition.
