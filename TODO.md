# ScanPerfect Pipeline (2026-03-13)

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

Examples run through the full classification race (ceiling calc, exit condition scan) like every other cluster. They get their exit_bar, ceiling, and move data. Classification is overridden to AUTO_WIN regardless of race outcome since they are validated examples.

- Input: Signal conditions + exit condition + example library + expression cache + 5yr OHLCV
- Output: Combined conditions (signal + refinement) + filtered winner/loser signal lists with move_adr data
- Script: `pyramid_grinder.py --blackout`
- Overfitting risk: More refinement depth = more conditions = higher curve fit risk. Depth progression output (TODO) will allow post-hoc threshold tuning.

### move_adr measurement

Every cluster with an exit_bar gets `move_adr` (entry_high to exit_close, in ADR units), `adr_at_signal` (14-bar ADR at rightmost bar), and `entry_high`.

Entry high is determined by two cases:
- **Examples** (have a real entry candle): entry_high = high of the entry candle.
- **Non-examples** (no defined entry candle): entry_high = max high in the forward window used during classification. This is the worst-case entry price — conservative, so the numbers understate real performance.

Exit price = close of the bar where the exit condition fired.

`move_adr = (entry_high - exit_close) / adr_at_signal` for shorts.

This is not true MFE (lowest low before exit). It's the actual captured move to the exit condition close — a consistent, tradeable measurement. The exit condition is a placeholder good enough for reliable filtering data. The profit grinder (Phase 4) later optimizes the actual exit strategy.

Winners without an exit_bar (held_to_end, no_data_after_window) get null — excluded from stats.

This data flows through to the refinement JSON (`winner_signals`, `loser_signals`, `eliminated_signals`) so all Phase 3 grinders can compute move stats per bucket alongside win rate.

---

## Phase 3 — Correlative Filtering

These filters find when and how much setups pay. They are "correlative" — they describe market and ticker conditions that increase or decrease win rate and ADR move size. They don't describe the setup itself.

Every correlative bucket that filters out losers also filters out some winners. More examples going in means you can afford tighter buckets.

### Combined Analysis: Market Regime + Setup-Specific Correlations

These are not sequential steps — they run together as two dimensions of the same analysis. Every signal gets evaluated simultaneously on both:

**Market regime** — broad market conditions: SPY trend, VIX level, sector rotation, breadth, interest rates, etc. Uses the 266-instrument market cache. Script: `market_grinder.py`.

**Setup-specific** — ticker characteristics that are NOT price-action/volume patterns (the signal grind already captured those). These are "what kind of stock is this" traits the causative filters can't see. Script: `setup_grinder.py`.

Currently computed setup-specific features (from OHLCV):
- Price level (close at signal bar)
- ADR (14-bar average daily range)
- Dollar volume (20-day average close × volume)
- Days since IPO (first bar in 5yr cache to signal bar — rough proxy)
- RS vs SPY daily (5-day rolling vol-adjusted intraday momentum, stock minus SPY)
- RS vs SPY weekly (same formula on weekly bars)

Future setup-specific features (need external data sourcing):
- Market cap
- Float (absolute level + volume/float ratio)
- RS vs sector (needs sector mapping)
- Sector RS vs SPY (needs sector mapping)
- EPS growth
- Revenue growth

Both grinders run on pre-refinement (full signal set) and post-refinement (surviving signals only) to compute redundancy: does refinement already capture what this feature measures, or is it genuine additional signal?

The buckets interact. A setup firing during a strong market on a mid-cap with high dollar volume has a different win rate than the same setup during a choppy market on a low-float micro-cap. Running them separately would mask those interactions — you need the combined effect.

Output is a multi-dimensional bucketing of win rate and ADR move size across both market conditions and ticker characteristics. This feeds directly into Phase 4's EV scoring.

### Three-Knob Architecture

The final correlative filter is three independent filtering dimensions, all computed pre+post refinement:

1. **Refinement conditions** — with tunable depth threshold (use 50 of 100 conditions? 70? all 100?)
2. **Market regime buckets** — already built, pre+post redundancy scored
3. **Setup-specific buckets** — already built, pre+post redundancy scored

