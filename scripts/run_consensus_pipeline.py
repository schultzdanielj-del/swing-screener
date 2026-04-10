"""
Run Consensus Pipeline — Orchestrator for full consensus pipeline.

Chains the entire pipeline as one unattended overnight run:
  Step 1:   15 real + 15 permuted signal grinds (interleaved)
  Step 2:   Signal consensus engine (z-gate: z > 3 required)
  Step 3:   Deterministic scan with locked signal conditions
  Step 3.5: Re-grind exit condition on consensus signal population
  Step 4:   Refinement grind x 10
  Step 5:   Refinement consensus engine

Stops after Step 5. EV grinder and profit grinder are a separate future build.

Pre-flight checks:
  - Nightly refresh guard (--skip-nightly-check to bypass)
  - Expression cache freshness (must cover full OHLCV range from EXPR_CACHE_START)

Early abort after 3+3 runs: if (mean_real - mean_perm) / mean_perm < 0.5, abort.

Usage:
    python scripts/run_consensus_pipeline.py --setup dtss
    python scripts/run_consensus_pipeline.py --setup dtss --test-mode --skip-nightly-check
    python scripts/run_consensus_pipeline.py --setup dtss --beam 5000 --depth 50

Based on SIGNAL_GRINDER.md spec (Increment 10).
"""

import os
import sys
import json
import time
import subprocess
import shutil
import argparse
from datetime import datetime, timezone, date

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

CONSENSUS_DIR = os.path.join(CACHE_DIR, "consensus")

# Six pass orderings for stability selection.
# Each real/permuted run cycles through these.
# A condition that only survives when its timeframe goes first appears in ~5/15 runs
# and fails consensus. A condition that survives regardless appears in 12+/15.
PASS_ORDERINGS = [
    [1, 2, 3],
    [1, 3, 2],
    [2, 1, 3],
    [2, 3, 1],
    [3, 1, 2],
    [3, 2, 1],
]


# ══════════════════════════════════════════════════════════════
# PRE-FLIGHT CHECKS
# ══════════════════════════════════════════════════════════════

def check_nightly_refresh():
    """Check that nightly refresh completed today.

    Reads local_runner/cache/nightly_log.txt for today's completion timestamp.
    Returns True if today's nightly completed, False otherwise.
    """
    log_path = os.path.join(CACHE_DIR, "nightly_log.txt")
    if not os.path.exists(log_path):
        print(f"  FAIL: Nightly log not found: {log_path}")
        print(f"  The nightly refresh has not run. Run it first, or use --skip-nightly-check.")
        return False

    today_str = date.today().isoformat()
    with open(log_path, encoding='utf-8', errors='replace') as f:
        content = f.read()

    if today_str in content:
        print(f"  OK: Nightly refresh completed today ({today_str})")
        return True
    else:
        print(f"  FAIL: Nightly log exists but no entry for today ({today_str})")
        print(f"  Run the nightly refresh first, or use --skip-nightly-check.")
        return False


