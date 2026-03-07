# TODO — Swing Screener (2026-03-06)

## DIRECTION: V2 ONLY — No more V1 patching

**Decision (2026-03-06):** Stop patching V1. Build V2 properly. The file collision bug (wrong exit condition loaded → 0 signals) is a symptom of the V1 architecture. V2 eliminates file-based handoffs entirely — all state lives in Railway SQLite with cycle versioning.

**V2 design docs:**
- `PIPELINE_V2.md` — full pipeline design
- `DATA_CONTRACT.md` — V2 schema (committed 2026-03-06)

---

## Current State

**71 DTSS examples.** Signal density confirmed: 281 filtered signals over 5yr (~1/week forward cadence). Workable.

**Signal density analysis (2026-03-06):**
- 946 raw → 612 deduped → 496 with exit → 281 filtered (≥2.9 ADR)
- Example floor: 3.2 ADR, median: 5.8 ADR — strong payoff profile
- Exit triggers on 81% of deduped signals — exit condition is doing real work
- ~1 actionable signal/week in forward scan

**Expr cache stale:** Last full rebuild predates the 9 new examples added before grind #5. 35/71 examples verified in signal_filter (the 36 that failed have stale cache data). **Fix: rebuild expr cache before next grind.**

---

## PRE-RUN CHECKLIST — Do these before first V2 run

### ONE-TIME SETUP (in order)

**1. Deploy V2 tables to Railway**
- Push `v2` branch — Railway auto-deploys, `init_db()` creates 7 new tables on startup
- Verify: `GET /api/v2/cycles/dtss` returns `{"cycles": []}` (empty, not 404)

**2. Rebuild expr cache (stale)**
```
python local_runner/expr_cache_builder.py --build --force
```
- Required before `scan_signals.py` — 36/71 examples have stale cache data
- Takes ~hours, run overnight. Set `EXPR_CACHE_WORKERS=8`.

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
- Required before `scan_signals.py`. No cache dependency — do this immediately.

**4. Run grind_upload.py**
```
python scripts/grind_upload.py --setup dtss
```
- Reads `cache/pyramid_results_dtss.json` (local grinder output from last run)
- Uploads grind_cycles + cycle_conditions rows, marks cycle current
- ⚠ WARNING: `data/pyramid_results_dtss.json` in repo = grind #3 (39 conditions, OLD).
  Grind #5 (94 conditions, 71 examples) is LOCAL ONLY at
  `local_runner/cache/pyramid_results_dtss.json`.
  grind_upload.py reads from `cache/` by default — confirm that file exists
  locally before running. Do NOT use the `data/` file.
- Prerequisites: steps 1 and 2 (tables live; grinder output file present locally)

**5. Run scan_signals.py**
```
python scripts/scan_signals.py --setup dtss
```
- Reads conditions from Railway (needs step 4)
- Reads exit condition from Railway (needs step 3)
- Scans 5yr history via expr cache (needs step 2)
- Writes cycle_signals rows to Railway (~10-15 min)

**6. Run cycle_health.py**
```
python scripts/cycle_health.py --setup dtss
```
- Reads everything from Railway (needs steps 4 and 5)
- Computes all health metrics, uploads cycle_health row
- Auto-promotes if recommend=promote; warns on flag; hard-stops on hard_reject
- Prints colored health report

---

### BEFORE NEXT GRIND (regrind with 71 examples)

1. Confirm expr cache is fresh (step 2 above)
2. Run pyramid_grinder.py as normal → output lands in `cache/pyramid_results_dtss.json`
3. Run steps 4 → 5 → 6

---

## V2 Build Order

### Done
1. `DATA_CONTRACT.md` — V2 schema defined and committed
2. V2 tables wired into `server.py init_db()` — all 7 tables, Railway deploys on push
3. `scripts/grind_upload.py` — uploads grinder JSON to Railway as versioned cycle
4. `scripts/scan_signals.py` — replaces signal_filter.py, reads/writes DB not files
5. `scripts/cycle_health.py` — computes health metrics, uploads, auto-promotes

### Next: V2 UI
Health panel, cycle diff, revert button, regrind indicator.

### Then: Market Grinder
V2-native, built on `cycle_signals` table.

---

## Grind History (DTSS)

| Grind | Date | Examples | Conditions | Signals (5yr) | Notes |
|-------|------|----------|------------|---------------|-------|
| 1 | ~2026-02-24 | 20 | 41 | 264 | First production grind |
| 2 | ~2026-03-01 | 35 | 53 | 91 | After first vetting pass |
| 3 | ~2026-03-02 | 48 | ~76 | ~200 | After second vetting pass |
| 4 | 2026-03-03 | 62 | 86 | 168 | After AI vetting |
| 4b-blackout | 2026-03-05 | 62 | 87 | 164 | Blackout re-grind |
| 5 | 2026-03-06 | 71 | 94 | 281 filtered | signal_filter run confirmed |

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

- Expression cache: 4,119 tickers x 12,421 expressions (~50 GB) — **stale, needs rebuild**
- Railway DB: 11M+ OHLCV rows, ~4,167 tickers
- 71 DTSS examples (68 in cache, 3 excluded: BRK-B, SMMT, VUZI)

---

## Rules

1. NEVER proceed without explicit go-ahead
2. All grinders: 100% example pass. No exceptions.
3. Push all work to GitHub before ending chat
4. Break work into small tasks
5. All OHLCV from Railway DB or local caches
6. Read ta_knowledge.md before any TA work
7. NEVER dump large data into context
8. Expression cache = single computation path
9. V2 only — no V1 patching

---

## Expression Library — Future (post-V2)

~3,630 new expressions treating 50 SMA and 200 SMA extension series as standalone price charts with full indicator suite. Deferred until V2 pipeline is stable.
