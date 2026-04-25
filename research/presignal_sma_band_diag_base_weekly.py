"""Quick diagnostic: BASE weekly short-term MAs at short offsets.

Dan's hypothesis: BASE consolidation means short-term weekly MAs should be tight and
discerning. My aggregate said BASE admission = 47.8%. Verify by pulling the actual
per-cell stats on short-term weekly MAs (SMA/EMA at periods 3,5,8,10) at offsets k=1..5.
"""
import os
import pickle

import numpy as np

TRAJ_DIR = r"C:/Users/Dan/Documents/ScanPerfect/swing-screener-win-loss-classifier/research/presignal_sma_band"

with open(os.path.join(TRAJ_DIR, "base_trajectories.pkl"), "rb") as f:
    traj = pickle.load(f)

w_lr = traj["weekly_logratio"]  # (n_ex, 15, W_N)
w_sig = traj["weekly_sigma"]
w_cells = traj["weekly_cells"]
W_N = traj["W_N"]
n_ex = w_lr.shape[0]
examples = traj["examples"]

print(f"BASE: n_ex={n_ex}, W_N={W_N}")
print()

SHORT = [("EMA", 3), ("SMA", 5), ("EMA", 5), ("SMA", 8), ("EMA", 8),
         ("SMA", 10), ("EMA", 10)]

print(f"{'cell':<8} {'k':>3}  {'tight':>8} {'lower':>9} {'upper':>9} {'width':>8} {'med_lr':>9} {'med_sig':>9}")
print("-" * 80)
for fam, p in SHORT:
    try:
        m = w_cells.index((fam, p))
    except ValueError:
        continue
    for k in range(min(6, W_N)):
        vals = w_lr[:, m, k]
        sigs = w_sig[:, m, k]
        with np.errstate(invalid='ignore'):
            tight = np.nanstd(vals, ddof=0)
            upper = np.nanmax(vals + sigs)
            lower = np.nanmin(vals - sigs)
        width = upper - lower
        med = np.nanmedian(vals)
        med_sig = np.nanmedian(sigs)
        label = f"{fam}{p}"
        print(f"{label:<8} {k:>3}  {tight:>8.4f} {lower:>9.4f} {upper:>9.4f} {width:>8.4f} {med:>9.4f} {med_sig:>9.4f}")
    print()

# Also show: which example is setting the edge at SMA5 weekly k=1 (Dan's key cell)?
print("\n=== Edge contributors at SMA5 weekly k=1 ===")
m_sma5 = w_cells.index(("SMA", 5))
vals = w_lr[:, m_sma5, 1]
sigs = w_sig[:, m_sma5, 1]
upper_contrib = vals + sigs
lower_contrib = vals - sigs
print(f"Full-N upper = {np.nanmax(upper_contrib):.4f}  lower = {np.nanmin(lower_contrib):.4f}")
order_upper = np.argsort(-upper_contrib)  # descending (NaN last)
order_lower = np.argsort(lower_contrib)   # ascending (NaN last)
print("Top 5 upper contributors:")
for idx in order_upper[:5]:
    if not np.isfinite(upper_contrib[idx]):
        continue
    e = examples[int(idx)]
    print(f"  {e['ticker']}/{e['entry_date']}  lr={vals[int(idx)]:.4f}  sig={sigs[int(idx)]:.4f}  upper={upper_contrib[int(idx)]:.4f}")
print("Top 5 lower contributors:")
for idx in order_lower[:5]:
    if not np.isfinite(lower_contrib[idx]):
        continue
    e = examples[int(idx)]
    print(f"  {e['ticker']}/{e['entry_date']}  lr={vals[int(idx)]:.4f}  sig={sigs[int(idx)]:.4f}  lower={lower_contrib[int(idx)]:.4f}")
