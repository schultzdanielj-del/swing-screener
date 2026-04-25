"""Emit classifier tag JSON per setup.

Per-signal tag: ENTRY / REDUNDANT / NOENTRY / MISSING.

Mechanic (locked 2026-04-23, see CLASSIFIER_SPEC.md §17.7):
- Adaptive 200-bar argmax-AVWAP resistance anchor (uses min(200, sig_idx) bars).
- AND-gate: close[sig+k] > sig_close AND close[sig+k] > resistance_AVWAP.
- Forward window W = 11 bars (derived from max sibling-to-entry gap on examples).
- REDUNDANT = another double signal on same ticker falls strictly between
  sig_idx and entry_bar_idx.
- NOENTRY = AND-gate never fires within W bars.
- MISSING = ticker absent from OHLCV cache.

Per-signal features (emitted alongside tag):
- run_length, run_position (first/middle/last/singleton), run_index_from_left/right
  on consecutive-bar runs of same-ticker pool signals.
- lead_up_density_50, lead_up_density_200: count of prior same-ticker pool signals
  in [sig-W, sig-1] for W=50 and W=200.
- bars_since_prior: gap to nearest prior same-ticker pool signal (None if none).
- min_low_offset_adr: signed min-low offset in ADR during (sig, entry) window.
  Negative = drawdown below sig_close (MAE). Positive = lows stayed above sig_close.
  None for NOENTRY/MISSING; 0.0 when entry_k==1 (no bars between).

Output: research/classifier_tags/{setup}_tags.json
"""
import json
import pickle
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import numpy as np

OHLCV = r"C:/Users/Dan/Documents/ScanPerfect/swing-screener/local_runner/cache/universe_ohlcv_daily.pkl"
POOL_DIR = Path("research/classifier_pool")
OUT_DIR = Path("research/classifier_tags")
OUT_DIR.mkdir(parents=True, exist_ok=True)

W = 11
LOOKBACK = 200

with open(OHLCV, "rb") as f:
    ohlcv = pickle.load(f)

print(f"OHLCV cache: {len(ohlcv)} tickers")


def argmax_avwap(h, l, c, v, sig_idx, lookback):
    A_start = max(0, sig_idx - lookback)
    A_end = sig_idx
    if A_start >= A_end:
        return None, float("nan")
    tp = (h + l + c) / 3.0
    cum_tpv = np.concatenate([[0.0], np.cumsum(tp * v)])
    cum_v = np.concatenate([[0.0], np.cumsum(v)])
    A_range = np.arange(A_start, A_end)
    ttpv = cum_tpv[sig_idx + 1] - cum_tpv[A_range]
    tv = cum_v[sig_idx + 1] - cum_v[A_range]
    with np.errstate(invalid="ignore", divide="ignore"):
        avwaps = np.where(tv > 0, ttpv / tv, -np.inf)
    best = int(np.argmax(avwaps))
    return int(A_range[best]), float(avwaps[best])


def bar_date(df, idx):
    d = df["date"].iloc[idx]
    if hasattr(d, "strftime"):
        return d.strftime("%Y-%m-%d")
    return str(d)[:10]


def adr14(highs, lows, idx):
    if idx < 14:
        return float("nan")
    return float(np.mean(highs[idx - 13 : idx + 1] - lows[idx - 13 : idx + 1]))


def run_info(sig_idx, sigs_set):
    """Consecutive-bar run containing sig_idx. Gap-0 (strictly consecutive)."""
    left_count = 0
    k = 1
    while (sig_idx - k) in sigs_set:
        left_count += 1
        k += 1
    right_count = 0
    k = 1
    while (sig_idx + k) in sigs_set:
        right_count += 1
        k += 1
    run_length = left_count + right_count + 1
    run_index = left_count
    if run_length == 1:
        position = "singleton"
    elif run_index == 0:
        position = "first"
    elif run_index == run_length - 1:
        position = "last"
    else:
        position = "middle"
    return {
        "run_length": run_length,
        "run_position": position,
        "run_index_from_left": run_index,
        "run_index_from_right": run_length - 1 - run_index,
    }


