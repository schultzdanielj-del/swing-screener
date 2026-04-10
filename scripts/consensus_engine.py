"""
Consensus Engine — Multi-Run Stability Selection for Signal & Refinement Conditions.

Signal mode (Inc 8): Reads real + permuted run outputs from --input-dir.
Computes z-score comparing real vs permuted condition counts.
Locks conditions appearing in >= threshold fraction of real runs,
applies 5% margin on bounds.

Refinement mode (Inc 9): Reads refinement run outputs. Two-test validation:
Test 1 consensus stability + Test 2 per-condition binomial significance (p < 0.01).
Depth progression ordered by filter power. Full output schema.

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


def compute_z_score(real_counts, permuted_counts, n_bootstrap=1000, seed=42):
    """Bootstrap z-score comparing real vs permuted condition counts.

    Resamples real and permuted counts with replacement n_bootstrap times.
    For each draw, computes mean_R - mean_P. The z-score is the mean of
    the bootstrap differences divided by their standard deviation.

    Args:
        real_counts: list of condition counts from real runs
        permuted_counts: list of condition counts from permuted runs
        n_bootstrap: number of bootstrap draws (default 1000)
        seed: RNG seed for reproducible bootstrap

    Returns:
        (z_score, mean_real, mean_permuted, std_permuted)
    """
    import random as _rng
    bs_rng = _rng.Random(seed)

    mean_r = sum(real_counts) / len(real_counts) if real_counts else 0
    mean_p = sum(permuted_counts) / len(permuted_counts) if permuted_counts else 0

    if len(permuted_counts) < 2:
        std_p = 0.0
    else:
        variance = sum((x - mean_p) ** 2 for x in permuted_counts) / (len(permuted_counts) - 1)
        std_p = math.sqrt(variance)

    # Bootstrap: resample and compute difference of means
    if not real_counts or not permuted_counts:
        z = float('inf') if mean_r > mean_p else 0.0
        return z, mean_r, mean_p, std_p

    diffs = []
    for _ in range(n_bootstrap):
        bs_real = [bs_rng.choice(real_counts) for _ in range(len(real_counts))]
        bs_perm = [bs_rng.choice(permuted_counts) for _ in range(len(permuted_counts))]
        diff = sum(bs_real) / len(bs_real) - sum(bs_perm) / len(bs_perm)
        diffs.append(diff)

    bs_mean = sum(diffs) / len(diffs)
    if len(diffs) < 2:
        z = float('inf') if bs_mean > 0 else 0.0
    else:
        bs_var = sum((d - bs_mean) ** 2 for d in diffs) / (len(diffs) - 1)
        bs_std = math.sqrt(bs_var)
        z = bs_mean / bs_std if bs_std > 0 else (float('inf') if bs_mean > 0 else 0.0)

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

def run_signal(setup_type, threshold=0.7, input_dir=None, output_dir=None):
    """Run signal consensus with permutation test z-score.

    Reads real (pyramid_*.json) and permuted (permuted_*.json) files from
    input_dir. Computes z-score and locks consensus conditions with 5% margin.

    Args:
        setup_type: e.g. "dtss"
        threshold: consensus frequency threshold (0.0-1.0)
        input_dir: directory containing real + permuted grind outputs
        output_dir: directory to save consensus output (default: CACHE_DIR)

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

    # Phase D: Gate decision
    gate_pass = False
    if z_score == float('inf'):
        print(f"  z = inf (std_P = 0 — permuted runs all same count, or < 2 permuted runs)")
        gate_pass = mean_r > mean_p  # proceed if real clearly above permuted
    elif z_score >= 3:
        print(f"  z >= 3: GATE PASS (99.7% confidence real > noise)")
        gate_pass = True
    elif z_score >= 2:
        print(f"  z >= 2: MARGINAL (95% confidence) — gate does NOT pass")
    else:
        print(f"  z < 2: GATE FAIL (real not clearly above noise)")

    # Compute consensus on real runs (always compute for reporting)
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

    # Phase E: Lock conditions with 5% margin (only if gate passes)
    if gate_pass:
        locked_conditions = apply_margin(cc, margin_pct=0.05)
        print(f"\n  Locked {len(locked_conditions)} conditions with 5% margin")
    else:
        locked_conditions = []
        print(f"\n  GATE FAILED (z={z_score:.2f} < 3) — no conditions locked")
        print(f"  Pipeline cannot proceed. Add more examples or investigate noise.")

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
        "gate_pass": gate_pass,
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

    # Save to output_dir (or CACHE_DIR if not specified)
    save_dir = output_dir or CACHE_DIR
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
# BINOMIAL TEST (normal approximation)
# ══════════════════════════════════════════════════════════════

