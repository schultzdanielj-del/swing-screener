# Multi-Stage Exit Grinder — Architecture & Rules

**Script:** `scripts/multistage_exit_grinder.py`
**Task:** 3.7 in ANALYSIS_SYSTEM.md
**Status:** Active development

---

## Purpose

Discovers optimal multi-stage conditional exit strategies for validated setup examples. Given a set of example trades with known entry dates, the grinder finds TA-driven conditions that tell you when to exit — and how much to trim at each stage.

Separate from single-stage `exit_grinder.py` (Step 6). Both coexist — this grinder tests structural variants (partial trims, staged exits, gated conditions) that the single-stage grinder can't express.

---

## Grinder Rules (Non-Negotiable)

These apply to ALL grinders in the system. No exceptions.

1. **100% example pass rate.** Every condition must fire on every example. If a condition doesn't trigger on even one example, that config is INVALID and thrown out. No backstops. No S99. No partial credit.

2. **Same computation methods.** Uses ExitExprEngine (same as exit_grinder.py), same expression library, same boolean aggregations. No divergence.

3. **All CPU cores used.** Every phase — matrix build, condition discovery, pass simulation — runs parallel via ProcessPoolExecutor.

4. **Never abort.** Errors are caught and logged. The grinder always produces output even if some expressions fail.

5. **Sort: floor primary, median secondary.** Floor capture efficiency (worst example) is the primary ranking metric. Median is the tiebreaker. This matches exit_grinder.py and the design principle of reliable capital turnover over max extraction.

---

## How It Works

### Phase 1: Matrix Build (parallel, all cores)
- Load examples from Railway API + 5yr OHLCV cache
- Compute exit expressions for each example's forward path using ExitExprEngine
- Each example produces a dict: `{expr_name: numpy_array}` (values at each forward bar)
- Optional: boolean aggregation expressions (rolling counts, streaks, etc.)

### Phase 2: Condition Discovery (parallel, all cores)
- For each expression × threshold × direction (above/below):
  - Check if the condition triggers on 100% of examples
  - "Triggers" = the expression crosses the threshold at some bar in the forward window
  - Only conditions passing 100% are kept
- Thresholds are data-driven (percentiles of combined values across examples)
- Chunked across workers by expression name

### Phase 3: 8 Passes (each parallel, all cores)
Each pass tests a different structural variant using the valid conditions from Phase 2:

| Pass | Label | Structure | What it tests |
|------|-------|-----------|---------------|
| P1 | single | 1-stage, 100% exit | Baseline — same as exit_grinder.py |
| P2 | early-trim | 2-stage: partial trim (max_window) + full exit | Trim early, ride rest |
| P3 | mfe-gated | 2-stage: MFE-gated partial + full exit | Only trim after move hits N×ADR |
| P4 | protect+trail | 2-stage: early trim + MFE-gated full | Protect capital early, let winner run |
| P5 | bar-gated | 1-stage with minimum bars | Don't exit before bar N |
| P6 | cross-cond | 2-stage, different conditions per stage | Stage 1 uses expr A, stage 2 uses expr B |
| P7 | 3stg-cross | 3-stage, different conditions | Three different exit triggers |
| P8 | refine | Finer thresholds around best from P1-P7 | Tighten parameters |

### Phase 4: Rank, Dedup, Report
- Pool all results from all passes
- Sort by (floor_capture_eff, median_capture_eff) descending
- Deduplicate identical stage configs
- Re-simulate top results for full per-example reporting
- Save JSON (timestamped archive + latest overwrite)

---

## Stage Config

Each stage is a tuple with 8 fields (tuples for fast pickling in multiprocessing):

```
(stage_id, expr_name, direction, threshold, trim_pct, gate_type, gate_value, max_window)
```

- **stage_id**: Sequential (1, 2, 3...). Stages fire in order.
- **expr_name**: Which exit expression to evaluate
- **direction**: "above" or "below" the threshold
- **threshold**: Numeric threshold value
- **trim_pct**: Fraction of remaining position to exit (0.25 = trim 25% of what's left)
- **gate_type**: "none", "mfe_adr" (only fire after MFE ≥ N×ADR), "bars_min" (only fire after bar N)
- **gate_value**: The gate threshold
- **max_window**: Maximum bar for this stage to fire (None = no limit)

### Simulation Logic
Walk forward bar-by-bar. At each bar, check stages in order. A stage fires if:
1. It hasn't already fired
2. Bar is within max_window (if set)
3. Gate is satisfied (MFE or bars)
4. Expression value crosses threshold

When a stage fires, it trims `trim_pct` of remaining position at that bar's close price.

**If ANY position remains after walking all bars, the config FAILS.** No backstop.

---

## Scoring

- **Capture efficiency** = effective_pct / MFE_pct per example
  - effective_pct = Σ(pct_trimmed × pct_move) across all stage events
  - For multi-stage: weighted average of each trim's % move
- **Floor capture efficiency** = worst example's capture_eff (primary sort)
- **Median capture efficiency** = median across examples (secondary sort)

Design principle: consistent reliable exits that turn over capital. A system that reliably captures 60% of every move beats one that captures 90% on some and 10% on others.

---

## Parameter Space

```
TRIM_PCTS = [0.25, 0.33, 0.50, 0.75, 1.0]
MAX_WINDOWS = [8, 12, 15, 20, 25, 30]
MFE_GATES = [0.5, 1.0, 1.5, 2.0, 2.5, 3.0, 4.0]
BAR_GATES = [10, 15, 20, 30, 40]
```

---

## Usage

```bash
# Full run (base + boolean aggregations)
python scripts/multistage_exit_grinder.py --setup dtss

# Fast run (base expressions only)
python scripts/multistage_exit_grinder.py --setup dtss --base-only

# Custom workers
python scripts/multistage_exit_grinder.py --setup dtss --workers 12

# All options
python scripts/multistage_exit_grinder.py \
    --setup dtss \
    --direction short \
    --max-forward 120 \
    --n-thresholds 20 \
    --workers 12 \
    --base-only
```

---

## Output

```
data/multistage_exit/
├── ms_exit_dtss_1stg_0.450floor_20260301_160000.json   # timestamped archive
└── ms_exit_dtss.json                                     # latest (overwritten each run)
```

JSON contains: setup metadata, example list with MFE, top 50 ranked configs with full per-example event breakdown.

---

## Dependencies

- `scripts/exit_expressions.py` — expression library generation
- `scripts/exit_compute.py` — ExitExprEngine (forward-path expression computation)
- `scripts/exit_grinder.py` — compute_boolean_aggregations (imported directly)
- `local_runner/cache/universe_ohlcv_5yr.pkl` — 5yr OHLCV cache
- Railway API — example metadata

---

## Changelog

- **2026-03-01**: v4 — Full parallelization (condition discovery + all passes). Removed S99 backstop. Fixed sort order to floor-primary. Enforced 100% fire rule.
- **2026-03-01**: v3 — 8-pass architecture replacing isolated stage grinding
- **2026-03-01**: v2 — Simplified from Phase A/B to single command
- **2026-02-28**: v1 — Initial multi-stage implementation with 3 separate stage grinders