def check_expr_cache_freshness():
    """Check that expression cache end date matches OHLCV end date.

    Compares the last date in the expression cache against the last date in
    the OHLCV cache. If the expression cache is more than 2 trading days behind,
    it's stale and needs a rebuild.

    Returns True if cache is fresh, False otherwise.
    """
    repo_root = os.environ.get("SCANPERFECT_REPO_ROOT", REPO_ROOT)
    local_runner_dir = os.path.join(repo_root, "local_runner")
    if local_runner_dir not in sys.path:
        sys.path.insert(0, local_runner_dir)

    try:
        from expr_cache_builder import ExprSeriesCache, load_manifest
    except ImportError as e:
        print(f"  FAIL: Cannot import ExprSeriesCache: {e}")
        return False

    cache = ExprSeriesCache()
    if not cache.is_valid():
        print(f"  FAIL: Expression cache not valid or not found")
        return False

    manifest = load_manifest()
    if not manifest:
        print(f"  FAIL: Expression cache manifest not found")
        return False

    n_expr_tickers = len(manifest.get("tickers", {}))

    # Get expression cache end date from a well-known ticker
    expr_end_date = None
    for tk in ["AAPL", "MSFT", "SPY"] + list(manifest.get("tickers", {}).keys())[:5]:
        dates, data = cache.get_ticker(tk)
        if dates is not None and len(dates) > 0:
            expr_end_date = str(dates[-1])[:10]
            expr_bars = len(dates)
            break

    if expr_end_date is None:
        print(f"  FAIL: Could not read any ticker from expression cache")
        return False

    # Get OHLCV end date
    try:
        import pickle
        import pandas as pd
        ohlcv_path = os.path.join(CACHE_DIR, "universe_ohlcv_daily.pkl")
        with open(ohlcv_path, "rb") as f:
            ohlcv = pickle.load(f)
        n_ohlcv_tickers = len(ohlcv)

        # Get OHLCV end date from the same well-known tickers
        # OHLCV DataFrames use RangeIndex (integers) with a 'date' column
        ohlcv_end_date = None
        for tk in ["AAPL", "MSFT", "SPY"] + list(ohlcv.keys())[:5]:
            if tk in ohlcv:
                df = ohlcv[tk]
                if len(df) > 0 and "date" in df.columns:
                    ohlcv_end_date = str(df["date"].iloc[-1])[:10]
                    break

        if ohlcv_end_date is None:
            print(f"  WARNING: Could not determine OHLCV end date")
            print(f"  Expression cache: {n_expr_tickers} tickers, ends {expr_end_date}")
            return True

        print(f"  Expression cache: {n_expr_tickers} tickers, {expr_bars} bars, ends {expr_end_date}")
        print(f"  OHLCV cache: {n_ohlcv_tickers} tickers, ends {ohlcv_end_date}")

        # Compare end dates
        if expr_end_date >= ohlcv_end_date:
            print(f"  OK: Expression cache is fresh")
            return True

        # Count trading days gap
        from datetime import datetime as _dt
        expr_dt = _dt.strptime(expr_end_date, "%Y-%m-%d")
        ohlcv_dt = _dt.strptime(ohlcv_end_date, "%Y-%m-%d")
        calendar_gap = (ohlcv_dt - expr_dt).days
        # Rough estimate: ~5 trading days per 7 calendar days
        trading_gap = int(calendar_gap * 5 / 7)

        if trading_gap <= 2:
            print(f"  OK: Expression cache is fresh (gap: ~{trading_gap} trading days)")
            return True

        print(f"  FAIL: Expression cache ends {expr_end_date}, OHLCV ends {ohlcv_end_date} "
              f"(~{trading_gap} trading days behind)")
        print(f"  Run: python local_runner/cache_builder.py --daily")
        print(f"  Then: python local_runner/expr_cache_builder.py --build")
        return False

    except Exception as e:
        print(f"  WARNING: Could not verify OHLCV freshness: {e}")
        print(f"  Expression cache: {n_expr_tickers} tickers, ends {expr_end_date}")
        return True


# ══════════════════════════════════════════════════════════════
# SUBPROCESS RUNNER
# ══════════════════════════════════════════════════════════════

def run_subprocess(args, description, timeout=None):
    """Run a subprocess, printing its output in real-time.

    Returns (success, output_text).
    """
    print(f"\n  >> {description}")
    print(f"     {' '.join(args)}")

    env = os.environ.copy()
    env["SCANPERFECT_CACHE_DIR"] = CACHE_DIR
    _main_repo = os.environ.get("SCANPERFECT_REPO_ROOT", REPO_ROOT)
    env["SCANPERFECT_REPO_ROOT"] = _main_repo

    try:
        result = subprocess.run(
            args, capture_output=True, text=True, timeout=timeout,
            cwd=REPO_ROOT, encoding='utf-8', errors='replace',
            env=env,
        )
        output = result.stdout + result.stderr
        if result.returncode != 0:
            print(f"     FAILED (exit code {result.returncode})")
            # Print last 300 chars of output for debugging
            if output:
                print(f"     ...{output[-300:]}")
        return result.returncode == 0, output
    except subprocess.TimeoutExpired:
        print(f"     TIMEOUT")
        return False, "TIMEOUT"
    except Exception as e:
        print(f"     ERROR: {e}")
        return False, str(e)


