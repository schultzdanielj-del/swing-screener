"""Visualization: stacked example SMA10 lines with Bollinger (±1σ) clouds,
anchored at E-1, normalized by SMA10[E-1]. Overlay the union band and a
random candidate for visual passes/fail check."""
from __future__ import annotations

import os
import pickle
import sqlite3
import sys

import matplotlib.pyplot as plt
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import visual_shape_compare as vsc

MAIN_ROOT = r"C:/Users/Dan/Documents/ScanPerfect/swing-screener"
CACHE_DIR = os.path.join(MAIN_ROOT, "local_runner", "cache")
DB = os.path.join(MAIN_ROOT, "data", "scanperfect.db")

SETUP = "htf"
N_BARS = 39
SMA_PERIOD = 10
SEED = 7


def rolling_sma(c, p):
    L = len(c)
    c = np.where(np.isfinite(c) & (c > 0), c, np.nan)
    out = np.full(L, np.nan)
    cs = np.cumsum(np.where(np.isfinite(c), c, 0.0))
    co = np.cumsum(np.isfinite(c).astype(np.int64))
    for t in range(p - 1, L):
        lo = t - p + 1
        ok = co[t] - (co[lo - 1] if lo > 0 else 0)
        if ok == p:
            s = cs[t] - (cs[lo - 1] if lo > 0 else 0)
            out[t] = s / p
    return out


def rolling_sd(c, p):
    L = len(c)
    c = np.where(np.isfinite(c) & (c > 0), c, np.nan)
    out = np.full(L, np.nan)
    for t in range(p - 1, L):
        w = c[t - p + 1:t + 1]
        if np.all(np.isfinite(w)):
            out[t] = w.std(ddof=0)
    return out


def rolling_sd_of_sma(sma_series, win):
    """Rolling SD of the SMA line itself over a `win`-bar window.
    Measures how much the SMA jitters — NOT a Bollinger SD of price."""
    L = len(sma_series)
    out = np.full(L, np.nan)
    s = np.where(np.isfinite(sma_series), sma_series, np.nan)
    for t in range(win - 1, L):
        w = s[t - win + 1:t + 1]
        if np.all(np.isfinite(w)):
            out[t] = w.std(ddof=0)
    return out


def trajectory(close, E, n_bars, p):
    """Return (sma_norm, sd_norm) of length n_bars, both normalized by
    SMA at anchor E-1. Ordered x = -(n_bars-1) ... 0 (0 = E-1)."""
    L = len(close)
    if E < 1 or E - 1 >= L:
        return None
    s = rolling_sma(close, p)
    sd = rolling_sd_of_sma(s, p)
    anchor = s[E - 1]
    if not np.isfinite(anchor) or anchor <= 0:
        return None
    traj_s = np.full(n_bars, np.nan)
    traj_sd = np.full(n_bars, np.nan)
    for i in range(n_bars):
        k = i + 1  # offset from E
        src = E - k
        if 0 <= src < L:
            traj_s[i] = s[src] / anchor
            traj_sd[i] = sd[src] / anchor
    return traj_s[::-1], traj_sd[::-1]


