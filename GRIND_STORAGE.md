# Grind Storage — V2 Reference

**Last updated:** 2026-03-08

This document defines how grind results are stored, uploaded, queried, and recovered.
Read this before touching `grind_uploader.py`, `pyramid_grinder.py` output logic,
or any `server.py` cycle endpoints.

---

## Design Principle

One grind run → two identical copies:

1. **Local timestamped JSON** — written by `pyramid_grinder.py` at end of run.
   Backup/debug copy. Unique filename, never overwrites. Ephemeral (sandbox resets).
2. **Railway cycle** — uploaded by `grind_uploader.py` immediately after local write.
   Permanent record. Source of truth. UI reads only from here.

There is no separate sync step. The upload happens inside the grinder function itself,
so both CLI runs and agent-triggered runs upload automatically.

---

## Local Storage

**Location:** `local_runner/cache/`

**Filename pattern:**
```
pyramid_{setup}_{mode}{blackout}_sig{total}_pk{peak}_{timestamp}.json
```

**Example:**
```
pyramid_dtss_mp_sig168_pk3_20260308_142000.json
pyramid_dtss_mp_blackout_sig95_pk2_20260308_153000.json
```

**Fields:**
- `setup` — setup type (dtss, 3-4db, htf, etc.)
- `mode` — `mp` (multi-pass) or `sp` (single-pass)
- `blackout` — `_blackout` suffix if refinement grind
- `sig{N}` — total signal count
- `pk{N}` — peak signals/day
- `timestamp` — `YYYYMMDD_HHMMSS` local time

**Content structure:** Full pyramid grinder output dict with:
```json
{
  "setup_type": "dtss",
  "timestamp": "2026-03-08T14:20:00+00:00",
  "total_time_s": 1847.3,
  "peak_target": 3,
  "multi_pass": true,
  "blackout": false,
  "n_conditions": 86,
  "all_conditions": [
    {
      "tier": "D1",
      "expression_name": "rsi_14_d",
      "low": 55.2,
      "high": 85.1,
      "filter_power": 0.943
    }
  ],
  "tier_results": { ... },
  "pass_summaries": { ... },
  "params": {
    "beam_width": 10000,
    "depth": 100,
    "peak_target": 3,
    "multi_pass": true,
    "blackout": false,
    "source": "pyramid_grinder"
  },
  "summary": {
    "final_total": 168,
    "final_peak": 3,
    "final_avg": 1.4
  },
  "example_signals": [ ... ],
  "examples_passing": 72,
  "examples_failing": 0
}
```

**What was removed (V1 artifacts):**
- `pyramid_results_{setup}.json` — latest pointer. Replaced by Railway `is_current=1`.
- `pyramid_results_{setup}_blackout.json` — blackout latest pointer. Same.
- `historical_results_{setup}.json` — compat format. Redundant with Railway conditions.
- `local_runner/grind_storage.py` — V1 file manager. Replaced by `grind_uploader.py`.
- `scripts/migrate_grinds.py` — migration for V1 system.
- `local_runner/grinds/` directory structure — no longer used.

---

## Railway Storage

### Tables

**`grind_cycles`** — one row per grind run.

| Column | Type | Description |
|--------|------|-------------|
| cycle_id | TEXT PK | `{setup}_{step_type}_{YYYYMMDD_HHMMSS}` |
| setup_type | TEXT | e.g. "dtss" |
| status | TEXT | "uploading" / "complete" / "error" / "reverted" |
| error_msg | TEXT | Error detail if status=error |
| is_current | INT | 1 = this is the active cycle for this setup type |
| n_examples_at_grind | INT | How many examples existed when grind ran |
| created_at | TEXT | ISO timestamp |
| completed_at | TEXT | ISO timestamp |
| reverted_at | TEXT | ISO timestamp (if reverted) |
| step_type | TEXT | "signal_grind" or "refinement_grind" |
| grind_params | TEXT | JSON blob: beam, depth, peak, blackout, multi_pass |
| source_hash | TEXT | SHA-256 of local JSON file |

**`cycle_conditions`** — one row per condition per cycle.

| Column | Type | Description |
|--------|------|-------------|
| id | INT PK | Auto-increment |
| cycle_id | TEXT FK | References grind_cycles |
| tier | TEXT | D1, T2, T3, T4, T5, T6 |
| expression_name | TEXT | Expression identifier |
| low | REAL | Lower bound |
| high | REAL | Upper bound |
| filter_power | REAL | Fraction of universe eliminated |
| sort_order | INT | Lock order (0-indexed) |

**`cycle_signals`** — one row per signal per cycle (populated by scan step, not grinder).

| Column | Type | Description |
|--------|------|-------------|
| id | INT PK | Auto-increment |
| cycle_id | TEXT FK | References grind_cycles |
| setup_type | TEXT | e.g. "dtss" |
| ticker | TEXT | Symbol |
| signal_date | TEXT | Date signal fired |
| bar_idx | INT | Bar index in OHLCV array |
| close | REAL | Close price on signal bar |
| adr | REAL | ADR on signal bar |
| is_example | INT | 1 if this signal matches a validated example |
| classification | TEXT | "WIN" or "LOSS" |
| classification_source | TEXT | "auto" / "manual" / "ai-approved" |
| exit_triggered | INT | 1 if exit condition fired |
| exit_date | TEXT | Date exit triggered |
| move_adr | REAL | Move in ADR units |
| mfe_adr | REAL | Max favorable excursion in ADR |
| capture_eff | REAL | move / mfe ratio |
| regime_score | REAL | From regime model |
| vetted_at | TEXT | Timestamp of manual review |