def read_grind_json(path):
    """Read a grind output JSON, return (n_conditions, data) or (0, None) on error."""
    try:
        with open(path) as f:
            data = json.load(f)
        n = data.get("n_conditions", len(data.get("all_conditions", [])))
        return n, data
    except Exception:
        return 0, None


# ══════════════════════════════════════════════════════════════
# STEP 1: SIGNAL GRINDS (15 REAL + 15 PERMUTED, INTERLEAVED)
# ══════════════════════════════════════════════════════════════

def run_signal_grinds(setup, n_real, n_perm, beam, depth, consensus_dir):
    """Run interleaved real + permuted signal grinds.

    Returns (real_files, perm_files, aborted).
    real_files/perm_files are lists of output file paths.
    aborted is True if early abort triggered.
    """
    print("\n" + "=" * 70)
    print(f"  STEP 1: SIGNAL GRINDS ({n_real} real + {n_perm} permuted, interleaved)")
    print("=" * 70)

    os.makedirs(consensus_dir, exist_ok=True)

    real_files = []
    perm_files = []
    real_counts = []
    perm_counts = []

    # Interleave: real1, perm1, real2, perm2, ...
    total_runs = max(n_real, n_perm)
    seed_counter = 0

    for i in range(total_runs):
        # Real run
        if i < n_real:
            seed_counter += 1
            ordering = PASS_ORDERINGS[i % len(PASS_ORDERINGS)]
            order_str = ",".join(str(x) for x in ordering)

            ok, output = run_subprocess([
                sys.executable, "-u",
                os.path.join("local_runner", "pyramid_grinder.py"),
                "--setup", setup,
                "--beam", str(beam),
                "--depth", str(depth),
                "--seed", str(seed_counter),
                "--subsample", "0.5",
                "--zero-margin",
                "--no-peak-target",
                "--pass-order", order_str,
                "--output-dir", consensus_dir,
            ], f"Real signal grind {i+1}/{n_real} (seed={seed_counter}, order={order_str})")

            if not ok:
                print(f"  ERROR: Real grind {i+1} failed. Aborting pipeline.")
                return real_files, perm_files, True

            # Find the output file
            new_files = _find_new_files(consensus_dir, "pyramid", setup, real_files + perm_files)
            if new_files:
                path = new_files[0]
                real_files.append(path)
                n_conds, _ = read_grind_json(path)
                real_counts.append(n_conds)
                print(f"     -> {os.path.basename(path)}: {n_conds} conditions")

        # Permuted run
        if i < n_perm:
            seed_counter += 1
            ordering = PASS_ORDERINGS[i % len(PASS_ORDERINGS)]
            order_str = ",".join(str(x) for x in ordering)

            ok, output = run_subprocess([
                sys.executable, "-u",
                os.path.join("local_runner", "pyramid_grinder.py"),
                "--setup", setup,
                "--beam", str(beam),
                "--depth", str(depth),
                "--seed", str(seed_counter),
                "--subsample", "0.5",
                "--zero-margin",
                "--no-peak-target",
                "--pass-order", order_str,
                "--permute",
                "--output-dir", consensus_dir,
            ], f"Permuted signal grind {i+1}/{n_perm} (seed={seed_counter}, order={order_str})")

            if not ok:
                print(f"  ERROR: Permuted grind {i+1} failed. Aborting pipeline.")
                return real_files, perm_files, True

            new_files = _find_new_files(consensus_dir, "permuted", setup, real_files + perm_files)
            if new_files:
                path = new_files[0]
                perm_files.append(path)
                n_conds, _ = read_grind_json(path)
                perm_counts.append(n_conds)
                print(f"     -> {os.path.basename(path)}: {n_conds} conditions")

        # Early abort checkpoint after 3+3
        if len(real_counts) == 3 and len(perm_counts) == 3 and n_real > 3:
            aborted = _early_abort_check(real_counts, perm_counts, setup, consensus_dir)
            if aborted:
                return real_files, perm_files, True

    print(f"\n  Step 1 complete: {len(real_files)} real + {len(perm_files)} permuted grinds")
    return real_files, perm_files, False


