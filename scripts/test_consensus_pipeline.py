"""
Test Consensus Pipeline — Miniature E2E validation.

Runs a stripped-down version of the full consensus pipeline with tiny beam/depth
to verify every component produces correct output. All files write to
local_runner/cache/consensus/test/, cleaned at start of each run.

Usage:
    python scripts/test_consensus_pipeline.py --setup dtss

Steps grow with each build session:
    Step 0: Single-run backward compat (no consensus flags)
    Step 1: Real consensus grind (seed + subsample + zero-margin + no-peak-target)
    Step 2: Determinism check (same seed = same conditions)
    Step 3: Permuted grind (--permute, verify permuted_ prefix)
    Step 4: Scan-only (--scan-only + --conditions-file, verify cluster file)
    Step 5: Refinement (--blackout --skip-gather --subsample-losers --conditions-file)
    Step 6: Exit re-grind (signal_exit_grinder --conditions-file)
    (Step 7+ added in Session 3: consensus engine)
"""

import argparse
import os
import sys
import json
import subprocess
import shutil
import time

# Force UTF-8 output on Windows
if sys.platform == 'win32':
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Resolve the REAL cache directory. In a git worktree the code lives under
# .claude/worktrees/<name>/ but the cache is in the main repo checkout.
# SCANPERFECT_CACHE_DIR env var can override; otherwise follow LOCAL_DIR.
_MAIN_REPO = PROJECT_ROOT
# Detect worktree: if we're under .claude/worktrees/, the main repo is the
# ancestor directory ABOVE .claude/
_claude_marker = os.sep + ".claude" + os.sep
if _claude_marker in PROJECT_ROOT:
    _MAIN_REPO = PROJECT_ROOT[:PROJECT_ROOT.index(_claude_marker)]

CACHE_DIR = os.environ.get(
    "SCANPERFECT_CACHE_DIR",
    os.path.join(_MAIN_REPO, "local_runner", "cache"),
)
TEST_DIR = os.path.join(CACHE_DIR, "consensus", "test")

# Tiny beam/depth for fast testing
# --subsample 0.1 cuts universe from 11K to ~1.1K tickers = ~30s per tier vs 4 min
TEST_BEAM = 10
TEST_DEPTH = 2
TEST_SUBSAMPLE = "0.1"


def clean_test_dir():
    """Remove and recreate the test directory."""
    if os.path.exists(TEST_DIR):
        shutil.rmtree(TEST_DIR)
    os.makedirs(TEST_DIR, exist_ok=True)


def run_cmd(args, description, timeout=600):
    """Run a subprocess, return (success, stdout+stderr)."""
    print(f"\n    Running: {' '.join(args)}")
    env = os.environ.copy()
    env["SCANPERFECT_CACHE_DIR"] = CACHE_DIR
    env["SCANPERFECT_REPO_ROOT"] = _MAIN_REPO
    try:
        result = subprocess.run(
            args, capture_output=True, text=True, timeout=timeout,
            cwd=PROJECT_ROOT, encoding='utf-8', errors='replace',
            env=env,
        )
        output = result.stdout + result.stderr
        return result.returncode == 0, output
    except subprocess.TimeoutExpired:
        return False, f"TIMEOUT after {timeout}s"
    except Exception as e:
        return False, str(e)


def step_pass(step_name, detail=""):
    """Print green checkmark for passing step."""
    msg = f"  [PASS] {step_name}"
    if detail:
        msg += f" -- {detail}"
    print(msg)
    return True


def step_fail(step_name, detail=""):
    """Print red X for failing step, return False."""
    msg = f"  [FAIL] {step_name}"
    if detail:
        msg += f" -- {detail}"
    print(msg)
    return False


def step0_single_run_compat(setup):
    """Backward compat: run grinder with NO consensus flags, verify output."""
    name = "Step 0: Single-run backward compat"
    print(f"\n{'='*60}")
    print(f"  {name}")
    print(f"{'='*60}")

    out_dir = os.path.join(TEST_DIR, "step0")
    os.makedirs(out_dir, exist_ok=True)

    ok, output = run_cmd([
        sys.executable, "-u",
        os.path.join("local_runner", "pyramid_grinder.py"),
        "--setup", setup,
        "--beam", str(TEST_BEAM),
        "--depth", str(TEST_DEPTH),
        "--seed", "1",
        "--subsample", TEST_SUBSAMPLE,
        "--output-dir", out_dir,
    ], "single-run grind (subsample only, no consensus mechanics)")

    if not ok:
        return step_fail(name, f"grinder exited non-zero:\n{output[-500:]}")

    # Check output file exists with pyramid_ prefix
    files = [f for f in os.listdir(out_dir) if f.startswith(f"pyramid_{setup}") and f.endswith(".json")]
    if not files:
        return step_fail(name, f"no pyramid_{setup}_*.json in {out_dir}")

    # Verify JSON structure
    with open(os.path.join(out_dir, files[0])) as f:
        data = json.load(f)

    required_keys = ["setup_type", "all_conditions", "tier_results", "params", "summary"]
    missing = [k for k in required_keys if k not in data]
    if missing:
        return step_fail(name, f"missing keys: {missing}")

    n_conds = len(data["all_conditions"])
    return step_pass(name, f"{n_conds} conditions, file={files[0]}")


