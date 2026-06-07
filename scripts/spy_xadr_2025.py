"""Rebuild the 2025 cycle on x-ADR (distance from 50SMA measured in average daily
ranges), the way the TC2000 'X ADR to 50sma' panel does it -- NOT raw % extension.
Shows how the peak location moves vs the % version. ASCII-only output. Read-only."""
import pickle
from pathlib import Path
import numpy as np
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates

CACHE = Path(r"C:\Users\Dan\Documents\ScanPerfect\swing-screener\local_runner\cache\universe_ohlcv_daily.pkl")
OUT = Path(r"C:\Users\Dan\Documents\ScanPerfect\swing-screener\research\spy_xadr_2025.png")
ADR_N = 20

with open(CACHE, "rb") as f:
    cache = pickle.load(f)
print("tickers in cache:", len(cache))
print("SPY columns:", list(cache["SPY"].columns))


def build(tk):
    d = cache[tk].copy()
    d["date"] = pd.to_datetime(d["date"])
    d = d.sort_values("date").set_index("date")
    c = d["close"].astype(float); h = d["high"].astype(float); lo = d["low"].astype(float)
    out = pd.DataFrame(index=d.index)
    out["close"] = c
    out["sma50"] = c.rolling(50).mean()
    out["ema21"] = c.ewm(span=21, adjust=False).mean()
    adr_pct = 100 * ((h / lo).rolling(ADR_N).mean() - 1)        # average daily range, %
    out["ext_pct"] = (c / out["sma50"] - 1) * 100               # extension, %
    out["xadr"] = out["ext_pct"] / adr_pct                      # extension in ADRs
    out["d1"] = c.pct_change() * 100
    return out


f = build("SPY")
cyc = f.loc["2025-05-01":"2025-10-31"]

print("\n=== 2025 SPY cycle: x-ADR vs % extension ===")
pk_x = cyc["xadr"].idxmax(); pk_p = cyc["ext_pct"].idxmax()
print(f"GLOBAL MAX in x-ADR  : {pk_x.date()}  {cyc['xadr'].max():.2f} ADR")
print(f"GLOBAL MAX in % ext  : {pk_p.date()}  +{cyc['ext_pct'].max():.1f}%   <- what I used before")
print(f"=> peak moves {(pk_x - pk_p).days:+d} days when measured in ADR")

reset = f.loc["2025-07-15":"2025-08-08", "d1"].idxmin()
print(f"\nend-July range expansion: {reset.date()}  d1 {f.loc[reset,'d1']:+.1f}%  "
      f"x-ADR that day {f.loc[reset,'xadr']:.2f}")
print(f"gap between x-ADR peak and range expansion: {(reset - pk_x).days} days")

print("\nmonthly MAX x-ADR (the lower-high downtrend):")
for m, g in f.loc["2025-05-01":"2025-12-31"].groupby(f.loc['2025-05-01':'2025-12-31'].index.to_period("M")):
    dt = g["xadr"].idxmax()
    print(f"   {str(m)}:  {g['xadr'].max():4.2f} ADR  (peak {dt.date()})")

# second wind: local max in Sep vs the July primary peak (should be lower = capped)
sep = f.loc["2025-09-01":"2025-09-30"]
sep_pk = sep["xadr"].idxmax()
print(f"\nSeptember second-wind local peak: {sep_pk.date()}  {sep['xadr'].max():.2f} ADR  "
      f"(vs July primary {cyc['xadr'].max():.2f} -> {'CAPPED below primary' if sep['xadr'].max() < cyc['xadr'].max() else 'EXCEEDED primary'})")

# reset below 50SMA (x-ADR crosses negative)
post = f.loc["2025-10-01":"2026-02-01"]
neg = post[post["xadr"] < 0]
if len(neg):
    print(f"reset below 50SMA (x-ADR < 0): first on {neg.index[0].date()}  ({neg['xadr'].iloc[0]:.2f} ADR)")
else:
    print("x-ADR never went negative through early Feb 2026")

# ---- plot replicating the 'X ADR to 50sma' panel ----
p = f.loc["2025-06-01":"2026-01-15"]
fig, (a1, a2) = plt.subplots(2, 1, figsize=(13, 9), sharex=True, facecolor="black",
                             gridspec_kw={"height_ratios": [2.3, 1.2]})
for ax in (a1, a2):
    ax.set_facecolor("black"); ax.tick_params(colors="#bbb"); ax.grid(True, alpha=0.18)
a1.plot(p.index, p["close"], color="white", lw=1.1, label="SPY")
a1.plot(p.index, p["sma50"], color="#ffcc00", lw=1.0, label="50 SMA")
a1.plot(p.index, p["ema21"], color="#5fc8ff", lw=1.0, label="21 EMA")
a1.legend(loc="upper left", fontsize=8, facecolor="#111", edgecolor="#444", labelcolor="#ddd")
a1.set_title("SPY 2025 -- X ADR to 50SMA (ADR-normalized extension)", color="white")
col = np.where(p["xadr"] >= 0, "#1eff1e", "#ff3030")
a2.bar(p.index, p["xadr"], color=col, width=1.0, alpha=0.8)
a2.axhline(0, color="#888", lw=0.7)
a2.axvline(pk_x, color="#00ffd5", lw=1.0, ls=":")
a2.axvline(reset, color="#ffffff", lw=1.0, ls="--")
a2.axvline(sep_pk, color="#ffaa00", lw=1.0, ls=":")
a2.set_ylabel("x ADR", color="#ddd")
a2.xaxis.set_major_locator(mdates.MonthLocator())
a2.xaxis.set_major_formatter(mdates.DateFormatter("%b"))
plt.tight_layout(); plt.savefig(OUT, facecolor="black", dpi=130, bbox_inches="tight")
print(f"\nsaved plot: {OUT}")
