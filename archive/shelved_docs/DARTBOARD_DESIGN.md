# The Dartboard Grinder — Design Document

## The Problem (in one sentence)

The current grinder only knows how wide the dartboard is. It needs to know
where the bullseye is.

## What Changes

**Before (bounding box):**
Each filter → is this value inside the min/max range? → yes/no
Stack 87 yes/no answers → all yes = signal, any no = rejected

**After (dartboard):**
Each filter → how close is this value to where examples cluster? → score 0.0 to 1.0
Combine all scores → composite score → rank everything → top of the list = best setups

There is no "pass/fail." There is no beam search. There is no tier cascade.
Every expression contributes evidence. The output is a ranked list, not a
binary gate.

## How It Works — Step by Step

### Step 1: Build the Example Profile

For each of the ~15,805 expressions, look at the values across all 69 examples.
Compute two numbers:
- **center**: where do examples cluster (the mean)
- **spread**: how tightly do they cluster (the standard deviation)

Example: for "ext_avgc150_adr14" (extension from 150 SMA):
- 69 example values might cluster around 5.2 with spread 1.8
- That's the bullseye: 5.2 is dead center, 1.8 tells you how big the rings are

This replaces the current (min, max) with (center, spread). Same data,
way more information extracted.

### Step 2: Weight the Expressions

Not all expressions are equally useful. Some separate examples from the
universe clearly. Others look the same for examples and for random tickers.

For each expression, compute a **discriminating power** score:
- How far apart are the example center and the universe center?
- Relative to their combined spreads?

Example:
- Expression A: examples cluster at 5.2, universe clusters at 5.0 → barely different → low weight
- Expression B: examples cluster at 5.2, universe clusters at 12.0 → very different → high weight

Expressions with near-zero discriminating power get dropped entirely (maybe
keep top 500-1000). This is automatic — no manual selection, no beam search.

### Step 3: Score Everything

