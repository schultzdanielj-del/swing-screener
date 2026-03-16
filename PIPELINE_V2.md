# Pipeline V2 — Ground-Up Design

**The goal, stated plainly:**

Always be in the highest probability positions the market is offering right now.
The system solves the same optimization problem every night: given everything the market
is doing, across every setup type the system knows, what are the mathematically strongest
positions to be in tomorrow? Compound at 2.5%/month for 20 years.

**What the system produces nightly:**

A single unified ranked watchlist across all setup types. The list contains only the
absolute highest-potential setups — trimmed to the number a human can realistically
stalk, alert, and enter during the ~90-minute entry window. Ranked highest to lowest
by regime-adjusted EV. You focus exclusively on the top of the list. The AI vet filters
out trash before it reaches you.

---

## Core Design Principles

**It's a loop, not a pipeline.**
There is no "done." Every cycle produces tighter conditions, more examples, better
classifications, and a stronger EV model. You run live when the metrics say you're
ready. The loop keeps running and improving the live system indefinitely.

**Every layer runs as soon as it has minimum viable data.**
Don't wait for the loop to converge before running the EV model. Run everything
you can with what you have. Each layer strengthens every other layer on each cycle.

**Convergence means live-ready, not finished.**
The loop is ready to go live when: signal set is stable across two consecutive cycles,
win rate is meaningful across enough market regimes, and the EV on the full signal set
is positive. Those are measurable thresholds, not judgment calls.

**Revert without fear.**
Every cycle is a versioned snapshot. A bad grind doesn't corrupt anything. You compare
the new cycle to the previous one, keep it if it's better, revert if it's worse.
Revert = restore previous cycle's result data and delete the current bad one. One click.

**The system is setup-type agnostic.**
DTSS is the guinea pig. The target is ~10 setup types running simultaneously. Each
setup type has its own example library, grind cycle, and EV model — but they all
run on the same expression cache, grinder engine, and infrastructure. The watchlist
unifies them. More setup types means more candidates competing for the same slots —
only the highest EV opportunities make the cut regardless of setup type. A market that
is excellent for DTSS and poor for HTF longs will surface DTSS picks. The EV model
per setup type handles this automatically.

**The expression library is the substrate.**
15,805 expressions covering every TA concept: price structure, extensions, AVWAP,
volume character, momentum, MA relationships, LSP levels, algo lines, HTF context,
and extension structure (RSI/stochastic/Bollinger applied to the SMA extension series
themselves — treating extension exhaustion as a standalone chart). This is complete
and is not being rebuilt. The brute force search against this library is the correct
method. What changes is what the search is optimizing against.

**Tight for building, loose for live.**
The refinement grinder has a tunable knob — depth (how many conditions discriminate
winners from losers). During the example-building phase, depth is cranked to maximum:
aggressively culls losers, producing a small high-concentration pile that makes vetting
fast. The output is intentionally too tight to catch live signals reliably — but every
chart you vet is likely a real setup, so you bank examples fast. Once the example
library is fat, you reduce depth to lower curve-fit risk. The EV grinder handles
ranking from there. Depth is set via a slider in the Settings Lock step after the
grind cycle completes.

The signal grind margin (5%) is a search parameter, not a post-hoc knob. Changing it
fundamentally alters what conditions the beam search finds. It stays fixed.

---

## The Pipeline

Nine steps. Steps 1-4 are the vetting loop — repeat until convergence (no new
examples found). Steps 5-8 run once after convergence.

**Nightly auto-refresh (4:30pm ET, fully automated):**
  OHLCV append → daily cache → 5yr cache → expr cache → matrix → earnings → market cache (256 instruments)
  When you sit down, all data is current. No manual refresh needed.

