# ScanPerfect Pipeline (2026-03-15, EV Grinder complete)

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
4. AI reviews each YES against the example library -- GREEN_LIGHT or FLAG
5. You see AI verdicts, one-click approve -- added to examples table
6. When enough examples have banked, trigger regrind from pipeline tab

**Self-improving:** More examples -- tighter centroid -- better entry candle scoring -- faster vetting -- more examples per session. The scorer gets better every time you use it.

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

## Phase 3 — Correlative Scoring (EV Grinder)

Phase 3 does not filter signals. Every signal that passes Phase 2 makes the watchlist. Phase 3 scores each signal with an accurate historical EV estimate so the watchlist can rank them.

This is what a discretionary trader does naturally — look at a setup and unconsciously weigh dozens of market and stock-specific factors to get a feel for "this one's A+ quality" vs "this one's marginal." The EV grinder does this with flawless accuracy against every historical signal that ever fired, weighted precisely, no recency bias, no forgetting, no emotional tilt.

### What the EV Grinder produces

Three numbers per signal:
- **Estimated win rate** — based on how signals with similar characteristics performed historically
- **Estimated median winner move (MFE)** — same basis
- **EV** — (WR × MFE) − ((1−WR) × 1.0 ADR assumed stop)

### Feature universe

The grinder tests every correlative feature available — both market conditions and stock characteristics — for their effect on win rate AND move size. Features that increase WR/MFE score positively. Features that decrease WR/MFE score negatively. Both directions matter.

**Market regime features** (~4M): 256 instruments × 15,805 expressions. Each instrument's expression value on the signal date. Covers SPY trend, VIX level, sector rotation, breadth, interest rates, credit spreads, bond market, commodities, international markets, and more.

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

All features are included for every setup type. The grinder's screening step determines which ones matter for each setup — something redundant for DTSS might be the strongest predictor for another setup.

### Architecture

**Step 1 — Feature matrix.** For each signal in the refinement output, look up the value of every candidate feature on that signal's date. Market features from the instrument caches. Setup features from OHLCV + external data. Result: 893 rows × ~4M columns.

**Step 2 — Univariate WR screening.** For each feature independently: bucket signals into deciles by feature value, compute win rate per decile. Keep features where the D10-D1 spread (top 10% vs bottom 10%) exceeds 10 percentage points. Deciles are far more discriminating than quartiles — random noise rarely produces a large D10-D1 spread because the extreme buckets are purer.

**Step 3 — Univariate MFE screening.** Same but for winner move_adr. Bucket into deciles, compute median move_adr per decile (winners only). Keep features where the D10-D1 spread exceeds 1.0 ADR.

**Step 4 — Per-instrument cap + Union survivors.** Each instrument's survivors are capped at top 200 by screening strength (max of WR spread and normalized MFE spread). This prevents correlated expression variants (SMA20, SMA21, SMA22) from flooding the results. A feature survives if it passed either the WR screen or the MFE screen. Tagged as "WR only", "MFE only", or "both."

**Step 5 — Deduplication.** Three-pass greedy dedup: (1) within-instrument correlation dedup (parallel, catches SMA20≈SMA21 on same instrument), (2) same-expression dedup (instant, keeps strongest instrument per expression — catches SPY_SMA20≈QQQ_SMA20), (3) cross-instrument correlation dedup (exact vectorized Pearson, catches remaining correlated pairs). Threshold: |r| ≥ 0.95. All CPU cores used.

**Step 6 — Percentile scoring.** For each survivor, compute percentile rank per signal. Store decile WR/MFE curves for interpolation. Category-balanced weighting: 50% market, 50% setup.

**Step 7 — Score every signal.** For each signal: compute quality_score (weighted percentile average, 0-100), interpolate decile curves for predicted WR and MFE, compute EV. All vectorized.

**Step 8 — Validation.** Bucket signals by predicted WR into deciles. Does actual WR match predicted WR per decile? Same for MFE. If predicted 85% WR signals actually win 85%, the model is calibrated.