The combined filter optimizer searches across all three knobs simultaneously to maximize win rate × median move_adr without killing sample size. The full distribution shape matters — median, mean, floor, ceiling — because a bucket with high median but terrible floor blows up the equity curve. This produces the final "take this signal or don't" decision.

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
| Phase 1: Vetting | ✅ 68 examples | 65 with valid scan bars in clusters |
| Phase 2a: Signal Grind | ✅ Done | 87 conditions, 1,218 raw → 893 deduped |
| Phase 2b: Exit Grind | ✅ Done | `slope_xavgc21_off7_adr14 <= -1.128826` |
| Phase 2c: Refinement Grind | ✅ Done | 100 refinement conditions, 426/528 clusters killed, 78% WR |
| Phase 3: Regime | ✅ Done | 50 features, D1→D10: 6.1%→65.7% pre, 42.2%→93.8% post |
| Phase 3: Setup-specific | ✅ Done | 6 features, 3 genuine (price, ADR, RS W1), 3 redundant |
| Phase 3: Combined optimizer | ⏸ Not built | |
| Phase 4: Profit Optimization | ⏸ Needs rewire | Script exists, needs new objective function (compound growth) |
| Phase 5: Live Watchlist | ⏸ Not built | |

### Refinement Grind Result (2026-03-13)
- 893 clusters: 365 WIN, 528 LOSS
- 100 refinement conditions (depth capped at 100)
- 426/528 losing clusters eliminated (80.7%)
- All 365 winners pass all conditions
- 182 combined conditions (87 signal + 100 refinement, 5 overlap)
- Pre-regime win rate: 78% (365 / 467)
- **Winner move_adr: median 6.4, mean 6.7, floor 2.9, ceiling 13.1 ADR (364/365 with data)**
- All examples now run through full classification race (exit_bar, ceiling, move_adr)
- File: `refinement_dtss_cl102_pk5_20260313_122818.json`

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

### Setup-Specific Correlation Result (2026-03-13)
- 6 features tested: price, ADR, dollar volume (20d avg), days since IPO, RS vs SPY (D1), RS vs SPY (W1)
- RS formula: 5-day rolling vol-adjusted intraday momentum ((C/O-1)*100 avg × (avg_price / ATR50)), stock minus SPY
- Verified manually against MSFT 2025-10-27: script output -13.1340 vs manual calc -13.1341 (PASS)
- 893/893 signals with features (100% coverage on OHLCV-derivable features, 813/893 for W1 RS)
- Pre-refinement baseline WR: 40.9% (365/893). Post-refinement: 78.2% (365/467)
- **Genuine features (ratio >= 0.5):**
  - price: pre spread +5.1%, post +8.5%, ratio 1.67 — higher priced stocks win more, refinement didn't capture this
  - ADR: pre spread +10.9%, post +9.4%, ratio 0.86 — higher ADR stocks win more
  - RS W1: pre spread +14.0%, post +11.0%, ratio 0.79 — stocks with stronger weekly RS vs SPY win more (counterintuitive for shorts — topping stocks, not freefall)
- **Redundant features (ratio < 0.5):**
  - dollar volume: pre +10.0%, post +4.3%, ratio 0.43 — refinement already captures liquidity signal
  - days since IPO: pre -2.7%, post +8.1%, ratio -3.04 — nonlinear U-shape, not useful
  - RS D1: pre -8.3%, post +0.9%, ratio -0.10 — daily RS had no signal after refinement
- All local: reads refinement JSON + 5yr OHLCV cache, saves to local_runner/cache/, mirrors to Railway
- File: `setup_dtss_20260313_135931.json`

---

## Immediate Tasks

