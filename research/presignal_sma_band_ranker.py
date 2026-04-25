"""§4 SMA-band presignal grinder — Phases 2+3+4: ranking, walk-up driver, boundary detection.

Per setup:
  Phase 2 — flatten (family, period, scale, offset) cells, compute tightness + full band + LOO bands.
           Rank cells ascending by tightness (loose to tight).
  Phase 3 — sample 5000 market bars (shared, seed=42). Build (market, cell) + (example, cell) boolean
           matrices, reorder by rank, cumulative AND -> admission curve + LOO pass curve.
  Phase 4 — kneedle elbow on admission (signal begins); first-drop on LOO (overfit begins).
           Kept cells = ranks [0, K*-1) where K* is first-drop index.

Output:
  research/presignal_sma_band_ranker/{setup}_ranker.pkl
  research/presignal_sma_band_ranker/walkup_{setup}.png
"""
from __future__ import annotations

import json
import os
import pickle
import sys
import time

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import visual_shape_compare as vsc
import presignal_sma_band_extract as ext

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

MAIN_ROOT = r"C:/Users/Dan/Documents/ScanPerfect/swing-screener"
CACHE_DIR = os.path.join(MAIN_ROOT, "local_runner", "cache")
WORKTREE = r"C:/Users/Dan/Documents/ScanPerfect/swing-screener"
TRAJ_DIR = os.path.join(WORKTREE, "research", "presignal_sma_band")
OUT_DIR = os.path.join(WORKTREE, "research", "presignal_sma_band_ranker")

SETUPS = ["htf", "bf", "base", "dtss", "3-4db"]
N_MARKET_SAMPLE = 5000
SEED = 42
DATE_CUTOFF = "2020-01-02"
MIN_TICKER_COUNT = 11000


# ───────────── helpers ─────────────

def cell_label(scale, family, period, k):
    scale_tag = "D" if scale == "daily" else "W"
    return f"{family}{period}@{scale_tag}-k{k}"


def build_cell_meta(d_cells, w_cells, N_daily, W_N):
    meta = []
    # Daily: k = 1..N_daily (k=1 is anchor E-1)
    for fam, p in d_cells:
        for i in range(N_daily):
            k = i + 1
            meta.append({"scale": "daily", "family": fam, "period": p, "k": k, "tensor_k_idx": i})
    # Weekly: k = 0..W_N-1 (k=0 is anchor W-0)
    for fam, p in w_cells:
        for i in range(W_N):
            k = i
            meta.append({"scale": "weekly", "family": fam, "period": p, "k": k, "tensor_k_idx": i})
    return meta


def kneedle_elbow(y):
    """Max perpendicular distance from chord on a monotone-ish curve."""
    y = np.asarray(y, dtype=np.float64)
    n = len(y)
    if n < 3:
        return 0
    x = np.arange(n, dtype=np.float64)
    dx = float(x[-1] - x[0])
    dy = float(y[-1] - y[0])
    if abs(dy) < 1e-12:
        return 0
    num = np.abs(dy * x - dx * y + dx * y[0])
    return int(np.argmax(num))


def first_drop_below_one(curve, tol=1e-9):
    """Smallest index K where curve[K] < 1.0 - tol. Returns len(curve) if never."""
    thresh = 1.0 - tol
    idx = np.where(curve < thresh)[0]
    if len(idx) == 0:
        return len(curve)
    return int(idx[0])


def flatten_scale(tensor_d, tensor_w):
    """(n, 21, N_daily) + (n, 15, W_N) -> (n, C)."""
    n = tensor_d.shape[0]
    N_daily = tensor_d.shape[2]
    W_N = tensor_w.shape[2]
    C_d = tensor_d.shape[1] * N_daily
    C_w = tensor_w.shape[1] * W_N
    out = np.full((n, C_d + C_w), np.nan, dtype=np.float64)
    out[:, :C_d] = tensor_d.reshape(n, C_d)
    out[:, C_d:] = tensor_w.reshape(n, C_w)
    return out, C_d, C_w


# ───────────── market sampling ─────────────

