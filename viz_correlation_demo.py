import numpy as np
import matplotlib.pyplot as plt
from scipy.stats import pearsonr

np.random.seed(7)
N = 40
t = np.arange(N)


def avatar():
    rip = np.linspace(0, 0.18, 10)
    rng_t = np.arange(30)
    decay = np.exp(-rng_t / 12)
    osc = np.sin(rng_t * 0.7) * 0.05 * decay
    return np.concatenate([rip, 0.18 + osc])


ex = avatar()

cands = []

cands.append(("Same shape, 2x magnitude", ex * 2.0))

cands.append(("Tight match + small noise", ex + np.random.normal(0, 0.006, N)))

fast_rip = np.linspace(0, 0.18, 7)
fpt = np.arange(33)
cands.append((
    "Faster rip (7 bars vs 10), similar range",
    np.concatenate([fast_rip, 0.18 + np.sin(fpt * 0.7) * 0.05 * np.exp(-fpt / 13)])
))

ept = np.arange(30)
cands.append((
    "Right rip, EXPANDING range (wrong)",
    np.concatenate([np.linspace(0, 0.18, 10), 0.18 + np.sin(ept * 0.6) * 0.04 * np.exp(ept / 50)])
))

cands.append(("Random walk", np.cumsum(np.random.normal(0, 0.015, N))))

cands.append(("Inverted shape", -ex + ex[0] * 2))

scored = [(name, c, pearsonr(ex, c)[0]) for name, c in cands]
scored = sorted(scored, key=lambda x: -x[2])

fig, axes = plt.subplots(4, 2, figsize=(13, 14))
axes = axes.flatten()

axes[0].plot(t, ex, 'k-', linewidth=2.5)
axes[0].set_title("AVATAR (the one example)", fontsize=12, fontweight='bold')
axes[0].grid(True, alpha=0.3)
axes[0].set_xlabel("Bar offset in 40-bar window")
axes[0].set_ylabel("Log return from start of window")

for i, (name, curve, r) in enumerate(scored):
    if i + 1 >= len(axes):
        break
    ax = axes[i + 1]
    ax.plot(t, ex, 'k-', alpha=0.35, linewidth=1.5, label='avatar')
    ax.plot(t, curve, 'b-', linewidth=2, label='candidate')
    color = 'darkgreen' if r > 0.9 else 'darkorange' if r > 0.5 else 'dimgray' if r > -0.5 else 'darkred'
    ax.set_title(f"{name}\nPearson r = {r:+.3f}", fontsize=11, color=color)
    ax.grid(True, alpha=0.3)
    ax.legend(fontsize=9, loc='best')

for j in range(len(scored) + 1, len(axes)):
    axes[j].set_visible(False)

plt.suptitle("Curve-vs-curve correlation: how Pearson r scores shape similarity",
             fontsize=13, y=1.00)
plt.tight_layout()
plt.savefig("viz_correlation_demo.png", dpi=120, bbox_inches='tight')

print("Saved viz_correlation_demo.png")
print()
print("Ranked by correlation:")
for name, _, r in scored:
    print(f"  r = {r:+.3f}   {name}")