### Grinder Improvements
1. **Depth progression output (refinement grinder)** — save level-by-level best path and cluster count in refinement JSON. Allows post-hoc condition threshold tuning without re-running.
2. **Margin progression output (signal grinder)** — save tier-by-tier signal counts at different bounding box margins (5%, 3%, 1%, 0%). More examples = tighter margins viable. Allows post-hoc margin tuning without re-running.
3. **Earnings proximity filter** — filter out signals/entries that are too close to earnings date to take safely. Needs to be applied in multiple spots: signal grind output, refinement grind classification, and live nightly scan.
4. **Market grinder: cluster-level win rate series** — currently builds the win rate time series from individual signal bars, so a 5-bar cluster counts as 5 data points with the same outcome. Should use one data point per cluster (rightmost bar date or average of cluster bars' market features). Avoids inflating weight of longer clusters.

### Regime Model
5. ~~**Wire regime model to new pipeline**~~ — **DONE 2026-03-13**. `market_grinder.py` rewritten to all-local. Reads refinement JSON, runs pre+post, computes redundancy scores, saves+mirrors. Market cache extended to 8y for full signal coverage. `fetch_missing_market.py` added for incremental fetches.

### Phase 3 — Correlative Filtering
6. ~~**Add move_adr to cluster/refinement output**~~ — **DONE 2026-03-13**. `move_adr` (entry_high to exit_close in ADR), `adr_at_signal`, `entry_high` computed on every cluster. Examples use entry candle high, non-examples use forward window max high (conservative worst-case entry). Flows through to refinement JSON on all signal lists. 364/365 winners with data. Winner stats: median 6.4, mean 6.7, floor 2.9, ceiling 13.1 ADR. Also fixed bug where examples skipped the classification race entirely — they now get exit_bar, ceiling, and move data like every other cluster.
7. ~~**Build setup-specific correlation analysis**~~ — **DONE 2026-03-13**. `setup_grinder.py` computes 6 stock-characteristic features per signal (price, ADR, dollar volume 20d, days since IPO, RS vs SPY D1+W1). RS uses TC2000 PCF formula: 5-day rolling vol-adjusted intraday momentum, stock minus SPY. Vectorized numpy + parallel across tickers (629 tickers in 1.7s). Pre+post redundancy analysis. 3 genuine features (price, ADR, RS W1), 3 redundant. Saves JSON + mirrors to Railway.
8. **Combined filter optimizer** — search across refinement condition depth threshold × regime score buckets × setup-specific buckets to maximize win rate × median move_adr. Three independent filtering knobs turned together. Evaluates full distribution shape (median, mean, floor, ceiling) for profit curve optimization. Produces the final "take this signal or don't" decision.
9. **Source external data for additional setup-specific features** — market cap, float, sector mapping, EPS growth, revenue growth. Needed for RS vs sector, sector RS vs SPY, and fundamental features. Separate data sourcing project.

### Vetting UI
10. **Read from signal grind and refinement grind outputs** — vetting UI currently reads signal_filter output. Needs to read from cluster files instead. Sort results by signal-to-exit ADR move (biggest movers first).
11. **AI vet queue** — signals go to AI review, then one-click "yes" adds them to the example library. This flow needs to work end-to-end.
12. **Workflow and ease-of-use improvements** — many setups will be running, vetting is factory-line gruntwork. UI needs to be fast, keyboard-driven, minimal clicks per chart.

### Pipeline UI
13. **Full pipeline control from UI** — every grinder step runnable from the UI with all parameters and tweaks selectable at each level. Fully wired to the pipeline agent.
14. **Update PIPELINE_V2.md** — remove proximity grind, profit grind. Update Phase 3 to reflect the three-knob architecture (refinement depth + regime + setup-specific). Update refinement spec (cluster-aware engine is built).

### Code Cleanup (future)
15. **Remove dead ADR code from signal_filter.py** — once vetting sources from cluster files, remove: `measure_example_exit_distances()`, ADR floor classification in `_build_classified_signals()`, ADR-based `min_adr` filtering. The ceiling+exit race in clusters replaces all of it. Three current ADR computation spots: `signal_filter.py` (two places) and `_gather_raw_signal_clusters()` (two places) — consolidate to clusters only.

### Vetting
16. **Vet winner pile** — review 365 winners, add examples, loop if needed.

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
- **Examples run through full classification race.** Not skipped — they get exit_bar, ceiling, move_adr like every other cluster. Classification overridden to AUTO_WIN.
- **move_adr uses conservative entry price for non-examples.** Forward window max high = worst-case fill. Real entries will be better.
- **Setup-specific features are NOT from the expression cache.** The signal grind already mined all 16K expressions — anything in the cache that separates winners from losers would already be a signal/refinement condition. Setup-specific features must come from outside the cache (stock characteristics, cross-instrument RS, fundamentals).
- **RS formula is TC2000 PCF-based.** 5-day rolling average intraday % move × (avg_price / ATR50). Stock value minus SPY value = relative strength. Computed on both D1 and W1 timeframes.
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
