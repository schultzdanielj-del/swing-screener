"""H2 Part 1: Auto-detect the relevant MA per chart by ADR distance from MA(sig) to close[sig].

For each example: compute SMA10/21/50/200, EMA10/21/50/200 at sig.
Distance metric: |close[sig] - MA(sig)| / ADR(sig), and signed variant.
The MA with smallest |distance| is the auto-detected relevant support.

Sanity checks:
  - Per-setup distribution of winning MA: does HTF tend to no-MA / fast, BF to EMA21, BASE to SMA50?
  - For the 8 failures: which MA wins per chart, and what's the ADR distance?
"""
import pickle
import sqlite3
import numpy as np
import pandas as pd
from collections import Counter

CACHE = r"C:/Users/Dan/Documents/ScanPerfect/swing-screener/local_runner/cache/universe_ohlcv_daily.pkl"
DB = r"C:/Users/Dan/Documents/ScanPerfect/swing-screener/data/scanperfect.db"

DAN_PICKS = {
    ("AR",   "2020-12-17"): "2020-12-11",
    ("BB",   "2024-12-20"): "2024-12-17",
    ("DRN",  "2024-07-11"): "2023-12-08",
    ("HTT",  "2021-01-12"): "2020-06-19",
    ("LMND", "2024-11-05"): "2024-11-01",
    ("LMND", "2024-11-06"): "2024-11-01",
    ("PTON", "2024-10-14"): "2024-09-20",
    ("REAL", "2024-11-13"): "2024-11-06",
}

with sqlite3.connect(DB) as conn:
    rows = conn.execute(
        "SELECT setup_type, ticker, entry_date FROM examples "
        "WHERE setup_type IN ('htf','bf','base') ORDER BY setup_type, ticker, entry_date"
    ).fetchall()

with open(CACHE, "rb") as f:
    universe = pickle.load(f)


def load(ticker):
    df = universe[ticker]
    if not pd.api.types.is_datetime64_any_dtype(df["date"]):
        df = df.copy()
        df["date"] = pd.to_datetime(df["date"])
    dates = df["date"].dt.strftime("%Y-%m-%d").values
    h = df["high"].values.astype(float)
    l = df["low"].values.astype(float)
    c = df["close"].values.astype(float)
    v = df["volume"].values.astype(float)
    return dates, h, l, c, v


def sma_at(arr, n, idx):
    if idx < n - 1:
        return float("nan")
    return float(np.mean(arr[idx-n+1:idx+1]))


def ema_at(arr, n, idx):
    if idx < n - 1:
        return float("nan")
    alpha = 2.0 / (n + 1)
    e = arr[0]
    for i in range(1, idx+1):
        e = alpha * arr[i] + (1 - alpha) * e
    return float(e)


def adr14(h, l, idx):
    if idx < 14:
        return float("nan")
    return float(np.mean(h[idx-13:idx+1] - l[idx-13:idx+1]))


MA_DEFS = [
    ("SMA10",  lambda c, idx: sma_at(c, 10, idx)),
    ("SMA21",  lambda c, idx: sma_at(c, 21, idx)),
    ("SMA50",  lambda c, idx: sma_at(c, 50, idx)),
    ("SMA200", lambda c, idx: sma_at(c, 200, idx)),
    ("EMA10",  lambda c, idx: ema_at(c, 10, idx)),
    ("EMA21",  lambda c, idx: ema_at(c, 21, idx)),
    ("EMA50",  lambda c, idx: ema_at(c, 50, idx)),
    ("EMA200", lambda c, idx: ema_at(c, 200, idx)),
]


records = []
for setup, ticker, entry_date in rows:
    if ticker not in universe:
        continue
    dates, h, l, c, v = load(ticker)
    hits = np.where(dates == entry_date)[0]
    if len(hits) == 0:
        continue
    entry_idx = int(hits[0])
    sig_idx = entry_idx - 1
    if sig_idx < 200:
        continue
    sig_close = float(c[sig_idx])
    adr = adr14(h, l, sig_idx)
    if not np.isfinite(adr) or adr <= 0:
        continue

    ma_dists = {}
    for ma_name, ma_fn in MA_DEFS:
        ma_val = ma_fn(c, sig_idx)
        if not np.isfinite(ma_val):
            ma_dists[ma_name] = (None, None, None)
            continue
        signed_adr = (sig_close - ma_val) / adr  # positive => MA below sig (foothold)
        abs_adr = abs(signed_adr)
        ma_dists[ma_name] = (ma_val, signed_adr, abs_adr)

    # Winner: smallest |distance| AMONG MAs that are below or equal to sig (positive signed)
    # If no MA is below sig, no foothold present; pick the closest abs-distance one anyway and flag.
    valid = [(name, d) for name, d in ma_dists.items() if d[1] is not None and d[1] >= 0]
    if valid:
        winner_below = min(valid, key=lambda x: x[1][2])
    else:
        winner_below = None
    all_valid = [(name, d) for name, d in ma_dists.items() if d[0] is not None]
    winner_closest = min(all_valid, key=lambda x: x[1][2]) if all_valid else None

    records.append({
        "setup": setup, "ticker": ticker, "entry_date": entry_date,
        "sig_idx": sig_idx, "sig_close": sig_close, "adr": adr,
        "ma_dists": ma_dists,
        "winner_below_name": winner_below[0] if winner_below else None,
        "winner_below_adr": winner_below[1][2] if winner_below else None,
        "winner_closest_name": winner_closest[0] if winner_closest else None,
        "winner_closest_adr": winner_closest[1][2] if winner_closest else None,
        "winner_closest_signed": winner_closest[1][1] if winner_closest else None,
    })

