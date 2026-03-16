# ScanPerfect UI Flow — Design Document

**Goal:** One UI that lets you build, refine, and deploy any setup type at maximum speed. Every screen exists to move you forward in the pipeline. Nothing decorative, nothing redundant.

---

## Top-Level Structure

**Navigation:** Setup selector (dropdown) + pipeline progress bar showing which phase you're in for the selected setup. No tabs for Pipeline/Examples/Vetting/Watchlist — those concepts are embedded in the flow itself.

**Layout:** Single-page vertical flow per setup. Each phase is a collapsible section. You work top to bottom. Completed phases collapse to a summary line. The active phase is expanded. Vetting expands to full-screen mode when you enter it.

**Global elements:**
- Setup selector (top-left): switch between setups, create new
- Agent status indicator (top-right): online/offline
- Watchlist link (top-right): always accessible, separate from the per-setup flow

---

## Phase 1 — Setup Creation

**Purpose:** Define the setup and build the initial example library.

**What you see:**
- Setup name + description (editable)
- Example count with target indicator (e.g. "26 / 50 target")
- Example list: ticker, entry date, move ADR (sortable)
- "Add Example" input: ticker + date fields, shows chart preview on entry, confirm button
- Bulk import option (paste CSV of ticker,date pairs)

**Actions:**
- Add individual examples (type ticker, type date, see chart, confirm)
- Bulk import examples
- Delete examples
- View any example's chart by clicking it

**When done:** Example count is sufficient (20+ minimum). Click "Run Signal Grind" which lives at the bottom of this section and is the entry point to Phase 2.

**Questions:**
- Q1: Do you ever need to come back here and add examples outside of the vetting loop? Or is vetting the only way examples get added after initial creation?
- Q2: Should the chart preview when adding an example show the same candlestick chart as the vetting UI? Or is a simpler view fine?
- Q3: Bulk import — is CSV paste enough, or do you need file upload?

---

## Phase 2 — Signal Grind

**Purpose:** Find mathematical conditions separating examples from universe.

**What you see (before run):**
- Parameters: beam width (default 10000), depth cap (default 100), peak target (default 3/day)
- Example count at grind time
- Previous run result summary (if exists): condition count, signal count, peak/day, duration

**What you see (during run):**
- Live log stream from agent
- Progress indicator

**What you see (after run):**
- Result summary: N conditions found, N signals across 5yr, peak X/day, avg Y/day
- Condition table: tier, expression, low/high bounds, filter power (collapsible)
- "Run Exit Grind →" button at bottom

**Actions:**
- Adjust parameters (beam, depth, peak target)
- Run / re-run
- View conditions
- Proceed to Exit Grind

