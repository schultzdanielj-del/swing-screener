"""Test the two-phase model on the 2025 bull cycle (Apr 28 -> end Oct).
Market-level: distance from 50SMA, 21EMA tests, locate the end-July range
expansion, compare phase-1 vs phase-2 extension trend. Read-only."""
import pickle
from pathlib import Path
import numpy as np
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates

CACHE = Path(r"C:\Users\Dan\Documents\ScanPerfect\swing-screener\local_runner\cache\universe_ohlcv_daily.pkl")
OUT = Path(r"C:\Users\Dan\Documents\ScanPerfect\swing-screener\research\cycle2025_phases.png")

with open(CACHE, "rb") as f:
    cache = pickle.load(f)
print("tickers in cache:", len(cache))

A0, A1 = pd.Timestamp("2025-04-15"), pd.Timestamp("2025-10-31")   # analysis window
RESET_LO, RESET_HI = pd.Timestamp("2025-07-15"), pd.Timestamp("2025-08-08")


def frame(tk):
    d = cache[tk].copy()
    d["date"] = pd.to_datetime(d["date"])
    d = d.sort_values("date").set_index("date")
    c = d["close"].astype(float); o = d["open"].astype(float)
    out = pd.DataFrame(index=d.index)
    out["close"] = c; out["open"] = o
    out["sma50"] = c.rolling(50).mean()
    out["sma200"] = c.rolling(200).mean()
    out["ema21"] = c.ewm(span=21, adjust=False).mean()
    out["ext"] = (c / out["sma50"] - 1) * 100
    out["d1"] = c.pct_change() * 100
    out["gap"] = (o / c.shift(1) - 1) * 100
    return out


def analyze(tk):
    if tk not in cache:
        print(f"\n##### {tk}: NOT IN CACHE")
        return None
    f = frame(tk)
    w = f.loc[A0:A1]
    print(f"\n{'='*68}\n{tk}  — 2025 cycle (Apr 28 -> Oct 31)\n{'='*68}")

    # cycle start: first reclaim & hold of 50SMA after mid-April
    above = w["close"] > w["sma50"]
    start = None
    for dt in w.index:
        if above.loc[dt]:
            start = dt; break
    if start is not None:
        r = w.loc[start]
        print(f"50SMA reclaim (cycle start): {start.date()}  close {r['close']:.2f}  "
              f"d1 {r['d1']:+.1f}%  gap {r['gap']:+.1f}%")

    # global max extension in the cycle
    emax_dt = w["ext"].idxmax(); emax = w["ext"].max()
    print(f"GLOBAL MAX extension from 50SMA: {emax_dt.date()}  +{emax:.1f}%")

    # end-July range expansion: biggest down day in the reset window
    rw = w.loc[RESET_LO:RESET_HI]
    rd = rw["d1"].idxmin(); rr = rw.loc[rd]
    # how far below 21EMA / 50SMA over the next 5 sessions
    fwd = f.loc[rd:].head(6)
    min_vs_ema = ((fwd["close"] / fwd["ema21"] - 1) * 100).min()
    min_vs_50 = ((fwd["close"] / fwd["sma50"] - 1) * 100).min()
    print(f"END-JULY RANGE EXPANSION: {rd.date()}  d1 {rr['d1']:+.1f}%  gap {rr['gap']:+.1f}%  "
          f"ext that day +{rr['ext']:.1f}%")
    print(f"   over next 5 sessions: low reached {min_vs_ema:+.1f}% vs 21EMA, {min_vs_50:+.1f}% vs 50SMA")

    # monthly max extension -> phase-1 rise vs phase-2 decline
    print("monthly MAX extension above 50SMA:")
    for m, g in w.groupby(w.index.to_period("M")):
        dt = g["ext"].idxmax()
        print(f"   {str(m)}:  +{g['ext'].max():4.1f}%  (peak {dt.date()})   price hi {g['close'].max():.2f}")

    # cycle end: first decisive close below 21EMA AND 50SMA after Oct 1
    oct_ = f.loc["2025-10-01":"2025-12-15"]
    end = None
    for dt in oct_.index:
        r = oct_.loc[dt]
        if r["close"] < r["ema21"] and r["close"] < r["sma50"]:
            end = dt; break
    if end is not None:
        r = oct_.loc[end]
        print(f"phase-2 end (close < 21EMA & 50SMA): {end.date()}  close {r['close']:.2f}  d1 {r['d1']:+.1f}%")
    else:
        print("phase-2 end: never closed below both 21EMA & 50SMA through mid-Dec (cycle persisted)")
    return f


frames = {}
for tk in ["SPY", "QQQ", "RSP"]:
    fr = analyze(tk)
    if fr is not None:
        frames[tk] = fr

# ---- plot SPY ----
if "SPY" in frames:
    f = frames["SPY"].loc["2025-03-15":"2025-11-15"]
    reset_dt = f.loc[RESET_LO:RESET_HI, "d1"].idxmin()
    emax_dt = f.loc[A0:A1, "ext"].idxmax()
    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(13, 9), sharex=True, facecolor="black",
                                   gridspec_kw={"height_ratios": [2.4, 1]})
    for ax in (ax1, ax2):
        ax.set_facecolor("black"); ax.tick_params(colors="#bbb"); ax.grid(True, alpha=0.2)
    ax1.plot(f.index, f["close"], color="white", lw=1.2, label="SPY close")
    ax1.plot(f.index, f["sma50"], color="#ffcc00", lw=1.0, label="50 SMA")
    ax1.plot(f.index, f["ema21"], color="#5fc8ff", lw=1.0, label="21 EMA")
    ax1.axvspan(pd.Timestamp("2025-04-28"), reset_dt, color="#1eff1e", alpha=0.07)
    ax1.axvspan(reset_dt, pd.Timestamp("2025-11-01"), color="#ffaa00", alpha=0.07)
    ax1.axvline(reset_dt, color="#ff3030", lw=1.2, ls="--")
    ax1.axvline(emax_dt, color="#00ffd5", lw=1.0, ls=":")
    ax1.text(reset_dt, f["close"].max(), " range expansion", color="#ff6666", fontsize=9, va="top")
    ax1.text(emax_dt, f["close"].min(), " max ext", color="#00ffd5", fontsize=8, va="bottom", ha="right")
    ax1.legend(loc="upper left", fontsize=8, facecolor="#111", edgecolor="#444", labelcolor="#ddd")
    ax1.set_title("SPY 2025 cycle — phase 1 (green) / range-expansion reset / phase 2 (amber)", color="white")
    ax2.plot(f.index, f["ext"], color="#9b8cff", lw=1.1)
    ax2.axhline(0, color="#888", lw=0.7)
    ax2.axvline(reset_dt, color="#ff3030", lw=1.2, ls="--")
    ax2.fill_between(f.index, 0, f["ext"], where=f["ext"] > 0, color="#1eff1e", alpha=0.25)
    ax2.fill_between(f.index, 0, f["ext"], where=f["ext"] < 0, color="#ff3030", alpha=0.25)
    ax2.set_ylabel("% above 50SMA", color="#ddd")
    ax2.xaxis.set_major_locator(mdates.MonthLocator())
    ax2.xaxis.set_major_formatter(mdates.DateFormatter("%b"))
    plt.tight_layout()
    plt.savefig(OUT, facecolor="black", dpi=130, bbox_inches="tight")
    print(f"\nsaved plot: {OUT}")
