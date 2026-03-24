"""
Consensus Engine — Multi-Run Stability Selection + Permutation Testing.

Signal mode:
  - Reads real + permuted grind outputs from a directory
  - Counts condition frequencies for both sets
  - Computes bootstrap z-score comparing real vs permuted
  - Gates on z > 3 (99.7% confidence)
  - Locks consensus conditions with 5% margin

Refinement mode:
  - TODO: Increment 9

Based on Meinshausen & Bühlmann (2010) stability selection framework
and standard permutation testing (Tusher et al. 2001).

Usage:
    python scripts/consensus_engine.py --setup dtss --stage signal \
        --threshold 0.7 --input-dir local_runner/cache/consensus/
"""

import os
import sys
import json
import glob
import argparse
import random
from datetime import datetime, timezone
from collections import Counter, defaultdict

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOCAL_DIR = os.path.join(REPO_ROOT, "local_runner")
CACHE_DIR = os.path.join(LOCAL_DIR, "cache")


# ══════════════════════════════════════════════════════════════
# FILE DISCOVERY
# ══════════════════════════════════════════════════════════════

def find_real_signal_files(setup_type, input_dir):
    """Find real signal grind outputs (pyramid_*.json) in input_dir."""
    pattern = os.path.join(input_dir, f"pyramid_{setup_type}_mp_*.json")
    files = []
    for path in sorted(glob.glob(pattern)):
        try:
            with open(path) as f:
                data = json.load(f)
            conds = data.get("all_conditions", [])
            if not conds:
                continue
            files.append({"path": path, "filename": os.path.basename(path),
                          "n_conditions": len(conds)})
        except Exception as e:
            print(f"  WARNING: Could not read {os.path.basename(path)}: {e}")
    return files


def find_permuted_signal_files(setup_type, input_dir):
    """Find permuted signal grind outputs (permuted_*.json) in input_dir."""
    pattern = os.path.join(input_dir, f"permuted_{setup_type}_mp_*.json")
    files = []
    for path in sorted(glob.glob(pattern)):
        try:
            with open(path) as f:
                data = json.load(f)
            conds = data.get("all_conditions", [])
            if not conds:
                continue
            files.append({"path": path, "filename": os.path.basename(path),
                          "n_conditions": len(conds)})
        except Exception as e:
            print(f"  WARNING: Could not read {os.path.basename(path)}: {e}")
    return files


# ══════════════════════════════════════════════════════════════
# CONDITION EXTRACTION
# ══════════════════════════════════════════════════════════════

def extract_conditions(path):
    """Extract all_conditions from a grind output JSON."""
    with open(path) as f:
        data = json.load(f)
    return data.get("all_conditions", [])


def extract_condition_names(path):
    """Extract just the condition names from a grind output."""
    conds = extract_conditions(path)
    return set(c.get("name", c.get("expr", "")) for c in conds)


# ══════════════════════════════════════════════════════════════
# BOOTSTRAP Z-SCORE
# ══════════════════════════════════════════════════════════════

def count_consensus_conditions(run_condition_sets, n_runs, threshold):
    """Count how many unique conditions appear in >= threshold fraction of runs.

    Args:
        run_condition_sets: list of sets, each containing condition names from one run
        n_runs: total number of runs (for threshold computation)
        threshold: fraction (0.0-1.0)

    Returns:
        set of condition names that meet the threshold
    """
    freq = Counter()
    for name_set in run_condition_sets:
        for name in name_set:
            freq[name] += 1
    min_appearances = max(1, int(n_runs * threshold))
    return set(name for name, count in freq.items() if count >= min_appearances)


def bootstrap_permuted_count(permuted_name_sets, n_real_runs, threshold,
                             n_bootstrap=1000, seed=42):
    """Bootstrap the number of consensus conditions from permuted runs.

    Resamples with replacement from the available permuted runs,
    counts consensus conditions at the given threshold, repeats n_bootstrap times.

    Args:
        permuted_name_sets: list of sets, each containing condition names from one permuted run
        n_real_runs: number of real runs (determines how many to draw per resample)
        threshold: consensus threshold
        n_bootstrap: number of bootstrap iterations
        seed: random seed for reproducibility

    Returns:
        list of n_bootstrap counts (one per iteration)
    """
    rng = random.Random(seed)
    n_perm = len(permuted_name_sets)
    counts = []
    for _ in range(n_bootstrap):
        # Draw n_real_runs permuted runs with replacement
        drawn = [permuted_name_sets[rng.randint(0, n_perm - 1)]
                 for _ in range(n_real_runs)]
        consensus = count_consensus_conditions(drawn, n_real_runs, threshold)
        counts.append(len(consensus))
    return counts


