"""Test the 'first-crossing-walking-backward' anchor rule against argmax + Dan's picks.

Rule under test:
  anchor = max A < sig such that AVWAP(A..sig) >= close[sig]
       AND for all A' in (A, sig), AVWAP(A'..sig) < close[sig]
  i.e., walking backward from sig-1, the first A where the cumulative AVWAP first reaches >= close[sig].

If no A satisfies AVWAP >= close[sig] at all, return None (no valid contextual anchor).

Output:
  1. Across all 113 examples: count how often first-crossing == argmax, when they differ, by how many bars.
  2. On the 8 known AND-gate failures: compare first-crossing to Dan's picks.
  3. T1 test: under first-crossing, does AND-gate pass on all 113?
"""
import pickle
import sqlite3
import numpy as np
import pandas as pd

CACHE = r"C:/Users/Dan/Documents/ScanPerfect/swing-screener/local_runner/cache/universe_ohlcv_daily.pkl"
DB = r"C:/Users/Dan/Documents/ScanPerfect/swing-screener/data/scanperfect.db"

DAN_PICKS = {
    # (ticker, entry_date) -> dan-anchor-date (earliest of provided range when range given)
    ("AR",   "2020-12-17"): "2020-12-11",
    ("BB",   "2024-12-20"): "2024-12-17",
    ("DRN",  "2024-07-11"): "2023-12-04",  # plateau 12-04..12-13
    ("HTT",  "2021-01-12"): "2020-06-19",  # +/- 1 candle
    ("LMND", "2024-11-05"): "2024-11-01",
    ("LMND", "2024-11-06"): "2024-11-01",
    ("PTON", "2024-10-14"): "2024-09-20",  # DB entry; Dan's chase calls 10-11, sig same trading bar
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
    tp = (h + l + c) / 3.0
    return dates, h, l, c, v, tp


def compute_anchors(tp, v, c, sig_idx):
    """Return (argmax_A, argmax_val, firstcross_A, firstcross_val) for given sig.
    firstcross_A = the largest A < sig with AVWAP(A..sig) >= c[sig] AND AVWAP(A+1..sig) < c[sig]
    (i.e., as A decreases from sig-1, the first A where the curve reaches >= c[sig]).
    None if no such A.
    """
    cum_tpv = np.concatenate([[0.0], np.cumsum(tp * v)])
    cum_v = np.concatenate([[0.0], np.cumsum(v)])
    A_range = np.arange(0, sig_idx)
    total_tpv = cum_tpv[sig_idx + 1] - cum_tpv[A_range]
    total_v = cum_v[sig_idx + 1] - cum_v[A_range]
    with np.errstate(invalid="ignore", divide="ignore"):
        avwaps = np.where(total_v > 0, total_tpv / total_v, -np.inf)

    argmax_local = int(np.argmax(avwaps))
    argmax_A = int(A_range[argmax_local])
    argmax_val = float(avwaps[argmax_local])

    sig_close = c[sig_idx]
    # firstcross: largest A (closest to sig) such that AVWAP(A..sig) >= sig_close
    above = avwaps >= sig_close
    if not above.any():
        return argmax_A, argmax_val, None, None

    # walk backward from sig-1 (highest A index) and find the first index where above is True
    # equivalently: among indices where above is True, the LARGEST index = closest to sig
    candidates = np.where(above)[0]  # indices into A_range (0 = bar 0, sig_idx-1 = bar sig-1)
    # The "first crossing walking backward" is actually: the largest A such that
    #   above[A] = True AND (A == sig_idx-1 OR above[A+1] = False)
    # That is: A is in `above`, and the bar immediately closer to sig (A+1) is NOT in `above`.
    # If above[sig_idx-1] = True (closest bar), that's the answer.
    fc_A = None
    for A in reversed(candidates.tolist()):
        if A == sig_idx - 1 or not above[A + 1]:
            fc_A = int(A)
            break
    if fc_A is None:
        # Should not happen, but fallback to the largest candidate
        fc_A = int(candidates.max())
    fc_val = float(avwaps[fc_A])
    return argmax_A, argmax_val, fc_A, fc_val


def evaluate_gate(c, sig_idx, anchor_avwap):
    sig_close = c[sig_idx]
    end = min(sig_idx + 10, len(c) - 1)
    fwd = c[sig_idx + 1: end + 1]
    pass_close = fwd > sig_close
    pass_avwap = fwd > anchor_avwap
    return bool((pass_close & pass_avwap).any())


# Build records
records = []
for setup, ticker, entry_date in rows:
    if ticker not in universe:
        continue
    dates, h, l, c, v, tp = load(ticker)
    hits = np.where(dates == entry_date)[0]
    if len(hits) == 0:
        continue
    entry_idx = int(hits[0])
    sig_idx = entry_idx - 1
    if sig_idx < 1:
        continue

    am_A, am_val, fc_A, fc_val = compute_anchors(tp, v, c, sig_idx)
    sig_close = float(c[sig_idx])

    am_pass = evaluate_gate(c, sig_idx, am_val)
    fc_pass = evaluate_gate(c, sig_idx, fc_val) if fc_val is not None else None

    records.append({
        "setup": setup, "ticker": ticker, "entry_date": entry_date,
        "sig_close": sig_close,
        "argmax_A_date": dates[am_A], "argmax_bars_back": sig_idx - am_A,
        "argmax_val": am_val, "argmax_pass": am_pass,
        "fc_A_date": dates[fc_A] if fc_A is not None else None,
        "fc_bars_back": (sig_idx - fc_A) if fc_A is not None else None,
        "fc_val": fc_val, "fc_pass": fc_pass,
        "same_anchor": (fc_A == am_A) if fc_A is not None else False,
    })

# Section 1: agreement summary
total = len(records)
same = sum(1 for r in records if r["same_anchor"])
diff = total - same
print(f"=== AGREEMENT (first-crossing vs argmax) ===")
print(f"Total examples: {total}")
print(f"first-crossing == argmax: {same} ({same/total*100:.1f}%)")
print(f"first-crossing != argmax: {diff} ({diff/total*100:.1f}%)")
print()

# Section 2: cases where they differ
print("=== CASES WHERE first-crossing != argmax ===")
print(f"{'setup':<6} {'ticker':<8} {'entry':<12} {'sig_cl':>8} {'argmax_date':<12} {'am_back':>7} {'am_val':>8} {'fc_date':<12} {'fc_back':>7} {'fc_val':>8} {'am_pass':>7} {'fc_pass':>7}")
for r in records:
    if r["same_anchor"]:
        continue
    fc_date = r["fc_A_date"] or "-"
    fc_back = r["fc_bars_back"] if r["fc_bars_back"] is not None else "-"
    fc_val = f"{r['fc_val']:.2f}" if r["fc_val"] is not None else "-"
    fc_pass = "Y" if r["fc_pass"] else ("N" if r["fc_pass"] is not None else "-")
    print(
        f"{r['setup']:<6} {r['ticker']:<8} {r['entry_date']:<12} "
        f"{r['sig_close']:>8.2f} {r['argmax_A_date']:<12} {r['argmax_bars_back']:>7d} "
        f"{r['argmax_val']:>8.2f} {fc_date:<12} {fc_back!s:>7} {fc_val:>8} "
        f"{'Y' if r['argmax_pass'] else 'N':>7} {fc_pass:>7}"
    )
print()

# Section 3: AND-gate T1 test under first-crossing
fc_pass_count = sum(1 for r in records if r["fc_pass"] is True)
fc_fail_count = sum(1 for r in records if r["fc_pass"] is False)
fc_none = sum(1 for r in records if r["fc_pass"] is None)
print(f"=== T1 (AND-gate) UNDER FIRST-CROSSING ===")
print(f"Pass: {fc_pass_count}/{total}")
print(f"Fail: {fc_fail_count}/{total}")
print(f"No anchor (no AVWAP >= sig_close anywhere): {fc_none}/{total}")
print()

# Section 4: comparison to Dan's picks on the 8 failures
print("=== DAN PICKS vs FIRST-CROSSING (8 failures) ===")
print(f"{'setup':<6} {'ticker':<8} {'entry':<12} {'dan_anchor':<12} {'fc_anchor':<12} {'match?':>8} {'dan_avwap':>10} {'fc_avwap':>10}")
for r in records:
    key = (r["ticker"], r["entry_date"])
    if key not in DAN_PICKS:
        continue
    dan_anchor = DAN_PICKS[key]
    # Compute Dan's anchor's AVWAP
    dates, h, l, c, v, tp = load(r["ticker"])
    a_idx = np.where(dates == dan_anchor)[0]
    if len(a_idx) == 0:
        continue
    a = int(a_idx[0])
    sig = np.where(dates == r["entry_date"])[0][0] - 1
    seg_tp = tp[a:sig+1]
    seg_v = v[a:sig+1]
    dan_val = float((seg_tp * seg_v).sum() / seg_v.sum())
    match = "YES" if r["fc_A_date"] == dan_anchor else "NO"
    fc_avwap = f"{r['fc_val']:.2f}" if r['fc_val'] is not None else "-"
    print(f"{r['setup']:<6} {r['ticker']:<8} {r['entry_date']:<12} {dan_anchor:<12} "
          f"{(r['fc_A_date'] or '-'):<12} {match:>8} {dan_val:>10.2f} {fc_avwap:>10}")
