# Refinement Grinder — Specification

**Created:** 2026-03-22
**Script:** `local_runner/pyramid_grinder.py` → `run_refinement()`
**Status:** SPEC ONLY — not yet implemented. Current state TBD (to be documented in next session by reading actual code).
**Prerequisite:** Signal grind z > 3 (see `SIGNAL_GRINDER.md`)

---

## CURRENT STATE

*(To be filled in next session by reading pyramid_grinder.py)*

- Entry point: `run_refinement()`
- Inputs: TBD
- Outputs: TBD
- How it loads the signal population: TBD
- Winner bounding box computation (exact min/max, no margin): TBD
- Loser cluster structure (multiple bars per cluster): TBD
- Cluster-aware beam search mechanics: TBD
- How clusters get eliminated (ALL bars must fail): TBD
- Winner leftward bars in loser matrix: TBD
- Combined conditions (signal + refinement): TBD
- Exit condition handling: TBD
- Classification pipeline (how WIN/LOSS is determined): TBD
- Validation (all winners must pass combined set): TBD
- Depth progression output: TBD
- Save format + Railway mirror: TBD

---

## WHERE REFINEMENT FITS IN THE PIPELINE

```
Signal grind consensus (z > 3)                   ← see SIGNAL_GRINDER.md
  → Locked signal conditions
    → Deterministic scan of tradable universe     ← see SIGNAL_GRINDER.md step 3
      → Signal population (WIN/LOSS classified)
        → THIS: Refinement grind input
          → EV grinder
            → Profit grinder
```

The signal population from SIGNAL_GRINDER.md step 3 is FIXED. Same winners, same loser clusters, same classification for every refinement run.

---

## WHY REFINEMENT CANNOT USE PERMUTATION TESTING

The refinement grinder has asymmetric data structures:

- **Winners:** Individual signals, each with one scan bar. They define the bounding box (min/max of each expression value across all winners).
- **Losers:** Organized into clusters of consecutive bars. A cluster is only "eliminated" when ALL its bars fail at least one condition.

You cannot cleanly shuffle WIN/LOSS labels because:
- A winner signal (single bar) cannot become a loser cluster (multiple bars)
- A loser cluster cannot become a winner (which bar defines the bounding box?)
- The data structures are fundamentally different shapes

A label permutation test does not apply mechanically to this architecture.

The signal grind z > 3 already validates that the pattern is real and the signal population is legitimate. The refinement grind operates on that validated population. The overfitting risk in refinement is the beam search finding coincidental conditions — multi-run consensus + per-condition significance testing handles this without needing permutation.

---

## PROPOSED CHANGES

### Step 4: Refinement grind × 10 runs

Run `run_refinement()` 10 times with identical inputs:
- Same winner pile (fixed from deterministic scan with consensus signal conditions)
- Same loser cluster pile (fixed from same scan)
- Same winner bounding box (computed from fixed winners, exact min/max, no margin)
- Same expression cache
- Only the beam search path varies (greedy order-dependent search)

Each run finds a somewhat different set of refinement conditions. Output: 10 JSON files.

### Step 5: Refinement consensus engine — two-test validation

Every refinement condition must pass BOTH tests to survive.

**Test 1 — Consensus stability:**
- Count condition frequency across 10 runs
- Apply consensus threshold from Meinshausen proven range (0.6-0.9)
- This is a convention within a mathematically proven range, not a self-derived number
- Conditions below threshold = artifacts of specific beam search order, discard

**Test 2 — Per-condition binomial significance (p < 0.01):**

This is the self-referential test. For each condition that passed Test 1:

1. The winner bounding box on expression X covers range [a, b]
2. Compute what fraction F of the entire tradable universe falls within [a, b] on any given day
3. By pure geometry, about (1 - F) of ANY random set of signals would fall outside [a, b] — not just losers
4. Measure the actual fraction of loser bars that fall outside [a, b]
5. Run a binomial test: is the loser exclusion rate significantly greater than the universe baseline (1 - F)?

```
Expected exclusion rate = (1 - F)    [from universe baseline]
Observed exclusion rate = fraction of loser bars outside [a, b]

Binomial test: is observed significantly > expected?
p < 0.01 → condition genuinely targets losers specifically
p ≥ 0.01 → condition is just geometric exclusion from a narrow bounding box, discard
```

**Plain language example:**
If the bounding box is so narrow that 92% of the whole market falls outside it, and 93% of losers fall outside — that 1% gap is nothing, probably coincidence. But if 92% of the market falls outside and 99% of losers fall outside — that 7% gap is real. Losers specifically have expression values outside the winner range on that expression more than random signals do.

