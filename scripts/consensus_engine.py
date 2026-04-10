"""
Consensus Engine — Multi-Run Stability Selection for Signal & Refinement Conditions.

Signal mode (Inc 8): Reads real + permuted run outputs from --input-dir.
Computes z-score comparing real vs permuted condition counts.
Locks conditions appearing in >= threshold fraction of real runs,
applies 5% margin on bounds.

Refinement mode (Inc 9): Reads refinement run outputs, applies consensus
threshold + binomial test. (Not yet rewritten — uses legacy logic.)

Based on Meinshausen & Buhlmann (2010) stability selection framework.
Permutation test replaces EPV formula — measures empirical noise floor.

Usage:
    python scripts/consensus_engine.py --setup dtss --stage signal \\
        --threshold 0.7 --input-dir local_runner/cache/consensus/run1/
    python scripts/consensus_engine.py --setup dtss --stage refinement --threshold 0.7
    python scripts/consensus_engine.py --setup dtss --stage signal --list-runs
"""

import os
import sys
import json
import glob
import math
import argparse
from datetime import datetime, timezone
from collections import Counter, defaultdict

# Force UTF-8 output on Windows
if sys.platform == 'win32':
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOCAL_DIR = os.path.join(REPO_ROOT, "local_runner")
CACHE_DIR = os.path.join(LOCAL_DIR, "cache")

# Detect worktree — resolve CACHE_DIR to main repo
_claude_marker = os.sep + ".claude" + os.sep
if _claude_marker in REPO_ROOT:
    _MAIN_REPO = REPO_ROOT[:REPO_ROOT.index(_claude_marker)]
    CACHE_DIR = os.environ.get(
        "SCANPERFECT_CACHE_DIR",
        os.path.join(_MAIN_REPO, "local_runner", "cache"),
    )


# ══════════════════════════════════════════════════════════════
# FILE DISCOVERY
# ══════════════════════════════════════════════════════════════

def _find_grind_files(search_dir, prefix, setup_type, skip_refinement=False):
    """Find grind output JSONs matching prefix_setup_*.json in search_dir."""
    pattern = os.path.join(search_dir, f"{prefix}_{setup_type}_*.json")
    candidates = []
    for path in glob.glob(pattern):
        try:
            with open(path) as f:
                data = json.load(f)
            if skip_refinement and data.get("refinement"):
                continue
            n_conds = data.get("n_conditions", len(data.get("all_conditions", [])))
            if n_conds == 0:
                continue
            candidates.append({
                "path": path,
                "filename": os.path.basename(path),
                "timestamp": data.get("timestamp", ""),
                "n_conditions": n_conds,
                "total_time_s": data.get("total_time_s", 0),
                "summary": data.get("summary", {}),
                "params": data.get("params", {}),
                "mtime": os.path.getmtime(path),
            })
        except Exception as e:
            print(f"  WARNING: Could not read {os.path.basename(path)}: {e}")
    candidates.sort(key=lambda c: c["mtime"], reverse=True)
    return candidates


def find_signal_grind_files(setup_type, search_dir=None, limit=None):
    """Find pyramid (real) grind outputs for a setup."""
    d = search_dir or CACHE_DIR
    files = _find_grind_files(d, "pyramid", setup_type, skip_refinement=True)
    return files[:limit] if limit else files


def find_permuted_grind_files(setup_type, search_dir=None, limit=None):
    """Find permuted grind outputs for a setup."""
    d = search_dir or CACHE_DIR
    files = _find_grind_files(d, "permuted", setup_type)
    return files[:limit] if limit else files


def find_refinement_grind_files(setup_type, search_dir=None, limit=None):
    """Find refinement grind outputs for a setup."""
    d = search_dir or CACHE_DIR
    pattern = os.path.join(d, f"refinement_{setup_type}_*.json")
    candidates = []
    for path in glob.glob(pattern):
        try:
            with open(path) as f:
                data = json.load(f)
            n_conds = len(data.get("refinement_conditions_only", []))
            if n_conds == 0:
                continue
            candidates.append({
                "path": path,
                "filename": os.path.basename(path),
                "timestamp": data.get("timestamp", ""),
                "n_conditions": n_conds,
                "total_time_s": data.get("total_time_s", 0),
                "summary": data.get("summary", {}),
                "mtime": os.path.getmtime(path),
            })
        except Exception as e:
            print(f"  WARNING: Could not read {os.path.basename(path)}: {e}")
    candidates.sort(key=lambda c: c["mtime"], reverse=True)
    return candidates[:limit] if limit else candidates