def lead_up_density(sig_idx, sigs_sorted, window):
    lo = sig_idx - window
    hi = sig_idx - 1
    return sum(1 for s in sigs_sorted if lo <= s <= hi)


def bars_since_prior(sig_idx, sigs_sorted):
    priors = [s for s in sigs_sorted if s < sig_idx]
    if not priors:
        return None
    return sig_idx - max(priors)


def min_low_offset_adr(lows, sig_idx, entry_idx, sig_close, adr):
    """Signed min-low offset from sig_close over (sig, entry) bars.

    Negative = low dropped below sig_close (actual drawdown / MAE).
    Zero     = entry_k == 1 (no intervening bars) or low exactly touched sig_close.
    Positive = lows stayed entirely above sig_close (strong pre-entry advance).
    """
    if entry_idx is None or not np.isfinite(adr) or adr <= 0:
        return None
    if entry_idx <= sig_idx + 1:
        return 0.0
    slice_lows = lows[sig_idx + 1 : entry_idx]
    if len(slice_lows) == 0:
        return 0.0
    return float((float(np.min(slice_lows)) - sig_close) / adr)


for setup in ["htf", "bf", "base"]:
    with open(POOL_DIR / f"{setup}_pool.json") as f:
        pool = json.load(f)
    clusters = pool["clusters"]

    # ticker -> sorted sig_idx list of all signals in pool (for redundancy + features)
    by_ticker = defaultdict(list)
    for c in clusters:
        by_ticker[c["ticker"]].append(c["rightmost"]["bar_idx"])
    by_ticker_sorted = {t: sorted(set(v)) for t, v in by_ticker.items()}
    by_ticker_set = {t: set(v) for t, v in by_ticker_sorted.items()}

    out_clusters = []
    counts = {"ENTRY": 0, "REDUNDANT": 0, "NOENTRY": 0, "MISSING": 0}
    counts_ex = {"ENTRY": 0, "REDUNDANT": 0, "NOENTRY": 0, "MISSING": 0}

    for c in clusters:
        ticker = c["ticker"]
        sig_idx = c["rightmost"]["bar_idx"]
        sig_date = c["rightmost"]["date"]
        sig_close = c["rightmost"]["close"]
        is_example = c["is_example"]

        # Features computed from pool structure alone (don't need OHLCV)
        sigs_sorted = by_ticker_sorted.get(ticker, [])
        sigs_set = by_ticker_set.get(ticker, set())
        run = run_info(sig_idx, sigs_set)
        dens_50 = lead_up_density(sig_idx, sigs_sorted, 50)
        dens_200 = lead_up_density(sig_idx, sigs_sorted, 200)
        prior_gap = bars_since_prior(sig_idx, sigs_sorted)

        record = {
            "cluster_id": c["cluster_id"],
            "ticker": ticker,
            "sig_idx": sig_idx,
            "sig_date": sig_date,
            "sig_close": sig_close,
            "is_example": is_example,
            "tag": None,
            "entry_k": None,
            "entry_idx": None,
            "entry_date": None,
            "anchor_idx": None,
            "anchor_date": None,
            "resistance_avwap": None,
            # Per-signal features
            "run_length": run["run_length"],
            "run_position": run["run_position"],
            "run_index_from_left": run["run_index_from_left"],
            "run_index_from_right": run["run_index_from_right"],
            "lead_up_density_50": dens_50,
            "lead_up_density_200": dens_200,
            "bars_since_prior": prior_gap,
            "min_low_offset_adr": None,
            "adr_at_sig": None,
        }

        if ticker not in ohlcv:
            record["tag"] = "MISSING"
            counts["MISSING"] += 1
            if is_example:
                counts_ex["MISSING"] += 1
            out_clusters.append(record)
            continue

        df = ohlcv[ticker]
        highs = df["high"].to_numpy()
        lows = df["low"].to_numpy()
        closes = df["close"].to_numpy()
        vols = df["volume"].to_numpy()
        n = len(closes)

        if sig_idx < 1 or sig_idx >= n:
            record["tag"] = "MISSING"
            counts["MISSING"] += 1
            if is_example:
                counts_ex["MISSING"] += 1
            out_clusters.append(record)
            continue

        adr = adr14(highs, lows, sig_idx)
        if np.isfinite(adr) and adr > 0:
            record["adr_at_sig"] = float(adr)

        anchor_idx, avwap = argmax_avwap(highs, lows, closes, vols, sig_idx, LOOKBACK)
        if anchor_idx is None or not np.isfinite(avwap):
            record["tag"] = "NOENTRY"
            counts["NOENTRY"] += 1
            if is_example:
                counts_ex["NOENTRY"] += 1
            out_clusters.append(record)
            continue

        record["anchor_idx"] = anchor_idx
        record["anchor_date"] = bar_date(df, anchor_idx)
        record["resistance_avwap"] = float(avwap)

        entry_k = None
        for k in range(1, W + 1):
            fk = sig_idx + k
            if fk >= n:
                break
            if closes[fk] > sig_close and closes[fk] > avwap:
                entry_k = k
                break

        if entry_k is None:
            record["tag"] = "NOENTRY"
            counts["NOENTRY"] += 1
            if is_example:
                counts_ex["NOENTRY"] += 1
            out_clusters.append(record)
            continue

        entry_idx = sig_idx + entry_k
        record["entry_k"] = entry_k
        record["entry_idx"] = entry_idx
        record["entry_date"] = bar_date(df, entry_idx)

        mae_adr = min_low_offset_adr(lows, sig_idx, entry_idx, sig_close, adr)
        record["min_low_offset_adr"] = mae_adr

        intervening = any(sig_idx < s < entry_idx for s in sigs_sorted)
        tag = "REDUNDANT" if intervening else "ENTRY"
        record["tag"] = tag
        counts[tag] += 1
        if is_example:
            counts_ex[tag] += 1
        out_clusters.append(record)

    out = {
        "setup": setup,
        "mechanic": {
            "lookback_bars": LOOKBACK,
            "lookback_note": "adaptive: min(lookback_bars, sig_idx) for recent-IPO cases",
            "trigger": "close[sig+k] > sig_close AND close[sig+k] > resistance_AVWAP",
            "forward_window_W": W,
            "tag_values": ["ENTRY", "REDUNDANT", "NOENTRY", "MISSING"],
            "tag_semantics": {
                "ENTRY": "AND-gate fires in [1, W] and no other double signal falls strictly between sig and entry",
                "REDUNDANT": "AND-gate fires but another same-ticker double signal falls strictly between sig and entry",
                "NOENTRY": "AND-gate never fires within W forward bars",
                "MISSING": "ticker not present in OHLCV cache",
            },
        },
        "per_signal_features": {
            "run_length": "length of consecutive-bar run of same-ticker pool signals containing this signal (1 = singleton)",
            "run_position": "first / middle / last / singleton position in run",
            "run_index_from_left": "0-indexed position within run (0 = first)",
            "run_index_from_right": "0-indexed position within run from end (0 = last)",
            "lead_up_density_50": "count of prior same-ticker pool signals in [sig-50, sig-1]",
            "lead_up_density_200": "count of prior same-ticker pool signals in [sig-200, sig-1]",
            "bars_since_prior": "gap in bars to nearest prior same-ticker pool signal (null if none)",
            "min_low_offset_adr": "signed min-low offset in ADR during (sig, entry). Negative=drawdown (MAE), Positive=lows stayed above sig_close. null for NOENTRY/MISSING; 0.0 when entry_k==1.",
            "adr_at_sig": "ADR(14) at signal bar in price units",
        },
        "derived_at": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        "pool_source": pool.get("pool_source"),
        "pool_timestamp": pool.get("timestamp"),
        "counts": {
            "total": len(out_clusters),
            "examples_total": sum(counts_ex.values()),
            "wild_total": len(out_clusters) - sum(counts_ex.values()),
            "by_tag": counts,
            "by_tag_examples": counts_ex,
        },
        "clusters": out_clusters,
    }

    out_path = OUT_DIR / f"{setup}_tags.json"
    with open(out_path, "w") as f:
        json.dump(out, f, indent=2)

    print(f"  {setup}: wrote {out_path}  (total={len(out_clusters)}, examples_ENTRY={counts_ex['ENTRY']}/{sum(counts_ex.values())}, wild_ENTRY={counts['ENTRY']-counts_ex['ENTRY']})")
