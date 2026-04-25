# Signal Filter — Classification Spec

> **Status (2026-04-15):** Classification lives in `_gather_raw_signal_clusters()` in `local_runner/pyramid_grinder.py`. Two paths (fade, breakout). Matching is strict entry-1 + dedup (ported from `scripts/signal_filter.py`). Fade stop is intraday-touch. Regression guard: `tests/test_signal_filter_contract.py` (run before any change in this area).
> `scripts/signal_filter.py` retained as reference implementation; the canonical matching logic lives there. Not invoked by the pipeline.
> See `DEPENDENCY_MAP.md` for full I/O and `PIPELINE_V2.md` for pipeline position.

---

## Purpose

Classify every historical signal as winner or loser. This is a logic filter, not a grinder — it applies a classification rule to all deduplicated signals (rightmost bar per cluster) found by the signal grinder.

This is the ONLY component that separates winner/loser piles. Everything downstream (EV grinder, management grinder) depends on this classification being correct.

This is NOT a trade simulator. It doesn't try to perfectly replicate live trade management. It classifies each signal: win or loss. Scratches count as wins. Losses normalize to 1 ADR downstream for EV/profit factor math.

---

## Constraints

- **Signal-relative only.** No reference to entry bar for non-examples. Classification must work using only the signal bar and bars after it.
- **120-bar window.** Maximum trade duration. If nothing resolves within 120 bars, the breakout path calls it a win (held); the fade path also calls it a win (held_to_end).
- **100% example survival.** Every example in the examples table must classify as AUTO_WIN. If any example fails the race, it's forced to AUTO_WIN anyway (example override).
- **Direction-aware.** Longs and shorts mirror their thresholds and breach checks.
- **Per deduplicated signal.** Classification runs on the rightmost bar of each cluster, not on every raw signal bar.
- **Strict example matching.** Every example's signal bar is exactly `entry_idx - 1`. No proximity matching, no fuzzy windows. The clustering logic splits clusters at example signal bars so entry-1 is always the rightmost of its own cluster. Examples where conditions don't fire on entry-1 are logged as `GRINDER BUG`, not silently matched to a nearby bar.
- **Binary output.** Every cluster is AUTO_WIN or AUTO_LOSS. No AMBIGUOUS category.

---

## Two classification paths

Setup class determines which path a signal goes through. `FADE_SETUPS = {"dtss", "3-4db"}` (hardcoded in pyramid_grinder.py); everything else is treated as breakout.

| Setup class | Stop mechanic | Classification outcomes |
|-------------|---------------|-------------------------|
| **Fade** (DTSS, 3-4DB) | Ceiling = max high of cluster bars + forward window | ceiling_breach (LOSS), exit_fired (WIN), held_to_end (WIN) |
| **Breakout** (BRKO) | ADR-derived loser level from signal close, hard stop on bar lows/highs | mae_breach (LOSS), clear_winner (WIN), exit_in_zone (WIN), timeout (WIN) |

Both paths: examples force to AUTO_WIN, tie goes to stop on same-bar breach+exit.

---

## Path 1 — Fade (ceiling + exit race)