For any ticker-day (historical or tonight's live bars), compute:

For each weighted expression:
1. How far is this bar's value from the example center?
2. Scale by the spread (a value 1 spread away is less suspicious than 3 spreads away)
3. Convert to a 0-1 score: dead center = 1.0, far away = ~0.0

Combine all expression scores into one composite number. This is the
"how much does this bar look like a DTSS" score.

### Step 4: Rank and Threshold

Every bar in 5 years of history gets a score. Sort by score, highest first.
Your examples should be near the top (they define the bullseye, so they
score high by construction).

The "signal count" becomes a threshold choice:
- Score > 0.9: only things that look almost exactly like the example pile → tight, few signals
- Score > 0.7: things that look pretty similar → more signals, more coverage
- Score > 0.5: anything that vaguely resembles the pattern → loose

You can tune this threshold to get your target 2-7 signals/day, and it
will be STABLE — adding a new example shifts the bullseye slightly but
doesn't send the whole thing in a random direction.

## Why More Examples Now HELPS

Current system: every new example can only push the walls outward. The box
gets bigger. Depth is ignored.

Dartboard system: every new example sharpens the bullseye. The center
becomes more precise, the spread estimate becomes more reliable. An
outlier example that sits far from the cluster barely moves the center
(mean is robust to one oddball). In the current system that same outlier
stretches the box edge dramatically.

**20 examples:** rough bullseye, blurry rings (uncertain spread estimate)
**69 examples:** precise bullseye, sharp rings (confident spread estimate)
**150 examples:** extremely precise bullseye, very confident scoring

The system gets better with every example instead of worse.

## Why Removing Outliers Becomes Predictable

In the current system, removing an outlier reshuffles the search space
and the beam search wanders to a random new answer.

In the dartboard system, removing an outlier barely moves the center
(the other 68 examples still define it) and might tighten the spread
slightly. The result changes by a small, predictable amount. No
random walk. No surprises.

## What Happens to the Beam Search and Tiers

They're gone. Not needed.

The beam search existed to find the best COMBINATION of binary filters.
That's a hard combinatorial problem that gets harder with more candidates.

The dartboard scores on ALL expressions simultaneously. No combinations
to search. The weighting step (step 2) automatically figures out which
expressions matter. The compute is straight-line arithmetic — no
branching, no search tree, no beam width, no depth parameter.

The tier system (D1 → 1wk → 1mo → 6mo → 1yr → 5yr) existed because
the beam search needed smaller matrices to be tractable. The dartboard
processes the full 5yr history in one pass because there's no search
— just a vectorized score computation per ticker.

## Computational Cost

Current grinder: ~10-20 minutes (beam search, tier cascade, parallel workers)

Dartboard grinder estimate:
- Step 1 (profile): seconds — just mean/std across 69 values per expression
- Step 2 (weights): seconds — compare example stats to universe stats
- Step 3 (scoring full 5yr): ~30-60 minutes first time
  - 4,167 tickers × 1,260 bars × 500-1000 weighted expressions
  - Pure numpy vectorized ops, ~1 second per ticker
  - Parallelizable across cores
  - Can be cached and only recomputed when examples change
- Step 4 (rank/threshold): seconds

Comparable to current grinder. No harder to run.

## What Stays the Same

- Expression library (15,805 expressions) — untouched
- Expression cache (~21 GB precomputed) — this IS the dartboard, we just score it differently
- Example library — more valuable than before
- Railway infrastructure — scores upload the same way
- Exit grinder, regime model, profit grinder — all downstream steps unchanged
- Nightly refresh pipeline — unchanged
- 100% example pass rate — examples by definition score high (they ARE the distribution)

## What Changes

| Component | Before | After |
|-----------|--------|-------|
| Scoring | Binary pass/fail per condition | Continuous 0-1 score per expression |
| Condition selection | Beam search (combinatorial) | Automatic weighting (arithmetic) |
| Tier cascade | D1 → 1wk → 1mo → 6mo → 1yr → 5yr | Single pass over full history |
| Output | Signal list (pass/fail) | Ranked list with scores |
| Sensitivity to examples | More = worse (wider boxes) | More = better (sharper bullseye) |
| Sensitivity to perturbation | Chaotic (different conditions each run) | Stable (small smooth shifts) |
| Condition count | 87 conditions, identity changes each run | All expressions contribute, weighted |
| Threshold | Implicit (bounding box edges) | Explicit (score cutoff you choose) |

## Risks / Things That Could Go Wrong

1. **All examples aren't actually from one cluster.** If DTSS has two distinct
   subtypes (e.g., "extended double top" vs. "flat double top"), a single
   center/spread would blur them together. Fix: detect multiple clusters, score
   against the nearest one. But start simple — one cluster — and see if it works.

2. **Expression correlations.** Many expressions measure related things (RSI and
   stochastic both measure momentum). Correlated expressions double-count evidence.
   Fix: downweight correlated groups. But even without this fix, the scoring is
   still better than bounding boxes.

3. **Threshold tuning.** The score cutoff is a new parameter. But it's one number
   that you tune once by looking at where your known examples score vs. the
   universe distribution. Much simpler than tuning beam width × depth × peak target
   × tier allocation.

## Suggested Build Order

1. Build the example profiler (mean/std/weight per expression)
2. Score the 69 known examples — verify they all score high
3. Score today's D1 universe — see what scores high, sanity check
4. Score full 5yr history — get the ranked list, compare to old signal set
5. Find the threshold that gives ~2-7/day average — compare win rate to old system
6. If it works: wire into pipeline, replace signal grinder step

Steps 1-4 can run right here in this session using the Railway API and
expr cache. Step 5 needs Dan's machine for the full 5yr scan. Step 6 is
integration.

## The Dartboard Metaphor (summary)

Old system: drew a big square on the wall. Anything that hits inside the
square counts. More examples = bigger square = more random darts count.

New system: drew a bullseye with rings. Anything that hits gets a score.
Center scores high, edges score low. More examples = tighter, more precise
bullseye = better separation between real setups and noise.

---

## Test Results (2026-03-10) — Pure Dartboard Doesn't Discriminate

Two runs completed with 69 DTSS examples, 500 expressions:

| Run | Threshold | Signals | Peak/day | Examples passing |
|-----|-----------|---------|----------|-----------------|
| 1 | 0.9158 (target_peak=5) | 304 | 5 | 1/66 |
| 2 | 0.5948 (min example score) | 53,447 | 518 | 66/66 |

**The problem:** Example scores range 0.59–0.92. The universe also has
millions of bars scoring 0.59+. Averaging 500 expression scores washes
out discrimination — weak signals average together, everything scores
similarly. There's no clean gap between examples and noise.

Run 1: Binary search for peak=5 forced the threshold to 0.9158, which is
above all but 1 example. The late-2021 signal cluster (a regime where DTSS
signals were everywhere) drove this — the search had to go absurdly high
to tame those few days.

Run 2: Threshold = min example score (GRPN at 0.5948). Every example passes
but so does everything else. 53K signals.

**Root causes:**
1. Additive scoring: a bar can be mediocre on most expressions and still
   average out to a passable score. Unlike the pyramid where one failed
   condition kills the bar.
2. 500 expressions is too many weak contributors. Most have marginal
   discriminating power, and they dilute the strong ones.
3. Threshold tuning has no sweet spot — the example distribution and
   universe distribution overlap too much.

## Proposed Fix: Hybrid Approach

Use the dartboard for **expression selection** and the pyramid for **signal filtering**.

**The insight:** The dartboard's Cohen's d weighting is a clean, deterministic
way to identify which expressions separate examples from the universe. No
beam search, no random walk, no instability. But combining them additively
(average score) is the wrong aggregation — it should be multiplicative
(all conditions must pass).

**How it works:**

1. **Dartboard step:** Build example profile, compute Cohen's d for every
   expression against the universe. Rank by discriminating power. This is
   stable and deterministic — same examples always produce the same ranking.

2. **Selection step:** Take the top N expressions by Cohen's d. For each,
   compute min/max across examples (like the pyramid's bounding box).
   N could be adaptive — e.g., all expressions with Cohen's d > some threshold,
   or a fixed count. The key question is what N to use.

3. **Filtering step:** Apply them as binary conditions, pyramid-style.
   A bar must be within [min, max] on ALL selected expressions. One failure
   kills the signal.

**What this gives us:**
- Stable expression selection (dartboard's strength)
- Tight multiplicative filtering (pyramid's strength)
- Setup-agnostic: Cohen's d adapts per setup
- Deterministic: same examples → same conditions → same signals
- More examples still help: sharper Cohen's d estimates

**What this replaces:**
- The beam search (replaced by Cohen's d ranking)
- The tier cascade (replaced by single-pass selection)
- The pure dartboard scoring (replaced by binary filtering)

**Open questions:**
- How many expressions to select? Fixed N, or adaptive d threshold?
- Should min/max ranges include a margin (e.g., p5/p95 instead of absolute min/max)?
- Do we still need the D1 cap (15 conditions) or is that a pyramid artifact?