def compute_z_score(R, bootstrap_counts):
    """Compute z-score: (R - mean_P) / std_P with edge case handling.

    Args:
        R: number of real consensus conditions
        bootstrap_counts: list of permuted consensus condition counts

    Returns:
        (z_score, mean_P, std_P)
    """
    mean_P = sum(bootstrap_counts) / len(bootstrap_counts)
    variance = sum((x - mean_P) ** 2 for x in bootstrap_counts) / len(bootstrap_counts)
    std_P = variance ** 0.5

    if std_P == 0:
        if R > mean_P:
            z = float("inf")
        elif R == mean_P:
            z = 0.0
        else:
            z = float("-inf")
    else:
        z = (R - mean_P) / std_P

    return z, mean_P, std_P


# ══════════════════════════════════════════════════════════════
# SIGNAL CONSENSUS
# ══════════════════════════════════════════════════════════════

def run_signal_consensus(setup_type, threshold, input_dir):
    """Run the full signal consensus pipeline.

    Phases A-E from SIGNAL_GRINDER.md spec.
    """
    print("\n" + "=" * 70)
    print("  CONSENSUS ENGINE — SIGNAL")
    print("=" * 70)
    print(f"  Setup: {setup_type.upper()}")
    print(f"  Threshold: {threshold} ({threshold:.0%})")
    print(f"  Input dir: {input_dir}")

    # ── Phase A: Count condition frequencies (real runs) ──
    print(f"\n  Phase A: Loading real signal grind outputs...")
    real_files = find_real_signal_files(setup_type, input_dir)
    n_real = len(real_files)
    print(f"  Found {n_real} real runs")

    if n_real == 0:
        print(f"  ERROR: No real signal grind files found in {input_dir}")
        print(f"  Expected: pyramid_{setup_type}_mp_*.json")
        return None

    for rf in real_files:
        print(f"    {rf['filename']}: {rf['n_conditions']} conditions")

    real_name_sets = []
    real_condition_dicts = {}  # name -> full condition dict (from any run)
    for rf in real_files:
        conds = extract_conditions(rf["path"])
        names = set()
        for c in conds:
            name = c.get("name", c.get("expr", ""))
            names.add(name)
            if name not in real_condition_dicts:
                real_condition_dicts[name] = c
        real_name_sets.append(names)

    # Count frequencies
    real_freq = Counter()
    for name_set in real_name_sets:
        for name in name_set:
            real_freq[name] += 1

    min_appearances = max(1, int(n_real * threshold))
    consensus_names = set(
        name for name, count in real_freq.items() if count >= min_appearances
    )
    R = len(consensus_names)

    print(f"\n  Real runs: {n_real}")
    print(f"  Unique conditions across all real runs: {len(real_freq)}")
    print(f"  Consensus threshold: {min_appearances}/{n_real} "
          f"({threshold:.0%})")
    print(f"  Conditions at consensus (R): {R}")

    # ── Phase B: Count condition frequencies (permuted runs) ──
    print(f"\n  Phase B: Loading permuted signal grind outputs...")
    perm_files = find_permuted_signal_files(setup_type, input_dir)
    n_perm = len(perm_files)
    print(f"  Found {n_perm} permuted runs")

    if n_perm < 3:
        print(f"  ERROR: Need at least 3 permuted runs for reliable null distribution. "
              f"Found {n_perm}.")
        # Write abort report
        _write_abort_report(setup_type, input_dir, n_real, n_perm, R,
                            reason="Too few permuted runs")
        return None

    for pf in perm_files:
        print(f"    {pf['filename']}: {pf['n_conditions']} conditions")

    perm_name_sets = []
    for pf in perm_files:
        names = extract_condition_names(pf["path"])
        perm_name_sets.append(names)

    # ── Phase C: Bootstrap z-score ──
    print(f"\n  Phase C: Bootstrap z-score (1000 iterations)...")
    bootstrap_counts = bootstrap_permuted_count(
        perm_name_sets, n_real, threshold, n_bootstrap=1000, seed=42
    )
    z, mean_P, std_P = compute_z_score(R, bootstrap_counts)

    print(f"  R (real consensus conditions): {R}")
    print(f"  Permuted null: mean={mean_P:.1f}, std={std_P:.2f}")
    if z == float("inf"):
        print(f"  z-score: inf")
    elif z == float("-inf"):
        print(f"  z-score: -inf")
    else:
        print(f"  z-score: {z:.2f}")

    # Frequency distribution for real runs
    print(f"\n  Frequency distribution (real runs):")
    freq_dist = Counter(real_freq.values())
    for freq_val in sorted(freq_dist.keys(), reverse=True):
        count = freq_dist[freq_val]
        bar = "█" * count
        marker = " ← threshold" if freq_val == min_appearances else ""
        print(f"    {freq_val:>3}/{n_real}: {count:>3} conditions  {bar}{marker}")

    # ── Phase D: Gate decision ──
    print(f"\n  Phase D: Gate decision...")
    if z > 3:
        gate = "PROCEED"
        z_disp = f"{z:.2f}" if z != float("inf") else "inf"
        print(f"  ✓ z = {z_disp} > 3 — PROCEED to Step 3")
        print(f"    99.7% confidence the pattern is real.")
    elif z >= 2:
        gate = "CAUTION"
        print(f"  ⚠ z = {z:.2f} — signal above noise but moderate confidence.")
        print(f"    Judgment call: proceed with caution or vet more examples.")
    else:
        gate = "STOP"
        print(f"  ✗ z = {z:.2f} < 2 — real conditions indistinguishable from noise.")
        print(f"    STOP. Vet more examples and re-run.")

    # ── Phase E: Lock conditions (only if gate allows) ──
    locked_conditions = []
    if gate in ("PROCEED", "CAUTION"):
        print(f"\n  Phase E: Locking {R} consensus conditions...")
        for name in sorted(consensus_names):
            c = real_condition_dicts[name]
            low = c.get("low", 0)
            high = c.get("high", 0)
            # Apply 5% margin
            margin = (high - low) * 0.05
            locked_low = low - margin
            locked_high = high + margin
            locked_conditions.append({
                "name": name,
                "expr": name,
                "category": c.get("category", ""),
                "tier": c.get("tier", ""),
                "compute": c.get("compute", ""),
                "filter_power": c.get("filter_power"),
                "low": locked_low,
                "high": locked_high,
                "frequency": real_freq[name],
                "frequency_pct": round(real_freq[name] / n_real, 3),
            })
        print(f"  {len(locked_conditions)} conditions locked with 5% margin")

        # Print locked conditions
        print(f"\n  Locked conditions:")
        print(f"    {'#':>3} {'Freq':>5} {'Name':<50s} {'Tier':<6}")
        print(f"    {'-'*3} {'-'*5} {'-'*50} {'-'*6}")
        for i, c in enumerate(locked_conditions):
            print(f"    {i+1:>3} {c['frequency']:>3}/{n_real} "
                  f"{c['name']:<50s} {c.get('tier',''):<6}")

    # ── Build output ──
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")

    # Stability metrics
    pairwise_overlaps = []
    for i in range(len(real_name_sets)):
        for j in range(i + 1, len(real_name_sets)):
            union = real_name_sets[i] | real_name_sets[j]
            inter = real_name_sets[i] & real_name_sets[j]
            if union:
                pairwise_overlaps.append(len(inter) / len(union))
    avg_overlap = (sum(pairwise_overlaps) / len(pairwise_overlaps)
                   if pairwise_overlaps else 0)
    avg_conds_per_run = (sum(len(s) for s in real_name_sets) / n_real
                         if n_real > 0 else 0)

    output = {
        "setup_type": setup_type,
        "stage": "signal",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "z_score": z if z != float("inf") else 999.0,
        "gate_decision": gate,
        "n_real_runs": n_real,
        "n_permuted_runs": n_perm,
        "threshold": threshold,
        "min_appearances": min_appearances,
        "R_real_consensus": R,
        "permuted_null_mean": round(mean_P, 2),
        "permuted_null_std": round(std_P, 4),
        "n_conditions": len(locked_conditions),
        "all_conditions": locked_conditions,
        "stability_metrics": {
            "avg_pairwise_overlap": round(avg_overlap, 4),
            "avg_conditions_per_run": round(avg_conds_per_run, 1),
            "frequency_distribution": {
                str(k): v for k, v in sorted(
                    Counter(real_freq.values()).items(), reverse=True)
            },
        },
        "run_files": {
            "real": [rf["filename"] for rf in real_files],
            "permuted": [pf["filename"] for pf in perm_files],
        },
    }

    # Save output
    if gate in ("PROCEED", "CAUTION"):
        # Write to standard cache directory
        os.makedirs(CACHE_DIR, exist_ok=True)
        out_path = os.path.join(
            CACHE_DIR, f"consensus_signal_{setup_type}.json")
        with open(out_path, "w") as f:
            json.dump(output, f, indent=2)
        print(f"\n  Saved: {out_path}")

        # Also save timestamped version
        ts_path = os.path.join(
            CACHE_DIR, f"consensus_signal_{setup_type}_{ts}.json")
        with open(ts_path, "w") as f:
            json.dump(output, f, indent=2)
        print(f"  Saved: {ts_path}")
    else:
        # z < 2: write gate report to input dir, not standard cache
        _write_gate_report(setup_type, input_dir, output)

    # Summary
    print(f"\n  {'='*50}")
    if locked_conditions:
        print(f"  ✓ CONSENSUS: {len(locked_conditions)} conditions locked")
        z_disp = f"{z:.2f}" if z != float("inf") else "inf"
        print(f"    z-score: {z_disp}")
        print(f"    Stability: {avg_overlap:.1%} avg pairwise overlap")
    else:
        print(f"  ✗ CONSENSUS FAILED: z = {z:.2f}")
        print(f"    Vet more examples and re-run.")
    print(f"  {'='*50}")

    return output


