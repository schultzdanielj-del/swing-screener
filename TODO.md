# ScanPerfect Pipeline (2026-03-12)

## The Goal

Always be in the highest probability positions the market is offering right now. Compound at 2.5%/month for 20 years.

---

## Pipeline Overview

```
Phase 1 — Sample Gathering
  a) Vetting System

Phase 2 — Causative Filtering
  a) Signal Grind
  b) Exit Signal Grind
  c) Refinement Grind

Phase 3 — Correlative Filtering
  a) Market Regime + Setup-Specific Correlations (combined, not sequential)

Phase 4 — Profit Optimization
  a) Profit Grinder — maximize compounded equity growth, not raw MFE capture

Phase 5 — Live Watchlist
  a) Dynamic EV Scoring
  b) Live Nightly Workflow

Phase 6 — Reverse Engineering (future)
  a) Monster Mover Discovery
  b) Setup Type Emergence
```

---

## The Core Insight

Examples are the currency of the entire system. Every filter costs examples. Phase 1 banks as many as possible. Phase 2 burns some to separate signal from noise. Phase 3 burns more to find when setups pay and how much. The more you bank in Phase 1, the more you can afford to spend in Phases 2 and 3.

In Phase 2, examples fight curve fitting — more examples means tighter bounding boxes and less overfitting risk from stacking conditions.

In Phase 3, examples fuel EV improvement — more examples means the correlative filters have richer data to find real market patterns vs noise.

---

## Phase 1 — Sample Gathering

### a) Vetting System

The vetting system is where setup examples are defined and collected. You start with a setup description and a baseline set of example trades. The more you vet, the better the entire system gets.

Early on, the system works from signal bars (the bar where conditions fired). As more charts get vetted, you get real entry bars, real exit bars, actual trade data. This tightens everything downstream — exit grind gets better targets, ADR moves become more accurate, correlative filters have cleaner data.

Vetting is not a one-time gate. It's a quality layer that improves continuously. Even after going live, vetting more historical signals keeps making the model better.

The vetting loop runs through Phase 2: signal grind → exit grind → rank output by biggest signal-to-exit ADR moves → vet top charts → add examples → repeat. When the good setups become buried in the output, you run the refinement grind and vet the winning pile rank-ordered the same way. Keep going until you've squeezed the sample set dry.

The goal: enter Phase 3 with as many examples as you can get.

---

## Phase 2 — Causative Filtering

These steps find the mathematical conditions that separate setup bars from the universe. They are "causative" — they describe what the chart looks like when the setup is present.

### a) Signal Grind

Examples vs full universe. The pyramid grinder beam-searches 16,000+ expressions across 4,167 tickers to find conditions where 100% of examples pass but most of the universe fails.

- Engine: Pyramid grinder, D1 cap=15
- Input: Example library + expression cache + 5yr OHLCV
- Output: Condition set + raw signal list across 5yr history
- Script: `pyramid_grinder.py`

### b) Exit Signal Grind

Finds the optimal exit condition — the expression threshold that best captures when the setup resolves (move is over).

- Input: Example entry bars + expression cache
- Output: Exit expression + direction + threshold
- Script: `signal_exit_grinder.py`

### c) Refinement Grind

Scans the full universe with signal grind conditions, clusters consecutive signal bars, classifies winners/losers via a ceiling+exit race, then beam-searches winners vs losers to find additional conditions that eliminate losing clusters.

Cluster-aware scoring: a losing cluster is only eliminated when ALL its bars are dead. No overcounting partial kills.

No re-scan, no re-classify after the beam search. Phase 1 classification (ceiling+exit race) is truth. The beam search filters the signal list by whole-cluster elimination.

- Input: Signal conditions + exit condition + example library + expression cache + 5yr OHLCV
- Output: Combined conditions (signal + refinement) + filtered winner/loser signal lists
- Script: `pyramid_grinder.py --blackout`
- Overfitting risk: More refinement depth = more conditions = higher curve fit risk. Depth progression output (TODO) will allow post-hoc threshold tuning.

---

## Phase 3 — Correlative Filtering

These filters find when and how much setups pay. They are "correlative" — they describe market and ticker conditions that increase or decrease win rate and ADR move size. They don't describe the setup itself.

Every correlative bucket that filters out losers also filters out some winners. More examples going in means you can afford tighter buckets.

### Combined Analysis: Market Regime + Setup-Specific Correlations

These are not sequential steps — they run together as two dimensions of the same analysis. Every signal gets evaluated simultaneously on both:

