# ScanPerfect Pipeline (2026-03-14, updated Entry Candle Scorer + Phase 3 → EV Grinder)

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

Phase 3 — Correlative Scoring (EV Grinder)
  a) Source external data (market cap, float, sector, fundamentals)
  b) EV Grinder — unified scoring of all market + setup-specific features

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

In Phase 3, examples fuel EV accuracy — more examples means the scoring model has richer data to accurately predict win rate and move size for fresh signals.

---

## Phase 1 — Sample Gathering

### a) Vetting System

The vetting system is where setup examples are defined and collected. You start with a setup description and a baseline set of example trades. The more you vet, the better the entire system gets.

Vetting is not a one-time gate. It is a quality layer that improves continuously. Even after going live, vetting more historical signals keeps making the model better. It is a standalone workbench that sits outside the pipeline loop -- you open it when you want to vet, do your work, close it, and the pipeline does not know or care. It just sees a bigger example library next time you trigger a regrind.

**Two vetting modes:**

**Signal Grind vet** -- after signal grind, before refinement. Raw signals sorted by move_adr (biggest movers first). No entry candle scoring available yet because there is no winner/loser split.

**Post-Refinement vet** -- after refinement grind produces the winner pile. The entry candle scorer runs on the winner pile and produces a combined_score per signal. Signals with both a high-ADR move AND a bar in the forward window that looks like a real entry candle float to the top. You vet top-down and stop when quality drops off.

**Entry Candle Scorer** (scripts/entry_candle_scorer.py) -- standalone vetting utility, not a pipeline step. Builds a centroid from all example entry candle expression vectors (16,051 dimensions), computes per-expression discrimination weights (how tightly entry candles cluster vs how spread out winner forward window bars are, capped at 95th percentile), then scores each winner forward window bar against the centroid using weighted cosine similarity. Scan range per cluster: leftmost bar through rightmost bar + forward_window. Best-matching bar per cluster is the entry candle score. Combined score = percentile rank of entry_candle_score x percentile rank of move_adr. Output: entry_scores_{setup}.json mirrored to Railway.

**Vetting flow:**
1. Click Update Scores -- entry candle scorer runs (~10 seconds)
2. UI shows all winners sorted by combined_score
3. Vet top-down: 1=YES, 2=NO, 3=SKIP
4. YES picks go to AI second-pass (pending_examples, status=pending)
5. AI reviews each YES against the example library -- GREEN_LIGHT or FLAG
6. You see AI verdicts, one-click approve -- added to examples table
7. When enough examples have banked, trigger regrind from pipeline tab

**Self-improving:** More examples -- tighter centroid -- better entry candle scoring -- faster vetting -- more examples per session. The scorer gets better every time you use it.

The goal: enter Phase 3 with as many examples as you can get.

---

## Phase 2 — Causative Filtering

These steps find the mathematical conditions that separate setup bars from the universe. They are “causative” — they describe what the chart looks like when the setup is present.

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

This is not true MFE (lowest low before exit). It’s the actual captured move to the exit condition close — a consistent, tradeable measurement. The exit condition is a placeholder good enough for reliable filtering data. The profit grinder (Phase 4) later optimizes the actual exit strategy.

Winners without an exit_bar (held_to_end, no_data_after_window) get null — excluded from stats.

This data flows through to the refinement JSON (`winner_signals`, `loser_signals`, `eliminated_signals`) so all Phase 3 grinders can compute move stats per bucket alongside win rate.

---

## Phase 3 — Correlative Scoring (EV Grinder)

Phase 3 does not filter signals. Every signal that passes Phase 2 makes the watchlist. Phase 3 scores each signal with an accurate historical EV estimate so the watchlist can rank them.

This is what a discretionary trader does naturally — look at a setup and unconsciously weigh dozens of market and stock-specific factors to get a feel for “this one’s A+ quality” vs “this one’s marginal.” The EV grinder does this with flawless accuracy against every historical signal that ever fired, weighted precisely, no recency bias, no forgetting, no emotional tilt.

