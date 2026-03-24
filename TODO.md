# ScanPerfect Pipeline (2026-03-20, Scan Tuning UI built, Profit Grinder Inc 1-4 done)

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
  a) Profit Grinder — TA-expression-based exit optimization

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

**Entry Candle Scorer** (scripts/entry_candle_scorer.py) -- standalone vetting utility, not a pipeline step. Builds a centroid from all example entry candle expression vectors (16,051 dimensions), computes per-expression discrimination weights (how tightly entry candles cluster vs how spread out winner forward window bars are, capped at 95th percentile), then scores each winner forward window bar against the centroid using weighted cosine similarity. Scan range per cluster: leftmost bar through rightmost bar + forward_window. Best-matching bar per cluster is the entry candle score. Combined score = percentile rank of entry_candle_score x percentile rank of move_adr. Output: entry_scores_{setup}.json mirrored to Railway. Also consumed by profit grinder: the raw `entry_candle_score` (not combined_score) provides tradability weighting for exit optimization.

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
- Overfitting risk: More refinement depth = more conditions = higher curve fit risk. Depth progression output (TODO) will allow post-hoc condition threshold tuning.

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
- ~~EPS growth QoQ~~ (scrapped — paid API required)
- ~~EPS growth trailing 4Q~~ (scrapped)
- ~~Revenue growth QoQ~~ (scrapped)
- ~~Revenue growth trailing 4Q~~ (scrapped)

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

Finds the optimal TA-expression-based exit conditions for maximizing trade profit. Brute-forces the expression cache (same 15,805 expressions, excluding boolean aggregations ct_/st_/tir_) testing every expression × threshold × direction against forward expression values of all winner signals.

**Weighting:** Examples and vetted YES get weight 1.0 with a hard trigger requirement (exit must fire on all of them). Vetted NO signals excluded. Unvetted winners weighted by raw `entry_candle_score` — cosine similarity to the example entry candle centroid. This measures "does this chart look like an entry I'd take" using only information available at trade time. Move size is explicitly NOT in the weight — it's the outcome, not the entry decision.