# ══════════════════════════════════════════════════════════════
# REPORT WRITERS
# ══════════════════════════════════════════════════════════════

def _write_abort_report(setup_type, input_dir, n_real, n_perm, R, reason):
    """Write abort report when pipeline can't proceed."""
    report = {
        "setup_type": setup_type,
        "stage": "signal",
        "status": "ABORTED",
        "reason": reason,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "n_real_runs": n_real,
        "n_permuted_runs": n_perm,
        "R_real_consensus": R,
    }
    os.makedirs(input_dir, exist_ok=True)
    path = os.path.join(input_dir, f"consensus_abort_{setup_type}.json")
    with open(path, "w") as f:
        json.dump(report, f, indent=2)
    print(f"\n  Abort report: {path}")


def _write_gate_report(setup_type, input_dir, output):
    """Write gate report when z < 2."""
    output["status"] = "GATE_FAILED"
    output["recommendation"] = (
        f"z = {output['z_score']:.1f}. Real conditions not distinguishable "
        f"from noise. Vet more examples and re-run."
    )
    os.makedirs(input_dir, exist_ok=True)
    path = os.path.join(input_dir, f"consensus_gate_{setup_type}.json")
    with open(path, "w") as f:
        json.dump(output, f, indent=2)
    print(f"\n  Gate report: {path}")