# ══════════════════════════════════════════════════════════════
# CONDITION EXTRACTION
# ══════════════════════════════════════════════════════════════

def extract_signal_conditions(path):
    """Extract condition names + bounds from a signal grind output."""
    with open(path) as f:
        data = json.load(f)
    conditions = data.get("all_conditions", [])
    result = []
    for c in conditions:
        result.append({
            "name": c.get("name", c.get("expr", "")),
            "low": c.get("low"),
            "high": c.get("high"),
            "category": c.get("category", ""),
            "tier": c.get("tier", ""),
            "compute": c.get("compute", ""),
            "filter_power": c.get("filter_power"),
        })
    return result


def extract_refinement_conditions(path):
    """Extract condition names + bounds from a refinement grind output."""
    with open(path) as f:
        data = json.load(f)
    conditions = data.get("refinement_conditions_only", [])
    result = []
    for c in conditions:
        result.append({
            "name": c.get("name", c.get("expr", "")),
            "low": c.get("low"),
            "high": c.get("high"),
            "category": c.get("category", ""),
            "tier": c.get("tier", ""),
            "compute": c.get("compute", ""),
            "filter_power": c.get("filter_power"),
        })
    return result


# ══════════════════════════════════════════════════════════════
# CONSENSUS COMPUTATION
# ══════════════════════════════════════════════════════════════

def compute_consensus(all_run_conditions, n_runs, threshold=0.7, max_conditions=None):
    """Compute consensus across N runs.

    Matches conditions by expression name (exact match).

    Args:
        all_run_conditions: list of lists, one per run, each containing condition dicts
        n_runs: total number of runs
        threshold: minimum selection frequency (0.0-1.0), default 0.7
        max_conditions: maximum conditions to keep (None = no cap)

    Returns:
        dict with consensus results
    """
    condition_freq = Counter()
    condition_runs = defaultdict(list)
    condition_examples = {}

    for run_i, conditions in enumerate(all_run_conditions):
        seen_this_run = set()
        for c in conditions:
            name = c["name"]
            if name not in seen_this_run:
                condition_freq[name] += 1
                seen_this_run.add(name)
            condition_runs[name].append(run_i)
            condition_examples[name] = c

    all_names = sorted(condition_freq.keys(), key=lambda n: condition_freq[n], reverse=True)
    n_unique = len(all_names)

    min_appearances = max(1, int(n_runs * threshold))
    consensus_names = [name for name in all_names if condition_freq[name] >= min_appearances]

    if max_conditions is not None:
        capped_names = consensus_names[:max_conditions]
    else:
        capped_names = consensus_names

    consensus_conditions = []
    for name in capped_names:
        c = condition_examples[name]
        consensus_conditions.append({
            "name": name,
            "frequency": condition_freq[name],
            "frequency_pct": round(condition_freq[name] / n_runs, 3),
            "runs_appeared": sorted(set(condition_runs[name])),
            "category": c.get("category", ""),
            "tier": c.get("tier", ""),
            "compute": c.get("compute", ""),
            "ref_low": c.get("low"),
            "ref_high": c.get("high"),
        })

    # Pairwise Jaccard overlap
    pairwise_overlaps = []
    for i in range(len(all_run_conditions)):
        names_i = set(c["name"] for c in all_run_conditions[i])
        for j in range(i + 1, len(all_run_conditions)):
            names_j = set(c["name"] for c in all_run_conditions[j])
            union = names_i | names_j
            inter = names_i & names_j
            if union:
                pairwise_overlaps.append(len(inter) / len(union))

    avg_overlap = sum(pairwise_overlaps) / len(pairwise_overlaps) if pairwise_overlaps else 0
    freq_dist = Counter(condition_freq.values())

    return {
        "n_runs": n_runs,
        "threshold": threshold,
        "min_appearances": min_appearances,
        "max_conditions_cap": max_conditions,
        "n_unique_conditions": n_unique,
        "n_above_threshold": len(consensus_names),
        "n_after_cap": len(capped_names),
        "consensus_conditions": consensus_conditions,
        "stability_metrics": {
            "avg_pairwise_overlap": round(avg_overlap, 4),
            "avg_conditions_per_run": round(
                sum(len(rc) for rc in all_run_conditions) / max(n_runs, 1), 1),
            "frequency_distribution": {
                str(k): v for k, v in sorted(freq_dist.items(), reverse=True)
            },
        },
        "all_conditions_ranked": [
            {
                "name": name,
                "frequency": condition_freq[name],
                "frequency_pct": round(condition_freq[name] / n_runs, 3),
                "above_threshold": condition_freq[name] >= min_appearances,
            }
            for name in all_names
        ],
    }


