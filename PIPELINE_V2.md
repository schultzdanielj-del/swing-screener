# Pipeline V2 — Ground-Up Design

**The goal, stated plainly:**

Find 2-7 high-quality short setups per day historically, where the math works: losers
under 1 ADR, winners median ~5-6 ADR, win rate high enough that the expectancy is
strongly positive. Compound at 2.5%/month for 20 years.

**What the system produces nightly:**

A ranked shortlist of tickers where the setup is present right now, the historical math
says this setup has strong positive EV in current market conditions, and the entry is
likely 1-2 days away.

---

## Core Design Principles

**It's a loop, not a pipeline.**
There is no "done." Every cycle produces tighter conditions, more examples, better
classifications, and a stronger regime model. You run live when the metrics say you're
ready. The loop keeps running and improving the live system indefinitely.

**Every layer runs as soon as it has minimum viable data.**
Don't wait for the loop to converge before running the regime model. Run everything
you can with what you have. Each layer strengthens every other layer on each cycle.

**Convergence means live-ready, not finished.**
The loop is ready to go live when: signal set is stable across two consecutive cycles,
win rate is meaningful across enough market regimes, and the EV on the full signal set
is positive. Those are measurable thresholds, not judgment calls.

**Revert without fear.**
Every cycle is a versioned snapshot. A bad grind doesn't corrupt anything. You compare
the new cycle to the previous one, keep it if it's better, revert if it's worse.

**The expression library is the substrate.**
12,131 expressions covering every TA concept: price structure, extensions, AVWAP,
volume character, momentum, MA relationships, LSP levels, algo lines, HTF context.
This is complete and is not being rebuilt. The brute force search against this library
is the correct method. What changes is what the search is optimizing against.

---

## The Loop

Each full cycle executes all layers in order, then feeds results back into the next cycle.

```
Cycle N:
  Layer 1: Grind          — examples vs universe → candidate conditions
  Layer 2: Scan           — apply conditions to 5yr history → raw signals
  Layer 3: Exit Filter    — apply exit condition → signals that moved
  Layer 4: Classify       — label every signal winner or loser
  Layer 5: Vet            — human review → AI queue → final approval → example library
  Layer 6: Regime         — correlate signal classifications vs market conditions
  Layer 7: Health Check   — measure cycle quality, compare to previous cycle
  → if healthier: promote to current. if worse: revert.
  → if live-ready: run tonight's watchlist from current conditions
  → queue Cycle N+1
```

**Regrind is manual, not automatic.**
Vetting and adding examples does not trigger a regrind. Examples accumulate until you
decide there are enough new ones to warrant a regrind. The UI shows a "regrind needed"
indicator when examples have been added since the last grind. You trigger the regrind
explicitly when ready — for example, after a vetting session that added 5-10 new examples,
or after clearing the AI review queue.

---

## Layer 1: Grind

**What it solves:** Find expression conditions that separate setup bars from the
universe. More examples → tighter discrimination → fewer false positives.

**Inputs:**
- Example library (all validated setup examples with entry dates)
- Expression series cache (12,131 expressions × 4,119 tickers × 5yr)
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
  stability vs previous cycle. These feed Layer 7.
- Results upload to Railway automatically as part of the grind run — no separate step.

**Output:**
- Condition set for this cycle
- Raw signal list (ticker + date) across 5yr history

**Reuse from V1:** `pyramid_grinder.py` — the engine is correct, fix D1 constraint.

---

## Layer 2: Scan

**What it solves:** Materialize the full historical signal set from the grind conditions.

**Inputs:**
- Condition set from Layer 1
- Expression series cache

**Method:**
- Scan all 4,119 tickers across full 5yr history
- Apply all conditions
- Deduplicate: consecutive signal bars for same ticker → keep rightmost
- Output: every (ticker, date) pair where conditions fire

**Output:**
- Deduped signal set: list of (ticker, date, bar_idx, close, adr)

**Reuse from V1:** `signal_filter.py` scan phase — works correctly.

---

## Layer 3: Exit Filter

**What it solves:** Identify which signals actually moved. Provides the primary
auto-classification signal and makes the vetting pile tractable.

**Inputs:**
- Signal set from Layer 2
- Exit condition (from exit grinder — see below)
- ADR threshold (minimum move to count as meaningful)