### What this replaces

The EV grinder replaces both `market_grinder.py` and `setup_grinder.py`. Those were built as separate analyses — market conditions in one, stock characteristics in another, with a planned "combined optimizer" to merge them. The EV grinder does everything in one unified pass where all features compete on equal footing.

The old regime model correlated features with a win-rate time series (temporal correlation). The EV grinder evaluates features at the individual signal level and predicts both WR and MFE. It also captures nonlinear effects through decile bucketing — features that only matter at extremes are visible.

### Additive model (current design)

Each feature contributes independently. The scoring equation is a weighted sum of per-feature contributions. This is well-supported by 893 data points — each feature's effect is measured across all signals.

True feature interactions (e.g., "UVXY OBV matters more on high-priced stocks") are not captured. However, features that matter in combination will both independently predict WR/MFE, so the additive model ranks those signals highly anyway. The main risk is missing pairs that are individually weak but combined are strong — rare in practice, and undetectable with 893 signals.

Interaction terms can be layered in later as more examples accumulate across setup types.

### Runtime and compute

~5 minutes on local desktop (16 cores). Screening ~172s (I/O bound, 256 .npz files ~80MB each), dedup ~30s (three-pass), setup features ~30s. Parallelizes across all CPU cores. All local data, no API calls.

- Input: Refinement output + market cache + 5yr OHLCV cache + external data cache
- Output: Scoring equation (surviving features + decile curves + weights) + per-signal scores (quality_score, WR, MFE, EV) + validation stats
- Script: `scripts/ev_grinder.py`
- Saves to `local_runner/cache/ev_{setup}_{timestamp}.json`, mirrors to Railway

### For live scoring

A new signal fires tonight. Compute its feature values (market cache lookup + OHLCV features + external data). Compute percentile rank for each feature, category-balanced weighted average → quality_score + interpolated WR/MFE → EV. Done in milliseconds. The watchlist sorts by EV.

---

## Phase 4 — Profit Optimization

### a) Profit Grinder

Finds the optimal exit strategy for maximum account growth, not maximum per-trade profit. Runs on the EV-scored signal set at various Slider 1 (quality_score threshold) and Slider 2 (minimum predicted WR) settings.

**Entry prices:** Uses actual entry candle prices where available (examples and vetted YES picks have real entry candles). For non-example signals, uses the best available guess — the forward window bar that best matches the entry candle centroid (from entry_candle_scorer.py). This gives realistic fill prices for the simulation.

**What it brute-forces:**
- Stop loss levels (in ADR units) — the risk per trade
- Target levels (in ADR units) — where to take profits
- Trail stop parameters — when to switch from fixed stop to trailing
- Trim-and-trail strategies — sell a portion at target, trail the rest
- All of the above tested across multiple Slider 1/2 threshold combinations, because the optimal exit strategy may differ at different quality levels

**Objective function:** SQN (System Quality Number) — sqrt(N) × expectancy / stdev of R-multiples. This optimizes for consistency of returns, not raw size. A strategy with slightly lower average win but tighter distribution of outcomes will score higher, because it compounds better. Drawdowns kill compounding; SQN penalizes variance.

**Why SQN, not compound growth rate directly:** Compound growth rate is the ultimate goal, but it's sensitive to sequence of returns and position sizing assumptions. SQN measures the quality of the edge independent of sizing. Once you have the highest-SQN exit strategy, position sizing (Kelly fraction, fixed fractional, etc.) is a separate optimization that can be layered on top.

**Data source:** Full 5yr OHLCV cache for post-entry price simulation. Every bar after entry is available to test exit conditions against.

**Output:**
- Optimal stop/target/trail parameters at each slider threshold level
- SQN score per parameter combination
- Compounded equity curve (using fixed fractional sizing as baseline)
- Drawdown profile (max drawdown, avg drawdown, recovery time)
- Per-trade stats: avg win (R), avg loss (R), win rate, expectancy
- MFE capture efficiency: what % of available move does the exit strategy capture
- Comparison table: top parameter combos ranked by SQN