def _find_new_files(directory, prefix, setup, known_files):
    """Find new JSON files in directory matching prefix_setup_*.json, excluding known files."""
    import glob
    pattern = os.path.join(directory, f"{prefix}_{setup}_*.json")
    known_set = set(known_files)
    candidates = []
    for path in glob.glob(pattern):
        if path not in known_set:
            candidates.append(path)
    # Sort by mtime, newest first
    candidates.sort(key=lambda p: os.path.getmtime(p), reverse=True)
    return candidates


def _early_abort_check(real_counts, perm_counts, setup, consensus_dir):
    """Early abort checkpoint after 3+3 runs.

    Computes (mean_real - mean_perm) / mean_perm.
    If < 0.5, abort and write report.

    This is a rough heuristic, NOT the bootstrap z-score.
    """
    mean_real = sum(real_counts) / len(real_counts)
    mean_perm = sum(perm_counts) / len(perm_counts)

    print(f"\n  ══ EARLY ABORT CHECKPOINT (3+3) ══")
    print(f"  Real counts: {real_counts} (mean={mean_real:.1f})")
    print(f"  Perm counts: {perm_counts} (mean={mean_perm:.1f})")

    if mean_perm == 0:
        if mean_real > 0:
            ratio = float('inf')
            print(f"  Separation: inf (permuted found 0 conditions)")
            print(f"  -> CONTINUING (clear separation)")
            return False
        else:
            print(f"  Both real and permuted found 0 conditions.")
            print(f"  -> ABORTING (no signal found)")
            _write_abort_report(setup, consensus_dir, real_counts, perm_counts,
                                0.0, "both_zero")
            return True

    ratio = (mean_real - mean_perm) / mean_perm
    print(f"  Separation ratio: ({mean_real:.1f} - {mean_perm:.1f}) / {mean_perm:.1f} = {ratio:.2f}")

    if ratio < 0.5:
        print(f"  -> ABORTING (ratio {ratio:.2f} < 0.5 — insufficient separation)")
        print(f"  Go vet more examples to strengthen the signal.")
        _write_abort_report(setup, consensus_dir, real_counts, perm_counts,
                            ratio, "insufficient_separation")
        return True
    else:
        print(f"  -> CONTINUING (ratio {ratio:.2f} >= 0.5 — viable separation)")
        return False


def _write_abort_report(setup, consensus_dir, real_counts, perm_counts, ratio, reason):
    """Write early abort report to consensus directory."""
    report = {
        "setup_type": setup,
        "stage": "early_abort",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "reason": reason,
        "real_counts": real_counts,
        "perm_counts": perm_counts,
        "mean_real": sum(real_counts) / len(real_counts) if real_counts else 0,
        "mean_perm": sum(perm_counts) / len(perm_counts) if perm_counts else 0,
        "separation_ratio": ratio,
        "message": "Early abort after 3+3 runs. Insufficient separation between "
                   "real and permuted condition counts. Vet more examples.",
    }
    path = os.path.join(consensus_dir, f"consensus_abort_{setup}.json")
    with open(path, "w") as f:
        json.dump(report, f, indent=2)
    print(f"  Abort report: {path}")


# ══════════════════════════════════════════════════════════════
# STEP 2: SIGNAL CONSENSUS (Z-GATE)
# ══════════════════════════════════════════════════════════════

