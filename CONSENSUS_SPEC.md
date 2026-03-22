# Consensus Engine + Permutation Test — Full Specification

**Created:** 2026-03-22
**Status:** SPEC ONLY — not yet implemented
**Replaces:** The single-run signal grind and refinement grind currently in pyramid_grinder.py

---

## WHY THIS EXISTS

The current pipeline runs the beam search once and uses all conditions it finds. With 68 examples and 15,805 expressions, a single beam search run finds ~87 conditions. Many of those conditions may be fitting noise — statistical coincidences in where the 68 examples happen to land across 15,805 dimensions.

Two problems need solving:

1. **Search instability:** The beam search is non-deterministic (greedy, order-dependent). Run it twice, get different conditions. Which run is right?
2. **Overfitting:** How many of those conditions describe the real DTSS pattern vs. noise in 68 data points?

Problem 1 is solved by multi-run consensus (Meinshausen & Bühlmann 2010 stability selection).
Problem 2 is solved by permutation testing — the standard method across genomics, neuroimaging, and ML for determining the noise floor of a search algorithm.

No EPV formula. No made-up ratios. No hard depth caps. The system generates its own answers from its own data.

---

## PIPELINE OVERVIEW

```
Step 1: Signal grind × 15 real runs + 10 permuted runs     (~8 hours overnight)
Step 2: Signal consensus engine                              (~seconds)
        → z-score gate: z > 3 required to proceed
        → If z < 3: STOP, vet more examples
Step 3: Deterministic scan with locked signal conditions     (~minutes)
        → Full signal population with WIN/LOSS classification
Step 4: Refinement grind × 10 runs                          (~3 hours overnight)
Step 5: Refinement consensus engine                          (~seconds)
        → Two tests per condition: consensus stability + binomial significance
        → If zero conditions survive: skip refinement, proceed with unrefined population
Step 6: EV grinder                                          (~minutes)
Step 7: Profit grinder                                      (~minutes)
```

Steps 1-2 are one overnight. Steps 3-5 are a second overnight. Steps 6-7 are fast.

The vetting loop still works: winner pile from step 3 → vet → new examples → re-run steps 1-2 with more examples → permutation test allows more conditions → tighter system.

---

## PART 1: SIGNAL GRIND CONSENSUS + PERMUTATION TEST

### What the permutation test does (plain language)

Imagine a metal detector on a beach. It beeps 87 times. How do you know it's not beeping at rocks?

Go to a beach where you KNOW there's no treasure. Same metal detector. It beeps 15 times.

Now you know: the first 15 beeps on the real beach might be junk too. Only beeps beyond what the empty beach produces are likely real.

The beam search is the metal detector. The 15,805 expressions are the beach. The 68 examples mark where treasure supposedly is. The "fake beach" is made by scrambling the examples to random (ticker, date) positions — they no longer represent a real pattern, but the beam search doesn't know that. It still finds conditions. Whatever it finds = the noise floor.

### Step 1A: Real signal grind runs (×15)

Run pyramid_grinder.py 15 times with the real example set. Each run uses:
- Same examples (same tickers, same entry_dates, same scan bars)
- Same expression cache
- Same universe
- Same beam search parameters (beam_width, peak_target)
- NO depth cap — let the beam search run to whatever depth it naturally reaches

The beam search is non-deterministic because it's greedy and order-dependent. Each run finds a somewhat different set of conditions. This is expected and desired — the variance between runs IS the measurement.

Output: 15 JSON files, each containing a list of conditions with names, bounds, filter power.

### Step 1B: Permuted signal grind runs (×10)

Run pyramid_grinder.py 10 times with SCRAMBLED examples. The scramble:
- Take the 68 (ticker, entry_date) pairs
- Randomly reassign each to a different (ticker, date) from the tradable universe
- The examples are now fake — they don't represent any real pattern
- Everything else stays the same: same expressions, same universe, same beam search parameters, same depth

The beam search runs on these fake examples and finds conditions — because with 15,805 features and 68 data points, there are always statistical coincidences to exploit. This is the noise floor.

Output: 10 JSON files, same format as real runs.

### Step 2: Signal consensus engine

**Phase A — Count condition frequencies (real runs):**
- For each of the 15 real runs, extract the condition names
- Count how many runs each condition appeared in
- A condition in 14/15 runs = reliably found regardless of search path
- A condition in 2/15 runs = artifact of that specific search order

