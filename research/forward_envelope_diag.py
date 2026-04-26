"""Forward-envelope labeler diagnostic — measure presignal-style envelope on FORWARD window.

Mirrors presignal §4 mechanic but applied forward of entry_bar, not back of signal_bar.
PURE MEASUREMENT. Writes diagnostic JSON + matplotlib figure. No labels written.

For HTF only:
  1. Read HALTED HTF JSON for cluster meta (entered clusters with horizon, cap_bar, etc.).
  2. For each ENTERED cluster, compute forward log-ratio per (MA_period, offset_k) cell.
       lr(p, k) = log(MA_p[entry_bar + k] / MA_p[entry_bar])  for k=1..horizon
  3. Build bands: Lower(p,k) = nanmin over examples; Upper(p,k) = nanmax.
  4. Diagnostics:
       - Per-cell band width distribution.
       - SVD effective rank on (n_examples, n_cells) feature matrix.
       - LOO drop: cells where each held-out example contracts past N-1 band.
       - Wild admission rate (NaN-lenient strict-AND across all cells).
       - Sanity cross-tabs: (5)-verdict vs mfe_adr, stop_hit_bar, top-50 rule consensus.
  5. Visual: forward tape overlay (28 examples + sample wild) and per-cell band sketch.
"""
from __future__ import annotations

import json
import os
import pickle
import sys
import time
from typing import Dict, List, Tuple

import numpy as np

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

MAIN_REPO = r"C:/Users/Dan/Documents/ScanPerfect/swing-screener"
HALTED_PATH = os.path.join(
    MAIN_REPO, "data", "signal_exit_grind",
    "signal_exit_pool_htf_HALTED_20260425_202509.json"
)
OHLCV_PATH = os.path.join(MAIN_REPO, "local_runner", "cache", "universe_ohlcv_daily.pkl")
OUT_DIR = os.path.join(MAIN_REPO, "research", "forward_envelope_diag")
os.makedirs(OUT_DIR, exist_ok=True)

# Same MA basis presignal §4 uses (daily only for first pass).
SMA_PERIODS = [5, 8, 10, 13, 20, 30, 50, 100, 150, 200]
EMA_PERIODS = [3, 5, 8, 10, 13, 20, 30, 50, 100, 150, 200]


# ───────────────────────── MA compute ─────────────────────────
def rolling_sma(close: np.ndarray, p: int) -> np.ndarray:
    L = len(close)
    c = np.where(np.isfinite(close) & (close > 0), close, np.nan)
    cs = np.concatenate([[0.0], np.cumsum(np.where(np.isfinite(c), c, 0.0))])
    co = np.concatenate([[0], np.cumsum(np.isfinite(c).astype(np.int64))])
    out = np.full(L, np.nan)
    if L < p:
        return out
    t_arr = np.arange(p - 1, L)
    lo_arr = t_arr - p + 1
    ok = co[t_arr + 1] - co[lo_arr]
    s = cs[t_arr + 1] - cs[lo_arr]
    out[t_arr] = np.where(ok == p, s / p, np.nan)
    return out


def rolling_ema(close: np.ndarray, p: int) -> np.ndarray:
    L = len(close)
    c = np.where(np.isfinite(close) & (close > 0), close, np.nan)
    alpha = 2.0 / (p + 1.0)
    ema = np.full(L, np.nan)
    first = -1
    for t in range(L):
        if np.isfinite(c[t]):
            first = t
            break
    if first < 0:
        return ema
    ema[first] = c[first]
    for t in range(first + 1, L):
        v = c[t]
        prev = ema[t - 1]
        if np.isfinite(v) and np.isfinite(prev):
            ema[t] = alpha * v + (1 - alpha) * prev
        elif np.isfinite(v):
            ema[t] = v
        else:
            ema[t] = prev
    return ema