The p < 0.01 threshold is a standard statistical significance level (1% false positive rate per condition).

### Why the binomial test is self-referential

The universe baseline exclusion rate comes from the data. The loser exclusion rate comes from the data. The p-value comes from standard binomial math. No external numbers, no formulas, no assumptions about sample size needed. The data generates its own significance threshold.

### There is no third test

Once conditions are locked from tests 1 and 2, applying them to the fixed loser pile is deterministic arithmetic. Fixed conditions + fixed loser pile = one number. There is no variance to measure, no spread to check. The output is what it is.

### Step 5 gate: proceed or skip refinement

- **Any conditions survive both tests?** → Apply them. The loser elimination rate is whatever it is — 15%, 40%, 80%. Any amount backed by validated conditions is genuine improvement. No minimum elimination threshold.
- **Zero conditions survive?** → Skip refinement entirely. Proceed to EV grinder on the unrefined signal population (lower WR, more signals). Refinement is an improvement layer, not a prerequisite. The pipeline works without it.

---

## WHAT THE SIGNAL GRIND z > 3 ALREADY PROVIDES

The signal grind permutation test validates:
- The setup pattern is real
- The signal population is trustworthy
- The WIN/LOSS classification within that population reflects real outcomes
- The refinement grind operates on validated data

What signal z > 3 does NOT validate:
- Whether winners and losers are distinguishable by expression conditions
- Whether the refinement beam search finds real structure or coincidence

That's what the two-test refinement validation covers. If signal z > 3 but refinement finds zero significant conditions, it means: the pattern exists and the scan finds genuine setups, but whether each one wins or loses isn't predictable from expression conditions. Winning might depend on things the 15,805 expressions don't capture — news, earnings timing, sector rotation, pure luck.

In that case: skip refinement, proceed to EV grinder on the unrefined population. The EV grinder scores on market features and setup features which might still have predictive power.

---

## WHAT NEEDS TO CHANGE IN pyramid_grinder.py

1. **`--runs N` for refinement:** Verify this works for `run_refinement()`, not just `run_pyramid()`.
2. **Refinement must accept fixed signal population from step 3** instead of re-scanning. Currently `run_refinement()` calls `_gather_raw_signal_clusters()` which re-scans. Needs to read from the deterministic scan output.

## WHAT NEEDS TO CHANGE IN consensus_engine.py

- `--stage refinement` mode
- Read 10 refinement run JSONs
- Extract `refinement_conditions_only` from each
- Count frequencies (Test 1)
- For each surviving condition, run binomial significance test against universe baseline (Test 2)
- Output locked refinement conditions

---

## THEORETICAL GROUNDING

**Stability selection (Meinshausen & Bühlmann 2010):**
- Consensus threshold π ∈ (0.6, 0.9) proven to control family-wise error rate
- 10 runs is minimum viable for consensus measurement
- The exact threshold within 0.6-0.9 is a convention; the binomial test (Test 2) does the heavy lifting

**Binomial significance test:**
- Standard statistical test for "is the observed rate different from the expected rate?"
- Used here to test each refinement condition: does it exclude losers more than the universe baseline?
- p < 0.01 is a standard significance threshold (1% false positive rate per condition)
- No assumptions about sample size, feature count, or search algorithm needed

---

## KEY DESIGN DECISIONS

1. **Refinement can't use permutation testing** because winner bounding box + loser cluster data structures are asymmetric — can't cleanly shuffle labels.
2. **The p < 0.01 binomial test is self-referential.** Universe baseline from data, loser exclusion from data, p-value from standard math. No external numbers.
3. **Refinement is optional.** If zero conditions pass both tests, skip it. Pipeline works without it.
4. **No minimum loser elimination threshold.** Any elimination backed by validated conditions is genuine. Whether it's 15% or 80% is an output, not a gate.
5. **Consensus threshold within 0.6-0.9** is a convention, not self-derived. Test 2 does the real work; Test 1 is secondary stability filter.

---

## OPEN QUESTIONS FOR IMPLEMENTATION

1. How does `run_refinement()` currently load the signal population? Does it re-scan, or read from a file?
2. The exact classification pipeline — which functions, which thresholds, which exit expression
3. How loser clusters are constructed (consecutive bars? same ticker?)
4. Universe baseline for binomial test — compute once from the full tradable universe expression cache, or per-date?
5. How to efficiently compute the universe fraction F for each expression's bounding box [a, b]
6. The `refinement_conditions_only` key in output — verify consistently present across all refinement outputs
7. Does the current `--runs` flag work for refinement, or only for signal grind?
8. Consensus threshold: exact value within 0.6-0.9 range