### What the EV Grinder produces

Three numbers per signal:
- **Estimated win rate** — based on how signals with similar characteristics performed historically
- **Estimated median winner move (MFE)** — same basis
- **EV** — (WR × MFE) − ((1−WR) × 1.0 ADR assumed stop)

### Feature universe

The grinder tests every correlative feature available — both market conditions and stock characteristics — for their effect on win rate AND move size. Features that increase WR/MFE score positively. Features that decrease WR/MFE score negatively. Both directions matter.

**Market regime features** (~4M): 256 instruments × 15,805 expressions. Each instrument’s expression value on the signal date. Covers SPY trend, VIX level, sector rotation, breadth, interest rates, credit spreads, bond market, commodities, international markets, and more.

**Setup-specific features (OHLCV-derived, available now — 6):**
- Price level (close at signal bar)
- ADR (14-bar average daily range)
- Dollar volume (20-day average close × volume)
- Days since IPO (first bar in 5yr cache to signal bar)
- RS vs SPY daily (5-day rolling vol-adjusted intraday momentum, stock minus SPY)
- RS vs SPY weekly (same formula on weekly bars)

**Setup-specific features (external data, needs sourcing — 10):**
- Market cap
- Float (absolute level)
- Volume/float ratio
- Sector mapping (GICS or similar)
- RS vs sector (stock RS minus sector RS)
- Sector RS vs SPY (sector performance vs broad market)
- EPS growth QoQ (most recent quarter vs prior quarter)
- EPS growth trailing 4Q (YoY from last 4 quarters)
- Revenue growth QoQ
- Revenue growth trailing 4Q

All features are included for every setup type. The grinder’s screening step determines which ones matter for each setup — something redundant for DTSS might be the strongest predictor for another setup.

### Architecture

**Step 1 — Feature matrix.** For each signal in the refinement output, look up the value of every candidate feature on that signal’s date. Market features from the instrument caches. Setup features from OHLCV + external data. Result: 893 rows × ~4M columns.

**Step 2 — Univariate WR screening.** For each feature independently: bucket signals into quartiles by feature value, compute win rate per quartile. Keep features where the spread between best and worst quartile exceeds a minimum threshold (configurable, default 10pp). This catches features in both directions — features that boost WR and features that tank WR.

**Step 3 — Univariate MFE screening.** Same but for winner move_adr. Bucket into quartiles, compute median move_adr per quartile (winners only). Keep features where the spread exceeds a minimum (configurable, default 1.0 ADR). A feature might not predict WR but strongly predicts move size, or vice versa.

**Step 4 — Union survivors.** A feature survives if it passed either the WR screen or the MFE screen. Tagged as “WR only”, “MFE only”, or “both.”

**Step 5 — Deduplication.** Greedy dedup by inter-feature correlation (same as current regime model). Ensures each survivor adds genuinely new information.

**Step 6 — Scoring curves.** For each survivor, store the quartile boundaries and the WR/MFE value per quartile. This is the lookup table — given a feature value, which quartile, what WR/MFE contribution.

**Step 7 — Score every signal.** For each signal: look up its quartile for each surviving feature, collect WR and MFE contributions, compute weighted average (weighted by each feature’s spread strength). Output: estimated WR, estimated MFE, EV.

**Step 8 — Validation.** Bucket signals by predicted WR into deciles. Does actual WR match predicted WR per decile? Same for MFE. If predicted 85% WR signals actually win 85%, the model is calibrated.

### What this replaces

The EV grinder replaces both `market_grinder.py` and `setup_grinder.py`. Those were built as separate analyses — market conditions in one, stock characteristics in another, with a planned “combined optimizer” to merge them. The EV grinder does everything in one unified pass where all features compete on equal footing.

The old regime model correlated features with a win-rate time series (temporal correlation). The EV grinder evaluates features at the individual signal level and predicts both WR and MFE. It also captures nonlinear effects through quartile bucketing — features that only matter at extremes are visible.