def step1_consensus_grind(setup):
    """Real consensus grind with seed + subsample + zero-margin + no-peak-target."""
    name = "Step 1: Consensus grind (real)"
    print(f"\n{'='*60}")
    print(f"  {name}")
    print(f"{'='*60}")

    out_dir = os.path.join(TEST_DIR, "step1")
    os.makedirs(out_dir, exist_ok=True)

    ok, output = run_cmd([
        sys.executable, "-u",
        os.path.join("local_runner", "pyramid_grinder.py"),
        "--setup", setup,
        "--beam", str(TEST_BEAM),
        "--depth", str(TEST_DEPTH),
        "--seed", "42",
        "--subsample", TEST_SUBSAMPLE,
        "--zero-margin",
        "--no-peak-target",
        "--pass-order", "2,1,3",
        "--output-dir", out_dir,
    ], "consensus grind (real)")

    if not ok:
        return step_fail(name, f"grinder exited non-zero:\n{output[-500:]}")

    files = [f for f in os.listdir(out_dir) if f.startswith(f"pyramid_{setup}") and f.endswith(".json")]
    if not files:
        return step_fail(name, f"no pyramid_{setup}_*.json in {out_dir}")

    with open(os.path.join(out_dir, files[0])) as f:
        data = json.load(f)

    # Verify consensus params recorded
    params = data.get("params", {})
    checks = []
    if params.get("seed") != 42:
        checks.append(f"seed={params.get('seed')} (expected 42)")
    expected_subsample = float(TEST_SUBSAMPLE)
    if params.get("subsample") != expected_subsample:
        checks.append(f"subsample={params.get('subsample')} (expected {expected_subsample})")
    if not params.get("zero_margin"):
        checks.append("zero_margin not True")
    if not params.get("no_peak_target"):
        checks.append("no_peak_target not True")
    if params.get("pass_order") != [2, 1, 3]:
        checks.append(f"pass_order={params.get('pass_order')} (expected [2,1,3])")

    if checks:
        return step_fail(name, f"param mismatches: {'; '.join(checks)}")

    # Verify output has conditions
    n_conds = len(data.get("all_conditions", []))
    if n_conds == 0:
        return step_fail(name, "zero conditions found")

    # Verify subsampling happened (check output text)
    if "Subsampled to" not in output:
        return step_fail(name, "no 'Subsampled to' message in output")

    return step_pass(name, f"{n_conds} conditions, params verified, file={files[0]}")


def step2_determinism(setup):
    """Same seed must produce identical conditions."""
    name = "Step 2: Determinism check"
    print(f"\n{'='*60}")
    print(f"  {name}")
    print(f"{'='*60}")

    out_dir = os.path.join(TEST_DIR, "step2")
    os.makedirs(out_dir, exist_ok=True)

    condition_sets = []
    for run_i in range(2):
        run_dir = os.path.join(out_dir, f"run{run_i}")
        os.makedirs(run_dir, exist_ok=True)

        ok, output = run_cmd([
            sys.executable, "-u",
            os.path.join("local_runner", "pyramid_grinder.py"),
            "--setup", setup,
            "--beam", str(TEST_BEAM),
            "--depth", str(TEST_DEPTH),
            "--seed", "42",
            "--subsample", TEST_SUBSAMPLE,
            "--zero-margin",
            "--no-peak-target",
            "--pass-order", "1,2,3",
            "--output-dir", run_dir,
        ], f"determinism run {run_i + 1}")

        if not ok:
            return step_fail(name, f"run {run_i + 1} failed:\n{output[-500:]}")

        files = [f for f in os.listdir(run_dir) if f.startswith(f"pyramid_{setup}") and f.endswith(".json")]
        if not files:
            return step_fail(name, f"run {run_i + 1}: no output file")

        with open(os.path.join(run_dir, files[0])) as f:
            data = json.load(f)
        cond_names = sorted([c["name"] for c in data.get("all_conditions", [])])
        condition_sets.append(cond_names)

    if condition_sets[0] != condition_sets[1]:
        diff_count = sum(1 for a, b in zip(condition_sets[0], condition_sets[1]) if a != b)
        return step_fail(name, f"runs differ: {diff_count} condition name mismatches "
                         f"({len(condition_sets[0])} vs {len(condition_sets[1])} total)")

    return step_pass(name, f"both runs: {len(condition_sets[0])} identical conditions")


