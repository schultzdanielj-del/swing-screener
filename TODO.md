# TODO — REWRITE (2026-02-26)

## What Went Wrong

The system was built with a critical flaw: **Step 4.5 "Strip Bespoke System"** ripped out all setup-specific LSP integration from the grinders and expression pipeline, replacing it with a "universal expression library only" approach. This means:

1. **Signal grinder has no LSP awareness.** It finds "vaguely extended stocks near highs" — not stocks approaching their LSP and failing. Zero calls to `set_lsp_context()`, zero LSP expressions in the grind.

2. **All grinders had computation parity issues.** Different ADX implementations (Wilder vs EMA smoothing), different measurement reference points (entry bar vs signal bar), no shared computation path. Fixed in today's session — all grinders now route through `ExpressionEngine` → `profiling_engine`.

3. **LSP AVWAP — the single strongest DTSS filter — was never integrated.** 100% of DTSS winners break below the LSP AVWAP. This is not a filter, it's the defining characteristic of the setup. The `avwap_lsp_distance` expression exists in `expression_engine.py` but nothing calls it.

4. **LSP data file was deleted** in a "cleanup" commit (`cdad877`). Restored today from `cae0755`.

5. **LSP detector** (`scripts/lsp_detector.py`, 459 lines) was built and validated but never integrated into any grinder. ~~Current accuracy: 78% exact match on 23 examples (4 misses: AAOI, BRK-B, SMMT, VUZI).~~ **UPDATE 2026-02-26: Now 23/23.** See "Data fixes" below.

### Data fixes (2026-02-26 session):
- **AAOI LSP relabeled:** Was Jan 10 @ $22.85 (labeling error — that pivot was broken by $0.01). Corrected to Dec 19 @ $24.08 (highest unbroken pivot high). Detector already picked this correctly.
- **BRKB:** Was listed as `BRK-B` — ticker naming mismatch. yfinance uses `BRK-B`, TC2000/DB uses `BRKB`. Renamed in LSP data. Fetched 1,545 OHLCV rows into Railway DB via new `/api/universe/insert-ohlcv` endpoint.
- **SMMT:** Was never in the universe — never fetched. Fetched 1,545 OHLCV rows into Railway DB.
- **VUZI:** Already matched correctly — TODO was stale listing it as a miss.
- **Detector now: 23/23 as #1 result on all labeled examples.**

### What exists but is disconnected:
- `data/dtss_lsp_data.json` — 23 hand-labeled examples with LSP date + price ✅ (restored, corrected)
- `scripts/lsp_detector.py` — algorithmic LSP detection, **23/23 accuracy** ✅
- `expression_engine.py` — 8 LSP expressions built: `lsp_distance`, `lsp_bounce_recovery`, `lsp_right_peak_ratio`, `lsp_volume_ratio`, `avwap_lsp_distance`, `lsp_bars_back`, `lsp_prominence`, `lsp_pullback_depth` ✅
- `expression_engine.set_lsp_context()` — injection method exists ✅
- `backtest_conditions.compute_series()` — 88 ops, shared computation path ✅

### What's broken/missing:
- Signal grinder: no LSP expressions, no `set_lsp_context()` calls
- No grinder uses the LSP AVWAP as a condition
- Exit/outcome grinders: computation parity fixed today but need re-run
- Exit grinder: doesn't validate that exit doesn't fire during formation period (before entry date)

---

## The Fix — Ordered Task List

### Task 0: Rewrite LSP Detector — ✅ COMPLETE (2026-02-27)

**Built:** `scripts/lsp_detector_v2.py` (1,004 lines) — full rewrite per EXPRESSION_ENGINE_V2.md Task A.

**What changed from v1:**
- Accepts DataFrames only (no API calls) — designed for cache builder integration
- Detects BOTH pivot highs AND lows (v1 was highs only)
- Multi-timeframe: daily + weekly + monthly pivots detected and merged
- Returns ALL pivots clustered into proximity-ordered levels (not opinionated about "the" LSP)
- Precomputed cumulative break arrays for O(1) break count at any bar
- Produces 80 expression series via `compute_all_lsp_series()`:
  - 70 level metrics (7 metrics × 5 ranks × 2 directions above/below)
  - 10 contextual AVWAP distances (1 × 5 ranks × 2 directions)
- Performance: ~0.5s/ticker (1,260 bars), ~4 min for full universe on 8 cores
- Old `lsp_detector.py` preserved (still used by validation scripts)

**Still needs:** Validation against real 5yr cache on Dan's machine (tested with synthetic data in sandbox). Run `python scripts/lsp_detector_v2.py validate` with the real cache.

**Next:** Tasks B+C from EXPRESSION_ENGINE_V2.md (register expressions in brute_expressions.py + HTF resampling in cache builder).