### Additive model (current design)

Each feature contributes independently. The scoring equation is a weighted sum of per-feature contributions. This is well-supported by 893 data points — each feature’s effect is measured across all signals.

True feature interactions (e.g., “UVXY OBV matters more on high-priced stocks”) are not captured. However, features that matter in combination will both independently predict WR/MFE, so the additive model ranks those signals highly anyway. The main risk is missing pairs that are individually weak but combined are strong — rare in practice, and undetectable with 893 signals.

Interaction terms can be layered in later as more examples accumulate across setup types.

### Runtime and compute

~5-20 minutes on local desktop. Same order of magnitude as the current regime model. The heavy part is disk I/O (loading 256 instrument caches, ~80MB each). Parallelizes across cores. All local data, no API calls.

- Input: Refinement output + market cache + 5yr OHLCV cache + external data cache
- Output: Scoring equation (surviving features + quartile boundaries + weights) + per-signal scores (WR, MFE, EV) + validation stats
- Script: `scripts/ev_grinder.py`
- Saves to `local_runner/cache/ev_{setup}_{timestamp}.json`, mirrors to Railway

### For live scoring

A new signal fires tonight. Compute its feature values (market cache lookup + OHLCV features + external data). Look up the quartile for each surviving feature. Weighted average of WR and MFE contributions. Done in milliseconds. The watchlist sorts by EV.

---

## Phase 4 — Profit Optimization

### a) Profit Grinder

Runs on the full signal set with EV scores attached. The profit grinder optimizes exit strategy across the signals you’d actually take — the ones the EV scoring ranks highest.

Tests multiple exit strategies (trim and trail, fixed targets, volatility-based stops, etc.) and evaluates them by compounded equity growth over N trades, not average MFE capture per trade. A strategy that captures 60% MFE consistently may outcompound one that captures 90% with high variance, because drawdowns from volatile strategies kill position sizing.

The objective function is compound growth rate, not raw MFE. Consistency IS the edge when compounding.

Output: optimal exit strategy with compounded equity curve, drawdown profile, and MFE capture stats.

- Input: EV-scored signal set with entry bars and price data
- Output: Exit strategy parameters + compounded equity simulation
- Script: `profit_grinder.py` (exists, needs rewiring to new pipeline and new objective function)

---

## Phase 5 — Live Watchlist

### a) EV Scoring

Each signal that fires tonight gets scored by the EV grinder’s equation. Look up its market regime features and setup-specific features, run through the scoring curves, output estimated WR, estimated MFE, and EV. Milliseconds per signal.

### b) Live Nightly Workflow

After market close:
1. Run tonight’s bars against signal + refinement conditions → signals that fired today
2. Score each signal using the EV equation → estimated WR, MFE, EV
3. Rank order by EV, highest to lowest
4. You take the top N that you have capital for — the bottom ones don’t get traded, not because they’re filtered out, but because better signals exist above them

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
| Phase 3: EV Grinder | ⏸ Not built | Replaces market_grinder + setup_grinder + combined optimizer |
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
  - price: pre spread +5.1%, post +8.5%, ratio 1.67 — higher priced stocks win more, refinement didn’t capture this
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
4. **Market grinder: cluster-level win rate series** — currently builds the win rate time series from individual signal bars, so a 5-bar cluster counts as 5 data points with the same outcome. Should use one data point per cluster (rightmost bar date or average of cluster bars’ market features). Avoids inflating weight of longer clusters.

### Phase 3 — EV Grinder
5. **Build the EV Grinder** — `scripts/ev_grinder.py`. Unified scoring engine replacing `market_grinder.py` + `setup_grinder.py` + the planned combined optimizer. Tests all ~4M market features + 16 setup-specific features for their effect on WR and MFE independently. Univariate quartile screening → dedup → additive weighted scoring model. Output: per-signal estimated WR, MFE, EV + the scoring equation for live use. ~5-20 min runtime.

