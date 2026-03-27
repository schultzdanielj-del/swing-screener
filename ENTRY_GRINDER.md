# Entry Grinder — Specification

**Created:** 2026-03-27
**Script:** `scripts/entry_grinder.py` (not yet built)
**Status:** Spec only. Awaiting approval before implementation.

---

## WHY THIS EXISTS

The classification system needs a stop level to determine win/loss for each signal. For fade setups (DTSS), the entry IS the extreme — the highest high of the forward window is naturally the entry/stop. The DTSS classification logic works because entry = stop = forward window extreme.

For breakout/breakdown setups, this doesn't work. The entry is a confirmation after a level breaks, not the extreme of the forward window. The stop is below the entry candle or the candle before it — not the lowest low of some window. The forward window extreme has no relationship to where you'd actually place your stop.

The entry grinder solves this by brute-forcing the most aggressive stop placement and stop-raise strategy that all examples survive. Since all examples are confirmed winners, any stop that keeps you in every example trade is a valid stop definition. The tightest one that works becomes the classification rule for non-example signals.

---

## WHAT IT DOES

Takes the example set for a breakout/breakdown setup and finds:

1. **Best static stop** — a single price level (derived from reference bars) that no example ever breaches from signal through exit.
2. **Best ratcheting stop** — a bar-by-bar stop path through the forward window that only moves in the favorable direction (up for longs, down for shorts), that no example ever breaches. The most aggressive surviving path = the tightest realistic stop for this setup type.
3. **Breakeven window** — the number of bars after the signal where it becomes safe to move the stop to breakeven (entry candle low + 1 ADR). Found by measuring when each example's price permanently leaves the entry zone, taking the max across all examples.

The output defines the stop rule and breakeven window used by the classification system to separate wins from losses on non-example signals.

---

## INPUTS

1. **Raw signal clusters file** — `local_runner/cache/raw_signal_clusters_{setup}.json`. Provides example cluster data (signal bar index, forward window, matched entry dates).
2. **5yr OHLCV cache** — `local_runner/cache/universe_ohlcv_5yr.pkl`. Provides price data (highs, lows, closes) for each example ticker.
3. **Setup direction** — from `setups` table in `data/scanperfect.db`.
4. **Exit condition** — from `data/signal_exit_grind/signal_exit_{setup}.json`. Provides the expression, threshold, and direction. Used to determine each example's exit bar so the stop survival check has an endpoint.

---

## REFERENCE BARS

For each example, 4 bars define the candidate stop levels:

| Bar | Description |
|-----|-------------|
| Pre-signal bar | The bar immediately before the rightmost signal bar |
| Signal bar | The rightmost signal bar in the cluster |
| FW highest-high bar | The bar within the forward window that has the highest high |
| FW lowest-low bar | The bar within the forward window that has the lowest low |

Each bar contributes 2 candidate price levels (its high and its low) = 8 static candidates.

---

## CANDIDATE STOP LEVELS

10 stop level functions tested at each bar position:

| Label | Definition |
|-------|------------|
| `pre_signal_low` | Low of the bar before the signal |
| `pre_signal_high` | High of the bar before the signal |
| `signal_low` | Low of the signal bar |
| `signal_high` | High of the signal bar |
| `cur_bar_low` | Low of the current bar (dynamic, changes per position) |
| `cur_bar_high` | High of the current bar |
| `prev_bar_low` | Low of the bar before current (dynamic) |
| `prev_bar_high` | High of the bar before current |
| `min_low_so_far` | Lowest low from signal through current bar |
| `max_high_so_far` | Highest high from signal through current bar |

---

## PART 1: STATIC STOP TEST

For each of the 8 static candidate levels, check every example:

1. During the forward window: does the close ever breach the stop?
2. After the forward window through the exit bar: does the close ever breach the stop?

"Breach" = close below stop for longs, close above stop for shorts.

A candidate passes if 100% of examples survive. Report survival rate, worst margin (closest an example got to being stopped), and rank by aggressiveness.

---

## PART 2: RATCHETING STOP TEST

Test every combination of the 10 stop level functions across (forward_window + 1) bar positions. For a 3-bar forward window, that's 10^4 = 10,000 combinations.

Rules:
- The stop can only move in the favorable direction (up for longs, down for shorts) or stay flat. Any path that moves the stop backwards is rejected.
- At each bar position, the close at that bar must not breach the stop at that position.
- After the forward window ends, the final stop level must hold through the exit bar.

A path passes if 100% of examples survive AND the path is monotonically favorable.

### Scoring metric

The goal is to find the stop path that minimizes the average/median ADR risk during the forward window while keeping all examples alive.

For each surviving path, compute the ADR risk at each bar from signal through end of forward window:

