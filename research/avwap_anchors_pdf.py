"""Render argmax-AVWAP anchor placement on all 113 breakout example entries to a PDF.

Output: research/avwap_anchors_all_113.pdf
One page per 4 examples (2x2 grid). Per panel:
  - candlesticks from anchor-5 to sig+5 (clipped to available bars)
  - AVWAP line drawn from anchor forward to end of window
  - vertical markers: anchor (blue), signal (orange), entry (red dashed)
  - title: setup ticker entry  anchor=YYYY-MM-DD (Nb back)  AVWAP=X.XX  close=X.XX
"""
import os
import pickle
import sqlite3
import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
from matplotlib.backends.backend_pdf import PdfPages
from matplotlib.patches import Rectangle

CACHE = r"C:/Users/Dan/Documents/ScanPerfect/swing-screener/local_runner/cache/universe_ohlcv_daily.pkl"
DB = r"C:/Users/Dan/Documents/ScanPerfect/swing-screener/data/scanperfect.db"
OUT = r"C:/Users/Dan/Documents/ScanPerfect/swing-screener-win-loss-classifier/research/avwap_anchors_all_113.pdf"

with sqlite3.connect(DB) as conn:
    ex_rows = conn.execute(
        "SELECT setup_type, ticker, entry_date FROM examples "
        "WHERE setup_type IN ('htf','bf','base') ORDER BY setup_type, entry_date"
    ).fetchall()

with open(CACHE, "rb") as f:
    universe = pickle.load(f)


def compute(ticker, entry_date):
    if ticker not in universe:
        return None
    df = universe[ticker]
    if not pd.api.types.is_datetime64_any_dtype(df["date"]):
        df = df.copy()
        df["date"] = pd.to_datetime(df["date"])
    dates = df["date"].dt.strftime("%Y-%m-%d").values
    o = df["open"].values.astype(float)
    h = df["high"].values.astype(float)
    l = df["low"].values.astype(float)
    c = df["close"].values.astype(float)
    v = df["volume"].values.astype(float)
    tp = (h + l + c) / 3.0

    hits = np.where(dates == entry_date)[0]
    if len(hits) == 0:
        return None
    entry_idx = int(hits[0])
    sig_idx = entry_idx - 1
    if sig_idx < 1:
        return None

    cum_tpv = np.concatenate([[0.0], np.cumsum(tp * v)])
    cum_v = np.concatenate([[0.0], np.cumsum(v)])
    A_range = np.arange(0, sig_idx)
    total_tpv = cum_tpv[sig_idx + 1] - cum_tpv[A_range]
    total_v = cum_v[sig_idx + 1] - cum_v[A_range]
    with np.errstate(invalid="ignore", divide="ignore"):
        avwaps = np.where(total_v > 0, total_tpv / total_v, -np.inf)
    argmax_A = int(A_range[int(np.argmax(avwaps))])
    argmax_val = float(avwaps.max())

    # AVWAP series from anchor forward to end of chart window
    window_start = max(0, argmax_A - 5)
    window_end = min(len(df) - 1, entry_idx + 5)
    # AVWAP series from argmax_A through window_end
    h_a = h[argmax_A:window_end + 1]
    l_a = l[argmax_A:window_end + 1]
    c_a = c[argmax_A:window_end + 1]
    v_a = v[argmax_A:window_end + 1]
    tp_a = (h_a + l_a + c_a) / 3.0
    cum_tpv_a = np.cumsum(tp_a * v_a)
    cum_v_a = np.cumsum(v_a)
    with np.errstate(invalid="ignore", divide="ignore"):
        avwap_line = np.where(cum_v_a > 0, cum_tpv_a / cum_v_a, np.nan)

    return {
        "ticker": ticker,
        "entry_date": entry_date,
        "dates": dates,
        "o": o, "h": h, "l": l, "c": c,
        "sig_idx": sig_idx,
        "entry_idx": entry_idx,
        "argmax_A": argmax_A,
        "argmax_val": argmax_val,
        "argmax_date": dates[argmax_A],
        "window_start": window_start,
        "window_end": window_end,
        "avwap_line_start_idx": argmax_A,
        "avwap_line": avwap_line,
    }