### Vetting UI
6. **Wire vetting UI to entry candle scorer output** -- vetting UI reads entry_scores_{setup}.json from Railway file mirror. Two modes: signal grind vet (sort by move_adr only) and post-refinement vet (sort by combined_score from entry candle scorer). Mode toggle in UI.
7. **AI vet queue** -- YES picks go to pending_examples (AI second-pass), then one-click approve adds to examples. Flow needs to work end-to-end.
8. **Workflow and ease-of-use improvements** -- many setups will be running, vetting is factory-line gruntwork. UI needs to be fast, keyboard-driven, minimal clicks per chart.

### Pipeline UI
9. **Full pipeline control from UI** — every grinder step runnable from the UI with all parameters and tweaks selectable at each level. Fully wired to the pipeline agent.
10. **Update PIPELINE_V2.md** — replace proximity grind, profit grind, regime model sections with EV Grinder architecture. Update pipeline diagram. Update watchlist section.

### Code Cleanup (future)
11. **Remove dead ADR code from signal_filter.py** — once vetting sources from cluster files, remove: `measure_example_exit_distances()`, ADR floor classification in `_build_classified_signals()`, ADR-based `min_adr` filtering. The ceiling+exit race in clusters replaces all of it. Three current ADR computation spots: `signal_filter.py` (two places) and `_gather_raw_signal_clusters()` (two places) — consolidate to clusters only.

### Vetting
12. **Vet winner pile** — review 365 winners, add examples, loop if needed.

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
- **Phase 3 scores, it does not filter.** Every signal that passes Phase 2 makes the watchlist. The EV grinder predicts WR and MFE per signal. The watchlist rank-orders by EV. You take the top N — the bottom ones don’t get traded because better signals exist above them.
- **EV grinder replaces market_grinder + setup_grinder + combined optimizer.** One unified engine tests all ~4M market features + all setup-specific features in a single pass. Features compete on equal footing. Both directions captured (features that help AND features that hurt).
- **EV grinder uses signal-level scoring, not time-series correlation.** The old regime model correlated features with a rolling win-rate time series. The EV grinder evaluates features at the individual signal level, predicting both WR and MFE through quartile bucketing (captures nonlinear effects).
- **Additive scoring model is appropriate for 893 signals.** Interaction terms require splitting data into cells (e.g., 16 cells for two 4-bucket features = ~56 signals per cell). Too thin to be reliable. Additive model measures each feature across all 893 signals. Interactions that matter will show up as multiple features independently predicting WR/MFE. True interaction terms can be layered in as more examples accumulate.
- **Assumed stop of 1.0 ADR for EV calculation.** Losers don’t have move_adr (the setup broke — no meaningful “loser move” to measure). The loss side of EV uses a fixed 1 ADR stop assumption. This is a parameter, not a constant — adjustable without re-running the grinder.
- **All setup-specific features included for every setup type.** What’s redundant for DTSS might be the strongest predictor for another setup. The grinder’s screening step decides what matters per setup.
- **100% example pass rate required.** Any grinder result where an example fails is invalid.
- **Silent failures are dangerous.** The system produces plausible wrong numbers. Verify empirically.

---

## Shelved / Legacy

- `dartboard_grinder.py` — additive scoring washes out discrimination
- `hybrid_grinder.py` — correlated booleans don’t filter
- `proximity_grinder.py` — replaced by refinement grinder
- `profit_grinder.py` — removed from pipeline
- `setup_refiner.py` — legacy, unused
- `signal_filter.py` classified output — replaced by `raw_signal_clusters_{setup}.json`
- `market_grinder.py` — replaced by EV grinder. Results preserved for reference (`regime_dtss_20260313_095056.json`). Feature selection work (top 50 of 3M+) informs EV grinder.
- `setup_grinder.py` — replaced by EV grinder. Results preserved (`setup_dtss_20260313_135931.json`). All 6 features carry forward into EV grinder.
