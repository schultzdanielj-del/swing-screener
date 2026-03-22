# Refinement Grinder — Specification

**Created:** 2026-03-22
**Script:** `local_runner/pyramid_grinder.py` → `run_refinement()`
**Status:** Current state TBD (to be documented in next session by reading actual code)

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

## PROPOSED CHANGES

### 1. Multi-run support (10 runs)

Run `run_refinement()` 10 times with identical inputs:
- Same winner pile (fixed from deterministic scan with consensus signal conditions)
- Same loser cluster pile (fixed from same scan)
- Same winner bounding box
- Same expression cache
- Only the beam search path varies (greedy order-dependent search)

Each run finds a somewhat different set of refinement conditions. Output: 10 JSON files.

### 2. Why refinement CANNOT use permutation testing

The refinement grinder has asymmetric data structures:

- **Winners:** Individual signals, each with one scan bar. They define the bounding box (min/max of each expression value across all winners).
- **Losers:** Organized into clusters of consecutive bars. A cluster is only "eliminated" when ALL its bars fail at least one condition.

You cannot cleanly shuffle WIN/LOSS labels because:
- A winner signal (single bar) cannot become a loser cluster (multiple bars)
- A loser cluster cannot become a winner (which bar defines the bounding box?)
- The data structures are fundamentally different shapes

A label permutation test does not apply mechanically to this architecture.

### 3. Two-test validation per condition

Every refinement condition must pass BOTH tests to survive.

**Test 1 — Consensus stability:**
- Count condition frequency across 10 runs
- Apply consensus threshold from Meinshausen proven range (0.6-0.9)
- This is a convention within a mathematically proven range
- Conditions below threshold = artifacts of specific beam search order, discard

**Test 2 — Per-condition binomial significance (p < 0.01):**

This is the self-referential test that replaces what permutation testing does for the signal grind.

For each condition that passed Test 1:

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

### 4. No third test exists

Once conditions are locked from tests 1 and 2, applying them to the fixed loser pile is deterministic arithmetic. Fixed conditions + fixed loser pile = one number. There is no variance to measure, no spread to check. The output is what it is.

### 5. Step gate: proceed or skip refinement

- **Any conditions survive both tests?** → Apply them. The loser elimination rate is whatever it is — 15%, 40%, 80%. Any amount backed by validated conditions is genuine improvement. No minimum elimination threshold.
- **Zero conditions survive?** → Skip refinement entirely. Proceed to EV grinder on the unrefined signal population (lower WR, more signals). Refinement is an improvement layer, not a prerequisite. The pipeline works without it.

### 6. Consensus engine handles refinement

`scripts/consensus_engine.py` (same script as signal consensus, different mode):

- `--stage refinement` flag
- Reads 10 refinement run JSONs
- Extracts `refinement_conditions_only` from each
- Counts frequencies (Test 1)
- For each surviving condition, runs binomial significance test against universe baseline (Test 2)
- Outputs locked refinement conditions

### 7. Prerequisite: signal grind z > 3

The refinement grind should only run after the signal grind has passed z > 3. The signal population that refinement operates on must come from validated consensus signal conditions. If the signal pattern isn't real, refining it is pointless.

However, the refinement z > 3 is NOT a separate gate in the same way. The signal grind z > 3 validates the pattern. The refinement two-test validation ensures individual conditions are real. If any conditions survive both tests, refinement proceeds. If none survive, refinement is skipped (not failed — just not applicable).

---

## RELATIONSHIP TO SIGNAL GRINDER

The refinement grinder's input is the signal grinder's output:

```
Signal grind consensus (z > 3)
  → Locked signal conditions
    → Deterministic scan of tradable universe
      → Signal population (WIN/LOSS classified)
        → Refinement grind input
```

The classification pipeline that determines WIN vs LOSS is CRITICAL and must be documented exactly. The exit condition alone does NOT determine winner/loser piles. The full pipeline includes: exit expression + threshold → exit bar → move measurement in ADR → classification based on move size vs thresholds. This must be traced through the actual code and documented in the CURRENT STATE section.

---

## WHAT THE SIGNAL GRIND z > 3 ALREADY PROVIDES

The signal grind permutation test validates that the setup pattern is real — the signal conditions catch genuine setups, not noise. This means:

- The signal population is trustworthy
- The WIN/LOSS classification within that population reflects real outcomes
- The refinement grind operates on validated data

What signal z > 3 does NOT validate:
- Whether winners and losers are distinguishable by expression conditions
- Whether the refinement beam search finds real structure or coincidence

That's what the two-test refinement validation covers.

---

## OPEN QUESTIONS FOR IMPLEMENTATION

1. How does `run_refinement()` currently load the signal population? Does it re-scan, or read from a file? (Needs to read from the deterministic scan output)
2. The exact classification pipeline — which functions, which thresholds, which exit expression
3. How loser clusters are constructed (consecutive bars? same ticker?)
4. Universe baseline for binomial test — compute once from the full tradable universe expression cache, or per-date?
5. How to efficiently compute the universe fraction F for each expression's bounding box [a, b]
6. The `refinement_conditions_only` key in output — verify this is consistently present across all refinement outputs
7. Does the current `--runs` flag work for refinement, or only for signal grind?
8. Consensus threshold within 0.6-0.9 range — exact value is a convention. The binomial test (Test 2) does the heavy lifting; Test 1 is secondary stability filter.
