# CONSENSUS.md

Active status tracker for the multi-run consensus pipeline.

## What it is

Multi-run stability-selection consensus on top of the signal grinder. The beam search is non-deterministic: different runs find different condition sets. Running N grinds with 50% universe subsampling and keeping only conditions that appear consistently (Meinshausen & Bühlmann 2010 stability selection) produces a stable, overfitting-resistant signal condition set. A permutation test (15 real + 15 permuted runs) provides the noise floor. z > 3 required to proceed.

Full design lives in `SIGNAL_GRINDER.md`. Refinement-grind consensus lives in `REFINEMENT_GRINDER.md`. Orchestrator: `scripts/run_consensus_pipeline.py`. Consensus engine: `scripts/consensus_engine.py`.

## Status

Build complete through Increment 10. Entire pipeline is wired end-to-end.

- `test_consensus_pipeline.py --setup dtss` → **9/9 PASS** (most recent: 1169s / 19.5 min)
- Single-grind cost at current defaults (beam=500, depth=100, subsample=0.5) → **~11–13 min/grind** on BRKO benchmark (47 conditions found, all examples validated)
- Full pipeline estimate at current per-grind cost → **~8.5 hours** (30 signal grinds × 12.8 min + 10 refinement grinds × ~12 min)

Pipeline is overnight-feasible as-is. Session 6 is deferred for **quality-preserving** speed optimization work before committing to the real overnight run — see the separate next-session optimization prompt (printed at the end of the 2026-04-11 cleanup session, not a doc in this repo).

**Entry grinder is still deferred** pending planning input. Not part of this pipeline.

## Preparing for the first real overnight run

Before kicking off the real consensus run, the expression cache must be **complete**, not just fresh.

1. Run nightly refresh: `python local_runner/nightly.py`
2. **Full rebuild the expression cache**: `python local_runner/expr_cache_builder.py --build`
   - **Do NOT use `--append` for consensus prep.** The `--append` path uses the forward-propagation engine (`forward_prop_engine.py`), which is a best-effort fast path designed for the live forward-scanning watchlist. Any per-expression computation failure silently leaves that cell as NaN. A full rebuild uses `_compute_ticker_full`, the ground-truth path, which guarantees every expression is computed from the full OHLCV history for every bar.
   - Staleness of days or a week is acceptable for historical condition derivation. **Completeness is required.**
3. Run the consensus pipeline: `python scripts/run_consensus_pipeline.py --setup dtss`

## The iron rule for optimization work

**Optimizations must preserve the full 15,805-expression search space AND search semantics.**

The signal grinder's job is to search 15,805 expressions to find the best conditions for a setup. Any change that reduces the number of expressions searched, reduces precision of stored values, pre-screens candidates by a metric other than the search objective, or otherwise changes *what conditions the grinder can find* is **a quality cut, not an optimization**. A speedup obtained by making the search worse is not a speedup — it's running a worse search faster.

Before proposing any optimization that touches the expression cache, data format, worker pool, or search algorithm, ask: *does this preserve the full 15,805 search space AND the peak-minimizing search semantics?* If no, reject on sight.

## Rejected "optimizations"

These were previously listed as candidates. They are quality cuts disguised as speedups and are explicitly **off the table**. Listed here so future sessions don't rediscover and re-propose them.

- **Slim expression cache** — Pre-compute per-ticker `.npz` files containing only ~2,000 "useful" expressions instead of all 15,805. Rejected: directly reduces the search space the beam search can explore. Any expression not in the slim cache is permanently invisible to the grinder. That is a quality cut, not a speedup.
- **Matmul vectorization revival at beam=500** — Batches `(beam, candidate)` pair evaluation into a single SGEMM call. Rejected at current beam size: the matmul pre-screens by TOTAL joint count, while the search objective is PEAK joint count (minimize max signals on any single date). The pre-screen misses combinations that the exhaustive search would find. This is a different local optimum, not a faster path to the same answer. (Separate discussion warranted only if beam width is raised significantly, where it might open up broader beam node coverage.)
- **Float16 throughout the grinder** — Drop the `float16 → float32` cast on `.npz` load and run searches in float16. Rejected: introduces precision risk in `bincount` and accumulation operations inside `PeakSpiderweb._peak_score` and `_daily_stats`. Silent numerical drift in peak counts can change which conditions the search selects. Cannot be ruled safe without a proof of precision preservation, which would consume more engineering effort than the gain is worth.

