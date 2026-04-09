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
    (Steps 3-7 added in later sessions as increments are built)
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