**Market regime** — broad market conditions: SPY trend, VIX level, sector rotation, breadth, interest rates, etc. Uses the 266-instrument market cache.

**Setup-specific** — ticker and setup characteristics: price level, market cap, dollar volume, sector, float, etc. Things specific to the individual stock and setup instance.

The buckets interact. A setup firing during a strong market on a mid-cap with high dollar volume has a different win rate than the same setup during a choppy market on a low-float micro-cap. Running them separately would mask those interactions — you need the combined effect.

Output is a multi-dimensional bucketing of win rate and ADR move size across both market conditions and ticker characteristics. This feeds directly into Phase 4's EV scoring.

- Input: Pre-refinement signal piles (full loser set, not post-refinement)
- Output: Combined correlation buckets with win rate and ADR move multipliers
- Script: `market_grinder.py` (regime done, setup-specific + combined analysis not built)

---

## Phase 4 — Profit Optimization

### a) Profit Grinder

Runs on the final filtered winner set — the trades you'd actually take after correlative filtering. No point optimizing exits on signals the regime would have excluded.

Tests multiple exit strategies (trim and trail, fixed targets, volatility-based stops, etc.) and evaluates them by compounded equity growth over N trades, not average MFE capture per trade. A strategy that captures 60% MFE consistently may outcompound one that captures 90% with high variance, because drawdowns from volatile strategies kill position sizing.

The objective function is compound growth rate, not raw MFE. Consistency IS the edge when compounding.

Output: optimal exit strategy with compounded equity curve, drawdown profile, and MFE capture stats.

- Input: Post-correlative winner pile with entry bars and price data
- Output: Exit strategy parameters + compounded equity simulation
- Script: `profit_grinder.py` (exists, needs rewiring to new pipeline and new objective function)

---

## Phase 5 — Live Watchlist

### a) Dynamic EV Scoring

Combines correlation buckets into a single EV score per signal. Each night's scan produces signals, and each signal gets scored based on where it falls in the correlation buckets.

Higher score = better regime + better setup characteristics = higher expected value.

### b) Live Nightly Workflow

After market close:
1. Run tonight's bars against final conditions → signals that fired today
2. Score each signal using correlation buckets → EV estimate
3. Rank order by EV, highest to lowest
4. You focus on the top of the list

The watchlist is the end product. Every cycle of the loop makes it more accurate.

---

## Phase 6 — Reverse Engineering (future)

### a) Monster Mover Discovery

Flip the pipeline. Instead of starting with a setup pattern, start with the biggest movers in history — stocks that went 100%+, 200%+, 500%+. Scan the expression library for what conditions were true on the bars before these moves started.

Same infrastructure: same 16,000 expressions, same grinder engine, same 5yr data. Different starting set.

### b) Setup Type Emergence

Different monster movers will cluster naturally into setup types based on which expression conditions they share. The setup types emerge from the data instead of being defined upfront.

This is the ultimate use of the system — find the optimal entry and exit conditions to maximize capture of the biggest moves the market has ever produced.

---

## DTSS — Current State

| Step | Status | Key Numbers |
|------|--------|-------------|
| Phase 1: Vetting | ✅ 68 examples | 66 with valid scan bars |
| Phase 2a: Signal Grind | ✅ Done | 87 conditions, 1,218 raw → 893 deduped |
| Phase 2b: Exit Grind | ✅ Done | `slope_xavgc21_off7_adr14 <= -1.128826` |
| Phase 2c: Refinement Grind | ✅ Done | 100 refinement conditions, 426/528 clusters killed, 78% WR |
| Phase 3: Correlative Filtering | ✅ Regime done | 50 features, D1→D10: 6.1%→65.7% pre, 42.2%→93.8% post. Setup-specific not built. |
| Phase 4: Profit Optimization | ⏸ Needs rewire | Script exists, needs new objective function (compound growth) |
| Phase 5: Live Watchlist | ⏸ Not built | |

### Refinement Grind Result (2026-03-12)
- 893 clusters: 365 WIN, 528 LOSS
- 100 refinement conditions (depth capped at 100)
- 426/528 losing clusters eliminated (80.7%)
- All 365 winners pass all conditions
- 182 combined conditions (87 signal + 100 refinement, 5 overlap)
- Pre-regime win rate: 78% (365 / 467)
- File: `refinement_dtss_cl102_pk5_20260312_150704.json`

