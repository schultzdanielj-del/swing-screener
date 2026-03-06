# BUGS.md — Known Issues

This file documents confirmed bugs and structural problems in the project.
Each entry includes what was observed, what the root cause is, and what needs to be fixed.
Nothing in here gets touched until explicitly approved.

---

## Project Goal

**A nightly automated watchlist of signals for setups, dynamically rank ordered by EV.**

The watchlist shows signal bars for potential setups and ranks them by how likely they
are to produce an entry candle very soon (ideally 1 day away) for a trade that will run
big — where "big" means running as much as the setup examples did on average/median.

Ranking is driven by historical EV:
- **Win rate** = ratio of signals that produced a winning entry bar like the examples,
  calculated per market condition bucket via the market grinder
- **Exit capture** = median MFE capture on exits from the exit grinder
- **EV** = win rate × median captured move

Everything in the system exists to serve this goal. Any fix, change, or addition must
make the nightly watchlist more accurate, more reliable, or higher EV. If it doesn't
serve that goal directly, it doesn't get built.

---

## BUG-001: D1 Tier Over-Locking Destroys Downstream Tiers

**Status:** Confirmed
**Severity:** Critical — makes grinder results unreliable as example count grows
**Discovered:** 2026-03-06 during audit

### What Was Observed

Grind #4 (62 examples) produced ~168 raw signals.
Grind #5 (68 examples, same params) produced 1,691 raw signals — a 10x increase.

The 1wk tier in grind #5 had only **166 surviving rows** across 6 dates.
The same tier in earlier grinds had **1,364 surviving rows**.

The 1mo tier hit a ceiling at 27/day and could not get below it.
Every tier from 1mo onward was working with a decimated universe and could not compensate.

### Root Cause

D1 locks conditions greedily with no awareness of how many historical rows will survive.

With 68 examples, D1 found 29 conditions (vs 7 with 23 examples). Those 29 conditions
filtered the 1wk matrix from ~1,364 rows down to 166 rows — 8x fewer rows.

With only 166 rows across 6 dates, the beam search at 1wk has almost nothing to work
with. It hits a ceiling fast. That ceiling propagates through every subsequent tier.
The grinder produces more conditions but far weaker filtering — hence 10x more signals
despite more examples.

### Why This Violates the Project Goal

The nightly watchlist needs 2-7 high-quality signals per day — consistent and reliable.
A grinder that produces 10x more signals when 6 more examples are added is not fit for
purpose. The signal count must be stable and predictable as the example library grows.

### Why This Is a Structural Problem

D1 and the historical tiers are coupled but D1 has no awareness of this.
D1 grinds to its ceiling and locks whatever it finds.
There is no constraint preventing D1 from locking so hard it destroys downstream tiers.
The problem silently gets worse as the example count grows.

### What Needs to Be Fixed

D1 must be constrained so that the conditions it locks cannot reduce the 1wk surviving
row count below a meaningful minimum threshold. Options:

1. **Hard row floor constraint on D1:** After each D1 condition is locked, check how many
   rows survive in the 1wk window. Stop locking D1 conditions if 1wk rows would drop
   below e.g. 500 rows.

2. **Remove D1 as a separate locking tier:** Let D1 conditions compete in the historical
   tiers instead of being locked first. D1's job becomes seeding candidates, not locking.

3. **Cap D1 condition count:** Hard limit on how many conditions D1 can lock (e.g. max 5-7),
   regardless of how many it finds.

Option 1 is the most principled fix — it directly addresses the coupling problem.

### What Must NOT Be Done

Do not touch the historical tier logic.
Do not change beam/depth params as a workaround.
Do not change the example range computation as a workaround for this specific bug.

---