```
The Vetting Loop (repeat until convergence):
  Step 1: Signal Grind      — examples vs universe → candidate conditions (5% margin, fixed)
  Step 2: Exit Grind        — optimal exit condition from example entry bar highs
  Step 3: Refinement Grind  — scans universe with step 1 conditions, clusters consecutive
                               bars, classifies via ceiling+exit race, then grinds
                               winners vs losers (cluster-aware). Runs at max depth.
                               Saves depth progression per level. Manual gate.
  Step 4: Vet               — review winner pile
                               YES → AI review → approve → examples → loop back to step 1

After Convergence (run once, in order):
  Step 5: EV Grinder        — unified correlative scoring: all ~4M market features +
                               setup-specific features → per-signal estimated WR, MFE, EV
  Step 6: Settings Lock     — review refinement depth slider, see downstream impact on
                               signal count / WR / EV calibration, lock in for nightly scanning
  Step 7: Profit Grind      — optimal exit strategy, maximizes compounded equity growth
                               (runs on the locked signal set from step 6)
  Step 8: Health Check      — cycle quality, EV, promote / revert / live-ready

Live:
  Nightly scan + EV scoring → unified watchlist (rank ordered, no filtering)
  Uses locked refinement depth from step 6
```

**Convergence:** When a full vetting pass on the refined winner pile produces no new
examples. The example library is as complete as the data allows.

**Refinement grind gate:** Manual decision. In early cycles with few examples, skip
step 4 and vet the full refinement output (step 3). Once enough examples exist that
the refinement grind produces stable conditions (not overfitted to a small sample),
enable step 4. The threshold is currently discretionary — will be data-derived after
2-3 setup types have been built through the pipeline.

**Regrind is manual, not automatic.**
Vetting and adding examples does not trigger a regrind. Examples accumulate until you
decide there are enough new ones to warrant a regrind. The UI shows a "regrind needed"
indicator when examples have been added since the last grind. You trigger the regrind
explicitly when ready.

---

## Layer 1: Grind

**What it solves:** Find expression conditions that separate setup bars from the
universe. More examples → tighter discrimination → fewer false positives.

**Inputs:**
- Example library (all validated setup examples with entry dates)
- Expression series cache (15,805 expressions × 4,119 tickers × 5yr)
- Universe OHLCV cache

**Method:**
- Pyramid grinder: examples vs tradable universe (4,167 tickers)
- 100% example pass rate enforced — every known example must pass all conditions
- Peak target: ≤3-5 signals/day at each historical tier
- Multi-pass: daily expressions first, then weekly, then monthly on top
- 5% margin on bounding boxes (fixed — this is a search parameter, not tunable post-hoc)

**What changes from V1:**
- D1 tier gets a hard row floor constraint: stop locking D1 conditions if surviving
  1wk rows would drop below a minimum threshold (e.g. 500 rows). This prevents D1
  from over-locking and destroying downstream tiers (BUG-001).
- After every grind, compute and store: n_conditions, n_signals, peak/day, signal
  stability vs previous cycle. These feed Layer 7.
- Results upload to Railway automatically as part of the grind run — no separate step.

**Output:**
- Condition set for this cycle
- Raw signal list (ticker + date) across 5yr history

**Reuse from V1:** `pyramid_grinder.py` — the engine is correct, fix D1 constraint.

---

## Layer 2: Refinement Grind

**What it solves:** Scan the universe, cluster consecutive signal bars, classify
winners/losers using a ceiling+exit race, then grind winners vs losers to find
conditions that eliminate losing clusters. This replaces the old separate Scan
(signal_filter.py) and Exit Filter steps — the refinement grinder handles
everything internally.

**Inputs:**
- Signal conditions from Layer 1 (step 1 pyramid result)
- Exit condition from exit grinder (step 2)
- Example library (from Railway API)
- Expression series cache (for scan + beam search)
- 5yr OHLCV cache (for price data)

**Phase 1: Gather raw signal clusters**

Scans the full universe with step 1 conditions, groups consecutive signal bars
into clusters (tracking rightmost + leftward bars), then classifies each cluster.