def compute_z_score(real_counts, permuted_counts):
    """Compute z-score comparing real vs permuted condition counts.

    z = (mean_R - mean_P) / std_P

    Args:
        real_counts: list of condition counts from real runs
        permuted_counts: list of condition counts from permuted runs

    Returns:
        (z_score, mean_real, mean_permuted, std_permuted)
    """
    mean_r = sum(real_counts) / len(real_counts) if real_counts else 0
    mean_p = sum(permuted_counts) / len(permuted_counts) if permuted_counts else 0

    if len(permuted_counts) < 2:
        std_p = 0.0
    else:
        variance = sum((x - mean_p) ** 2 for x in permuted_counts) / (len(permuted_counts) - 1)
        std_p = math.sqrt(variance)

    if std_p == 0:
        z = float('inf') if mean_r > mean_p else 0.0
    else:
        z = (mean_r - mean_p) / std_p

    return z, mean_r, mean_p, std_p


def apply_margin(conditions, margin_pct=0.05):
    """Apply margin to condition bounds (widen low/high by margin_pct).

    Phase E: Deep-copy each condition dict, widen bounds outward by margin.
    low gets lower (multiplied by 1-margin or 1+margin depending on sign),
    high gets higher (multiplied by 1+margin or 1-margin depending on sign).

    Returns new list of condition dicts with widened bounds.
    """
    result = []
    for c in conditions:
        c_copy = dict(c)
        low = c_copy.get("low") or c_copy.get("ref_low")
        high = c_copy.get("high") or c_copy.get("ref_high")

        if low is not None and high is not None:
            span = high - low
            if span > 0:
                margin = span * margin_pct
            else:
                margin = abs(low) * margin_pct if low != 0 else margin_pct
            c_copy["low"] = low - margin
            c_copy["high"] = high + margin

        result.append(c_copy)
    return result


# ══════════════════════════════════════════════════════════════
# SIGNAL MODE (Inc 8 — permutation test based)
# ══════════════════════════════════════════════════════════════