**Method:**
- For each signal, scan forward up to max_forward bars
- Check if exit condition fires
- If exit fires AND move >= ADR threshold: candidate winner
- If exit never fires OR move < ADR threshold: candidate loser
- Measure: signal close → exit close in ADR, MFE, capture efficiency

**Exit condition source:**
The exit grinder runs against the example set using the same expression library and
expr cache. It finds the expression condition that best describes "the move is over"
across all examples. This runs once per setup type and re-runs whenever the example
library grows materially. It is NOT re-run every cycle — only when examples change
enough to warrant it.

**ADR target:**
The ADR target for winner classification is derived from the sample median, not a
fixed threshold. Signals that trigger the exit but fall well short of sample-median
ADR are worth manual review — they may be technically valid but not sample-quality
moves. The goal is sample-type exits, not scratching tiny winners.

**Output:**
- Each signal labeled: exit_triggered (bool), move_adr, mfe_adr, capture_eff
- Signals split into: moved (candidate winner) / didn't move (candidate loser)

**Reuse from V1:** `signal_filter.py` exit phase + `exit_grinder.py` — both work.

---

## Layer 4: Classify

**What it solves:** Assign every signal a winner/loser label for use by the regime
model. More vetting = more accurate labels, but the model runs at any completeness level.

**Classification rules (in priority order):**

1. **Example → AUTO WIN.** Every signal bar that matches a validated example is a winner.
   Examples are the ground truth. They never get reclassified.

2. **Manual YES (AI-approved) → WIN.** Human reviewed, AI confirmed, human gave final
   approval. Signal is labeled WIN and added to example library.

3. **Exit triggered + move >= ADR threshold → AUTO WIN.** Mechanical confirmation
   that the setup resolved correctly.

4. **Manual NO → LOSS.** Human reviewed and rejected. Overrides auto-win. This is
   how you clean up signals that triggered the exit but were untradeable (chop,
   earnings gap, extended trend, etc.).

5. **Exit never triggered OR move < ADR threshold → AUTO LOSS.**

**Key property:** Every signal gets a label. No unclassified signals. The model runs
on all of them. Manual vetting improves label accuracy but is never a prerequisite.

**Output:**
- Full signal set with winner/loser labels and label source (auto/manual/ai-approved)
- Win rate on current signal set
- Ratio of manually vetted vs auto-classified signals

**Reuse from V1:** Classification logic already implemented in signal_filter.py +
vetting endpoints — transplant rules, rewire storage to cycle-versioned schema.

---

## Layer 5: Vet

**What it solves:** Improve label accuracy and grow the example library through a
two-stage human + AI gate. Neither stage alone is sufficient — human identifies
candidates, AI checks them against the example library, human makes the final call.

**When to vet:**
- When the health check (Layer 7) says label accuracy is limiting regime model quality
- When a new grind cycle produces new signals that haven't been seen before
- NOT as a default "always do this next" step — driven by metrics

**What gets surfaced for vetting:**
- Unvetted signals that passed the exit filter (candidate winners not yet confirmed)
- Ordered by move_adr descending — best candidates first
- Existing examples are excluded from the vetting pile

**Stage 1 — Human review:**
- One key per decision: 1=YES, 2=NO, 3=SKIP
- YES → goes to AI review queue (not yet in example library)
- NO → immediately labeled LOSS
- SKIP → stays in unvetted pile

**Stage 2 — AI review queue:**
- AI receives the chart + the full example library as context
- AI checks: does this chart genuinely match the setup pattern of the examples?
- AI outputs: GREEN LIGHT or FLAG with specific reasoning
- You review the AI verdict and make the final call: approve or reject
- Approved → added to example library + labeled WIN
- Rejected → labeled LOSS

**Why two stages:**
Human vetting at speed catches obvious candidates but can drift during long sessions.
The AI is checking against the full example library simultaneously — it doesn't get
fatigued or loosen criteria. It catches discretion drift. The human has final authority
but the AI acts as a quality control gate.

**Regrind trigger:**
Adding examples does NOT trigger an automatic regrind. The UI shows a persistent
"regrind needed" indicator whenever examples have been added since the last grind.
The workflow is: vet a batch → clear the AI review queue → check the indicator →
decide when you have enough new examples to warrant a regrind → trigger it manually.

