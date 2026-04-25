"""Audit the per-setup envelope: where do examples sit (frontier vs interior),
and how tight are the bands.

For each labeled example in a setup, re-extract its N-bar lead-up window
across the relevant feature set. At every (offset, feature) with non-NaN
example value and band, classify the example's position:
  - at_min  : example value == ex_min
  - at_max  : example value == ex_max
  - interior: strictly between

Per-band properties:
  - degenerate (ex_min == ex_max at that cell)
  - width relative to the per-feature max observed across offsets

Output: research/n_derivation_cache/envelope_diag_{setup}.txt
        research/n_derivation_cache/envelope_diag_summary.txt
"""
from __future__ import annotations

import os
import sys
import sqlite3
import time
from collections import defaultdict

import numpy as np
import pandas as pd

MAIN_ROOT = r"C:/Users/Dan/Documents/ScanPerfect/swing-screener"
CACHE_DIR = os.path.join(MAIN_ROOT, "local_runner", "cache")
DB = os.path.join(MAIN_ROOT, "data", "scanperfect.db")
WORKTREE = r"C:/Users/Dan/Documents/ScanPerfect/swing-screener-win-loss-classifier"
ENV_DIR = os.path.join(WORKTREE, "research", "n_derivation_cache")

sys.path.insert(0, os.path.join(MAIN_ROOT, "local_runner"))
from expr_cache_builder import ExprSeriesCache  # noqa: E402

SETUP_ORDER = ["htf", "bf", "base", "dtss", "3-4db"]
K_MAX_FOR_CHECK = 120
EPS = 1e-8  # tolerance for float equality


def get_examples(setup):
    conn = sqlite3.connect(DB)
    rows = conn.execute(
        "SELECT ticker, entry_date FROM examples WHERE setup_type=?", (setup,)
    ).fetchall()
    conn.close()
    return [{"ticker": t, "entry_date": d} for t, d in rows]


def dates_as_str(dates):
    if pd.api.types.is_datetime64_any_dtype(dates):
        return pd.to_datetime(dates).strftime('%Y-%m-%d').values.astype('<U10')
    return np.array([str(d)[:10] for d in dates], dtype='<U10')


def extract_example_window(ec, ticker, entry_date, N, feature_indices):
    """Return (N, n_rel) array where row 0 = E-1, row N-1 = E-N.
    Returns None if ticker/date not found or insufficient history.
    """
    dates, data = ec.get_ticker(ticker)
    if dates is None:
        return None
    ds = dates_as_str(dates)
    m = np.where(ds == str(entry_date)[:10])[0]
    if len(m) == 0:
        return None
    E_idx = int(m[0])
    if E_idx < N:
        return None
    # Rows E_idx-N..E_idx-1, oldest first; reverse so row 0 = E-1
    win = data[E_idx - N:E_idx, feature_indices].astype(np.float32)
    return win[::-1, :]


