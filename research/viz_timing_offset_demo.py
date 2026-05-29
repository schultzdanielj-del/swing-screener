import numpy as np
import matplotlib.pyplot as plt
from scipy.stats import pearsonr

np.random.seed(7)
N = 40
t = np.arange(N)
PHASE_BOUNDARY = 10
ADR = 0.02


def avatar():
    rip = np.linspace(0, 0.18, 10)
    rng_t = np.arange(30)
    decay = np.exp(-rng_t / 12)
    osc = np.sin(rng_t * 0.7) * 0.05 * decay
    return np.concatenate([rip, 0.18 + osc])


def shifted_avatar(shift):
    if shift == 0:
        return avatar()
    av = avatar()
    pre = np.zeros(shift)
    post = av[:N - shift]
    return np.concatenate([pre, post])


def phase_scalars(curve):
    p1 = curve[:PHASE_BOUNDARY + 1]
    p2 = curve[PHASE_BOUNDARY:]
    return {
        "p1_net": (p1[-1] - p1[0]) / ADR,
        "p1_range": (p1.max() - p1.min()) / ADR,
        "p2_net": (p2[-1] - p2[0]) / ADR,
        "p2_range": (p2.max() - p2.min()) / ADR,
    }


ex = avatar()
ex_s = phase_scalars(ex)

shifts = [0, 1, 2, 3, 5, 8]
results = []
for s in shifts:
    c = shifted_avatar(s)
    r = pearsonr(ex, c)[0]
    cs = phase_scalars(c)
    d1n = cs["p1_net"] - ex_s["p1_net"]
    d1r = cs["p1_range"] - ex_s["p1_range"]
    d2n = cs["p2_net"] - ex_s["p2_net"]
    d2r = cs["p2_range"] - ex_s["p2_range"]
    p1c = d1n ** 2 + d1r ** 2
    p2c = d2n ** 2 + d2r ** 2
    results.append((s, c, r, p1c, p2c, p1c + p2c))

fig, axes = plt.subplots(3, 2, figsize=(13, 12))
axes = axes.flatten()

for i, (s, c, r, p1c, p2c, total) in enumerate(results):
    ax = axes[i]
    ax.plot(t, ex, 'k-', alpha=0.4, linewidth=1.5, label='avatar')
    ax.plot(t, c, 'b-', linewidth=2, label=f'same shape, shifted +{s}')
    ax.axvline(PHASE_BOUNDARY, color='red', linestyle='--', alpha=0.5, linewidth=1.0)
    ax.set_title(
        f"Shift = +{s} bars\n"
        f"Pearson r = {r:+.3f}     Phase total = {total:.1f} (P1 {p1c:.1f} + P2 {p2c:.1f})",
        fontsize=10.5
    )
    ax.grid(True, alpha=0.3)
    ax.legend(fontsize=9, loc='best')

plt.suptitle("Same avatar shape, shifted forward 0/1/2/3/5/8 bars in the 40-bar window\n"
             "How each scoring method handles a pure timing offset",
             fontsize=12, y=1.00)
plt.tight_layout()
plt.savefig("viz_timing_offset_demo.png", dpi=120, bbox_inches='tight')

print("Saved viz_timing_offset_demo.png")
print()
print(f"{'Shift':>6}  {'Pearson r':>10}  {'Phase total':>12}  (P1 + P2)")
for s, _, r, p1c, p2c, total in results:
    print(f"  +{s:<3}  {r:+.3f}      {total:7.2f}     ({p1c:6.2f} + {p2c:6.2f})")