def run_signal_consensus(setup, consensus_dir):
    """Run signal consensus engine on real + permuted outputs.

    Returns (gate_pass, z_score, consensus_output_path) or (False, z, None) on failure.
    """
    print("\n" + "=" * 70)
    print(f"  STEP 2: SIGNAL CONSENSUS ENGINE")
    print("=" * 70)

    # Consensus engine reads from consensus_dir, writes to CACHE_DIR (standard location)
    # only if gate passes. We use --output-dir to control where it saves.
    # Save to consensus_dir first; copy to standard location only if z >= 3.
    ok, output = run_subprocess([
        sys.executable, "-u",
        os.path.join("scripts", "consensus_engine.py"),
        "--setup", setup,
        "--stage", "signal",
        "--threshold", "0.7",
        "--input-dir", consensus_dir,
        "--output-dir", consensus_dir,
    ], "Signal consensus engine")

    if not ok:
        print(f"  ERROR: Consensus engine failed")
        return False, 0, None

    # Find consensus output
    import glob
    pattern = os.path.join(consensus_dir, f"consensus_signal_{setup}_*.json")
    files = sorted(glob.glob(pattern), key=os.path.getmtime, reverse=True)
    if not files:
        print(f"  ERROR: No consensus output found")
        return False, 0, None

    consensus_path = files[0]
    n_conds, data = read_grind_json(consensus_path)
    if data is None:
        print(f"  ERROR: Could not read consensus output")
        return False, 0, None

    z_score = data.get("z_score", 0)
    gate_pass = data.get("gate_pass", False)

    # Handle "inf" string from JSON
    if z_score == "inf":
        z_score = float('inf')

    print(f"\n  z-score: {z_score}")
    print(f"  Gate pass: {gate_pass}")
    print(f"  Conditions locked: {len(data.get('all_conditions', []))}")

    if not gate_pass:
        # Write gate report
        gate_report = {
            "setup_type": setup,
            "stage": "z_gate_fail",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "z_score": data.get("z_score"),
            "permutation_test": data.get("permutation_test"),
            "message": f"z-score {z_score} < 3. Pipeline stopped. "
                       "Vet more examples to strengthen the signal.",
        }
        gate_path = os.path.join(consensus_dir, f"consensus_gate_{setup}.json")
        with open(gate_path, "w") as f:
            json.dump(gate_report, f, indent=2)
        print(f"\n  GATE FAILED: z={z_score} < 3")
        print(f"  Gate report: {gate_path}")
        print(f"  Pipeline stopped. Vet more examples.")
        return False, z_score, None

    # Gate passed — copy consensus output to standard cache directory
    # This is the ONLY file that goes to the standard location.
    standard_path = os.path.join(CACHE_DIR, f"consensus_signal_{setup}.json")
    shutil.copy2(consensus_path, standard_path)
    print(f"\n  GATE PASSED: z={z_score} >= 3")
    print(f"  Consensus output: {standard_path}")

    return True, z_score, standard_path


# ══════════════════════════════════════════════════════════════
# STEP 3: SCAN-ONLY (DETERMINISTIC SCAN WITH LOCKED CONDITIONS)
# ══════════════════════════════════════════════════════════════

def run_scan_only(setup, conditions_file):
    """Run deterministic scan with locked signal conditions.

    Produces raw_signal_clusters_{setup}.json in CACHE_DIR.
    """
    print("\n" + "=" * 70)
    print(f"  STEP 3: DETERMINISTIC SCAN (SCAN-ONLY)")
    print("=" * 70)

    ok, output = run_subprocess([
        sys.executable, "-u",
        os.path.join("local_runner", "pyramid_grinder.py"),
        "--setup", setup,
        "--scan-only",
        "--conditions-file", conditions_file,
    ], "Scan-only with consensus conditions", timeout=1800)

    if not ok:
        print(f"  ERROR: Scan-only failed")
        return False

    cluster_path = os.path.join(CACHE_DIR, f"raw_signal_clusters_{setup}.json")
    if os.path.exists(cluster_path):
        with open(cluster_path) as f:
            data = json.load(f)
        n_clusters = len(data.get("clusters", []))
        print(f"  Cluster file: {cluster_path}")
        print(f"  Clusters: {n_clusters}")
        return True
    else:
        print(f"  WARNING: Cluster file not found at {cluster_path}")
        return False


