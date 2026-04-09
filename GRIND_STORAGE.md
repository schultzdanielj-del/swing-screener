# Grind Storage — Reference

**Last updated:** 2026-03-17

How grind results are stored and backed up.

---

## Design Principle

One grind run → one authoritative local file + one Railway backup copy.

1. **Local timestamped JSON** — written by the grinder script to `local_runner/cache/`.
   Unique filename, never overwrites. This is the source of truth.
2. **Railway mirror** — copied to Railway's `file_mirror` table via `file_mirror.py`.
   Backup copy. Claude reads grind results from here during chat sessions.
   The PySide6 app does NOT read from Railway — it reads local files.

---

## Local Storage

**Location:** `local_runner/cache/`

**Signal grind:**
```
pyramid_{setup}_{mode}_sig{total}_pk{peak}_{timestamp}.json
```

**Refinement grind (cluster-aware):**
```
raw_signal_clusters_{setup}.json          (phase 1: clusters + classification)
raw_signal_clusters_{setup}_{timestamp}.json  (timestamped archive)
refinement_{setup}_cl{N}_pk{N}_{timestamp}.json  (phase 2: winner/loser signals + conditions)
```
The `_cl` prefix in refinement files indicates cluster-aware output. Only `_cl` files contain the authoritative winner pile.

**EV grinder:**
```
ev_{setup}_inc{N}_{timestamp}.json
```

**Entry candle scorer:**
```
entry_scores_{setup}.json
entry_scores_{setup}_{timestamp}.json
```

---

## Railway Backup

Grind results are mirrored to Railway via `file_mirror.py` immediately after local write. This is a backup, not the source of truth. The seed vault (`scripts/seed_vault.py`) also backs up all SQLite tables nightly.

Recovery: `python scripts/seed_vault.py --restore` pulls everything back from Railway.

---

## What the PySide6 App Reads

The desktop app reads ONLY from local files:
- `local_runner/cache/refinement_{setup}_cl*.json` — winner pile for examples progress bar
- `local_runner/cache/ev_{setup}_*.json` — EV grinder existence check for unlock gates
- `data/scanperfect.db` — examples, setups, earnings, pending reviews
- `data/pipeline_state.json` — grinder run status
- `data/pipeline_logs.json` — grinder log output
- `data/vetting/vetting_{setup}.json` — vetting decisions
