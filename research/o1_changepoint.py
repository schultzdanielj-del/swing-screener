"""
O.1 — Per-setup change-point detection on monthly signal-rate timeseries.

Derives the classifier holdout cutoff date (CLASSIFIER_SPEC.md §12.9 O.1) from
data: finds the most recent significant shift in each setup's signal-firing
rate. Binary-segmentation with permutation-based significance testing.

No eyeballed thresholds. Significance cutoff is alpha=0.05 on a permutation null.
"""

import json
import os
from collections import Counter

import numpy as np

CACHE = "C:/Users/Dan/Documents/ScanPerfect/swing-screener/local_runner/cache"
LATEST = {
    "htf":   "pyramid_htf_mp_sig534_pk5_20260417_231416.json",
    "bf":    "pyramid_bf_mp_sig530_pk5_20260417_234303.json",
    "base":  "pyramid_base_mp_sig646_pk4_20260417_235640.json",
    "dtss":  "pyramid_dtss_mp_sig1369_pk11_20260417_232945.json",
    "3-4db": "pyramid_3-4db_mp_sig653_pk11_20260418_000904.json",
}

MIN_SEGMENT = 3     # months on each side of a break (allows shifts within last 3 mo)
ALPHA = 0.05
N_PERM = 500
SEED = 42


def monthly_counts(signal_dates):
    """All months in [min, max] with signal count (zero-filled)."""
    months = [d[:7] for d in signal_dates]
    if not months:
        return [], np.array([], dtype=float)
    counter = Counter(months)
    start, end = min(months), max(months)
    sy, sm = int(start[:4]), int(start[5:7])
    ey, em = int(end[:4]), int(end[5:7])
    labels, counts = [], []
    y, m = sy, sm
    while (y, m) <= (ey, em):
        key = f"{y:04d}-{m:02d}"
        labels.append(key)
        counts.append(counter.get(key, 0))
        m += 1
        if m > 12:
            m = 1
            y += 1
    return labels, np.array(counts, dtype=float)


def best_break(x, min_segment=MIN_SEGMENT):
    """Single-break likelihood-maximizing cut. Returns (break_idx, gain)."""
    n = len(x)
    if n < 2 * min_segment:
        return None, 0.0
    cs = np.concatenate([[0.0], np.cumsum(x)])
    cs2 = np.concatenate([[0.0], np.cumsum(x * x)])
    total_sum = cs[-1]
    total_sq = cs2[-1]
    total_sse = total_sq - total_sum * total_sum / n

    ts = np.arange(min_segment, n - min_segment + 1)
    left_sum = cs[ts]
    left_sq = cs2[ts]
    right_sum = total_sum - left_sum
    right_sq = total_sq - left_sq
    left_sse = left_sq - left_sum * left_sum / ts
    right_sse = right_sq - right_sum * right_sum / (n - ts)
    gains = total_sse - (left_sse + right_sse)
    idx = int(np.argmax(gains))
    return int(ts[idx]), float(gains[idx])


def permutation_pvalue(x, observed_gain, rng, n_perm=N_PERM, min_segment=MIN_SEGMENT):
    """Null: signal-count labels shuffled across months."""
    y = x.copy()
    hits = 0
    for _ in range(n_perm):
        rng.shuffle(y)
        _, g = best_break(y, min_segment=min_segment)
        if g >= observed_gain:
            hits += 1
    return (hits + 1) / (n_perm + 1)


def binary_segmentation(x, rng, min_segment=MIN_SEGMENT, alpha=ALPHA, n_perm=N_PERM):
    """All significant breaks via recursive bisection."""
    breaks = []

    def recurse(start, end):
        seg = x[start:end]
        if len(seg) < 2 * min_segment:
            return
        t_rel, gain = best_break(seg, min_segment=min_segment)
        if t_rel is None or gain <= 0:
            return
        pval = permutation_pvalue(seg, gain, rng, n_perm=n_perm, min_segment=min_segment)
        if pval >= alpha:
            return
        t_abs = start + t_rel
        breaks.append((t_abs, pval, float(seg[:t_rel].mean()), float(seg[t_rel:].mean())))
        recurse(start, t_abs)
        recurse(t_abs, end)

    recurse(0, len(x))
    return sorted(breaks)


def main():
    rng = np.random.default_rng(SEED)
    print("O.1 change-point detection — per-setup monthly signal-rate timeseries")
    print("=" * 74)
    print(f"min_segment={MIN_SEGMENT} months, alpha={ALPHA}, n_perm={N_PERM}")
    print()

    results = {}
    for setup, fn in LATEST.items():
        path = os.path.join(CACHE, fn)
        with open(path) as f:
            data = json.load(f)
        tiers = data["tier_results"]
        last = tiers[list(tiers.keys())[-1]]
        sigs = last["final_signals"]
        dates = [s["date"] for s in sigs]
        months, counts = monthly_counts(dates)

        print(f"=== {setup} ===")
        if len(counts) == 0:
            print("  no signals\n")
            continue
        print(f"  range: {months[0]} to {months[-1]}  ({len(months)} months, {int(counts.sum())} total signals)")
        breaks = binary_segmentation(counts, rng)
        if not breaks:
            print("  no significant break at alpha=0.05")
            results[setup] = None
        else:
            print(f"  {len(breaks)} significant break(s):")
            for t_abs, pval, pre, post in breaks:
                direction = "drop" if post < pre else "rise"
                print(f"    at {months[t_abs]}  ({direction}: {pre:.1f} -> {post:.1f} sig/mo, p={pval:.3f})")
            # Most-recent break drives O.1 cutoff
            most_recent = max(breaks, key=lambda b: b[0])
            t_abs, pval, pre, post = most_recent
            results[setup] = {
                "cutoff_month": months[t_abs],
                "pval": pval,
                "pre_mean": pre,
                "post_mean": post,
                "direction": "drop" if post < pre else "rise",
            }
        print()

    print("=" * 74)
    print("O.1 recommendation — most recent significant break per setup")
    print("=" * 74)
    for setup in LATEST:
        r = results.get(setup)
        if r is None:
            print(f"  {setup:<7}  no significant shift - use full history for holdout")
        else:
            print(f"  {setup:<7}  cutoff = {r['cutoff_month']}-01  "
                  f"({r['direction']}: {r['pre_mean']:.1f} -> {r['post_mean']:.1f} sig/mo, p={r['pval']:.3f})")


if __name__ == "__main__":
    main()