# ══════════════════════════════════════════════════════════════
# STEP 3.5: EXIT RE-GRIND
# ══════════════════════════════════════════════════════════════

def run_exit_regrind(setup, conditions_file):
    """Re-grind exit condition on consensus signal population."""
    print("\n" + "=" * 70)
    print(f"  STEP 3.5: EXIT RE-GRIND")
    print("=" * 70)

    ok, output = run_subprocess([
        sys.executable, "-u",
        os.path.join("scripts", "signal_exit_grinder.py"),
        "--setup", setup,
        "--conditions-file", conditions_file,
    ], "Signal exit grinder with consensus conditions", timeout=1800)

    if not ok:
        print(f"  ERROR: Exit re-grind failed")
        return False

    print(f"  Exit re-grind complete")
    return True


# ══════════════════════════════════════════════════════════════
# STEP 4: REFINEMENT GRINDS (x10)
# ══════════════════════════════════════════════════════════════

def run_refinement_grinds(setup, n_runs, conditions_file, consensus_dir):
    """Run N refinement grinds with loser subsampling.

    Returns list of output file paths.
    """
    print("\n" + "=" * 70)
    print(f"  STEP 4: REFINEMENT GRINDS ({n_runs} runs)")
    print("=" * 70)

    ref_files = []

    for i in range(n_runs):
        seed = i + 1

        ok, output = run_subprocess([
            sys.executable, "-u",
            os.path.join("local_runner", "pyramid_grinder.py"),
            "--setup", setup,
            "--blackout",
            "--skip-gather",
            "--subsample-losers",
            "--conditions-file", conditions_file,
            "--seed", str(seed),
            "--output-dir", consensus_dir,
        ], f"Refinement grind {i+1}/{n_runs} (seed={seed})")

        if not ok:
            print(f"  WARNING: Refinement grind {i+1} failed. Continuing with remaining runs.")
            continue

        # Find new refinement file
        new_files = _find_new_files(consensus_dir, "refinement", setup, ref_files)
        if new_files:
            path = new_files[0]
            ref_files.append(path)
            n_conds, _ = read_grind_json(path)
            print(f"     -> {os.path.basename(path)}: {n_conds} conditions")

    print(f"\n  Step 4 complete: {len(ref_files)} refinement grinds")
    return ref_files


# ══════════════════════════════════════════════════════════════
# STEP 5: REFINEMENT CONSENSUS
# ══════════════════════════════════════════════════════════════

def run_refinement_consensus(setup, consensus_dir):
    """Run refinement consensus engine on refinement outputs.

    Returns (success, output_path).
    """
    print("\n" + "=" * 70)
    print(f"  STEP 5: REFINEMENT CONSENSUS ENGINE")
    print("=" * 70)

    # Cluster file for binomial test is in CACHE_DIR (from Step 3)
    cluster_file = os.path.join(CACHE_DIR, f"raw_signal_clusters_{setup}.json")

    ok, output = run_subprocess([
        sys.executable, "-u",
        os.path.join("scripts", "consensus_engine.py"),
        "--setup", setup,
        "--stage", "refinement",
        "--threshold", "0.7",
        "--input-dir", consensus_dir,
        "--output-dir", CACHE_DIR,
        "--cluster-file", cluster_file,
    ], "Refinement consensus engine")

    if not ok:
        print(f"  ERROR: Refinement consensus failed")
        return False, None

    # Find output
    import glob
    pattern = os.path.join(CACHE_DIR, f"consensus_refinement_{setup}_*.json")
    files = sorted(glob.glob(pattern), key=os.path.getmtime, reverse=True)
    if not files:
        print(f"  ERROR: No refinement consensus output found")
        return False, None

    out_path = files[0]
    n_conds, data = read_grind_json(out_path)
    if data:
        t1 = data.get("summary", {}).get("test1_survivors", "?")
        t2 = data.get("summary", {}).get("test2_survivors", "?")
        n_ref = len(data.get("refinement_conditions_only", []))
        print(f"  Refinement consensus: Test1={t1}, Test2={t2}, {n_ref} conditions")

    print(f"  Output: {out_path}")
    return True, out_path


