"""
Illustrative visual for the extension-chart feature redesign.
Shows: 50-SMA extension for AAPL over full daily OHLCV history (EODHD),
peak/trough pivots, downtrendlines between consecutive peaks,
and a side-histogram+KDE of peak and trough extension values
so the modal reversal levels are visible.
"""

import os
import io
import urllib.request
import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
from scipy.stats import gaussian_kde
from scipy.signal import find_peaks

TICKER = "AAPL"
OUT = r"C:\Users\Dan\Documents\ScanPerfect\swing-screener\research\ext50_aapl.png"

PIVOT_W = 10

# 1. Load daily OHLCV from EODHD (split + dividend adjusted).
#    Horizon: 2020-05-01 onwards — matches TC2000's deepest lookback, which is
#    the source of Dan's lived intuition about reversal levels. Older data would
#    mix in a different-regime AAPL that hasn't been manually labeled against.
STAT_START = "2020-05-01"
token = os.environ["EODHD_API_TOKEN"]
url = (
    f"https://eodhd.com/api/eod/{TICKER}.US"
    f"?from=2019-01-01&period=d&fmt=csv&api_token={token}"
)
with urllib.request.urlopen(url, timeout=60) as resp:
    raw = resp.read().decode("utf-8")
df = pd.read_csv(io.StringIO(raw))
df.columns = [c.strip().lower() for c in df.columns]
df["date"] = pd.to_datetime(df["date"])
df = df.sort_values("date").set_index("date")

ratio = df["adjusted_close"] / df["close"]
for col in ("open", "high", "low", "close"):
    df[col] = df[col] * ratio

# 2. ext50 = (close - SMA50) / ADR14
sma50 = df["close"].rolling(50).mean()
adr14 = (df["high"] - df["low"]).rolling(14).mean()
ext = (df["close"] - sma50) / adr14
ext = ext.dropna()
ext = ext[ext.index >= STAT_START]

# 3. Prominence-based peak detection.
#    A "reversal" is a peak that stands out from surrounding values by at least
#    one std of the extension series itself — fully self-inferring, no picked
#    threshold. Troughs are peaks of the negated series, same rule.
vals = ext.values
sigma = float(np.std(vals))
PROM = 2.0 * sigma

# Chop band = IQR of per-bar |ext|. Range where the ticker spends half its time
# with no directional conviction. Natural from the data, no picked cutoff.
chop_lo = float(np.percentile(np.abs(vals), 25))
chop_hi = float(np.percentile(np.abs(vals), 75))

peaks_idx, _ = find_peaks(vals, prominence=PROM)
troughs_idx, _ = find_peaks(-vals, prominence=PROM)

peak_dates = ext.index[peaks_idx]
peak_vals = vals[peaks_idx]
trough_dates = ext.index[troughs_idx]
trough_vals = vals[troughs_idx]

# 4. Downtrendlines between consecutive peaks, extended until broken
segments = []
peak_ordinals = np.array([mdates.date2num(d) for d in peak_dates])
for k in range(len(peaks_idx) - 1):
    if peak_vals[k + 1] >= peak_vals[k]:
        continue
    x0, y0 = peak_ordinals[k], peak_vals[k]
    x1, y1 = peak_ordinals[k + 1], peak_vals[k + 1]
    slope = (y1 - y0) / (x1 - x0)
    break_x, break_y = x1, y1
    for m in range(k + 2, len(peaks_idx)):
        proj = y0 + slope * (peak_ordinals[m] - x0)
        if peak_vals[m] > proj:
            break_x = peak_ordinals[m]
            break_y = y0 + slope * (break_x - x0)
            break
        break_x = peak_ordinals[m]
        break_y = y0 + slope * (break_x - x0)
    segments.append((x0, y0, break_x, break_y))

# 5. Figure
fig = plt.figure(figsize=(18, 10))
gs = fig.add_gridspec(1, 2, width_ratios=[4, 1], wspace=0.02)
ax_ts = fig.add_subplot(gs[0, 0])
ax_h = fig.add_subplot(gs[0, 1], sharey=ax_ts)

ylo = min(np.percentile(ext.values, 0.3), -4.0)
yhi = max(np.percentile(ext.values, 99.7), 10.0)

ax_ts.plot(ext.index, ext.values, color="#888", lw=0.8, label="ext50 (ADR14 multiples)")
ax_ts.axhline(0, color="black", lw=0.8)
ax_ts.scatter(peak_dates, peak_vals, s=36, color="#d62728", zorder=5, label=f"peaks (prominence ≥ 2σ = {PROM:.2f})")
trough_sizes = 10 + np.clip(-trough_vals, 0, None) * 6
ax_ts.scatter(trough_dates, trough_vals, s=trough_sizes, color="#1f77b4", zorder=5, label="troughs (prominence ≥ 2σ, size ~ depth)")

for (x0, y0, x1, y1) in segments:
    ax_ts.plot([x0, x1], [y0, y1], color="#d62728", lw=1.0, alpha=0.6)

ax_ts.set_title(f"{TICKER} — 50-SMA extension (ADR14 multiples)   {ext.index[0].date()} → {ext.index[-1].date()}")
ax_ts.set_ylabel("extension (× ADR14)")
ax_ts.grid(alpha=0.25)
ax_ts.legend(loc="upper left", fontsize=9)
ax_ts.xaxis.set_major_locator(mdates.YearLocator())
ax_ts.xaxis.set_major_formatter(mdates.DateFormatter("%Y"))

y_grid = np.linspace(ylo, yhi, 600)
BW = 0.4

