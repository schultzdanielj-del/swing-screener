# TODO — Swing Screener (2026-03-08)

## V2 Pipeline — The Correct Order

See `PIPELINE_V2.md` for full spec.

**Nightly auto-refresh (4:30pm ET):** OHLCV → caches → expr cache → matrix → earnings → market cache (266 instruments). Fully automated.

**The Vetting Loop (repeat until convergence):**
1. Signal Grind — examples vs universe → conditions
2. Exit Grind — optimal exit condition from example entry bar highs
3. Scan — apply conditions to 5yr → deduped signals + exit filter → classified signal set
4. Refinement Grind — (examples + exit-triggered) vs no-exit, blackout. Manual gate.
5. Vet — review winner pile (source toggle: step 3 or step 4). YES → AI review → approve → examples → loop.

**After Convergence (run once, in order):**
6. Proximity Grind — trim leftward/early signal bars from lose pile. Only safe post-convergence.
7. Regime Model — winner/loser ratio vs 266 market instruments (runs on proximity-trimmed set)
8. Health Check — cycle quality, EV, promote/revert

**Convergence:** Full vetting pass produces no new examples.

**Refinement gate:** Discretionary — skip step 5 in early cycles with few examples. Enable once example library is large enough for stable refinement conditions. Threshold TBD after 2-3 setup types built.

---

## Current State — DTSS

**72 examples. Converged.** Last vetting pass produced near-zero new examples.

**Proven math:**
- 41% win rate, EV 1.479 (from cycle_health on 68/68 examples)
- Winners: median 5.8 ADR, floor 3.2 ADR
- Losers: under 1 ADR
- Market regime model: win rate lift from 8% (worst decile) to 75% (best decile)
- 1111 signals, 456 wins, 50 independent regime features

**What's done:**
- Grind #5: 71 examples, 94 conditions, 281 filtered signals
- Exit condition: `slope_xavgc21_off7_adr14 <= -1.128826` (median 5.8 ADR capture)
- Market grinder complete: 245 instruments, 50 features, regime scores uploaded
- V2 server deployed on Railway (v2 branch)
- V2 UI: 4 tabs (Pipeline, Examples, Vetting, Watchlist), 8-step sidebar

**What's done (2026-03-08):**
- Refinement grind (blackout): 89 conditions, 68/68 pass, 132 deduped → 42 final signals
- Regime model re-run: 50 features, D1=8% → D9=75% win rate lift
- Health check: PROMOTE, EV 1.479, 41% win rate

**What's next for DTSS:**
1. Build proximity grinder (`scripts/proximity_grinder.py`)
2. Run proximity grind on current DTSS cycle
3. Re-run regime model on proximity-trimmed signal set
4. Cycle health redesign (see below)
5. Nightly scan + watchlist → go live

### Proximity Grind (new pipeline step, post-refinement)

**Purpose:** Trim false/early signal bars without losing any real triggers. Massive win rate impact — every loser removed is pure EV gain.

**Win pile (keep all):**
- Deduped winner signals from refinement grind output
- For each example: the signal bar closest to entry date (within ±5 days)

**Lose pile (try to trim):**
- Signal bars that are duplicates to the LEFT of the closest-to-entry signal bar on examples
- Loser signals from refinement grind output (no exit triggered)

**Hard constraint:** Cannot trim ANY rightmost win pile signals OR the signal bar closest to entry on any example.

**What it finds:** Conditions visible on the signal bar that distinguish "setup completing" from "setup in progress." Not a time machine — the bar right before entry has structural differences (momentum exhaustion, volume confirmation, resistance proximity) vs earlier duplicate bars where conditions happened to fire early.

**Math:** With 41% WR and 5.48/1.0 ADR winner/loser, trimming 5 losers without touching winners moves EV from ~1.66 to ~1.98 (realistic loser cap). Profit factor goes from 3.81 to 4.67.

### Cycle Health Redesign

**Current health check is wrong.** 100% example pass is a build rule not a health metric. Median loser ADR threshold is a model artifact (real losses are capped much lower by stop management). EV is a setup property, not a cycle metric.

**New health check — four metrics:**
1. **Convergence rate** — examples added last vetting pass as % of total examples. Near zero = scan is catching everything. Primary "reliable catch" metric.
2. **Signal density** — avg/day and peak/day in practical range for human stalking during entry window.
3. **Signal stability** — % overlap with previous cycle's signal set. High = robust conditions, low = regression.
4. **Regime lift** — D1 vs D10 win rate spread. Strong lift = signals mean something. Flat = noise.

**Live-ready = human judgment informed by these four numbers, not a mechanical threshold gate.**

---

## V2 Build Status