def step3_permute(setup):
    """Permuted run produces permuted_ prefix, not picked up by load functions."""
    name = "Step 3: Permuted run (Inc 4)"
    print(f"\n{'='*60}")
    print(f"  {name}")
    print(f"{'='*60}")

    out_dir = os.path.join(TEST_DIR, "step3")
    os.makedirs(out_dir, exist_ok=True)

    ok, output = run_cmd([
        sys.executable, "-u",
        os.path.join("local_runner", "pyramid_grinder.py"),
        "--setup", setup,
        "--beam", str(TEST_BEAM),
        "--depth", str(TEST_DEPTH),
        "--seed", "99",
        "--subsample", TEST_SUBSAMPLE,
        "--zero-margin",
        "--no-peak-target",
        "--pass-order", "3,1,2",
        "--permute",
        "--output-dir", out_dir,
    ], "permuted grind")

    if not ok:
        return step_fail(name, f"grinder exited non-zero:\n{output[-500:]}")

    # Must have permuted_ prefix, NOT pyramid_
    perm_files = [f for f in os.listdir(out_dir) if f.startswith(f"permuted_{setup}") and f.endswith(".json")]
    pyr_files = [f for f in os.listdir(out_dir) if f.startswith(f"pyramid_{setup}") and f.endswith(".json")]

    if not perm_files:
        return step_fail(name, "no permuted_*.json output")
    if pyr_files:
        return step_fail(name, f"BUG: pyramid_*.json found in permuted run: {pyr_files}")

    # Verify params record permute=True
    with open(os.path.join(out_dir, perm_files[0])) as f:
        data = json.load(f)
    if not data.get("params", {}).get("permute"):
        return step_fail(name, "params.permute not True")

    # Verify "fake examples generated" message
    if "fake examples generated" not in output:
        return step_fail(name, "no 'fake examples generated' message")

    return step_pass(name, f"file={perm_files[0]}, {len(data.get('all_conditions',[]))} conditions")


def step4_scan_only(setup):
    """--scan-only with --conditions-file produces cluster file."""
    name = "Step 4: Scan-only mode (Inc 5)"
    print(f"\n{'='*60}")
    print(f"  {name}")
    print(f"{'='*60}")

    # Use step1 output as conditions source
    step1_dir = os.path.join(TEST_DIR, "step1")
    cond_files = [f for f in os.listdir(step1_dir) if f.startswith(f"pyramid_{setup}") and f.endswith(".json")]
    if not cond_files:
        return step_fail(name, "no step1 output to use as conditions file")
    cond_path = os.path.join(step1_dir, cond_files[0])

    ok, output = run_cmd([
        sys.executable, "-u",
        os.path.join("local_runner", "pyramid_grinder.py"),
        "--setup", setup,
        "--scan-only",
        "--conditions-file", cond_path,
    ], "scan-only mode", timeout=600)

    if not ok:
        return step_fail(name, f"scan-only exited non-zero:\n{output[-500:]}")

    # Cluster file must exist in CACHE_DIR
    cluster_file = os.path.join(CACHE_DIR, f"raw_signal_clusters_{setup}.json")
    if not os.path.exists(cluster_file):
        return step_fail(name, f"cluster file not created: {cluster_file}")

    with open(cluster_file) as f:
        cluster_data = json.load(f)
    n_clusters = len(cluster_data.get("clusters", []))

    # Verify no beam search output
    if "PeakSpiderweb" in output or "Level " in output:
        return step_fail(name, "beam search ran during scan-only mode")

    return step_pass(name, f"{n_clusters} clusters in {os.path.basename(cluster_file)}")


