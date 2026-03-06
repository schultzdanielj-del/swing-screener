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

## V2 Build Order

### ✅ Done
1. `DATA_CONTRACT.md` — V2 schema defined and committed

### 🔲 Next: Wire V2 tables into server.py `init_db`

Add all 7 new V2 tables to `init_db()` in `server.py`:
- `grind_cycles`
- `cycle_conditions`
- `cycle_signals`
- `exit_conditions`
- `cycle_health`
- `regime_model`
- `signal_regime_scores`
- `nightly_watchlist`

Schema is fully specified in `DATA_CONTRACT.md`. Railway auto-deploys on push — tables created on next startup.

### 🔲 Then: grind_upload.py
Script that runs after pyramid_grinder.py completes and uploads the result to Railway as a new `grind_cycle` row + `cycle_conditions` rows. Replaces the current zero-upload situation (BUG-003).

### 🔲 Then: scan_signals.py (replaces signal_filter.py)
Reads conditions from `cycle_conditions` (not a local JSON file). Writes signals to `cycle_signals`. No file handoffs.

### 🔲 Then: cycle_health.py
Computes all `cycle_health` metrics from `cycle_signals` + `grind_cycles`. Uploads to Railway. Powers the health dashboard.

### 🔲 Then: V2 UI
- Health metrics panel
- Cycle diff (conditions added/removed vs prev cycle)
- Revert button (flip is_current in transaction)
- Regrind indicator
- Unified watchlist (reads nightly_watchlist table)

### 🔲 Then: Market Grinder (V2-native)
Built on `cycle_signals` table, not `refined_{setup}.json`. SPY regime indicators → win rate by bucket → live win rate estimator.

---

## Pipeline Steps (V1 reference — being replaced)

| # | Step | Status |
|---|------|--------|
| 0 | Nightly Refresh | Works |
| 1 | Optimal Samples | 71 DTSS examples |
| 2 | Signal Brute Forcing | Grind #5 done (94 conditions) |
| 3 | Sample Expansion | signal_filter.py — being replaced by scan_signals.py |
| 4a | Exit Grinder | Done — slope_xavgc21_off7_adr14 ≤ -1.128826 |
| 4b | Setup Grinder | Replaced by cycle_health.py in V2 |
| 5 | Market Grinder | V2-native — build after cycle_signals table exists |

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
| `slope_xavgc21_off7_adr14 ≤ -1.128826` | 3.2 ADR | 5.8 ADR | 38 bars |

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

- Expression cache: 4,119 tickers × 12,421 expressions (~50 GB) — **stale, needs rebuild**
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
9. **V2 only — no V1 patching**

---

## Expression Library — Future (post-V2)

### Extension Structure Expressions

~3,630 new expressions treating 50 SMA and 200 SMA extension series as standalone price charts with full indicator suite. Scope and implementation documented in previous TODO versions. Deferred until V2 pipeline is stable.
