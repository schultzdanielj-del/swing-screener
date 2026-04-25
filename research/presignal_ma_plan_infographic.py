"""Render a 4-panel infographic explaining the MA-corridor filter plan.

Output: C:/Users/Dan/Documents/ScanPerfect/swing-screener-dual-exit/research/MAplan_infographic.png
"""
from __future__ import annotations

import os
import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
from matplotlib.patches import FancyArrowPatch

OUT_PATH = r"C:/Users/Dan/Documents/ScanPerfect/swing-screener-dual-exit/research/MAplan_infographic.png"

fig = plt.figure(figsize=(16, 10))
fig.suptitle("MA-corridor filter plan — fundamental idea + stacking", fontsize=14, fontweight="bold", y=0.98)

# ------------------- Panel 1: single MA, multiple examples, corridor -------------------
ax1 = fig.add_subplot(2, 2, 1)
x = np.linspace(0, 40, 60)
# Simulate example MA curves - noisy upward trend
rng = np.random.default_rng(0)
base = 1.0 + 0.008 * (x - 10) + 0.1 * np.sin(x / 6)
examples = []
for _ in range(6):
    noise = 0.03 * rng.standard_normal(len(x)) + 0.02 * rng.standard_normal(1)
    examples.append(base + noise)
examples = np.array(examples)
mean_curve = examples.mean(axis=0)
sd = examples.std(axis=0, ddof=1)
# Use 2-sigma band for visual
upper = mean_curve + 2 * sd
lower = mean_curve - 2 * sd
ax1.fill_between(x, lower, upper, color="#66b3ff", alpha=0.25, label="corridor (SD band from pool)")
ax1.plot(x, upper, "--", color="#1f77b4", lw=1, alpha=0.7)
ax1.plot(x, lower, "--", color="#1f77b4", lw=1, alpha=0.7)
for e in examples:
    ax1.plot(x, e, color="#d62728", lw=0.9, alpha=0.7)
ax1.set_title("1. For ONE MA type (e.g. SMA50):\nbuild a corridor from the example pool's values", fontsize=10)
ax1.set_xlabel("bars before entry")
ax1.set_ylabel("MA value (schematic)")
ax1.set_xticks([])
ax1.set_yticks([])
ax1.legend(
    handles=[
        mpatches.Patch(color="#d62728", label="each example's SMA50 curve (red)"),
        mpatches.Patch(color="#66b3ff", alpha=0.4, label="corridor = SD band across examples (blue)"),
    ],
    loc="lower right", fontsize=8
)

# ------------------- Panel 2: many MAs, many corridors (stacked) -------------------
ax2 = fig.add_subplot(2, 2, 2)
labels = ["SMA5", "SMA20", "SMA50", "SMA200", "EMA10", "EMA100"]
colors = ["#d62728", "#9467bd", "#2ca02c", "#ff7f0e", "#1f77b4", "#8c564b"]
for i, (lab, col) in enumerate(zip(labels, colors)):
    yshift = 1.1 - 0.18 * i
    base = yshift + 0.004 * (x - 20) + 0.08 * np.sin(x / (8 - i * 0.5) + i)
    sd_band = 0.04 + 0.01 * i
    ax2.fill_between(x, base - sd_band, base + sd_band, color=col, alpha=0.25)
    ax2.plot(x, base, color=col, lw=1.6, label=lab)
ax2.set_title("2. Repeat for MANY MA types:\neach gets its own corridor across the lead-up", fontsize=10)
ax2.set_xlabel("bars before entry")
ax2.set_xticks([])
ax2.set_yticks([])
ax2.legend(loc="upper right", fontsize=7, ncol=2)

# ------------------- Panel 3: heatmap meat/fat -------------------
ax3 = fig.add_subplot(2, 2, 3)
# Fake heatmap: 12 MAs x 40 offsets
rng2 = np.random.default_rng(1)
hm = 0.3 + 0.4 * rng2.random((12, 40))
# Boost a few cells to represent "meat"
meat_cells = [(2, 3), (2, 5), (5, 0), (5, 1), (5, 2), (7, 8), (10, 15), (10, 18), (11, 20), (4, 30)]
for r, c in meat_cells:
    hm[r, max(0, c - 1):c + 2] += 0.45
hm = np.clip(hm, 0, 1)
im = ax3.imshow(hm, aspect="auto", cmap="viridis", vmin=0, vmax=1)
ax3.set_yticks(range(12))
ax3.set_yticklabels(["SMA5", "SMA10", "SMA20", "SMA50", "SMA100", "SMA200",
                     "EMA5", "EMA10", "EMA20", "EMA50", "EMA100", "EMA200"], fontsize=8)
ax3.set_xticks([0, 10, 20, 30, 39])
ax3.set_xticklabels(["E", "E-10", "E-20", "E-30", "E-40"], fontsize=8)
ax3.set_title("3. Measure discriminating power per cell:\ndark = meat (carves a lot) | light = fat (carves little)", fontsize=10)
ax3.set_xlabel("bar offset from entry")
ax3.set_ylabel("MA type")
cbar = plt.colorbar(im, ax=ax3)
cbar.set_label("reject rate vs market", fontsize=8)

# ------------------- Panel 4: filter stacking / funnel -------------------
ax4 = fig.add_subplot(2, 2, 4)
ax4.set_xlim(0, 10)
ax4.set_ylim(0, 10)
ax4.axis("off")
ax4.set_title("4. Stack ONLY the meat cells — most discriminating first:\nbroad market → progressively carved", fontsize=10)

# Funnel stages with sizes proportional to remaining fraction
stages = [
    ("Full market\n~12.7M bars", 1.00, "#cccccc"),
    ("Meat cell #1\ne.g. SMA50 at E-3", 0.12, "#88ccff"),
    ("+ Meat cell #2\ne.g. EMA100 at E-20", 0.04, "#66aaff"),
    ("+ Meat cell #3\ne.g. SMA200 at E", 0.015, "#4488ee"),
    ("+ Meat cell #4 ...\n(knee in carve curve)", 0.005, "#226699"),
]
y_positions = np.linspace(9, 1, len(stages))
for i, (label, frac, col) in enumerate(stages):
    width = 3 + frac * 15  # schematic
    x0 = 5 - width / 2
    rect = mpatches.Rectangle((x0, y_positions[i] - 0.6), width, 1.1, color=col,
                              edgecolor="black", lw=1, alpha=0.9)
    ax4.add_patch(rect)
    ax4.text(5, y_positions[i] - 0.04, f"{label}\n({frac * 100:.2f}% of market)",
             ha="center", va="center", fontsize=7.5)
    if i < len(stages) - 1:
        ax4.annotate("", xy=(5, y_positions[i + 1] + 0.7), xytext=(5, y_positions[i] - 0.7),
                     arrowprops=dict(arrowstyle="->", color="black", lw=1.2))

fig.text(0.5, 0.02,
         "Per setup: SAME algorithm identifies meat vs. fat cells; selection is data-derived. "
         "Threshold per cell = corridor that contains 100% of labeled examples.",
         ha="center", fontsize=9, style="italic", color="#444")

plt.tight_layout(rect=[0, 0.03, 1, 0.96])
os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
plt.savefig(OUT_PATH, dpi=140, bbox_inches="tight")
plt.close(fig)
print(f"wrote {OUT_PATH}")