### ✅ Done
- V2 server.py on v2 branch — all endpoints
- V2 UI: Pipeline (8 steps), Examples (pending queue + validated), Vetting (source toggle step 4/5, keyboard shortcuts), Watchlist (placeholder)
- Pipeline agent wiring — run/stop/logs from UI
- AI vetting flow: YES → pending → review_samples.py (Claude CLI) → approve/reject in Examples tab
- /api/chart/{setup}/{ticker}/{date} — chart PNG for AI review (universe_ohlcv first, yfinance fallback)
- Nightly refresh includes market cache append (266 instruments)
- Market grinder complete + uploaded
- Cycle health + versioning system
- DB tables: cycles, cycle_signals, cycle_conditions, health_metrics, regime models

### ✅ Agent step mapping wired (2026-03-07)
- `pyramid_grinder.py` → step signal_grind (beam=10000 depth=100 peak=3)
- `exit_grinder.py` → step exit_grind
- `signal_filter.py` → step scan (scan + exit filter in one pass; exit_filter step removed)
- `pyramid_grinder.py --blackout` + `setup_refiner.py` → step refinement_grind
- `market_grinder.py` → step regime
- `cycle_health.py` → step health
- vet → is_manual, no agent command (UI-only)

### ✅ Grind storage V2 (2026-03-08) — BUG-003 fixed
- `grind_uploader.py` — transactional upload built into pyramid_grinder.py
- Every grind writes timestamped local JSON + uploads to Railway in same function call
- 5-point defense: retry+pending queue, schema validation, partial upload protection, read-back verification, SHA-256 hash
- `PATCH /api/v2/cycles/{id}` endpoint + step_type/grind_params/source_hash columns
- `GET /api/v2/cycles/{setup}?step_type=` filter param
- Pending uploads retried on agent startup and before each new upload
- V1 cleanup: deleted grind_storage.py, migrate_grinds.py, latest pointer writes, compat file writes
- Reference doc: `GRIND_STORAGE.md`
- 16 tests covering all failure modes (tests/test_grind_uploader.py)

### 🔲 Not yet built
- **Pipeline Integrity Audit** — CRITICAL. Manual code review of the 6 critical path scripts. The code can produce plausible wrong numbers without erroring — the only defense is human eyes on the data flow. For each script: what does it read, what does it produce, does the scope match what PIPELINE_V2.md says it should be doing. Scripts to review: `pyramid_grinder.py`, `setup_refiner.py`, `signal_filter.py`, `profit_grinder.py`, `market_grinder.py`, `cycle_health.py`. Also add inline assertions at each junction: scan steps assert ticker count matches tradable universe, downstream steps assert signal counts match upstream output, classification labels are never re-derived.
- **Setup Dashboard** — per-setup "home" screen showing overall status at a glance:
  - Convergence status, example count, final condition count
  - Win rate, EV, median winner/loser ADR
  - Full condition list (collapsible)
  - Regime model summary: top predictive features, current regime score, win rate by decile
  - Grind history table
- **Regime Visual on SPY chart** — SPY chart with all 50 regime features clustered visually:
  - Overlay winner/loser signal dates on SPY
  - Show regime indicator values at each signal date
  - Cluster/heatmap view of which regime features are active
- Watchlist page (needs nightly scan output)
- Nightly live scan (apply current conditions to today's bars)

---

## Grind History (DTSS)

| Grind | Date | Examples | Conditions | Signals | Notes |
|-------|------|----------|------------|---------|-------|
| 1 | ~2026-02-24 | 20 | 41 | 264 | First grind |
| 2 | ~2026-03-01 | 35 | 53 | 91 | After first vet |
| 3 | ~2026-03-02 | 48 | ~76 | ~200 | After second vet |
| 4 | 2026-03-03 | 62 | 86 | 168 | After AI vetting |
| 4b | 2026-03-05 | 62 | 87 | 164 | Blackout re-grind |
| 5 | 2026-03-06 | 71 | 94 | 281 | Current |
| 5b | 2026-03-08 | 68 | 89 | 42 (refined) | Blackout refinement grind |

---

## Data

- Expression library: 15,805 expressions
- Expression cache: 4,119 tickers × 16,051 expr (64.6 GB) — current as of 2026-03-06
- Market cache: 245/266 instruments × 15,805 expr (3.96 GB)
- Railway DB: 11M+ OHLCV rows, ~4,167 tickers
- 72 DTSS examples (69 in cache, 3 excluded: BRK-B, SMMT, VUZI)

---

## Rules

1. NEVER proceed without explicit go-ahead
2. All grinders: 100% example pass. No exceptions.
3. Push all work to GitHub before ending chat (v2 branch)
4. Break work into small tasks
5. All OHLCV from Railway DB or local caches. Never yfinance in pipelines.
6. Read ta_knowledge.md before any TA work
7. NEVER dump large data into context
8. Expression cache = single computation path
9. V2 only — no V1 patching
10. Read PIPELINE_V2.md before touching any grinder or pipeline_agent.py. No exceptions.