# ══════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════

def _cluster_to_signal(cluster):
    """Convert a cluster dict from raw_signal_clusters to a per-signal dict."""
    return {
        "ticker": cluster["ticker"],
        "signal_date": cluster["rightmost"].get("date"),
        "bar_idx": cluster["rightmost"]["bar_idx"],
        "close": cluster["rightmost"].get("close"),
        "is_example": cluster.get("is_example", 0),
        "classification": cluster["classification"],
        "move_adr": cluster.get("move_adr"),
        "adr_at_signal": cluster.get("adr_at_signal"),
        "entry_high": cluster.get("entry_high"),
        "exit_bar": cluster.get("exit_bar"),
        "exit_date": cluster.get("exit_date"),
    }


def _get_loser_bars_from_clusters(lose_clusters):
    """Extract all (ticker, bar_idx) from losing clusters (rightmost + leftward)."""
    bars = []
    for c in lose_clusters:
        ticker = c["ticker"]
        bars.append((ticker, c["rightmost"]["bar_idx"]))
        for lw in c.get("leftward", []):
            bars.append((ticker, lw["bar_idx"]))
    return bars


def _build_losing_cluster_bars(lose_clusters):
    """Build list of lists: each inner list = [(ticker, bar_idx), ...] for one losing cluster."""
    result = []
    for c in lose_clusters:
        ticker = c["ticker"]
        cluster_bars = [(ticker, c["rightmost"]["bar_idx"])]
        for lw in c.get("leftward", []):
            cluster_bars.append((ticker, lw["bar_idx"]))
        result.append(cluster_bars)
    return result