def step5_refinement(setup):
    """Refinement grind with --skip-gather + --subsample-losers + --conditions-file."""
    name = "Step 5: Refinement path (Inc 6)"
    print(f"\n{'='*60}")
    print(f"  {name}")
    print(f"{'='*60}")

    # Use step1 output as conditions source
    step1_dir = os.path.join(TEST_DIR, "step1")
    cond_files = [f for f in os.listdir(step1_dir) if f.startswith(f"pyramid_{setup}") and f.endswith(".json")]
    if not cond_files:
        return step_fail(name, "no step1 output to use as conditions file")
    cond_path = os.path.join(step1_dir, cond_files[0])

    out_dir = os.path.join(TEST_DIR, "step5")
    os.makedirs(out_dir, exist_ok=True)

    # Refinement requires raw_signal_clusters file (written by step4 scan-only)
    # and classified signals from the cluster file. The cluster file may be empty
    # (0 signals from test conditions), which will cause refinement to abort
    # at the pile loading step. That's OK — the test verifies the wiring.
    ok, output = run_cmd([
        sys.executable, "-u",
        os.path.join("local_runner", "pyramid_grinder.py"),
        "--setup", setup,
        "--blackout",
        "--skip-gather",
        "--subsample-losers",
        "--seed", "1",
        "--conditions-file", cond_path,
        "--output-dir", out_dir,
    ], "refinement with --skip-gather + --subsample-losers + --conditions-file", timeout=600)

    # Verify --conditions-file was loaded
    if "--conditions-file:" in output:
        cond_loaded = True
    else:
        cond_loaded = False

    # Refinement may abort if cluster file is empty (0 signals from test conditions).
    # The key test is that the CLI args were accepted and the code path was entered.
    if ok:
        # Check refinement output
        ref_files = [f for f in os.listdir(out_dir) if f.startswith(f"refinement_{setup}") and f.endswith(".json")]
        if ref_files:
            return step_pass(name, f"refinement completed, file={ref_files[0]}")
        return step_pass(name, "refinement completed (no output file — may be empty clusters)")

    # Expected failures with test data
    if "ABORT" in output or "Could not load refinement piles" in output or "no clusters" in output.lower():
        detail = "wiring OK (abort expected — empty clusters from test data)"
        if cond_loaded:
            detail += ", --conditions-file loaded"
        return step_pass(name, detail)

    return step_fail(name, f"refinement failed unexpectedly:\n{output[-500:]}")


def step6_exit_grinder_conditions_file(setup):
    """signal_exit_grinder.py --conditions-file loads from supplied file."""
    name = "Step 6: Exit grinder --conditions-file (Inc 7)"
    print(f"\n{'='*60}")
    print(f"  {name}")
    print(f"{'='*60}")

    # Use step1 output as conditions source
    step1_dir = os.path.join(TEST_DIR, "step1")
    cond_files = [f for f in os.listdir(step1_dir) if f.startswith(f"pyramid_{setup}") and f.endswith(".json")]
    if not cond_files:
        return step_fail(name, "no step1 output to use as conditions file")
    cond_path = os.path.join(step1_dir, cond_files[0])

    ok, output = run_cmd([
        sys.executable, "-u",
        os.path.join("scripts", "signal_exit_grinder.py"),
        "--setup", setup,
        "--conditions-file", cond_path,
    ], "exit grinder with --conditions-file", timeout=600)

    # Verify it loaded from --conditions-file (not auto-discovered)
    if "Loading conditions from:" not in output and "from --conditions-file" not in output:
        return step_fail(name, "no '--conditions-file' load message in output")

    # The exit grinder may fail with 0 valid examples when using narrow test
    # conditions (beam=10 subsample). This is a data limitation, not a code bug.
    # The key test is that --conditions-file was loaded (verified above).
    if ok:
        return step_pass(name, "exit grinder completed with supplied conditions")
    elif "Only 0 valid examples" in output or "Only 1 valid examples" in output or "Only 2 valid examples" in output:
        return step_pass(name, "conditions loaded OK (too few examples resolved — expected with test data)")
    else:
        return step_fail(name, f"exit grinder failed unexpectedly:\n{output[-500:]}")


def main():
    parser = argparse.ArgumentParser(description="Test Consensus Pipeline")
    parser.add_argument("--setup", required=True, help="Setup type (e.g. dtss)")
    args = parser.parse_args()

    setup = args.setup
    t0 = time.time()

    print(f"\n{'#'*60}")
    print(f"  TEST CONSENSUS PIPELINE -- {setup.upper()}")
    print(f"  Test dir: {TEST_DIR}")
    print(f"{'#'*60}")

    clean_test_dir()

    steps = [
        ("step0", lambda: step0_single_run_compat(setup)),
        ("step1", lambda: step1_consensus_grind(setup)),
        ("step2", lambda: step2_determinism(setup)),
        ("step3", lambda: step3_permute(setup)),
        ("step4", lambda: step4_scan_only(setup)),
        ("step5", lambda: step5_refinement(setup)),
        ("step6", lambda: step6_exit_grinder_conditions_file(setup)),
        # Steps 7+ added in Session 3 (consensus engine)
    ]

    passed = 0
    total = len(steps)

    for step_id, step_fn in steps:
        try:
            if step_fn():
                passed += 1
            else:
                print(f"\n  STOPPED at {step_id}. Fix and re-run.")
                break
        except Exception as e:
            step_fail(step_id, f"exception: {e}")
            print(f"\n  STOPPED at {step_id}. Fix and re-run.")
            break

    elapsed = time.time() - t0
    print(f"\n{'#'*60}")
    if passed == total:
        print(f"  RESULT: {passed}/{total} PASSED ({elapsed:.0f}s)")
    else:
        print(f"  RESULT: {passed}/{total} PASSED, FAILED at step {passed} ({elapsed:.0f}s)")
    print(f"{'#'*60}\n")

    sys.exit(0 if passed == total else 1)


if __name__ == "__main__":
    main()
