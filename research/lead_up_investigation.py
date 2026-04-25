"""Lead-up envelope characterization from labeled examples (entry-anchored).

No assumptions about signal bars. Builds per-offset shape envelope going backward
from each example's labeled entry date using normalized OHLCV.

Normalization (per §4.7 / Dan 2026-04-19 decision):
  - Log returns:     log(close_{E-k}/close_E), log(high_{E-k}/close_E),
                     log(low_{E-k}/close_E)
  - ATR-normalized:  (close_{E-k}-close_E)/ATR_E, same for high/low

Random baseline: ~10k (ticker, bar) pairs drawn from universe, each treated as
its own faux-E. Measures per-offset envelope strength against an anchor-less
universe null.

Outputs per-offset trajectory per setup + summary text proposing N definitions
from the data (no eyeball cut — multiple candidate rules computed, Dan picks).
"""
from __future__ import annotations

import os
import pickle
import sqlite3
import sys
import time
from collections import defaultdict

import numpy as np
import pandas as pd

MAIN_ROOT = r"C:/Users/Dan/Documents/ScanPerfect/swing-screener"
CACHE_DIR = os.path.join(MAIN_ROOT, "local_runner", "cache")
DB = os.path.join(MAIN_ROOT, "data", "scanperfect.db")
WORKTREE = r"C:/Users/Dan/Documents/ScanPerfect/swing-screener-win-loss-classifier"
OUT_DIR = os.path.join(WORKTREE, "research", "lead_up_investigation")

SETUP_ORDER = ["htf", "bf", "base", "dtss", "3-4db"]
K_MAX = 100
ATR_PERIOD = 14
N_RANDOM_TICKERS = 500
BARS_PER_TICKER = 20
RANDOM_SEED = 42
AXES = ["log_close", "log_high", "log_low", "atr_close", "atr_high", "atr_low"]


def dates_as_str(df):
    if pd.api.types.is_datetime64_any_dtype(df["date"]):
        return pd.to_datetime(df["date"]).dt.strftime('%Y-%m-%d').values
    return np.array([str(d)[:10] for d in df["date"].values])


def lookup_idx(df, date_str):
    ds = dates_as_str(df)
    m = np.where(ds == date_str)[0]
    return int(m[0]) if len(m) > 0 else -1


def compute_atr(high, low, close, idx, period=ATR_PERIOD):
    """Simple N-period mean true range ending at idx (inclusive).
    Returns NaN if insufficient history."""
    if idx < period:
        return np.nan
    h = high[idx - period + 1:idx + 1]
    l = low[idx - period + 1:idx + 1]
    c_prev = close[idx - period:idx]
    tr = np.maximum(h - l, np.maximum(np.abs(h - c_prev), np.abs(l - c_prev)))
    if not np.all(np.isfinite(tr)):
        return np.nan
    return float(tr.mean())


def extract_window(df, E_idx, k_max=K_MAX):
    """Return dict of (K_MAX+1,) arrays of normalized values at offsets 0..k_max.
    Offset 0 = E itself (trivially zero). None if insufficient history or bad ATR."""
    if E_idx < k_max + ATR_PERIOD:
        return None
    close = df["close"].values.astype(np.float64)
    high = df["high"].values.astype(np.float64)
    low = df["low"].values.astype(np.float64)
    close_E = close[E_idx]
    if not np.isfinite(close_E) or close_E <= 0:
        return None
    atr_E = compute_atr(high, low, close, E_idx, ATR_PERIOD)
    if not np.isfinite(atr_E) or atr_E <= 0:
        return None
    offsets = np.arange(k_max + 1)
    idx = E_idx - offsets  # shape (K_MAX+1,)
    closes_k = close[idx]
    highs_k = high[idx]
    lows_k = low[idx]
    if not np.all(np.isfinite(closes_k)) or np.any(closes_k <= 0):
        return None
    if not np.all(np.isfinite(highs_k)) or not np.all(np.isfinite(lows_k)):
        return None
    with np.errstate(all='ignore'):
        return {
            "log_close": np.log(closes_k / close_E),
            "log_high":  np.log(highs_k / close_E),
            "log_low":   np.log(lows_k / close_E),
            "atr_close": (closes_k - close_E) / atr_E,
            "atr_high":  (highs_k - close_E) / atr_E,
            "atr_low":   (lows_k - close_E) / atr_E,
        }