def sample_market(universe, n_target, rng, N_daily_max, W_N_max, tcache):
    """Returns (mkt_d_lr, mkt_w_lr, meta) — shared across setups."""
    tickers = list(universe.keys())
    min_hist = max(ext.SMA_DAILY) + N_daily_max + 5
    d_list, w_list, meta = [], [], []
    attempts = 0
    max_attempts = n_target * 20
    t0 = time.time()
    while len(d_list) < n_target and attempts < max_attempts:
        attempts += 1
        tk = tickers[rng.integers(0, len(tickers))]
        df = universe[tk]
        L = len(df)
        if L < min_hist:
            continue
        ds = vsc.dates_as_str(df)
        eligible = np.where((np.arange(L) >= min_hist) & (ds >= DATE_CUTOFF))[0]
        if len(eligible) == 0:
            continue
        E = int(eligible[rng.integers(0, len(eligible))])
        if tk not in tcache:
            tcache[tk] = ext.build_ticker_cache(df)
        tc = tcache[tk]
        d_lr, _ = ext.extract_daily(tc, E, N_daily_max)
        w_lr, _ = ext.extract_weekly(tc, E, W_N_max)
        if d_lr is None or w_lr is None:
            continue
        d_list.append(d_lr)
        w_list.append(w_lr)
        meta.append({"ticker": tk, "E_idx": E, "date": ds[E]})
        if len(d_list) % 500 == 0:
            print(f"    market sample: {len(d_list)}/{n_target}  (attempts={attempts}, {time.time()-t0:.1f}s)", flush=True)
    return np.stack(d_list), np.stack(w_list), meta


# ───────────── per-setup pipeline ─────────────

def run_setup(setup, traj, mkt_d_lr_max, mkt_w_lr_max, mkt_meta):
    d_lr_ex = traj["daily_logratio"]    # (n_ex, 21, N_daily)
    d_sig_ex = traj["daily_sigma"]
    w_lr_ex = traj["weekly_logratio"]   # (n_ex, 15, W_N)
    w_sig_ex = traj["weekly_sigma"]
    d_cells = traj["daily_cells"]
    w_cells = traj["weekly_cells"]
    examples = traj["examples"]
    n_ex = d_lr_ex.shape[0]
    N_daily = d_lr_ex.shape[2]
    W_N = w_lr_ex.shape[2]

    # Flatten example cells: (n_ex, C)
    lr_ex, C_d, C_w = flatten_scale(d_lr_ex, w_lr_ex)
    sig_ex, _, _ = flatten_scale(d_sig_ex, w_sig_ex)
    C = C_d + C_w

    cell_meta = build_cell_meta(d_cells, w_cells, N_daily, W_N)
    assert len(cell_meta) == C, f"cell count mismatch: {len(cell_meta)} vs {C}"

    # Phase 2 — tightness + full band
    with np.errstate(invalid='ignore'):
        tightness = np.nanstd(lr_ex, axis=0, ddof=0)  # (C,)
        upper_full = np.nanmax(lr_ex + sig_ex, axis=0)
        lower_full = np.nanmin(lr_ex - sig_ex, axis=0)

    # Phase 2 — LOO example pass
    upper_i = lr_ex + sig_ex   # (n_ex, C)
    lower_i = lr_ex - sig_ex
    ex_passes_loo = np.ones((n_ex, C), dtype=bool)
    for i in range(n_ex):
        mask = np.ones(n_ex, dtype=bool); mask[i] = False
        with np.errstate(invalid='ignore'):
            u_loo = np.nanmax(upper_i[mask], axis=0)
            l_loo = np.nanmin(lower_i[mask], axis=0)
        val = lr_ex[i]
        nan_val = np.isnan(val)
        nan_band = np.isnan(u_loo) | np.isnan(l_loo)
        with np.errstate(invalid='ignore'):
            in_band = (val >= l_loo) & (val <= u_loo)
        ex_passes_loo[i] = nan_val | nan_band | in_band

    # Phase 3 — market flatten sliced to this setup's dims
    mkt_d = mkt_d_lr_max[:, :, :N_daily]
    mkt_w = mkt_w_lr_max[:, :, :W_N]
    mkt_lr, _, _ = flatten_scale(mkt_d, mkt_w)  # (M, C)
    M = mkt_lr.shape[0]

    nan_mval = np.isnan(mkt_lr)
    nan_band = np.isnan(upper_full) | np.isnan(lower_full)
    with np.errstate(invalid='ignore'):
        in_band = (mkt_lr >= lower_full[None, :]) & (mkt_lr <= upper_full[None, :])
    mkt_passes = nan_mval | nan_band[None, :] | in_band

    # Rank cells ascending by tightness (NaN -> end)
    tight_rank = np.where(np.isnan(tightness), np.inf, tightness)
    order = np.argsort(tight_rank, kind='stable')

    # Reorder
    ex_ranked = ex_passes_loo[:, order]
    mkt_ranked = mkt_passes[:, order]

    # Walk-up: cumulative AND over cells
    cum_ex = np.minimum.accumulate(ex_ranked.astype(np.int8), axis=1).astype(bool)
    cum_mkt = np.minimum.accumulate(mkt_ranked.astype(np.int8), axis=1).astype(bool)
    loo_pass = cum_ex.mean(axis=0)
    admission = cum_mkt.mean(axis=0)
    # Prepend K=0 baseline (no cells -> 100% pass)
    loo_pass = np.concatenate([[1.0], loo_pass])
    admission = np.concatenate([[1.0], admission])

    # Boundaries
    market_elbow_K = kneedle_elbow(admission)
    loo_drop_K = first_drop_below_one(loo_pass)
    kept_count = max(loo_drop_K - 1, 0)
    kept_ranks = order[:kept_count].tolist() if kept_count > 0 else []

    # Which example dropped first
    first_drop_example = None
    if loo_drop_K <= len(loo_pass) - 1 and loo_drop_K >= 1:
        col = ex_ranked[:, loo_drop_K - 1] if loo_drop_K - 1 < ex_ranked.shape[1] else None
        if col is not None:
            # Who failed at this step? Those with cum_ex[i, loo_drop_K-1] == False
            failed_at = np.where(~cum_ex[:, loo_drop_K - 1])[0]
            first_drop_example = [examples[int(i)] for i in failed_at]

    return {
        "setup": setup,
        "n_ex": n_ex, "N_daily": N_daily, "W_N": W_N,
        "C": C, "C_d": C_d, "C_w": C_w,
        "cell_meta": cell_meta,
        "tightness": tightness,
        "upper_full": upper_full, "lower_full": lower_full,
        "order": order,
        "admission": admission, "loo_pass": loo_pass,
        "market_elbow_K": market_elbow_K,
        "loo_drop_K": loo_drop_K,
        "kept_count": kept_count,
        "kept_ranks": kept_ranks,
        "first_drop_examples": first_drop_example or [],
        "examples": examples,
        "market_meta": mkt_meta,
    }


