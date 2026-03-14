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

---

## The Pipeline

Eight steps. Steps 1-4 are the vetting loop — repeat until convergence (no new
examples found). Steps 5-7 run once after convergence.

**Nightly auto-refresh (4:30pm ET, fully automated):**
  OHLCV append → daily cache → 5yr cache → expr cache → matrix → earnings → market cache (256 instruments)
  When you sit down, all data is current. No manual refresh needed.

```
The Vetting Loop (repeat until convergence):
  Step 1: Signal Grind      — examples vs universe → candidate conditions
  Step 2: Exit Grind        — optimal exit condition from example entry bar highs
  Step 3: Refinement Grind  — scans universe with step 1 conditions, clusters consecutive
                               bars, classifies via ceiling+exit race, then grinds
                               winners vs losers (cluster-aware). Manual gate.
  Step 4: Vet               — review winner pile
                               YES → AI review → approve → examples → loop back to step 1

After Convergence (run once, in order):
  Step 5: EV Grinder        — unified correlative scoring: all ~4M market features +
                               setup-specific features → per-signal estimated WR, MFE, EV
  Step 6: Profit Grind      — optimal exit strategy, maximizes compounded equity growth
  Step 7: Health Check      — cycle quality, EV, promote / revert / live-ready

Live:
  Nightly scan + EV scoring → unified watchlist (rank ordered, no filtering)
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

**What changes from V1:**
- D1 tier gets a hard row floor constraint: stop locking D1 conditions if surviving
  1wk rows would drop below a minimum threshold (e.g. 500 rows). This prevents D1
  from over-locking and destroying downstream tiers (BUG-001).
- After every grind, compute and store: n_conditions, n_signals, peak/day, signal
  stability vs previous cycle. These feed Layer 6.
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

**Phase 2: Cluster-aware beam search** (not yet built — current engine uses rightmost bars only)

Must-pass set: Expression values at rightmost bars of winning clusters. These
define the bounding boxes — every winning signal must still pass.

Expendable set: ALL bars from losing clusters (rightmost + leftward) + leftward
bars from winning clusters. The beam search tries to eliminate these.

Scoring is cluster-aware: a losing cluster only counts as eliminated when ALL
its bars are dead. Killing 3 of 4 bars in a losing cluster is useless — the
4th still fires in live scanning.

**Phase 3: Combine + output**

Signal + refinement conditions combined. The output is the deduped signal set
(rightmost bars) minus signals whose entire losing cluster was eliminated.
No re-scan, no re-classify — the phase 1 classification is the truth.

**Output:**
- `all_conditions`: combined signal + refinement conditions
- `refinement_conditions_only`: just the new refinement conditions
- `winner_signals` / `loser_signals`: classified deduped signals
- `forward_window`: bars used for ceiling calculation
- Saved to `local_runner/cache/raw_signal_clusters_{setup}.json` (phase 1)
  and `local_runner/cache/refinement_{setup}_*.json` (full output)

**Script:** `pyramid_grinder.py --blackout`

**Reuse:** `scan_all_signals()` from signal_filter.py for the universe scan.
Beam search engine from pyramid_grinder.py (to be upgraded to cluster-aware).

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

2. Univariate WR screening: for each feature, bucket signals into quartiles by value,
   compute win rate per quartile. Keep features where Q4-Q1 spread exceeds threshold
   (default 10pp). Catches both directions.

3. Univariate MFE screening: same, but for winner move_adr (median per quartile).
   Keep features where spread exceeds threshold (default 1.0 ADR).

4. Union survivors: feature passes if it cleared either WR or MFE screen. Tagged as
   "WR only", "MFE only", or "both."

5. Deduplication: greedy dedup by inter-feature correlation (< 0.95). Each survivor
   adds genuinely new information.

6. Scoring curves: for each survivor, store quartile boundaries + WR/MFE per quartile.
   This is the lookup table for live scoring.

7. Score every historical signal: look up quartile for each feature, weighted average
   of WR and MFE contributions (weighted by feature spread strength). Output: estimated
   WR, estimated MFE, EV = (WR × MFE) − ((1−WR) × 1.0 ADR assumed stop).

8. Validation: bucket signals by predicted WR into deciles. Does actual WR match
   predicted? Same for MFE. If predicted 85% WR decile wins 85%, the model is calibrated.

**Additive model:** Each feature contributes independently. Well-supported by ~893
data points. Interaction terms (e.g., "UVXY matters more on high-priced stocks") are
not captured, but features that matter in combination will both independently predict
WR/MFE, so the additive model ranks those signals highly anyway. Interaction terms
can be layered in as more examples accumulate.

**Output:**
- Scoring equation: surviving features + quartile boundaries + weights
- Per-signal scores: ticker, date, estimated WR, estimated MFE, EV
- Validation stats: predicted vs actual WR/MFE by decile
- Feature importance ranking

**Script:** `scripts/ev_grinder.py --setup {setup}`

**Replaces:** `market_grinder.py` + `setup_grinder.py` + the planned combined optimizer.
Those scripts are preserved for reference but are no longer pipeline dependencies.

---

## Layer 5: Profit Grind

**What it solves:** Finds the optimal trade exit condition — when to close the
position to maximize compounded equity growth. Uses a bespoke exit expression
library (exit_expressions.py) specifically designed to evaluate the traded range
(entry bar forward to exit bar).

**When it runs:** After EV grinder (step 5). Runs on the EV-scored signal set —
the profit grind optimizes exit strategy for the signals you'd actually take
(highest EV), not the entire signal set.

**Distinction from Step 2 (Exit Grind / signal_exit_grinder.py):**
Step 2 finds a signal-level exit condition using the main expression cache —
it answers "did the setup work?" for classification purposes. The profit grind
answers "given the setup worked, when should you close the trade?" using a
dedicated expression set built for post-entry price action analysis.

**Objective function:** Compound growth rate, not raw MFE capture. A strategy
that captures 60% MFE consistently may outcompound one that captures 90% with
high variance, because drawdowns from volatile strategies kill position sizing.

**Expression set:** ~4,500 bespoke exit expressions (exit_expressions.py):
- Move captured (close, low, ADR, ATR, % normalized)
- MFE, capture efficiency
- Extension structure dynamics in the traded range
- MA reclaim sequences, volume character
- Boolean aggregations across 7 forward windows (5-60 bars)

**Script:** `scripts/profit_grinder.py --setup {setup}`

**Output:**
- Optimal exit strategy parameters
- Compounded equity curve + drawdown profile
- Per-example capture stats (median, mean, distribution)
- Uploads to Railway

---

## Layer 6: Health Check

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

1. For each live setup type: run tonight's bars against signal + refinement conditions
2. For each signal that fires: compute EV score using the EV grinder's equation
   (look up market regime features + setup-specific features, run through scoring curves)
3. Run AI chart vet on each signal — chart shape check only, flags but doesn't remove
4. Pool all signals across all setup types into one list
5. Rank by EV, highest to lowest
6. You take the top N you have capital for — bottom doesn't get traded

**Watchlist entry contains:**
- Ticker, signal bar date, setup type
- Estimated win rate (from EV model)
- Estimated median move in ADR (from EV model)
- EV = (WR × move) − ((1−WR) × 1.0 ADR)
- AI vet status: LOOKS GOOD / FLAGGED + one-line reason if flagged

**Scoring is milliseconds per signal.** Feature lookups from the market cache +
OHLCV + fundamentals cache, then quartile-based weighted average. No heavy compute
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
health_metrics    — all Layer 6 metrics for this cycle
promoted_at       — timestamp when promoted to current
reverted_at       — timestamp if reverted
```

