"""Visual comparison: existing σ-cloud mechanic vs option-1 (no σ, just [min lr, max lr]).

Two cases:
  Top row: HTF × SMA10 daily — the existing viz's case.
  Bottom row: BASE × SMA5 weekly — the tight-consolidation case where σ is inflating the band.

For each: left panel = with σ clouds (current spec), right panel = without σ (option 1).
Same scale on left/right so band contraction is immediately visible.
"""
from __future__ import annotations

import os
import pickle

import matplotlib.pyplot as plt
import numpy as np

TRAJ_DIR = r"C:/Users/Dan/Documents/ScanPerfect/swing-screener/research/presignal_sma_band"


def bands_for_cell(log_ratio, sigma):
    """log_ratio, sigma: (n_ex, N). Returns dict with ratio per ex, clouds per ex,
    union bands with and without sigma."""
    ratio = np.exp(log_ratio)
    cloud_up = np.exp(log_ratio + sigma)
    cloud_lo = np.exp(log_ratio - sigma)
    with np.errstate(invalid='ignore'):
        union_up_with = np.nanmax(cloud_up, axis=0)
        union_lo_with = np.nanmin(cloud_lo, axis=0)
        union_up_wo = np.nanmax(ratio, axis=0)
        union_lo_wo = np.nanmin(ratio, axis=0)
    return {
        "ratio": ratio, "cloud_up": cloud_up, "cloud_lo": cloud_lo,
        "union_up_with": union_up_with, "union_lo_with": union_lo_with,
        "union_up_wo": union_up_wo, "union_lo_wo": union_lo_wo,
    }


def panel(ax, x, b, with_sigma, title, color_band):
    n_ex = b["ratio"].shape[0]
    if with_sigma:
        for i in range(n_ex):
            ax.fill_between(x, b["cloud_lo"][i], b["cloud_up"][i], alpha=0.04, color='steelblue', linewidth=0)
    for i in range(n_ex):
        ax.plot(x, b["ratio"][i], color='steelblue', alpha=0.3, lw=0.7)
    up = b["union_up_with"] if with_sigma else b["union_up_wo"]
    lo = b["union_lo_with"] if with_sigma else b["union_lo_wo"]
    ax.plot(x, up, color=color_band, lw=2.0, label='Union upper')
    ax.plot(x, lo, color=color_band, lw=2.0, label='Union lower')
    ax.fill_between(x, lo, up, alpha=0.15, color=color_band)
    ax.axhline(1.0, color='gray', ls='--', alpha=0.6, lw=1)
    ax.axvline(0, color='black', alpha=0.4, lw=1)
    ax.set_title(title, fontsize=11)
    ax.legend(loc='upper left', fontsize=8)
    ax.grid(alpha=0.3)


# HTF daily SMA10
with open(os.path.join(TRAJ_DIR, "htf_trajectories.pkl"), "rb") as f:
    htf = pickle.load(f)
d_cells = htf["daily_cells"]
sma10_idx = d_cells.index(("SMA", 10))
htf_lr = htf["daily_logratio"][:, sma10_idx, :]
htf_sig = htf["daily_sigma"][:, sma10_idx, :]
N_daily = htf["N_daily"]
x_htf = -np.arange(N_daily)  # 0..-(N_daily-1); index 0 is anchor at x=0
b_htf = bands_for_cell(htf_lr, htf_sig)

# BASE weekly SMA5
with open(os.path.join(TRAJ_DIR, "base_trajectories.pkl"), "rb") as f:
    base = pickle.load(f)
w_cells = base["weekly_cells"]
sma5_idx = w_cells.index(("SMA", 5))
base_lr = base["weekly_logratio"][:, sma5_idx, :]
base_sig = base["weekly_sigma"][:, sma5_idx, :]
W_N = base["W_N"]
x_base = -np.arange(W_N)  # 0..-(W_N-1); index 0 is anchor at x=0
b_base = bands_for_cell(base_lr, base_sig)

# 2x2 figure
fig, axes = plt.subplots(2, 2, figsize=(18, 11))
n_ex_htf = htf_lr.shape[0]
n_ex_base = base_lr.shape[0]

# Top row: HTF x SMA10 daily
panel(axes[0, 0], x_htf, b_htf, with_sigma=True,
      title=f'HTF x SMA10 daily — WITH sigma clouds (current spec)  n_ex={n_ex_htf}', color_band='navy')
panel(axes[0, 1], x_htf, b_htf, with_sigma=False,
      title=f'HTF x SMA10 daily — NO sigma (option 1)  n_ex={n_ex_htf}', color_band='darkgreen')
axes[0, 0].set_ylabel('SMA10 / SMA10 at E-1')
axes[0, 0].set_xlabel('Bar offset (0 = E-1 anchor)')
axes[0, 1].set_xlabel('Bar offset (0 = E-1 anchor)')

# Bottom row: BASE x SMA5 weekly
panel(axes[1, 0], x_base, b_base, with_sigma=True,
      title=f'BASE x SMA5 weekly — WITH sigma clouds (current spec)  n_ex={n_ex_base}', color_band='navy')
panel(axes[1, 1], x_base, b_base, with_sigma=False,
      title=f'BASE x SMA5 weekly — NO sigma (option 1)  n_ex={n_ex_base}', color_band='darkgreen')
axes[1, 0].set_ylabel('SMA5 / SMA5 at W-0 (weekly)')
axes[1, 0].set_xlabel('Weekly bar offset (0 = W-0 anchor)')
axes[1, 1].set_xlabel('Weekly bar offset (0 = W-0 anchor)')

# Match y-axes between with/without for each row so contraction is obvious
for row in range(2):
    y0, y1 = axes[row, 0].get_ylim()
    y2, y3 = axes[row, 1].get_ylim()
    ymin = min(y0, y2); ymax = max(y1, y3)
    axes[row, 0].set_ylim(ymin, ymax)
    axes[row, 1].set_ylim(ymin, ymax)

fig.suptitle('Option 1: drop sigma, use union = [min lr, max lr]. Left = current. Right = option 1.', fontsize=13)
fig.tight_layout(rect=(0, 0, 1, 0.97))
out = os.path.join(TRAJ_DIR, "option1_comparison.png")
fig.savefig(out, dpi=110)
print(f"Saved {out}", flush=True)

# Print band widths at key offsets for numeric check
print("\nBand widths at key offsets:")
print(f"\nHTF x SMA10 daily:")
for k_display, idx in [(1, 0), (5, 4), (10, 9), (20, 19)]:
    if idx < N_daily:
        w_with = b_htf["union_up_with"][idx] - b_htf["union_lo_with"][idx]
        w_wo = b_htf["union_up_wo"][idx] - b_htf["union_lo_wo"][idx]
        ratio_shrink = w_with / w_wo if w_wo > 0 else float('inf')
        print(f"  k={k_display}:  with_sigma={w_with:.3f}  no_sigma={w_wo:.3f}  shrink_factor={ratio_shrink:.2f}x")

print(f"\nBASE x SMA5 weekly:")
for k_display in [1, 2, 3, 5, 10]:
    idx = k_display
    if idx < W_N:
        w_with = b_base["union_up_with"][idx] - b_base["union_lo_with"][idx]
        w_wo = b_base["union_up_wo"][idx] - b_base["union_lo_wo"][idx]
        ratio_shrink = w_with / w_wo if w_wo > 0 else float('inf')
        print(f"  k={k_display}:  with_sigma={w_with:.3f}  no_sigma={w_wo:.3f}  shrink_factor={ratio_shrink:.2f}x")
