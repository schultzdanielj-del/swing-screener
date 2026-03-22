# Signal Grinder — Specification

**Created:** 2026-03-22
**Script:** `local_runner/pyramid_grinder.py` → `run_pyramid()`
**Status:** SPEC ONLY — not yet implemented. Current state TBD (to be documented in next session by reading actual code).

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

## CURRENT STATE

*(To be filled in next session by reading pyramid_grinder.py)*

- Entry point: `run_pyramid()`
- Inputs: TBD
- Outputs: TBD
- Beam search mechanics: TBD
- Multi-pass logic (daily→weekly→monthly): TBD
- D1 tier logic: TBD
- Example loading + scan bar resolution: TBD
- Expression cache usage: TBD
- Universe matrix construction: TBD
- Pre-filter (85% threshold): TBD
- Condition output format: TBD
- Validation (100% example pass): TBD
- Save format + Railway mirror: TBD

---

## FULL PIPELINE OVERVIEW

```
Step 1: Signal grind × 15 real runs + 10 permuted runs     (~8 hours overnight)
Step 2: Signal consensus engine                              (~seconds)
        → z-score gate: z > 3 required to proceed
        → If z < 3: STOP, vet more examples
Step 3: Deterministic scan with locked signal conditions     (~minutes)
        → Full signal population with WIN/LOSS classification
        → This is the input to REFINEMENT_GRINDER.md
Step 4: Refinement grind × 10 runs                          (~3 hours, see REFINEMENT_GRINDER.md)
Step 5: Refinement consensus engine                          (see REFINEMENT_GRINDER.md)
Step 6: EV grinder                                          (~minutes, no changes needed)
Step 7: Profit grinder                                      (~minutes, no changes needed)
```

Steps 1-2 are one overnight. Steps 3-5 are a second overnight. Steps 6-7 are fast.

The vetting loop still works: winner pile from step 3 → vet → new examples → re-run steps 1-2 with more examples → permutation test allows more conditions → tighter system.

---

## PROPOSED CHANGES

### Step 1A: Real signal grind runs (×15)

Run pyramid_grinder.py 15 times with the real example set. Each run uses:
- Same examples (same tickers, same entry_dates, same scan bars)
- Same expression cache
- Same universe
- Same beam search parameters (beam_width, peak_target)
- NO depth cap — let the beam search run to whatever depth it naturally reaches

The beam search is non-deterministic because it's greedy and order-dependent. Each run finds a somewhat different set of conditions. This is expected and desired — the variance between runs IS the measurement.

Output: 15 JSON files, each containing a list of conditions with names, bounds, filter power.

- `--runs N` flag already exists (line 3435). Needs verification that it produces N independent outputs with unique filenames.

### Step 1B: Permuted signal grind runs (×10)

New `--permute` flag. When set:

- Take the 68 (ticker, entry_date) pairs
- Randomly reassign each to a different (ticker, date) from the tradable universe
- The examples are now fake — they don't represent any real pattern
- Everything else stays the same: same expressions, same universe, same beam search parameters, same depth

The beam search runs on these fake examples and finds conditions — because with 15,805 features and 68 data points, there are always statistical coincidences to exploit. This is the noise floor.

Output: 10 JSON files, same format as real runs, tagged as permuted (filename or field in JSON).

**Scramble method (needs precise specification):**
- For each of the N examples, pick a random ticker from the tradable universe and a random valid bar index from that ticker's 5yr cache
- Construct a fake example_df with that ticker's OHLCV data and the random scan_idx
- Feed these fake example_dfs into the exact same pipeline as real examples
- The beam search computes bounding boxes, pre-filters, builds matrices, runs beam search — all identical, just on fake data

### What the permutation test does (plain language)

Imagine a metal detector on a beach. It beeps 87 times. How do you know it's not beeping at rocks?

Go to a beach where you KNOW there's no treasure. Same metal detector. It beeps 15 times.

Now you know: the first 15 beeps on the real beach might be junk too. Only beeps beyond what the empty beach produces are likely real.

The beam search is the metal detector. The 15,805 expressions are the beach. The 68 examples mark where treasure supposedly is. The "fake beach" is made by scrambling the examples to random (ticker, date) positions — they no longer represent a real pattern, but the beam search doesn't know that. It still finds conditions. Whatever it finds = the noise floor.

### Step 2: Signal consensus engine

`scripts/consensus_engine.py` (needs rewrite from current version)

**Phase A — Count condition frequencies (real runs):**
- Read all 15 real run JSONs
- Extract condition names from each
- Count how many of 15 runs each condition appeared in
- A condition in 14/15 runs = reliably found regardless of search path
- A condition in 2/15 runs = artifact of that specific search order

**Phase B — Count condition frequencies (permuted runs):**
- Read all 10 permuted run JSONs
- Same frequency counting
- These conditions are 100% noise — there's no real pattern in the scrambled data

**Phase C — z-score computation:**

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

z > 3 is a universal statistical convention (99.7% confidence), not a system-specific parameter.

**Phase E — Lock conditions:**

If z > 3, take the conditions from the real consensus that appeared above the frequency threshold. These are the locked signal conditions. The bounds (low/high) will be recomputed from the full example set — the consensus only determines WHICH conditions to keep.

Output: `consensus_signal_{setup}.json` with locked conditions + z-score + stability metrics.

### Step 3: Deterministic scan with locked conditions