def plot_walkup(st, out_path):
    C = st["C"]
    xs = np.arange(C + 1)
    fig, ax = plt.subplots(figsize=(12, 6))
    ax.plot(xs, st["admission"], color='steelblue', lw=1.5, label='Market admission rate')
    ax.plot(xs, st["loo_pass"], color='crimson', lw=1.5, label='LOO example pass rate')
    ax.axvline(st["market_elbow_K"], color='steelblue', ls='--', alpha=0.6,
               label=f"Market elbow (K={st['market_elbow_K']})")
    ax.axvline(st["loo_drop_K"], color='crimson', ls='--', alpha=0.6,
               label=f"LOO first drop (K={st['loo_drop_K']})")
    if st["kept_count"] > 0:
        ax.axvspan(st["market_elbow_K"], st["loo_drop_K"], alpha=0.08, color='green',
                   label=f"Kept range (size={st['kept_count']})")
    ax.set_xlabel('K (cells added)')
    ax.set_ylabel('Pass rate')
    ax.set_title(f"{st['setup'].upper()} walk-up  n_ex={st['n_ex']}  C={C}  kept={st['kept_count']}")
    ax.legend(loc='lower left', fontsize=9)
    ax.grid(alpha=0.3)
    ax.set_ylim(-0.02, 1.05)
    fig.tight_layout()
    fig.savefig(out_path, dpi=110)
    plt.close(fig)


# ───────────── main ─────────────

