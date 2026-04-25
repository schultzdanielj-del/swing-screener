"""Compare argmax-AVWAP vs Dan's hand-picked anchors on the 8 AND-gate failures.

Dan's picks (provided 2026-04-22):
  AR 2020-12-17 -> anchor 2020-12-11
  BB 2024-12-20 -> anchor 2024-12-17
  DRN 2024-07-11 -> anchor in [2023-12-04, 2023-12-13] (super close, can't disambiguate on TC2000)
  HTT 2021-01-12 -> anchor 2020-06-19 (+/- 1)
  LMND 2024-11-05 -> anchor 2024-11-01
  LMND 2024-11-06 -> anchor 2024-11-01
  PTON entry-date dispute: DB says 2024-10-14, Dan says 2024-10-11. Anchor 2024-09-20.
  REAL 2024-11-13 -> anchor 2024-11-06

For each:
  - Verify entry_date in OHLCV (flag PTON discrepancy)
  - Compute AVWAP(dan_anchor..sig) and compare to argmax AVWAP
  - Show where Dan's pick falls on the AVWAP-over-anchor curve (rank, percentile)
"""
import pickle
import sqlite3
import numpy as np
import pandas as pd

CACHE = r"C:/Users/Dan/Documents/ScanPerfect/swing-screener/local_runner/cache/universe_ohlcv_daily.pkl"
DB = r"C:/Users/Dan/Documents/ScanPerfect/swing-screener/data/scanperfect.db"

PICKS = [
    # (ticker, db_entry_date, dan_entry_date_override, dan_anchor_date_or_range)
    ("AR",   "2020-12-17", None,         ["2020-12-11"]),
    ("BB",   "2024-12-20", None,         ["2024-12-17"]),
    ("DRN",  "2024-07-11", None,         ["2023-12-04","2023-12-07","2023-12-08","2023-12-11","2023-12-12","2023-12-13"]),
    ("HTT",  "2021-01-12", None,         ["2020-06-18","2020-06-19","2020-06-22"]),
    ("LMND", "2024-11-05", None,         ["2024-11-01"]),
    ("LMND", "2024-11-06", None,         ["2024-11-01"]),
    ("PTON", "2024-10-14", "2024-10-11", ["2024-09-20"]),
    ("REAL", "2024-11-13", None,         ["2024-11-06"]),
]

with open(CACHE, "rb") as f:
    universe = pickle.load(f)


def load_arrays(ticker):
    df = universe[ticker]
    if not pd.api.types.is_datetime64_any_dtype(df["date"]):
        df = df.copy()
        df["date"] = pd.to_datetime(df["date"])
    dates = df["date"].dt.strftime("%Y-%m-%d").values
    h = df["high"].values.astype(float)
    l = df["low"].values.astype(float)
    c = df["close"].values.astype(float)
    v = df["volume"].values.astype(float)
    tp = (h + l + c) / 3.0
    return dates, h, l, c, v, tp


def avwap_a_to_sig(tp, v, A, sig):
    seg_tp = tp[A:sig+1]
    seg_v = v[A:sig+1]
    sv = seg_v.sum()
    if sv <= 0:
        return float("nan")
    return float((seg_tp * seg_v).sum() / sv)


def avwap_curve(tp, v, sig):
    cum_tpv = np.concatenate([[0.0], np.cumsum(tp * v)])
    cum_v = np.concatenate([[0.0], np.cumsum(v)])
    A_range = np.arange(0, sig)
    total_tpv = cum_tpv[sig + 1] - cum_tpv[A_range]
    total_v = cum_v[sig + 1] - cum_v[A_range]
    with np.errstate(invalid="ignore", divide="ignore"):
        avwaps = np.where(total_v > 0, total_tpv / total_v, -np.inf)
    return A_range, avwaps


def index_of(dates, target):
    hits = np.where(dates == target)[0]
    if len(hits) == 0:
        return None
    return int(hits[0])


for ticker, db_entry, dan_entry_override, anchor_dates in PICKS:
    print("=" * 80)
    print(f"{ticker}  db_entry={db_entry}  dan_entry={dan_entry_override or db_entry}")
    dates, h, l, c, v, tp = load_arrays(ticker)

    # Resolve entry index
    eff_entry = dan_entry_override or db_entry
    entry_idx = index_of(dates, eff_entry)
    db_entry_idx = index_of(dates, db_entry)
    if entry_idx is None:
        print(f"  ! Entry date {eff_entry} NOT in OHLCV")
        if db_entry_idx is None:
            print(f"  ! DB entry date {db_entry} ALSO not in OHLCV")
            continue
        entry_idx = db_entry_idx

    if dan_entry_override and db_entry_idx is not None and dan_entry_override != db_entry:
        print(f"  ENTRY-DATE NOTE: DB says {db_entry} (idx {db_entry_idx} = {dates[db_entry_idx]}), "
              f"Dan says {dan_entry_override} (idx {entry_idx} = {dates[entry_idx]}). "
              f"diff = {db_entry_idx - entry_idx} bars.")

    sig_idx = entry_idx - 1
    sig_close = c[sig_idx]
    sig_date = dates[sig_idx]
    print(f"  sig_idx={sig_idx} ({sig_date}), sig_close={sig_close:.2f}")

    # AVWAP curve
    A_range, avwaps = avwap_curve(tp, v, sig_idx)
    argmax_local = int(np.argmax(avwaps))
    argmax_A = int(A_range[argmax_local])
    argmax_val = float(avwaps[argmax_local])
    print(f"  ARGMAX:    A={argmax_A} ({dates[argmax_A]}), {sig_idx-argmax_A} bars back, AVWAP={argmax_val:.2f}")

    # Dan's picks
    for ad in anchor_dates:
        a_idx = index_of(dates, ad)
        if a_idx is None:
            print(f"  Dan pick {ad}: NOT in OHLCV")
            continue
        if a_idx >= sig_idx:
            print(f"  Dan pick {ad}: A={a_idx} >= sig_idx={sig_idx}, invalid")
            continue
        val = avwap_a_to_sig(tp, v, a_idx, sig_idx)
        # rank of Dan's AVWAP in the full curve (1 = highest)
        rank = int((avwaps > val).sum()) + 1
        pct_above = float((avwaps > val).sum()) / len(avwaps) * 100.0
        bars_back = sig_idx - a_idx
        print(f"  DAN PICK:  A={a_idx} ({ad}), {bars_back} bars back, AVWAP={val:.2f}  "
              f"(rank #{rank} of {len(avwaps)} candidate anchors; {pct_above:.1f}% of candidates yield higher AVWAP)")