- Input: EV-scored signal set + entry candle data + Slider 1/2 ranges + 5yr OHLCV cache
- Output: Exit strategy parameters + equity simulation + SQN ranking
- Script: `profit_grinder.py` (exists, needs full rewire to new pipeline, new objective function, slider integration)

---

## Phase 5 — Live Watchlist

### a) EV Scoring

Each signal that fires tonight gets scored by the EV grinder's equation. Look up its market regime features and setup-specific features, run through the scoring curves, output estimated WR, estimated MFE, and EV. Milliseconds per signal.

### b) Live Nightly Workflow

After market close:
1. Run tonight's bars against signal + refinement conditions → signals that fired today
2. Score each signal using the EV equation → estimated WR, MFE, EV
3. Rank order by EV, highest to lowest
4. You take the top N that you have capital for — the bottom ones don't get traded, not because they're filtered out, but because better signals exist above them

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
| Phase 3: EV Grinder | ✅ Complete (inc 1-6) | 1,816 pre / 1,940 post features. Continuous percentile scoring, category-balanced weighting (50/50). Calibration: pre D1=19.1%→D10=64.1%, post D1=56.5%→D10=93.5%. RMSE 0.090 post. 247 genuine features, 1,569 redundant. File: `ev_dtss_inc6_*.json` (4.4MB) |
| Phase 4: Profit Optimization | ⏸ Not started | Script exists, needs full rewire: SQN objective, slider integration, entry candle prices |
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

### EV Grinder Dedup Result (2026-03-15)
- 256 instruments × 15,805 expressions + 10 setup features → ~4M features tested
- Screening: 51,204 pre / 51,201 post (decile D10-D1 ≥ 10pp WR or ≥ 1.0 ADR MFE, top-200 cap/instrument)
- Three-pass dedup (corr threshold 0.95):
  - Pass 1 (within-instrument, parallel): 51K → 24K (9s)
  - Pass 1.5 (same-expression, instant): 24K → 1,950 (22K expr dupes removed)
  - Pass 2 (cross-instrument, exact Pearson batched): 1,950 → 1,816 pre / 2,076 → 1,940 post (20s/12s)
- Verification: 0 violations pre, 0 violations post
- 3 setup + 1,813 market features pre (54 instruments), 1 setup + 1,939 market features post (51 instruments)
- Total time: 308s (~5 min)
- File: `ev_dtss_inc4_20260315_005210.json`

### EV Grinder Scoring Result (2026-03-15)
- Continuous percentile scoring (Option C) — no discrete quartile buckets
- Category-balanced weighting: market features (1,813) get 50%, setup features (3) get 50%
- Percentile rank per signal per feature via scipy.stats.rankdata, direction-flipped so higher = better
- Predicted WR + MFE via vectorized decile curve interpolation (np.searchsorted, no Python signal loop)
- Pre-refinement (893 signals): quality_score 20.3–75.9, predicted WR 0.31–0.49, EV 1.27–2.62
- Pre-refinement calibration: D1=19.1% → D10=64.1% actual WR (+45.0pp spread)
- Post-refinement (467 signals): top decile actual WR = 93.5%, bottom = 56.5%
- Post-refinement top 50% by quality: 234 signals, 86.8% actual WR, ~39/year
- All 65 examples scored, all verifications passed
- feature_percentiles stripped from output (40MB → 4.3MB)
- Total runtime: ~317s (~5 min, dominated by market screening at 171s)
- File: `ev_dtss_inc5_20260315_214318.json`