# ───────────────── Build forward log-ratio tensors ─────────────────
def build_forward_lr(meta_entered: List[dict], ohlcv: dict,
                     ma_specs: List[Tuple[str, int]]) -> Tuple[np.ndarray, np.ndarray, int]:
    """Returns (lr_tensor, mask, max_h) where:
       lr_tensor: (n_clusters, max_h, n_MAs) of log(MA[entry+k]/MA[entry]).
       mask: (n_clusters, max_h) — True if k <= horizon for that cluster.
       max_h: max horizon across all clusters."""
    n = len(meta_entered)
    n_ma = len(ma_specs)
    horizons = [m["horizon"] for m in meta_entered]
    max_h = max(horizons)
    lr = np.full((n, max_h, n_ma), np.nan, dtype=np.float64)
    mask = np.zeros((n, max_h), dtype=bool)

    for i, m in enumerate(meta_entered):
        ticker = m["ticker"]
        df = ohlcv.get(ticker)
        if df is None:
            continue
        close = df["close"].values.astype(np.float64)
        entry_bar = m["entry_bar"]
        cap_bar = m["cap_bar"]
        h = m["horizon"]  # = cap_bar - entry_bar
        mask[i, :h] = True

        for j, (kind, p) in enumerate(ma_specs):
            if kind == "sma":
                ma = rolling_sma(close, p)
            else:
                ma = rolling_ema(close, p)
            anchor = ma[entry_bar]
            if not np.isfinite(anchor) or anchor <= 0:
                continue  # leave row j as NaN — example/wild has insufficient warmup
            fwd = ma[entry_bar + 1: cap_bar + 1]
            with np.errstate(invalid="ignore", divide="ignore"):
                lr_vals = np.log(fwd / anchor)
            lr[i, :h, j] = lr_vals

    return lr, mask, max_h


# ───────────────────────── Diagnostics ─────────────────────────
def per_cell_lower(lr_ex: np.ndarray) -> np.ndarray:
    """One-sided: only Lower(p,k) = nanmin over examples. (Fix A)"""
    with np.errstate(invalid="ignore"):
        lower = np.nanmin(lr_ex, axis=0)
    return lower


def svd_effective_rank(lr_ex: np.ndarray) -> dict:
    n_ex, max_h, n_ma = lr_ex.shape
    flat = lr_ex.reshape(n_ex, max_h * n_ma)
    cell_mean = np.nanmean(flat, axis=0)
    cell_mean = np.where(np.isfinite(cell_mean), cell_mean, 0.0)
    filled = np.where(np.isfinite(flat), flat, cell_mean[None, :])
    centered = filled - filled.mean(axis=0, keepdims=True)
    U, S, Vt = np.linalg.svd(centered, full_matrices=False)
    energy = S ** 2
    cum = np.cumsum(energy) / energy.sum()
    return {
        "singular_values": [float(s) for s in S],
        "cum_energy": [float(c) for c in cum],
        "rank_50pct": int(np.argmax(cum >= 0.5) + 1),
        "rank_90pct": int(np.argmax(cum >= 0.9) + 1),
        "rank_99pct": int(np.argmax(cum >= 0.99) + 1),
        "n_examples": int(n_ex),
        "n_cells": int(max_h * n_ma),
    }


def loo_lower_drop(lr_ex: np.ndarray) -> dict:
    """For each example i, rebuild Lower from remaining N-1, count cells where i falls below it.
       (One-sided LOO drop under Fix A.)"""
    n_ex, max_h, n_ma = lr_ex.shape
    drops_per_ex = []
    for i in range(n_ex):
        mask = np.arange(n_ex) != i
        sub = lr_ex[mask]
        with np.errstate(invalid="ignore"):
            sub_lower = np.nanmin(sub, axis=0)
        held = lr_ex[i]
        out = (held < sub_lower)
        out = np.where(np.isnan(held), False, out)
        drops_per_ex.append(int(out.sum()))
    return {
        "drops_per_example": drops_per_ex,
        "median": float(np.median(drops_per_ex)),
        "mean": float(np.mean(drops_per_ex)),
        "max": int(max(drops_per_ex)),
        "min": int(min(drops_per_ex)),
        "p90": float(np.percentile(drops_per_ex, 90)),
    }


