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
        print("  ERROR: Refinement consensus not yet implemented (Increment 9)")
        sys.exit(1)