def main():
    os.makedirs(OUT_DIR, exist_ok=True)

    print(f"OHLCV cache: {CACHE_DIR}", flush=True)
    with open(os.path.join(CACHE_DIR, "universe_ohlcv_daily.pkl"), "rb") as f:
        universe = pickle.load(f)
    print(f"Universe: {len(universe):,} tickers", flush=True)
    if len(universe) < MIN_TICKER_COUNT:
        print(f"FAIL: ticker count too low"); sys.exit(1)

    # Load all trajectories + determine max dims for market sampling
    trajs = {}
    for setup in SETUPS:
        p = os.path.join(TRAJ_DIR, f"{setup}_trajectories.pkl")
        with open(p, "rb") as f:
            trajs[setup] = pickle.load(f)
    N_daily_max = max(trajs[s]["N_daily"] for s in SETUPS)
    W_N_max = max(trajs[s]["W_N"] for s in SETUPS)
    print(f"N_daily max={N_daily_max}  W_N max={W_N_max}", flush=True)

    # Shared market sample
    print(f"\nSampling {N_MARKET_SAMPLE:,} market bars (shared, seed={SEED})...", flush=True)
    rng = np.random.default_rng(SEED)
    tcache = {}
    t0 = time.time()
    mkt_d, mkt_w, mkt_meta = sample_market(universe, N_MARKET_SAMPLE, rng, N_daily_max, W_N_max, tcache)
    print(f"  market sample done: daily {mkt_d.shape}  weekly {mkt_w.shape}  ({time.time()-t0:.1f}s)", flush=True)

    summary = {}
    for setup in SETUPS:
        print(f"\n=== {setup.upper()}", flush=True)
        t0 = time.time()
        st = run_setup(setup, trajs[setup], mkt_d, mkt_w, mkt_meta)
        print(f"  n_ex={st['n_ex']}  C={st['C']}  (daily {st['C_d']} + weekly {st['C_w']})  ({time.time()-t0:.1f}s)", flush=True)
        elbow = st["market_elbow_K"]; drop = st["loo_drop_K"]; C = st["C"]
        adm = st["admission"]; loo = st["loo_pass"]
        print(f"  market_elbow_K={elbow}  admission at K=0: {adm[0]:.3f}  at elbow: {adm[elbow]:.3f}  at drop: {adm[drop] if drop<=C else adm[-1]:.3f}  at end: {adm[-1]:.6f}", flush=True)
        print(f"  loo_drop_K={drop} (never={drop>C})  loo_pass at drop: {loo[drop] if drop<=C else loo[-1]:.3f}  at end: {loo[-1]:.3f}", flush=True)
        print(f"  kept_count={st['kept_count']}  (cells at ranks [0..{st['kept_count']-1}])", flush=True)

        if st["kept_count"] == 0:
            print(f"  *** INFEASIBLE — LOO drops at first cell added. Filter has no feasible range.", flush=True)
        else:
            first_5 = st["kept_ranks"][:5]
            last_5 = st["kept_ranks"][-5:]
            def lbl(r):
                m = st["cell_meta"][r]
                return cell_label(m["scale"], m["family"], m["period"], m["k"])
            print(f"  5 loosest kept: " + ", ".join(f"{lbl(r)}[t={st['tightness'][r]:.4f}]" for r in first_5), flush=True)
            print(f"  5 tightest kept: " + ", ".join(f"{lbl(r)}[t={st['tightness'][r]:.4f}]" for r in last_5), flush=True)

        if st["first_drop_examples"]:
            names = [f"{e['ticker']}/{e['entry_date']}" for e in st["first_drop_examples"][:5]]
            print(f"  first LOO-drop examples: {names}", flush=True)

        # Save pickle
        out_pkl = os.path.join(OUT_DIR, f"{setup}_ranker.pkl")
        with open(out_pkl, "wb") as f:
            pickle.dump(st, f)
        # Plot
        out_png = os.path.join(OUT_DIR, f"walkup_{setup}.png")
        plot_walkup(st, out_png)
        print(f"  saved {out_pkl}  and  {out_png}", flush=True)

        summary[setup] = {
            "n_ex": st["n_ex"], "C": st["C"], "C_d": st["C_d"], "C_w": st["C_w"],
            "market_elbow_K": int(elbow),
            "loo_drop_K": int(drop),
            "kept_count": int(st["kept_count"]),
            "admission_at_elbow": float(adm[elbow]),
            "admission_at_drop": float(adm[drop] if drop <= C else adm[-1]),
            "admission_at_end": float(adm[-1]),
            "loo_pass_at_drop": float(loo[drop] if drop <= C else loo[-1]),
            "loo_pass_at_end": float(loo[-1]),
        }

    with open(os.path.join(OUT_DIR, "summary.json"), "w") as f:
        json.dump(summary, f, indent=2)

    print("\n=== SUMMARY ===")
    print(json.dumps(summary, indent=2))
    print(f"\nOutputs in {OUT_DIR}", flush=True)


if __name__ == "__main__":
    main()