**Current pointer:** One cycle per setup type is "current." The watchlist always reads
from current.

**Revert mechanics:** Revert = restore the previous cycle's result data as current and
delete the bad cycle. One operation. No manual reconstruction. The previous cycle's
conditions, signals, and health metrics become current instantly.

**Railway is the authoritative store.** All compute runs locally, all results upload
to Railway on completion. The UI reads only from Railway. Local files are ephemeral.

---

## Agent / Pipeline Agent

The pipeline agent maps UI step triggers to local compute commands. The mapping must
be exact — no step ID mismatch between UI and agent.

**Step ID → command mapping (V2) — WIRED 2026-03-07:**

```
signal_grind     → pyramid_grinder.py --setup {setup} --beam 10000 --depth 100 --peak-target 3
exit_grind       → signal_exit_grinder.py --setup {setup}
refinement_grind → pyramid_grinder.py --setup {setup} --blackout
                   (gathers raw signal clusters + ceiling/exit classification
                    + cluster-aware beam search + combine conditions, all in one)
vet              → is_manual=True, no agent command (UI-only)
ev_grind         → ev_grinder.py --setup {setup}
                   (unified correlative scoring: market + setup features → WR, MFE, EV)
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
| EV grinder | New — `scripts/ev_grinder.py` per setup type (replaces market_grinder + setup_grinder) |
| Fundamentals cache | New — `scripts/fetch_fundamentals.py` (Yahoo Finance sector/float/shares) |
| Nightly watchlist | New — unified ranked list across all setup types |
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
8. **Fundamentals cache** — `fetch_fundamentals.py` pulls sector/float/shares from Yahoo Finance
9. **EV grinder** — unified correlative scoring, `scripts/ev_grinder.py` ← **NEXT**
10. **Profit grind** — trade exit optimization, `scripts/profit_grinder.py`
11. **UI: EV display + unified nightly watchlist** — the live product

**Current status (2026-03-14):** DTSS Phase 2 complete (signal grind + exit grind +
refinement grind all done). Regime model and setup correlation analysis completed and
shelved — replaced by EV grinder. Next: build EV grinder (step 9), then vet winner pile.