**Questions:**
- Q4: Do you ever need to manually add/remove/edit individual conditions? Or is the grinder output always taken as-is?
- Q5: The margin progression output (TODO #2) — when that's built, should there be a slider here to adjust bounding box margins post-hoc?

---

## Phase 3 — Exit Grind

**Purpose:** Find optimal exit expression.

**What you see (before run):**
- Parameters: max forward bars (default 120)
- Previous result (if exists): expression name, direction, threshold

**What you see (after run):**
- Result: expression name, direction (<=/>= threshold), threshold value
- "Run Refinement Grind →" button at bottom

**Actions:**
- Adjust max forward bars
- Run / re-run
- Proceed to Refinement

**Questions:**
- Q6: Is there ever a reason to manually override the exit expression? Or always trust the grinder?

---

## Phase 4 — Refinement Grind

**Purpose:** Scan universe with conditions, classify winners/losers, eliminate losers.

**What you see (before run):**
- Parameters: depth cap (default 100)
- The depth progression slider (TODO #1) — when built, allows post-hoc tuning of how many refinement conditions to keep

**What you see (after run):**
- Result summary: total clusters, winners, losers, eliminated, surviving losers
- Win rate: X% (winners / (winners + surviving losers))
- Winner move stats: median, mean, floor, ceiling (in ADR)
- Condition count: N signal + N refinement = N combined
- Depth progression chart (when built): X-axis = refinement depth, Y-axis = loser elimination %, with a draggable threshold line
- "Enter Vetting →" button at bottom

**Actions:**
- Adjust depth cap
- Run / re-run
- Tune depth threshold via slider (future)
- Proceed to Vetting

**Questions:**
- Q7: After refinement, do you always vet? Or do you sometimes skip straight to EV Grinder?
- Q8: The depth progression slider — should adjusting it immediately recompute the signal set (filtering to only conditions at or above that depth), or just show you what the numbers would be?

---

## Phase 5 — Vetting

**Purpose:** Review winner signals, bank new examples, loop back to Signal Grind when ready.

**This is the factory line. Speed is everything.**

**Layout:** Full-screen takeover when entered. Three zones:
- Left panel: signal list (scrollable, keyboard navigable)
- Center: chart (large, dominant)
- Bottom bar: signal metadata + action buttons

**Signal list:**
- Sorted by combined_score (entry candle scorer) in post-refinement mode, or by move_adr in signal grind mode
- Each row: ticker, date, score/move, verdict indicator
- Filter toggles: Unvetted / All / Yes / No
- Source toggle: Signal Grind / Refinement (determines which signal set to show)
- Count: N remaining, session stats (Y yes, N no)

**Chart:**
- Full candlestick with EMAs/SMAs
- Signal bar highlighted
- Exit bar highlighted
- Earnings dates marked
- Click to set entry bar (for YES picks)

**Bottom bar:**
- Ticker, signal date, move ADR, MFE, capture efficiency, exit date, entry date (click to set)
- Action buttons: ✓ Yes (1) | — Skip (3) | ✗ No (2)
- Keyboard driven: 1/2/3 for verdict, ↑↓ for navigation

**Flow:**
1. Signal auto-selected (first unvetted)
2. Chart loads
3. You look at it, hit 1 (yes), 2 (no), or 3 (skip)
4. If yes: click chart to set entry bar (or accept signal date as default), then confirm
5. Next signal auto-loads
6. YES picks go to pending_examples → AI second-pass → approve on Examples section
7. When you've banked enough, exit vetting, loop back to Signal Grind

**"Update Scores" button:** Runs entry candle scorer, refreshes the list sorted by combined_score. Use after adding new examples to get better scoring.

**Questions:**
- Q9: The AI second-pass — is this still the flow you want? (YES → pending → AI review → approve/reject → example). Or should YES just immediately become an example?
- Q10: Should there be a "quick add" mode where YES = immediately add as example, skipping AI review? For speed when you're confident.
- Q11: When you exit vetting, should the UI prompt "You've added N new examples. Run Signal Grind to incorporate them?" as a nudge to loop?
- Q12: The entry candle scorer "Update Scores" — should this auto-run when you enter vetting, or manual trigger only?

---

## Phase 6 — EV Grinder

**Purpose:** Score every signal with predicted WR, MFE, EV. Produce the scoring equation for live use.

**What you see (before run):**
- Parameters: (currently none user-adjustable — all automated)
- Previous result summary (if exists)

**What you see (after run):**
- Feature summary: N market features, N setup features surviving dedup
- Calibration table: decile buckets with predicted vs actual WR (pre and post refinement)
- RMSE
- Signal distribution: quality_score histogram
- **Slider 1 (quality_score threshold):** drag to set minimum quality. Shows: N signals above threshold, actual WR of those signals, actual median move
- **Slider 2 (future — minimum predicted WR):** additional filter
- Top signals table: ranked by EV, showing ticker, date, quality_score, predicted WR, predicted MFE, EV
- "Run Profit Grind →" button at bottom

**Actions:**
- Run / re-run
- Adjust Slider 1 to see how different quality thresholds affect the signal pool
- Browse top signals
- Proceed to Profit Grind

**Questions:**
- Q13: Slider 1 and Slider 2 — are these set here and then passed to the Profit Grind? i.e. the Profit Grind optimizes exit strategy only for signals above the slider thresholds?
- Q14: Or does the Profit Grind test across multiple slider settings itself, finding the optimal threshold + exit combo?
- Q15: Should the EV Grinder results show any per-year breakdown? (e.g. "2021: 45 signals, 82% WR" vs "2024: 38 signals, 76% WR")

---

## Phase 7 — Profit Grind

**Purpose:** Optimize exit strategy for maximum account growth (SQN), using real entry candles and the EV-scored signal set.

**What you see (before run):**
- Parameters to be determined — at minimum:
  - Slider 1/2 range to test (or fixed values from Phase 6)
  - Stop loss range (in ADR units)
  - Target range (in ADR units)
  - Trail stop options
  - Position sizing model
- Which signals to use: all above slider threshold, or a specific subset

**What you see (after run):**
- Optimal parameters: stop, target, trail settings at each slider level
- SQN score
- Compounded equity curve
- Drawdown profile
- Per-trade stats: avg win, avg loss, win rate, expectancy
- MFE capture efficiency
- Comparison table: different parameter combos ranked by SQN

**Actions:**
- Adjust parameter ranges
- Run / re-run
- Select optimal parameter set
- "Go Live →" button at bottom

**Questions:**
- Q16: Does the Profit Grind use actual entry candle prices (from examples + vetting YES picks), or does it still use the conservative forward-window-max-high for non-examples?
- Q17: SQN optimization — is this Van Tharp's SQN (sqrt(N) × expectancy / stdev)? Or a modified version?
- Q18: Position sizing — fixed % risk per trade, or does the grinder also optimize position sizing?
- Q19: Should the Profit Grind test trim-and-trail strategies (e.g. sell half at 3 ADR, trail rest)? Or just single-exit strategies first?
- Q20: The Profit Grind needs the full OHLCV data after entry to simulate exits. Does it use the 5yr cache for this?

---

## Phase 8 — Live Watchlist

**Purpose:** Nightly ranked signal list across all setup types.

**Layout:** Not per-setup — this is the cross-setup view. Separate from the pipeline flow.

**What you see:**
- Today's date, market status
- Ranked signal list: setup type, ticker, signal date, quality_score, predicted WR, predicted MFE, EV, suggested stop/target from profit grind
- Filters: by setup type, minimum EV, minimum WR
- Historical performance summary: last 30 days, last 90 days, all-time
- Position tracker (future): what you're currently in, P&L

**Actions:**
- View any signal's chart
- Filter/sort the list
- Mark signals as "taken" (future)

**Questions:**
- Q21: Do you want the watchlist to show signals from today only, or also recent signals still in their trade window?
- Q22: Should there be a "paper trade" mode that tracks what would have happened if you took the top N signals each day?
- Q23: Multiple setup types on one watchlist — should they be interleaved by EV rank, or grouped by setup type?

---

## Cross-Cutting UI Questions

- Q24: The loop from Vetting back to Signal Grind — should the UI make this explicit (a "Loop Back" button that takes you to Phase 2 with the new examples counted), or just let you scroll up?
- Q25: Should completed phases auto-collapse, or stay expanded?
- Q26: When switching setups via the dropdown, should the UI remember where you were in each setup's pipeline?
- Q27: Mobile — do you ever use this on a phone/tablet, or desktop only?
- Q28: Dark mode only (per the design spec), confirmed?

---

## Implementation Priority

Once we agree on the flow, build order should be:

1. **Skeleton:** Top bar + collapsible phase sections + setup selector (no functionality, just layout)
2. **Phase 1:** Setup creation + example management (functional)
3. **Phase 5:** Vetting UI (functional — this is where you spend the most time)
4. **Phase 2-4:** Signal/Exit/Refinement grind panels (run button + log + results display)
5. **Phase 6:** EV Grinder panel + sliders
6. **Phase 8:** Watchlist (cross-setup view)
7. **Phase 7:** Profit Grind (when the engine is built)