def draw_candles(ax, idx_array, o, h, l, c):
    for i, idx in enumerate(idx_array):
        color = "#2ca02c" if c[idx] >= o[idx] else "#d62728"
        ax.plot([i, i], [l[idx], h[idx]], color="black", linewidth=0.5, zorder=2)
        body_bot = min(o[idx], c[idx])
        body_top = max(o[idx], c[idx])
        rect = Rectangle((i - 0.3, body_bot), 0.6, max(body_top - body_bot, 1e-6),
                         facecolor=color, edgecolor="black", linewidth=0.3, zorder=3)
        ax.add_patch(rect)


def render_panel(ax, setup, data):
    ws, we = data["window_start"], data["window_end"]
    idx_array = np.arange(ws, we + 1)
    draw_candles(ax, idx_array, data["o"], data["h"], data["l"], data["c"])

    # AVWAP line in chart coordinates (x = position within idx_array)
    avwap_x_start = data["avwap_line_start_idx"] - ws
    avwap_x = np.arange(avwap_x_start, avwap_x_start + len(data["avwap_line"]))
    ax.plot(avwap_x, data["avwap_line"], color="purple", linewidth=1.5, zorder=4,
            label=f"AVWAP from anchor")

    # Markers
    anchor_x = data["argmax_A"] - ws
    sig_x = data["sig_idx"] - ws
    entry_x = data["entry_idx"] - ws
    ax.axvline(anchor_x, color="blue", linestyle=":", linewidth=1.0, zorder=1, alpha=0.7)
    ax.axvline(sig_x, color="orange", linestyle=":", linewidth=1.0, zorder=1, alpha=0.7)
    if 0 <= entry_x <= len(idx_array) - 1:
        ax.axvline(entry_x, color="red", linestyle="--", linewidth=0.8, zorder=1, alpha=0.6)

    bars_back = data["sig_idx"] - data["argmax_A"]
    sig_close = data["c"][data["sig_idx"]]
    ax.set_title(
        f"{setup}  {data['ticker']}  entry={data['entry_date']}  "
        f"anchor={data['argmax_date']} ({bars_back}b back)  "
        f"AVWAP={data['argmax_val']:.2f}  close@sig={sig_close:.2f}",
        fontsize=8,
    )
    ax.tick_params(labelsize=6)
    ax.grid(True, alpha=0.2)
    # X tick labels as dates at a handful of positions
    n = len(idx_array)
    tick_positions = np.linspace(0, n - 1, min(5, n)).astype(int)
    tick_labels = [data["dates"][ws + p] for p in tick_positions]
    ax.set_xticks(tick_positions)
    ax.set_xticklabels(tick_labels, fontsize=6, rotation=0)


PANELS_PER_PAGE = 4
panels = []
for setup, ticker, entry_date in ex_rows:
    data = compute(ticker, entry_date)
    if data is None:
        continue
    panels.append((setup, data))

print(f"Rendering {len(panels)} panels to {OUT}")
os.makedirs(os.path.dirname(OUT), exist_ok=True)

with PdfPages(OUT) as pdf:
    for page_start in range(0, len(panels), PANELS_PER_PAGE):
        page = panels[page_start:page_start + PANELS_PER_PAGE]
        fig, axes = plt.subplots(2, 2, figsize=(14, 9))
        axes = axes.flatten()
        for i, (setup, data) in enumerate(page):
            render_panel(axes[i], setup, data)
        for j in range(len(page), PANELS_PER_PAGE):
            axes[j].axis("off")
        fig.tight_layout()
        pdf.savefig(fig)
        plt.close(fig)
        print(f"  page {page_start // PANELS_PER_PAGE + 1} done ({len(page)} panels)")

print(f"Done. PDF at: {OUT}")
