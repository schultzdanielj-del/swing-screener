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

**Next:** Task F from EXPRESSION_ENGINE_V2.md (matrix builder verification with 12,175 expressions), then full cache rebuild on Dan's machine.

### Task 1: Expression Engine V2 — ✅ COMPLETE (2026-02-27)
**Built per EXPRESSION_ENGINE_V2.md.** Replaced Tasks 1-3 with a generic approach:
- `lsp_detector_v2.py`: Detects ALL pivot levels, generates 80 precomputed LSP expressions per ticker
- `brute_expressions.py`: Expanded from 4,017 → 12,175 expressions (daily + LSP + algo + weekly + monthly)
- `expr_cache_builder.py`: Updated to compute all 12,175 expressions per ticker (HTF resampling, LSP detection, algo line detection)
- Full cache built: 4,119 tickers × 12,175 expressions = ~50 GB on disk
- See EXPRESSION_ENGINE_V2.md for full task breakdown (Tasks A-G all complete)

### Task 2: Matrix Builder + Grinder Integration — ✅ COMPLETE (2026-02-27)
**All grinders now use expr cache as single computation path:**
- `matrix_builder.py`: Loads universe matrix from expr cache (~51s, was ~30 min)
- `pyramid_grinder.py`: `compute_example_ranges()` + `validate_examples()` use expr cache
- Historical tiers already used expr cache — now D1 tier does too
- Spiderweb 70% example threshold fixed → 100% (was allowing conditions examples couldn't pass)
- Examples not in expr cache filtered out before any computation

### Task 3: Multi-Pass Pyramid Grinder — ✅ COMPLETE (2026-02-27)

**Latest result (with algo lines): 264 signals, peak 3/day, 41 conditions, 20/20 examples pass. 12.6 min runtime.**
- Saved: `pyramid_dtss_mp_sig264_pk3_20260228_163923.json`
- Previous (pre-algo): 339 signals, peak 3/day, 41 conditions.
- **22% fewer signals, same peak.** Algo line expression (`algo_lplus2_slope`) contributed at 5yr tier.
- 1 weekly HTF expression (`w_nr_h_maxh20_atr14`) also selected at 5yr.

**What was built:**
- `pyramid_grinder.py` multi-pass mode (default): 3 sequential passes
  - Pass 1 (Daily+LSP+Algo, 4,141 exprs): D1→5yr, locked 40 conditions
  - Pass 2 (Weekly, 4,017 exprs): 1mo→5yr, added 1 condition at 5yr tier
  - Pass 3 (Monthly, 4,017 exprs): 6mo→5yr, added 0 (already at target)
- `--single-pass` flag for legacy mode (all 12K expressions in one pass)
- All fallback computation paths removed — expr cache REQUIRED
- D1 tier filters full 12K matrix to pass-specific columns

**Key insight confirmed:** HTF expressions crowd out daily at D1. Multi-pass ensures daily gets first crack at every horizon. Weekly/monthly only contribute where daily hit a ceiling.

### Task 3.5: Algo Line Expressions — ✅ COMPLETE (2026-02-28)
**Built:** `scripts/algo_line_detector.py` — detects H- and L+ algo lines from high-volume D1 candles.

**What it produces:**
- Detects H- (downsloping from highs) and L+ (upsloping from lows) trendlines
- Origination from candles with V > 50-period SMA(V), strict wick-based violation checking
- 44 precomputed expressions per ticker:
  - 6 metrics × 3 ranks × 2 directions = 36 ranked (distance, touch_count, hivol_touch_count, slope, broken, retest_distance)
  - 4 shallowest metrics × 2 directions = 8 contextual (distance, slope, touch_count, avwap_convergence)
- Daily timeframe only — skips weekly/monthly grinder passes
- Touch tolerance: 0.3% (standard charting software snap)
- Minimum 2 touch points to qualify

**Integration:**
- `brute_expressions.py`: 12,175 expressions (was 12,131 + 44 algo)
- `expr_cache_builder.py`: Phase 2b between LSP and HTF, same worker pattern
- Grinder picks up new columns automatically after cache rebuild
- To revert: remove algo sections from brute_expressions.py + expr_cache_builder.py, rebuild cache

**Needs:** Full cache rebuild on Dan's machine to include algo line columns. ✅ DONE (2026-02-28)

### Task 3.6: Exit Grinder — Add LSP/Algo/AVWAP Expressions ✅ COMPLETE (2026-03-01)
**Goal:** Upgrade exit expression library with the same structural detection systems used in the signal grinder (LSP, algo lines, AVWAPs). No weekly/monthly pass — exit detection is daily-only.

**Final exit library:** 6,410 expressions (446 base + 5,964 boolean aggregations).

**Steps completed:**

#### Step 3.6a: LSP base expressions ✅ (already existed)
- 17 LSP base expressions: distance (above/below × rank 1-3), broken, congestion, nearest_unbroken
- Compute: `_get_lsp_levels()` lazy loader using `LSPDetectorV2` on pre-entry history
- Boolean conditions: 6 native (lsp_broken) + threshold booleans for distance/congestion

#### Step 3.6b: Algo line base expressions ✅ (2026-03-01)
- 20 algo_lines base expressions: distance, broken, touch_count (rank 1-3), shallowest distance/slope
- Compute: `_get_algo_lines()` lazy loader using `detect_algo_lines()` on pre-entry history
- Uses same `_get_active_lines_at_bar()`, `_find_shallowest_line()`, `_line_price_at_bar()` helpers as signal grinder
- Fixed `_find_base_expr_name()` for algo threshold boolean resolution

#### Step 3.6c: AVWAP base expressions ✅ (2026-03-01)
- 9 avwap base expressions: LSP-anchored AVWAP distance (above/below × rank 1-2), entry AVWAP distance, LSP AVWAP slope, LSP AVWAP crossed
- Compute: `_get_avwap_arrays()` lazy loader using `precompute_avwap_arrays()` + `avwap_from_anchor()` from lsp_detector_v2 — same computation path as signal grinder
- O(1) per forward bar after one-time precomputation

#### Step 3.6d: Entry-relative expressions ✅ (2026-03-01)
- 39 entry_relative base expressions: delta_from_entry (extension, RSI, ADX, DI spread, stoch, BB %B, MA distance) + ratio_to_entry (RVOL, BB bandwidth, LSP dist, algo dist, AVWAP dist)
- Compute: generic `delta_from_entry` and `ratio_to_entry` ops that delegate to any base expression, then subtract/divide by entry bar value
- 18 threshold booleans for RSI delta and extension delta

#### Step 3.6e: Run exit grinder with upgraded library ✅ (2026-03-01)
- Ran with 6,410 expressions, 20/20 examples pass, 0 expression failures
- Result: `avg_range_atr_10b above 1.0541` — 71% median capture eff, 64% avg
- **Key finding:** Single-condition exit hits a ceiling. All structural expressions (LSP, algo, AVWAP, entry-relative) compute successfully but cannot beat a simple volatility expansion rule as a standalone universal trigger across 20 different tickers. Multi-stage exit is needed to unlock the structural expressions' value.

**Additional fixes during this task:**
- Hardcoded 100% example pass in exit grinder (removed `--min-trigger-pct` parameter)
- Added timestamped + latest save pattern (matches signal grinder)
- Fixed `obv_slope` offset param alias
- Fixed `_find_base_expr_name` for algo + AVWAP + entry-relative mappings

### Task 3.7: Multi-Stage Exit Grinder ✅ COMPLETE (2026-03-01)
**Result:** Multi-stage did not beat single-stage for DTSS. Single-stage winner holds: `bars_since_reclaim_xavgc8 >= 18` (43% floor capture, 72% median). Multi-stage may help other setups. Script preserved at `scripts/multistage_exit_grinder.py`.

### Task 4: Signal Filter + Vetting Pipeline 🔧 IN PROGRESS (2026-03-02)
**Replaces old Tasks 4-5.** The old formation period / outcome grinder tasks are deprioritized. The real next step is expanding the example set through a vetting loop.

**Architectural fix (2026-03-02): Two types of exit grinds.**
The original exit grinder (`exit_grinder.py`) uses entry-relative expressions (e.g. `bars_since_reclaim_xavgc8 >= 18`) that require the entry bar as an anchor. These can't be precomputed into the expression cache. The signal filter needs cache-compatible exits. Solution: two separate exit grinds for two separate purposes.

1. **Signal exit grind** (`scripts/signal_exit_grinder.py`) — NEW:
   - Uses ONLY expressions in the expression cache (same 12,175+ as signal grinder)
   - Runs forward from DEDUPLICATED SIGNAL BAR (scan candle = entry - 1)
   - 100% example pass rate hardcoded
   - Output: `data/signal_exit_grind/signal_exit_{setup}.json`
   - Purpose: filter backtest signals for chart vetting ("did this signal produce a move?")
   - Same computation path as signal grinder — no grinder rule violations

2. **Trade exit grind** (`scripts/exit_grinder.py`) — SHELVED:
   - Uses 6,410 expressions from `exit_expressions.py` (many entry-relative)
   - Runs forward from ENTRY BAR
   - Uses ExitExprEngine (separate computation path — intentionally different)
   - Winner: `bars_since_reclaim_xavgc8 >= 18` (43% floor capture, 72% median)
   - Purpose: live trade management ("when to cover/sell")
   - Shelved until setup library : signal : conditions ratio is much stronger

**Built:**
- `scripts/signal_exit_grinder.py` — cache-compatible exit grinder:
  - Resolves example signal bars (scan candle) with full condition verification
  - Builds forward matrices directly from expression cache
  - Grinds all cache expressions × thresholds × directions
  - 100% example pass hardcoded, same MFE/capture efficiency scoring
  - Output format compatible with signal_filter.py
- `scripts/signal_filter.py` — 7-phase pipeline:
  1. Dedup example signal bars (verify all conditions pass via expr cache)
  2. Measure example exit distances (rightmost signal close → exit close in ADR)
  3. Scan all 5yr signals (parallel, all cores, expression cache)
  4. Dedup backtest signals (consecutive → rightmost)
  5. Apply exit condition, measure signal close → exit close in ADR
  6. Exclude existing examples from results
  7. Filter: keep only signals ≥ example floor ADR, rank descending
- `signal_filter.py` now loads exit from `data/signal_exit_grind/` (cache-compatible)
- Output: `data/signal_filter/filtered_dtss.json` — ranked signals for chart vetting
- **Single computation path enforced:** ALL signal conditions AND exit conditions read from expression cache.

**Pipeline Dashboard (remote):**
- `app/pipeline.html` — served from Railway, accessible from anywhere
- Pipeline agent integrated into `local_runner/agent.py` — polls both grinder and pipeline job queues
- 13 new `/api/pipeline/*` endpoints in `server.py`
- Architecture: Railway queues jobs → desktop agent polls → runs subprocess → streams logs back

**⚠️ NEXT STEPS (run on desktop):**
```
git pull
# 1. Run signal exit grinder (discovers cache-compatible exit)
python scripts/signal_exit_grinder.py --setup dtss
# 2. Run signal filter (uses signal exit result)
python scripts/signal_filter.py --setup dtss
# 3. Begin chart vetting from ranked output
```

**The vetting loop (after filter works):**
1. Run signal filter → get ranked signals with exit distance
2. Flip through charts (top-ranked first = most obvious winners)
3. Tag winners as new examples (real DTSS + catchable entry + it worked)
4. Re-grind with expanded example set → conditions tighten
5. Repeat until convergence (no new examples to add)

**Still needs:**
- Chart vetting UI in ScanPerfect (thumbs up/skip per signal, feeds back to examples)
- Re-grind trigger from dashboard after examples added

### Task 5: Market Regime Filter ⬜
**What:** Correlate signal outcomes with market conditions. Which market environments produce winners vs losers? This is the "when to trade it" filter.
**Blocked by:** Needs enough vetted signals to have meaningful win/loss data.

### Task 6: Live Scan + Watchlist ⬜
**What:** Nightly scan → apply conditions to today's bars → rank by expected value → output watchlist.
**Blocked by:** Task 5 (need regime filter for EV calculation).

---

## What NOT to Touch

- Nightly pipeline — working, don't break it
- Expression series cache — **needs rebuild** (`--build --force`) to include exit expressions. Currently 12,175 exprs, will grow by ~200+ generic exit exprs.
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
| `scripts/backtest_conditions.py` | ✅ 115 ops (88 signal + 27 exit), parity | Shared computation path — ALL grinders route through this |
| `scripts/signal_filter.py` | 🔧 Blocked on cache rebuild | Dedup + exit + rank for chart vetting. Uses expr cache only. No fallbacks. |
| `scripts/multistage_exit_grinder.py` | ✅ COMPLETE — did not beat single-stage for DTSS | 8-pass multi-stage exit search, preserved for other setups |
| `server.py` | ✅ Pipeline endpoints added (2026-03-01) | Railway FastAPI backend + 13 `/api/pipeline/*` endpoints |
| `app/pipeline.html` | ✅ NEW (2026-03-01) | Remote pipeline dashboard served from Railway |
| `local_runner/agent.py` | ✅ Updated: polls both grinder + pipeline job queues (2026-03-02) | Desktop agent — handles grinder jobs AND pipeline step execution |
| `local_runner/pyramid_grinder.py` | ✅ Production | Multi-pass pyramid grinder, expr cache required |
| `local_runner/matrix_builder.py` | ✅ Production | Loads universe matrix from expr cache (~51s) |
| `local_runner/brute_expressions.py` | ✅ 12,175 expressions (4,017 daily + 80 LSP + 44 algo + 8,034 HTF) | Expression library with HTF + algo auto-generation |
| `local_runner/expr_cache_builder.py` | ✅ Updated: now includes generic exit expressions (2026-03-02) | 3-phase worker: daily + LSP + HTF. _load_expressions() merges signal + exit libs. **Needs --force rebuild.** |
| `scripts/exit_grinder.py` | ✅ 6,410 expressions, 100% pass hardcoded — SHELVED | Trade management exit — entry-relative, uses ExitExprEngine. Shelved until example library stronger. |
| `scripts/signal_exit_grinder.py` | ✅ NEW (2026-03-02) — needs first run on desktop | Signal filtering exit — cache-compatible, uses expression cache only. Same computation path as signal grinder. |
| `scripts/exit_expressions.py` | ✅ 446 base + 5,964 boolean aggs = 6,410 total | LSP + algo + AVWAP + entry-relative expressions (for trade exit grinder) |
| `scripts/exit_compute.py` | ✅ Full parity — LSP, algo, AVWAP, entry-relative ops | Post-signal expression compute engine |
| `scripts/outcome_grinder.py` | ⚠️ Deprioritized — vetting loop takes precedence | May revisit after example expansion |
| `EXPRESSION_ENGINE_V2.md` | ✅ Updated — Tasks A-E,G complete, F pending | V2 build plan + next steps |

---

## Rules (unchanged)

1. NEVER proceed without explicit go-ahead
2. All grinders: ExpressionEngine → profiling_engine for ALL computations
3. All grinders: 100% of examples must pass. No exceptions.
4. Push all work to GitHub before ending chat
5. Break work into small tasks
6. NEVER use yfinance — all OHLCV from Railway DB or local caches
7. Read ta_knowledge.md before any TA work