def run_signal(setup_type, threshold=0.7, input_dir=None):
    """Run signal consensus with permutation test z-score.

    Reads real (pyramid_*.json) and permuted (permuted_*.json) files from
    input_dir. Computes z-score and locks consensus conditions with 5% margin.

    Args:
        setup_type: e.g. "dtss"
        threshold: consensus frequency threshold (0.0-1.0)
        input_dir: directory containing real + permuted grind outputs

    Returns:
        dict with consensus output, or None on error
    """
    search_dir = input_dir or CACHE_DIR

    print("\n" + "=" * 70)
    print("  CONSENSUS ENGINE — SIGNAL MODE")
    print("=" * 70)
    print(f"  Setup: {setup_type.upper()}")
    print(f"  Threshold: {threshold} ({threshold:.0%})")
    print(f"  Input dir: {search_dir}")

    # Find real + permuted files
    real_files = find_signal_grind_files(setup_type, search_dir=search_dir)
    perm_files = find_permuted_grind_files(setup_type, search_dir=search_dir)

    n_real = len(real_files)
    n_perm = len(perm_files)
    print(f"\n  Real runs: {n_real}")
    print(f"  Permuted runs: {n_perm}")

    if n_real < 1:
        print(f"  ERROR: Need at least 1 real run. Found {n_real}.")
        return None

    # List files
    print(f"\n  Real runs:")
    for i, rf in enumerate(real_files):
        print(f"    {i+1:>3}. {rf['filename']:<55s} {rf['n_conditions']:>3} conds")
    if perm_files:
        print(f"\n  Permuted runs:")
        for i, rf in enumerate(perm_files):
            print(f"    {i+1:>3}. {rf['filename']:<55s} {rf['n_conditions']:>3} conds")

    # Extract conditions from each run
    print(f"\n  Extracting conditions...")
    real_conditions = []
    for rf in real_files:
        conds = extract_signal_conditions(rf["path"])
        real_conditions.append(conds)

    perm_conditions = []
    for rf in perm_files:
        conds = extract_signal_conditions(rf["path"])
        perm_conditions.append(conds)

    # Z-score: compare condition counts (real vs permuted)
    real_counts = [len(c) for c in real_conditions]
    perm_counts = [len(c) for c in perm_conditions]

    z_score, mean_r, mean_p, std_p = compute_z_score(real_counts, perm_counts)

    print(f"\n  ══ PERMUTATION TEST ══")
    print(f"  Real: mean={mean_r:.1f} conditions (counts: {real_counts})")
    if perm_counts:
        print(f"  Permuted: mean={mean_p:.1f}, std={std_p:.1f} (counts: {perm_counts})")
    else:
        print(f"  Permuted: no permuted runs")
    print(f"  z-score: {z_score:.2f}")

    if z_score == float('inf'):
        print(f"  z = inf (std_P = 0 — permuted runs all same count, or < 2 permuted runs)")
    elif z_score >= 3:
        print(f"  z >= 3: PASS (99.7% confidence real > noise)")
    elif z_score >= 2:
        print(f"  z >= 2: MARGINAL (95% confidence)")
    else:
        print(f"  z < 2: FAIL (real not clearly above noise)")

    # Compute consensus on real runs
    consensus = compute_consensus(real_conditions, n_real, threshold=threshold)

    # Print consensus results
    cc = consensus["consensus_conditions"]
    print(f"\n  ══ CONSENSUS RESULTS ══")
    print(f"  Unique conditions across real runs: {consensus['n_unique_conditions']}")
    print(f"  Above {threshold:.0%} threshold: {consensus['n_above_threshold']}")

    sm = consensus["stability_metrics"]
    print(f"\n  Stability Metrics:")
    print(f"    Avg pairwise overlap (Jaccard): {sm['avg_pairwise_overlap']:.1%}")
    print(f"    Avg conditions per run: {sm['avg_conditions_per_run']:.0f}")

    # Frequency distribution
    print(f"\n  Frequency Distribution:")
    fd = sm["frequency_distribution"]
    for freq, count in sorted(fd.items(), key=lambda x: int(x[0]), reverse=True):
        bar = "=" * count
        marker = " <- threshold" if int(freq) == consensus["min_appearances"] else ""
        print(f"    {freq:>3}/{n_real}: {count:>3} conditions  {bar}{marker}")

    if cc:
        print(f"\n  Consensus Conditions ({len(cc)}):")
        print(f"    {'#':>3} {'Freq':>5} {'%':>5}  {'Name':<50s} {'Tier':<6}")
        print(f"    {'-'*3} {'-'*5} {'-'*5}  {'-'*50} {'-'*6}")
        for i, c in enumerate(cc):
            print(f"    {i+1:>3} {c['frequency']:>3}/{n_real} "
                  f"{c['frequency_pct']:>4.0%}  {c['name']:<50s} {c.get('tier',''):<6}")
    else:
        print(f"\n  No conditions survived consensus.")

    # Phase E: Lock conditions with 5% margin
    locked_conditions = apply_margin(cc, margin_pct=0.05)
    print(f"\n  Locked {len(locked_conditions)} conditions with 5% margin")

    # Build output
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    output = {
        "setup_type": setup_type,
        "stage": "signal",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "n_real_runs": n_real,
        "n_permuted_runs": n_perm,
        "threshold": threshold,
        "z_score": z_score if z_score != float('inf') else "inf",
        "permutation_test": {
            "mean_real": round(mean_r, 2),
            "mean_permuted": round(mean_p, 2),
            "std_permuted": round(std_p, 2),
            "real_counts": real_counts,
            "permuted_counts": perm_counts,
        },
        "all_conditions": locked_conditions,
        "consensus": consensus,
        "run_files": {
            "real": [rf["filename"] for rf in real_files],
            "permuted": [rf["filename"] for rf in perm_files],
        },
        "stability_metrics": sm,
    }

    # Save to CACHE_DIR (not input_dir — this is the final consensus output)
    save_dir = CACHE_DIR
    os.makedirs(save_dir, exist_ok=True)
    out_path = os.path.join(save_dir, f"consensus_signal_{setup_type}_{ts}.json")
    with open(out_path, "w") as f:
        json.dump(output, f, indent=2)
    print(f"\n  Saved: {out_path}")

    # Latest pointer
    import shutil
    latest_path = os.path.join(save_dir, f"consensus_signal_{setup_type}.json")
    shutil.copy2(out_path, latest_path)
    print(f"  Latest: {latest_path}")

    print(f"\n  {'='*50}")
    if locked_conditions:
        print(f"  CONSENSUS: {len(locked_conditions)} conditions locked (z={z_score:.2f})")
        print(f"    Stability: {sm['avg_pairwise_overlap']:.1%} avg overlap")
    else:
        print(f"  CONSENSUS FAILED: No conditions survived")
    print(f"  {'='*50}")

    return output


# ══════════════════════════════════════════════════════════════
# REFINEMENT MODE (legacy — Inc 9 will rewrite)
# ══════════════════════════════════════════════════════════════