- **Worst-case entry price** = entry bar low + 1 ADR (on examples where entry bar is known). Entry always falls within 1 ADR of the entry bar low.
- **Risk at each bar** = distance from that fixed worst-case entry price down to the stop level at that bar, measured in ADR.
- **Path score** = average and median risk-ADR across all examples and all bars in the forward window.

The path with the lowest average/median risk-ADR wins. This finds the stop that minimizes how much is at risk during the critical entry period.

Risk scoring is computed from signal bar through end of forward window ONLY. Post-forward-window survival is a pass/fail check, not a scoring input.

Note: entry bar data is used for SCORING only. The output stop definition cannot reference the entry bar — see Design Constraints.

---

## PART 3: BREAKEVEN WINDOW

For each example, using the known entry bar:

1. Compute breakeven level = entry candle low + 1 ADR (using ADR at the entry candle, not later bars where a big breakout move might inflate ADR).
2. Walk forward from the signal bar. Find the first bar after which price never revisits the breakeven level again (close never drops below it for longs, never rises above it for shorts).
3. The breakeven window = max of this bar count across all examples.

The breakeven window defines the boundary between the only two classification outcomes. Before it elapses, only the stop matters. After it elapses, you're at breakeven and can't lose.

---

## OUTPUT

**File:** `local_runner/cache/entry_grinder_{setup}.json`

```
{
  "setup_type": "brko",
  "direction": "long",
  "forward_window": 3,
  "n_examples": 46,
  "elapsed_s": 12.3,
  "breakeven_window": 8,

  "static_stops": [
    {
      "label": "signal_low",
      "survival_rate": 1.0,
      "n_survive": 46,
      "n_total": 46,
      "worst_margin_pct": 0.83,
      "avg_risk_adr": 1.2,
      "median_risk_adr": 1.1
    },
    ...
  ],

  "ratchet_stops_top50": [
    {
      "path": ["pre_signal_low", "signal_low", "cur_bar_low", "prev_bar_low"],
      "worst_margin_pct": 0.42,
      "avg_risk_adr": 0.8,
      "median_risk_adr": 0.7,
      "n_survive": 46
    },
    ...
  ],
  "ratchet_stops_total_valid": 342,
  "ratchet_stops_total_tested": 10000
}
```

---

## HOW IT FITS IN THE PIPELINE

The entry grinder runs AFTER the signal grind and exit grind, BEFORE the refinement grind. Its output defines the stop rule that `_gather_raw_signal_clusters()` uses for win/loss classification on breakout/breakdown setups.

```
Signal grind → Exit grind → ENTRY GRINDER → Refinement grind → EV grinder
```

For fade setups (DTSS, 3-4DB, UNR, shoryuken, parabolic), the entry grinder is not needed — the existing forward-window-extreme logic is correct because entry = extreme for fades.

The entry grinder only runs for setup types where the entry mechanic is breakout or breakdown (to be added to the `setups` table).

Currently: standalone script for testing. When validated, the `compute_entry_stop()` function gets called from the pipeline and its output gets read by the classification logic.

---

## CLASSIFICATION RULES (using entry grinder output)

The exit condition is suppressed until after the breakeven window elapses. Before the breakeven window, only the stop matters. This produces a clean binary classification with no ambiguous pile.

- **LOSS** = close breaches the stop path before the breakeven window elapses. Move = flat 1 ADR regardless of actual loss size.
- **WIN** = breakeven window elapses without stop breach. At this point the stop moves to breakeven — worst case is a scratch, you can't lose. Two sub-outcomes:
  - Exit fires above the highest high of the forward window → move measured normally (breakout worked, stock moved up).
  - Exit fires below the forward window high, or never fires → move = 0 ADR. In the win pile for refinement grinder protection, contributes 0 to MFE/EV until profit grinder assigns a real exit.

No ambiguous category. Binary win/loss.

---

## DESIGN CONSTRAINTS

- **100% example survival required.** Any stop definition that stops out even one example is invalid.
- **Entry bar data used for scoring only.** The output stop definition cannot reference the entry bar because it's unknown during classification of non-example signals. Stop rules expressed only in terms of: signal bar, bar before signal, forward window bars.
- **Exit condition suppressed before breakeven window.** The exit condition does not evaluate until after the breakeven window elapses. Before that, only stop breach matters.
- **Self-adjusting.** Rerun when examples change — the stop definition and breakeven window update automatically.
- **No hardcoded prices.** Everything derived from bar relationships, not absolute levels.
- **Runs independently per setup type.** No assumption of symmetry between breakout long and breakdown short stop placements.
- **Direction-aware within a run.** Monotonicity direction, breach direction, and risk measurement adjust for long vs short.
- **Standalone first, pipeline later.** Build and test as `scripts/entry_grinder.py`, graft into pipeline once validated.
