# Signal Grinder — Specification

**Created:** 2026-03-22
**Script:** `local_runner/pyramid_grinder.py` → `run_pyramid()`
**Status:** Current state TBD (to be documented in next session by reading actual code)

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

## PROPOSED CHANGES

### 1. Multi-run support (15 real runs)

Run `run_pyramid()` 15 times with identical inputs. The beam search is non-deterministic (greedy, order-dependent), so each run finds a somewhat different condition set. This variance is the measurement — not a bug.

- `--runs N` flag already exists (line 3435). Needs verification that it produces N independent outputs with unique filenames.
- Each run uses: same examples, same expression cache, same universe, same beam search parameters.
- No depth cap. Let the beam search run to whatever depth it naturally reaches.
- Output: 15 JSON files in `local_runner/cache/`, each with timestamped unique name.

### 2. Permutation mode (10 permuted runs)

New `--permute` flag. When set:

- Take the N example (ticker, entry_date) pairs
- Randomly reassign each to a different (ticker, date) position from the tradable universe
- The examples are now fake — they don't represent any real pattern
- Everything else stays identical: same expressions, same universe, same beam search params, same depth
- The beam search runs on fake examples and finds conditions from pure statistical coincidence
- This IS the noise floor — it measures what the search algorithm finds when there's nothing real to find

**Scramble method (needs precise specification):**
- For each of the N examples, pick a random ticker from the tradable universe and a random valid bar index from that ticker's 5yr cache
- Construct a fake example_df with that ticker's OHLCV data and the random scan_idx
- Feed these fake example_dfs into the exact same pipeline as real examples
- The beam search computes bounding boxes, pre-filters, builds matrices, runs beam search — all identical, just on fake data

**Output:** 10 JSON files, same format as real runs, but tagged as permuted (filename or field in JSON).

### 3. Consensus engine reads signal grind outputs

`scripts/consensus_engine.py` (needs rewrite from current version):

**Phase A — Count condition frequencies (real runs):**
- Read all 15 real run JSONs
- Extract condition names from each
- Count how many of 15 runs each condition appeared in
- Condition in 14/15 = reliably found. Condition in 2/15 = search artifact.

**Phase B — Count condition frequencies (permuted runs):**
- Read all 10 permuted run JSONs
- Same frequency counting
- These conditions are 100% noise

**Phase C — z-score computation:**
```
R = number of conditions surviving consensus threshold from real runs
P_i = number of conditions surviving same threshold from permuted runs
     (computed per permuted run or via bootstrap from the 10 runs)
mean_P = average across permuted distribution
std_P = standard deviation

z = (R - mean_P) / std_P
```

**Phase D — Gate:**
- z > 3: PROCEED (99.7% confidence pattern is real)
- z 2-3: Judgment call
- z < 2: STOP — vet more examples

z > 3 is a universal statistical convention, not system-specific.

**Phase E — Lock conditions:**
- If z > 3, take real consensus conditions as locked set
- Bounds (low/high) recomputed from full example set
- Output: `consensus_signal_{setup}.json` with locked conditions + z-score + stability metrics

### 4. Conditions-file input for deterministic scan

New `--conditions-file` argument on pyramid_grinder. Accepts a JSON file of pre-supplied consensus conditions. When provided:
- Skip the beam search entirely
- Use the supplied conditions to scan the universe
- Classify every passing signal as WIN/LOSS
- Output: signal population for refinement

This bridges signal consensus → refinement grind.

---

## WHY PERMUTATION TEST IS THE EPV

No EPV formula works for this system:
- PAC/VC theory gives K≤1 for 68 examples on 15,805 features (absurdly conservative)
- Peduzzi EPV=10 is for logistic regression, not conjunctive beam search
- No published EPV guideline exists for "beam search conjunctive rule mining on 15,805 TA expressions with 100%-pass constraint"

The permutation test directly measures: "given MY search algorithm, MY feature space, and MY sample size, how many conditions does noise produce?" That measurement IS the EPV for this specific system.

With 68 examples: noise produces X conditions. Real produces Y. The gap (z-score) tells you if Y is real.
With 120 examples: noise floor drops (harder to find coincidences with more data constraining bounding boxes). Same real signal. Higher z-score. More conditions are safe.

The noise floor naturally calibrates to example count, feature count, and search algorithm. No formula needed.

---

## OPEN QUESTIONS FOR IMPLEMENTATION

1. Exact scramble method for `--permute` (random ticker + random bar from that ticker? random row from universe matrix?)
2. Does `--runs N` already produce N independent outputs with unique filenames, or does it overwrite?
3. z-score computation: at what consensus threshold do you count R and P? Sweep thresholds? Fixed?
4. How to tag permuted outputs so consensus engine can distinguish real vs permuted
5. The current condition output format — need to document the exact JSON structure
