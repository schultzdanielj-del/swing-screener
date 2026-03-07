# TODO — Swing Screener (2026-03-07)

## DIRECTION: V2 ONLY — No more V1 patching

**Decision (2026-03-06):** Stop patching V1. Build V2 properly. All state lives in Railway SQLite with cycle versioning. No file-based handoffs.

**V2 design docs:**
- `PIPELINE_V2.md` — full pipeline design
- `DATA_CONTRACT.md` — V2 schema

---

## Current State

**71 DTSS examples.** Signal density confirmed: 281 filtered signals over 5yr (~1/week forward cadence).

**Signal density analysis (2026-03-06):**
- 946 raw → 612 deduped → 496 with exit → 281 filtered (≥2.9 ADR)
- Example floor: 3.2 ADR, median: 5.8 ADR — strong payoff profile
- Exit triggers on 81% of deduped signals
- ~1 actionable signal/week in forward scan

**Expr cache stale:** Last full rebuild predates the 9 new examples added before grind #5. 35/71 examples verified in signal_filter. **Fix: rebuild expr cache before next grind.**

---

## V2 Build Status

### ✅ Done
1. `DATA_CONTRACT.md` — V2 schema defined and committed
2. V2 DB tables wired into `server.py init_db()` — all tables, deploys on push
3. `scripts/grind_upload.py` — uploads grinder JSON to Railway as versioned cycle
4. `scripts/scan_signals.py` — reads/writes DB only, replaces signal_filter.py
5. `scripts/cycle_health.py` — computes health metrics, uploads, auto-promotes
6. **V2 server.py rewrite** (2026-03-07) — V1 cruft removed, ~910 lines. All needed endpoints kept: pipeline/agent, examples CRUD, vetting, pending/AI review, universe OHLCV insert, all V2 cycle endpoints
7. **V2 index.html rewrite** (2026-03-07) — fresh shell, new nav. V1 vetting UI copied verbatim: VettingChart, VettingPage (Step 2/4 toggle, keyboard shortcuts, log streaming), ExamplesPage (Pending/AI tab, approve-all, approve/reject). V1 originals archived at `archive/v1/`.

### ✅ V2 UI completion
- CycleHealthPage fully wired — health panel, cycle diff, revert button, all live
- WatchlistPage placeholder — blocked on Market Grinder

### 🔲 Next: Market Grinder (Step 5)
- V2-native, built on `cycle_signals` table
- Design TBD

---

## PRE-RUN CHECKLIST — Do these before first V2 run

### ONE-TIME SETUP (in order)

**1. Deploy V2 server to Railway**
- Push `v2` branch → Railway auto-deploys
- Verify: `GET /api/v2/cycles/dtss` returns `{"cycles": []}` (not 404)
- Verify: `GET /api/pipeline/steps` returns steps array (not 500)

**2. Rebuild expr cache (stale)**
```
python local_runner/expr_cache_builder.py --build --force
```
- Required before `scan_signals.py` — 36/71 examples have stale cache data
- Run overnight. `EXPR_CACHE_WORKERS=8`

**3. Seed exit condition for DTSS**
```
POST /api/v2/exit_conditions
{
  "setup_type": "dtss",
  "expression_name": "slope_xavgc21_off7_adr14",
  "direction": "below",
  "threshold": -1.128826,
  "max_forward_bars": 120,
  "adr_threshold_multiplier": 1.0
}
```

**4. Run grind_upload.py**
```
python scripts/grind_upload.py --setup dtss
```
- ⚠ Use `local_runner/cache/pyramid_results_dtss.json` (grind #5, 94 conditions, 71 examples)
- Do NOT use `data/pyramid_results_dtss.json` (grind #3, stale)

**5. Run scan_signals.py**
```
python scripts/scan_signals.py --setup dtss
```
~10-15 min, needs steps 2-4

**6. Run cycle_health.py**
```
python scripts/cycle_health.py --setup dtss
```

---

## Grind History (DTSS)

| Grind | Date | Examples | Conditions | Signals (5yr) | Notes |
|-------|------|----------|------------|---------------|-------|
| 1 | ~2026-02-24 | 20 | 41 | 264 | First production grind |
| 2 | ~2026-03-01 | 35 | 53 | 91 | After first vetting pass |
| 3 | ~2026-03-02 | 48 | ~76 | ~200 | After second vetting pass |
| 4 | 2026-03-03 | 62 | 86 | 168 | After AI vetting |
| 4b-blackout | 2026-03-05 | 62 | 87 | 164 | Blackout re-grind |
| 5 | 2026-03-06 | 71 | 94 | 281 filtered | signal_filter confirmed |

---

## Exit Grinder Results (locked)

| Expression | Floor | Median | Avg Bars |
|-----------|-------|--------|----------|
| `slope_xavgc21_off7_adr14 <= -1.128826` | 3.2 ADR | 5.8 ADR | 38 bars |

---

## The Math

- Losers: solidly under 1 ADR
- Winners: median 5.8 ADR, floor 3.2 ADR
- Even 36% win rate → positive expectancy
- 50% win rate (after market grinder) → massive profit factor
- 10% position size, 25% net winner/month = 2.5% compounding
- 20 years of 2.5%/month = mid 8 figures

---

## Data

- Expression cache: 4,119 tickers × 15,805 expressions (~50 GB) — **stale, needs rebuild**
- Railway DB: 11M+ OHLCV rows, ~4,167 tickers
- 71 DTSS examples (68 in cache, 3 excluded: BRK-B, SMMT, VUZI)

---

## Rules

1. NEVER proceed without explicit go-ahead
2. All grinders: 100% example pass. No exceptions.
3. Push all work to GitHub before ending chat
4. Break work into small tasks
5. All OHLCV from Railway DB or local caches. Never yfinance in pipelines.
6. Read ta_knowledge.md before any TA work
7. NEVER dump large data into context
8. Expression cache = single computation path
9. V2 only — no V1 patching

---

## Expression Library

- **V2 complete:** 15,805 total expressions
  - 4,017 daily + 80 LSP + 4,017 weekly + 4,017 monthly
  - 3,630 extension_structure (50 SMA + 200 SMA extension series as standalone price charts)
- Cache rebuild required after extension_structure addition (in progress)
- `EXPR_CACHE_WORKERS=8` for builds