New `--conditions-file` argument on pyramid_grinder. Accepts the consensus JSON. When provided:
- Skip the beam search entirely
- Use the supplied conditions to scan the full tradable universe
- Every (ticker, date) that passes all conditions = a signal
- Classify each signal as WIN or LOSS

**Classification pipeline (CRITICAL — must match exactly):**

The classification is NOT just the exit condition. The full pipeline:
1. Apply locked signal conditions → signal bar identified
2. Apply exit expression + threshold → exit bar identified
3. Measure the move from signal bar to exit bar in ADR
4. Classify based on move size vs. ADR thresholds

This must reproduce the EXACT classification logic from Phase 2 (exit grind result + move measurement + ADR threshold). Same conditions, same exit expression, same thresholds. No shortcuts.

The signal population from this scan is FIXED for all subsequent steps. It does not change. It becomes the input to the refinement grinder (see `REFINEMENT_GRINDER.md`).

---

## WHY PERMUTATION TEST IS THE EPV

No EPV formula works for this system:
- PAC/VC theory gives K≤1 for 68 examples on 15,805 features (absurdly conservative, distribution-free worst-case)
- Peduzzi EPV=10 is for logistic regression, not conjunctive beam search
- Vittinghoff & McCulloch EPV=5 still doesn't apply to this architecture
- No published EPV guideline exists for "beam search conjunctive rule mining on 15,805 TA expressions with 100%-pass constraint"

The permutation test directly measures: "given MY search algorithm, MY feature space, and MY sample size, how many conditions does noise produce?" That measurement IS the EPV for this specific system.

With 68 examples: noise produces X conditions. Real produces Y. The gap (z-score) tells you if Y is real.
With 120 examples: noise floor drops (harder to find coincidences with more data constraining bounding boxes). Same real signal. Higher z-score. More conditions survive.

The noise floor naturally calibrates to example count, feature count, and search algorithm. No formula needed.

The example count does NOT gate the pipeline. You can run this with 68 examples tonight. The permutation test tells you what 68 examples can support. More examples → noise floor drops → more conditions survive → better system. But it's continuous improvement, not a binary prerequisite.

---

## WHAT NEEDS TO CHANGE IN pyramid_grinder.py

1. **New `--permute` flag:** When set, scrambles example assignments before running. Takes the N (ticker, entry_date) pairs and randomly reassigns them to different positions in the tradable universe. Everything else stays the same.

2. **`--runs N` already exists** (line 3435). Needs to work with `--permute` and produce independently-named outputs.

3. **New `--conditions-file` argument:** Accepts pre-supplied consensus conditions for deterministic scan (step 3).

4. **No depth cap changes needed.** The beam search runs to whatever depth it reaches. The permutation test determines how many conditions are real after the fact.

## WHAT NEEDS TO CHANGE IN consensus_engine.py

Full rewrite (current version has fake EPV cap logic):
- Read real + permuted run outputs
- Count condition frequencies for both sets
- Compute z-score comparing real vs permuted
- Print z-score, gate decision, frequency distributions
- Output locked condition set if z > 3

---

## THEORETICAL GROUNDING

**Stability selection (Meinshausen & Bühlmann 2010):**
- Running a selection algorithm N times on subsampled data and keeping only features that appear consistently
- Proven to control false discoveries under general conditions
- Recommended N: 100 for lasso (fast), 10-20 for expensive searches
- 15 runs gives clear separation between stable (11+/15) and noise (4-/15)
- 10 runs is the minimum viable for permutation null distribution
- Consensus threshold π ∈ (0.6, 0.9) proven to control family-wise error rate

**Permutation testing:**
- Standard across genomics (Tusher et al. 2001 SAM), neuroimaging, ML (scikit-learn permutation_test_score)
- Shuffles labels to destroy real signal, re-runs the analysis, measures what the algorithm finds from pure noise
- The null distribution is specific to the exact algorithm, feature space, and sample size — no assumptions needed
- z-score comparison to null distribution is the standard framework

**PAC learning / VC dimension (context for why formulas don't work):**
- For K conjunction conditions on d=15,805 features with N=68 examples: theoretical safe K ≤ 1
- These bounds are distribution-free worst-case, famously conservative
- Confirms that no formula can give a precise EPV for this system
- Motivates the empirical (permutation test) approach

---

## KEY DESIGN DECISIONS

1. **No EPV formula.** The permutation test IS the EPV. The only honest answer is empirical measurement.
2. **No hard depth cap.** The beam search runs to whatever depth it reaches. Capping depth with a formula would either be too conservative (throwing away real conditions) or too aggressive (keeping noise).
3. **z > 3 is the gate.** Universal statistical convention (99.7% confidence). Means "0.1% chance the real result came from noise."
4. **On-demand, not scheduled.** This runs when enough new examples have been vetted to justify the compute time. Not nightly.
5. **Number of runs:** 15 real + 10 permuted. From Meinshausen & Bühlmann (2010).

---

## OPEN QUESTIONS FOR IMPLEMENTATION

1. Exact scramble method for `--permute` (random ticker + random bar from that ticker? random row from universe matrix?)
2. Does `--runs N` already produce N independent outputs with unique filenames, or does it overwrite?
3. z-score computation: at what consensus threshold do you count R and P? Sweep thresholds? Fixed?
4. How to tag permuted outputs so consensus engine can distinguish real vs permuted
5. The current condition output format — need to document exact JSON structure
6. Step 3 classification pipeline — needs exact code path traced and documented