**Phase B — Count condition frequencies (permuted runs):**
- Same counting on the 10 permuted runs
- These conditions are 100% noise — there's no real pattern in the scrambled data

**Phase C — Compute z-score:**

```
R = number of conditions that appeared in ≥ X/15 real runs (for some threshold X)
P_i = number of conditions that appeared in ≥ X/10 permuted runs (for each of 10 subsets)
mean_P = average of P_i across permuted bootstrap samples
std_P = standard deviation of P_i

z = (R - mean_P) / std_P
```

More precisely: the permuted runs generate a distribution of "how many conditions does noise produce at this consensus level." The real runs produce one number. The z-score measures how far the real number is from the noise distribution.

**Phase D — Gate decision:**

| z-score | Meaning | Decision |
|---------|---------|----------|
| z > 3 | Real conditions far exceed noise floor. 99.7% confidence the pattern is real. | PROCEED to step 3 |
| z 2-3 | Signal above noise, but moderate confidence. | Judgment call — proceed with caution or vet more |
| z < 2 | Real conditions statistically indistinguishable from noise. | STOP. Vet more examples. |

The z > 3 threshold is a universal statistical convention (99.7% confidence), not a system-specific parameter. It's the same standard used across all of science for "this result is not due to chance."

**Phase E — Lock conditions:**

If z > 3, take the conditions from the real consensus that appeared above the frequency threshold. These are the locked signal conditions. The bounds (low/high) will be recomputed from the full example set — the consensus only determines WHICH conditions to keep.

### Why this works as the EPV

The permutation test directly measures: "given my specific search algorithm, my specific feature space, and my specific sample size, how many conditions does noise produce?"

With 68 examples: noise might produce 15 consensus conditions. Real produces 40. The gap is the signal.

With 120 examples: noise might produce 8 consensus conditions (harder to find coincidences with more data points constraining the bounding box). Real produces 55. Bigger gap, more conditions are safe.

The noise floor shrinks as examples grow. The real signal stays or grows. The z-score naturally increases. No formula needed — the permutation test IS the EPV measurement, specific to your exact system.

### What needs to change in pyramid_grinder.py

1. **New `--permute` flag:** When set, scrambles example assignments before running. Takes the N (ticker, entry_date) pairs and randomly reassigns them to different positions in the tradable universe. Everything else stays the same.

2. **`--runs N` already exists** (line 3435). Just needs to work with `--permute`.

3. **No depth cap needed.** The beam search runs to whatever depth it reaches. The permutation test tells you which conditions are real after the fact.

---

## PART 2: DETERMINISTIC SCAN (Step 3)

After locking signal conditions, run ONE scan of the full tradable universe:
- Apply all locked consensus signal conditions
- Every (ticker, date) that passes all conditions = a signal
- Classify each signal as WIN or LOSS

### Classification pipeline (CRITICAL — must match exactly)

The classification is NOT just the exit condition. The full pipeline:
1. Apply locked signal conditions → signal bar identified
2. Apply exit expression + threshold → exit bar identified
3. Measure the move from signal bar to exit bar in ADR
4. Classify based on move size vs. ADR thresholds

This must reproduce the EXACT classification logic from Phase 2 (exit grind result + move measurement + ADR threshold). Same conditions, same exit expression, same thresholds. No shortcuts, no approximations.

The signal population from this scan is FIXED for all subsequent steps. It does not change.

---

## PART 3: REFINEMENT GRIND CONSENSUS (Steps 4-5)

### Why refinement can't use a permutation test

The refinement grinder has asymmetric data structures:
- **Winners:** Individual signals, each with one scan bar. They define the bounding box (min/max of each expression across all winners).
- **Losers:** Organized into clusters of consecutive bars. A cluster is only "eliminated" when ALL its bars fail at least one condition.

You can't cleanly shuffle WIN/LOSS labels because:
- A winner signal (single bar) can't become a loser cluster (multiple bars)
- A loser cluster can't become a winner (which bar defines the bounding box?)

The data structures are fundamentally different shapes. A label permutation doesn't apply mechanically.

### What refinement uses instead: two-test validation

The signal grind z > 3 already validates that the pattern is real and the signal population is legitimate. The refinement grind operates on that validated population (potentially thousands of classified signals). The overfitting risk in refinement is the beam search finding coincidental conditions — multi-run consensus + per-condition significance testing handles this.