def effective_test_horizon(meta_list: List[dict]) -> np.ndarray:
    """Fix B: test window goes only up to the stop bar (inclusive) for stopped trades,
       full horizon for non-stopped. stop_hit_bar is j-indexed (0 = entry+1).
       stop_hit_bar = horizon means no stop hit. Returns: per-cluster effective horizon
       in number of bars (so test j=0..eff_h-1)."""
    out = np.empty(len(meta_list), dtype=np.int64)
    for i, m in enumerate(meta_list):
        h = m["horizon"]
        sh = m["stop_hit_bar"] if m["stop_hit_bar"] is not None else h
        # Inclusive of the stop bar: window goes j=0..sh, so length = sh+1.
        # If no stop (sh = h), window is full (length = h).
        out[i] = min(sh + 1, h)
    return out


def admission_lower_only(lr_target: np.ndarray, lower: np.ndarray,
                         eff_horizons: np.ndarray) -> Tuple[np.ndarray, np.ndarray]:
    """One-sided lower envelope (Fix A) + stop-restricted window (Fix B).
       Returns (n_pass, n_active) per target."""
    n, max_h, n_ma = lr_target.shape
    n_pass = np.zeros(n, dtype=np.int64)
    n_active = np.zeros(n, dtype=np.int64)
    for i in range(n):
        h = int(eff_horizons[i])
        x = lr_target[i, :h]
        l = lower[:h]
        active = np.isfinite(x) & np.isfinite(l)
        passed = (x >= l)
        passed = passed | ~active
        n_active[i] = int(active.sum())
        n_pass[i] = int(passed.sum())
    return n_pass, n_active


def lower_only_verdict(lr_target: np.ndarray, lower: np.ndarray,
                       eff_horizons: np.ndarray) -> np.ndarray:
    """Per-target verdict under Fix A + Fix B. True = passes Lower at every active cell in window."""
    n, max_h, n_ma = lr_target.shape
    out = np.zeros(n, dtype=bool)
    for i in range(n):
        h = int(eff_horizons[i])
        if h == 0:
            out[i] = True  # vacuous
            continue
        x = lr_target[i, :h]
        l = lower[:h]
        active = np.isfinite(x) & np.isfinite(l)
        if not active.any():
            out[i] = True
            continue
        passed = (x >= l)
        passed = passed | ~active
        out[i] = bool(passed.all())
    return out


def per_ma_carving(lr_target: np.ndarray, lower: np.ndarray,
                   eff_horizons: np.ndarray, ext_label: np.ndarray,
                   ma_specs: List[Tuple[str, int]]) -> List[dict]:
    """For each MA in basis: measure how much that MA alone discriminates ext_label.
       ext_label: external WIN(1)/LOSS(0) label per target — independent of envelope.
       For each (MA, offset) cell, compute Cohen's d between ext_label=1 and =0 lr distributions.
       Average |d| across offsets per MA.
       Plus single-MA-only admission rate gap WIN vs LOSS."""
    n, max_h, n_ma = lr_target.shape
    results = []
    for j, (kind, p) in enumerate(ma_specs):
        # For each offset k, gather lr values per ext_label class. Restrict each cluster's
        # contribution to k < eff_horizon[i] (Fix B).
        ds = []
        ds_signed = []
        for k in range(max_h):
            mask_in_window = np.array([k < eff_horizons[i] for i in range(n)])
            x = lr_target[:, k, j]
            x_ok = np.isfinite(x) & mask_in_window
            v_w = x[x_ok & (ext_label == 1)]
            v_l = x[x_ok & (ext_label == 0)]
            if len(v_w) < 5 or len(v_l) < 5:
                continue
            mw = float(np.mean(v_w)); ml = float(np.mean(v_l))
            sw = float(np.std(v_w, ddof=1)); sl = float(np.std(v_l, ddof=1))
            pooled = np.sqrt(((len(v_w) - 1) * sw**2 + (len(v_l) - 1) * sl**2)
                             / max(len(v_w) + len(v_l) - 2, 1))
            if pooled == 0:
                continue
            d = (mw - ml) / pooled
            ds.append(abs(d)); ds_signed.append(d)
        # Single-MA-only admission: build single-MA verdict (lower-only over this MA's offsets only).
        single = lower_only_verdict(lr_target[:, :, j:j+1], lower[:, j:j+1], eff_horizons)
        ad_w = single[ext_label == 1].mean() if (ext_label == 1).any() else None
        ad_l = single[ext_label == 0].mean() if (ext_label == 0).any() else None
        results.append({
            "ma": f"{kind.upper()}{p}",
            "mean_abs_cohen_d": float(np.mean(ds)) if ds else None,
            "median_abs_cohen_d": float(np.median(ds)) if ds else None,
            "mean_signed_cohen_d": float(np.mean(ds_signed)) if ds_signed else None,
            "n_offsets_measured": len(ds),
            "single_ma_admission_win": float(ad_w) if ad_w is not None else None,
            "single_ma_admission_loss": float(ad_l) if ad_l is not None else None,
            "single_ma_admission_gap": (float(ad_w) - float(ad_l)) if (ad_w is not None and ad_l is not None) else None,
        })
    return results