def _normal_cdf(x):
    """Standard normal CDF using math.erf. No scipy needed."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def binomial_test_normal(n_trials, n_successes, expected_rate):
    """One-sided binomial test via normal approximation.

    Tests whether observed success rate is significantly GREATER than expected_rate.

    Args:
        n_trials: total number of trials (loser bars)
        n_successes: observed successes (loser bars outside bounding box)
        expected_rate: baseline rate from universe (1 - F)

    Returns:
        (z_stat, p_value) — p < 0.01 means condition is significant
    """
    if n_trials < 1 or expected_rate <= 0 or expected_rate >= 1:
        return 0.0, 1.0

    observed_rate = n_successes / n_trials
    se = math.sqrt(expected_rate * (1 - expected_rate) / n_trials)

    if se < 1e-12:
        return 0.0, 1.0

    z = (observed_rate - expected_rate) / se
    p_value = 1.0 - _normal_cdf(z)
    return z, p_value


# ══════════════════════════════════════════════════════════════
# EXPRESSION CACHE ACCESS (lazy import from local_runner)
# ══════════════════════════════════════════════════════════════

def _load_expr_cache():
    """Lazy-load ExprSeriesCache. Returns cache instance or None."""
    repo_root = os.environ.get("SCANPERFECT_REPO_ROOT", REPO_ROOT)
    local_runner_dir = os.path.join(repo_root, "local_runner")
    if local_runner_dir not in sys.path:
        sys.path.insert(0, local_runner_dir)
    try:
        from expr_cache_builder import ExprSeriesCache
        cache = ExprSeriesCache()
        if not cache.is_valid():
            print("  WARNING: Expression cache not valid")
            return None
        return cache
    except Exception as e:
        print(f"  WARNING: Could not load ExprSeriesCache: {e}")
        return None


def _load_cluster_file(setup_type, cluster_file_path=None):
    """Load raw_signal_clusters JSON. Returns dict or None."""
    path = cluster_file_path or os.path.join(
        CACHE_DIR, f"raw_signal_clusters_{setup_type}.json")
    if not os.path.exists(path):
        print(f"  ERROR: Cluster file not found: {path}")
        return None
    with open(path) as f:
        data = json.load(f)
    return data


def _collect_loser_bars(cluster_data):
    """Extract all (ticker, bar_idx) pairs from losing clusters.

    "Loser bars" = all bars (rightmost + leftward) from ALL losing clusters.
    Returns list of (ticker, bar_idx) tuples and count of losing clusters.
    """
    loser_bars = []
    n_losing = 0
    for cluster in cluster_data.get("clusters", []):
        if cluster.get("classification") != "AUTO_LOSS":
            continue
        n_losing += 1
        ticker = cluster["ticker"]
        # Rightmost bar
        loser_bars.append((ticker, cluster["rightmost"]["bar_idx"]))
        # Leftward bars
        for lb in cluster.get("leftward", []):
            loser_bars.append((ticker, lb["bar_idx"]))
    return loser_bars, n_losing


def _collect_winner_loser_signals(cluster_data):
    """Extract winner and loser signal dicts from cluster file for output schema."""
    winners = []
    losers = []
    for cluster in cluster_data.get("clusters", []):
        sig = {
            "ticker": cluster["ticker"],
            "signal_date": cluster["rightmost"].get("date", ""),
            "bar_idx": cluster["rightmost"]["bar_idx"],
            "close": cluster["rightmost"].get("close"),
            "classification": cluster.get("classification", ""),
            "move_adr": cluster.get("move_adr"),
            "adr_at_signal": cluster.get("adr_at_signal"),
            "entry_high": cluster.get("entry_high"),
            "is_example": 1 if cluster.get("is_example") else 0,
            "exit_bar": cluster.get("exit_bar_idx"),
            "exit_date": cluster.get("exit_date", ""),
        }
        if cluster.get("classification") == "AUTO_WIN":
            winners.append(sig)
        elif cluster.get("classification") == "AUTO_LOSS":
            losers.append(sig)
    return winners, losers


# ══════════════════════════════════════════════════════════════
# REFINEMENT MODE (Inc 9 — consensus stability + binomial test)
# ══════════════════════════════════════════════════════════════

def run_refinement(setup_type, threshold=0.7, input_dir=None, output_dir=None,
                   cluster_file=None):
    """Run refinement consensus with two-test validation.

    Test 1 — Consensus stability: condition frequency across N runs >= threshold.
    Test 2 — Per-condition binomial significance (p < 0.01):
        For each surviving condition, compare loser exclusion rate against
        universe baseline. Conditions that don't specifically target losers
        (just geometric exclusion from narrow bounding box) are discarded.

    Depth progression: order surviving conditions by individual filter power,
    progressively apply against full loser cluster pile.

    Args:
        setup_type: e.g. "dtss"
        threshold: consensus frequency threshold (0.0-1.0)
        input_dir: directory containing refinement grind JSONs
        output_dir: directory to save consensus output (default: CACHE_DIR)
        cluster_file: path to raw_signal_clusters JSON (default: auto from CACHE_DIR)

    Returns:
        dict with consensus output, or None on error
    """
    import time as _time
    t_total = _time.time()
    search_dir = input_dir or CACHE_DIR

    print("\n" + "=" * 70)
    print("  CONSENSUS ENGINE — REFINEMENT MODE (Inc 9)")
    print("=" * 70)
    print(f"  Setup: {setup_type.upper()}")
    print(f"  Threshold: {threshold} ({threshold:.0%})")
    print(f"  Input dir: {search_dir}")

    # ── Find refinement run files ──
    run_files = find_refinement_grind_files(setup_type, search_dir=search_dir)
    n_runs = len(run_files)
    print(f"\n  Refinement runs: {n_runs}")

    if n_runs < 1:
        print(f"  ERROR: Need at least 1 refinement run. Found {n_runs}.")
        return None

    for i, rf in enumerate(run_files):
        print(f"    {i+1:>3}. {rf['filename']:<55s} {rf['n_conditions']:>3} conds")

    # ── Extract conditions from each run ──
    all_run_conditions = []
    for rf in run_files:
        conds = extract_refinement_conditions(rf["path"])
        all_run_conditions.append(conds)

    # ══ TEST 1: Consensus stability ══
    print(f"\n  ══ TEST 1: CONSENSUS STABILITY ══")
    consensus = compute_consensus(all_run_conditions, n_runs, threshold=threshold)
    cc = consensus["consensus_conditions"]

    n_test1_pass = len(cc)
    print(f"  Unique conditions: {consensus['n_unique_conditions']}")
    print(f"  Above {threshold:.0%} threshold: {n_test1_pass}")

    sm = consensus["stability_metrics"]
    print(f"  Avg pairwise overlap: {sm['avg_pairwise_overlap']:.1%}")
    print(f"  Avg conditions per run: {sm['avg_conditions_per_run']:.0f}")

    if n_test1_pass == 0:
        print(f"\n  No conditions passed Test 1. Pipeline cannot proceed.")

    # ══ TEST 2: Per-condition binomial significance ══
    print(f"\n  ══ TEST 2: BINOMIAL SIGNIFICANCE (p < 0.01) ══")

    # These are set during test 2 and reused by depth progression
    cluster_data = None
    available_tickers = set()

    # Load expression cache
    expr_cache = _load_expr_cache()
    if expr_cache is None:
        print("  WARNING: No expression cache — skipping binomial test.")
        print("  All Test 1 survivors kept without binomial validation.")
        test2_survivors = cc
        binomial_results = []
    else:
        # Load cluster file for loser bars
        cluster_data = _load_cluster_file(setup_type, cluster_file)
        if cluster_data is None:
            print("  WARNING: No cluster file — skipping binomial test.")
            test2_survivors = cc
            binomial_results = []
        else:
            loser_bars, n_losing_clusters = _collect_loser_bars(cluster_data)
            available_tickers = expr_cache.get_available_tickers()
            print(f"  Loser bars: {len(loser_bars)} from {n_losing_clusters} clusters")

            if len(loser_bars) == 0:
                print("  WARNING: 0 loser bars — skipping binomial test.")
                test2_survivors = cc
                binomial_results = []
            else:
                # Group loser bars by ticker for efficient cache loading
                loser_by_ticker = defaultdict(list)
                for ticker, bar_idx in loser_bars:
                    loser_by_ticker[ticker].append(bar_idx)

                # For each condition, run binomial test
                test2_survivors = []
                binomial_results = []
                n_total_loser_bars = len(loser_bars)

                for ci, cond in enumerate(cc):
                    cond_name = cond["name"]
                    expr_idx = expr_cache.expr_index(cond_name)
                    low = cond.get("ref_low")
                    high = cond.get("ref_high")

                    if expr_idx is None:
                        print(f"    {cond_name}: expression not in cache — SKIP")
                        binomial_results.append({
                            "name": cond_name, "status": "skip_no_expr",
                            "z": 0, "p": 1.0})
                        continue
                    if low is None or high is None:
                        print(f"    {cond_name}: no bounds — SKIP")
                        binomial_results.append({
                            "name": cond_name, "status": "skip_no_bounds",
                            "z": 0, "p": 1.0})
                        continue

                    # Universe baseline: fraction of ALL values inside [low, high]
                    n_universe_inside = 0
                    n_universe_total = 0
                    for ticker in available_tickers:
                        dates, data = expr_cache.get_ticker(ticker)
                        if dates is None:
                            continue
                        col = data[:, expr_idx]
                        valid = ~(col != col)  # not-NaN check (NaN != NaN)
                        n_valid = int(valid.sum())
                        if n_valid == 0:
                            continue
                        inside = ((col >= low) & (col <= high) & valid)
                        n_universe_inside += int(inside.sum())
                        n_universe_total += n_valid

                    if n_universe_total == 0:
                        print(f"    {cond_name}: no universe data — SKIP")
                        binomial_results.append({
                            "name": cond_name, "status": "skip_no_universe",
                            "z": 0, "p": 1.0})
                        continue

                    F = n_universe_inside / n_universe_total
                    expected_exclusion = 1.0 - F

                    # Loser exclusion: fraction of loser bars outside [low, high]
                    n_loser_outside = 0
                    n_loser_evaluated = 0
                    for ticker, bar_indices in loser_by_ticker.items():
                        if ticker not in available_tickers:
                            continue
                        dates, data = expr_cache.get_ticker(ticker)
                        if dates is None:
                            continue
                        for bi in bar_indices:
                            if bi >= len(data):
                                continue
                            val = data[bi, expr_idx]
                            if val != val:  # NaN
                                continue
                            n_loser_evaluated += 1
                            if val < low or val > high:
                                n_loser_outside += 1

                    if n_loser_evaluated < 5:
                        print(f"    {cond_name}: only {n_loser_evaluated} loser bars evaluated — SKIP")
                        binomial_results.append({
                            "name": cond_name, "status": "skip_few_losers",
                            "n_evaluated": n_loser_evaluated,
                            "z": 0, "p": 1.0})
                        continue

                    z_stat, p_value = binomial_test_normal(
                        n_loser_evaluated, n_loser_outside, expected_exclusion)
                    observed_exclusion = n_loser_outside / n_loser_evaluated

                    br = {
                        "name": cond_name,
                        "universe_F": round(F, 6),
                        "expected_exclusion": round(expected_exclusion, 4),
                        "observed_exclusion": round(observed_exclusion, 4),
                        "n_loser_bars": n_loser_evaluated,
                        "n_outside": n_loser_outside,
                        "z": round(z_stat, 3),
                        "p": round(p_value, 6),
                    }

                    if p_value < 0.01:
                        br["status"] = "pass"
                        test2_survivors.append(cond)
                        print(f"    {cond_name}: excl={observed_exclusion:.1%} vs baseline={expected_exclusion:.1%}  "
                              f"z={z_stat:.2f}  p={p_value:.4f}  PASS")
                    else:
                        br["status"] = "fail"
                        print(f"    {cond_name}: excl={observed_exclusion:.1%} vs baseline={expected_exclusion:.1%}  "
                              f"z={z_stat:.2f}  p={p_value:.4f}  FAIL (geometric only)")

                    binomial_results.append(br)

    n_test2_pass = len(test2_survivors)
    print(f"\n  Test 1 survivors: {n_test1_pass}")
    print(f"  Test 2 survivors: {n_test2_pass}")

    # ══ Apply 5% margin to surviving conditions ══
    locked_conditions = apply_margin(test2_survivors, margin_pct=0.05)
    print(f"  Locked {len(locked_conditions)} conditions with 5% margin")

    # ══ DEPTH PROGRESSION ══
    # Order conditions by individual filter power against full loser cluster pile.
    # Each condition scored by how many losing clusters it eliminates on its own.
    print(f"\n  ══ DEPTH PROGRESSION ══")

    # Reuse cluster_data from test 2 if available; otherwise try loading fresh
    if cluster_data is None:
        cluster_data = _load_cluster_file(setup_type, cluster_file)
        if cluster_data and expr_cache:
            available_tickers = expr_cache.get_available_tickers()

    depth_progression = []
    winner_signals = []
    loser_signals = []
    eliminated_signals = []

    if cluster_data and expr_cache and locked_conditions:
        # Collect losing cluster info: for each cluster, store (ticker, rightmost_bar_idx, all_bar_indices)
        losing_clusters = []
        for cluster in cluster_data.get("clusters", []):
            if cluster.get("classification") != "AUTO_LOSS":
                continue
            tk = cluster["ticker"]
            all_bars = [cluster["rightmost"]["bar_idx"]]
            for lb in cluster.get("leftward", []):
                all_bars.append(lb["bar_idx"])
            losing_clusters.append({
                "ticker": tk,
                "bars": all_bars,
                "rightmost_bar": cluster["rightmost"]["bar_idx"],
                "cluster": cluster,
            })

        n_losing_input = len(losing_clusters)
        winner_sigs, all_loser_sigs = _collect_winner_loser_signals(cluster_data)

        # Score each condition: how many losing clusters does it eliminate solo?
        cond_solo_scores = []
        for cond in locked_conditions:
            cond_name = cond["name"]
            expr_idx = expr_cache.expr_index(cond_name)
            low = cond.get("low")
            high = cond.get("high")
            if expr_idx is None or low is None or high is None:
                cond_solo_scores.append((cond, 0))
                continue

            eliminated = 0
            for lc in losing_clusters:
                # A cluster is eliminated if ANY of its bars falls outside [low, high]
                tk = lc["ticker"]
                if tk not in available_tickers:
                    continue
                dates, data = expr_cache.get_ticker(tk)
                if dates is None:
                    continue
                cluster_outside = False
                for bi in lc["bars"]:
                    if bi >= len(data):
                        continue
                    val = data[bi, expr_idx]
                    if val != val:  # NaN
                        continue
                    if val < low or val > high:
                        cluster_outside = True
                        break
                if cluster_outside:
                    eliminated += 1

            cond_solo_scores.append((cond, eliminated))

        # Sort by solo elimination count (descending = strongest first)
        cond_solo_scores.sort(key=lambda x: x[1], reverse=True)

        # Progressive application: apply conditions in order, track cumulative elimination
        remaining_clusters = set(range(len(losing_clusters)))
        applied_conditions = []

        for depth_i, (cond, solo_score) in enumerate(cond_solo_scores):
            cond_name = cond["name"]
            expr_idx = expr_cache.expr_index(cond_name)
            low = cond.get("low")
            high = cond.get("high")

            applied_conditions.append({
                "name": cond_name,
                "low": low,
                "high": high,
                "category": cond.get("category", ""),
            })

            # Eliminate from remaining set
            newly_eliminated = set()
            if expr_idx is not None and low is not None and high is not None:
                for ci in list(remaining_clusters):
                    lc = losing_clusters[ci]
                    tk = lc["ticker"]
                    if tk not in available_tickers:
                        continue
                    dates, data = expr_cache.get_ticker(tk)
                    if dates is None:
                        continue
                    for bi in lc["bars"]:
                        if bi >= len(data):
                            continue
                        val = data[bi, expr_idx]
                        if val != val:
                            continue
                        if val < low or val > high:
                            newly_eliminated.add(ci)
                            break

            remaining_clusters -= newly_eliminated
            n_surviving = len(remaining_clusters)
            n_eliminated = n_losing_input - n_surviving
            n_winners = len(winner_sigs)
            total_signals = n_winners + n_surviving
            wr = round(n_winners / total_signals, 4) if total_signals > 0 else 0.0

            depth_progression.append({
                "depth": depth_i + 1,
                "conditions": list(applied_conditions),
                "losing_clusters_surviving": n_surviving,
                "losing_clusters_eliminated": n_eliminated,
                "winners": n_winners,
                "total_signals": total_signals,
                "wr": wr,
                "solo_filter_power": solo_score,
            })

        # Build final signal lists from remaining/eliminated clusters
        eliminated_indices = set(range(len(losing_clusters))) - remaining_clusters
        winner_signals = winner_sigs
        for ci in range(len(losing_clusters)):
            lc = losing_clusters[ci]
            sig = {
                "ticker": lc["ticker"],
                "signal_date": lc["cluster"]["rightmost"].get("date", ""),
                "bar_idx": lc["rightmost_bar"],
                "close": lc["cluster"]["rightmost"].get("close"),
                "classification": "AUTO_LOSS",
                "move_adr": lc["cluster"].get("move_adr"),
                "adr_at_signal": lc["cluster"].get("adr_at_signal"),
                "entry_high": lc["cluster"].get("entry_high"),
                "is_example": 1 if lc["cluster"].get("is_example") else 0,
                "exit_bar": lc["cluster"].get("exit_bar_idx"),
                "exit_date": lc["cluster"].get("exit_date", ""),
            }
            if ci in eliminated_indices:
                eliminated_signals.append(sig)
            else:
                loser_signals.append(sig)

        if depth_progression:
            print(f"  {len(depth_progression)} depth levels")
            print(f"    Depth  1: {depth_progression[0]['losing_clusters_eliminated']} eliminated, "
                  f"WR {depth_progression[0]['wr']:.1%}")
            print(f"    Depth {depth_progression[-1]['depth']:2d}: {depth_progression[-1]['losing_clusters_eliminated']} eliminated, "
                  f"WR {depth_progression[-1]['wr']:.1%}")
    else:
        print("  Skipped (no cluster data, cache, or conditions)")
        winner_signals, loser_signals = _collect_winner_loser_signals(
            cluster_data) if cluster_data else ([], [])

    # ══ Load signal conditions from consensus_signal output ══
    signal_conditions = []
    sig_consensus_path = os.path.join(CACHE_DIR, f"consensus_signal_{setup_type}.json")
    if os.path.exists(sig_consensus_path):
        with open(sig_consensus_path) as f:
            sig_data = json.load(f)
        signal_conditions = sig_data.get("all_conditions", [])
        print(f"\n  Signal conditions: {len(signal_conditions)} from {os.path.basename(sig_consensus_path)}")
    else:
        print(f"\n  WARNING: No signal consensus file found at {sig_consensus_path}")

    # Load exit condition from signal_exit grind
    exit_condition = None
    exit_path = os.path.join(CACHE_DIR, f"signal_exit_{setup_type}.json")
    # Also check data/signal_exit_grind/
    if not os.path.exists(exit_path):
        repo_root = os.environ.get("SCANPERFECT_REPO_ROOT", REPO_ROOT)
        alt_dir = os.path.join(repo_root, "data", "signal_exit_grind")
        if os.path.isdir(alt_dir):
            candidates = sorted(glob.glob(os.path.join(alt_dir, f"signal_exit_{setup_type}*.json")))
            if candidates:
                exit_path = candidates[-1]
    if os.path.exists(exit_path):
        with open(exit_path) as f:
            exit_data = json.load(f)
        exit_condition = exit_data.get("exit_condition") or exit_data.get("best_exit")
        if exit_condition:
            print(f"  Exit condition: {exit_condition.get('expression','')} "
                  f"{exit_condition.get('direction','')} {exit_condition.get('threshold','')}")

    # ══ Combine signal + refinement conditions ══
    combined_conditions = list(signal_conditions)
    ref_names = {c["name"] for c in locked_conditions}
    sig_names = {c.get("name", "") for c in signal_conditions}
    overlap = ref_names & sig_names
    # Refinement overrides signal where names overlap
    if overlap:
        combined_conditions = [c for c in combined_conditions if c.get("name", "") not in overlap]
    combined_conditions.extend(locked_conditions)
    print(f"  Combined: {len(signal_conditions)} signal + {len(locked_conditions)} refinement "
          f"({len(overlap)} overlap) = {len(combined_conditions)} total")

    # ══ Save ══
    total_time = _time.time() - t_total
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")

    n_losing_input = 0
    n_eliminated = len(eliminated_signals)
    if cluster_data:
        n_losing_input = sum(1 for c in cluster_data.get("clusters", [])
                             if c.get("classification") == "AUTO_LOSS")

    output = {
        "setup_type": setup_type,
        "stage": "refinement",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "total_time_s": round(total_time, 1),
        "refinement": True,
        "n_runs": n_runs,
        "threshold": threshold,
        "n_conditions": len(combined_conditions),
        "all_conditions": combined_conditions,
        "refinement_conditions_only": locked_conditions,
        "signal_conditions": signal_conditions,
        "exit_condition": exit_condition,
        "params": {
            "consensus_threshold": threshold,
            "binomial_p_threshold": 0.01,
            "margin_pct": 0.05,
            "n_refinement_runs": n_runs,
            "source": "consensus_engine_refinement",
        },
        "summary": {
            "test1_survivors": n_test1_pass,
            "test2_survivors": n_test2_pass,
            "losing_clusters_input": n_losing_input,
            "losing_clusters_eliminated": n_eliminated,
            "losing_clusters_surviving": n_losing_input - n_eliminated,
            "winners_input": len(winner_signals),
            "winners_passing": len(winner_signals),
        },
        "binomial_tests": binomial_results,
        "winner_signals": winner_signals,
        "loser_signals": loser_signals,
        "eliminated_signals": eliminated_signals,
        "depth_progression": depth_progression,
        "consensus": consensus,
        "run_files": [rf["filename"] for rf in run_files],
    }

    save_dir = output_dir or CACHE_DIR
    os.makedirs(save_dir, exist_ok=True)
    out_path = os.path.join(save_dir, f"consensus_refinement_{setup_type}_{ts}.json")
    with open(out_path, "w") as f:
        json.dump(output, f, indent=2)
    print(f"\n  Saved: {out_path}")

    import shutil
    latest_path = os.path.join(save_dir, f"consensus_refinement_{setup_type}.json")
    shutil.copy2(out_path, latest_path)
    print(f"  Latest: {latest_path}")

    print(f"\n  {'='*50}")
    print(f"  REFINEMENT CONSENSUS: {len(locked_conditions)} conditions validated ({total_time:.0f}s)")
    print(f"    Test 1 (stability): {n_test1_pass} passed")
    print(f"    Test 2 (binomial):  {n_test2_pass} passed")
    if depth_progression:
        print(f"    Depth progression:  {len(depth_progression)} levels")
        print(f"    Final WR:           {depth_progression[-1]['wr']:.1%}")
    print(f"  {'='*50}")

    return output


# ══════════════════════════════════════════════════════════════
# DISPATCH
# ══════════════════════════════════════════════════════════════

def run(setup_type, stage, threshold=0.7, input_dir=None, output_dir=None,
        cluster_file=None, **kwargs):
    """Dispatch to signal or refinement consensus."""
    if stage == "signal":
        return run_signal(setup_type, threshold=threshold, input_dir=input_dir,
                          output_dir=output_dir)
    elif stage == "refinement":
        return run_refinement(setup_type, threshold=threshold, input_dir=input_dir,
                              output_dir=output_dir, cluster_file=cluster_file)
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
    parser.add_argument("--output-dir", type=str, default=None,
                        help="Directory to save consensus output (default: CACHE_DIR)")
    parser.add_argument("--cluster-file", type=str, default=None,
                        help="Path to raw_signal_clusters JSON for refinement binomial test")
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
            output_dir=args.output_dir,
            cluster_file=args.cluster_file,
        )
        if result is None:
            sys.exit(1)
