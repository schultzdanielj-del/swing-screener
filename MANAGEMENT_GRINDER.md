# Management Grinder — Phase 4: Trade Management

> **⚠ DEFERRED**
>
> Cannot be built until signal filter classification (Problem 1) is solved. Trade management optimization depends on correct W/L classification.
>
> Scripts exist: `scripts/entry_grinder.py` (stop management, v1 — needs rewrite), `scripts/profit_grinder.py` (exit optimization, Inc 1-4 complete). Both standalone, not wired into pipeline.

**See `DEPENDENCY_MAP.md` for full I/O of both scripts.**

---

## Purpose

Given that you're in a winning trade, how do you manage it for minimum adverse exposure and maximum profit capture?

Phase 4 in the architecture. Runs AFTER classification (signal filter) and EV scoring (EV grinder). Two sub-problems:

1. **Stop management** — Initial stop placement, stop movement protocol, breakeven timing.
2. **Exit optimization** — TA-based exit conditions for profit-taking, multi-stage trim.

---

## Why This Is One Component

The old architecture had separate entry grinder and profit grinder with crossed wires. The entry grinder tried to solve both stop-based classification AND stop management in one script. Classification is signal-relative (no entry bar). Management is entry-relative (entry bar TA is fair game).

Clean separation (decided 2026-04-13):
- **Classification → signal_filter.py** — signal-relative W/L separation (see `SIGNAL_FILTER.md`)
- **Management → management grinder** — entry-relative stop + exit optimization (this doc)

The management grinder is one architectural component using multiple scripts. Classification and management have different inputs, different constraints, different outputs. They do not share code paths.

---

## Entry-Relative: What That Means

Management works with examples and signals where entry bars are known or probable. Entry bar TA is available because:

- **Examples** have confirmed entry dates in SQLite
- **Probable-entry signals** have entry candle probability scores from the entry candle scorer

This is the fundamental difference from classification, which operates without entry bar knowledge. Management can reference entry candle highs, lows, closes, and indicators computed at the entry bar.

---

## Correlative Weighting

The example set is small (A+ winners only). Optimizing management solely on examples risks curve-fitting. The management grinder broadens the dataset using correlative weighting:

| Signal type | Weight |
|-------------|--------|
| Examples (confirmed entry bars) | 1.0 |
| Vetted YES (approved in vetting UI) | 1.0 |
| Vetted NO (rejected in vetting UI) | **Excluded** |
| Unvetted winners | Scaled by entry candle probability |

Examples anchor the rules with hard constraints (100% must survive). Weighted signals prevent overfitting by testing management rules against a larger, more representative dataset.

### Entry candle boundary box (potential pre-filter)

Before applying weighted optimization, filter out signals where no candle in the forward window falls inside the boundary box of example entry candle characteristics. If no plausible entry candle exists in the forward window, the signal shouldn't influence management rules.

---

## Sub-component 1: Stop Management

**Script:** `scripts/entry_grinder.py` (v1 exists, needs rewrite)

### What it answers

- **Initial stop placement.** Tightest initial stop all example winners survive. In practice, likely stop under entry candle low — under 1 ADR.
- **Ratchet protocol.** How does the stop move bar-by-bar after entry? Most aggressive tightening schedule that doesn't shake out winners on normal pullbacks.
- **Breakeven timing.** How many bars until it's safe to move to breakeven?

### Session 1 findings (2026-04-13)

**Rolling ratchet abandoned.** A monotonic ratchet that tightens every bar was tested. It tightens too fast and kills winners on pullbacks. Needs a different approach.

**OHLCV indicator sweep.** 5,762 indicators tested for survival from signal bar, 4,473 achieve 100% close-below survival. Tightest: EMA_61, kelt5_atr7x2.25, ema5_atr7x2.2, sma_131. These are signal-relative results — stop sizes are too wide for practical management (EMA_61 is far from price). Entry-relative analysis should find much tighter stops.

**Key insight.** The actual risk on example winners is small — stop under entry candle low, under 1 ADR. The wide indicator results are an artifact of forcing everything into signal-relative terms. Management doesn't have that constraint.

### Status

Needs a fresh design pass. v1 mixed classification and management logic. Once classification (Problem 1) is solved, stop management can be rebuilt purely entry-relative.

---

## Sub-component 2: Exit Optimization

**Script:** `scripts/profit_grinder.py` (Inc 1-4 complete, standalone)

### What it answers

"Given the setup fired and you entered the trade, what TA expression condition tells you the move is over and it's time to take profit?"

Distinct from the exit grinder (Phase 2b), which finds an expression condition for classification. The profit grinder finds expression conditions for profit-taking. These may be completely different expressions.

### Core engine

**Per signal:**
1. Load ticker .npz from expression cache
2. Find entry bar by date in cache dates
3. Slice forward window: entry bar through entry bar + max_forward
4. Result: matrix of (n_forward_bars x n_expressions)

**Per candidate expression x threshold x direction:**
1. Walk each signal's forward expression series
2. Find first bar where expression crosses threshold in specified direction
3. Record: exit bar index, captured move (entry_high to exit bar close), bars held
4. Apply trigger rules: hard gate on examples/vetted YES, 1-ADR loss penalty for unvetted non-triggers