### Step 4: Refinement grind × 10 runs

Run 10 times with:
- Same winner pile (fixed from step 3)
- Same loser cluster pile (fixed from step 3)
- Same winner bounding box (computed from fixed winners)
- Same expression cache
- Only the beam search path varies

Each run finds conditions that eliminate loser clusters while keeping all winners passing.

### Step 5: Refinement consensus engine

**Test 1 — Consensus stability:**
- Count condition frequency across 10 runs
- Apply consensus threshold from Meinshausen range (0.6-0.9)
- This is a convention within a proven range, not a self-derived number
- Conditions below threshold = artifacts of specific search order, discard

**Test 2 — Per-condition binomial significance (p < 0.01):**

This is the self-referential test. For each condition that passed Test 1:

The winner bounding box on expression X covers range [a, b]. Some fraction F of the entire tradable universe falls within [a, b] on any given day. By pure geometry, about (1 - F) of ANY random set of signals would fall outside [a, b] — not just losers.

The test asks: are losers excluded MORE than you'd expect from a random pile?

```
Expected exclusion rate = (1 - F)    [from universe baseline]
Observed exclusion rate = fraction of loser bars outside [a, b]

Binomial test: is observed significantly greater than expected?
p < 0.01 → condition genuinely targets losers, not just a narrow bounding box
p ≥ 0.01 → condition is geometric exclusion, discard
```

Plain language: if the bounding box is so narrow that 92% of the whole market falls outside it, and 93% of losers fall outside it — that 1% gap is nothing, probably coincidence. But if 92% of the market falls outside and 99% of losers fall outside — that 7% gap is real. Losers specifically cluster outside the winner range on that expression.

**A condition must pass BOTH tests to survive:**
1. Appeared in enough runs (not a beam search fluke)
2. Individually significant at p < 0.01 (genuinely targets losers beyond geometric chance)

**After both tests, there is no third test.** Once conditions are locked, applying them to the fixed loser pile is deterministic arithmetic — one answer, no variance, no spread to measure.

### Step 5 gate: proceed or skip refinement

- **Any conditions survive both tests?** → Apply them. The loser elimination rate is whatever it is — 15%, 40%, 80%. Any amount backed by validated conditions is genuine improvement.
- **Zero conditions survive?** → Skip refinement entirely. Proceed to EV grinder on the unrefined signal population (lower WR, more signals). The pipeline still works — refinement is an improvement layer, not a prerequisite.

---

## PART 4: DOWNSTREAM (Steps 6-7)

After consensus signal conditions + validated refinement conditions:

**Step 6: EV grinder** — runs on the (potentially refined) signal population. Scores every signal using market features + setup features. No changes to EV grinder needed — it takes whatever signal population it gets.

**Step 7: Profit grinder** — runs on scored signals. Entry/exit optimization. No changes needed.

The sliders in the UI (entry quality, refinement depth) work as before — they let you trade off signal count vs. quality within the validated population.

---

## WHAT NEEDS TO BE BUILT

### New scripts:
1. **`scripts/consensus_engine.py`** — EXISTS but needs rewrite to remove fake EPV cap and add permutation comparison + z-score
2. Permutation logic inside `pyramid_grinder.py` — new `--permute` flag

### Changes to existing scripts:
1. **`local_runner/pyramid_grinder.py`**:
   - `--permute` flag: scrambles example (ticker, date) assignments
   - `--conditions-file` argument: accepts pre-supplied consensus conditions for deterministic scan in step 3
   - No depth cap changes needed

2. **`scripts/consensus_engine.py`** (rewrite):
   - Read real + permuted run outputs
   - Count condition frequencies for both sets
   - Compute z-score comparing real vs permuted
   - Print z-score, gate decision, frequency distributions
   - Output locked condition set if z > 3
   - For refinement: consensus stability + binomial significance test per condition
   - Output locked refinement conditions

### No changes needed:
- `scripts/ev_grinder.py` — takes whatever signal population it gets
- `scripts/profit_grinder.py` / `profit_grinder_2stage.py` — takes whatever signals it gets
- `scanperfect.py` — UI doesn't need changes for the overnight runs
- Nightly pipeline — this is on-demand, not nightly

---

## KEY DESIGN DECISIONS (preserved from 2026-03-22 session)