### Endpoints

**Create:**
```
POST /api/v2/cycles
Body: { cycle_id, setup_type, status, step_type, grind_params, source_hash, n_examples_at_grind, created_at, completed_at }
```

**Update:**
```
PATCH /api/v2/cycles/{cycle_id}
Body: { status?, error_msg?, step_type?, grind_params?, source_hash?, completed_at?, reverted_at? }
```

**Upload conditions:**
```
POST /api/v2/cycles/{cycle_id}/conditions
Body: { conditions: [{ tier, expression_name, low, high, filter_power, sort_order }] }
```
Replaces existing conditions for this cycle (DELETE + INSERT).

**Activate (set as current):**
```
POST /api/v2/cycles/{cycle_id}/activate
```
Sets `is_current=0` on all other cycles for same setup, `is_current=1` on this one.

**List cycles:**
```
GET /api/v2/cycles/{setup_type}?step_type=signal_grind
```
Returns all cycles for setup, optionally filtered by step type. Newest first.

**Get conditions:**
```
GET /api/v2/cycles/{cycle_id}/conditions
```

**Upload signals (used by scan step, not grinder):**
```
POST /api/v2/cycles/{cycle_id}/signals
Body: { signals: [...], replace: true/false }
```

**Get signals:**
```
GET /api/v2/cycles/{cycle_id}/signals
```

---

## Upload Transaction

Handled by `local_runner/grind_uploader.py`. Called automatically by `pyramid_grinder.py`
after local file writes. The sequence:

```
1. Validate result JSON structure
2. Compute SHA-256 of local file
3. POST /api/v2/cycles              → status="uploading"
4. POST /api/v2/cycles/{id}/conditions
5. GET  /api/v2/cycles/{id}/conditions  → verify count matches
6. POST /api/v2/cycles/{id}/activate
7. PATCH /api/v2/cycles/{id}         → status="complete", source_hash
```

### Failure defenses

**Network/crash (steps 3-7 fail):**
- Retry 3x with 2s/5s/10s backoff
- On final failure: write to `local_runner/pending_uploads/{cycle_id}.json`
- Pending files retried on next grind run or agent startup (`retry_pending()`)
- Local timestamped JSON always exists regardless of upload outcome

**Schema mismatch (step 1 fails):**
- Validate before any HTTP call: top-level keys, conditions list, condition keys
- On failure: skip upload, print loud error, no pending file (this is a code bug, not transient)

**Partial upload (step 4 or 5 fails after step 3):**
- Cycle exists in Railway with `status="uploading"`
- On condition upload failure: PATCH cycle to `status="error"` with error message
- Cycle is visibly broken, never activated, never shown as current
- On retry: detects existing cycle, re-uploads conditions (DELETE + INSERT)

**Verification mismatch (step 5 shows wrong count):**
- PATCH cycle to `status="error"`, save pending file for retry
- Mismatch message includes expected vs actual count

**Manual file edits (post-hoc detection):**
- SHA-256 of local file stored in `source_hash` column
- Compare local file hash vs Railway hash to detect divergence
- Not an active defense — detection only

### Invariant

The upload function **never raises**. If everything fails — network down, disk full,
Railway dead — it logs warnings, saves a pending file if possible, and returns `None`.
The grind result is always saved locally. The grind is never blocked by upload failure.

---

## Pending Uploads

**Location:** `local_runner/pending_uploads/`

**Filename:** `{cycle_id}.json`

**Content:** Full upload payload including the grind result, cycle_id, step_type,
setup_type, source_hash, grind_params, and failure timestamp.

**Retry triggers:**
1. `grind_uploader.upload()` calls `retry_pending()` before every new upload
2. `pipeline_agent.py` calls `retry_pending()` on startup

**On success:** pending file is deleted.
**On continued failure:** pending file remains for next retry attempt.

---

## How Downstream Steps Read Grind Results

**UI conditions display (`loadConds`):**
```
GET /api/v2/cycles/{setup_type} → find is_current=1 → GET /api/v2/cycles/{id}/conditions
```

**signal_filter.py (scan step):**
Searches `local_runner/cache/` for `pyramid_{setup}_*.json` files, picks most
recently modified. The timestamped archive file matches this glob pattern.

**setup_refiner.py (refinement grind sub-step):**
Searches for `pyramid_{setup}_*_blackout_*.json` in `local_runner/cache/`.
Refuses to load non-blackout files to prevent stage mixing.

**Vetting UI:**
Reads from `/api/vetting/{setup}/signals` (step 3 exit filter output) or
`/api/setup-grinder/{setup}/signals` (step 4 refinement output). These are
populated by the scan and setup_refiner scripts, not by the grinder directly.

---

## Revert

Revert = activate a previous cycle as current.

```
POST /api/v2/cycles/{previous_cycle_id}/activate
```

This sets `is_current=0` on the bad cycle and `is_current=1` on the previous one.
All conditions, signals, and health metrics for both cycles remain in Railway.
Nothing is deleted. The UI immediately reads from the restored cycle.

To mark a cycle as explicitly reverted:
```
PATCH /api/v2/cycles/{bad_cycle_id}
Body: { "status": "reverted", "reverted_at": "2026-03-08T..." }
```

---

## Cycle ID Convention

```
{setup_type}_{step_type}_{YYYYMMDD_HHMMSS}
```

Examples:
```
dtss_signal_grind_20260308_142000
dtss_refinement_grind_20260308_153000
3-4db_signal_grind_20260310_091500
```

The timestamp is UTC. The cycle_id is unique across all setup types and step types.
