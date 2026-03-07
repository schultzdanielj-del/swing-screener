"""
scan_signals.py  —  V2 signal scanner. Replaces signal_filter.py.

Reads conditions from Railway (cycle_conditions table, not a local JSON file).
Writes all signals to Railway (cycle_signals table, not a local JSON file).
No file handoffs.

Phases:
    1. Load conditions for the current (or specified) cycle from Railway
    2. Load exit_conditions row from Railway
    3. Load examples from Railway (for is_example tagging + exit floor)
    4. Scan 5yr history against conditions (parallel, expr cache)
    5. Deduplicate: consecutive bars per ticker → keep rightmost
    6. Apply exit condition forward scan + measure (expr cache)
    7. Classify every signal (AUTO_WIN / AUTO_LOSS per DATA_CONTRACT rules)
    8. Upload all signals to Railway as cycle_signals rows

Usage:
    python scripts/scan_signals.py --setup dtss
    python scripts/scan_signals.py --setup dtss --cycle dtss_20260306_143022
    python scripts/scan_signals.py --setup dtss --workers 8
"""

import argparse
import json
import os
import pickle
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime, timezone

import numpy as np
import requests

# Force UTF-8 output on Windows
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

REPO_ROOT  = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)
sys.path.insert(0, os.path.join(REPO_ROOT, "local_runner"))

from expr_cache_builder import ExprSeriesCache

# ── Config ────────────────────────────────────────────────────────────────────

RAILWAY_URL      = "https://web-production-e3025.up.railway.app"
LOCAL_DIR        = os.path.join(REPO_ROOT, "local_runner")
CACHE_DIR        = os.path.join(LOCAL_DIR, "cache")
EXPR_SERIES_DIR  = os.path.join(CACHE_DIR, "expr_series")
MAX_FORWARD      = 120
DEFAULT_WORKERS  = os.cpu_count() or 8

SETUP_CONFIGS = {
    "dtss": {"direction": "short"},
}

# ── Railway helpers ───────────────────────────────────────────────────────────

def _get(endpoint, timeout=30):
    r = requests.get(f"{RAILWAY_URL}{endpoint}", timeout=timeout)
    r.raise_for_status()
    return r.json()


def _post(endpoint, payload, timeout=120):
    r = requests.post(f"{RAILWAY_URL}{endpoint}", json=payload, timeout=timeout)
    if not r.ok:
        print(f"  ERROR {r.status_code} posting to {endpoint}: {r.text[:300]}")
        r.raise_for_status()
    return r.json()


# ── Data loading ──────────────────────────────────────────────────────────────

def load_current_cycle(setup_type, cycle_id=None):
    """
    Load cycle_id and conditions from Railway.
    If cycle_id is given, load that specific cycle.
    Otherwise load the is_current=1 cycle for this setup_type.
    """
    data = _get(f"/api/v2/cycles/{setup_type}")
    cycles = data.get("cycles", [])
    if not cycles:
        print(f"  ERROR: No cycles found for {setup_type}")
        sys.exit(1)

    if cycle_id:
        match = next((c for c in cycles if c["cycle_id"] == cycle_id), None)
        if not match:
            print(f"  ERROR: cycle_id {cycle_id!r} not found for {setup_type}")
            sys.exit(1)
        target = match
    else:
        current = [c for c in cycles if c["is_current"] == 1]
        if not current:
            print(f"  ERROR: No is_current cycle found for {setup_type}. "
                  "Run grind_upload.py first.")
            sys.exit(1)
        target = current[0]

    cid = target["cycle_id"]
    print(f"  Cycle: {cid}  (status={target['status']}, "
          f"conditions={target['n_conditions']}, "
          f"examples_at_grind={target.get('n_examples_at_grind', '?')})")
    return cid, target


def load_conditions_from_railway(cycle_id):
    """Fetch cycle_conditions rows for this cycle."""
    data = _get(f"/api/v2/cycles/{cycle_id}/conditions")
    conditions = data.get("conditions", [])
    if not conditions:
        print(f"  ERROR: No conditions found for cycle {cycle_id}")
        sys.exit(1)
    print(f"  Loaded {len(conditions)} conditions from Railway")
    return conditions