# ══════════════════════════════════════════════════════════════
# SUMMARY REPORT
# ══════════════════════════════════════════════════════════════

def write_summary(setup, consensus_dir, z_score, signal_consensus_path,
                  ref_consensus_path, n_real, n_perm, n_ref_runs, total_time):
    """Write consensus_complete_{setup}.json summary report."""
    print("\n" + "=" * 70)
    print(f"  SUMMARY")
    print("=" * 70)

    # Read signal consensus data
    signal_data = {}
    if signal_consensus_path and os.path.exists(signal_consensus_path):
        with open(signal_consensus_path) as f:
            signal_data = json.load(f)

    # Read refinement consensus data
    ref_data = {}
    if ref_consensus_path and os.path.exists(ref_consensus_path):
        with open(ref_consensus_path) as f:
            ref_data = json.load(f)

    # Read cluster file for signal population size
    cluster_path = os.path.join(CACHE_DIR, f"raw_signal_clusters_{setup}.json")
    n_clusters = 0
    if os.path.exists(cluster_path):
        with open(cluster_path) as f:
            cluster_data = json.load(f)
        n_clusters = len(cluster_data.get("clusters", []))

    n_signal_conds = len(signal_data.get("all_conditions", []))
    n_ref_conds = len(ref_data.get("refinement_conditions_only", []))
    n_combined = len(ref_data.get("all_conditions", []))

    summary = {
        "setup_type": setup,
        "stage": "complete",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "total_time_s": round(total_time, 1),
        "z_score": z_score if z_score != float('inf') else "inf",
        "signal_conditions": n_signal_conds,
        "refinement_conditions": n_ref_conds,
        "combined_conditions": n_combined,
        "signal_population_clusters": n_clusters,
        "n_real_runs": n_real,
        "n_permuted_runs": n_perm,
        "n_refinement_runs": n_ref_runs,
        "signal_consensus_file": os.path.basename(signal_consensus_path) if signal_consensus_path else None,
        "refinement_consensus_file": os.path.basename(ref_consensus_path) if ref_consensus_path else None,
        "stability_metrics": signal_data.get("stability_metrics"),
        "refinement_summary": ref_data.get("summary"),
    }

    out_path = os.path.join(CACHE_DIR, f"consensus_complete_{setup}.json")
    with open(out_path, "w") as f:
        json.dump(summary, f, indent=2)

    print(f"  z-score: {z_score}")
    print(f"  Signal conditions: {n_signal_conds}")
    print(f"  Refinement conditions: {n_ref_conds}")
    print(f"  Combined conditions: {n_combined}")
    print(f"  Signal population: {n_clusters} clusters")
    print(f"  Total time: {total_time/60:.0f} min")
    print(f"  Summary: {out_path}")

    return out_path