print(f"Evaluated: {len(records)} examples")
print()

# Per-setup distribution of winners (foothold = MA below sig)
print("=== PER-SETUP WINNING MA (foothold MA — closest below sig in ADR) ===")
by_setup = {"htf": [], "bf": [], "base": []}
for r in records:
    by_setup.setdefault(r["setup"], []).append(r["winner_below_name"])

for setup in ["htf", "bf", "base"]:
    items = by_setup.get(setup, [])
    n = len(items)
    counter = Counter(items)
    print(f"\n  {setup} (n={n}):")
    for ma, cnt in counter.most_common():
        ma_label = ma if ma is not None else "(no MA below sig)"
        print(f"    {ma_label:<30} {cnt:>3} ({cnt/n*100:.1f}%)")
print()

# Per-setup distribution of CLOSEST MA (regardless of side)
print("=== PER-SETUP WINNING MA (closest by absolute ADR — either side) ===")
by_setup_closest = {"htf": [], "bf": [], "base": []}
for r in records:
    by_setup_closest.setdefault(r["setup"], []).append(r["winner_closest_name"])

for setup in ["htf", "bf", "base"]:
    items = by_setup_closest.get(setup, [])
    n = len(items)
    counter = Counter(items)
    print(f"\n  {setup} (n={n}):")
    for ma, cnt in counter.most_common():
        print(f"    {ma:<30} {cnt:>3} ({cnt/n*100:.1f}%)")
print()

# 8 failures detail
print("=== 8 FAILURES — MA distance breakdown ===")
print(f"{'setup':<6} {'ticker':<6} {'entry':<12} {'ADR':>6} | "
      + " ".join(f"{n:>10}" for n, _ in MA_DEFS)
      + " | winner_below winner_closest")
for r in records:
    if (r["ticker"], r["entry_date"]) not in DAN_PICKS:
        continue
    dist_strs = []
    for ma_name, _ in MA_DEFS:
        v, signed, _ = r["ma_dists"][ma_name]
        if signed is None:
            dist_strs.append("    N/A   ")
        else:
            dist_strs.append(f"{signed:>+9.2f}A")  # in ADR
    print(f"{r['setup']:<6} {r['ticker']:<6} {r['entry_date']:<12} {r['adr']:>6.3f} | "
          + " ".join(dist_strs)
          + f" | {r['winner_below_name']} ({r['winner_below_adr']:.2f}A) "
          + f" {r['winner_closest_name']} ({r['winner_closest_signed']:+.2f}A)")

# 9 valids detail
print()
print("=== 9 HAND-PICK VALIDS (FORM, XPEV, RHP, CRCL, MSTR, SOUN, ANF, APP, IREN) — MA distance breakdown ===")
valid_tickers = {"FORM","XPEV","RHP","CRCL","MSTR","SOUN","ANF","APP","IREN"}
print(f"{'setup':<6} {'ticker':<6} {'entry':<12} {'ADR':>6} | "
      + " ".join(f"{n:>10}" for n, _ in MA_DEFS)
      + " | winner_below winner_closest")
for r in records:
    if r["ticker"] not in valid_tickers:
        continue
    dist_strs = []
    for ma_name, _ in MA_DEFS:
        v, signed, _ = r["ma_dists"][ma_name]
        if signed is None:
            dist_strs.append("    N/A   ")
        else:
            dist_strs.append(f"{signed:>+9.2f}A")
    print(f"{r['setup']:<6} {r['ticker']:<6} {r['entry_date']:<12} {r['adr']:>6.3f} | "
          + " ".join(dist_strs)
          + f" | {r['winner_below_name']} ({r['winner_below_adr']:.2f}A) "
          + f" {r['winner_closest_name']} ({r['winner_closest_signed']:+.2f}A)")