def diagnostic_for_setup(setup, ec):
    env_path = os.path.join(ENV_DIR, f"{setup}_envelope.npz")
    if not os.path.exists(env_path):
        return None, f"no envelope for {setup}"
    env = np.load(env_path)
    ex_min = env["ex_min"].astype(np.float32)  # (N, n_rel)
    ex_max = env["ex_max"].astype(np.float32)
    feat_idx = env["feature_indices"]
    N, n_rel = ex_min.shape

    # Band-level stats
    band_cov_mask = ~np.isnan(ex_min) & ~np.isnan(ex_max)
    n_bands_total = N * n_rel
    n_bands_covered = int(band_cov_mask.sum())
    band_width = np.where(band_cov_mask, ex_max - ex_min, np.nan).astype(np.float32)
    n_degenerate = int(np.sum(band_cov_mask & (band_width <= EPS)))
    # Per-feature dynamic range (max range across offsets) — approximates how
    # much the feature varies in this setup at all. Near-zero = inert.
    feat_max_range = np.nanmax(band_width, axis=0)  # (n_rel,)
    n_features_inert = int(np.sum(feat_max_range <= EPS))

    # Per-example frontier analysis
    examples = get_examples(setup)
    per_ex_rows = []
    for ex in examples:
        win = extract_example_window(ec, ex["ticker"], ex["entry_date"], N, feat_idx)
        if win is None:
            per_ex_rows.append({
                "ticker": ex["ticker"], "date": str(ex["entry_date"])[:10],
                "status": "DROPPED (insufficient history or missing)",
            })
            continue
        measured = ~np.isnan(win) & band_cov_mask
        # Equality within tolerance
        at_min = measured & (np.abs(win - ex_min) <= EPS)
        at_max = measured & (np.abs(win - ex_max) <= EPS)
        # If min == max (degenerate), count as at_min (not both)
        degenerate_cells = band_cov_mask & (band_width <= EPS)
        at_max_only = at_max & ~at_min
        interior = measured & ~at_min & ~at_max
        per_ex_rows.append({
            "ticker": ex["ticker"],
            "date": str(ex["entry_date"])[:10],
            "status": "ok",
            "n_measured": int(measured.sum()),
            "n_at_min": int(at_min.sum()),
            "n_at_max": int(at_max_only.sum()),
            "n_interior": int(interior.sum()),
            "n_on_degenerate": int((at_min & degenerate_cells).sum()),
        })

    # Aggregate
    lines = []
    lines.append("=" * 80)
    lines.append(f"{setup.upper()}  envelope diagnostic")
    lines.append("=" * 80)
    lines.append(f"N_bars={N}  n_relevant_features={n_rel}  total_bands={n_bands_total:,}")
    lines.append(f"bands covered (>= min_ex_measured): {n_bands_covered:,} "
                 f"({n_bands_covered / n_bands_total * 100:.1f}%)")
    lines.append(f"degenerate bands (ex_min == ex_max): {n_degenerate:,} "
                 f"({n_degenerate / max(n_bands_covered, 1) * 100:.1f}% of covered)")
    lines.append(f"inert features (max range across offsets <= {EPS}): "
                 f"{n_features_inert} / {n_rel} "
                 f"({n_features_inert / n_rel * 100:.1f}%)")
    # Band width quantiles on non-degenerate bands
    nondeg = band_width[band_cov_mask & (band_width > EPS)]
    if len(nondeg) > 0:
        q = np.quantile(nondeg, [0.01, 0.05, 0.25, 0.5, 0.75, 0.95, 0.99])
        lines.append("non-degenerate band width quantiles (absolute units): "
                     f"p1={q[0]:.4g} p5={q[1]:.4g} p25={q[2]:.4g} p50={q[3]:.4g} "
                     f"p75={q[4]:.4g} p95={q[5]:.4g} p99={q[6]:.4g}")
    lines.append("")

    ok_rows = [r for r in per_ex_rows if r["status"] == "ok"]
    dropped_rows = [r for r in per_ex_rows if r["status"] != "ok"]

    # Per-example headline
    lines.append("Per-example frontier positions (out of n_measured cells):")
    lines.append(f"  {'ticker':<8} {'date':<12} {'meas':>8} {'at_min':>8} {'at_max':>8} "
                 f"{'interior':>10} {'on_deg':>8} {'front%':>8} {'int%':>8}")
    for r in ok_rows:
        m = r["n_measured"]
        front = r["n_at_min"] + r["n_at_max"]
        front_pct = front / max(m, 1) * 100
        int_pct = r["n_interior"] / max(m, 1) * 100
        lines.append(f"  {r['ticker']:<8} {r['date']:<12} {m:>8,} "
                     f"{r['n_at_min']:>8,} {r['n_at_max']:>8,} {r['n_interior']:>10,} "
                     f"{r['n_on_degenerate']:>8,} {front_pct:>7.2f}% {int_pct:>7.2f}%")
    for r in dropped_rows:
        lines.append(f"  {r['ticker']:<8} {r['date']:<12}  {r['status']}")
    lines.append("")

    # Aggregate frontier share
    if ok_rows:
        total_meas = sum(r["n_measured"] for r in ok_rows)
        total_front = sum(r["n_at_min"] + r["n_at_max"] for r in ok_rows)
        total_int = sum(r["n_interior"] for r in ok_rows)
        total_deg = sum(r["n_on_degenerate"] for r in ok_rows)
        lines.append(f"Aggregate across {len(ok_rows)} examples:")
        lines.append(f"  total measured cells: {total_meas:,}")
        lines.append(f"  on min/max frontier:  {total_front:,} "
                     f"({total_front / total_meas * 100:.1f}%)")
        lines.append(f"  strictly interior:    {total_int:,} "
                     f"({total_int / total_meas * 100:.1f}%)")
        lines.append(f"  on degenerate cells (auto-at-min+at-max): {total_deg:,} "
                     f"({total_deg / total_meas * 100:.1f}%)")
        # Examples fully on frontier (zero interior)
        fully_frontier = sum(1 for r in ok_rows if r["n_interior"] == 0)
        lines.append(f"  examples with 0 interior cells: {fully_frontier} / {len(ok_rows)}")
    lines.append("")

    return per_ex_rows, "\n".join(lines)


