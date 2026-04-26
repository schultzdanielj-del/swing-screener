"""Quick L14 generalization check on BF and BASE — without running the full pool grinder.

Reads classifier_tags/{setup}_tags.json directly, builds minimum cluster meta from OHLCV+earnings,
applies §15.4 stop/effective_entry/cap rules, computes mfe_during_life per cluster,
applies L14: WIN iff mfe_during_life >= min(example mfe_during_life) per setup.
"""
from __future__ import annotations
import json
import os
import pickle
import sqlite3
import sys
import time
from typing import List, Tuple

import numpy as np

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

MAIN_REPO = r"C:/Users/Dan/Documents/ScanPerfect/swing-screener"
TAGS_DIR = os.path.join(MAIN_REPO, "research", "classifier_tags")
OHLCV = os.path.join(MAIN_REPO, "local_runner", "cache", "universe_ohlcv_daily.pkl")
DB = os.path.join(MAIN_REPO, "data", "scanperfect.db")
OUT_DIR = os.path.join(MAIN_REPO, "research", "labeler_bakeoff")
os.makedirs(OUT_DIR, exist_ok=True)

SETUPS = ("htf", "bf", "base")
ADR_LOOKBACK = 14
TRADE_LIFETIME_CAP_BARS = 120


def load_earnings_map():
    conn = sqlite3.connect(DB)
    rows = conn.execute("SELECT ticker, earnings_date FROM earnings_dates").fetchall()
    conn.close()
    by_ticker = {}
    for t, d in rows:
        by_ticker.setdefault(t, []).append(str(d)[:10])
    for t in by_ticker:
        by_ticker[t] = np.array(sorted(set(by_ticker[t])), dtype="<U10")
    return by_ticker


def next_earnings_bar(earnings_arr, ohlcv_dates_str, entry_bar_idx):
    if earnings_arr is None or len(earnings_arr) == 0:
        return None
    entry_date_str = ohlcv_dates_str[entry_bar_idx]
    pos = np.searchsorted(earnings_arr, entry_date_str, side="right")
    if pos >= len(earnings_arr):
        return None
    next_ern = earnings_arr[pos]
    bp = int(np.searchsorted(ohlcv_dates_str, next_ern, side="left"))
    if bp <= entry_bar_idx or bp >= len(ohlcv_dates_str):
        return None
    return bp


