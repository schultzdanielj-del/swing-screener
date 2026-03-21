# Data Contract — ScanPerfect Schema & Data Flow

**Last updated:** 2026-03-20
**Status:** Authoritative. Build from this. Do not assume anything not written here.

---

## Design Principles

- **Local is authoritative.** SQLite DB (`data/scanperfect.db`) and local cache files (`local_runner/cache/`) are the single source of truth. All compute runs locally, all results write locally.
- **Railway is a seed vault.** Daily backup of DB tables + grind result JSONs to Railway via `scripts/seed_vault.py`. Railway has no compute, no UI, no pipeline logic. It exists for disaster recovery and for Claude to read grind results during chat sessions.
- **Grind results are local JSON files.** The grinders write timestamped JSON to `local_runner/cache/`. These are the authoritative outputs. They are also mirrored to Railway's `file_mirror` table as a backup copy.
- **The PySide6 app reads local files directly.** No HTTP layer, no server process, no API calls. SQLite for structured data (examples, setups, earnings). Local JSON files for grind outputs. 5yr OHLCV pickle loaded into memory.
- All timestamps are UTC ISO-8601 strings: `"2026-03-06T14:30:22Z"`.
- All dates (signal dates, entry dates) are `"YYYY-MM-DD"` strings.
- **OHLCV and expr cache must stay in sync.** Both use append-only nightly updates — never rebuild from scratch, never drop old bars. If they drift (different bar counts for the same ticker), signal bar indices between the two point to different dates, breaking example matching and condition checking. Fixed 2026-03-20.
- **Example matching uses hardcoded entry_date, never bar indices.** Bar indices shift when caches rebuild. Dates are stable.

---

## SQLite Tables (data/scanperfect.db)

### `setups`
Setup type definitions. Seeded with DTSS, 3-4DB, HTF.

| Column | Type | Notes |
|--------|------|-------|
| setup_type | TEXT PK | e.g. `"dtss"` |
| name | TEXT | Display name |
| description | TEXT | Pattern description (feeds AI reviewer) |
| direction | TEXT | `"long"` or `"short"` |
| created_at | TEXT | |

### `examples`
Ground truth. One row per validated setup example.

| Column | Type | Notes |
|--------|------|-------|
| id | INTEGER PK | autoincrement |
| setup_type | TEXT | |
| ticker | TEXT | |
| chart_date | TEXT | date the chart pattern is visible |
| entry_date | TEXT | date of the actual entry bar |
| created_at | TEXT | |

Unique: `(setup_type, ticker, entry_date)`

### `pending_examples`
AI vet queue. YES picks awaiting AI review.

| Column | Type | Notes |
|--------|------|-------|
| id | INTEGER PK | |
| setup_type | TEXT | |
| ticker | TEXT | |
| signal_date | TEXT | |
| entry_date | TEXT | |
| status | TEXT | `pending` / `reviewed` |
| ai_verdict | TEXT | `APPROVE` / `REJECT` |
| ai_reasoning | TEXT | |
| created_at | TEXT | |
| reviewed_at | TEXT | |

### `rejected_signals`
Signals marked NO. Prevents re-surfacing in vetting. Also used by profit grinder to exclude vetted-NO signals from the population.

### `earnings_dates`
Cached earnings dates per ticker.

### `file_mirror`
Backup copies of grind result JSONs. Used by seed vault for Railway backup. The PySide6 app does NOT read from this table — it reads local files directly.

### `nightly_watchlist`
Future: ranked signals from the live nightly scan.

---

## Local Cache Files (local_runner/cache/)

These are the authoritative grind outputs. The PySide6 app reads them directly.

| File Pattern | Producer | Contents |
|-------------|----------|----------|
| `pyramid_{setup}_*.json` | Signal grind | Condition set + raw signals |
| `raw_signal_clusters_{setup}.json` | Refinement grind (phase 1) | All clusters with classification |
| `refinement_{setup}_cl*.json` | Refinement grind (phase 2) | Winner/loser/eliminated signals + combined conditions + `depth_progression` (condition set + cluster counts + WR at each depth level) |
| `ev_{setup}_*.json` | EV grinder | Scoring equation + per-signal WR/MFE/EV + setup_score/market_score + killed_at_depth + calibration |
| `entry_scores_{setup}.json` | Entry candle scorer | Per-winner entry_candle_score, combined_score for vetting sort. Also consumed by profit grinder for tradability weighting. |
| `profit_{setup}_*.json` | Profit grinder | Exit expression candidates + weighted stats + equity curves + per-trade detail |
| `profit_{setup}.json` | Profit grinder | Latest pointer (symlink-style copy of most recent timestamped file) |
| `scan_settings_{setup}.json` | Scan Tuning UI | Locked slider settings: setup/market score floors, refinement depth, WR floor, exit objective, trim %. Read by nightly scan. |
| `universe_ohlcv_5yr.pkl` | cache_builder.py | All available OHLCV history for ~4,169 tickers (no bar limit). Nightly append-only — never rebuilds, never drops old bars. |
| `market_cache_*.npz` | market_cache_builder.py | 256 instrument expression series |
| `expr_cache/*.npz` | expr_cache_builder.py | Per-ticker expression series (~21 GB). Nightly append-only. |
| `fundamentals_cache.json` | fetch_fundamentals.py | Per-ticker sector, shares outstanding, float |

---

## Data Flow

```
Examples (SQLite) + Expression Cache (local .npz files)
    → Signal Grind → pyramid_{setup}_*.json
    → Exit Grind → exit condition in local cache
    → Refinement Grind → raw_signal_clusters_{setup}.json + refinement_{setup}_cl*.json

Refinement output + Market Cache + OHLCV + Fundamentals
    → EV Grinder → ev_{setup}_*.json

EV output + Entry Scores + Vetting Decisions (SQLite) + Expression Cache + OHLCV
    → Profit Grinder → profit_{setup}_*.json

All outputs mirrored to Railway via file_mirror.py (backup only)
Nightly seed vault pushes SQLite tables to Railway (backup only)
```