**Convergence signal:**
Track examples added per cycle. When two consecutive cycles add near-zero new examples
from a full vetting pass, the example library has converged. Stop driving vetting as
the primary activity and focus on regime model quality instead.

**UI requirements:**
- Stage 1: one key per decision, instant chart load, no gaps
- Shows: ticker, date, move_adr, mfe_adr, capture_eff, exit_date
- 250-bar lookback, 80-bar forward, EMA 8/21, SMA 50/200, earnings markers
- Auto-advances after decision, shows N remaining and examples added this session
- Stage 2: AI review queue visible in UI, shows AI verdict + reasoning per pick
- Persistent "regrind needed" indicator showing N examples added since last grind

**Reuse from V1:** Chart vetting UI core — transplant with speed improvements and
AI queue addition.

---

## Layer 6: Regime Model

**What it solves:** Given tonight's market conditions, what is the expected win rate
for this setup? Weights signals up or down based on how favorable the current
environment is historically.

**When it runs:** Every cycle, as soon as there are enough classified signals to
produce meaningful correlations. Minimum viable: ~50 classified signals across at
least 6 months of history. Improves continuously as more signals get classified.

**Inputs:**
- Full classified signal set (winners + losers with dates)
- SPY OHLCV from expr cache (same 5yr data)
- Market internal indicators (see below)

**Market conditions computed at each signal date:**

Price and trend:
- SPY close vs SMA50, SMA200
- SPY EMA8/21/SMA50 stack state
- SPY extension from SMA50 and SMA200 in ATR units
- SPY SMA50 slope, SMA200 slope

Momentum:
- SPY RSI(14)
- SPY ROC(20)
- SPY % from 52-week high
- SPY % from 52-week low

Volatility:
- SPY ATR14 relative to its 50-day average
- SPY 20-day realized volatility

**Method:**
- Compute all indicators at each signal date using the expr cache
- Correlate each indicator with winner/loser classification
- Not hard buckets — weighted correlations across all indicators simultaneously
- Output: a continuous regime score for any given market fingerprint
- The score is: how similar is tonight's market to the historical fingerprint of
  winning signal environments for this setup?

**No hardcoded thresholds.** The correlations are data-derived and update every cycle
as more classified signals are added. The model gets sharper over time automatically.

**Rolling score for entry window:**
The signal fires on day X but the entry bar may not come until X+1 or X+2. The regime
score is computed as a rolling average across the signal-to-entry window, not just the
signal day. This prevents a signal from getting a high score on day X when conditions
deteriorate before the entry actually triggers.

**Output:**
- Regime score for tonight (0.0 to 1.0, where 1.0 = historically best conditions)
- Which indicators are most predictive for this setup
- Per-signal regime score for the historical record
- Expected win rate given tonight's regime score

**Reuse from V1:** Nothing built yet — new script `scripts/market_grinder.py`.

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
- win_rate_auto >= 40% (regime model will improve this further)
- ev_estimate > 0 (positive expectancy even without regime filtering)
- median_loser_adr < 1.0 (losers capped under 1 ADR)
- examples_added_last_two_cycles < 5 (example library approaching convergence)

**Output:**
- Health report for current cycle (all metrics above)
- Promote / revert / flag recommendation
- Live-ready: yes/no with which thresholds are not yet met

---

## Nightly Watchlist (live mode)

Once live-ready, after each market close:

1. Run today's conditions against tonight's bars (last bar of 5yr cache, updated nightly)
2. For each signal that fires: compute rolling regime score across the signal window
3. Rank signals by regime score × estimated win rate
4. For each ranked signal, run AI chart vet: does this look like the setup or is it
   garbage? Garbage signals are flagged, not removed — you see the flag and decide.
5. Pull fundamental context for each signal ticker: recent earnings result, sector
   theme momentum, any known catalyst. Informational only — does not filter signals.
6. Output: ranked list with regime-adjusted EV, AI vet status, and fundamental notes

**Watchlist entry contains:**
- Ticker, signal bar date, setup type
- Regime score (rolling, 0.0–1.0)
- Historical win rate at this regime score
- Expected move in ADR (from sample median)
- AI vet: LOOKS GOOD / FLAGGED + brief reason
- Fundamental notes: earnings recency/direction, sector theme, notable catalyst