def main():
    os.makedirs(ENV_DIR, exist_ok=True)
    print(f"OUT: {ENV_DIR}", flush=True)

    print("Opening expression cache...", flush=True)
    ec = ExprSeriesCache()
    if not ec.is_valid():
        print("FAIL: expr cache invalid")
        sys.exit(1)

    rollup = []
    rollup.append("=" * 80)
    rollup.append("ENVELOPE DIAGNOSTIC — frontier vs interior, band widths")
    rollup.append("=" * 80)
    rollup.append(f"{'setup':<8}{'N':<5}{'n_rel':<8}{'covered':<12}{'degen%':<10}"
                  f"{'frontier%':<12}{'interior%':<12}{'full_front':<12}")
    totals_by_setup = {}

    for setup in SETUP_ORDER:
        print(f"\n=== {setup.upper()} ===", flush=True)
        t0 = time.time()
        per_ex, text = diagnostic_for_setup(setup, ec)
        if per_ex is None:
            print(f"  {text}", flush=True)
            continue
        out_path = os.path.join(ENV_DIR, f"envelope_diag_{setup}.txt")
        with open(out_path, 'w', encoding='utf-8') as f:
            f.write(text)
        print(f"  wrote {out_path} ({time.time() - t0:.1f}s)", flush=True)

        ok_rows = [r for r in per_ex if r["status"] == "ok"]
        if ok_rows:
            total_meas = sum(r["n_measured"] for r in ok_rows)
            total_front = sum(r["n_at_min"] + r["n_at_max"] for r in ok_rows)
            total_int = sum(r["n_interior"] for r in ok_rows)
            fully_frontier = sum(1 for r in ok_rows if r["n_interior"] == 0)

        env = np.load(os.path.join(ENV_DIR, f"{setup}_envelope.npz"))
        ex_min = env["ex_min"]
        ex_max = env["ex_max"]
        N, n_rel = ex_min.shape
        band_cov_mask = ~np.isnan(ex_min) & ~np.isnan(ex_max)
        band_width = np.where(band_cov_mask, ex_max - ex_min, np.nan)
        n_cov = int(band_cov_mask.sum())
        n_deg = int(np.sum(band_cov_mask & (band_width <= EPS)))
        cov_pct = n_cov / (N * n_rel) * 100
        deg_pct = n_deg / max(n_cov, 1) * 100
        front_pct = total_front / max(total_meas, 1) * 100 if ok_rows else 0.0
        int_pct = total_int / max(total_meas, 1) * 100 if ok_rows else 0.0

        rollup.append(f"{setup:<8}{N:<5}{n_rel:<8}{cov_pct:<11.1f}%"
                      f"{deg_pct:<9.1f}%"
                      f"{front_pct:<11.1f}%"
                      f"{int_pct:<11.1f}%"
                      f"{fully_frontier}/{len(ok_rows):<6}")

    rollup_path = os.path.join(ENV_DIR, "envelope_diag_summary.txt")
    with open(rollup_path, 'w', encoding='utf-8') as f:
        f.write("\n".join(rollup))
    print(f"\nWrote rollup to {rollup_path}", flush=True)
    print("\n" + "\n".join(rollup), flush=True)


if __name__ == "__main__":
    main()
