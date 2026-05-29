import numpy as np
import matplotlib.pyplot as plt

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


def phase_scalars(curve):
    p1 = curve[:PHASE_BOUNDARY + 1]
    p2 = curve[PHASE_BOUNDARY:]
    return {
        "p1_net": (p1[-1] - p1[0]) / ADR,
        "p1_range": (p1.max() - p1.min()) / ADR,
        "p2_net": (p2[-1] - p2[0]) / ADR,
        "p2_range": (p2.max() - p2.min()) / ADR,
    }


def cost(av_s, c_s):
    d_p1_net = c_s["p1_net"] - av_s["p1_net"]
    d_p1_range = c_s["p1_range"] - av_s["p1_range"]
    d_p2_net = c_s["p2_net"] - av_s["p2_net"]
    d_p2_range = c_s["p2_range"] - av_s["p2_range"]
    p1_cost = d_p1_net ** 2 + d_p1_range ** 2
    p2_cost = d_p2_net ** 2 + d_p2_range ** 2
    return d_p1_net, d_p1_range, d_p2_net, d_p2_range, p1_cost, p2_cost, p1_cost + p2_cost


ex = avatar()
ex_s = phase_scalars(ex)

cands = []

cands.append(("Self-match", ex.copy()))

cands.append(("Same shape, 2x magnitude", ex * 2.0))

cands.append(("Tight match + small noise", ex + np.random.normal(0, 0.006, N)))

fast_rip = np.linspace(0, 0.18, 7)
fpt = np.arange(33)
cands.append((
    "Faster rip (7 bars), similar range",
    np.concatenate([fast_rip, 0.18 + np.sin(fpt * 0.7) * 0.05 * np.exp(-fpt / 13)])
))

ept = np.arange(30)
cands.append((
    "Right rip, EXPANDING range",
    np.concatenate([np.linspace(0, 0.18, 10), 0.18 + np.sin(ept * 0.6) * 0.04 * np.exp(ept / 50)])
))

cands.append(("Random walk", np.cumsum(np.random.normal(0, 0.015, N))))

cands.append(("Inverted shape", -ex + ex[0] * 2))

scored = []
for name, c in cands:
    cs = phase_scalars(c)
    d1n, d1r, d2n, d2r, p1c, p2c, total = cost(ex_s, cs)
    scored.append((name, c, cs, d1n, d1r, d2n, d2r, p1c, p2c, total))

scored = sorted(scored, key=lambda x: x[9])

fig, axes = plt.subplots(4, 2, figsize=(14, 16))
axes = axes.flatten()

ax = axes[0]
ax.plot(t, ex, 'k-', linewidth=2.5)
ax.axvline(PHASE_BOUNDARY, color='red', linestyle='--', alpha=0.5, linewidth=1.2)
ax.set_title("AVATAR\n"
             f"Phase 1 (bars 0-10): net = {ex_s['p1_net']:+.1f} ADR, range = {ex_s['p1_range']:.1f} ADR\n"
             f"Phase 2 (bars 10-39): net = {ex_s['p2_net']:+.1f} ADR, range = {ex_s['p2_range']:.1f} ADR",
             fontsize=10, fontweight='bold')
ax.grid(True, alpha=0.3)
ax.set_xlabel("Bar offset")
ax.set_ylabel("Log return from start")
ax.text(5, 0.02, "Phase 1\n(rip)", ha='center', fontsize=9, color='gray')
ax.text(25, 0.02, "Phase 2\n(contracting range)", ha='center', fontsize=9, color='gray')

for i, (name, curve, cs, d1n, d1r, d2n, d2r, p1c, p2c, total) in enumerate(scored):
    if i + 1 >= len(axes):
        break
    ax = axes[i + 1]
    ax.plot(t, ex, 'k-', alpha=0.35, linewidth=1.5, label='avatar')
    ax.plot(t, curve, 'b-', linewidth=2, label='candidate')
    ax.axvline(PHASE_BOUNDARY, color='red', linestyle='--', alpha=0.4, linewidth=1.0)

    color = 'darkgreen' if total < 1 else 'goldenrod' if total < 25 else 'darkorange' if total < 100 else 'darkred'
    title = (
        f"{name}\n"
        f"P1: Δnet={d1n:+.1f}, Δrange={d1r:+.1f}  →  cost {p1c:.1f}\n"
        f"P2: Δnet={d2n:+.1f}, Δrange={d2r:+.1f}  →  cost {p2c:.1f}\n"
        f"TOTAL = {total:.1f}"
    )
    ax.set_title(title, fontsize=9.5, color=color, loc='left')
    ax.grid(True, alpha=0.3)
    ax.legend(fontsize=9, loc='best')

for j in range(len(scored) + 1, len(axes)):
    axes[j].set_visible(False)

plt.suptitle("Per-phase 2-scalar scoring (net move + range size in ADR units, per phase)\n"
             "Lower total = better match. Self-match = 0.",
             fontsize=12, y=1.00)
plt.tight_layout()

out = "viz_phase_scoring_demo.png"
plt.savefig(out, dpi=120, bbox_inches='tight')
print(f"Saved {out}")
print()
print("Avatar scalars:")
for k, v in ex_s.items():
    print(f"  {k}: {v:+.2f}")
print()
print("Ranked by per-phase cost (lower is better):")
for name, _, cs, d1n, d1r, d2n, d2r, p1c, p2c, total in scored:
    print(f"  total={total:7.2f}  P1={p1c:6.2f}  P2={p2c:6.2f}   {name}")