The watchlist is the end product. Every cycle of the loop makes it more accurate.

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
health_metrics    — all Layer 7 metrics for this cycle
promoted_at       — timestamp when promoted to current
reverted_at       — timestamp if reverted
```

**Current pointer:** One cycle per setup type is "current." The watchlist always reads
from current. Reverting moves the current pointer back one cycle.

**Railway is the authoritative store.** All compute runs locally, all results upload
to Railway on completion. The UI reads only from Railway. Local files are ephemeral.

---

## Agent / Pipeline Agent

The pipeline agent maps UI step triggers to local compute commands. The mapping must
be exact — no step ID mismatch between UI and agent (BUG-002 in current system).

**Step ID → command mapping (V2):**

```
nightly          → nightly.py (OHLCV append + cache rebuild)
grind            → pyramid_grinder.py --setup {setup} --peak-target 3 --beam 10000 --depth 100
scan             → signal_filter.py --setup {setup} (scan + dedup phase only)
exit_filter      → signal_filter.py --setup {setup} (exit + classify phase)
exit_grind       → exit_grinder.py --setup {setup}
classify         → classify_signals.py --setup {setup} (apply classification rules)
regime           → market_grinder.py --setup {setup}
health           → health_check.py --setup {setup}
```

Every command uploads its output to Railway on completion. No exceptions.
The agent streams logs to Railway in real time. Status updates after every major step.

---

## What to Transplant from V1

These components are correct and reusable:

| Component | File | Status |
|-----------|------|--------|
| Expression library | `local_runner/brute_expressions.py` | ✅ Keep as-is |
| Expression cache | `local_runner/expr_cache_builder.py` | ✅ Keep as-is |
| Pyramid grinder engine | `local_runner/pyramid_grinder.py` | ✅ Keep, fix D1 constraint |
| OHLCV cache builder | `local_runner/cache_builder.py` | ✅ Keep as-is |
| Nightly pipeline | `local_runner/nightly.py` | ✅ Keep as-is |
| Signal scan (expr cache path) | `scripts/signal_filter.py` scan phase | ✅ Keep |
| Exit filter + measurement | `scripts/signal_filter.py` exit phase | ✅ Keep |
| Exit grinder | `scripts/exit_grinder.py` | ✅ Keep as-is |
| Classification logic | `server.py` vetting endpoints | ✅ Keep rules, rewire storage |
| Chart vetting UI | `app/index.html` vetting page | ✅ Keep, add AI queue + speed |
| Example library | Railway DB `examples` table | ✅ Keep — 71 DTSS examples |
| OHLCV data | Railway SQLite | ✅ Keep |

These need to be rebuilt or are new:

| Component | Notes |
|-----------|-------|
| Pipeline agent step mapping | BUG-002 — remap to V2 step IDs |
| Grinder → Railway upload | BUG-003 — add upload on completion |
| Cycle versioning / revert | New — data contract layer |
| Health check script | New — `scripts/health_check.py` |
| AI review queue | New — server.py endpoint + UI queue view |
| Market regime model | New — `scripts/market_grinder.py` |
| Nightly watchlist output | New — ranked list with regime score + AI vet + fundamentals |
| UI cycle management | New — health metrics, diff, revert button, regrind indicator |

---

## Build Order

Build in this order so each piece is useful immediately when complete:

1. **Fix BUG-001** (D1 row floor constraint) — makes the grinder reliable again
2. **Fix BUG-002** (agent step ID mapping) — makes the agent functional
3. **Fix BUG-003** (grinder → Railway upload) — closes the data flow gap
4. **Cycle versioning** — data contract, promote/revert logic in Railway
5. **Health check script** — measure cycle quality after every grind
6. **UI: health metrics + diff + revert + regrind indicator** — control surface for the loop
7. **AI review queue** — two-stage vetting gate, server endpoint + UI
8. **Market regime model** — runs on existing classified signal set
9. **UI: regime display + nightly watchlist** — the live product

At step 5, the loop is runnable end-to-end with the existing example library and
classified signals. Steps 6-9 improve the live product incrementally.