kde_p = gaussian_kde(peak_vals, bw_method=BW)
ax_h.fill_betweenx(y_grid, 0, kde_p(y_grid), color="#d62728", alpha=0.35, label="peak KDE")
ax_h.hist(peak_vals, bins=50, orientation="horizontal", density=True,
          color="#d62728", alpha=0.30, edgecolor="#a11", range=(ylo, yhi))

kde_t = gaussian_kde(trough_vals, bw_method=BW)
ax_h.fill_betweenx(y_grid, 0, kde_t(y_grid), color="#1f77b4", alpha=0.35, label="trough KDE")
ax_h.hist(trough_vals, bins=50, orientation="horizontal", density=True,
          color="#1f77b4", alpha=0.30, edgecolor="#14a", range=(ylo, yhi))

ax_h.axhline(0, color="black", lw=0.8)
ax_h.set_xlabel("density")
ax_h.set_title("reversal distributions")
ax_h.grid(alpha=0.25)
ax_h.legend(loc="upper right", fontsize=8)
ax_h.tick_params(labelleft=False)

# Chop band — gray shading between ±chop_lo and ±chop_hi
for ax in (ax_ts, ax_h):
    ax.axhspan(chop_lo, chop_hi, color="#888", alpha=0.15)
    ax.axhspan(-chop_hi, -chop_lo, color="#888", alpha=0.15)
ax_h.text(0.02, (chop_lo + chop_hi)/2, f" chop {chop_lo:.1f}–{chop_hi:.1f}",
          fontsize=9, color="#555", va="center", transform=ax_h.get_yaxis_transform())
ax_h.text(0.02, -(chop_lo + chop_hi)/2, f" chop -{chop_hi:.1f}–-{chop_lo:.1f}",
          fontsize=9, color="#555", va="center", transform=ax_h.get_yaxis_transform())

# Three trading-actionable bands per side:
#   onset   = 10th pct (where reversal threat becomes real)
#   core    = 25-75 IQR (typical reversal zone)
#   extended= 90th pct (rare territory)
# For TROUGHS we invert: onset = 90th pct (i.e. -5 is onset, -8 is extended).
def band_levels(arr, side):
    if side == "upside":
        return dict(onset=np.percentile(arr,10), q25=np.percentile(arr,25),
                    q75=np.percentile(arr,75), extended=np.percentile(arr,90))
    else:  # downside
        return dict(onset=np.percentile(arr,90), q25=np.percentile(arr,75),
                    q75=np.percentile(arr,25), extended=np.percentile(arr,10))

pb = band_levels(peak_vals, "upside")
tb = band_levels(trough_vals, "downside")

# Draw upside bands (reds)
for ax in (ax_ts, ax_h):
    ax.axhspan(pb["q25"], pb["q75"], color="#d62728", alpha=0.10)  # core IQR
ax_h.axhline(pb["onset"], color="#a11", lw=1.2, ls="--")
ax_ts.axhline(pb["onset"], color="#a11", lw=1.0, ls="--", alpha=0.6)
ax_h.text(0.02, pb["onset"], f" onset {pb['onset']:+.2f}", fontsize=9, color="#a11", va="bottom", transform=ax_h.get_yaxis_transform())
ax_h.axhline(pb["extended"], color="#a11", lw=0.8, ls=":")
ax_ts.axhline(pb["extended"], color="#a11", lw=0.8, ls=":", alpha=0.5)
ax_h.text(0.02, pb["extended"], f" extended {pb['extended']:+.2f}", fontsize=9, color="#a11", va="bottom", transform=ax_h.get_yaxis_transform())

# Draw downside bands (blues)
for ax in (ax_ts, ax_h):
    ax.axhspan(tb["q75"], tb["q25"], color="#1f77b4", alpha=0.10)  # core IQR
ax_h.axhline(tb["onset"], color="#14a", lw=1.2, ls="--")
ax_ts.axhline(tb["onset"], color="#14a", lw=1.0, ls="--", alpha=0.6)
ax_h.text(0.02, tb["onset"], f" onset {tb['onset']:+.2f}", fontsize=9, color="#14a", va="top", transform=ax_h.get_yaxis_transform())
ax_h.axhline(tb["extended"], color="#14a", lw=0.8, ls=":")
ax_ts.axhline(tb["extended"], color="#14a", lw=0.8, ls=":", alpha=0.5)
ax_h.text(0.02, tb["extended"], f" extended {tb['extended']:+.2f}", fontsize=9, color="#14a", va="top", transform=ax_h.get_yaxis_transform())

ax_ts.set_ylim(ylo, yhi)
ax_h.set_ylim(ylo, yhi)

plt.savefig(OUT, dpi=130, bbox_inches="tight")
print(f"saved {OUT}")
print(f"bars: {len(ext)}   sigma={sigma:.3f}   prominence={PROM:.3f}")
print(f"peaks: n={len(peak_vals)}   pcts 25/50/75/90 = {np.round(np.percentile(peak_vals,[25,50,75,90]),2).tolist()}")
print(f"troughs: n={len(trough_vals)}   pcts 10/25/50/75 = {np.round(np.percentile(trough_vals,[10,25,50,75]),2).tolist()}")
print(f"chop band: +/- {chop_lo:.2f} to +/- {chop_hi:.2f}")
print(f"upside bands: onset={pb['onset']:.2f}  core {pb['q25']:.2f}-{pb['q75']:.2f}  extended {pb['extended']:.2f}")
print(f"downside bands: onset={tb['onset']:.2f}  core {tb['q25']:.2f} to {tb['q75']:.2f}  extended {tb['extended']:.2f}")
