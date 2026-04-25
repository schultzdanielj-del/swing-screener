"""Compute real-world stop-distance and FW stats for every example across all setups.

Pure read-only analysis. Pulls:
  - examples table (ticker + entry_date) from main repo SQLite
  - OHLCV from main repo daily pickle
  - setups table (direction) from SQLite

For each example computes:
  - signal_bar_idx (= entry_bar - 1 by spec, but we also scan backwards up to 3 bars)
  - entry_bar_idx, entry_bar_low, entry_bar_high
  - signal_close, sig_ADR (14-period H-L mean at signal bar)
  - Real stop distance:
      long:  (signal_close - entry_bar_low)   / sig_ADR
      short: (entry_bar_high - signal_close)  / sig_ADR
  - FW (forward window) = bars between signal and entry (per-example, max across setup)
  - FW extremes:
      long:  min FW low, max FW low, last FW low
      short: max FW high, min FW high, last FW high

Outputs a CSV + prints distribution summary per setup.
"""
from __future__ import annotations

import os
import sys
import sqlite3
import pickle
from collections import defaultdict
from statistics import median

import numpy as np
import pandas as pd

MAIN_ROOT = r"C:/Users/Dan/Documents/ScanPerfect/swing-screener"
DB = os.path.join(MAIN_ROOT, "data", "scanperfect.db")
PKL = os.path.join(MAIN_ROOT, "local_runner", "cache", "universe_ohlcv_daily.pkl")

OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "out")
os.makedirs(OUT_DIR, exist_ok=True)


def load_setups():
    with sqlite3.connect(DB) as c:
        rows = c.execute("SELECT setup_type, direction FROM setups").fetchall()
    return {s: d for s, d in rows}


def load_examples():
    with sqlite3.connect(DB) as c:
        rows = c.execute("SELECT setup_type, ticker, entry_date FROM examples").fetchall()
    by_setup = defaultdict(list)
    for st, tk, ed in rows:
        by_setup[st].append((tk, ed))
    return by_setup


def adr14(df, i):
    start = max(0, i - 13)
    h = df["high"].values[start : i + 1]
    l = df["low"].values[start : i + 1]
    if len(h) == 0:
        return float("nan")
    return float(np.mean(h - l))