Cluster structure:
- Rightmost bar: the deduped signal bar (what you'd trade from in live scanning)
- Leftward bars: earlier consecutive bars where conditions also fired (sacrificial)

Classification uses a ceiling + exit race:
1. Forward window derived from examples: max distance (in bars) from leftmost
   signal bar in cluster to entry bar (scan bar + 1) across all examples, +10%.
   This accounts for the gap between signal firing and actual trade entry.
2. Ceiling = max(highest high across all signal bars in cluster, highest high
   in forward_window bars after rightmost bar). This captures the entry area
   including potential gap reversals.
3. After rightmost + forward_window, scan forward bar by bar:
   - Close above ceiling (for shorts) → AUTO_LOSS (setup broke)
   - Exit condition fires → AUTO_WIN (setup resolved)
   - End of available data → AUTO_WIN (setup held, never stopped out)
4. Example clusters (any bar matches a validated example) → AUTO_WIN regardless

No ADR floor for pile separation. A scratch or tiny win is not a loser — the
setup held. The profit side (how much winners win) gets handled by later steps.

**Phase 2: Cluster-aware beam search**

Must-pass set: Expression values at rightmost bars of winning clusters. These
define the bounding boxes (0% margin — exact min/max across all winners).

Expendable set: ALL bars from losing clusters (rightmost + leftward) + leftward
bars from winning clusters. The beam search tries to eliminate these.

Scoring is cluster-aware: a losing cluster only counts as eliminated when ALL
its bars are dead. Killing 3 of 4 bars in a losing cluster is useless — the
4th still fires in live scanning.

Always runs at maximum configured depth. The depth slider is applied post-hoc
via the Settings Lock step.

**Depth progression:**
At each depth level during the beam search, the output records: the condition set
at that level, the number of losing clusters eliminated, the number surviving, and
the resulting win rate. This data is saved as `depth_progression` in the output JSON.

The depth slider controls how aggressively winners are separated from losers:
- **Deep (high depth):** More conditions stacked, more losers killed, higher WR in
  the surviving pile. Best for vetting — clean pile, high hit rate.
- **Shallow (low depth):** Fewer conditions, more losers survive, less curve-fit risk.
  Best for live scanning — the EV grinder ranks the remaining signals.

**Phase 3: Combine + output**

Signal + refinement conditions combined. The output is the deduped signal set
(rightmost bars) minus signals whose entire losing cluster was eliminated.
No re-scan, no re-classify — the phase 1 classification is the truth.

**Output:**
- `all_conditions`: combined signal + refinement conditions
- `refinement_conditions_only`: just the new refinement conditions
- `winner_signals` / `loser_signals`: classified deduped signals
- `depth_progression`: condition set + cluster counts at each depth level
- `forward_window`: bars used for ceiling calculation
- Saved to `local_runner/cache/raw_signal_clusters_{setup}.json` (phase 1)
  and `local_runner/cache/refinement_{setup}_*.json` (full output)

**Script:** `pyramid_grinder.py --blackout`

**Reuse:** `scan_all_signals()` from signal_filter.py for the universe scan.
Beam search engine from pyramid_grinder.py (cluster-aware).

---

## Layer 3: Vet

**What it solves:** Improve label accuracy and grow the example library. The vetting
system is a standalone workbench outside the pipeline -- you use it when you want,
and the pipeline just sees a bigger example library next regrind.

**Two vetting modes:**

Signal Grind vet -- after signal grind, before refinement. Raw signals sorted by
move_adr (biggest movers first). No entry candle scoring available yet.

Post-Refinement vet -- after refinement grind produces the winner pile. The entry
candle scorer runs and produces a combined_score per signal, putting signals with
both a big ADR move AND a tradable entry candle at the top of the list.

**Entry Candle Scorer** (scripts/entry_candle_scorer.py):

A standalone vetting utility, not a pipeline step. Run on demand.

How it works:
1. Builds a centroid from all example entry candle expression vectors (16,051 dims)
2. Computes per-expression discrimination weights: for each expression, measures how
   tightly the entry candles cluster (entry_stdev) vs how spread out the winner pile
   forward window bars are (fw_stdev). Weight = fw_stdev / entry_stdev. Capped at
   95th percentile to prevent extreme outliers from dominating.
3. For each winner cluster: identifies scan range (leftmost bar through rightmost
   bar + forward_window -- same bars the refinement grinder used for classification).
4. Scores every bar in the scan range via weighted cosine similarity to the centroid.
   Keeps the single best-matching bar per cluster.
5. Computes combined_score = percentile_rank(entry_candle_score) x percentile_rank(move_adr).
6. Outputs entry_scores_{setup}.json, mirrored to Railway for the vetting UI.

Self-improving: more examples = tighter centroid = better entry candle scoring =
faster vetting = more examples per session. The flywheel accelerates.

DTSS validation (65 examples, 365 winners): examples average rank 127/365 with
+0.143 combined score separation vs non-examples. 46/65 examples in top half,
28/65 in top quarter.

**Vetting flow:**
1. Click "Update Scores" in vetting UI (runs entry candle scorer, ~10 seconds)
2. UI shows all winners sorted by combined_score
3. Vet top-down: 1=YES, 2=NO, 3=SKIP (keyboard-driven)
4. YES picks go to AI second-pass (pending_examples table, status=pending)
5. AI receives chart + full example library, checks pattern match
6. AI outputs GREEN_LIGHT or FLAG with reasoning
7. You review AI verdict, one-click approve -> added to examples table
8. "Regrind needed" indicator shows how many examples added since last grind

**When to vet:**
- After any refinement grind produces new signals
- When the health check says label accuracy is limiting EV model quality
- NOT as a default "always do this next" step -- driven by quality needs

**Convergence signal:**
When two consecutive full vetting passes add near-zero new examples, the example
library has converged. Stop driving vetting and focus on EV model quality.

**Output files:**
- entry_scores_{setup}_{timestamp}.json (archive)
- entry_scores_{setup}.json (latest pointer)
- Both mirrored to Railway

---

## Layer 4: EV Grinder

**What it solves:** Given a signal that fired tonight, what is its expected win rate,
expected move size, and expected value? This is the "dynamic contextual risk/reward
calculation" — the same thing a discretionary trader does naturally, but with flawless
accuracy against every historical signal, weighted precisely, no recency bias.

**Critical design principle: scoring, not filtering.** Every signal that passes the
causative filters (Phase 2) makes the watchlist. The EV grinder does not eliminate
signals. It scores them so the watchlist can rank them. The bottom of the list simply
doesn't get traded because better signals exist above them.

**Per setup type:** Each setup type gets its own EV model. Features that matter for
DTSS (e.g., "shorts win more when VIX is rising") may be irrelevant for long setups.

**When it runs:** After convergence — needs a stable classified signal set. Re-runs
whenever the refinement grind produces a new signal set.

**Feature universe (~4M+ features):**

Market regime features: 256 instruments × 15,805 expressions from the market cache.
Each instrument's expression value on the signal date. Covers broad market conditions:
SPY trend, VIX level, sector rotation, breadth, interest rates, credit spreads, bond
market, commodities, international markets, and more. Both directions — features that
boost WR/MFE AND features that tank WR/MFE.

Setup-specific features (OHLCV-derived, 6):
- Price level, ADR, dollar volume (20d), days since IPO
- RS vs SPY daily, RS vs SPY weekly

Setup-specific features (from Yahoo Finance fundamentals cache, 4):
- Market cap (shares outstanding × close at signal date)
- Volume/float ratio (daily volume / float shares)
- RS vs sector (ticker RS − avg RS of same-sector tickers)
- Sector RS vs SPY (avg sector RS − SPY RS)

All features are included for every setup type. The screening step determines which
matter per setup.

**Method:**

1. Build feature matrix: for each signal, look up every feature value on that date.
   893 rows × ~4M columns. Parallelized per instrument (~5-20 min).

2. Univariate WR screening: for each feature, bucket signals into deciles by value,
   compute win rate per decile. Keep features where D10-D1 spread exceeds threshold
   (default 10pp). Catches both directions.

3. Univariate MFE screening: same, but for winner move_adr (median per decile).
   Keep features where spread exceeds threshold (default 1.0 ADR).

4. Union survivors: feature passes if it cleared either WR or MFE screen. Tagged as
   "WR only", "MFE only", or "both." Per-instrument cap at top 200.

5. Deduplication: three-pass greedy dedup. Pass 1 (within-instrument) catches expression
   variants. Pass 1.5 (same-expression) keeps strongest instrument per expression.
   Pass 2 (cross-instrument exact Pearson) catches remaining correlated pairs.
   Threshold: |r| >= 0.95. All CPU cores used.

6. Scoring: continuous percentile ranking per signal per feature via scipy.stats.rankdata.
   Direction-flipped so higher = better. Category-balanced weighting: 50% market
   features, 50% setup features. Predicted WR + MFE via vectorized decile curve
   interpolation.

7. Score every historical signal: quality_score (weighted percentile average, 0-100),
   interpolated WR and MFE, EV = (WR × MFE) − ((1−WR) × 1.0 ADR assumed stop).

8. Validation: bucket signals by predicted WR into deciles. Does actual WR match
   predicted? Same for MFE. RMSE reported. If predicted 85% WR signals actually win
   85%, the model is calibrated.

**Additive model:** Each feature contributes independently. Well-supported by ~893
data points. Interaction terms (e.g., "UVXY matters more on high-priced stocks") are
not captured, but features that matter in combination will both independently predict
WR/MFE, so the additive model ranks those signals highly anyway. Interaction terms
can be layered in as more examples accumulate.

**Output:**
- Scoring equation: surviving features + decile curves + weights
- Per-signal scores: ticker, date, quality_score, estimated WR, estimated MFE, EV
- Validation stats: predicted vs actual WR/MFE by decile, RMSE
- Redundancy analysis: genuine vs redundant features
- Feature importance ranking

**Script:** `scripts/ev_grinder.py --setup {setup}`

**Replaces:** `market_grinder.py` + `setup_grinder.py` + the planned combined optimizer.
Those scripts are preserved for reference but are no longer pipeline dependencies.

---

## Layer 5: Settings Review & Lock-In

**What it solves:** After the grind cycle and EV scoring are complete, you need to
choose the operating point for live scanning. The refinement grinder ran at maximum
depth to give maximum flexibility. Now you decide where on the depth spectrum the
nightly scan should operate.

**When it runs:** After the EV grinder (step 5). This is a manual, UI-driven step —
no compute, just review and decision.

**What you see:**

A summary screen with one slider and its impact:

**Refinement depth slider (1 to max depth):**
- Reads `depth_progression` from the refinement grind output JSON
- Shows clusters eliminated + WR at each depth level
- At max depth: most losers killed, highest WR (vetting mode)
- At shallow depth: fewer conditions applied, less curve fit (live mode)
- Conditions literally fall off — at depth 5, only the first 5 refinement
  conditions are applied. The rest are ignored.

**Downstream impact preview:**
- Signal count at the chosen depth
- Estimated WR and EV calibration at that depth (from EV grinder data)
- Comparison to the deepest and shallowest extremes

**Lock-in:**
When you're satisfied, hit "Lock In." This writes a settings config:

```
scan_settings_{setup}.json:
{
  "setup_type": "dtss",
  "locked_at": "2026-03-16T...",
  "refinement_depth": 60,
  "signal_grind_source": "pyramid_dtss_mp_sig1218_pk14_20260310.json",
  "refinement_source": "refinement_dtss_cl102_pk5_20260313.json",
  "ev_source": "ev_dtss_inc6_20260315.json"
}
```

The nightly scan reads this config. It applies all signal conditions (unchanged) plus
only the first N refinement conditions (where N = locked depth), and scans tonight's
bars against that combined set.

**Lifecycle:**
- **Example building phase:** Lock at max depth. The nightly scan is intentionally
  tight — it's there for live testing and catching obvious signals, not for breadth.
  Your energy goes into vetting.
- **Live readiness phase:** Lock at moderate depth. The nightly scan catches more
  signals. The EV grinder ranks them. You trade the top of the list.
- **Re-lock any time:** After any regrind cycle, you review and re-lock. The old
  settings are overwritten. The nightly scan picks up the new settings automatically.

**Output:**
- `scan_settings_{setup}.json` — saved to local cache + mirrored to Railway
- The downstream profit grinder and nightly scan both read this config

---

## Layer 6: Profit Grind

**What it solves:** Finds the optimal trade exit strategy — stop, target, trail
parameters — that maximize account growth consistency (SQN), not raw per-trade
MFE capture. Brute-forces across the full parameter space at multiple EV slider
threshold levels.

**When it runs:** After Settings Lock (step 6). Runs on the signal set defined by
the locked settings — the signals you'd actually trade at the chosen depth, scored
by the EV grinder. The profit grind optimizes exit strategy for the top-ranked
signals, not the entire set.

**Entry prices:** Uses actual entry candle prices where available (examples and
vetted YES picks have real entry candles from the vetting flow). For non-example
signals, uses the forward window bar that best matches the entry candle centroid
(from entry_candle_scorer.py). This gives realistic fill prices for the simulation
rather than the conservative forward-window-max-high used in refinement.

**Distinction from Layer 2 (Exit Grind / signal_exit_grinder.py):**
Layer 2 finds a signal-level exit condition using the main expression cache —
it answers "did the setup work?" for classification purposes. The profit grind
answers "given the setup worked, when should you close the trade?" by brute-forcing
stop/target/trail parameters against actual post-entry price action.

**Parameter space (brute-forced):**
- Stop loss levels (in ADR units) — the risk per trade
- Target levels (in ADR units) — where to take profits
- Trail stop parameters — when to switch from fixed stop to trailing
- Trim-and-trail strategies — sell a portion at target, trail the rest
- All tested across multiple Slider 1/2 threshold combinations

**Objective function:** SQN (System Quality Number) — sqrt(N) × expectancy /
stdev of R-multiples. Optimizes for consistency, not raw size. A strategy with
slightly lower average win but tighter distribution compounds better because
drawdowns kill compounding and SQN penalizes variance. Once you have the
highest-SQN exit strategy, position sizing (Kelly, fixed fractional) is a
separate optimization layered on top.

**Data source:** Full 5yr OHLCV cache. Every bar after entry is available to
test exit conditions against.

**Script:** `scripts/profit_grinder.py --setup {setup}`

**Output:**
- Optimal stop/target/trail parameters at each slider threshold level
- SQN score per parameter combination
- Compounded equity curve (fixed fractional sizing baseline)
- Drawdown profile (max, avg, recovery time)
- Per-trade stats: avg win (R), avg loss (R), win rate, expectancy
- MFE capture efficiency
- Comparison table: top parameter combos ranked by SQN
- Uploads to Railway

---

## Layer 7: Health Check

**What it solves:** Tells you whether the new cycle is better or worse than the
previous one. Drives the revert decision. Drives the live-ready determination.

**Metrics computed after every cycle:**

Signal quality:
- n_signals: total signals in current cycle's deduped set
- peak_per_day: max signals on any single calendar day
- avg_per_day: average signals on active days
- signal_stability: % of signals that also appeared in the previous cycle's set
  (high stability = conditions are robust, low = overfit or D1 blew up)

Example coverage:
- examples_passing: must be 100% — hard fail if not
- examples_added_this_cycle: convergence signal
- cumulative_examples: total in library
- examples_since_last_grind: drives the "regrind needed" indicator

Classification quality:
- win_rate_auto: winners / total using auto-classification only
- win_rate_vetted: winners / total on manually vetted subset only
- pct_manually_vetted: what fraction of signals have human labels

EV on signal set:
- median_winner_adr: median move on auto-win signals
- median_loser_adr: median move on auto-loss signals (should be < 1 ADR)
- ev_estimate: win_rate × median_winner − loss_rate × median_loser

Settings impact:
- locked_refinement_depth: current refinement depth setting
- signal_count_at_locked_settings: how many signals the locked settings produce
- wr_at_locked_settings: win rate at the locked settings

Cycle delta (comparison to previous cycle):
- signal_count_delta: +N or -N signals vs previous cycle
- condition_count_delta: +N or -N conditions
- win_rate_delta: change in win rate
- stability_score: % signal overlap with previous cycle

**Promote vs revert rule:**
- If examples_passing < 100%: hard reject, do not promote
- If signal_count increased AND win_rate decreased: flag as likely regression
- If stability_score < 50%: flag as large change, require explicit confirmation
- Otherwise: auto-promote

**Live readiness thresholds (all must be true):**
- signal_stability >= 80% across two consecutive cycles
- n_signals produces 2-7/day average over 5yr history
- win_rate_auto >= 40% (EV grinder scoring will surface the best signals)
- ev_estimate > 0 (positive expectancy across the full signal set)
- median_loser_adr < 1.0 (losers capped under 1 ADR)
- examples_added_last_two_cycles < 5 (example library approaching convergence)
- settings_locked: true (Settings Lock step completed for this cycle)

**Output:**
- Health report for current cycle (all metrics above)
- Promote / revert / flag recommendation
- Live-ready: yes/no with which thresholds are not yet met

---

## Nightly Watchlist (live mode)

**Purpose:** Always be in the highest probability positions the market is offering
right now. The watchlist is the answer to that question every night — unified across
all setup types, ranked by EV.

More setup types means more candidates competing for the same watchlist slots. Only
the highest EV opportunities make the cut regardless of setup type. The EV model
per setup type surfaces the right setup for tonight's market automatically.

Once a setup type is live-ready, it contributes to the unified nightly watchlist.
After each market close:

1. Load locked settings from `scan_settings_{setup}.json` for each live setup type
2. For each live setup type: apply signal conditions + refinement conditions truncated
   to the locked depth. Scan tonight's bars.
3. For each signal that fires: compute EV score using the EV grinder's equation
   (look up market regime features + setup-specific features, run through scoring curves)
4. Run AI chart vet on each signal — chart shape check only, flags but doesn't remove
5. Pool all signals across all setup types into one list
6. Rank by EV, highest to lowest
7. You take the top N you have capital for — bottom doesn't get traded

**Watchlist entry contains:**
- Ticker, signal bar date, setup type
- Estimated win rate (from EV model)
- Estimated median move in ADR (from EV model)
- EV = (WR × move) − ((1−WR) × 1.0 ADR)
- AI vet status: LOOKS GOOD / FLAGGED + one-line reason if flagged

**Scoring is milliseconds per signal.** Feature lookups from the market cache +
OHLCV + fundamentals cache, then percentile-based weighted average. No heavy compute
at scan time.

The watchlist is the end product. Every cycle of the loop, on every setup type, makes
it more accurate.

---

## Data Contract

All layers read from and write to a single versioned store. A cycle is a snapshot —
all layers within a cycle share the same cycle_id. Downstream layers cannot read from
a different cycle than their inputs came from.

**Cycle record:**
```
cycle_id          — timestamp-based unique ID
setup_type        — e.g. "dtss"
status            — running / complete / reverted
conditions        — full condition set with tier, expression, low, high, filter_power
signals           — deduped signal set with classification labels
exit_condition    — expression, direction, threshold used for exit filter
locked_settings   — refinement_depth from Settings Lock
health_metrics    — all Layer 7 metrics for this cycle
promoted_at       — timestamp when promoted to current
reverted_at       — timestamp if reverted
```

**Current pointer:** One cycle per setup type is "current." The watchlist always reads
from current.

**Revert mechanics:** Revert = restore the previous cycle's result data as current and
delete the bad cycle. One operation. No manual reconstruction. The previous cycle's
conditions, signals, locked settings, and health metrics become current instantly.

**Railway is the authoritative store.** All compute runs locally, all results upload
to Railway on completion. The UI reads only from Railway. Local files are ephemeral.

---

## Agent / Pipeline Agent

The pipeline agent maps UI step triggers to local compute commands. The mapping must
be exact — no step ID mismatch between UI and agent.

**Step ID → command mapping (V2) — WIRED 2026-03-07:**

```
signal_grind     → pyramid_grinder.py --setup {setup} --beam 10000 --depth 100 --peak-target 3
                   (5% margin, fixed — this is a search parameter)
exit_grind       → signal_exit_grinder.py --setup {setup}
refinement_grind → pyramid_grinder.py --setup {setup} --blackout
                   (gathers raw signal clusters + ceiling/exit classification
                    + cluster-aware beam search + combine conditions, all in one)
                   (always runs at max depth; saves depth_progression per level)
vet              → is_manual=True, no agent command (UI-only)
ev_grind         → ev_grinder.py --setup {setup}
                   (unified correlative scoring: market + setup features → WR, MFE, EV)
settings_lock    → is_manual=True, no agent command (UI-only — writes scan_settings_{setup}.json)
profit_grind     → profit_grinder.py --setup {setup}
health           → cycle_health.py --setup {setup}
```

Note: The old scan step (signal_filter.py) is no longer a pipeline dependency.
The refinement grinder handles scanning, clustering, and classification internally.
signal_filter.py is retained for standalone signal analysis and chart vetting.
It no longer produces `classified_{setup}.json` — that file has been replaced by
`raw_signal_clusters_{setup}.json` produced by the refinement grinder itself.

Every command uploads its output to Railway on completion. No exceptions.
The agent streams logs to Railway in real time. Status updates after every major step.

---

## What to Transplant from V1

These components are correct and reusable:

| Component | File | Status |
|-----------|------|--------|
| Expression library | `local_runner/brute_expressions.py` | ✅ Keep as-is |
| Expression cache | `local_runner/expr_cache_builder.py` | ✅ Keep as-is |
| Pyramid grinder engine | `local_runner/pyramid_grinder.py` | ✅ Keep — D1 cap=15 implemented |
| OHLCV cache builder | `local_runner/cache_builder.py` | ✅ Keep as-is |
| Nightly pipeline | `local_runner/nightly.py` | ✅ Keep as-is |
| Signal scan (expr cache path) | `scripts/signal_filter.py` scan phase | ✅ Keep |
| Exit filter + measurement | `scripts/signal_filter.py` exit phase | ✅ Keep |
| Exit grinder | `scripts/exit_grinder.py` | ✅ Keep as-is |
| Classification logic | `server.py` vetting endpoints | ✅ Keep rules, rewire storage |
| Chart vetting UI | `app/index.html` vetting page | ✅ Keep, add AI queue |
| Example library | Railway DB `examples` table | ✅ Keep — 68 DTSS examples (65 valid scan bars) |
| OHLCV data | Railway SQLite | ✅ Keep |

These need to be rebuilt or are new:

| Component | Notes |
|-----------|-------|
| Pipeline agent step mapping | ~~BUG-002~~ — **FIXED 2026-03-07** |
| Grinder → Railway upload | ~~BUG-003~~ — **FIXED 2026-03-08** — `grind_uploader.py` |
| Cycle versioning / revert | New — data contract layer |
| Health check script | New — `scripts/health_check.py` |
| AI review queue | New — server.py endpoint + UI queue view |
| EV grinder | ✅ **DONE** — `scripts/ev_grinder.py` (replaces market_grinder + setup_grinder) |
| Fundamentals cache | ✅ **DONE** — `scripts/fetch_fundamentals.py` (Yahoo Finance sector/float/shares) |
| Settings Lock UI | New — refinement depth slider, writes scan_settings JSON |
| Depth progression (refinement grind) | New — condition set + cluster counts at each depth level |
| Nightly watchlist | New — unified ranked list across all setup types, reads locked settings |
| UI: EV display + watchlist | New — EV scores, WR, MFE per signal |
| UI cycle management | New — health metrics, diff, revert button, regrind indicator |

---

## Build Order

Build in this order so each piece is useful immediately when complete:

1. ~~**Fix BUG-001**~~ (D1 row floor constraint) — **DONE** — D1 cap=15
2. ~~**Fix BUG-002**~~ (agent step ID mapping) — **DONE 2026-03-07**
3. ~~**Fix BUG-003**~~ (grinder → Railway upload) — **DONE 2026-03-08** — `grind_uploader.py`, see `GRIND_STORAGE.md`
4. **Cycle versioning** — data contract, promote/revert logic in Railway
5. **Health check script** — measure cycle quality after every grind
6. **UI: health metrics + diff + revert + regrind indicator** — control surface for the loop
7. **AI review queue** — two-stage vetting gate, server endpoint + UI
8. ~~**Fundamentals cache**~~ — **DONE** — `fetch_fundamentals.py`
9. ~~**EV grinder**~~ — **DONE** — `scripts/ev_grinder.py`
10. **Depth progression** — refinement grind saves condition set + cluster counts per depth level
11. **Settings Lock UI** — depth slider + lock-in, writes `scan_settings_{setup}.json`
12. **Profit grind** — trade exit optimization, reads locked settings
13. **UI: EV display + unified nightly watchlist** — the live product

**Current status (2026-03-16):** DTSS Phase 2 complete (signal grind + exit grind +
refinement grind). EV grinder complete (inc 1-6). Signal grind margin slider attempted
and reverted — margin is a search parameter, not post-hoc tunable. Next: depth
progression (step 10), then Settings Lock UI (step 11), then vet winner pile.