# ───────────────────────── Main ─────────────────────────
def main():
    print("Loading HALTED HTF JSON...")
    halted = json.load(open(HALTED_PATH))
    meta = halted["cluster_meta"]
    entered = [m for m in meta if m["status"] == "ENTERED"]
    examples = [m for m in entered if m["is_example"] == 1]
    wild = [m for m in entered if m["is_example"] == 0]
    print(f"  entered clusters: {len(entered)}  examples: {len(examples)}  wild: {len(wild)}")
    print(f"  example horizons: min={min(m['horizon'] for m in examples)}  "
          f"max={max(m['horizon'] for m in examples)}  "
          f"median={np.median([m['horizon'] for m in examples]):.0f}")

    print(f"Loading OHLCV...")
    with open(OHLCV_PATH, "rb") as f:
        ohlcv = pickle.load(f)
    print(f"  OHLCV tickers: {len(ohlcv):,}")

    ma_specs: List[Tuple[str, int]] = (
        [("sma", p) for p in SMA_PERIODS] + [("ema", p) for p in EMA_PERIODS]
    )
    print(f"  MA basis: {len(ma_specs)} types  ({len(SMA_PERIODS)} SMA + {len(EMA_PERIODS)} EMA)")

    print("Building forward lr tensors...")
    t0 = time.time()
    lr_ex, mask_ex, max_h_ex = build_forward_lr(examples, ohlcv, ma_specs)
    lr_wild, mask_wild, max_h_wild = build_forward_lr(wild, ohlcv, ma_specs)
    print(f"  lr_ex shape {lr_ex.shape}  max_h={max_h_ex}  in {time.time()-t0:.1f}s")
    print(f"  lr_wild shape {lr_wild.shape}  max_h={max_h_wild}")

    # Pad whichever has shorter max_h to align with the larger.
    if max_h_ex < max_h_wild:
        pad = np.full((lr_ex.shape[0], max_h_wild - max_h_ex, lr_ex.shape[2]), np.nan)
        lr_ex = np.concatenate([lr_ex, pad], axis=1)
        pad_m = np.zeros((lr_ex.shape[0], max_h_wild - max_h_ex), dtype=bool)
        mask_ex = np.concatenate([mask_ex, pad_m], axis=1)
    elif max_h_wild < max_h_ex:
        pad = np.full((lr_wild.shape[0], max_h_ex - max_h_wild, lr_wild.shape[2]), np.nan)
        lr_wild = np.concatenate([lr_wild, pad], axis=1)
        pad_m = np.zeros((lr_wild.shape[0], max_h_ex - max_h_wild), dtype=bool)
        mask_wild = np.concatenate([mask_wild, pad_m], axis=1)
    max_h = max(max_h_ex, max_h_wild)
    print(f"  unified max_h={max_h}")

    print("Building bands (Fix A: one-sided Lower only)...")
    lower = per_cell_lower(lr_ex)
    n_cells_active_lower = int(np.isfinite(lower).sum())
    print(f"  Lower band cells active: {n_cells_active_lower} / {lower.size}")
    print(f"  Lower band: median={np.nanmedian(lower):.4f}  p25={np.nanpercentile(lower,25):.4f}  "
          f"p75={np.nanpercentile(lower,75):.4f}  p95={np.nanpercentile(lower,95):.4f}")

    # Effective test horizons (Fix B)
    eff_h_ex = effective_test_horizon(examples)
    eff_h_wild = effective_test_horizon(wild)
    print(f"\n  Effective horizons (Fix B: window=1..stop_hit_bar):")
    print(f"    examples (never stop):  min={eff_h_ex.min()}  median={int(np.median(eff_h_ex))}  max={eff_h_ex.max()}")
    print(f"    wild:                   min={eff_h_wild.min()}  median={int(np.median(eff_h_wild))}  max={eff_h_wild.max()}")

    print("\nVerifying examples lock by construction (Fix A + Fix B)...")
    ex_verdict = lower_only_verdict(lr_ex, lower, eff_h_ex)
    ex_pass = int(ex_verdict.sum())
    print(f"  examples passing lower-only strict-AND: {ex_pass} / {len(examples)} (must be 28/28)")
    if ex_pass != len(examples):
        for i, p in enumerate(ex_verdict):
            if not p:
                print(f"    FAIL ex {i} ({examples[i]['ticker']} cluster {examples[i]['cluster_id']})")

    print("\nSVD effective rank (unchanged from v1 — band primitive does not affect this)...")
    svd = svd_effective_rank(lr_ex)
    print(f"  rank @ 50%/90%/99%: {svd['rank_50pct']} / {svd['rank_90pct']} / {svd['rank_99pct']}")

    print("\nLOO drop pattern (one-sided lower)...")
    loo = loo_lower_drop(lr_ex)
    print(f"  drops per held-out example: median={loo['median']:.0f}  "
          f"mean={loo['mean']:.1f}  min={loo['min']}  max={loo['max']}  p90={loo['p90']:.0f}")

    print("\nWild admission (Fix A + Fix B)...")
    wild_pass, wild_active = admission_lower_only(lr_wild, lower, eff_h_wild)
    wild_verdict = lower_only_verdict(lr_wild, lower, eff_h_wild)
    n_wild_pass = int(wild_verdict.sum())
    print(f"  wild WIN under (5'): {n_wild_pass} / {len(wild)}  ({100*n_wild_pass/len(wild):.1f}%)")
    print(f"  wild pass-rate distribution:  "
          f"min_pass={int(wild_pass.min())}  max_pass={int(wild_pass.max())}  "
          f"median={int(np.median(wild_pass))}")

    print("\nSanity cross-tabs...")
    # 1. (5)-verdict vs mfe_adr / stop_hit_bar / cap_cause
    mfe_arr = np.array([m["mfe_adr"] for m in wild])
    stop_arr = np.array([m["stop_hit_bar"] if m["stop_hit_bar"] is not None else m["horizon"]
                          for m in wild])
    horiz_arr = np.array([m["horizon"] for m in wild])
    early_stop = stop_arr < horiz_arr
    print(f"  WILD WIN  (under (5)):  mfe median={np.median(mfe_arr[wild_verdict]):.2f}  "
          f"stop_hit median={np.median(stop_arr[wild_verdict]):.0f}  "
          f"early_stop_rate={100*early_stop[wild_verdict].mean():.1f}%")
    print(f"  WILD LOSS (under (5)):  mfe median={np.median(mfe_arr[~wild_verdict]):.2f}  "
          f"stop_hit median={np.median(stop_arr[~wild_verdict]):.0f}  "
          f"early_stop_rate={100*early_stop[~wild_verdict].mean():.1f}%")

    # 2. (5)-verdict vs HALTED top-50 rule consensus
    top = halted["top_conditions"]
    # Per cluster, how many of top-50 said WIN
    n_top = len(top)
    cluster_id_to_idx = {m["cluster_id"]: i for i, m in enumerate(meta)}
    rule_consensus_wild = np.zeros(len(wild), dtype=np.int64)
    for w_i, m in enumerate(wild):
        cid = m["cluster_id"]
        pool_i = cluster_id_to_idx[cid]
        win_count = sum(
            1 for r in top
            if r["per_cluster_final_label"][pool_i] == "WIN"
        )
        rule_consensus_wild[w_i] = win_count
    print(f"  HALTED top-{n_top} rule consensus on WIN clusters under (5):  "
          f"median={int(np.median(rule_consensus_wild[wild_verdict]))}/{n_top}")
    print(f"  HALTED top-{n_top} rule consensus on LOSS clusters under (5):  "
          f"median={int(np.median(rule_consensus_wild[~wild_verdict]))}/{n_top}")

    # Examples — same diagnostic for completeness
    rule_consensus_ex = np.zeros(len(examples), dtype=np.int64)
    for w_i, m in enumerate(examples):
        pool_i = cluster_id_to_idx[m["cluster_id"]]
        rule_consensus_ex[w_i] = sum(
            1 for r in top if r["per_cluster_final_label"][pool_i] == "WIN"
        )
    print(f"  HALTED top-{n_top} rule consensus on examples (all label WIN under (5)):  "
          f"median={int(np.median(rule_consensus_ex))}/{n_top}  "
          f"min={int(rule_consensus_ex.min())}  max={int(rule_consensus_ex.max())}")

    # ─────────────────── Per-MA carving check ───────────────────
    print("\nPer-MA carving check (uses HALTED top-50 rule consensus as external label)...")
    # External label: rule-vocabulary consensus on wild + examples
    ext_label_wild = (rule_consensus_wild >= 25).astype(np.int64)
    ext_label_ex = (rule_consensus_ex >= 25).astype(np.int64)
    # Combine for richer signal
    lr_all = np.concatenate([lr_ex, lr_wild], axis=0)
    eff_h_all = np.concatenate([eff_h_ex, eff_h_wild])
    ext_all = np.concatenate([ext_label_ex, ext_label_wild])
    print(f"  external WIN/LOSS by rule consensus (>=25/50):  "
          f"WIN={int(ext_all.sum())} / LOSS={int((1-ext_all).sum())}")
    per_ma = per_ma_carving(lr_all, lower, eff_h_all, ext_all, ma_specs)
    per_ma_sorted = sorted(per_ma, key=lambda r: -(r["mean_abs_cohen_d"] or 0))
    print(f"  Top 10 MAs by mean |Cohen's d| (averaged across offsets):")
    print(f"    {'MA':>8s}  {'mean|d|':>8s}  {'median|d|':>9s}  {'signed_d':>9s}  {'admit_W':>8s}  {'admit_L':>8s}  {'gap':>6s}")
    for r in per_ma_sorted[:10]:
        print(f"    {r['ma']:>8s}  "
              f"{(r['mean_abs_cohen_d'] or 0):>8.3f}  "
              f"{(r['median_abs_cohen_d'] or 0):>9.3f}  "
              f"{(r['mean_signed_cohen_d'] or 0):>9.3f}  "
              f"{(r['single_ma_admission_win'] or 0):>8.3f}  "
              f"{(r['single_ma_admission_loss'] or 0):>8.3f}  "
              f"{(r['single_ma_admission_gap'] or 0):>6.3f}")
    print(f"  Bottom 5 MAs by mean |Cohen's d|:")
    for r in per_ma_sorted[-5:]:
        print(f"    {r['ma']:>8s}  "
              f"{(r['mean_abs_cohen_d'] or 0):>8.3f}  "
              f"{(r['median_abs_cohen_d'] or 0):>9.3f}  "
              f"{(r['mean_signed_cohen_d'] or 0):>9.3f}  "
              f"{(r['single_ma_admission_win'] or 0):>8.3f}  "
              f"{(r['single_ma_admission_loss'] or 0):>8.3f}  "
              f"{(r['single_ma_admission_gap'] or 0):>6.3f}")

    # Save diagnostic JSON
    out_path = os.path.join(OUT_DIR, "htf_diag.json")
    diag = {
        "setup": "htf",
        "n_examples": len(examples),
        "n_wild": len(wild),
        "ma_basis_n": len(ma_specs),
        "max_h": int(max_h),
        "envelope_mechanic": "lower_only_with_stop_window",
        "fix_A": "one-sided lower envelope (no upper bound)",
        "fix_B": "test window = j=0..stop_hit_bar inclusive (full horizon if no stop)",
        "n_cells_active_lower": n_cells_active_lower,
        "lower_band_stats": {
            "median": float(np.nanmedian(lower)),
            "p25": float(np.nanpercentile(lower, 25)),
            "p75": float(np.nanpercentile(lower, 75)),
            "p95": float(np.nanpercentile(lower, 95)),
            "min": float(np.nanmin(lower)),
            "max": float(np.nanmax(lower)),
        },
        "examples_lock_pass": int(ex_pass),
        "svd": svd,
        "loo": loo,
        "wild_admission": {
            "n_wild_pass": int(n_wild_pass),
            "pass_rate": float(n_wild_pass / len(wild)),
            "pass_count_dist": {
                "min": int(wild_pass.min()),
                "max": int(wild_pass.max()),
                "median": int(np.median(wild_pass)),
                "p25": int(np.percentile(wild_pass, 25)),
                "p75": int(np.percentile(wild_pass, 75)),
            },
        },
        "per_ma_carving": per_ma_sorted,
        "sanity_cross_tabs": {
            "wild_win_under_5": {
                "mfe_median": float(np.median(mfe_arr[wild_verdict])) if n_wild_pass else None,
                "stop_hit_median": float(np.median(stop_arr[wild_verdict])) if n_wild_pass else None,
                "early_stop_rate": float(early_stop[wild_verdict].mean()) if n_wild_pass else None,
                "rule_consensus_median": int(np.median(rule_consensus_wild[wild_verdict])) if n_wild_pass else None,
            },
            "wild_loss_under_5": {
                "mfe_median": float(np.median(mfe_arr[~wild_verdict])) if (len(wild) - n_wild_pass) else None,
                "stop_hit_median": float(np.median(stop_arr[~wild_verdict])) if (len(wild) - n_wild_pass) else None,
                "early_stop_rate": float(early_stop[~wild_verdict].mean()) if (len(wild) - n_wild_pass) else None,
                "rule_consensus_median": int(np.median(rule_consensus_wild[~wild_verdict])) if (len(wild) - n_wild_pass) else None,
            },
            "examples": {
                "rule_consensus_median": int(np.median(rule_consensus_ex)),
                "rule_consensus_min": int(rule_consensus_ex.min()),
                "rule_consensus_max": int(rule_consensus_ex.max()),
            },
        },
    }
    with open(out_path, "w") as f:
        json.dump(diag, f, indent=2, default=str)
    print(f"\nDiagnostic written: {out_path}")

    # Save per-cluster details for later inspection
    per_cluster_path = os.path.join(OUT_DIR, "htf_per_cluster.json")
    per_cluster = {
        "examples": [
            {
                "cluster_id": m["cluster_id"],
                "ticker": m["ticker"],
                "horizon": m["horizon"],
                "mfe_adr": m["mfe_adr"],
                "stop_hit_bar": m["stop_hit_bar"],
                "verdict": "WIN" if ex_verdict[i] else "LOSS",
                "rule_consensus": int(rule_consensus_ex[i]),
            }
            for i, m in enumerate(examples)
        ],
        "wild": [
            {
                "cluster_id": m["cluster_id"],
                "ticker": m["ticker"],
                "horizon": m["horizon"],
                "mfe_adr": m["mfe_adr"],
                "stop_hit_bar": m["stop_hit_bar"],
                "n_cells_pass": int(wild_pass[i]),
                "n_cells_active": int(wild_active[i]),
                "verdict": "WIN" if wild_verdict[i] else "LOSS",
                "rule_consensus": int(rule_consensus_wild[i]),
            }
            for i, m in enumerate(wild)
        ],
    }
    with open(per_cluster_path, "w") as f:
        json.dump(per_cluster, f, indent=2, default=str)
    print(f"Per-cluster details: {per_cluster_path}")


if __name__ == "__main__":
    main()