# ══════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description="Run Consensus Pipeline — Full overnight orchestrator")
    parser.add_argument("--setup", required=True, help="Setup type (e.g. dtss)")
    parser.add_argument("--beam", type=int, default=10000,
                        help="Beam width for signal grinds (default: 10000)")
    parser.add_argument("--depth", type=int, default=100,
                        help="Search depth for signal grinds (default: 100)")
    parser.add_argument("--test-mode", action="store_true",
                        help="Mini run: 1+1 signal grinds + 1 refinement (not 15+15 + 10)")
    parser.add_argument("--skip-nightly-check", action="store_true",
                        help="Skip nightly refresh guard")
    args = parser.parse_args()

    setup = args.setup
    t0 = time.time()

    # Test mode settings
    if args.test_mode:
        n_real = 1
        n_perm = 1
        n_ref = 1
        beam = 10
        depth = 2
        print(f"\n  ** TEST MODE: 1+1 signal + 1 refinement, beam={beam} depth={depth} **")
    else:
        n_real = 15
        n_perm = 15
        n_ref = 10
        beam = args.beam
        depth = args.depth

    print("\n" + "#" * 70)
    print(f"  CONSENSUS PIPELINE — {setup.upper()}")
    print(f"  {n_real} real + {n_perm} permuted signal grinds")
    print(f"  {n_ref} refinement grinds")
    print(f"  Beam: {beam}, Depth: {depth}")
    print(f"  Consensus dir: {CONSENSUS_DIR}")
    print("#" * 70)

    # ── Pre-flight checks ──
    print(f"\n  ── PRE-FLIGHT CHECKS ──")

    if not args.skip_nightly_check:
        if not check_nightly_refresh():
            sys.exit(1)
    else:
        print(f"  Nightly check: SKIPPED (--skip-nightly-check)")

    if not check_expr_cache_freshness():
        print(f"\n  Expression cache is stale. Cannot run consensus pipeline.")
        print(f"  Fix: python local_runner/cache_builder.py --daily && "
              f"python local_runner/expr_cache_builder.py --build")
        sys.exit(1)

    # ── Clean consensus directory ──
    if os.path.exists(CONSENSUS_DIR):
        shutil.rmtree(CONSENSUS_DIR)
    os.makedirs(CONSENSUS_DIR, exist_ok=True)
    print(f"\n  Cleaned consensus directory: {CONSENSUS_DIR}")

    # ── Step 1: Signal grinds ──
    real_files, perm_files, aborted = run_signal_grinds(
        setup, n_real, n_perm, beam, depth, CONSENSUS_DIR)

    if aborted:
        total_time = time.time() - t0
        print(f"\n  Pipeline aborted after {total_time/60:.0f} min")
        sys.exit(1)

    # ── Step 2: Signal consensus ──
    gate_pass, z_score, signal_consensus_path = run_signal_consensus(setup, CONSENSUS_DIR)

    if not gate_pass:
        total_time = time.time() - t0
        print(f"\n  Pipeline stopped at z-gate after {total_time/60:.0f} min")
        sys.exit(1)

    # ── Step 3: Scan-only ──
    if not run_scan_only(setup, signal_consensus_path):
        print(f"  WARNING: Scan-only failed. Continuing to exit grinder.")

    # ── Step 3.5: Exit re-grind ──
    if not run_exit_regrind(setup, signal_consensus_path):
        print(f"  WARNING: Exit re-grind failed. Continuing to refinement.")

    # ── Step 4: Refinement grinds ──
    ref_files = run_refinement_grinds(setup, n_ref, signal_consensus_path, CONSENSUS_DIR)

    if not ref_files:
        print(f"  WARNING: No refinement grinds completed. Skipping refinement consensus.")
        ref_consensus_path = None
    else:
        # ── Step 5: Refinement consensus ──
        ref_ok, ref_consensus_path = run_refinement_consensus(setup, CONSENSUS_DIR)
        if not ref_ok:
            ref_consensus_path = None

    # ── Summary ──
    total_time = time.time() - t0
    summary_path = write_summary(
        setup, CONSENSUS_DIR, z_score, signal_consensus_path,
        ref_consensus_path, len(real_files), len(perm_files),
        len(ref_files), total_time)

    print("\n" + "#" * 70)
    print(f"  CONSENSUS PIPELINE COMPLETE ({total_time/60:.0f} min)")
    print(f"  Summary: {summary_path}")
    print("#" * 70 + "\n")


if __name__ == "__main__":
    main()
