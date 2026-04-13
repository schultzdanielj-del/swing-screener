# Signal Filter — Classification Spec

> **Status:** Classification logic implemented in `_gather_raw_signal_clusters()` in `local_runner/pyramid_grinder.py` (2026-04-13). Pending validation on DTSS + BRKO.
> `scripts/signal_filter.py` exists for standalone use but has not been updated yet.
> See `DEPENDENCY_MAP.md` for full I/O and `PIPELINE_V2.md` for pipeline position.

---

## Purpose

Classify every historical signal as winner, loser, or ambiguous. This is a logic filter, not a grinder — it applies a classification rule to all deduplicated signals (rightmost bar per cluster) found by the signal grinder.

This is the ONLY component that separates winner/loser piles. Everything downstream (EV grinder, management grinder) depends on this classification being correct.

This is NOT a trade simulator. It doesn't try to perfectly replicate live trade management. It classifies each signal: clear win, clear loss, or ambiguous. What the trader does live (re-enter after a chop, tighten stops, skip signals) is outside scope.

---

## Constraints

- **Signal-relative only.** No reference to entry bar. Entry bar identification requires specific TA that this component does not have and should not try to replicate. Classification must work using only the signal bar and bars after it.
- **120-bar window.** Maximum trade duration. If nothing resolves within 120 bars, done.
- **100% example survival.** Every example in the examples table must classify as a winner. If any example fails, the classification logic is wrong — not the example.
- **Setup-agnostic.** Same classification framework for all setup types (DTSS, BRKO, etc.). Direction (long/short) mirrors the thresholds.
- **Per deduplicated signal.** Classification runs on the rightmost bar of each cluster after deduplication, not on every raw signal bar.

---

## Classification Approach: ADR Thresholds

Two boundaries derived from the example set, both measured in ADR relative to signal bar close. Computed once per setup, applied to every signal.

### Winner threshold

Determines the minimum exit price that guarantees profit regardless of actual entry price.

**Longs:** From each example, measure (entry candle low − signal close) in ADR. Take the max across all examples. Add 1 ADR (practical worst-case fill — it's very rare to enter a candle at a price greater than 1 ADR from the low of that candle). Add 0.1 ADR cushion.

**Winner threshold = max(entry candle low − signal close) in ADR + 1.0 + 0.1**

**Shorts:** Mirror — use entry candle high. Measure (signal close − entry candle high) in ADR.

**Rule:** Exit above this level (longs) or below it (shorts) = definite winner.

### Loser threshold

Determines the maximum adverse excursion (MAE) any confirmed winner experienced during the setup phase.

**Longs:** From each example, find the lowest bar LOW in the **forward window** (the short setup window — e.g., 3 bars for BRKO, NOT 120 bars). Measure (signal close − that lowest low) in ADR. Take the max across all examples. Multiply by 1.10 (10% cushion).

**Loser threshold = max(signal close − lowest FW low) in ADR × 1.10**

**Shorts:** Mirror — find the highest bar HIGH in the forward window. Measure (highest high − signal close) in ADR.

The forward window is only used to **find the distance**. The hard stop derived from it is active immediately from signal+1 for the life of the trade.

**Rule:** Bar low (longs) or bar high (shorts) breaches this level = definite loser. Hard stop mechanics — a resting order fills on any touch. No example winner ever went that far during the setup phase.

### Why bar lows/highs for loser, close for winner

The loser threshold is a hard stop. A resting stop order fills on any touch — use bar lows (longs) or highs (shorts).

The winner threshold uses the exit condition fire price (expression-based, evaluated at close). Exit signals are discretionary decisions evaluated end of day, not resting orders.

---

## Breakeven Bar

For each example, find the last bar where the low (longs) or high (shorts) crosses the winner level, before the exit condition fires. Take the max across all examples.

After this many bars, if the signal hasn't been stopped out, a breakeven stop would hold — worst case is a scratch.

Used to resolve ambiguous scenarios: signals that survive past the breakeven bar are at worst a scratch.

---

## Four Classification Scenarios

Within the 120-bar window. The loser threshold check runs from signal+1 onward. The exit condition evaluates after the forward window only.

**It's a race: which happens first — hard stop hit or exit fires?**

| # | Scenario | Label | Reason |
|---|----------|-------|--------|
| 1 | Exit fires (post-FW), price clears winner threshold | `AUTO_WIN` | `clear_winner` |
| 2 | Bar low/high breaches loser threshold before exit fires | `AUTO_LOSS` | `mae_breach` |
| 3 | Exit fires (post-FW), price between thresholds | `AMBIGUOUS` | `exit_in_zone` |
| 4 | Neither exit fires nor loser threshold breached within 120 bars | `AMBIGUOUS` | `timeout` |

Examples are forced to `AUTO_WIN` regardless of race outcome.

### MAE check during the forward window

The loser threshold check starts at signal+1, NOT after the forward window. This is safe because:

- If price breaches the MAE threshold during the FW, a hard stop would have been hit. That signal is a clear loser.
- If price then recovers and rips, a new signal fires on the recovery. That later signal is the rightmost in the cluster. Deduplication keeps rightmost — we never analyze the earlier chopped-out signal.
- If price breaches and never recovers, it's genuinely a loser.

Both outcomes are correct. Rightmost deduplication naturally handles the "chop then rip" edge case.

### Ambiguous zone

The zone between thresholds (scenarios 3 and 4) shrinks as the example set clusters more tightly — similar entry candle positions, similar drawdowns = tighter thresholds = more signals cleanly classified.

Currently labelled `AMBIGUOUS` for measurement. The % of signals falling here determines whether the ambiguous category needs further handling or is negligible.

---

## Implementation

Classification is implemented in `_gather_raw_signal_clusters()` in `local_runner/pyramid_grinder.py`. The thresholds are computed from matched examples after Pass 1 determines the forward window. The classification loop applies them per cluster.

`scripts/signal_filter.py` has its own classification logic (median-split) that has NOT been updated yet. It is used for standalone analysis/vetting, not in the consensus pipeline.

---

## Open Questions

- **Ambiguous zone size.** What % of deduplicated signals fall into AMBIGUOUS? Need to measure on real data (DTSS + BRKO) before deciding how to handle them downstream.
- **Ambiguous handling downstream.** Once measured: exclude from refinement grinder? Separate pile? Conservative AUTO_LOSS? Depends on the numbers.
- **Entry candle boundary box.** Could pre-filter signals where no candle in the forward window matches the characteristic range of example entry candles. Discussed, not finalized.
- **signal_filter.py sync.** The standalone script needs its classification logic updated to match the pipeline implementation.
