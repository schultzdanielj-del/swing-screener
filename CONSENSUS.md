# CONSENSUS.md

## Status

The full consensus pipeline spec lives in **`SIGNAL_GRINDER.md`** — see the "FULL PIPELINE OVERVIEW", "PROPOSED CHANGES", and "BUILD INCREMENTS" sections.

## What This Is

Multi-run consensus for the signal grinder. The beam search is non-deterministic — different runs find different condition sets. Running N times with 50% universe subsampling and keeping only conditions that appear consistently (Meinshausen & Bühlmann 2010 stability selection) produces a stable, overfitting-resistant signal condition set.

A permutation test (15 real + 15 permuted runs) provides the noise floor. z > 3 required to proceed.

## Current State

Not yet built. This is the next major infrastructure task after HTF cache (Expression Engine V2 Step 1) and incremental append (Step 2) are complete.

## Reference

- Full spec, architecture, and build increments: `SIGNAL_GRINDER.md`
- Refinement consensus (runs in the same pipeline): `REFINEMENT_GRINDER.md`
- Orchestrator: `scripts/run_consensus_pipeline.py` (not yet built)
- Consensus engine: `scripts/consensus_engine.py` (needs full rewrite)