def run_refinement_consensus(setup_type, threshold, input_dir):
    """Run the refinement consensus pipeline.

    Two-test validation: consensus stability + binomial significance.
    See REFINEMENT_GRINDER.md for full spec.
    """
    import numpy as np
    import time as _time
    from scipy.stats import binomtest

    t_start = _time.time()

    print("\n" + "=" * 70)
    print("  CONSENSUS ENGINE — REFINEMENT")
    print("=" * 70)
    print(f"  Setup: {setup_type.upper()}")
    print(f"  Threshold: {threshold} ({threshold:.0%})")
    print(f"  Input dir: {input_dir}")

    # ── Load refinement run JSONs ──
    print(f"\n  Loading refinement grind outputs...")
    pattern = os.path.join(input_dir, f"refinement_{setup_type}_*.json")
    ref_files = []
    for path in sorted(glob.glob(pattern)):
        try:
            with open(path) as f:
                data = json.load(f)
            conds = data.get("refinement_conditions_only", [])
            ref_files.append({"path": path, "filename": os.path.basename(path),
                              "n_conditions": len(conds)})
        except Exception as e:
            print(f"  WARNING: Could not read {os.path.basename(path)}: {e}")

    n_runs = len(ref_files)
    print(f"  Found {n_runs} refinement runs")
    if n_runs == 0:
        print(f"  ERROR: No refinement grind files found in {input_dir}")
        return None

    for rf in ref_files:
        print(f"    {rf['filename']}: {rf['n_conditions']} conditions")

    # ── Test 1: Consensus stability ──
    print(f"\n  Test 1: Consensus stability...")
    run_condition_sets = []
    condition_dicts = {}  # name -> full condition dict from any run
    for rf in ref_files:
        with open(rf["path"]) as f:
            data = json.load(f)
        conds = data.get("refinement_conditions_only", [])
        names = set()
        for c in conds:
            name = c.get("name", c.get("expr", ""))
            names.add(name)
            if name not in condition_dicts:
                condition_dicts[name] = c
        run_condition_sets.append(names)

    freq = Counter()
    for name_set in run_condition_sets:
        for name in name_set:
            freq[name] += 1

    min_appearances = max(1, int(n_runs * threshold))
    test1_survivors = set(
        name for name, count in freq.items() if count >= min_appearances
    )

    print(f"  Unique conditions across all runs: {len(freq)}")
    print(f"  Consensus threshold: {min_appearances}/{n_runs} ({threshold:.0%})")
    print(f"  Conditions passing Test 1: {len(test1_survivors)}")

    # Frequency distribution
    freq_dist = Counter(freq.values())
    for freq_val in sorted(freq_dist.keys(), reverse=True):
        count = freq_dist[freq_val]
        bar = "█" * count
        marker = " ← threshold" if freq_val == min_appearances else ""
        print(f"    {freq_val:>3}/{n_runs}: {count:>3} conditions  {bar}{marker}")

    if not test1_survivors:
        print(f"  No conditions passed Test 1. Skipping refinement.")
        # Still produce output — empty refinement
        return _build_refinement_output(
            setup_type, [], input_dir, t_start, threshold, n_runs)

    # ── Test 2: Binomial significance ──
    print(f"\n  Test 2: Binomial significance (p < 0.01)...")

    # Load expression cache
    sys.path.insert(0, LOCAL_DIR)
    from pyramid_grinder import ExprSeriesCache
    expr_cache = ExprSeriesCache()
    if not expr_cache.is_valid():
        print(f"  ERROR: Expression cache not found or invalid.")
        return None
    print(f"  Expression cache: {expr_cache.n_expressions} expressions")

    # Load cluster file for loser bars
    cluster_path = os.path.join(CACHE_DIR, f"raw_signal_clusters_{setup_type}.json")
    if not os.path.exists(cluster_path):
        print(f"  ERROR: Cluster file not found: {cluster_path}")
        return None
    with open(cluster_path) as f:
        cluster_data = json.load(f)
    clusters = cluster_data.get("clusters", [])
    lose_clusters = [c for c in clusters if c["classification"] == "AUTO_LOSS"]
    loser_bars = _get_loser_bars_from_clusters(lose_clusters)
    print(f"  Loser bars for binomial test: {len(loser_bars)} "
          f"(from {len(lose_clusters)} losing clusters)")

    cache_name_to_idx = dict(expr_cache._expr_name_to_idx)

    # Map conditions to cache column indices
    cond_names_sorted = sorted(test1_survivors)
    cond_col_indices = []
    cond_bounds = []
    valid_cond_names = []
    for name in cond_names_sorted:
        col_idx = cache_name_to_idx.get(name)
        if col_idx is None:
            print(f"    {name}: not in expr cache — SKIP")
            continue
        c = condition_dicts[name]
        cond_col_indices.append(col_idx)
        cond_bounds.append((c["low"], c["high"]))
        valid_cond_names.append(name)

    n_conds = len(valid_cond_names)
    if n_conds == 0:
        print(f"  No conditions have valid expr cache columns.")
        return _build_refinement_output(
            setup_type, [], input_dir, t_start, threshold, n_runs)

    col_idx_arr = np.array(cond_col_indices)
    lows = np.array([b[0] for b in cond_bounds], dtype=np.float64)
    highs = np.array([b[1] for b in cond_bounds], dtype=np.float64)

    # Single pass over expr cache: compute universe baseline for all conditions
    print(f"  Computing universe baseline for {n_conds} conditions (single pass)...")
    uni_n_total = np.zeros(n_conds, dtype=np.int64)
    uni_n_inside = np.zeros(n_conds, dtype=np.int64)
    available_tickers = sorted(expr_cache.get_available_tickers())
    for ti, ticker in enumerate(available_tickers):
        dates, data = expr_cache.get_ticker(ticker)
        if data is None:
            continue
        # Extract columns for all conditions at once
        cols = data[:, col_idx_arr]  # shape: (n_bars, n_conds)
        valid = ~np.isnan(cols)
        uni_n_total += valid.sum(axis=0).astype(np.int64)
        inside = valid & (cols >= lows) & (cols <= highs)
        uni_n_inside += inside.sum(axis=0).astype(np.int64)
        if (ti + 1) % 500 == 0:
            print(f"    {ti+1}/{len(available_tickers)} tickers...")

    print(f"  Universe baseline computed ({len(available_tickers)} tickers)")

    # Single pass over loser bars: compute loser exclusion for all conditions
    print(f"  Computing loser exclusion rates...")
    loser_n_total = np.zeros(n_conds, dtype=np.int64)
    loser_n_outside = np.zeros(n_conds, dtype=np.int64)

    # Group loser bars by ticker to load each ticker once
    loser_bars_by_ticker = defaultdict(list)
    for ticker, bar_idx in loser_bars:
        loser_bars_by_ticker[ticker].append(bar_idx)

    for ticker, bar_indices in loser_bars_by_ticker.items():
        dates, data = expr_cache.get_ticker(ticker)
        if data is None:
            continue
        for bar_idx in bar_indices:
            if bar_idx >= len(data):
                continue
            vals = data[bar_idx, col_idx_arr]  # shape: (n_conds,)
            valid = ~np.isnan(vals)
            loser_n_total += valid.astype(np.int64)
            outside = valid & ((vals < lows) | (vals > highs))
            loser_n_outside += outside.astype(np.int64)

    # Run binomial test for each condition
    test2_survivors = set()
    test2_results = {}

    for i, name in enumerate(valid_cond_names):
        if uni_n_total[i] == 0:
            print(f"    {name}: no valid universe data — SKIP")
            continue
        F = uni_n_inside[i] / uni_n_total[i]
        expected_exclusion = 1.0 - F

        if loser_n_total[i] == 0:
            print(f"    {name}: no valid loser data — SKIP")
            continue
        observed_exclusion = loser_n_outside[i] / loser_n_total[i]

        result = binomtest(int(loser_n_outside[i]), int(loser_n_total[i]),
                           expected_exclusion, alternative="greater")
        p_value = result.pvalue

        test2_results[name] = {
            "F_universe": round(float(F), 4),
            "expected_exclusion": round(float(expected_exclusion), 4),
            "observed_exclusion": round(float(observed_exclusion), 4),
            "n_loser_bars": int(loser_n_total[i]),
            "n_outside": int(loser_n_outside[i]),
            "p_value": float(p_value),
        }

        if p_value < 0.01:
            test2_survivors.add(name)
            print(f"    ✓ {name}: obs={observed_exclusion:.3f} vs exp={expected_exclusion:.3f} "
                  f"p={p_value:.4f}")
        else:
            print(f"    ✗ {name}: obs={observed_exclusion:.3f} vs exp={expected_exclusion:.3f} "
                  f"p={p_value:.4f} — geometric, not targeted")

    # Final survivors = passed BOTH tests
    survivors = test1_survivors & test2_survivors
    print(f"\n  Conditions passing both tests: {len(survivors)}")
    print(f"    Test 1 (consensus): {len(test1_survivors)}")
    print(f"    Test 2 (binomial):  {len(test2_survivors)}")
    print(f"    Both:               {len(survivors)}")

    # Build surviving condition list
    surviving_conditions = []
    for name in sorted(survivors):
        surviving_conditions.append(condition_dicts[name])

    return _build_refinement_output(
        setup_type, surviving_conditions, input_dir, t_start, threshold, n_runs,
        test2_results=test2_results, freq=freq)