def process_setup(setup, ohlcv, earnings_map):
    print(f"\n{'='*70}\n  SETUP: {setup.upper()}\n{'='*70}")
    tags_json = json.load(open(os.path.join(TAGS_DIR, f"{setup}_tags.json")))
    clusters = tags_json["clusters"]
    n_total = len(clusters)
    by_tag = tags_json.get("counts", {}).get("by_tag", {})
    print(f"  pool clusters: {n_total}  ENTRY={by_tag.get('ENTRY')}  REDUNDANT={by_tag.get('REDUNDANT')}  "
          f"NOENTRY={by_tag.get('NOENTRY')}  MISSING={by_tag.get('MISSING')}")

    entered_results = []
    n_entry = 0
    n_skipped = 0

    for c in clusters:
        if c.get("tag") != "ENTRY":
            continue
        n_entry += 1
        ticker = c["ticker"]
        df = ohlcv.get(ticker)
        if df is None:
            n_skipped += 1
            continue
        high = df["high"].values.astype(np.float64)
        low = df["low"].values.astype(np.float64)
        close = df["close"].values.astype(np.float64)
        dates_str = np.array([str(d)[:10] for d in df["date"].values], dtype="<U10")
        n_bars = len(df)

        entry_bar = c.get("entry_idx")
        entry_date = c.get("entry_date")
        if entry_bar is None or entry_bar < 0 or entry_bar >= n_bars:
            n_skipped += 1; continue
        if entry_date and dates_str[entry_bar] != entry_date:
            n_skipped += 1; continue

        e_high = high[entry_bar]; e_low = low[entry_bar]
        start = max(0, entry_bar - (ADR_LOOKBACK - 1))
        adr14 = float(np.mean(high[start:entry_bar + 1] - low[start:entry_bar + 1]))
        if adr14 <= 0 or np.isnan(adr14):
            n_skipped += 1; continue
        eff = min(e_high, e_low + adr14)
        stop = e_low

        ern = earnings_map.get(ticker)
        ern_bar = next_earnings_bar(ern, dates_str, entry_bar)
        time_cap = entry_bar + TRADE_LIFETIME_CAP_BARS
        end_of_tape = n_bars - 1
        cands = [time_cap, end_of_tape]
        if ern_bar is not None:
            cands.append(ern_bar - 1)
        cap_bar = min(cands)
        if cap_bar <= entry_bar:
            n_skipped += 1; continue

        h = cap_bar - entry_bar
        fwd_low = low[entry_bar + 1: cap_bar + 1]
        fwd_high = high[entry_bar + 1: cap_bar + 1]
        stop_mask = fwd_low <= stop
        stop_hit_bar = int(np.argmax(stop_mask)) if stop_mask.any() else h
        eff_h = min(int(stop_hit_bar) + 1, h)
        if eff_h == 0:
            n_skipped += 1; continue
        mfe_during_life = float((fwd_high[: eff_h].max() - eff) / adr14)

        entered_results.append({
            "cluster_id": c["cluster_id"],
            "ticker": ticker,
            "is_example": c.get("is_example", 0),
            "horizon": h,
            "stop_hit_bar": stop_hit_bar,
            "eff_horizon": eff_h,
            "mfe_during_life": mfe_during_life,
            "entry_date": entry_date,
        })

    print(f"  ENTRY tagged: {n_entry}  processed: {len(entered_results)}  skipped: {n_skipped}")

    # Apply L14
    examples = [r for r in entered_results if r["is_example"] == 1]
    wild = [r for r in entered_results if r["is_example"] == 0]
    print(f"  examples: {len(examples)}  wild: {len(wild)}")

    if not examples:
        print("  no examples — cannot compute L14 threshold")
        return None

    ex_mfes = sorted([r["mfe_during_life"] for r in examples])
    T = ex_mfes[0]
    setting_example = next(r for r in examples if r["mfe_during_life"] == T)
    print(f"  L14 threshold T = min(example mfe_during_life) = {T:.3f} ADR  "
          f"(set by {setting_example['ticker']} cid={setting_example['cluster_id']}, "
          f"horizon={setting_example['horizon']}, stop={setting_example['stop_hit_bar']}, "
          f"eff_h={setting_example['eff_horizon']})")
    print(f"  Example mfe_during_life: min={ex_mfes[0]:.2f}  "
          f"p10={ex_mfes[len(ex_mfes)//10] if len(ex_mfes)>=10 else ex_mfes[0]:.2f}  "
          f"p25={ex_mfes[len(ex_mfes)//4]:.2f}  median={ex_mfes[len(ex_mfes)//2]:.2f}  "
          f"p75={ex_mfes[3*len(ex_mfes)//4]:.2f}  max={ex_mfes[-1]:.2f}")

    # Lock check (must be N/N)
    n_ex_pass = sum(1 for r in examples if r["mfe_during_life"] >= T)
    print(f"  Examples lock: {n_ex_pass} / {len(examples)} (must equal {len(examples)})")

    # Wild admission
    n_wild_win = sum(1 for r in wild if r["mfe_during_life"] >= T)
    print(f"  Wild WIN under L14: {n_wild_win} / {len(wild)} ({100*n_wild_win/len(wild):.1f}%)")

    # Distribution of wild mfe_during_life
    wild_mfes = sorted([r["mfe_during_life"] for r in wild])
    if wild_mfes:
        print(f"  Wild mfe_during_life: min={wild_mfes[0]:.2f}  p25={wild_mfes[len(wild_mfes)//4]:.2f}  "
              f"median={wild_mfes[len(wild_mfes)//2]:.2f}  p75={wild_mfes[3*len(wild_mfes)//4]:.2f}  "
              f"max={wild_mfes[-1]:.2f}")

    # Sample WIN/LOSS for sanity
    wild_win = sorted([r for r in wild if r["mfe_during_life"] >= T],
                       key=lambda x: -x["mfe_during_life"])
    wild_loss = sorted([r for r in wild if r["mfe_during_life"] < T],
                        key=lambda x: x["mfe_during_life"])
    print(f"  Top 5 wild WINs (by mfe_during_life):")
    for r in wild_win[:5]:
        print(f"    {r['ticker']:>6s} cid={r['cluster_id']:>4d}  mfe_life={r['mfe_during_life']:>6.2f}  "
              f"horizon={r['horizon']:>3d}  stop={r['stop_hit_bar']:>3}")
    print(f"  Bottom 5 wild LOSSes (by mfe_during_life):")
    for r in wild_loss[:5]:
        print(f"    {r['ticker']:>6s} cid={r['cluster_id']:>4d}  mfe_life={r['mfe_during_life']:>6.2f}  "
              f"horizon={r['horizon']:>3d}  stop={r['stop_hit_bar']:>3}")

    return {
        "setup": setup,
        "n_entry": n_entry,
        "n_processed": len(entered_results),
        "n_examples": len(examples),
        "n_wild": len(wild),
        "T": T,
        "setting_example": setting_example,
        "examples_lock_pass": n_ex_pass,
        "n_wild_win": n_wild_win,
        "wild_win_rate": n_wild_win / len(wild) if wild else 0,
        "example_mfe_distribution": {
            "min": ex_mfes[0], "max": ex_mfes[-1],
            "median": ex_mfes[len(ex_mfes)//2],
            "p25": ex_mfes[len(ex_mfes)//4],
            "p75": ex_mfes[3*len(ex_mfes)//4],
        },
        "wild_mfe_distribution": {
            "min": wild_mfes[0], "max": wild_mfes[-1],
            "median": wild_mfes[len(wild_mfes)//2],
            "p25": wild_mfes[len(wild_mfes)//4],
            "p75": wild_mfes[3*len(wild_mfes)//4],
        } if wild_mfes else None,
    }


def main():
    print("Loading OHLCV...")
    with open(OHLCV, "rb") as f:
        ohlcv = pickle.load(f)
    print(f"  OHLCV tickers: {len(ohlcv):,}")
    print("Loading earnings map...")
    earnings_map = load_earnings_map()
    print(f"  earnings tickers: {len(earnings_map):,}")

    all_results = {}
    for setup in SETUPS:
        r = process_setup(setup, ohlcv, earnings_map)
        if r:
            all_results[setup] = r

    # Cross-setup summary
    print("\n\n══════════ CROSS-SETUP SUMMARY ══════════")
    print(f"{'setup':>6s} {'n_ex':>5s} {'n_wild':>7s} {'T_ADR':>7s} {'lock':>5s} {'wild_WIN':>9s} {'rate':>7s}  {'set_by':>20s}")
    for setup, r in all_results.items():
        sb = r["setting_example"]
        s = f"{sb['ticker']}/h{sb['horizon']}/s{sb['stop_hit_bar']}/e{sb['eff_horizon']}"
        print(f"{setup:>6s} {r['n_examples']:>5d} {r['n_wild']:>7d} {r['T']:>7.3f} "
              f"{r['examples_lock_pass']}/{r['n_examples']:<3d} {r['n_wild_win']:>9d} "
              f"{100*r['wild_win_rate']:>6.1f}%  {s:>20s}")

    out_path = os.path.join(OUT_DIR, "bf_base_l14_check.json")
    with open(out_path, "w") as f:
        json.dump(all_results, f, indent=2, default=str)
    print(f"\n  Saved: {out_path}")


if __name__ == "__main__":
    main()
