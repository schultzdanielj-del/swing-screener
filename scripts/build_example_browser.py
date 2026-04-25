"""Generate a single-file HTML browser for visual review of example entry dates.

For each example in the DB (filtered by setup_type + excluded list), renders
a candlestick chart showing 30 bars before and after the entry date, with
the entry bar highlighted. Output is a single HTML file with embedded PNGs.

Usage:
    python scripts/build_example_browser.py --setup brko --out brko_review.html
"""

import argparse
import base64
import io
import os
import sqlite3
import sys
from datetime import datetime

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import numpy as np
import pandas as pd
from matplotlib.patches import Rectangle

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.abspath(os.path.join(SCRIPT_DIR, ".."))
sys.path.insert(0, os.path.join(REPO_ROOT, "local_runner"))

from pyramid_grinder import load_daily_cache  # noqa: E402

BRKO_REVIEWED = {
    ("ANF", "2023-11-02"),
    ("BE", "2025-07-23"),
    ("EAT", "2024-09-16"),
    ("ERO", "2025-11-24"),
    ("IREN", "2025-06-24"),
    ("KTOS", "2025-05-16"),
    ("KTOS", "2025-09-11"),
    ("SNDK", "2025-08-22"),
    ("SNDK", "2026-01-02"),
    ("SOUN", "2024-11-21"),
    ("VITL", "2024-02-20"),
}