def main():
    print("Loading universe...", flush=True)
    with open(os.path.join(CACHE_DIR, "universe_ohlcv_daily.pkl"), "rb") as f:
        universe = pickle.load(f)
    print(f"  {len(universe):,} tickers", flush=True)

    conn = sqlite3.connect(DB)
    rows = conn.execute(
        "SELECT ticker, entry_date FROM examples WHERE setup_type=?", (SETUP,)
    ).fetchall()
    conn.close()
    print(f"  {len(rows)} examples for {SETUP}", flush=True)

    ex_sma, ex_sd, ex_label = [], [], []
    for t, d in rows:
        df = universe.get(t)
        if df is None:
            continue
        E = vsc.lookup_idx(df, d)
        if E < 0:
            continue
        close = df["close"].values.astype(np.float64)
        r = trajectory(close, E, N_BARS, SMA_PERIOD)
        if r is None:
            continue
        s, sd = r
        if np.all(np.isfinite(s)) and np.all(np.isfinite(sd)):
            ex_sma.append(s)
            ex_sd.append(sd)
            ex_label.append(f"{t} {d}")
    ex_sma = np.array(ex_sma)
    ex_sd = np.array(ex_sd)
    print(f"  kept {len(ex_sma)} examples with full trajectories", flush=True)

    upper = np.max(ex_sma + ex_sd, axis=0)
    lower = np.min(ex_sma - ex_sd, axis=0)

    # Tight-bar selection: cross-example SD of the anchored SMA values at each bar.
    # At the anchor (x=0) SD is 0 by construction. Walk back until it grows past
    # a kneedle-derived elbow on the per-bar SD curve.
    per_bar_sd = np.nanstd(ex_sma, axis=0, ddof=0)  # length N_BARS, x = -(N-1)..0
    # Walk back from anchor end: find last contiguous tight run
    # Kneedle on the reversed curve (anchor → oldest) to locate elbow
    curve = per_bar_sd[::-1]  # index 0 = anchor (x=0), index N-1 = oldest
    x_k = np.arange(len(curve), dtype=np.float64)
    y_k = curve.astype(np.float64)
    dx = float(x_k[-1] - x_k[0])
    dy = float(y_k[-1] - y_k[0])
    if dy > 0:
        num = np.abs(dy * x_k - dx * y_k + dx * y_k[0])
        elbow = int(np.argmax(num))
    else:
        elbow = len(curve) - 1
    tight_count = elbow + 1  # bars from anchor kept (inclusive)
    # Indices in the x-axis frame (x=-(N-1)..0): last tight_count bars
    tight_slice = slice(N_BARS - tight_count, N_BARS)
    tight_x_start = x_k.size - tight_count
    print(f"  tight-bar elbow @ {elbow} bars back; keeping last {tight_count} bars", flush=True)

    # Specific well-known tickers; find the bar in each with
    # the most interesting trajectory variation as the anchor.
    rng = np.random.default_rng(SEED)
    cand_tickers = ["META", "GOOGL", "CAT"]
    cands = []
    for tk in cand_tickers:
        df = universe.get(tk)
        if df is None:
            print(f"  {tk} not in universe", flush=True)
            continue
        L = len(df)
        ds = vsc.dates_as_str(df)
        elig = np.where((np.arange(L) >= N_BARS + SMA_PERIOD) & (ds >= "2020-01-02"))[0]
        if len(elig) == 0:
            print(f"  {tk} no eligible bars post-2020", flush=True)
            continue
        close = df["close"].values.astype(np.float64)
        # Sample 30 random eligible bars and pick the one with largest spread
        idx_sample = rng.choice(elig, size=min(30, len(elig)), replace=False)
        best = None
        best_spread = -1
        for E_c in idx_sample:
            r = trajectory(close, int(E_c), N_BARS, SMA_PERIOD)
            if r is None:
                continue
            s, _ = r
            if not np.all(np.isfinite(s)):
                continue
            spread = float(np.nanmax(s) - np.nanmin(s))
            if spread > best_spread:
                best_spread = spread
                best = {"ticker": tk, "date": ds[int(E_c)], "sma": s, "spread": spread}
        if best is not None:
            cands.append(best)

    x = np.arange(-(N_BARS - 1), 1)

    fig, ax = plt.subplots(figsize=(15, 8))

    # Each example's cloud (pale)
    for i in range(len(ex_sma)):
        ax.fill_between(x, ex_sma[i] - ex_sd[i], ex_sma[i] + ex_sd[i],
                        alpha=0.04, color='steelblue', linewidth=0)
    # Each example's SMA line (pale)
    for i in range(len(ex_sma)):
        ax.plot(x, ex_sma[i], color='steelblue', alpha=0.25, lw=0.7)

    # Union band — draw full range pale, tight range bold
    ax.plot(x, upper, color='navy', lw=1, alpha=0.3, zorder=5)
    ax.plot(x, lower, color='navy', lw=1, alpha=0.3, zorder=5)
    ax.plot(x[tight_slice], upper[tight_slice], color='navy', lw=2.6, label='Union upper (tight window)', zorder=6)
    ax.plot(x[tight_slice], lower[tight_slice], color='navy', lw=2.6, label='Union lower (tight window)', zorder=6)
    ax.fill_between(x[tight_slice], lower[tight_slice], upper[tight_slice], alpha=0.12, color='navy', zorder=2)
    # Shade the inactive region lightly
    inactive_slice = slice(0, N_BARS - tight_count)
    if tight_count < N_BARS:
        ax.axvspan(x[0], x[inactive_slice.stop - 1] + 0.5, color='gray', alpha=0.15, zorder=0)

    # Candidates — tested only within the tight window
    cand_colors = ['red', 'orange', 'purple']
    for ci, cand in enumerate(cands):
        in_band_all = (cand["sma"] >= lower) & (cand["sma"] <= upper)
        # Only bars within the tight window matter for verdict
        tight_mask = np.zeros(N_BARS, dtype=bool)
        tight_mask[tight_slice] = True
        in_band_tight = in_band_all | ~tight_mask  # outside tight window auto-pass
        n_out = int((~in_band_tight).sum())
        pass_all = n_out == 0
        base = cand_colors[ci % len(cand_colors)]
        verdict = "PASS" if pass_all else f"FAIL ({n_out} bars out in tight window)"
        # Plot candidate full trajectory dim outside tight window, bold inside
        ax.plot(x[inactive_slice], cand["sma"][inactive_slice], color=base, lw=1.2, alpha=0.35, zorder=9)
        ax.plot(x[tight_slice], cand["sma"][tight_slice], color=base, lw=2.4, zorder=10,
                label=f'{cand["ticker"]} {cand["date"]} (spread={cand["spread"]:.2f}) — {verdict}')
        for i in np.where(~in_band_tight)[0]:
            ax.plot(x[i], cand["sma"][i], marker='x', color=base, markersize=10,
                    markeredgewidth=2.2, zorder=11)

    ax.axhline(1.0, color='gray', ls='--', alpha=0.6, lw=1, label='Anchor SMA10 at E-1')
    ax.axvline(0, color='black', alpha=0.4, lw=1)
    ax.set_xlabel('Bar offset (0 = E-1, anchor bar)')
    ax.set_ylabel('SMA10 / SMA10 at E-1')
    ax.set_title(f'{SETUP.upper()} — SMA{SMA_PERIOD} example overlay with ±1σ clouds (σ = rolling SD of the SMA line over {SMA_PERIOD} bars)  |  n_ex={len(ex_sma)}')
    ax.legend(loc='upper left', fontsize=9)
    ax.grid(alpha=0.3)

    fig.tight_layout()
    out = os.path.join(HERE, f"presignal_sma_band_{SETUP}_viz.png")
    fig.savefig(out, dpi=110)
    print(f"Saved {out}", flush=True)
    for cand in cands:
        in_band = (cand["sma"] >= lower) & (cand["sma"] <= upper)
        n_out = int((~in_band).sum())
        print(f"  {cand['ticker']} {cand['date']}  spread={cand['spread']:.3f}  verdict={'PASS' if n_out==0 else f'FAIL ({n_out} out)'}", flush=True)


if __name__ == "__main__":
    main()
