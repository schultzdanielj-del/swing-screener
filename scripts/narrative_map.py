"""Browsable AI narrative map: the buildout chain (inputs), the outputs (what AI
enables), the half-in adjacents, and the off-narrative noise. Concept diagram, no
data. Saves PNG."""
from pathlib import Path
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import FancyBboxPatch

OUT = Path(r"C:\Users\Dan\Documents\ScanPerfect\swing-screener\research\narrative_map.png")

fig, ax = plt.subplots(figsize=(17, 11), facecolor="black")
ax.set_facecolor("black"); ax.set_xlim(0, 170); ax.set_ylim(0, 124); ax.axis("off")

def box(x, y, w, h, title, lines, fc, tc="white"):
    ax.add_patch(FancyBboxPatch((x, y), w, h, boxstyle="round,pad=0.6,rounding_size=2",
                                facecolor=fc, edgecolor="#888", lw=1.3, alpha=0.92))
    ax.text(x + w/2, y + h - 3, title, ha="center", va="top", color=tc, fontsize=11, fontweight="bold")
    for i, ln in enumerate(lines):
        ax.text(x + w/2, y + h - 7.5 - i*4.2, ln, ha="center", va="top", color="#e8e8e8", fontsize=9)

def arrow(x0, y0, x1, y1, txt="", col="#aaa"):
    ax.annotate("", xy=(x1, y1), xytext=(x0, y0), arrowprops=dict(arrowstyle="-|>", color=col, lw=2))
    if txt: ax.text((x0+x1)/2, (y0+y1)/2 + 1.5, txt, ha="center", color=col, fontsize=9, style="italic")

# noise strip
box(5, 108, 160, 13, "OFF-NARRATIVE  -  NOISE  (filter out)",
    ["Consumer / retail / beauty / restaurants   .   Beaten-down & AI-DISRUPTED SaaS   .   "
     "Defensives (managed care, gold-as-hedge)   .   Banks (rates)   .   Shipping (idiosyncratic)"],
    "#3a1414")

ax.text(38, 105, "THE BUILDOUT CHAIN  (inputs / picks & shovels)", ha="center", color="#7fd0ff", fontsize=12, fontweight="bold")
ax.text(126, 105, "WHAT AI ENABLES  (outputs / applications)", ha="center", color="#7dff9e", fontsize=12, fontweight="bold")

# chain (left, top->bottom)
box(6, 86, 64, 14, "HUB  -  AI Compute + Datacenters", ["NVDA  AVGO  AMD   .   DELL  SMCI   .   datacenter buildout"], "#0d3a5c")
box(6, 69, 64, 14, "AI Infrastructure", ["Networking ANET CRDO  .  Memory MU SNDK  .  Optics POET"], "#114a6e")
box(6, 52, 64, 14, "Power", ["Nuclear OKLO CCJ  .  Grid VST GEV  .  Storage  .  (Solar* half-in)"], "#155a80")
box(6, 35, 64, 14, "Raw Materials   <-  FLOOR  (here now / rolling over)", ["Critical minerals  .  Copper  .  Uranium  .  Industrial metals"], "#1a6a92")
arrow(2.5, 96, 2.5, 38, col="#7fd0ff")
ax.text(0.5, 67, "money walked DOWN  2023 -> 2025", rotation=90, ha="center", va="center", color="#7fd0ff", fontsize=9.5, style="italic")

arrow(70, 93, 84, 93, "AI enables ->", "#7dff9e")

# outputs (right)
box(84, 76, 80, 23, "OUTPUT  -  Frontier / explosive  (PURE AI-applications)",
    ["Robotics (physical AI)   .   Space / Autonomy   .   AI Apps & Agents",
     "Quantum   .   Biotech (AI drug discovery)",
     "-- story-driven  .  big moves, big fails  .  the 'next leg' candidates --"], "#1f5c2a")
box(84, 33, 80, 39, "OUTPUT  -  Steady grinders  (HALF-IN: own engine + AI tailwind)",
    ["Cybersecurity   ->   FTNT   TENB   CRWD",
     "Cloud / infra software (runs + monitors the build)   ->   DDOG   TWLO",
     "Dev / collab tools (AI tailwind OR disruption)   ->   GTLB   TEAM*",
     "",
     "-- cleaner trends, fewer fireworks  .  TEAM* leans disruption-risk --"], "#1f6b4a")

# legend
box(6, 4, 158, 24, "HOW TO BROWSE IT",
    ["GREEN zones = CHASE (connected + fresh): the chain layers + both output boxes  -  hunt the SOLDIERS, not the mega-cap generals",
     "RED zone (top) = NOISE: orphans, AI-disrupted SaaS, defensives, banks, the 'everything scattering' pops  -  filter out, however good the chart looks",
     "Frontier output = explosive / story (robotics, space)   |   Grinder output = steady trend (cyber, DDOG, TWLO)   |   GTLB/TEAM = contested (tailwind vs disruption)",
     "Master test for any ticker: is it CONNECTED (chain layer / output branch) or an ORPHAN?  -  that one question screens ~80% of the noise."],
    "#1a1a1a")

plt.tight_layout()
plt.savefig(OUT, facecolor="black", dpi=140, bbox_inches="tight")
print(f"saved: {OUT}")