def get_examples(setup):
    conn = sqlite3.connect(DB)
    rows = conn.execute(
        "SELECT ticker, entry_date FROM examples WHERE setup_type=?", (setup,)
    ).fetchall()
    conn.close()
    return [{"ticker": t, "entry_date": d} for t, d in rows]


def sample_random_windows(universe, n_tickers, bars_per_ticker, k_max, rng):
    tickers = list(universe.keys())
    rng.shuffle(tickers)
    windows = []
    for t in tickers:
        df = universe.get(t)
        if df is None or len(df) < k_max + ATR_PERIOD + 20:
            continue
        valid_range = np.arange(k_max + ATR_PERIOD, len(df))
        if len(valid_range) < bars_per_ticker:
            continue
        picks = rng.choice(valid_range, size=bars_per_ticker, replace=False)
        for p in picks:
            w = extract_window(df, int(p), k_max)
            if w is not None:
                windows.append(w)
        if len(windows) >= n_tickers * bars_per_ticker:
            break
    return windows


def stack_axis(windows, axis_name):
    """Stack (n_windows, K_MAX+1) for given axis."""
    return np.vstack([w[axis_name] for w in windows])


def pct(num, den):
    if den == 0:
        return "0/0 (-)"
    return f"{num}/{den} ({num/den*100:.1f}%)"


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    print(f"OUT: {OUT_DIR}", flush=True)

    ohlcv_path = os.path.join(CACHE_DIR, "universe_ohlcv_daily.pkl")
    print(f"Loading OHLCV: {ohlcv_path}", flush=True)
    with open(ohlcv_path, 'rb') as f:
        universe = pickle.load(f)
    print(f"Universe tickers: {len(universe)}", flush=True)
    if len(universe) < 11000:
        print("FAIL: OHLCV ticker count too low", flush=True)
        sys.exit(1)

    rng = np.random.default_rng(RANDOM_SEED)
    t0 = time.time()
    print(f"Sampling random windows ({N_RANDOM_TICKERS}x{BARS_PER_TICKER})...", flush=True)
    rand_windows = sample_random_windows(universe, N_RANDOM_TICKERS, BARS_PER_TICKER, K_MAX, rng)
    print(f"Random windows: {len(rand_windows)}  ({time.time()-t0:.1f}s)", flush=True)

    rand_stack = {a: stack_axis(rand_windows, a) for a in AXES}
    n_rand = rand_stack[AXES[0]].shape[0]

    all_rows = []

    for setup in SETUP_ORDER:
        print(f"\n=== {setup.upper()} ===", flush=True)
        examples = get_examples(setup)
        print(f"  examples from db: {len(examples)}", flush=True)
        ex_windows = []
        skipped = defaultdict(int)
        for ex in examples:
            df = universe.get(ex["ticker"])
            if df is None:
                skipped["ticker_not_in_universe"] += 1
                continue
            E_idx = lookup_idx(df, ex["entry_date"])
            if E_idx < 0:
                skipped["entry_date_not_found"] += 1
                continue
            w = extract_window(df, E_idx, K_MAX)
            if w is None:
                skipped["insufficient_history_or_bad_data"] += 1
                continue
            ex_windows.append(w)
        print(f"  valid example windows: {len(ex_windows)}  skipped: {dict(skipped)}", flush=True)

        if len(ex_windows) < 3:
            print(f"  {setup}: too few valid examples — skipping", flush=True)
            continue

        ex_stack = {a: stack_axis(ex_windows, a) for a in AXES}
        n_ex = ex_stack[AXES[0]].shape[0]

        setup_rows = []
        # Track cumulative AND across offsets: random bar passes at ALL offsets 0..k
        cumulative_in = np.ones(n_rand, dtype=bool)
        for k in range(K_MAX + 1):
            row = {"setup": setup, "offset": k, "n_ex": n_ex, "n_rand": n_rand}
            in_all_axes = np.ones(n_rand, dtype=bool)
            for a in AXES:
                ex_vals = ex_stack[a][:, k]
                rand_vals = rand_stack[a][:, k]
                ex_min = float(np.nanmin(ex_vals))
                ex_max = float(np.nanmax(ex_vals))
                ex_std = float(np.nanstd(ex_vals))
                rand_std = float(np.nanstd(rand_vals))
                valid = ~np.isnan(rand_vals)
                in_band = (rand_vals >= ex_min) & (rand_vals <= ex_max) & valid
                n_in = int(in_band.sum())
                n_valid = int(valid.sum())
                row[f"{a}_ex_min"] = ex_min
                row[f"{a}_ex_max"] = ex_max
                row[f"{a}_ex_range"] = ex_max - ex_min
                row[f"{a}_ex_std"] = ex_std
                row[f"{a}_rand_std"] = rand_std
                row[f"{a}_rand_in_frac"] = n_in / max(n_valid, 1)
                in_all_axes = in_all_axes & in_band
            valid_all = ~np.any(np.stack([np.isnan(rand_stack[a][:, k]) for a in AXES]), axis=0)
            n_in_all = int((in_all_axes & valid_all).sum())
            n_valid_all = int(valid_all.sum())
            row["all_axes_rand_in_frac"] = n_in_all / max(n_valid_all, 1)
            row["n_valid_all"] = n_valid_all
            # Cumulative AND: bar passes at every offset 0..k on every axis
            cumulative_in = cumulative_in & in_all_axes & valid_all
            row["cum_and_rand_in_frac"] = float(cumulative_in.sum() / n_rand)
            # Per-offset carve contribution: delta in cum AND pass (how much this offset carves
            # beyond what previous offsets already rejected)
            if k == 0:
                row["offset_carve_delta"] = 1.0 - row["cum_and_rand_in_frac"]
            else:
                prev_cum = setup_rows[-1]["cum_and_rand_in_frac"]
                row["offset_carve_delta"] = prev_cum - row["cum_and_rand_in_frac"]
            setup_rows.append(row)
            all_rows.append(row)

        pd.DataFrame(setup_rows).to_csv(os.path.join(OUT_DIR, f"{setup}_per_offset.csv"), index=False)
        print(f"  wrote {setup}_per_offset.csv", flush=True)

    if all_rows:
        pd.DataFrame(all_rows).to_csv(os.path.join(OUT_DIR, "all_setups_per_offset.csv"), index=False)

    # ── Summary text with candidate N definitions ────────────────────────
    lines = []
    lines.append("=" * 100)
    lines.append("LEAD-UP ENVELOPE INVESTIGATION — per-offset trajectory, N candidates")
    lines.append("=" * 100)
    lines.append("")
    lines.append(f"Window: offsets 0..{K_MAX} bars BACKWARD from labeled entry E.")
    lines.append(f"Offset 0 = entry bar E itself (trivially zero on all axes).")
    lines.append(f"Axes (6): log_close, log_high, log_low (log returns vs close_E)")
    lines.append(f"          atr_close, atr_high, atr_low (displacement / ATR_E)")
    lines.append(f"Random baseline: {n_rand} random (ticker, bar) pairs treated as own faux-E.")
    lines.append("")
    lines.append("Per offset k:")
    lines.append("  - example envelope [min, max] per axis")
    lines.append("  - rand_in_frac = fraction of random windows whose value at offset k is in the ex envelope")
    lines.append("  - all_axes_rand_in_frac = fraction of random windows INSIDE the envelope on ALL 6 axes")
    lines.append("")
    lines.append("Low all_axes_rand_in_frac = envelope is discriminating at that offset")
    lines.append("Close to 1.0 = envelope has become permissive; examples have diverged.")
    lines.append("")

    def kneedle_elbow(values):
        """Find elbow of a growing curve using the kneedle (L-curve) method:
        normalize to unit square, find point with max perpendicular distance from
        the straight line between first and last points. Parameter-free."""
        v = np.array(values, dtype=np.float64)
        n = len(v)
        if n < 3:
            return 0
        # Normalize
        x = np.arange(n) / (n - 1)
        vmax = float(np.max(v)) if np.max(v) > 0 else 1.0
        vmin = float(np.min(v))
        if vmax == vmin:
            return 0
        y = (v - vmin) / (vmax - vmin)
        # Line from (0,0) to (1,1): y = x. Distance at each point: |y - x| / sqrt(2).
        # Sign: we want the point where y is MOST above the line (convex-down = curve
        # rises quickly then plateaus = distance is positive and large).
        dist = y - x
        return int(np.argmax(dist))

    def derive_N_divergence(rows):
        """N = elbow of example log_close RANGE trajectory vs offset.
        Data-derived via kneedle — no picked thresholds."""
        rows_sorted = sorted(rows, key=lambda r: r["offset"])
        log_close_range = [r["log_close_ex_range"] for r in rows_sorted]
        atr_close_range = [r["atr_close_ex_range"] for r in rows_sorted]
        log_high_range = [r["log_high_ex_range"] for r in rows_sorted]
        log_low_range = [r["log_low_ex_range"] for r in rows_sorted]
        # Average the normalized curves across axes to be robust to per-axis noise
        def normalize(arr):
            a = np.array(arr, dtype=np.float64)
            m = float(a.max()) if a.max() > 0 else 1.0
            return a / m
        combined = (normalize(log_close_range) +
                    normalize(atr_close_range) +
                    normalize(log_high_range) +
                    normalize(log_low_range)) / 4.0
        idx = kneedle_elbow(combined.tolist())
        return {
            "N_divergence": int(rows_sorted[idx]["offset"]),
            "log_close_range_at_N": float(log_close_range[idx]),
            "atr_close_range_at_N": float(atr_close_range[idx]),
        }

    def derive_N(setup, rows):
        """Candidate N definitions based on the cumulative-AND random pass trajectory."""
        rows_sorted = sorted(rows, key=lambda r: r["offset"])
        offsets = np.array([r["offset"] for r in rows_sorted])
        cum_and = np.array([r["cum_and_rand_in_frac"] for r in rows_sorted])
        deltas = np.array([r["offset_carve_delta"] for r in rows_sorted])
        cands = {}
        # (1) N_saturate: smallest k past which cum AND changes negligibly
        # Use the point where cumulative remaining pass rate plateaus — defined as
        # the first k where the remaining cum_and has decayed to under 1% AND the
        # next-5-offsets' carve deltas are all below the 10th percentile of positive
        # deltas. Self-inferring — the rule compares to the trajectory's own stats.
        positive_deltas = deltas[deltas > 0]
        delta_floor = float(np.percentile(positive_deltas, 10)) if len(positive_deltas) >= 10 else 0.0
        k_saturate = None
        for i, r in enumerate(rows_sorted):
            if r["cum_and_rand_in_frac"] < 0.01:
                forward = deltas[i+1:i+6] if i+1 < len(deltas) else []
                if len(forward) == 0 or np.all(forward <= delta_floor):
                    k_saturate = r["offset"]
                    break
        cands["N_saturate (cum AND < 1% + next 5 offsets carve negligibly)"] = k_saturate
        # (2) N_pareto: k at which 95% of the total achievable carve has been done
        if len(cum_and) > 0:
            initial = 1.0  # random starts at 100% (nothing rejected)
            final = cum_and[-1]
            total_carve = initial - final
            if total_carve > 0:
                target_cum = initial - 0.95 * total_carve
                idx = int(np.argmax(cum_and <= target_cum)) if np.any(cum_and <= target_cum) else len(cum_and) - 1
                cands["N_pareto (95% of total carve achieved)"] = int(offsets[idx])
            else:
                cands["N_pareto"] = None
        # (3) N_max_delta: k of the biggest single-offset carve contribution
        if len(deltas) > 0:
            k_max_idx = int(np.argmax(deltas))
            cands["N_max_delta (offset contributing largest single carve step)"] = int(offsets[k_max_idx])
        # (4) N_cum_half: k where cumulative AND first drops below 50% (half of random excluded)
        k_cum_half = None
        for r in rows_sorted:
            if r["cum_and_rand_in_frac"] < 0.5:
                k_cum_half = r["offset"]
                break
        cands["N_cum_half (first k where cum AND < 50%)"] = k_cum_half
        return cands

    for setup in SETUP_ORDER:
        rows = [r for r in all_rows if r["setup"] == setup]
        if not rows:
            continue
        lines.append("-" * 100)
        lines.append(f"{setup.upper()}  n_ex={rows[0]['n_ex']}  n_rand={rows[0]['n_rand']}")
        lines.append("")
        lines.append(f"{'k':>4}  {'per-off':>8}  {'cum AND':>9}  {'carve Δ':>9}  {'ex log_close':>17}  {'ex atr_close':>17}")
        lines.append(f"{'':>4}  {'rand%':>8}  {'rand%':>9}  {'vs prev':>9}  {'[min, max]':>17}  {'[min, max]':>17}")
        rows_sorted = sorted(rows, key=lambda r: r["offset"])
        printed = set()
        for r in rows_sorted[:11]:
            k = r["offset"]
            lines.append(
                f"{k:>4}  {r['all_axes_rand_in_frac']*100:>7.1f}%  "
                f"{r['cum_and_rand_in_frac']*100:>8.2f}%  "
                f"{r['offset_carve_delta']*100:>8.2f}pp  "
                f"[{r['log_close_ex_min']:+.3f},{r['log_close_ex_max']:+.3f}]  "
                f"[{r['atr_close_ex_min']:+.2f},{r['atr_close_ex_max']:+.2f}]"
            )
            printed.add(k)
        for r in rows_sorted:
            k = r["offset"]
            if k in printed or k % 5 != 0:
                continue
            lines.append(
                f"{k:>4}  {r['all_axes_rand_in_frac']*100:>7.1f}%  "
                f"{r['cum_and_rand_in_frac']*100:>8.2f}%  "
                f"{r['offset_carve_delta']*100:>8.2f}pp  "
                f"[{r['log_close_ex_min']:+.3f},{r['log_close_ex_max']:+.3f}]  "
                f"[{r['atr_close_ex_min']:+.2f},{r['atr_close_ex_max']:+.2f}]"
            )
        lines.append("")
        # Final pass rate through the full window
        final_cum = rows_sorted[-1]["cum_and_rand_in_frac"]
        lines.append(f"  Full-window AND pass (random): {final_cum*100:.3f}% (K_MAX={K_MAX})")
        # Divergence-based N (Dan's definition: where examples start diverging)
        div = derive_N_divergence(rows)
        N_div = div["N_divergence"]
        cum_at_N = [r["cum_and_rand_in_frac"] for r in rows_sorted if r["offset"] == N_div]
        cum_at_N_val = cum_at_N[0] if cum_at_N else float('nan')
        lines.append(f"  N_divergence (elbow in example range trajectory): N = {N_div}")
        lines.append(f"    log_close range at N = {div['log_close_range_at_N']:.3f}")
        lines.append(f"    atr_close range at N = {div['atr_close_range_at_N']:.2f}")
        lines.append(f"    cum AND random pass at N = {cum_at_N_val*100:.3f}%")
        cands = derive_N(setup, rows)
        lines.append("  Candidate N (bars of lead-up):")
        for name, val in cands.items():
            lines.append(f"    {name}: N = {val}")
        lines.append("")

    lines.append("=" * 100)
    lines.append("INTERPRETATION GUIDE")
    lines.append("=" * 100)
    lines.append("")
    lines.append("Read columns:")
    lines.append("  per-off rand%: fraction of random bars that fit the example envelope AT that")
    lines.append("    single offset across all 6 axes. Low = envelope is tight at that offset.")
    lines.append("  cum AND rand%: fraction of random bars still passing after including offsets 0..k.")
    lines.append("    Monotonically decreasing. This is the ACTUAL filter pass rate if you cut at k.")
    lines.append("  carve Δ: how much this offset subtracted from the running pass rate (in pp).")
    lines.append("    Big Δ = this offset contributes real incremental discrimination.")
    lines.append("")
    lines.append("Candidate N definitions (all data-derived, no picked thresholds):")
    lines.append("  N_saturate: cum AND has decayed under 1% AND the next five offsets add nothing")
    lines.append("    meaningful. Picks where lengthening the window stops buying anything.")
    lines.append("  N_pareto:   95% of total achievable carve is done. Pareto-optimal window length.")
    lines.append("  N_max_delta: offset of the biggest single-offset carve step. (Diagnostic only,")
    lines.append("    not a stopping point — marks the most discriminating single bar.)")
    lines.append("  N_cum_half: first k where cum AND drops under 50%. Very coarse check.")
    lines.append("")
    lines.append("Expected read: if lead-up shape is coherent over a real window, cum AND trajectory")
    lines.append("  starts near 1.0, drops fast, and plateaus near 0. N_pareto identifies plateau.")
    lines.append("  If cum AND stays high (> 30%) through k=100, the envelope is weak for that setup.")
    lines.append("")
    lines.append("Dan picks which N definition to adopt; the report gives the data to pick from.")

    with open(os.path.join(OUT_DIR, "summary.txt"), 'w', encoding='utf-8') as f:
        f.write("\n".join(lines))
    print(f"\nWrote summary.txt ({len(lines)} lines)", flush=True)
    print("DONE.", flush=True)


if __name__ == "__main__":
    main()