### EV Grinder Validation Result (2026-03-15)
- Decile calibration (pre, 893 signals): D1=19.1% → D10=64.1% actual WR, RMSE=0.114
- Decile calibration (post, 467 signals): D1=56.5% → D10=93.5% actual WR, RMSE=0.090
- Predicted WR compressed (additive model averaging effect) but ranking is monotonically correct
- Redundancy: 247 features both, 1,569 pre-only (refinement captured), 1,693 post-only
- File: `ev_dtss_inc6_20260315_221658.json`

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
1. **Depth progression output (refinement grinder)** — save level-by-level best path and cluster count in refinement JSON. Allows post-hoc condition threshold tuning without re-running via Settings Lock UI.
2. **Multi-run consensus (signal grinder)** — the beam search is non-deterministic: different runs find different condition sets with wildly different signal counts. Run N times (e.g. 5-10), keep conditions that appear in most runs. A condition in 8/10 runs is robust; a condition in 1/10 was a fluke. This stabilizes the foundation the entire downstream pipeline depends on. Signal grind margin (5%) is a search parameter and stays fixed — it is NOT tunable post-hoc (attempted and reverted 2026-03-16, produced worse results).
3. **Earnings proximity filter** — filter out signals/entries that are too close to earnings date to take safely. Needs to be applied in multiple spots: signal grind output, refinement grind classification, and live nightly scan.

### Phase 3 — EV Grinder
4. ~~**EV Grinder increments 5-6**~~ — ✅ DONE. Inc 1-6 complete. Continuous percentile scoring, category-balanced weighting, decile calibration tables, redundancy analysis. Output: `ev_{setup}_inc6_*.json` mirrored to Railway.

### Vetting UI
5. **Entry candle scorer integrated into refinement grind** — scorer runs automatically at the end of refinement grind, not as a separate step. Produces combined_score per winner signal (move_adr × entry candle similarity). Vetting UI sorts by combined_score when available, falls back to move_adr.
6. **AI vet queue** -- YES picks go to pending_examples (AI second-pass), then one-click approve adds to examples. Flow needs to work end-to-end.
7. **Workflow and ease-of-use improvements** -- many setups will be running, vetting is factory-line gruntwork. UI needs to be fast, keyboard-driven, minimal clicks per chart.

### Pipeline UI
8. **Pipeline flowchart UI (post-localization)** — replace current sidebar+panel layout with a visual flowchart. Each pipeline stage is a clickable node: Examples → Signal Grind → Exit Grind → Refinement Grind → Vetting → EV Grinder → Scan Tuning (sliders) → Profit Grind → Live Watchlist. Click a node to expand it inline (run controls, logs, results) or navigate to its tab (Examples, Vetting, Watchlist). Shows setup development progress visually. Build after localization for instant rendering and direct subprocess calls.
9. **Update PIPELINE_V2.md** — replace proximity grind, profit grind, regime model sections with EV Grinder architecture. Update pipeline diagram. Update watchlist section.

### Code Cleanup (future)
10. **Remove dead ADR code from signal_filter.py** — once vetting sources from cluster files, remove: `measure_example_exit_distances()`, ADR floor classification in `_build_classified_signals()`, ADR-based `min_adr` filtering. The ceiling+exit race in clusters replaces all of it. Three current ADR computation spots: `signal_filter.py` (two places) and `_gather_raw_signal_clusters()` (two places) — consolidate to clusters only.