def run_refinement(setup_type, threshold=0.7, input_dir=None):
    """Run refinement consensus (legacy, reads from CACHE_DIR or input_dir)."""
    search_dir = input_dir or CACHE_DIR
    run_files = find_refinement_grind_files(setup_type, search_dir=search_dir)
    n_runs = len(run_files)

    print("\n" + "=" * 70)
    print("  CONSENSUS ENGINE — REFINEMENT MODE")
    print("=" * 70)
    print(f"  Setup: {setup_type.upper()}")
    print(f"  Threshold: {threshold} ({threshold:.0%})")
    print(f"  Found {n_runs} refinement grind outputs")

    if n_runs < 1:
        print(f"  ERROR: Need at least 1 refinement run. Found {n_runs}.")
        return None

    for i, rf in enumerate(run_files):
        print(f"    {i+1:>3}. {rf['filename']:<55s} {rf['n_conditions']:>3} conds")

    all_run_conditions = []
    for rf in run_files:
        conds = extract_refinement_conditions(rf["path"])
        all_run_conditions.append(conds)

    consensus = compute_consensus(all_run_conditions, n_runs, threshold=threshold)
    cc = consensus["consensus_conditions"]

    print(f"\n  Consensus: {len(cc)} conditions locked")

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    output = {
        "setup_type": setup_type,
        "stage": "refinement",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "n_runs": n_runs,
        "threshold": threshold,
        "all_conditions": apply_margin(cc, margin_pct=0.05),
        "consensus": consensus,
        "run_files": [rf["filename"] for rf in run_files],
    }

    save_dir = CACHE_DIR
    os.makedirs(save_dir, exist_ok=True)
    out_path = os.path.join(save_dir, f"consensus_refinement_{setup_type}_{ts}.json")
    with open(out_path, "w") as f:
        json.dump(output, f, indent=2)
    print(f"  Saved: {out_path}")

    import shutil
    latest_path = os.path.join(save_dir, f"consensus_refinement_{setup_type}.json")
    shutil.copy2(out_path, latest_path)
    print(f"  Latest: {latest_path}")

    return output


# ══════════════════════════════════════════════════════════════
# DISPATCH
# ══════════════════════════════════════════════════════════════

def run(setup_type, stage, threshold=0.7, input_dir=None, **kwargs):
    """Dispatch to signal or refinement consensus."""
    if stage == "signal":
        return run_signal(setup_type, threshold=threshold, input_dir=input_dir)
    elif stage == "refinement":
        return run_refinement(setup_type, threshold=threshold, input_dir=input_dir)
    else:
        print(f"  ERROR: Unknown stage '{stage}'. Use 'signal' or 'refinement'.")
        return None


def list_runs(setup_type, stage):
    """List available grind outputs without running consensus."""
    print(f"\n  Available {stage} grind outputs for {setup_type.upper()}:")
    if stage == "signal":
        files = find_signal_grind_files(setup_type)
        perm = find_permuted_grind_files(setup_type)
        if perm:
            files = files + [{"filename": f["filename"], "n_conditions": f["n_conditions"],
                              "timestamp": f["timestamp"], "summary": f["summary"]}
                             for f in perm]
    else:
        files = find_refinement_grind_files(setup_type)

    if not files:
        print(f"  None found.")
        return

    for i, rf in enumerate(files):
        s = rf.get("summary", {})
        total = s.get("final_total", s.get("losing_clusters_eliminated", "?"))
        ts = rf.get("timestamp", "")[:19]
        print(f"  {i+1:>3}. {rf['n_conditions']:>3} conds  "
              f"{total:>6} signals  {ts}  {rf['filename']}")
    print(f"\n  Total: {len(files)} runs")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Consensus Engine — Stability Selection + Permutation Test")
    parser.add_argument("--setup", default="dtss", help="Setup type (default: dtss)")
    parser.add_argument("--stage", required=True, choices=["signal", "refinement"],
                        help="Which grind stage to analyze")
    parser.add_argument("--threshold", type=float, default=0.7,
                        help="Consensus frequency threshold 0.0-1.0 (default: 0.7)")
    parser.add_argument("--input-dir", type=str, default=None,
                        help="Directory containing grind output JSONs (default: CACHE_DIR)")
    parser.add_argument("--list-runs", action="store_true",
                        help="Just list available runs, don't compute consensus")
    args = parser.parse_args()

    if args.list_runs:
        list_runs(args.setup, args.stage)
    else:
        result = run(
            setup_type=args.setup,
            stage=args.stage,
            threshold=args.threshold,
            input_dir=args.input_dir,
        )
        if result is None:
            sys.exit(1)