1. **No EPV formula.** The permutation test IS the EPV. Adjacent research (Peduzzi EPV=10, Vittinghoff EPV=5) doesn't apply to conjunctive beam search on 15,805 TA expressions. The PAC learning bounds give K≤1 for 68 examples (absurdly conservative). The only honest answer is empirical measurement.

2. **No hard depth cap.** The beam search runs to whatever depth it reaches. The permutation test determines how many conditions are real after the fact. Capping depth with a formula would either be too conservative (throwing away real conditions) or too aggressive (keeping noise).

3. **z > 3 is the signal grind gate.** This is a universal statistical convention (99.7% confidence), not a system-specific parameter. It means "there's a 0.1% chance the real result came from noise."

4. **Refinement can't use permutation testing** because the winner bounding box + loser cluster data structures are asymmetric — you can't cleanly shuffle WIN/LOSS labels. Instead: consensus stability + per-condition binomial significance test.

5. **The p < 0.01 binomial test is self-referential.** The universe baseline exclusion rate comes from the data. The loser exclusion rate comes from the data. The p-value comes from standard binomial math. No external numbers needed.

6. **Refinement is optional.** If zero conditions pass both refinement tests, skip it. The EV grinder runs on the unrefined population. The pipeline works without refinement — it just works better with it.

7. **No minimum loser elimination threshold.** Any elimination backed by validated conditions is genuine. Whether it's 15% or 80% is an output you observe, not a gate you enforce.

8. **The example count does NOT gate the pipeline.** You can run this with 68 examples tonight. The permutation test tells you what 68 examples can support. More examples → permutation noise floor drops → more conditions survive → better system. But it's a continuous improvement, not a binary gate.

9. **On-demand, not scheduled.** This runs when enough new examples have been vetted to justify the compute time. Not nightly. 8-10 setup types, one per night when needed.

10. **Number of runs:** 15 real signal + 10 permuted signal + 10 refinement. These numbers come from Meinshausen & Bühlmann (2010) — 15 runs gives clear separation between stable (11+/15) and noise (4-/15). 10 runs is the minimum viable for permutation null distribution.

---

## THEORETICAL GROUNDING

**Stability selection (Meinshausen & Bühlmann 2010):**
- Running a selection algorithm N times on subsampled data and keeping only features that appear consistently
- Proven to control false discoveries under general conditions
- Recommended N: 100 for lasso (fast), 10-20 for expensive searches
- Consensus threshold π ∈ (0.6, 0.9) proven to control family-wise error rate

**Permutation testing:**
- Standard across genomics (Tusher et al. 2001 SAM), neuroimaging, ML (scikit-learn permutation_test_score)
- Shuffles labels to destroy real signal, re-runs the analysis, measures what the algorithm finds from pure noise
- The null distribution is specific to the exact algorithm, feature space, and sample size — no assumptions needed
- z-score comparison to null distribution is the standard framework

**PAC learning / VC dimension (for context, not used directly):**
- For K conjunction conditions on d=15,805 features with N=68 examples: theoretical safe K ≤ 1
- These bounds are distribution-free worst-case, famously conservative
- Confirms that no formula can give a precise EPV for this system
- Motivates the empirical (permutation test) approach

**Binomial significance test:**
- Standard statistical test for "is the observed rate different from the expected rate?"
- Used here to test each refinement condition: does it exclude losers more than the universe baseline?
- p < 0.01 is a standard significance threshold (1% false positive rate per condition)

---

## OPEN QUESTIONS

1. **Consensus threshold for refinement (Test 1):** Meinshausen proves 0.6-0.9 works. The exact value within that range is a convention, not self-derived. The binomial test (Test 2) does the heavy lifting — Test 1 is a secondary stability filter. Getting it wrong by ±0.1 changes which borderline conditions survive, but individually non-significant ones are already gone from Test 2.

2. **Permutation scramble method:** "Randomly reassign each example to a different (ticker, date)" — needs to be specified precisely. Options: (a) random ticker + random date from that ticker's history, (b) random row from the full universe matrix. Need to match the data structure the beam search expects.

3. **z-score computation details:** The description above is conceptual. Need to specify exactly: at what consensus threshold do you count R and P? Do you sweep thresholds? Do you use the total count, or compare at each frequency level? This needs to be worked out during implementation.

4. **Step 3 classification pipeline:** Needs to be documented in exact detail — which script, which function, which thresholds. "Must match exactly" is the requirement; the specific code path needs to be traced and documented.