def render_candles(df, entry_offset, ticker, entry_date):
    fig, ax = plt.subplots(figsize=(14, 5))

    for i, row in df.iterrows():
        is_up = row["close"] >= row["open"]
        color = "#26a69a" if is_up else "#ef5350"
        edge = "#1a7d72" if is_up else "#c83c3b"
        ax.vlines(i, row["low"], row["high"], color=edge, linewidth=1)
        body_bottom = min(row["open"], row["close"])
        body_height = abs(row["close"] - row["open"])
        if body_height < (row["high"] - row["low"]) * 0.005:
            body_height = (row["high"] - row["low"]) * 0.005
        ax.add_patch(Rectangle((i - 0.4, body_bottom), 0.8, body_height,
                               facecolor=color, edgecolor=edge, linewidth=0.5))

    ax.axvspan(entry_offset - 0.5, entry_offset + 0.5, alpha=0.25,
               facecolor="#ffd54f", edgecolor="none", zorder=0)
    ax.axvline(entry_offset, color="#f57f17", linewidth=1.2, linestyle="--", alpha=0.8)

    dates = [d.strftime("%m-%d") for d in df["date"]]
    step = max(1, len(dates) // 12)
    tick_positions = list(range(0, len(dates), step))
    ax.set_xticks(tick_positions)
    ax.set_xticklabels([dates[i] for i in tick_positions], rotation=45, fontsize=8)

    low_min = df["low"].min()
    high_max = df["high"].max()
    pad = (high_max - low_min) * 0.05
    ax.set_ylim(low_min - pad, high_max + pad)
    ax.set_xlim(-1, len(df))

    entry_bar = df.iloc[entry_offset]
    ax.set_title(
        f"{ticker} | entry {entry_date}  |  "
        f"O {entry_bar['open']:.2f}  H {entry_bar['high']:.2f}  "
        f"L {entry_bar['low']:.2f}  C {entry_bar['close']:.2f}",
        fontsize=10
    )
    ax.grid(alpha=0.2)
    ax.set_axisbelow(True)

    buf = io.BytesIO()
    plt.savefig(buf, format="png", dpi=80, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    return base64.b64encode(buf.getvalue()).decode()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--setup", required=True, help="setup_type (brko, dtss, etc.)")
    parser.add_argument("--out", required=True, help="output HTML filename (goes to local_runner/cache/)")
    parser.add_argument("--exclude-reviewed", action="store_true",
                        help="skip BRKO examples already reviewed")
    parser.add_argument("--bars-before", type=int, default=30)
    parser.add_argument("--bars-after", type=int, default=30)
    args = parser.parse_args()

    db_path = os.path.join(REPO_ROOT, "data", "scanperfect.db")
    conn = sqlite3.connect(db_path)
    rows = conn.execute(
        "SELECT ticker, entry_date FROM examples WHERE setup_type=? ORDER BY ticker, entry_date",
        (args.setup,)
    ).fetchall()
    conn.close()

    if args.exclude_reviewed and args.setup == "brko":
        rows = [r for r in rows if (r[0], r[1]) not in BRKO_REVIEWED]

    print(f"Rendering {len(rows)} {args.setup.upper()} examples...")
    universe = load_daily_cache()

    html_parts = [
        "<!DOCTYPE html>",
        "<html><head><meta charset='utf-8'>",
        f"<title>{args.setup.upper()} example browser</title>",
        "<style>",
        "body { font-family: -apple-system, system-ui, sans-serif; max-width: 1400px; margin: 20px auto; padding: 0 20px; background: #fafafa; }",
        "h1 { color: #222; }",
        ".chart { background: white; border: 1px solid #ddd; border-radius: 6px; padding: 15px; margin-bottom: 20px; box-shadow: 0 1px 3px rgba(0,0,0,0.05); }",
        ".chart-header { display: flex; justify-content: space-between; align-items: baseline; margin-bottom: 8px; }",
        ".chart-title { font-weight: 600; font-size: 16px; }",
        ".chart-idx { color: #999; font-size: 13px; }",
        "img { max-width: 100%; display: block; }",
        ".error { color: #c62828; padding: 20px; background: #ffebee; border-radius: 4px; }",
        "</style></head><body>",
        f"<h1>{args.setup.upper()} example browser ({len(rows)} entries)</h1>",
        "<p>Each chart shows 30 bars before and after the recorded entry date. "
        "Entry bar is highlighted in yellow. Flag any where the entry bar doesn't "
        "match what you'd have actually traded.</p>",
    ]

    n_rendered = 0
    for idx, (ticker, entry_date) in enumerate(rows, 1):
        df = universe.get(ticker)
        if df is None:
            html_parts.append(
                f"<div class='chart'><div class='chart-header'>"
                f"<span class='chart-title'>{ticker} | {entry_date}</span>"
                f"<span class='chart-idx'>{idx} / {len(rows)}</span></div>"
                f"<div class='error'>NO OHLCV</div></div>"
            )
            continue

        dates = [str(d)[:10] for d in df["date"].values]
        if entry_date not in dates:
            html_parts.append(
                f"<div class='chart'><div class='chart-header'>"
                f"<span class='chart-title'>{ticker} | {entry_date}</span>"
                f"<span class='chart-idx'>{idx} / {len(rows)}</span></div>"
                f"<div class='error'>ENTRY DATE NOT IN OHLCV (not a trading day?)</div></div>"
            )
            continue

        entry_idx = dates.index(entry_date)
        start = max(0, entry_idx - args.bars_before)
        end = min(len(df), entry_idx + args.bars_after + 1)
        sub = df.iloc[start:end].reset_index(drop=True)
        entry_offset = entry_idx - start

        try:
            img_b64 = render_candles(sub, entry_offset, ticker, entry_date)
            html_parts.append(
                f"<div class='chart'><div class='chart-header'>"
                f"<span class='chart-title'>{ticker} | entry {entry_date}</span>"
                f"<span class='chart-idx'>{idx} / {len(rows)}</span></div>"
                f"<img src='data:image/png;base64,{img_b64}' alt='{ticker} chart'/>"
                f"</div>"
            )
            n_rendered += 1
        except Exception as e:
            html_parts.append(
                f"<div class='chart'><div class='chart-header'>"
                f"<span class='chart-title'>{ticker} | {entry_date}</span>"
                f"<span class='chart-idx'>{idx} / {len(rows)}</span></div>"
                f"<div class='error'>RENDER ERROR: {e}</div></div>"
            )

        if idx % 10 == 0:
            print(f"  {idx}/{len(rows)}...")

    html_parts.append("</body></html>")

    out_dir = os.path.join(REPO_ROOT, "local_runner", "cache")
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, args.out)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write("\n".join(html_parts))

    size_mb = os.path.getsize(out_path) / 1024 / 1024
    print(f"\nRendered {n_rendered}/{len(rows)} charts")
    print(f"Saved: {out_path} ({size_mb:.1f} MB)")


if __name__ == "__main__":
    main()