**Threshold generation:**
- Per expression: gather all forward-window values across all signals
- Generate N thresholds from percentiles (5th to 95th, N=50)
- Test each threshold in both directions (above and below)

**Expression filtering:**
- Exclude boolean aggregations (ct_, st_, tir_ prefixes) — monotonically increasing, structurally wrong for exit detection

### Trigger requirement

**Hard gate (binary, pass/fail):**
- Must trigger on 100% of examples + vetted YES signals. Any miss = candidate rejected.

**Unvetted winners — no gate, penalty via weighted scoring:**
- Triggers: scored normally (captured move in ADR) at the signal's entry_candle_score weight.
- Does NOT trigger: scored as 1-ADR loss at the signal's entry_candle_score weight.

A candidate that misses high-score signals takes a heavy hit (those are charts you'd trade). A candidate that misses low-score signals barely feels it (weight is negligible). The scoring function IS the regulation. No bins, no hardcoded thresholds.

### Multi-stage trim

Finds expression conditions for up to 3 exit stages:

**1-stage (full exit):** Expression A fires → sell 100% of position.

**2-stage (trim + exit):** Expression A fires → trim X%. Expression B fires after A → sell remainder. Trim % is a search parameter.

**3-stage (trim + trim + exit):** Expression A → trim X%. Expression B (after A) → trim Y%. Expression C (after B) → sell remainder.

Each stage's expression evaluates only on bars AFTER the previous stage fired. Prevents conditions that fire simultaneously.

### Stats panel (per candidate)

All stats weighted by entry_candle_score:

- **Trade quality:** SQN, expectancy, profit factor, payoff ratio, win rate
- **Per-trade:** avg/median/best/worst moves (ADR), avg bars held, max consecutive W/L
- **Risk:** max drawdown, avg drawdown, max drawdown duration
- **Growth:** CAGR, Sharpe, Sortino, Calmar
- **Equity:** fixed fractional sizing (1% risk, $100K start), chronological
- **Capture:** MFE capture efficiency, floor capture, median capture

### Loss assumption

1 ADR on the loss side for all stats. Not a real stop — the assumed loss for computing expectancy/SQN/risk metrics. Also the penalty for non-triggering unvetted winners.

### Parallelization

- ProcessPoolExecutor (not ThreadPoolExecutor — GIL blocks CPU-bound numpy)
- 3D expression array saved to temp .npy, workers open via np.load(mmap_mode='r')
- Default 12 workers
- No API calls, no network I/O — all local cache reads

### Runtime (actual, DTSS)

- **1-stage:** 3.3 min (12 workers, i5-12600k). 12,878 exprs x ~100 thresholds x 2 dirs = 1.1M tested.
- **2-stage:** 7.9 min. Top 300 1-stage x 50 final exits x ~100 thresholds x 2 dirs x 3 trim% = 2.9M tested.
- **Total:** ~12 min including data loading.
- **Memory:** ~2.25 GB for 3D array (364 x 120 x 12,878 float32), memory-mapped.

### Multi-stage search strategy

Full expression^3 combos for 3-stage is infeasible (12K^3 = 1.7 trillion). Cascading approach:
1. **1-stage:** brute-force all expressions as single exits. Keep top N by weighted median capture.
2. **2-stage:** for each top N exit, test all expressions as trim triggers. Keep top M combos.
3. **3-stage:** for each top M 2-stage combo, test all expressions as middle trigger. Keep top K.

### Build status

Inc 1-4 complete. Produces weighted multi-stage exit optimization results. Consumes EV grinder output.

---

## Output

Both sub-components produce JSON results mirrored to Railway via file_mirror. See `DEPENDENCY_MAP.md` for file paths and consumers.

---

## Pipeline Position

```
Signal grind → Exit grind → Signal filter (classification) → EV grinder → MANAGEMENT GRINDER → Watchlist
```

Depends on:
1. Signal filter classification being solved first
2. EV grinder output for scored signals
3. Entry candle scorer for correlative weighting

---

## Key Design Decisions

- **TA-based exits only.** No fixed ADR price targets, no fixed stop losses. The chart determines the exit through expression conditions.
- **entry_candle_score weighting, not combined_score.** Weight reflects "would I enter this trade" — information available at trade time. Move size is the outcome, not the entry decision.
- **Scoring is the regulation.** No trigger rate floors, no bins, no hardcoded thresholds. N signals = N resolution. The weighted scoring function self-regulates.
- **Hard gate on examples + vetted YES only.** These are confirmed tradable — exit/stop rules must work on every one. Binary pass/fail.
- **Improves with vetting.** Zero vetting = usable but noisy. Heavy vetting = highly accurate. Every vetting session tightens both the entry candle scorer (centroid) and the management grinder (more high-weight signals).
- **Cascading multi-stage search.** Keeps compute tractable while exploring full expression space at each stage.
- **Expression cache only.** No live computation. Same expression library, same cache, same as all other grinders.

---

## Supersedes

This spec replaces:
- `ENTRY_GRINDER.md` (classification parts moved to `SIGNAL_FILTER.md`, management parts here)
- `archive/shelved_docs/PROFIT_GRINDER.md` (exit optimization content preserved here)
