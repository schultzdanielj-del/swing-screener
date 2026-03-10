# Fix: Refinement Grinder (Step 4)

## The Bug

`pyramid_grinder.py --blackout` currently runs the SAME examples-vs-universe grind as step 1, just with post-entry bars masked out. It does NOT grind winners vs losers.

The blackout mode in `_load_blackout_map()` loads profit grinder output and masks bars between entry and exit for each example. The grinder then runs examples vs the full tradable universe (minus masked bars). This is just step 1 with a minor tweak — it finds "what looks like a DTSS" not "what separates winning DTSSs from losing ones."

Result: refinement grind produces ~1,338 signals — basically the same as step 1's 1,218. It adds nothing.

## What PIPELINE_V2.md Says It Should Do

From the pipeline spec (line 75):

```
Step 4: Refinement Grind — (examples + exit-triggered) vs no-exit, blackout. Manual gate.
```

The refinement grind should:

1. **Win pile (what must ALL pass):**
   - All example signal bars (the 66 scan candles from validated examples)
   - All AUTO_WIN signals from step 3 output (exit triggered + move >= ADR threshold)
   - These come from the classified signal set in the v2 cycle on Railway

2. **Lose pile (the universe to grind against):**
   - All AUTO_LOSS signals from step 3 output (exit never triggered OR move < ADR threshold)
   - NOT the full tradable universe — only the classified losers

3. **Blackout masking:**
   - For each example/winner, mask entry-to-exit bars so the grinder can't see the move
   - Conditions must be visible AT THE SIGNAL BAR, not during the trade

4. **Output:**
   - Refinement conditions that APPEND to step 1 conditions (they don't replace)
   - When both step 1 + refinement conditions are applied together, the signal set should be TIGHTER (fewer signals, higher win rate)

5. **Purpose:**
   - Find conditions visible on the signal bar that predict "will this setup actually move?"
   - Losers pass step 1 conditions (they look like the setup) but fail on something the refinement grind discovers
   - This is different from proximity grind (step 6) which targets early/duplicate signals

## How the Pyramid Grinder Works Internally

The pyramid grinder (`run_pyramid()` in `pyramid_grinder.py`) works by:

1. Loading examples and computing min/max ranges per expression across examples
2. Building a "universe matrix" — expression values for every bar in the universe
3. Running beam search per tier (D1, 1wk, 1mo, etc.) to find conditions that minimize peak signals/day
4. Each tier filters further: D1 bars → 1wk surviving → 1mo surviving → etc.

For the refinement grind, the key change is:
- **"Examples"** = win pile (examples + exit-triggered winners)
- **"Universe"** = lose pile (auto-loss signals only)
- The grinder finds conditions where ALL win pile bars pass but many lose pile bars fail

## Data Sources

**Step 3 classified signals are in Railway:**
```
GET /api/v2/cycles/dtss_signal_grind_20260310_043849/signals
```
Each signal has: ticker, signal_date, bar_idx, classification (AUTO_WIN/AUTO_LOSS), classification_source (example/exit_filter), exit_triggered, move_adr, etc.

**Win pile extraction:**
```python
signals = fetch_cycle_signals(cycle_id)
win_pile = [s for s in signals if s["classification"] == "AUTO_WIN"]
# This includes examples (classification_source="example") 
# and exit filter winners (classification_source="exit_filter")
```

**Lose pile extraction:**
```python
lose_pile = [s for s in signals if s["classification"] == "AUTO_LOSS"]
```

**Expression values for each signal bar:**
Each signal has ticker + bar_idx. Load from expr cache:
```python
dates, data = expr_cache.get_ticker(ticker)
row = data[bar_idx, :]  # float32 values for all 15,805 expressions
```

## Implementation Approach

Two options:

### Option A: Modify pyramid_grinder.py blackout mode
Add a `--refinement` flag that changes the example loading and universe construction:
- Instead of loading examples from Railway API, load the win pile from cycle signals
- Instead of scanning the full universe, scan only the lose pile bars
- Keep the beam search, tier cascade, and condition locking logic

### Option B: New dedicated refinement_grinder.py
Build a standalone script that:
- Loads classified signals from Railway cycle
- Splits into win/lose piles
- Builds win matrix (n_winners, n_expressions) and lose matrix (n_losers, n_expressions)
- Runs the beam search (can reuse PeakSpiderweb from pyramid_grinder)
- Outputs conditions that append to step 1

Option B is cleaner — doesn't risk breaking step 1. But more code duplication.

## Critical Requirements

1. **100% win pile pass rate.** Every example and every AUTO_WIN signal must pass all refinement conditions. Non-negotiable.

2. **Conditions APPEND.** The output conditions are additional conditions on top of step 1. When signal_filter re-scans with step 1 + refinement conditions together, the signal set must be a SUBSET of the step 1 signal set. (Fewer signals, never more.)

3. **Same computation path.** Expression cache only. No live compute. Same float32 data path.

4. **Blackout masking.** For win pile bars, mask the forward bars (entry → exit) so the grinder can't learn from the move itself. The conditions must be visible on the signal bar.

5. **Upload to Railway.** Refinement conditions uploaded to the cycle. File mirrored.

6. **Also uploads sacrificial signals** — the leftward duplicate signal bars from examples. These are needed by the proximity grind (step 6) later. For each example, find all signal bars within ±7 calendar days. Rightmost → win pile. All leftward → sacrificial. Upload sacrificial signals to:
   ```
   POST /api/v2/cycles/{cycle_id}/sacrificial_signals
   ```

## Relevant Files to Read Before Coding

- `PIPELINE_V2.md` — authoritative spec (read the refinement grind AND proximity grind sections)
- `local_runner/pyramid_grinder.py` — current implementation, beam search logic, tier cascade
- `scripts/signal_filter.py` — how classified signals are structured and uploaded
- `scripts/proximity_grinder.py` — reads sacrificial signals, shows expected data format
- `local_runner/grind_uploader.py` — Railway upload pattern
- `local_runner/file_mirror.py` — file mirror pattern
- `DATA_CONTRACT.md` — cycle/signal data structure

## What the Refinement Grind Output Looks Like

```json
{
  "setup_type": "dtss",
  "grinder_type": "refinement",
  "n_conditions": 15,
  "all_conditions": [
    {"name": "expr_name", "low": 1.23, "high": 4.56, "tier": "refinement", ...}
  ],
  "win_pile_size": 380,
  "lose_pile_size": 513,
  "lose_pile_trimmed": 287,
  "examples_passing": 66,
  "summary": {
    "final_total": ...,
    "final_peak": ...,
  }
}
```

## Test: How to Verify It's Working

After running the fixed refinement grind:
1. Load step 1 conditions (87) + refinement conditions (N) = combined set
2. Run signal_filter with combined conditions
3. The new signal count MUST be <= 893 (step 1 deduped count)
4. Win rate should be HIGHER than 42.6%
5. All 66 examples must still pass

If the signal count goes UP or examples fail, the implementation is wrong.