### Task 1: Integrate LSP into Signal Grinder
**What:** The pyramid grinder must use LSP data when grinding DTSS.
**How:**
- For examples: use hand-labeled LSP from `dtss_lsp_data.json`
- For universe tickers: use `LSPDetector` to find LSP on each signal date
- Call `set_lsp_context()` on ExpressionEngine before computing expressions
- Add LSP expressions to the expression library for DTSS grinds
- The grinder stays universal in architecture but accepts setup-specific data injection
**Key insight:** The LSP expressions should NOT be optional extras. For DTSS, `avwap_lsp_distance` (close below LSP AVWAP) is the defining condition. The grinder should discover this automatically if given the right expressions.

### Task 2: LSP AVWAP as Core DTSS Condition
**What:** Validate that "close below optimized LSP AVWAP" passes 23/23 examples.
**How:**
- For each example: anchor AVWAP at LSP bar (±3 bars, pick highest AVWAP on signal day)
- Check: signal day close < LSP AVWAP?
- This should be 23/23 by definition — if the DTSS worked, price broke below the LSP AVWAP
**Why separate from Task 1:** This validates the concept before grinding. If any examples fail, we have an LSP labeling problem, not a grinder problem.

### Task 3: Re-run Signal Grinder with LSP
**What:** Full pyramid grind for DTSS with LSP expressions included.
**Expect:** Much tighter conditions. The grinder should lock onto LSP-related expressions early because they're the strongest discriminators.
**Validation:** All 23 examples must pass. Signal count should be lower than current 576 (which was found WITHOUT any LSP awareness).

### Task 4: Re-run Exit Grinder with Formation Period Validation
**What:** Exit conditions must NOT fire before the entry date.
**Problem found today:** CELH earliest signal bar (2024-05-13) had exit trigger on bar 3 (2024-05-16), but entry wasn't until 2024-05-22. Exit fired during formation period. 19/20 examples failed when measured from earliest signal bar.
**Fix:** Exit grinder must test from EVERY signal bar (not just last one before entry), and exit must not trigger before entry date on any of them.

### Task 5: Re-run Outcome Grinder with Fixed Measurements
**What:** Measure from signal bar close (not entry bar), use ExpressionEngine for all computations.
**Already partially fixed today:** Outcome grinder now uses ExpressionEngine, measures from signal bar close, auto-computes ADR/MFE floors.
**Needs:** Re-run with corrected exit conditions from Task 4.

### Task 6: Steps 8-9 (Pre-Signal Refinement + Environment Clustering)
**What:** These steps are unchanged in concept but need the corrected inputs from Tasks 3-5.

---

## What NOT to Touch

- Nightly pipeline — working, don't break it
- Expression series cache — working, 21 GB, don't rebuild unless expressions change
- Frontend/ScanPerfect — working
- Railway DB — working
- Matrix builder — working (but will need LSP-aware rebuild for DTSS)

---

## Files That Matter

| File | Status | Purpose |
|------|--------|---------|
| `data/dtss_lsp_data.json` | ✅ Corrected (23 examples, AAOI relabeled, BRKB renamed) | Hand-labeled LSP dates + prices |
| `scripts/lsp_detector.py` | ✅ 23/23 accuracy, superseded by v2 | Old algorithmic LSP detection (API-based) |
| `scripts/lsp_detector_v2.py` | ✅ NEW (2026-02-27) — needs real-data validation | V2: DataFrame-based, multi-TF, 80 expressions, cache-builder ready |
| `scripts/expression_engine.py` | ✅ Has LSP ops, unused | 8 LSP expressions + `set_lsp_context()` |
| `scripts/backtest_conditions.py` | ✅ 88 ops, parity | Shared computation path |
| `server.py` | ✅ New `/api/universe/insert-ohlcv` endpoint added | Railway FastAPI backend |
| `local_runner/pyramid_grinder.py` | ❌ No LSP | Needs LSP injection (after cache integration) |
| `local_runner/matrix_builder.py` | ❌ No LSP | Needs LSP-aware example matrix |
| `local_runner/brute_expressions.py` | ❌ No LSP expressions | Needs 80 LSP expressions registered (Task B) |
| `local_runner/expr_cache_builder.py` | ❌ No LSP/HTF | Needs LSP + HTF integration (Task E) |
| `scripts/exit_grinder.py` | ⚠️ Parity fixed, needs re-run | Formation period validation missing |
| `scripts/outcome_grinder.py` | ⚠️ Parity fixed, needs re-run | Needs corrected exit conditions |
| `EXPRESSION_ENGINE_V2.md` | ✅ Updated — Task A complete, B-G pending | V2 build plan + next steps |

---

## Rules (unchanged)

1. NEVER proceed without explicit go-ahead
2. All grinders: ExpressionEngine → profiling_engine for ALL computations
3. All grinders: 100% of examples must pass. No exceptions.
4. Push all work to GitHub before ending chat
5. Break work into small tasks
6. NEVER use yfinance — all OHLCV from Railway DB or local caches
7. Read ta_knowledge.md before any TA work