### Regime Model Result (2026-03-13)
- 256 instruments × 15,805 expressions → 3M+ features tested → 50 selected (deduplicated)
- Runs on both pre-refinement (893 clusters) and post-refinement (467 clusters)
- Pre-refinement decile lift: D1=6.1% → D10=65.7% (+59.6pp)
- Post-refinement decile lift: D1=42.2% → D10=93.8% (+51.6pp)
- Redundancy analysis: 31 genuine features, 18 redundant (already captured by refinement)
- 100% signal coverage across all years (2021-2026) after 8y market cache extension
- Top genuine feature: UVXY OBV slope 30 (ratio 1.13 — stronger post-refinement)
- All local: reads refinement JSON, saves to local_runner/cache/, mirrors to Railway
- File: `regime_dtss_20260313_095056.json`

---

## Immediate Tasks

### Grinder Improvements
1. **Depth progression output (refinement grinder)** — save level-by-level best path and cluster count in refinement JSON. Allows post-hoc condition threshold tuning without re-running.
2. **Margin progression output (signal grinder)** — save tier-by-tier signal counts at different bounding box margins (5%, 3%, 1%, 0%). More examples = tighter margins viable. Allows post-hoc margin tuning without re-running.
3. **Earnings proximity filter** — filter out signals/entries that are too close to earnings date to take safely. Needs to be applied in multiple spots: signal grind output, refinement grind classification, and live nightly scan.

### Regime Model
4. ~~**Wire regime model to new pipeline**~~ — **DONE 2026-03-13**. `market_grinder.py` rewritten to all-local. Reads refinement JSON, runs pre+post, computes redundancy scores, saves+mirrors. Market cache extended to 8y for full signal coverage. `fetch_missing_market.py` added for incremental fetches.

### Vetting UI
5. **Read from signal grind and refinement grind outputs** — vetting UI currently reads signal_filter output. Needs to read from cluster files instead. Sort results by signal-to-exit ADR move (biggest movers first).
6. **AI vet queue** — signals go to AI review, then one-click "yes" adds them to the example library. This flow needs to work end-to-end.
7. **Workflow and ease-of-use improvements** — many setups will be running, vetting is factory-line gruntwork. UI needs to be fast, keyboard-driven, minimal clicks per chart.

### Pipeline UI
8. **Full pipeline control from UI** — every grinder step runnable from the UI with all parameters and tweaks selectable at each level. Fully wired to the pipeline agent.
9. **Update PIPELINE_V2.md** — remove proximity grind, profit grind. Update refinement spec (cluster-aware engine is built).

### Code Cleanup (future)
10. **Remove dead ADR code from signal_filter.py** — once vetting sources from cluster files, remove: `measure_example_exit_distances()`, ADR floor classification in `_build_classified_signals()`, ADR-based `min_adr` filtering. The ceiling+exit race in clusters replaces all of it. Three current ADR computation spots: `signal_filter.py` (two places) and `_gather_raw_signal_clusters()` (two places) — consolidate to clusters only.

### Vetting
11. **Vet winner pile** — review 365 winners, add examples, loop if needed.

---

## Infrastructure

- **Repo:** `schultzdanielj-del/swing-screener`, branch `v2`
- **Railway:** `https://web-production-e3025.up.railway.app`
- **Expression cache:** 16,051 expressions, ~21 GB
- **5yr OHLCV cache:** ~4,167 tickers
- **File mirror:** All grind results → Railway via `file_mirror.py`
- **Nightly refresh:** 4:30pm ET, 7 steps, fully automated

---

## Key Design Decisions

- **Pyramid with D1 cap=15 is the official signal grind engine.** Experimental grinders (dartboard, hybrid) failed. Shelved.
- **Beam search instability accepted.** Individual runs produce usable signal sets despite low run-to-run overlap.
- **Cluster-aware refinement scoring.** A losing cluster only counts as eliminated when ALL its bars are dead.
- **No re-scan/re-classify in refinement.** Phase 1 classification is truth.
- **Regime runs on pre-refinement data.** Post-refinement has too few losers for the model to learn from.
- **100% example pass rate required.** Any grinder result where an example fails is invalid.
- **Silent failures are dangerous.** The system produces plausible wrong numbers. Verify empirically.

---

## Shelved / Legacy

- `dartboard_grinder.py` — additive scoring washes out discrimination
- `hybrid_grinder.py` — correlated booleans don't filter
- `proximity_grinder.py` — replaced by refinement grinder
- `profit_grinder.py` — removed from pipeline
- `setup_refiner.py` — legacy, unused
- `signal_filter.py` classified output — replaced by `raw_signal_clusters_{setup}.json`
