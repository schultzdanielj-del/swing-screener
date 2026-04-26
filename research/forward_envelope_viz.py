"""Visual: forward MA tapes overlay (28 examples + sample wild WINs and wild LOSSes)."""
from __future__ import annotations
import json, os, pickle, sys
import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from forward_envelope_diag import build_forward_lr, SMA_PERIODS, EMA_PERIODS

MAIN_REPO = r"C:/Users/Dan/Documents/ScanPerfect/swing-screener"
HALTED = os.path.join(MAIN_REPO, "data", "signal_exit_grind",
                      "signal_exit_pool_htf_HALTED_20260425_202509.json")
OHLCV = os.path.join(MAIN_REPO, "local_runner", "cache", "universe_ohlcv_daily.pkl")
PER = os.path.join(MAIN_REPO, "research", "forward_envelope_diag", "htf_per_cluster.json")
OUT = os.path.join(MAIN_REPO, "research", "forward_envelope_diag", "htf_overlay.png")


def main():
    halted = json.load(open(HALTED))
    per = json.load(open(PER))
    with open(OHLCV, "rb") as f:
        ohlcv = pickle.load(f)

    cluster_meta = {m["cluster_id"]: m for m in halted["cluster_meta"]
                    if m["status"] == "ENTERED"}

    fig, axes = plt.subplots(1, 2, figsize=(16, 7))

    # Left panel: example forward close tapes in ADR units
    ax = axes[0]
    for ex in per["examples"]:
        m = cluster_meta[ex["cluster_id"]]
        ticker = m["ticker"]
        df = ohlcv.get(ticker)
        if df is None:
            continue
        close = df["close"].values
        e = m["entry_bar"]; cap = m["cap_bar"]
        adr = m["adr14_at_entry"]
        eff = m["effective_entry"]
        fwd = (close[e: cap + 1] - eff) / adr
        ax.plot(np.arange(len(fwd)), fwd, "g-", alpha=0.4, lw=1.0)
        ax.text(len(fwd) - 1, fwd[-1], ticker, fontsize=6, color="darkgreen", alpha=0.6)
    ax.axhline(0, color="black", lw=0.5)
    ax.axhline(-1, color="red", lw=0.5, ls="--", label="stop (-1 ADR)")
    ax.set_xlabel("Bars after entry")
    ax.set_ylabel("(close − effective_entry) / ADR14")
    ax.set_title(f"HTF examples (n={len(per['examples'])}) — forward close tapes")
    ax.legend(loc="upper left")
    ax.grid(True, alpha=0.3)

    # Right panel: wild WIN (blue) vs wild LOSS (red) sample
    ax = axes[1]
    wild = per["wild"]
    wild_win = sorted([w for w in wild if w["verdict"] == "WIN"],
                      key=lambda x: -x["mfe_adr"])[:15]
    wild_loss = sorted([w for w in wild if w["verdict"] == "LOSS"],
                       key=lambda x: -x["mfe_adr"])[:15]

    for w_list, color, label in [(wild_win, "blue", "(5)-WIN"),
                                  (wild_loss, "red", "(5)-LOSS (high MFE — round-trips)")]:
        first = True
        for w in w_list:
            m = cluster_meta[w["cluster_id"]]
            ticker = m["ticker"]
            df = ohlcv.get(ticker)
            if df is None:
                continue
            close = df["close"].values
            e = m["entry_bar"]; cap = m["cap_bar"]
            adr = m["adr14_at_entry"]
            eff = m["effective_entry"]
            fwd = (close[e: cap + 1] - eff) / adr
            ax.plot(np.arange(len(fwd)), fwd, color=color, alpha=0.5, lw=0.8,
                    label=label if first else None)
            first = False
            stop_bar = m["stop_hit_bar"]
            if stop_bar is not None and stop_bar < m["horizon"]:
                ax.axvline(stop_bar + 1, ymin=0, ymax=0.05, color=color, alpha=0.3)
    ax.axhline(0, color="black", lw=0.5)
    ax.axhline(-1, color="red", lw=0.5, ls="--", label="stop (-1 ADR)")
    ax.set_xlabel("Bars after entry")
    ax.set_ylabel("(close − effective_entry) / ADR14")
    ax.set_title(f"HTF wild — top-15 by MFE in each (5)-verdict")
    ax.legend(loc="upper left")
    ax.grid(True, alpha=0.3)

    plt.tight_layout()
    plt.savefig(OUT, dpi=110)
    print(f"Wrote {OUT}")


if __name__ == "__main__":
    main()