**No trigger gate on unvetted winners.** If the exit doesn't trigger on an unvetted winner, that signal is scored as a 1-ADR loss at its `entry_candle_score` weight. The weighted scoring self-regulates: missing high-score signals (charts you'd trade) is heavily penalized, missing low-score signals (charts you'd skip) is negligible. No bins, no hardcoded thresholds — fully continuous, self-referencing.

**Multi-stage trim:** 1-stage (full exit) + 2-stage (optional trim + exit). 3-stage shelved for now. Trim is optional — if it doesn't fire before the final exit, full position rides to final exit (no penalty). Trim percentages: 33%, 50%, 67%.

**Two distinct forward windows:**
- `entry_window`: how far from the signal bar the refinement grinder looked for entry_high (from cluster file, 6 bars for DTSS)
- `exit_horizon`: how far to search for exit expressions (default 120 bars)

**Entry bar detection (deterministic, no price matching):**
- Examples: actual entry_date from SQLite DB → find in OHLCV date index → compute offset
- Non-examples: argmax of highs within entry_window bars (replicates refinement grinder)

**Already-true-at-entry filter:** If the exit expression condition is already satisfied at the entry_high bar, that signal is treated as non-triggering. Killed 92% of candidates in DTSS.

**Parallelization:** ProcessPoolExecutor + numpy mmap. 3D expression array (364 × 120 × 12,878) saved to temp .npy, workers open with `np.load(mmap_mode='r')`. Saturates all CPU cores. Threading does NOT work (GIL prevents real parallelism for CPU-bound numpy loops).

**2-stage optimization:** Only top 300 1-stage expressions tested as trim candidates (not all 12,878). Thresholds computed once per expression. Workers compute expectancy only — full stats on top 500 after all workers finish.

**All stats weighted by `entry_candle_score`.** SQN, expectancy, equity curve, drawdown — all reflect performance on signals you'd actually trade.

- Input: EV-scored signal set + entry candle scores + vetting decisions (SQLite) + expression cache + 5yr OHLCV
- Output: Exit expression candidates + weighted stats + equity curves + per-trade detail
- Scripts: `profit_grinder.py` + `profit_grinder_2stage.py`
- See `PROFIT_GRINDER.md` for full spec

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
| Phase 2c: Refinement Grind | ✅ Done | 100 refinement conditions, 420/519 clusters killed, 78% WR. Depth progression saved (100 levels). |
| Phase 3: EV Grinder | ✅ Complete (inc 1-6) | 1,816 pre / 1,940 post features. Continuous percentile scoring, category-balanced weighting (50/50). Calibration: pre D1=19.1%→D10=64.1%, post D1=56.5%→D10=93.5%. RMSE 0.090 post. 247 genuine features, 1,569 redundant. File: `ev_dtss_inc6_*.json` (4.4MB) |
| Phase 4: Profit Optimization | ✅ Inc 1-4 done | 835 1-stage, 7,703 2-stage combos. Top: slope_xavgc21_off7_adr14 below -1.1675 (Exp=6.730). Output: profit_dtss_20260320_133906.json (0.3 MB). ~12 min total. |
| Scan Tuning UI | ✅ Built | Entry tab (setup/market/depth/WR sliders) + Exit tab (objective/expression/trim). SPY bubble chart overlay. Settings auto-save. EV grinder outputs setup_score + market_score per signal. |
| Phase 5: Live Watchlist | ⏸ Not built | |

### Refinement Grind Result (2026-03-20)
- 872 clusters: 353 WIN, 519 LOSS (64/66 examples matched by entry_date)
- 100 refinement conditions (depth capped at 100)
- 420/519 losing clusters eliminated (80.9%)
- All 353 winners pass all conditions
- Pre-regime win rate: 78.1% (353 / 452)
- Forward window: 6 bars (max 5 + 10%)
- **Depth progression: 100 levels saved (D1=41.7% WR → D100=78.1% WR)**
- Example matching uses hardcoded entry_date (date proximity), not bar indices
- Nightly 5yr cache now appends only — no more OHLCV/expr cache date drift
- File: `refinement_dtss_cl99_pk5_20260320_*.json`

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


### Profit Grinder Result (2026-03-20)
- **1-stage:** 12,878 expressions × ~100 thresholds × 2 dirs = 1,104,338 tested
  - Already-true-at-entry filter killed 92% (1,020,982)
  - Hard gate fails: 63,037
  - Raw candidates: 20,319 → dedup → 835
  - Top: `slope_xavgc21_off7_adr14 below -1.1675` (Exp=6.730, Capture=1.00±0.13, Bars med=20)
  - All top 10 are "below" direction — DTSS exits when price drops hard below moving averages
  - Runtime: 3.3 min (12 ProcessPoolExecutor workers)
- **2-stage:** Top 300 trim exprs × 50 final exits × ~100 thresholds × 2 dirs × 3 trim%
  - 2,864,500 tested, 46,054 raw combos → dedup → 7,753
  - 7,703 combos beat their 1-stage final exit
  - Best: `slope_xavgc21_off3_adr14 below` trim 67% + `slope_xavgc21_off7_adr14 below` final (Exp=6.767, +0.038 vs 1-stage)
  - Same expression family (slope of 21-day XMA), just shorter lookback offset (3 vs 7) fires earlier
  - Finding: 2-stage trim adds marginal value for DTSS. Shorts are smash-and-grab — the exit fires when the move is done, trimming earlier just captures less.
  - Runtime: 7.9 min (12 workers)
- **Total runtime:** 11.8 min (was 2+ hours before ProcessPoolExecutor + mmap optimization)
- File: `profit_dtss_20260320_133906.json` (0.3 MB, mirrored to Railway)
- Latest pointer: `profit_dtss.json`
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

### NEXT: Phase 5 (Live Watchlist) or Vet Winner Pile
0. **Profit Grinder** — ✅ COMPLETE (Inc 1-4). 1-stage brute-force (12,878 exprs, ~3 min), 2-stage trim search (top 300 exprs × 50 final exits, ~8 min). ProcessPoolExecutor + numpy mmap. Output saved to profit_{setup}_{ts}.json + Railway mirror. DTSS finding: 2-stage trim adds marginal value for shorts (+0.038 ADR best).

### Localization
1. ~~**Localize everything**~~ — ✅ DONE. See `LOCALIZE.md`.

### Grinder Improvements
2. ~~**Depth progression output (refinement grinder)**~~ — ✅ DONE. Beam search saves condition set + cluster counts + WR at each depth level in `depth_progression` key of refinement JSON. Settings Lock slider reads this.
3. ~~**Multi-run consensus (signal grinder)**~~ — ✅ DONE (2026-03-24). Full consensus pipeline built on `v2-consensus` branch. 15 real + 15 permuted signal grinds with 50% universe subsampling, randomized pass ordering, 0% margin during grind. Bootstrap z-score (Meinshausen stability selection + permutation test). z > 3 gate. Deterministic scan → exit re-grind → 10 refinement runs with loser subsampling → two-test refinement consensus (stability + binomial significance). Orchestrator: `scripts/run_consensus_pipeline.py`. Test runner: `scripts/test_consensus_pipeline.py` (8/9 steps verified, Step 9 profit grinder RAM constraint on larger populations). Signal grind margin: 0% during consensus grind, 5% applied at lock time by consensus engine.
4. **Earnings proximity filter** — filter out signals/entries that are too close to earnings date to take safely. Needs to be applied in multiple spots: signal grind output, refinement grind classification, and live nightly scan.

### Infrastructure
5. **Remove Railway from nightly data flow** — currently yfinance → Railway → local (round trip). Should be yfinance → local directly, Railway gets a backup copy. The nightly refresh should not depend on Railway for any compute or data. Railway is seed vault only. **Partially fixed 2026-03-20:** 5yr cache `LIMIT 1260` removed (was dropping old bars causing OHLCV/expr cache date drift). Nightly step 3 changed from full rebuild to append-only. OHLCV and expr cache now stay in sync permanently. **Remaining:** Steps 1-2 still go through Railway. Yfinance → local direct pipeline not yet built.

### Phase 3 — EV Grinder
5. ~~**EV Grinder increments 5-6**~~ — ✅ DONE.

### Advanced Scoring (Phase 3 upgrades)
14. **XGBoost tree model replacing additive scorer** — The additive percentile model tests each feature independently. A tree-based model natively discovers interactions ("VIX above X AND price below Y AND ADR above Z") without enumerating them. Handles nonlinear relationships, missing values, mixed feature types. Industry standard for tabular prediction. At 893 signals with ~1,800 features, needs aggressive regularization (max_depth 3-4, heavy subsampling) to avoid overfitting, but cross-validated properly should beat the additive model. **Script built:** `ev_tree_scorer.py`. Wired into `ev_grinder.py` as A/B comparison — runs alongside additive model, prints D10-D1 spread comparison + disagreement analysis. Tree scores saved as `tree_predicted_wr`, `tree_ev` etc on each signal. **Status: awaiting first real-data A/B run.** Deps: `pip install xgboost shap`.
15. **SHAP-based score decomposition** — SHAP (SHapley Additive exPlanations) gives per-signal, per-feature contribution scores from the tree model that are mathematically exact. Replaces percentile-weighted-average with interaction-aware decomposition. A signal's predicted WR reflects "VIX is high AND breadth is narrowing AND low-float" as a combined contribution, not three independent additive terms. Already built into `ev_tree_scorer.py` — SHAP values aggregate into setup_score / market_score for slider compatibility. Ships with #14.
16. **Signal novelty detection (Isolation Forest / LOF)** — Instead of scoring by feature quality, detect which signals are unlike anything in the training set. A new signal fires tonight — is it within the learned distribution of historical signals, or an outlier the model has no basis to predict? High-novelty signals get a confidence haircut on predicted WR. Overfitting defense mechanism that nothing in the current pipeline addresses. Applies at Phase 5 (live scoring time). Cheap to compute — seconds on the existing feature matrix.
17. **Survival analysis (time-to-resolution modeling)** — Currently every signal is binary WIN/LOSS. A winner that hits ceiling in 3 days is fundamentally different from one that takes 40 days. Survival analysis (Cox proportional hazards or Kaplan-Meier per feature bucket) models time-to-resolution, not just outcome. Features that accelerate time-to-ceiling are more valuable because faster resolution = more capital turns/year. Completely different objective function. Needs time-to-exit per signal — profit grinder already computes this. Cuts across Phase 3 (EV Grinder) and Phase 4 (Profit Grinder).
18. **Bayesian optimization for slider settings** — Sliders (refinement depth × setup floor × market floor × WR floor) are currently set manually. Bayesian optimization (Gaussian Process surrogate + acquisition function) searches the joint space to find the combination that maximizes out-of-sample EV or SQN. Replaces human intuition with principled search that handles the tradeoffs (more depth = higher WR but fewer signals = lower capital utilization). Applies at Phase 5 (scan configuration).

### Vetting UI
6. **Entry candle scorer integrated into refinement grind** — scorer runs automatically at the end of refinement grind, not as a separate step. Produces combined_score per winner signal (move_adr × entry candle similarity). Vetting UI sorts by combined_score when available, falls back to move_adr.
7. ~~**AI vet queue**~~ — ✅ BUILT. YES → pending → AI review → approve on Examples tab. Pending items show as chart grid inside Add Examples.
8. ~~**Workflow and ease-of-use improvements**~~ — ✅ BUILT. Keyboard-driven (1/2/3/↑↓), floating Yes button at click position, mouse wheel zoom, V/U/N checkboxes, chart preloading, entry bar requirement enforced.

### Pipeline UI
9. ~~**Pipeline flowchart UI**~~ — ✅ DONE. 7-node flowchart with color-coded cards, animated expansion, unlock progression, two feedback loops. No tabs — flowchart is the interface.
10. ~~**Update PIPELINE_V2.md**~~ — ✅ DONE (2026-03-17). All docs updated to reflect local-first architecture and 7-node flowchart.
11. ~~**Scan Tuning workspace**~~ — ✅ DONE (2026-03-20). Two-tab (Entry/Exit) workspace with SPY bubble chart. Entry: setup/market feature floors, refinement depth, WR floor. Exit: SQN/max profit objective, exit expression, trim. EV grinder outputs setup_score + market_score per signal. Settings auto-save on close, restore on open. Profit grinder output browsed in exit tab — no re-run needed.

### Code Cleanup (future)
11. **Remove dead ADR code from signal_filter.py** — once vetting sources from cluster files, remove: `measure_example_exit_distances()`, ADR floor classification in `_build_classified_signals()`, ADR-based `min_adr` filtering. The ceiling+exit race in clusters replaces all of it. Three current ADR computation spots: `signal_filter.py` (two places) and `_gather_raw_signal_clusters()` (two places) — consolidate to clusters only.

### Vetting
12. **Vet winner pile** — review 365 winners, add examples, loop if needed.
13. **AI review quality improvement** — `review_samples.py` prompt needs tightening. Current issues: (a) AI sometimes returns "UNKNOWN" instead of APPROVE/REJECT, (b) reasoning is verbose chart description instead of pattern evaluation, (c) needs to compare candidate against example library centroid/characteristics, not just describe what it sees. The prompt should force a binary decision with 2-3 sentence reasoning focused on why this does or doesn't match the setup pattern. No chart narration.

---

## Infrastructure

- **Repo:** `schultzdanielj-del/swing-screener`, branch `v2`
- **Railway:** `https://web-production-e3025.up.railway.app` — seed vault only (see LOCALIZE.md)
- **Expression cache:** 16,051 expressions, ~21 GB
- **5yr OHLCV cache:** ~4,169 tickers (all available history, no bar limit)
- **File mirror:** Grind results → Railway via `file_mirror.py` (stays post-localization for Claude access)
- **Nightly refresh:** 4:30pm ET, 9 steps + seed vault push, fully automated
  - Step 3 (5yr cache) now **appends** new bars only — never rebuilds, never drops old bars
  - Step 4 (expr cache) appends new bars to match
  - OHLCV and expr cache stay permanently in sync
- **UI:** DM Sans, grayscale design system. PySide6 desktop app (`scanperfect.py`)

---

## Key Design Decisions

- **Pyramid with D1 cap=15 is the official signal grind engine.** Experimental grinders (dartboard, hybrid) failed. Shelved.
- **Beam search instability solved by consensus pipeline.** Multi-run consensus (15 real + 15 permuted, stability selection + permutation testing) keeps only conditions that appear consistently across subsampled runs. z > 3 gate validates the pattern is real. Individual runs still vary, but consensus extracts the stable core. See `SIGNAL_GRINDER.md` and `REFINEMENT_GRINDER.md` for full spec.
- **Signal grind margin: 0% during consensus, 5% at lock time.** Consensus grind uses exact min/max (0% margin) for sharpest conditions during discovery. The consensus engine applies 5% margin when locking conditions for downstream use. This replaces the old fixed 5% margin during single-run grinds. The 5% margin at lock time exists because 68 examples are a sample — the sample min/max underestimates the true range.
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
- **Profit grinder uses `entry_candle_score` weighting, not `combined_score`.** Move size is future information — not available at trade time. Entry candle similarity is the right tradability proxy.
- **Profit grinder: no trigger gate on unvetted winners.** Non-triggers scored as 1-ADR loss at their weight. The scoring function self-regulates. No bins, no hardcoded thresholds.
- **Profit grinder: TA-expression-based exits only.** No fixed ADR price targets or stop losses. The chart determines the exit through expression conditions.
- **Example matching uses hardcoded entry_date, never bar indices.** Bar indices shift when OHLCV or expr caches rebuild. Dates are stable. The refinement grinder matches examples to clusters by finding the cluster with ANY signal bar within forward_window bars before the example's entry_date. Two-pass: seed distance 3 for forward_window computation, then forward_window as distance for classification. Fixed 2026-03-20 after OHLCV/expr cache drift caused 65→7 example matches.
- **Nightly 5yr cache appends only, never rebuilds.** The old full-rebuild approach dropped bars from the front of the 5yr window, drifting OHLCV start dates away from the expr cache. Fixed 2026-03-20. The expr cache append already worked correctly (add to end only).

---

## Shelved / Legacy

- `dartboard_grinder.py` — additive scoring washes out discrimination
- `hybrid_grinder.py` — correlated booleans don't filter
- `proximity_grinder.py` — replaced by refinement grinder
- `setup_refiner.py` — legacy, unused
- `signal_filter.py` classified output — replaced by `raw_signal_clusters_{setup}.json`
- `market_grinder.py` — replaced by EV grinder. Results preserved for reference (`regime_dtss_20260313_095056.json`). Feature selection work (top 50 of 3M+) informs EV grinder.
- `setup_grinder.py` — replaced by EV grinder. Results preserved (`setup_dtss_20260313_135931.json`). All 6 features carry forward into EV grinder.