def main():
    print("Loading setups/examples...")
    setups = load_setups()
    examples = load_examples()
    total = sum(len(v) for v in examples.values())
    print(f"  {len(setups)} setups, {total} examples total")

    print("Loading daily OHLCV pickle (~1-2 GB)...")
    with open(PKL, "rb") as f:
        universe = pickle.load(f)
    print(f"  {len(universe):,} tickers loaded")

    # For each setup, compute stats
    rows = []
    per_setup_summary = {}

    MAX_FW_SCAN = 10  # cap when computing FW backward-scan from entry

    for setup, exs in examples.items():
        direction = setups.get(setup, "long")
        stats = {
            "setup": setup,
            "direction": direction,
            "n_examples": len(exs),
            "real_stop_adr": [],   # (sig_close - entry_low) / ADR  (long) or mirror
            "fw_offset": [],       # entry_bar_idx - signal_bar_idx
            "fw_min_adr": [],      # signal_close - min(FW_lows) / ADR  (long), mirror short
            "fw_max_adr": [],      # signal_close - max(FW_lows) / ADR  (long)  (shallowest entry)
            "fw_last_adr": [],     # signal_close - low[signal+FW]  / ADR
            "fw_first_adr": [],    # signal_close - low[signal+1]   / ADR
            "post_entry_max_mae_adr": [],  # deepest adverse move after entry, in ADR
        }

        fw_offsets = []  # collected separately first to discover per-setup FW
        # Pre-pass 1 — collect entry indices and fw offsets (signal = entry - 1 per spec)
        resolved = []
        for tk, entry_date in exs:
            df = universe.get(tk)
            if df is None or len(df) < 20:
                continue
            dates = pd.to_datetime(df["date"]).dt.strftime("%Y-%m-%d").values
            m = np.where(dates == entry_date)[0]
            if len(m) == 0:
                continue
            entry_idx = int(m[0])
            if entry_idx < 14:
                continue
            resolved.append((tk, entry_date, entry_idx, df))

        # For offset stats we use signal = entry-1 (spec), FW-offset always = 1 by that def.
        # To discover the real-world FW (signal-to-entry max), we'd need the raw cluster data,
        # not available here. Use FW=3 as the known value (DTSS=3, BRKO=3) — verified later.
        DEFAULT_FW = 3

        for tk, ed, ei, df in resolved:
            signal_idx = ei - 1
            if signal_idx < 14:
                continue
            adr = adr14(df, signal_idx)
            if not np.isfinite(adr) or adr <= 0:
                continue
            sc = float(df["close"].values[signal_idx])

            # Real stop distance (known because we know the entry bar)
            if direction == "long":
                el = float(df["low"].values[ei])
                real_stop_dist_adr = (sc - el) / adr
            else:
                eh = float(df["high"].values[ei])
                real_stop_dist_adr = (eh - sc) / adr
            stats["real_stop_adr"].append(real_stop_dist_adr)

            # FW offset is always 1 per the strict-entry-1 spec
            stats["fw_offset"].append(ei - signal_idx)

            # FW stats over signal+1 .. signal+DEFAULT_FW
            fw_start = signal_idx + 1
            fw_end = min(signal_idx + DEFAULT_FW, len(df) - 1)
            if fw_end < fw_start:
                continue
            lows = df["low"].values[fw_start : fw_end + 1]
            highs = df["high"].values[fw_start : fw_end + 1]

            if direction == "long":
                fw_min_adr = (sc - float(np.min(lows))) / adr
                fw_max_adr = (sc - float(np.max(lows))) / adr  # shallowest
                fw_first_adr = (sc - float(lows[0])) / adr
                fw_last_adr = (sc - float(lows[-1])) / adr
            else:
                fw_min_adr = (float(np.max(highs)) - sc) / adr
                fw_max_adr = (float(np.min(highs)) - sc) / adr  # shallowest
                fw_first_adr = (float(highs[0]) - sc) / adr
                fw_last_adr = (float(highs[-1]) - sc) / adr

            stats["fw_min_adr"].append(fw_min_adr)
            stats["fw_max_adr"].append(fw_max_adr)
            stats["fw_first_adr"].append(fw_first_adr)
            stats["fw_last_adr"].append(fw_last_adr)

            # Post-entry deepest adverse move (up to 120 bars)
            post_start = ei + 1
            post_end = min(ei + 120, len(df) - 1)
            if post_end > post_start:
                post_lows = df["low"].values[post_start : post_end + 1]
                post_highs = df["high"].values[post_start : post_end + 1]
                if direction == "long":
                    worst = float(np.min(post_lows))
                    mae = (sc - worst) / adr
                else:
                    worst = float(np.max(post_highs))
                    mae = (worst - sc) / adr
                stats["post_entry_max_mae_adr"].append(mae)

            rows.append({
                "setup": setup,
                "direction": direction,
                "ticker": tk,
                "entry_date": ed,
                "signal_idx": signal_idx,
                "entry_idx": ei,
                "signal_close": round(sc, 4),
                "adr14": round(adr, 4),
                "real_stop_dist_adr": round(real_stop_dist_adr, 4),
                "fw_min_adr": round(fw_min_adr, 4),
                "fw_max_adr": round(fw_max_adr, 4),
                "fw_first_adr": round(fw_first_adr, 4),
                "fw_last_adr": round(fw_last_adr, 4),
            })

        # Summary
        def pct(xs, p):
            if not xs:
                return None
            s = sorted(xs)
            k = max(0, min(len(s) - 1, int(round(p / 100 * (len(s) - 1)))))
            return round(s[k], 3)

        per_setup_summary[setup] = {
            "n": len(stats["real_stop_adr"]),
            "direction": direction,
            "real_stop_adr": {
                "min": round(min(stats["real_stop_adr"]), 3) if stats["real_stop_adr"] else None,
                "p10": pct(stats["real_stop_adr"], 10),
                "p50": pct(stats["real_stop_adr"], 50),
                "p90": pct(stats["real_stop_adr"], 90),
                "max": round(max(stats["real_stop_adr"]), 3) if stats["real_stop_adr"] else None,
            },
            "fw_min_adr (current loser_thr raw)": {
                "p50": pct(stats["fw_min_adr"], 50),
                "p90": pct(stats["fw_min_adr"], 90),
                "max": round(max(stats["fw_min_adr"]), 3) if stats["fw_min_adr"] else None,
            },
            "fw_max_adr (shallowest)": {
                "p50": pct(stats["fw_max_adr"], 50),
                "p90": pct(stats["fw_max_adr"], 90),
                "max": round(max(stats["fw_max_adr"]), 3) if stats["fw_max_adr"] else None,
            },
            "fw_first_adr (signal+1 bar)": {
                "p50": pct(stats["fw_first_adr"], 50),
                "p90": pct(stats["fw_first_adr"], 90),
                "max": round(max(stats["fw_first_adr"]), 3) if stats["fw_first_adr"] else None,
            },
            "fw_last_adr (signal+FW bar)": {
                "p50": pct(stats["fw_last_adr"], 50),
                "p90": pct(stats["fw_last_adr"], 90),
                "max": round(max(stats["fw_last_adr"]), 3) if stats["fw_last_adr"] else None,
            },
            "post_entry_max_mae_adr": {
                "p50": pct(stats["post_entry_max_mae_adr"], 50),
                "p90": pct(stats["post_entry_max_mae_adr"], 90),
                "max": round(max(stats["post_entry_max_mae_adr"]), 3) if stats["post_entry_max_mae_adr"] else None,
            },
        }

    print("\n===== PER-SETUP SUMMARY =====")
    import json
    print(json.dumps(per_setup_summary, indent=2))

    out_csv = os.path.join(OUT_DIR, "example_stats.csv")
    pd.DataFrame(rows).to_csv(out_csv, index=False)
    print(f"\nWrote {out_csv} ({len(rows)} rows)")


if __name__ == "__main__":
    main()