def _build_refinement_output(setup_type, surviving_conditions, input_dir,
                              t_start, threshold, n_runs,
                              test2_results=None, freq=None):
    """Build the final refinement consensus output JSON."""
    import numpy as np
    import time as _time
    from collections import Counter as _Counter

    total_time = _time.time() - t_start

    # Load cluster file
    cluster_path = os.path.join(CACHE_DIR, f"raw_signal_clusters_{setup_type}.json")
    with open(cluster_path) as f:
        cluster_data = json.load(f)
    clusters = cluster_data.get("clusters", [])
    win_clusters = [c for c in clusters if c["classification"] == "AUTO_WIN"]
    lose_clusters = [c for c in clusters if c["classification"] == "AUTO_LOSS"]

    # Load signal conditions from consensus file
    consensus_path = os.path.join(CACHE_DIR, f"consensus_signal_{setup_type}.json")
    signal_conditions = []
    if os.path.exists(consensus_path):
        with open(consensus_path) as f:
            signal_conditions = json.load(f).get("all_conditions", [])
        print(f"\n  Signal conditions: {len(signal_conditions)} from {os.path.basename(consensus_path)}")
    else:
        print(f"\n  WARNING: No consensus signal file found at {consensus_path}")

    # Load exit condition
    exit_path = os.path.join(
        REPO_ROOT, "data", "signal_exit_grind", f"signal_exit_{setup_type}.json")
    exit_condition = None
    if os.path.exists(exit_path):
        with open(exit_path) as f:
            exit_data = json.load(f)
        if exit_data.get("top_conditions"):
            tc = exit_data["top_conditions"][0]
            exit_condition = {
                "expression": tc["expression"],
                "threshold": tc["threshold"],
                "direction": tc["direction"],
            }
        print(f"  Exit condition: {exit_condition['expression'] if exit_condition else 'NONE'}")

    # ── Order conditions by individual filter power against FULL loser pile ──
    if surviving_conditions:
        sys.path.insert(0, LOCAL_DIR)
        from pyramid_grinder import ExprSeriesCache
        expr_cache = ExprSeriesCache()
        cache_name_to_idx = dict(expr_cache._expr_name_to_idx)

        losing_cluster_bars = _build_losing_cluster_bars(lose_clusters)

        # For each condition, count how many clusters it eliminates solo
        cond_power = []
        for cond in surviving_conditions:
            name = cond["name"]
            low, high = cond["low"], cond["high"]
            col_idx = cache_name_to_idx.get(name)
            if col_idx is None:
                cond_power.append((cond, 0))
                continue
            eliminated = 0
            for cluster_bars in losing_cluster_bars:
                all_outside = True
                for ticker, bar_idx in cluster_bars:
                    dates, data = expr_cache.get_ticker(ticker)
                    if data is None or bar_idx >= len(data):
                        continue
                    val = data[bar_idx, col_idx]
                    if np.isnan(val) or (val >= low and val <= high):
                        all_outside = False
                        break
                if all_outside:
                    eliminated += 1
            cond_power.append((cond, eliminated))

        # Sort by power descending
        cond_power.sort(key=lambda x: x[1], reverse=True)
        surviving_conditions = [cp[0] for cp in cond_power]

        print(f"\n  Conditions ordered by filter power:")
        for cond, power in cond_power:
            print(f"    {cond['name']:<50s} eliminates {power}/{len(losing_cluster_bars)}")

        # ── Build depth progression ──
        print(f"\n  Building depth progression...")
        depth_progression = []
        active_conditions = []
        for i, cond in enumerate(surviving_conditions):
            active_conditions.append(cond)
            # Replay active conditions against full loser pile
            surviving_count = 0
            eliminated_count = 0
            for cluster_bars in losing_cluster_bars:
                cluster_alive = False
                for ticker, bar_idx in cluster_bars:
                    bar_passes_all = True
                    for ac in active_conditions:
                        col_idx = cache_name_to_idx.get(ac["name"])
                        if col_idx is None:
                            continue
                        dates, data = expr_cache.get_ticker(ticker)
                        if data is None or bar_idx >= len(data):
                            bar_passes_all = False
                            break
                        val = data[bar_idx, col_idx]
                        if np.isnan(val) or val < ac["low"] or val > ac["high"]:
                            bar_passes_all = False
                            break
                    if bar_passes_all:
                        cluster_alive = True
                        break
                if cluster_alive:
                    surviving_count += 1
                else:
                    eliminated_count += 1

            n_winners = len(win_clusters)
            total_signals = n_winners + surviving_count
            wr = n_winners / total_signals if total_signals > 0 else 0

            depth_progression.append({
                "depth": i + 1,
                "conditions": [{"name": ac["name"], "low": ac["low"],
                                "high": ac["high"],
                                "category": ac.get("category", "")}
                               for ac in active_conditions],
                "losing_clusters_surviving": surviving_count,
                "losing_clusters_eliminated": eliminated_count,
                "winners": n_winners,
                "total_signals": total_signals,
                "wr": round(wr, 4),
            })

            print(f"    Depth {i+1}: {eliminated_count} eliminated, "
                  f"WR {wr:.1%}")

        # Final state from last depth level
        final_surviving = depth_progression[-1]["losing_clusters_surviving"]
        final_eliminated = depth_progression[-1]["losing_clusters_eliminated"]
    else:
        depth_progression = []
        final_surviving = len(lose_clusters)
        final_eliminated = 0
        losing_cluster_bars = _build_losing_cluster_bars(lose_clusters)

    # ── Replay to determine eliminated vs surviving clusters ──
    eliminated_cluster_set = set()
    if surviving_conditions:
        for ci, cluster_bars in enumerate(losing_cluster_bars):
            cluster_alive = False
            for ticker, bar_idx in cluster_bars:
                bar_passes_all = True
                for cond in surviving_conditions:
                    col_idx = cache_name_to_idx.get(cond["name"])
                    if col_idx is None:
                        continue
                    dates, data = expr_cache.get_ticker(ticker)
                    if data is None or bar_idx >= len(data):
                        bar_passes_all = False
                        break
                    val = data[bar_idx, col_idx]
                    if np.isnan(val) or val < cond["low"] or val > cond["high"]:
                        bar_passes_all = False
                        break
                if bar_passes_all:
                    cluster_alive = True
                    break
            if not cluster_alive:
                eliminated_cluster_set.add(ci)

    # ── Build signal lists ──
    winner_signals = [_cluster_to_signal(c) for c in win_clusters]
    loser_signals = []
    eliminated_signals = []
    for ci, c in enumerate(lose_clusters):
        sig = _cluster_to_signal(c)
        if ci in eliminated_cluster_set:
            eliminated_signals.append(sig)
        else:
            loser_signals.append(sig)

    print(f"\n  Signal lists:")
    print(f"    Winners: {len(winner_signals)}")
    print(f"    Losers surviving: {len(loser_signals)}")
    print(f"    Losers eliminated: {len(eliminated_signals)}")

    # ── Compute peak/day from surviving signals ──
    all_surviving = winner_signals + loser_signals
    if all_surviving:
        from collections import Counter as _Counter
        date_counts = _Counter(s.get("signal_date", "") for s in all_surviving)
        final_peak = max(date_counts.values()) if date_counts else 0
        final_avg = sum(date_counts.values()) / len(date_counts) if date_counts else 0
    else:
        final_peak = 0
        final_avg = 0

    # ── Merge signal + refinement conditions ──
    combined_conditions = list(signal_conditions)
    if surviving_conditions:
        sig_names = {c["name"] for c in signal_conditions}
        for rc in surviving_conditions:
            if rc["name"] in sig_names:
                combined_conditions = [c for c in combined_conditions
                                       if c["name"] != rc["name"]]
            combined_conditions.append(rc)

    # ── Build output ──
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")

    output = {
        "setup_type": setup_type,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "total_time_s": round(total_time, 1),
        "refinement": True,
        "n_conditions": len(combined_conditions),
        "all_conditions": combined_conditions,
        "refinement_conditions_only": surviving_conditions,
        "signal_conditions": signal_conditions,
        "exit_condition": exit_condition,
        "params": {
            "beam_width": None,
            "depth": None,
            "peak_target": None,
            "source": "refinement_consensus",
            "threshold": threshold,
            "n_runs": n_runs,
        },
        "summary": {
            "losing_clusters_input": len(lose_clusters),
            "losing_clusters_eliminated": final_eliminated,
            "losing_clusters_surviving": final_surviving,
            "final_peak": final_peak,
            "final_avg": round(final_avg, 1) if final_avg else 0,
            "winners_input": len(win_clusters),
            "winners_passing": len(win_clusters),
        },
        "winner_signals": winner_signals,
        "loser_signals": loser_signals,
        "eliminated_signals": eliminated_signals,
        "depth_progression": depth_progression,
    }

    # Save to standard cache directory
    os.makedirs(CACHE_DIR, exist_ok=True)
    desc = f"refinement_{setup_type}_cl{final_surviving}_pk{final_peak}_consensus_{ts}"
    out_path = os.path.join(CACHE_DIR, f"{desc}.json")
    with open(out_path, "w") as f:
        json.dump(output, f, indent=2)
    print(f"\n  Saved: {out_path}")

    # Summary
    print(f"\n  {'='*50}")
    if surviving_conditions:
        print(f"  ✓ REFINEMENT CONSENSUS: {len(surviving_conditions)} conditions")
        print(f"    Eliminated: {final_eliminated}/{len(lose_clusters)} clusters")
        print(f"    WR: {len(win_clusters)}/{len(win_clusters)+final_surviving} "
              f"= {len(win_clusters)/(len(win_clusters)+final_surviving):.1%}")
    else:
        print(f"  ⚠ No refinement conditions survived both tests.")
        print(f"    Proceeding with unrefined signal population.")
    print(f"  {'='*50}")

    return output


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Consensus Engine — Multi-Run Stability Selection + Permutation Testing")
    parser.add_argument("--setup", default="dtss", help="Setup type (default: dtss)")
    parser.add_argument("--stage", required=True, choices=["signal", "refinement"],
                        help="Which grind stage to analyze")
    parser.add_argument("--threshold", type=float, default=0.7,
                        help="Consensus frequency threshold 0.0-1.0 (default: 0.7)")
    parser.add_argument("--input-dir", type=str, required=True,
                        help="Directory containing real + permuted grind outputs")
    args = parser.parse_args()

    if args.stage == "signal":
        result = run_signal_consensus(
            setup_type=args.setup,
            threshold=args.threshold,
            input_dir=args.input_dir,
        )
        if result is None:
            sys.exit(1)
    elif args.stage == "refinement":
        result = run_refinement_consensus(
            setup_type=args.setup,
            threshold=args.threshold,
            input_dir=args.input_dir,
        )
        if result is None:
            sys.exit(1)