Used for setups where the signal IS the extreme of the move being faded (e.g., DTSS shorts a big up-move; the highest high IS the level that can't be exceeded).

### Ceiling

The ceiling is the natural stop — if price exceeds it, the fade has failed.

**Shorts:** ceiling = max(highest high across cluster bars, highest high in forward window after rightmost bar)
**Longs:** ceiling = min(lowest low across cluster bars, lowest low in forward window after rightmost bar) — applied as a floor

### Race

Starting from `rightmost_bar + forward_window + 1`, race two conditions:

1. **Bar breaches ceiling intraday** (bar high > ceiling for shorts, bar low < ceiling for longs) → `AUTO_LOSS` reason `ceiling_breach`. Resting stop order fills on any touch — use bar highs/lows, not close.
2. **Exit condition fires** → `AUTO_WIN` reason `exit_fired`
3. **Neither fires before end of data** → `AUTO_WIN` reason `held_to_end`

Tie-break: if ceiling breach and exit fire on the same bar, ceiling breach wins (the fade has failed regardless of the exit signal).

### Why no ADR threshold for fades

For fades, the entry is far from signal close (the big up-move). ADR-based thresholds measured from signal close include the setup's inherent spike as "adverse excursion," producing 7+ ADR loser thresholds that are physically unreachable. The ceiling approach anchors the stop to the actual fade failure level, not to a signal_close reference that's nowhere near the trade's entry point.

---

## Path 2 — Breakout (ADR thresholds)

Used for setups where the signal is a confirmation of a breakout and entry is near signal close.

### Winner threshold (for determining clear wins)

Determines the minimum exit close that unambiguously clears fill slippage + profit.

**Longs:** measure (entry candle low − signal close) in ADR per example. Take the max. Add 1.0 ADR (worst-case fill buffer) + 0.1 ADR cushion.

**Winner threshold = max(entry candle low − signal close) in ADR + 1.0 + 0.1**

**Shorts:** mirror — use entry candle high. `max(signal close − entry candle high) in ADR + 1.0 + 0.1`.

### Loser threshold (hard stop)

Maximum adverse excursion any example showed during the forward window, +10% cushion.

**Longs:** per example, find the lowest bar LOW in the forward window. Measure (signal close − lowest low) in ADR. Take max across examples. × 1.10.

**Loser threshold = max(signal close − lowest FW low) in ADR × 1.10**

**Shorts:** mirror — highest high in the forward window.

Hard stop active from signal+1 for the life of the trade. Uses bar lows (longs) or highs (shorts) — resting stop order fills on any touch.

### Race

Within 120-bar window, race two events:

1. **Bar low/high breaches loser level** (before exit fires or concurrent) → `AUTO_LOSS` reason `mae_breach`
2. **Exit condition fires after forward window**:
   - Exit close clears winner threshold → `AUTO_WIN` reason `clear_winner`
   - Exit close in zone (between loser and winner levels) → `AUTO_WIN` reason `exit_in_zone` (scratch)
3. **Neither breaches nor exit fires within 120 bars** → `AUTO_WIN` reason `timeout` (held)

Tie-break: if stop and exit fire on the same bar, stop wins (intrabar low triggers before close-based exit evaluation).

### Breakeven bar (informational)

Last bar in the example set where price is on the wrong side of the winner level before exit fires. Persisted to output JSON as `breakeven_bars` (breakout only, null for fade). Not currently used in classification — available for future breakeven-stop logic.

---

## Why bar lows/highs for loser, close for winner

The loser threshold is a hard stop. A resting stop order fills on any touch — use bar lows (longs) or highs (shorts).

The winner threshold uses the exit condition fire price (expression-based, evaluated at close). Exit signals are discretionary decisions evaluated end of day, not resting orders.

---

## Scan behavior (context for classification)

The scan inside `_gather_raw_signal_clusters()` reads conditions directly from `.npz` expression cache files (not `.im`). Per-bar tradable filter (`compute_tradable_masks()`) and 50-bar warmup mask are applied to all non-example tickers. Example tickers are **exempted** from both filters — their signal bars must survive to guarantee example coverage, even for IPO setups (CRCL, CRWV) or high-volume low-ADRP large caps (MSFT, TJX).

OHLCV data is truncated to the expression cache window (`EXPR_CACHE_START`) before classification. All bar indices are expression-cache-relative.

---

## move_adr computation

Per cluster with an `exit_bar`:
- **entry_high**: for examples, the high of the actual entry candle; for non-examples, max high (longs — or min low for shorts) in the forward window after rightmost bar (worst-fill assumption)
- **move_adr = (entry_high − exit_close) / ADR** for shorts, reversed for longs
- ADR sourced from `adr14` column in expression cache, fallback to 14-bar manual computation
- All ADR values — example threshold derivation, per-cluster classification, and move_adr — use the same path: cached `adr14` first, fallback to 14-bar SMA of (high − low).

Used downstream for EV math and filtering, not for classification itself.

---

## Implementation notes

- Two classification paths in `_gather_raw_signal_clusters()` branched by `setup_class`. Both share the same pass-1 forward window derivation and example matching.
- `scripts/signal_filter.py` has its own median-split logic that has NOT been updated to match. Used for standalone analysis only, not in the consensus pipeline.

---

## Current state (2026-04-15)

- **Strict entry-1 matching** ported from `scripts/signal_filter.py` (commit 61e4129 logic). Build `example_signal_set = {(ticker, entry_idx-1): ...}`, split clusters at example signal bars, tag `is_example` via exact set lookup. GRINDER BUG logged if conditions don't fire on entry-1.
- **Fade stop is intraday-touch**, not close-based. Bar high > ceiling (shorts) triggers stop.
- **ADR computation unified** (2026-04-15): examples, per-cluster classification, and move_adr all use cached `adr14` first, manual 14-bar SMA fallback.
- **Forward window derivation fixed** (2026-04-15): `forward_window = max(example_fwd_windows)`, no ×1.1 multiplier. The ×1.10 cushion applies only to loser_threshold_adr (MAE buffer), not forward window.
- **Aggregate thresholds persisted to JSON** (2026-04-15): `winner_threshold_adr`, `loser_threshold_adr`, `breakeven_bars`, `setup_class`, `n_examples_validated`, `grinder_bugs` list now in output.
- **BRKO baseline**: 48.9% win rate, 5,518 clusters, 58/58 examples strict, loser_threshold_adr = 0.60, winner_threshold_adr = 2.26, forward_window = 3.
- **DTSS baseline**: 71.1% win rate, 1,122 clusters, 73/73 examples strict, forward_window = 3.
- Latest BRKO pyramid: `pyramid_brko_mp_sig7966_pk39_20260414_140016.json` (207 conditions).
- Regression test: `tests/test_signal_filter_contract.py` (4 tests, 3 passing + 1 informational).

## Open issues

- **DTSS classification still too lenient.** 71.1% above Dan's expected 40-60%. The ceiling is `max(cluster_highs ∪ FW_highs)` — FW bars inflate the ceiling AND are exempt from the breach race. Likely next move: narrow ceiling to signal bar high only, keeping race start at FW+1.
- **BRKO stop logic under review.** Current universal 0.60 ADR loser threshold (driven by one example — PTON) may be replaced by per-signal stop: floor = min(low in FW bars), activated post-FW. This would mirror the fade ceiling's per-signal derivation.
- **BRKO winner move_adr floor is −4.83** — measurement artifact from `entry_high = max high in FW` for non-examples. move_adr uses entry_high as reference; classification uses signal_close. The two can diverge. Anchoring move_adr to signal_close or entry_bar_open is under consideration.
- **Winner threshold +1 ADR buffer** is structural: represents fill-to-stop distance under 1-ADR-loss normalization. Not a padding to be tuned.
- **Loser threshold stall for high-ADR stocks**: 0.60 ADR on RGTU ($22.67 ADR) = ~$14 stop. Per-signal FW-low stop would resolve this.
- **Tradable filter exemption for examples** may distort scan statistics.
- **breach_bar semantics differ by path.** Fade: minimum = FW+1 (race starts post-FW). Breakout: minimum = 1 (stop from signal+1). Same field name.