def load_exit_condition_from_railway(setup_type):
    """Fetch exit_conditions row for this setup type."""
    data = _get(f"/api/v2/exit_conditions/{setup_type}")
    ec = data.get("exit_condition")
    if not ec:
        print(f"  ERROR: No exit condition found for {setup_type}.")
        print(f"  Upload one first: POST /api/v2/exit_conditions")
        sys.exit(1)
    print(f"  Exit condition: {ec['expression_name']} {ec['direction']} {ec['threshold']}  "
          f"(max_forward={ec['max_forward_bars']} bars, "
          f"adr_mult={ec['adr_threshold_multiplier']})")
    return ec


def load_examples_from_railway(setup_type):
    """Fetch validated examples for is_example tagging."""
    data = _get(f"/api/examples/{setup_type}")
    examples = data.get("examples", [])
    print(f"  Loaded {len(examples)} examples from Railway")
    return examples


def load_5yr_cache():
    path = os.path.join(CACHE_DIR, "universe_ohlcv_5yr.pkl")
    print(f"  Loading 5yr cache...")
    with open(path, "rb") as f:
        cache = pickle.load(f)
    print(f"  Loaded {len(cache):,} tickers")
    return cache


# ── Phase 1: Scan (parallel, expr cache) ─────────────────────────────────────

_worker_cache         = None
_worker_conditions    = None
_worker_cond_indices  = None
_worker_expr_dir      = None


def _init_scan_worker(cache, conditions, cond_indices, expr_dir):
    global _worker_cache, _worker_conditions, _worker_cond_indices, _worker_expr_dir
    _worker_cache        = cache
    _worker_conditions   = conditions
    _worker_cond_indices = cond_indices
    _worker_expr_dir     = expr_dir


def _load_npz(ticker):
    safe = ticker.replace("/", "_").replace("\\", "_")
    path = os.path.join(_worker_expr_dir, f"{safe}.npz")
    if not os.path.exists(path):
        return None, None
    try:
        loaded = np.load(path, allow_pickle=True)
        return loaded["dates"], loaded["data"]
    except Exception:
        return None, None


def _scan_batch(tickers):
    signals = []
    skipped = 0
    for ticker in tickers:
        df = _worker_cache.get(ticker)
        if df is None or len(df) < 100:
            skipped += 1
            continue
        try:
            dates_cache, data_cache = _load_npz(ticker)
            if dates_cache is None or len(dates_cache) != len(df):
                skipped += 1
                continue

            n_bars    = len(df)
            pass_mask = np.ones(n_bars, dtype=bool)
            pass_mask[:50] = False  # warmup

            for i, cond in enumerate(_worker_conditions):
                col_idx = _worker_cond_indices[i]
                if col_idx is None:
                    pass_mask[:] = False
                    break
                series   = data_cache[:, col_idx]
                in_range = (series >= cond["low"]) & (series <= cond["high"])
                in_range[np.isnan(series)] = False
                pass_mask &= in_range

            for idx in np.where(pass_mask)[0]:
                signals.append({
                    "ticker":  ticker,
                    "date":    str(df["date"].values[idx])[:10],
                    "bar_idx": int(idx),
                    "close":   float(df["close"].values[idx]),
                })
        except Exception:
            skipped += 1
    return signals, skipped