### Vetting
11. **Vet winner pile** — review 365 winners, add examples, loop if needed.
12. **AI review quality improvement** — `review_samples.py` prompt needs tightening. Current issues: (a) AI sometimes returns "UNKNOWN" instead of APPROVE/REJECT, (b) reasoning is verbose chart description instead of pattern evaluation, (c) needs to compare candidate against example library centroid/characteristics, not just describe what it sees. The prompt should force a binary decision with 2-3 sentence reasoning focused on why this does or doesn't match the setup pattern. No chart narration.

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
- **Beam search instability is a known problem.** Individual runs produce usable signal sets but with low run-to-run overlap. Multi-run consensus (task #2) will fix this by keeping only conditions that appear across most runs. Until then, instability is accepted.
- **Signal grind margin (5%) is a search parameter, not a post-hoc knob.** Changing it to 0% was attempted (2026-03-16) and produced worse results (2,254 signals vs 1,218 at 5%). The margin fundamentally changes what conditions the beam search finds. It stays fixed at 5%.
- **Cluster-aware refinement scoring.** A losing cluster only counts as eliminated when ALL its bars are dead.
- **No re-scan/re-classify in refinement.** Phase 1 classification is truth.
- **Regime runs on pre-refinement data.** Post-refinement has too few losers for the model to learn from.
- **Examples run through full classification race.** Not skipped — they get exit_bar, ceiling, move_adr like every other cluster. Classification overridden to AUTO_WIN.
- **move_adr uses conservative entry price for non-examples.** Forward window max high = worst-case fill. Real entries will be better.
- **Setup-specific features are NOT from the expression cache.** The signal grind already mined all 16K expressions — anything in the cache that separates winners from losers would already be a signal/refinement condition. Setup-specific features must come from outside the cache (stock characteristics, cross-instrument RS, fundamentals).
- **RS formula is TC2000 PCF-based.** 5-day rolling average intraday % move × (avg_price / ATR50). Stock value minus SPY value = relative strength. Computed on both D1 and W1 timeframes.
- **Phase 3 scores, it does not filter.** Every signal that passes Phase 2 makes the watchlist. The EV grinder predicts WR and MFE per signal. The watchlist rank-orders by EV. You take the top N — the bottom ones don't get traded because better signals exist above them.
- **EV grinder replaces market_grinder + setup_grinder + combined optimizer.** One unified engine tests all ~4M market features + all setup-specific features in a single pass. Features compete on equal footing. Both directions captured (features that help AND features that hurt).
- **EV grinder uses signal-level scoring, not time-series correlation.** The old regime model correlated features with a rolling win-rate time series. The EV grinder evaluates features at the individual signal level, predicting both WR and MFE through decile curve interpolation (captures nonlinear effects).
- **Category-balanced weighting (50/50 market/setup).** Without balancing, 1,800+ market features drown out 3 setup features by headcount alone. Each category gets 50% of total weight. Within each category, features compete by individual screening strength. This ensures stock-level characteristics (price, ADR, RS) have equal voice to market regime in the quality score.
- **Continuous percentile scoring (Option C) replaces discrete quartiles.** Each signal gets a percentile rank (0-100) for each feature via scipy.stats.rankdata. Descending features are flipped so higher always = better. Weighted average → quality_score (0-100). Slider 2 sets a minimum quality_score threshold — truly continuous, not 4 discrete levels.
- **Additive scoring model is appropriate for 893 signals.** Interaction terms require splitting data into cells (e.g., 16 cells for two 4-bucket features = ~56 signals per cell). Too thin to be reliable. Additive model measures each feature across all 893 signals. Interactions that matter will show up as multiple features independently predicting WR/MFE. True interaction terms can be layered in as more examples accumulate.
- **Assumed stop of 1.0 ADR for EV calculation.** Losers don't have move_adr (the setup broke — no meaningful "loser move" to measure). The loss side of EV uses a fixed 1 ADR stop assumption. This is a parameter, not a constant — adjustable without re-running the grinder.
- **All setup-specific features included for every setup type.** What's redundant for DTSS might be the strongest predictor for another setup. The grinder's screening step decides what matters per setup.
- **Three-pass dedup catches all redundancy levels.** Pass 1 (within-instrument) catches expression variants. Pass 1.5 (same-expression) keeps only the strongest instrument per expression. Pass 2 (cross-instrument exact Pearson) catches remaining correlated pairs. All use np.isfinite to handle inf values from market caches.
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
- `market_grinder.py` — replaced by EV grinder. Results preserved for reference (`regime_dtss_20260313_095056.json`). Feature selection work (top 50 of 3M+) informs EV grinder.
- `setup_grinder.py` — replaced by EV grinder. Results preserved (`setup_dtss_20260313_135931.json`). All 6 features carry forward into EV grinder.
