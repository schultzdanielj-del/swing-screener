# Handoff: Expression Cache Builder — Parallelization

## Context

The swing-screener vectorized expression cache builder produces correct output but is too slow (11 hours single-threaded vs 4.5 hours for the old per-ticker pandas builder). CPU utilization was 8% — the machine's 10 cores sat idle. The fix is parallelizing the expression computation loop.

## The Problem

`vectorized_cache_builder.py` computes 15,805 expressions sequentially. Each call to `compute_expr_2d()` costs ~18ms in Python function dispatch + numpy array allocation. With 422 output batches of 25 tickers, that's 6.7M sequential Python calls. The numpy math is trivial — Python interpreter overhead is the bottleneck.

## What Exists and Works

All expression computation code is **correct and tested** (5,215 daily/boolean/ext-struct expressions validated against pandas reference, 90/90 HTF base ops validated):

- `local_runner/vectorized_indicators.py` — 28 numpy 2D base indicator functions
- `local_runner/vectorized_dispatch.py` — `build_intermediates()` computes all shared intermediates (MAs, ATR, RSI, ADX, etc.) as 2D arrays. `compute_expr_2d(comp, im, O, H, L, C, V)` computes one expression for a batch of tickers. `_eval_bool_condition_2d()` evaluates boolean conditions.
- `local_runner/vectorized_cache_builder.py` — the pipeline: loads OHLCV pkl → builds 2D matrices for all tickers → computes intermediates once globally → processes expression computation in output batches of 25 → writes per-ticker .npz files. Currently single-threaded on the expression loop.

## What Needs to Change

The expression computation loop in `build_vectorized()` needs to be parallelized. The current flow per output batch:

```
for j in groups["daily"]:                    # 1,604 sequential calls
    compute_expr_2d(expressions[j], im_b, ...)
for j in groups["ext_struct"]:               # 1,198 sequential calls  
    compute_expr_2d(expressions[j], im_b, ...)
# boolean conditions + agg ops               # 2,413 sequential calls
# HTF weekly                                 # 5,233 sequential calls
# HTF monthly                               # 5,233 sequential calls
```

Each call is independent — embarrassingly parallel.

## Constraints

- **RAM:** 32GB total. Intermediates for all 10,542 tickers consume ~20GB (174 arrays of 10,542 × 1,297 float64). Peak observed: 26.4GB (83%). Workers need read-only access to intermediates + ~109MB per expression output.
- **CPU:** i5-12600K, 10 cores (6P + 4E).
- **Windows:** The machine runs Windows. `fork()` is not available — `multiprocessing` uses `spawn` which copies data to child processes. Shared memory (`multiprocessing.shared_memory`) or memory-mapped files may be needed to avoid duplicating the 20GB intermediates dict.
- **Output format:** Per-ticker .npz files with `data` array (n_bars, 15805) float32 and `dates` array of strings. Must match existing format exactly.

## Target

Full rebuild in 1.5–2.5 hours (5–8x speedup from parallelization). The original 30-minute target was unrealistic — that would require eliminating Python dispatch overhead entirely (Numba/compiled code), which is a separate optimization.

## Key Files to Read First

- `UNIVERSE_EXPANSION.md` — full plan with Phase 2 section documenting what works and what failed
- `PIPELINE_V2.md` — always read before touching any grinder (Rule 10)
- `local_runner/vectorized_cache_builder.py` — the pipeline to parallelize
- `local_runner/vectorized_dispatch.py` — the expression computation functions

## How We Work Together

- Read `UNIVERSE_EXPANSION.md` first
- Sandbox test everything in this chat before pushing — Dan does not write or run test scripts
- Dan vets every change before go-ahead
- No code pushed without explicit "go/yes/do it"
- BENCHMARK AT SCALE before claiming performance improvements. The previous session made a 30-minute estimate that turned out to be 11 hours. Do not repeat this.

## Repo

- Repo: `schultzdanielj-del/swing-screener`, branch `v2`
- Git push auth: see memory / previous handoff docs

## Current Status

Expression cache builder produces correct output, needs parallelization for production speed. Backups exist from before this work started. The old `expr_cache_builder.py` still works as fallback (4.5 hours, 10 workers, per-ticker pandas).
