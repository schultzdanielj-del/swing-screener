# EV Optimizer — Signal Intelligence System

**Status:** Design phase
**Depends on:** Grinder output (any run), 5yr OHLCV cache, expression series cache
**Goal:** For every signal the grinder produces, answer: **"How far is this thing likely to run, and how confident should I be?"**

---

## Philosophy

The grinder answers: **"What to watch tonight."**
The EV optimizer answers: **"How much runway each one has."**

**What the system does NOT do:**
- Tell you where to enter (that's intraday discretion)
- Tell you where to stop (that's your risk management)
- Tell you how to manage the trade (scaling, partials, re-entries — that's your skill)

**What the system DOES do:**
- Confirm that a signal actually triggered (post-entry behavioral match)
- Estimate how far the move historically goes (in technical terms: which MAs, what extension levels, how many ADR)
- Score the current environment to adjust that estimate up or down
- Rank tonight's signals by expected runway so you allocate capital to the biggest moves

**Why this matters:** If you know a setup historically runs 8 ADR, you re-enter after a stop-out. If you know it only runs 2 ADR, you take the loss and move on. Six paper cuts at 0.5 ADR followed by a 20 ADR runner = +17 ADR net. Nobody takes that trade without the data.

---

## Pipeline Overview

```
Grinder signals (e.g., 368 raw)
    ↓
Step 1: Post-Entry Grinder — behavioral filter using example post-entry patterns
    ↓
Confirmed entries (e.g., ~100-150 that actually behaved like the examples)
    ↓  
Step 2: Distance Profiling — how far did each confirmed entry run?
    ↓
Step 3: Environment Scoring — which market conditions predict bigger moves?
    ↓
Scoring model + distance estimates
    ↓
Step 4: Nightly Priority Queue — tonight's signals ranked by expected runway
```

---

## Step 1: Post-Entry Grinder

**Script:** `scripts/post_entry_grinder.py` (NEW)
**Input:** 26 validated examples (known entry bars) + raw grinder signal set (e.g., 368 signals)
**Output:** Filtered signal set — only signals whose post-entry behavior matches the examples

### How It Works

The grinder logic is identical to the pyramid grinder, just applied to a different problem:
- **Universe:** The raw grinder signals (368 ticker-date pairs) instead of 4,000 tickers
- **Examples:** The 26 validated examples with known entry dates
- **Expressions:** A NEW post-entry expression library (see below)
- **Window:** Bars 1-5 (or configurable) after the signal bar

### Post-Entry Expression Library

The current 4,017 expressions are all relative to the current bar. Post-entry expressions measure what happens AFTER the signal, relative to the signal bar as anchor:

| Expression Type | Examples |
|----------------|----------|
| **Distance from LSP** | Min close in bars 1-5 relative to LSP price, in ADR multiples |
| **AVWAP behavior** | Did price break below AVWAP anchored to signal bar? How far? |
| **Support breaks** | Did close break below prior support (20-day low, 50 SMA, etc.)? |
| **Volume character** | Down-bar volume vs up-bar volume in the post-entry window |
| **Flush depth** | Max drawdown from signal bar close within N bars, in ADR |
| **Close position** | Where did it close relative to its range in the post-entry bars? |
| **MA behavior** | Did it close below the 8 EMA? 21 EMA? How quickly? |
| **Candle character** | Red candle %, average body size, wick ratios in post-entry window |

This is a purpose-built expression set — maybe 200-500 expressions focused on post-entry behavior. The grinder then finds which combinations ALL 26 examples share, and uses those to filter the 368 raw signals.

### What This Solves

- **Entry candle identification:** No manual tagging. Signals that never triggered (price never breached the LSP) won't show the post-entry behavioral pattern → filtered out.
- **Quality filter:** Signals that triggered but immediately reversed (scratches) won't match the winning pattern either → filtered out.
- **Automatic signal refinement:** Turns a loose 368-signal set into a tight set of historically confirmed entries.

### Key Design Note

The post-entry grinder runs on HISTORICAL signals only (the 5yr backtest set). For nightly live signals, you can't look at post-entry bars yet — you use the distance profile from Step 2 to estimate runway based on signal characteristics at scan time. The post-entry grinder's job is to clean the historical dataset so Steps 2-3 have accurate training data.

For live monitoring: you COULD run the post-entry filter on T+2 or T+3 to confirm whether a live signal actually triggered and started running. This would be a "confirmation alert" — "that DTSS signal from Monday? It triggered. Historical runway says 8 ADR."

---

## Step 2: Distance Profiling

**Script:** `scripts/distance_profiler.py` (NEW)
**Input:** Confirmed entries from Step 1 + 5yr OHLCV cache
**Output:** `data/ev_optimizer/{run_id}/distance_profiles.parquet`

### What We Measure

For each confirmed entry, track the forward price path in TECHNICAL terms (not R-multiples):

**Distance milestones (for shorts):**
- Bars to reach each MA: 8 EMA, 21 EMA, 50 SMA, 200 SMA (measured from when price first closes below each)
- Maximum extension reached: peak ADR multiples below 20 EMA, 50 SMA
- Total move in ADR from signal bar high to lowest low within 60 bars
- Total move in % terms

**Path shape:**
- How many bars to reach 50% of the total move?
- How many bars to reach max extension?
- Did it V-bounce or grind lower?
- Was there a clear secondary leg (re-entry opportunity)?

**Structural destinations:**
- Did it reach the 50 SMA? (yes/no + how many bars)
- Did it reach the 200 SMA?
- Did it reach a prior structural support level?
- What extension level (ADR multiples below 20 EMA) did it peak at?

### Output: Distance Profile Per Signal

```json
{
    "ticker": "NVDA",
    "signal_date": "2024-03-15",
    "total_move_adr": 8.3,
    "total_move_pct": 22.1,
    "reached_50sma": true,
    "bars_to_50sma": 12,
    "reached_200sma": false,
    "max_extension_20ema_adr": -6.2,
    "max_extension_50sma_adr": -3.1,
    "bars_to_max_extension": 18,
    "path_shape": "grind",
    "secondary_leg": true
}
```

### Aggregate Distance Profile Per Setup

Across all confirmed entries:

```
DTSS Distance Profile (N=XX confirmed entries)
────────────────────────────────────────────────
Median total move:        6.2 ADR
Mean total move:          7.8 ADR (skewed by runners)
Reached 50 SMA:          78% of signals
Reached 200 SMA:         31% of signals
Median bars to 50 SMA:   14
Median max extension:     -4.8 ADR below 20 EMA
Had secondary leg:        45%
```

This becomes the baseline expectation for the setup.

---

## Step 3: Environment Scoring

**Script:** `scripts/environment_scorer.py` (NEW)
**Input:** Distance profiles + market context at each signal
**Output:** `data/ev_optimizer/{run_id}/scoring_model.json`

### Core Question

Which environmental factors at signal time predict BIGGER moves vs SMALLER moves?

### Market Context Factors (captured at signal bar)

| Factor | Source | Description |
|--------|--------|-------------|
| `spy_trend` | SPY extension vs 50 SMA in ADR | Trend direction + strength |
| `spy_ext_200` | SPY extension vs 200 SMA | Long-term regime |
| `spy_rsi14` | SPY RSI(14) | Momentum regime |
| `signal_density` | Count of other signals within ±5 trading days | Clustering |
| `ticker_ext_50` | Signal ticker's extension vs 50 SMA | How stretched the ticker is |
| `ticker_adr` | Signal ticker's ADR | Volatility of the setup |
| `ticker_rel_vol` | Volume vs 50-day avg volume | Volume character at signal |

### Method

For each factor, split confirmed entries into quantile groups and compare distance outcomes:

```
spy_trend factor — DTSS signals
    Q1 (SPY strong, above 50): median move = 4.1 ADR
    Q2 (SPY neutral):          median move = 6.8 ADR  
    Q3 (SPY weak, below 50):   median move = 9.4 ADR
    → SPY weakness adds ~5 ADR of expected runway
```

Each factor gets a weight based on how much it shifts expected distance. Bootstrap confidence intervals for reliability.

### Output: Scoring Model

```json
{
    "setup": "dtss",
    "base_distance_adr": 6.2,
    "factors": [
        {
            "name": "spy_trend",
            "weight": 2.5,
            "direction": "negative",
            "description": "Weaker SPY → bigger DTSS moves"
        },
        {
            "name": "signal_density",
            "weight": 1.2,
            "direction": "positive", 
            "description": "Clustered signals → regime confirmed → bigger moves"
        }
    ]
}
```

### Missing Cache Data

Expression cache may have gaps for recent days. **Gracefully skip** any signal where context data is missing — mark as incomplete, don't crash. The scoring model just has slightly less training data.

---

## Step 4: Nightly Priority Queue

**Script:** `scripts/priority_scorer.py` (NEW)
**Input:** Tonight's grinder signals + scoring model + current market data
**Output:** Ranked signal list with expected runway

### Nightly Flow

```
Grinder fires signals
    ↓
For each signal:
    1. Compute current environmental factors
    2. Apply scoring formula: expected_distance = base + Σ(weight × factor)
    3. Flag any setup-specific context (extension ceiling, prior structure)
    ↓
Rank signals by expected distance
    ↓
Output tonight's priority list
```

### Output Format

```
Tonight's Signals — DTSS (2026-02-25)
Market: SPY -2.1 ADR below 50 SMA, 4 signals clustered

Rank | Ticker | Expected Move | Destination    | Confidence | Context
1    | NVDA   | ~9 ADR        | 200 SMA likely | High       | SPY weak ✓, clustered ✓, high ext
2    | TSLA   | ~7 ADR        | 50 SMA likely  | High       | SPY weak ✓, clustered ✓
3    | META   | ~5 ADR        | 50 SMA likely  | Medium     | SPY weak ✓
4    | XYZ    | ~4 ADR        | 21 EMA         | Low        | Base expectation only
```

**What you do with this:**
- NVDA: Be aggressive. Re-enter after stop-outs. Scale in. This one's going to the 200 SMA.
- XYZ: Take it if nothing else is on, tight stop, don't chase if it stops you out.

### Cross-Setup Priority

When multiple setup types fire on the same night (DTSS + 3-4DB + HTF):

```
Tonight's Priority — All Setups
1 | DTSS | NVDA | ~9 ADR  | High confidence
2 | HTF  | CRWD | ~12 ADR | Medium confidence  
3 | DTSS | TSLA | ~7 ADR  | High confidence
4 | 3-4DB| AAPL | ~3 ADR  | High confidence
```

Capital allocation: biggest expected moves get the biggest positions. 3-4DB AAPL at 3 ADR gets a smaller position than DTSS NVDA at 9 ADR, even though both are high confidence.

---

## Step 5: Cross-Run Comparison

**Script:** `scripts/ev_compare.py` (NEW)
**Input:** Multiple run outputs from different grinder results
**Output:** Comparison report

Everything is keyed by run_id, so swapping in a tighter grind run (179 vs 368 signals) → re-run Steps 1-3 → compare:

```
                    | 368-signal run | 179-signal run
Confirmed entries   | 142            | 98
Median distance     | 5.8 ADR        | 7.1 ADR
Reached 50 SMA %   | 71%            | 83%
Top env factor      | spy_trend      | spy_trend
```

Tighter grind → fewer but higher quality signals → bigger expected moves. This validates whether the tighter grind actually produces better outcomes.

---

## File Structure

```
scripts/
├── post_entry_grinder.py       # Step 1: behavioral filter (NEW)
├── distance_profiler.py        # Step 2: how far do they run (NEW)
├── environment_scorer.py       # Step 3: market context scoring (NEW)
├── priority_scorer.py          # Step 4: nightly ranking (NEW)
├── ev_compare.py               # Step 5: cross-run comparison (NEW)
├── management_optimizer.py     # EXISTS — still useful for mechanical setups (3-4DB)
├── outcome_engine.py           # EXISTS — MFE/MAE computation, feeds distance profiler

data/ev_optimizer/
├── dtss_368_20260225/
│   ├── confirmed_entries.csv       # Step 1 output
│   ├── distance_profiles.parquet   # Step 2 output
│   └── scoring_model.json          # Step 3 output
└── dtss_179_20260226/
    └── ...
```

---

## Implementation Order

| Phase | Script | Effort | Notes |
|-------|--------|--------|-------|
| **1** | `post_entry_grinder.py` | Medium | New expression library + grinder adaptation |
| **2** | `distance_profiler.py` | Medium | Forward path analysis in technical terms |
| **3** | `environment_scorer.py` | Medium | Factor analysis against distance outcomes |
| **4** | `priority_scorer.py` | Small | Consumer of Step 3 output |
| **5** | `ev_compare.py` | Small | Comparison across runs |

**Step 1 is the critical path.** It unblocks everything else and solves the entry candle problem.

---

## Open Questions

1. **Post-entry window size:** 5 bars after signal? 3? The examples should tell us — how quickly do they all show the behavioral pattern?

2. **Distance measurement: from where?** Signal bar high? Signal bar close? The post-entry grinder identifies WHEN the entry happened, but "entry price" is still approximate. Probably signal bar high (the LSP area) is the most conservative anchor.

3. **Minimum confirmed entries for reliable scoring:** With 368 raw → maybe 100-150 confirmed. Split into quantile groups of 30-50 for environment scoring. Probably enough for 5-6 factors. More if we get the 179 run working (paradoxically, fewer raw signals might give MORE confirmed entries per signal if the grind is tighter quality).

4. **Live post-entry confirmation:** Do we want the nightly pipeline to check T+2/T+3 on recent signals and send a "confirmed — be aggressive" alert? Or is that overengineering for now?

5. **3-4DB management optimizer:** The existing `management_optimizer.py` is still relevant for shorter, more mechanical setups where EMA exits make sense. Keep it as a parallel path for setups where management IS optimizable (entry/exit are more mechanical)?

---

## Relationship to ANALYSIS_SYSTEM.md

This replaces **Steps 7 (Market Context) and 8 (EV Optimization)** with a different framing:

| ANALYSIS_SYSTEM.md | This System | Why |
|--------------------|-------------|-----|
| Step 7: Market Context filter | Step 3: Environment Scoring | Not a filter — a scoring model that adjusts distance estimates |
| Step 8: Management Optimization | Step 2: Distance Profiling | Not optimizing management — estimating runway. Management is discretionary. |
| (not in original) | Step 1: Post-Entry Grinder | Solves entry candle problem, cleans historical data |

The `management_optimizer.py` remains available for mechanical setups (3-4DB style) where entry/exit rules can be optimized. For discretionary setups (DTSS, parabolic shorts), the system provides intelligence, not instructions.

ANALYSIS_SYSTEM.md should be updated to reflect this once validated.