def scan_all_signals(cache, conditions, workers, expr_cache):
    """Parallel scan of full universe against conditions using expression cache."""
    tickers = list(cache.keys())
    batch_size = max(1, len(tickers) // (workers * 4))
    batches = [tickers[i:i + batch_size] for i in range(0, len(tickers), batch_size)]

    # Map condition names to expr cache column indices
    cond_indices = []
    missing = []
    for cond in conditions:
        name    = cond.get("expression_name") or cond.get("name", "")
        col_idx = expr_cache.expr_index(name)
        if col_idx is None:
            missing.append(name)
        cond_indices.append(col_idx)

    if missing:
        print(f"  WARNING: {len(missing)} condition(s) not in expression cache:")
        for m in missing[:10]:
            print(f"    {m}")
        if len(missing) > 10:
            print(f"    ... and {len(missing)-10} more")
        print("  Rebuild cache: python local_runner/expr_cache_builder.py --build --force")

    # Normalise conditions to have "low"/"high" keys (V2 schema uses those directly)
    norm_conds = []
    for cond in conditions:
        norm_conds.append({
            "name": cond.get("expression_name") or cond.get("name", ""),
            "low":  cond["low"],
            "high": cond["high"],
        })

    print(f"\n  Scanning {len(tickers):,} tickers × {len(norm_conds)} conditions "
          f"({workers} workers, {len(batches)} batches)...")
    t0 = time.time()

    all_signals = []
    with ProcessPoolExecutor(
        max_workers=workers,
        initializer=_init_scan_worker,
        initargs=(cache, norm_conds, cond_indices, EXPR_SERIES_DIR),
    ) as pool:
        futures = [pool.submit(_scan_batch, batch) for batch in batches]
        for done_n, future in enumerate(futures, 1):
            batch_sigs, _ = future.result()
            all_signals.extend(batch_sigs)
            if done_n % max(len(batches) // 5, 1) == 0 or done_n == len(batches):
                pct = done_n / len(batches) * 100
                print(f"    {pct:.0f}% [{time.time()-t0:.0f}s] {len(all_signals):,} raw signals")

    print(f"\n  Raw signals: {len(all_signals):,} in {time.time()-t0:.0f}s")
    return all_signals


# ── Phase 2: Deduplicate ──────────────────────────────────────────────────────

def deduplicate_signals(signals):
    """Consecutive bars per ticker → keep rightmost (identical logic to V1)."""
    signals.sort(key=lambda s: (s["ticker"], s["bar_idx"]))
    deduped = []
    i = 0
    while i < len(signals):
        ticker = signals[i]["ticker"]
        j = i + 1
        while j < len(signals):
            if signals[j]["ticker"] != ticker:
                break
            if signals[j]["bar_idx"] != signals[j - 1]["bar_idx"] + 1:
                break
            j += 1
        rightmost = signals[j - 1]
        rightmost["cluster_size"] = j - i
        deduped.append(rightmost)
        i = j
    print(f"  Deduped: {len(signals):,} → {len(deduped):,} "
          f"({len(signals)-len(deduped):,} collapsed)")
    return deduped


# ── Phase 3: Exit filter + measurement ───────────────────────────────────────

def apply_exit(signals, cache, exit_cond, direction, expr_cache):
    """
    Forward scan each signal for exit condition.
    Returns signals annotated with: exit_triggered, exit_date, exit_bar,
    move_adr, mfe_adr, capture_eff, adr (at signal bar).
    Signals with no exit get exit_triggered=0 and nulls.
    """
    expr_name  = exit_cond["expression_name"]
    threshold  = float(exit_cond["threshold"])
    ec_dir     = exit_cond["direction"]       # "above" / "below" / "crosses_above" / "crosses_below"
    max_fwd    = int(exit_cond["max_forward_bars"])

    exit_col = expr_cache.expr_index(expr_name)
    if exit_col is None:
        print(f"  ERROR: exit expression '{expr_name}' not in expression cache!")
        sys.exit(1)
    adr_col = expr_cache.expr_index("adr14")

    # Normalise direction to simple comparison operators used in V1
    # DATA_CONTRACT direction: "above" → >=, "below" → <=,
    #                          "crosses_above" → >=, "crosses_below" → <=
    above_ops = {"above", "crosses_above", ">="}
    ec_op = ">=" if ec_dir in above_ops else "<="

    print(f"\n  Exit: {expr_name} {ec_op} {threshold}  "
          f"(max_forward={max_fwd} bars, direction={direction})")

    _ticker_npz = {}
    results = []
    no_exit = 0
    errors  = 0

    for i, sig in enumerate(signals):
        ticker  = sig["ticker"]
        bar_idx = sig["bar_idx"]
        df      = cache.get(ticker)
        if df is None or bar_idx >= len(df) - 1:
            errors += 1
            continue
        try:
            if ticker not in _ticker_npz:
                dates, data = expr_cache.get_ticker(ticker)
                _ticker_npz[ticker] = (dates, data)
            _, data_cache = _ticker_npz[ticker]

            if data_cache is None or len(data_cache) != len(df):
                errors += 1
                continue

            # ADR at signal bar
            if adr_col is not None:
                adr = float(data_cache[bar_idx, adr_col])
            else:
                h = df["high"].values
                l = df["low"].values
                s = max(0, bar_idx - 13)
                adr = float(np.mean(h[s:bar_idx+1] - l[s:bar_idx+1]))

            if adr <= 0 or np.isnan(adr):
                errors += 1
                continue

            signal_close = float(df["close"].values[bar_idx])
            actual_fwd   = min(max_fwd, len(df) - bar_idx - 1)
            if actual_fwd < 5:
                errors += 1
                continue

            exit_series = data_cache[:, exit_col]
            exit_bar    = None
            exit_close  = None
            for fwd in range(1, actual_fwd + 1):
                idx = bar_idx + fwd
                val = exit_series[idx]
                if np.isnan(val):
                    continue
                if ec_op == ">=" and val >= threshold:
                    exit_bar   = fwd
                    exit_close = float(df["close"].values[idx])
                    break
                elif ec_op == "<=" and val <= threshold:
                    exit_bar   = fwd
                    exit_close = float(df["close"].values[idx])
                    break

            if exit_bar is None:
                no_exit += 1
                results.append({
                    **sig,
                    "adr":            round(adr, 4),
                    "exit_triggered": 0,
                    "exit_date":      None,
                    "move_adr":       None,
                    "mfe_adr":        None,
                    "capture_eff":    None,
                })
                continue

            exit_date = str(df["date"].values[bar_idx + exit_bar])[:10]
            fwd_slice = slice(bar_idx + 1, bar_idx + exit_bar + 1)
            if direction == "short":
                move_adr = (signal_close - exit_close) / adr
                mfe_price = float(df["low"].values[fwd_slice].min())
                mfe_adr   = (signal_close - mfe_price) / adr
            else:
                move_adr  = (exit_close - signal_close) / adr
                mfe_price = float(df["high"].values[fwd_slice].max())
                mfe_adr   = (mfe_price - signal_close) / adr

            cap_eff = round(move_adr / mfe_adr, 3) if mfe_adr > 0 else 0.0

            results.append({
                **sig,
                "adr":            round(adr, 4),
                "exit_triggered": 1,
                "exit_date":      exit_date,
                "move_adr":       round(move_adr, 4),
                "mfe_adr":        round(mfe_adr, 4),
                "capture_eff":    cap_eff,
            })

        except Exception:
            errors += 1
            results.append({
                **sig,
                "adr":            None,
                "exit_triggered": 0,
                "exit_date":      None,
                "move_adr":       None,
                "mfe_adr":        None,
                "capture_eff":    None,
            })

        if (i + 1) % 100 == 0:
            print(f"    {i+1}/{len(signals)}  exit={len([r for r in results if r['exit_triggered']])}  "
                  f"no_exit={no_exit}  errors={errors}")

    triggered = sum(1 for r in results if r["exit_triggered"])
    print(f"\n  Exit applied: {triggered} triggered, {no_exit} no exit, {errors} errors")
    return results


# ── Phase 4: Classify ─────────────────────────────────────────────────────────

def classify_signals(signals, examples, exit_cond):
    """
    Apply DATA_CONTRACT classification priority rules:
      1. (ticker, signal_date) in examples table → AUTO_WIN / source=example
      2. exit_triggered=1 AND move_adr >= adr_threshold → AUTO_WIN / source=exit_filter
      3. otherwise → AUTO_LOSS / source=exit_filter

    Also sets is_example flag.
    Pending_examples / rejected_signals re-classification happens server-side
    via classify_signals.py (a separate future step). This script writes the
    initial auto-classification.
    """
    # Build example lookup using proximity matching against scanned signals.
    # The grinder fires the signal 1 bar before entry (scan_idx = entry_idx - 1),
    # but the exact offset can vary. Strategy: for each example, find the scanned
    # signal for that ticker whose date is closest to (and <=) entry_date within
    # a 7-calendar-day window, then tag that signal date as is_example.
    import datetime as _dt

    ticker_signal_dates: dict = {}
    for sig in signals:
        t = sig["ticker"]
        if t not in ticker_signal_dates:
            ticker_signal_dates[t] = []
        ticker_signal_dates[t].append(sig["date"])

    example_dates: set = set()
    for ex in examples:
        ticker     = ex.get("ticker", "")
        entry_date = ex.get("entry_date") or ex.get("entryDate", "")
        if not ticker or not entry_date:
            continue
        sig_dates = ticker_signal_dates.get(ticker, [])
        if not sig_dates:
            continue
        entry_dt   = _dt.date.fromisoformat(entry_date)
        candidates = sig_dates  # search both directions; window check below enforces ±7 days
        best       = min(candidates,
                         key=lambda d: abs((_dt.date.fromisoformat(d) - entry_dt).days))
        if abs((_dt.date.fromisoformat(best) - entry_dt).days) <= 7:
            example_dates.add((ticker, best))

    # Derive ADR threshold from exit_cond
    adr_mult = float(exit_cond.get("adr_threshold_multiplier", 1.0))

    # Compute sample median ADR from auto-win signals (exit_triggered=1, not examples)
    # For classification we use a simple approach: compute median move_adr across
    # all signals with exit triggered, then scale by adr_mult.
    # On the first run there are no pre-classified wins so we use move_adr > 0
    # combined with adr_mult to get a threshold that roughly matches sample quality.
    exit_moves = [s["move_adr"] for s in signals
                  if s.get("exit_triggered") and s["move_adr"] is not None]

    if exit_moves:
        exit_moves_sorted = sorted(exit_moves)
        sample_median     = exit_moves_sorted[len(exit_moves_sorted) // 2]
        adr_threshold     = sample_median * adr_mult
    else:
        adr_threshold = 0.0

    print(f"  Classification: adr_mult={adr_mult}, sample_median={sample_median:.2f}, "
          f"adr_threshold={adr_threshold:.2f}")

    for sig in signals:
        ticker      = sig["ticker"]
        signal_date = sig["date"]

        is_example_flag = 1 if (ticker, signal_date) in example_dates else 0
        sig["is_example"] = is_example_flag

        if is_example_flag:
            sig["classification"]        = "AUTO_WIN"
            sig["classification_source"] = "example"
        elif sig.get("exit_triggered") and sig.get("move_adr") is not None \
                and sig["move_adr"] >= adr_threshold:
            sig["classification"]        = "AUTO_WIN"
            sig["classification_source"] = "exit_filter"
        else:
            sig["classification"]        = "AUTO_LOSS"
            sig["classification_source"] = "exit_filter"

    n_win  = sum(1 for s in signals if s["classification"] == "AUTO_WIN")
    n_loss = sum(1 for s in signals if s["classification"] == "AUTO_LOSS")
    n_ex   = sum(1 for s in signals if s["is_example"])
    wr     = n_win / len(signals) if signals else 0
    print(f"  Classified: {n_win} WIN ({wr:.1%}), {n_loss} LOSS  "
          f"[{n_ex} are examples]")
    return signals


# ── Phase 5: Upload to Railway ────────────────────────────────────────────────

def upload_signals(signals, cycle_id, setup_type):
    """
    Bulk-upload classified signals to Railway as cycle_signals rows.
    Endpoint clears existing rows for this cycle_id before inserting (idempotent).
    """
    payload_signals = []
    for s in signals:
        payload_signals.append({
            "cycle_id":              cycle_id,
            "setup_type":            setup_type,
            "ticker":                s["ticker"],
            "signal_date":           s["date"],
            "bar_idx":               s["bar_idx"],
            "close":                 s["close"],
            "adr":                   s.get("adr"),
            "is_example":            s.get("is_example", 0),
            "classification":        s.get("classification"),
            "classification_source": s.get("classification_source"),
            "exit_triggered":        s.get("exit_triggered", 0),
            "exit_date":             s.get("exit_date"),
            "move_adr":              s.get("move_adr"),
            "mfe_adr":               s.get("mfe_adr"),
            "capture_eff":           s.get("capture_eff"),
        })

    print(f"\n  Uploading {len(payload_signals):,} signals to Railway...")
    chunk_size = 500
    total_inserted = 0
    for i in range(0, len(payload_signals), chunk_size):
        chunk = payload_signals[i:i + chunk_size]
        resp = _post(
            f"/api/v2/cycles/{cycle_id}/signals",
            {"cycle_id": cycle_id, "signals": chunk, "replace": i == 0},
            timeout=120,
        )
        total_inserted += resp.get("inserted", len(chunk))
        print(f"    chunk {i//chunk_size + 1}: {len(chunk)} → "
              f"{resp.get('inserted', '?')} inserted  (total={total_inserted})")

    print(f"  ✓ Uploaded {total_inserted} signal rows to Railway")
    return total_inserted


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="V2 signal scanner — reads from DB, writes to DB."
    )
    parser.add_argument("--setup",   required=True, help="Setup type, e.g. dtss")
    parser.add_argument("--cycle",   default=None,
                        help="Specific cycle_id to scan (default: current cycle)")
    parser.add_argument("--workers", type=int, default=DEFAULT_WORKERS)
    args = parser.parse_args()

    setup     = args.setup.lower()
    direction = SETUP_CONFIGS.get(setup, {}).get("direction", "short")

    print(f"\n{'='*60}")
    print(f"  SCAN SIGNALS  —  {setup.upper()}")
    print(f"{'='*60}\n")
    t0 = time.time()

    # ── Load from Railway ────────────────────────────────────────────────────
    print("  [railway] Loading cycle...")
    cycle_id, cycle_meta = load_current_cycle(setup, args.cycle)

    print("  [railway] Loading conditions...")
    conditions = load_conditions_from_railway(cycle_id)

    print("  [railway] Loading exit condition...")
    exit_cond = load_exit_condition_from_railway(setup)

    print("  [railway] Loading examples...")
    examples = load_examples_from_railway(setup)

    # ── Load local caches ────────────────────────────────────────────────────
    print("\n  [local] Loading 5yr OHLCV cache...")
    cache = load_5yr_cache()

    print("  [local] Loading expression cache...")
    expr_cache = ExprSeriesCache()
    if not expr_cache.is_valid():
        print("  ERROR: Expression cache not found or invalid.")
        print("  Run: python local_runner/expr_cache_builder.py --build")
        sys.exit(1)
    print(f"  Expression cache: {expr_cache.n_expressions:,} expressions")

    # ── Phase 1: Scan ─────────────────────────────────────────────────────────
    print(f"\n  PHASE 1: Scan all signals")
    raw_signals = scan_all_signals(cache, conditions, args.workers, expr_cache)

    # ── Phase 2: Deduplicate ──────────────────────────────────────────────────
    print(f"\n  PHASE 2: Deduplicate (consecutive → rightmost)")
    deduped = deduplicate_signals(raw_signals)

    # ── Phase 3: Exit filter ──────────────────────────────────────────────────
    print(f"\n  PHASE 3: Apply exit condition + measure")
    with_exit = apply_exit(deduped, cache, exit_cond, direction, expr_cache)

    # ── Phase 4: Classify ─────────────────────────────────────────────────────
    print(f"\n  PHASE 4: Classify signals")
    classified = classify_signals(with_exit, examples, exit_cond)

    # ── Phase 5: Upload ───────────────────────────────────────────────────────
    print(f"\n  PHASE 5: Upload to Railway")
    n_uploaded = upload_signals(classified, cycle_id, setup)

    # ── Summary ───────────────────────────────────────────────────────────────
    n_win  = sum(1 for s in classified if s.get("classification") == "AUTO_WIN")
    n_loss = sum(1 for s in classified if s.get("classification") == "AUTO_LOSS")
    wr     = n_win / len(classified) if classified else 0
    elapsed = time.time() - t0

    print(f"\n{'='*60}")
    print(f"  DONE in {elapsed:.0f}s")
    print(f"  cycle_id  : {cycle_id}")
    print(f"  raw       : {len(raw_signals):,}")
    print(f"  deduped   : {len(deduped):,}")
    print(f"  uploaded  : {n_uploaded:,}")
    print(f"  AUTO_WIN  : {n_win:,} ({wr:.1%})")
    print(f"  AUTO_LOSS : {n_loss:,}")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    main()