## Candidate quality-preserving optimizations

Every item here preserves the full search space, full precision, and search semantics. None have been implemented; all are candidates for the next session's work.

- **LZ4 or zstd re-compression of the expression cache** — Replace zlib with a faster-decompressing codec (same float16 values, identical data, ~5× faster decompression for LZ4). Requires a one-time cache rebuild via `expr_cache_builder.py --build`. Ripples to any reader that assumes zlib (signal_filter, scan_engine). Projected: substantial reduction in per-grind CPU cost (which profile data on 2026-04-11 showed is the dominant bottleneck).
- **Persistent worker pool across tiers within a pass** — Currently each `run_historical_tier()` call spawns a fresh `ProcessPoolExecutor`, so each ticker's `.npz` is loaded and decompressed once per tier (12 times per grind). A persistent pool would load each ticker once per pass and reuse the decompressed data across that pass's tiers. Must keep the full 15,805 columns per ticker. Memory-blocked on 32 GB RAM in the naive implementation — requires design work (shared memory, memmap, or batched ticker assignment) to fit. Savings could be very large if it fits.
- **Cache `example_matrix` across passes within a grind** — Currently recomputed three times per grind (once per pass with a different expression filter). Compute once with the full expression set, slice per pass. Modest savings, simple and safe.
- **Shared memory for tier matrix results** — Workers currently `pickle`-serialize their tier-matrix contributions to send back to the parent. `multiprocessing.shared_memory` eliminates the round-trip. Modest savings per tier, low complexity.
- **Memory-map universe matrix** — Convert `universe_matrix.pkl` from pickle to `.npy` + `mmap` for lazy paging. Small savings, low risk if it doesn't touch the nightly path.
- **Batch grinds in a single subprocess** — Orchestrator currently spawns 40 separate Python processes (one per grind). Run multiple grinds in one interpreter to save subprocess startup cost × 40. Modest savings. Requires a clean state reset between grinds to avoid memory accumulation.

## Investigated and dismissed (not rejected on quality grounds, just not worth it)

- **Parallelizing grinds** — Running 2+ grinds in parallel, each with the full 15 workers. Dismissed: each grind is already CPU-bound at ~84% across 13–14 cores (2026-04-11 profile), so parallel grinds would oversubscribe and thrash without adding throughput. A variant with fewer workers per grind (e.g. 3 grinds × 5 workers = 15 total) was considered, but memory is the binding constraint: one grind currently peaks at ~16.5 GB, three in parallel would need ~49 GB on a 32 GB system. Viable only after per-grind memory footprint is reduced.
- **Heuristic "skip remaining tiers when a tier returns empty"** — Tempting because the 2026-04-11 BRKO profile showed Pass 2 and Pass 3 beam searches returning 0.00s after matrix builds consumed ~6 min per grind. Dismissed because different tiers use different windows (1wk vs 5yr) — a tier with more bars can find conditions that a tier with fewer bars missed, even with the same locked conditions. Skipping is not semantically safe as a general rule.
- **Heuristic "skip the 5yr tier"** — Too risky. 5yr adds conditions for some setups (BRKO Pass 1 added 10 from 5yr in benchmark runs).
- **Refinement blackout / whitelist date translation** — Investigated, not needed. `blackout_map` is never assigned; `whitelist_map` is consumed by `run_refinement`'s own loser-matrix builder which already uses cache-relative coordinates.

## Current uncommitted state

As of the 2026-04-11 doc-cleanup session, there is an uncommitted profiling patch to `local_runner/pyramid_grinder.py` that adds non-invasive wall-clock phase timers and optional `psutil` resource sampling, prints a TIMING SUMMARY block at the end of each grind, and made no logic changes. The patch is +315 / −114 lines and was validated by `test_consensus_pipeline.py --setup dtss` (9/9 PASS at 1169s). It is NOT in the git history. The next session should decide whether to commit it as permanent diagnostic tooling or revert it before new work begins.
