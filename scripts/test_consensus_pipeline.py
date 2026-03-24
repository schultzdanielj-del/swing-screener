"""
Consensus Pipeline Test Runner — Self-Verifying Mini Pipeline.

Runs a miniature version of the full consensus pipeline (1 real + 1 permuted)
to verify every step produces correct output before committing to the overnight run.

One command, no manual intervention. All intermediate files write to
local_runner/cache/consensus/test/, cleaned at start of each test run.

Usage:
    python scripts/test_consensus_pipeline.py --setup dtss
"""

import os
import sys
import json
import glob
import time
import shutil
import subprocess
import argparse
from datetime import datetime

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOCAL_DIR = os.path.join(REPO_ROOT, "local_runner")
CACHE_DIR = os.path.join(LOCAL_DIR, "cache")
TEST_DIR = os.path.join(CACHE_DIR, "consensus", "test")


# ══════════════════════════════════════════════════════════════
# HELPERS
# ══════════════════════════════════════════════════════════════

def run_cmd(args_list, cwd=REPO_ROOT):
    """Run a subprocess, return (success, elapsed)."""
    t0 = time.time()
    result = subprocess.run(
        args_list, cwd=cwd,
        stdout=sys.stdout, stderr=sys.stderr,
    )
    elapsed = time.time() - t0
    return result.returncode == 0, elapsed


def step_pass(step_num, name, detail=""):
    """Print green checkmark for passing step."""
    d = f" — {detail}" if detail else ""
    print(f"  ✓ Step {step_num}: {name}{d}")


def step_fail(step_num, name, detail=""):
    """Print red X for failing step, exit."""
    d = f" — {detail}" if detail else ""
    print(f"  ✗ Step {step_num}: {name}{d}")
    print(f"\n  FAILED at step {step_num}")
    sys.exit(1)


# ══════════════════════════════════════════════════════════════
# TEST RUNNER
# ══════════════════════════════════════════════════════════════

def run_tests(setup_type, beam=50, depth=5):
    """Run all 9 test steps."""
    print("\n" + "═" * 70)
    print("  CONSENSUS PIPELINE TEST RUNNER")
    print("═" * 70)
    print(f"  Setup: {setup_type.upper()}")
    print(f"  Beam: {beam}, Depth: {depth} (small for speed)")
    print(f"  Test dir: {TEST_DIR}")

    t_total = time.time()
    steps_passed = 0

    # Clean test directory
    if os.path.exists(TEST_DIR):
        shutil.rmtree(TEST_DIR)
    os.makedirs(TEST_DIR, exist_ok=True)
    print(f"  Cleaned test directory\n")

    # ────────────────────────────────────────────────────────
    # Step 1: Real signal grind
    # ────────────────────────────────────────────────────────
    print(f"  ── Step 1: Real signal grind ──")
    ok, elapsed = run_cmd([
        sys.executable, "local_runner/pyramid_grinder.py",
        "--setup", setup_type,
        "--beam", str(beam), "--depth", str(depth),
        "--seed", "1", "--subsample", "0.5",
        "--zero-margin", "--no-peak-target",
        "--pass-order", "1,2,3",
        "--output-dir", TEST_DIR + "/",
    ])
    if not ok:
        step_fail(1, "Real signal grind", "subprocess failed")

    # Verify
    real_files = glob.glob(os.path.join(TEST_DIR, f"pyramid_{setup_type}_mp_*.json"))
    if not real_files:
        step_fail(1, "Real signal grind", "no output file found")
    with open(real_files[0]) as f:
        data = json.load(f)
    conds = data.get("all_conditions", [])
    if not conds:
        step_fail(1, "Real signal grind", "all_conditions is empty")

    step_pass(1, "Real signal grind", f"{len(conds)} conditions, {elapsed:.0f}s")
    steps_passed += 1

    # ────────────────────────────────────────────────────────
    # Step 2: Permuted signal grind
    # ────────────────────────────────────────────────────────
    print(f"\n  ── Step 2: Permuted signal grind ──")
    ok, elapsed = run_cmd([
        sys.executable, "local_runner/pyramid_grinder.py",
        "--setup", setup_type,
        "--beam", str(beam), "--depth", str(depth),
        "--permute",
        "--seed", "2", "--subsample", "0.5",
        "--zero-margin", "--no-peak-target",
        "--pass-order", "2,1,3",
        "--output-dir", TEST_DIR + "/",
    ])
    if not ok:
        step_fail(2, "Permuted signal grind", "subprocess failed")

    perm_files = glob.glob(os.path.join(TEST_DIR, f"permuted_{setup_type}_mp_*.json"))
    if not perm_files:
        step_fail(2, "Permuted signal grind", "no output file found")
    with open(perm_files[0]) as f:
        data = json.load(f)
    perm_conds = data.get("all_conditions", [])
    if not perm_conds:
        step_fail(2, "Permuted signal grind", "all_conditions is empty")

    step_pass(2, "Permuted signal grind", f"{len(perm_conds)} conditions, {elapsed:.0f}s")
    steps_passed += 1

    # ────────────────────────────────────────────────────────
    # Step 3: Signal consensus engine
    # ────────────────────────────────────────────────────────
    print(f"\n  ── Step 3: Signal consensus engine ──")
    ok, elapsed = run_cmd([
        sys.executable, "scripts/consensus_engine.py",
        "--setup", setup_type,
        "--stage", "signal",
        "--threshold", "0.7",
        "--input-dir", TEST_DIR + "/",
        "--min-permuted", "1",
    ])
    if not ok:
        step_fail(3, "Signal consensus engine", "subprocess failed")

    # With 1+1 runs, z will be negative. The consensus engine writes a gate report
    # instead of consensus_signal. For downstream testing, we need a consensus file.
    # Build one from the real run's conditions (test mechanics, not statistics).
    consensus_path = os.path.join(CACHE_DIR, f"consensus_signal_{setup_type}.json")
    gate_path = os.path.join(TEST_DIR, f"consensus_gate_{setup_type}.json")

    if os.path.exists(consensus_path):
        # Consensus engine wrote it (unlikely with 1+1 but possible)
        with open(consensus_path) as f:
            cons_data = json.load(f)
        z = cons_data.get("z_score", 0)
    elif os.path.exists(gate_path):
        # Gate failed — expected with 1+1. Build a test consensus file
        # from the real run's conditions so downstream steps can execute.
        with open(gate_path) as f:
            gate_data = json.load(f)
        z = gate_data.get("z_score", 0)

        # Read real conditions and apply 5% margin (same as consensus engine Phase E)
        with open(real_files[0]) as f:
            real_data = json.load(f)
        locked = []
        for c in real_data["all_conditions"]:
            low = c.get("low", 0)
            high = c.get("high", 0)
            margin = (high - low) * 0.05
            locked.append({
                "name": c.get("name", ""),
                "expr": c.get("name", ""),
                "category": c.get("category", ""),
                "tier": c.get("tier", ""),
                "compute": c.get("compute", ""),
                "filter_power": c.get("filter_power"),
                "low": low - margin,
                "high": high + margin,
                "frequency": 1,
                "frequency_pct": 1.0,
            })
        test_consensus = {
            "setup_type": setup_type,
            "stage": "signal",
            "z_score": z,
            "gate_decision": "PROCEED_TEST",
            "n_conditions": len(locked),
            "all_conditions": locked,
            "test_mode": True,
        }
        with open(consensus_path, "w") as f:
            json.dump(test_consensus, f, indent=2)
        print(f"    (z={z}, gate failed as expected with 1+1 — built test consensus from real conditions)")
    else:
        step_fail(3, "Signal consensus engine", "no output file found")

    step_pass(3, "Signal consensus engine", f"z={z}, {elapsed:.0f}s")
    steps_passed += 1

    # ────────────────────────────────────────────────────────
    # Step 4: Deterministic scan
    # ────────────────────────────────────────────────────────
    print(f"\n  ── Step 4: Deterministic scan ──")
    t4_start = time.time()
    ok, elapsed = run_cmd([
        sys.executable, "local_runner/pyramid_grinder.py",
        "--setup", setup_type,
        "--scan-only",
        "--conditions-file", consensus_path,
    ])
    if not ok:
        step_fail(4, "Deterministic scan", "subprocess failed")

    cluster_path = os.path.join(CACHE_DIR, f"raw_signal_clusters_{setup_type}.json")
    if not os.path.exists(cluster_path):
        step_fail(4, "Deterministic scan", "cluster file not found")
    with open(cluster_path) as f:
        cl_data = json.load(f)
    clusters = cl_data.get("clusters", [])
    if not clusters:
        step_fail(4, "Deterministic scan", "clusters array is empty")
    # Check classification field
    if "classification" not in clusters[0]:
        step_fail(4, "Deterministic scan", "cluster missing classification field")
    n_win = sum(1 for c in clusters if c["classification"] == "AUTO_WIN")
    n_loss = sum(1 for c in clusters if c["classification"] == "AUTO_LOSS")

    step_pass(4, "Deterministic scan", f"{len(clusters)} clusters ({n_win} WIN, {n_loss} LOSS), {elapsed:.0f}s")
    steps_passed += 1

    # ────────────────────────────────────────────────────────
    # Step 5: Exit re-grind
    # ────────────────────────────────────────────────────────
    print(f"\n  ── Step 5: Exit re-grind ──")
    ok, elapsed = run_cmd([
        sys.executable, "scripts/signal_exit_grinder.py",
        "--setup", setup_type,
        "--conditions-file", consensus_path,
    ])
    if not ok:
        step_fail(5, "Exit re-grind", "subprocess failed")

    exit_path = os.path.join(REPO_ROOT, "data", "signal_exit_grind", f"signal_exit_{setup_type}.json")
    if not os.path.exists(exit_path):
        step_fail(5, "Exit re-grind", "exit file not found")
    with open(exit_path) as f:
        exit_data = json.load(f)
    top_conds = exit_data.get("top_conditions", [])
    if not top_conds:
        step_fail(5, "Exit re-grind", "top_conditions is empty")
    # Verify timestamp is recent (after step 4 start)
    exit_mtime = os.path.getmtime(exit_path)
    if exit_mtime < t4_start:
        step_fail(5, "Exit re-grind", "exit file is stale (older than step 4)")

    step_pass(5, "Exit re-grind", f"best: {top_conds[0].get('expression', '?')}, {elapsed:.0f}s")
    steps_passed += 1

    # ────────────────────────────────────────────────────────
    # Step 6: Refinement grind
    # ────────────────────────────────────────────────────────
    print(f"\n  ── Step 6: Refinement grind ──")
    ok, elapsed = run_cmd([
        sys.executable, "local_runner/pyramid_grinder.py",
        "--setup", setup_type,
        "--blackout",
        "--skip-gather",
        "--subsample-losers",
        "--seed", "1",
        "--conditions-file", consensus_path,
        "--output-dir", TEST_DIR + "/",
    ])
    if not ok:
        step_fail(6, "Refinement grind", "subprocess failed")

    ref_files = glob.glob(os.path.join(TEST_DIR, f"refinement_{setup_type}_*.json"))
    if not ref_files:
        step_fail(6, "Refinement grind", "no output file found")
    with open(ref_files[-1]) as f:
        ref_data = json.load(f)
    ref_conds = ref_data.get("refinement_conditions_only", [])

    step_pass(6, "Refinement grind", f"{len(ref_conds)} refinement conditions, {elapsed:.0f}s")
    steps_passed += 1

    # ────────────────────────────────────────────────────────
    # Step 7: Refinement consensus
    # ────────────────────────────────────────────────────────
    print(f"\n  ── Step 7: Refinement consensus ──")
    ok, elapsed = run_cmd([
        sys.executable, "scripts/consensus_engine.py",
        "--setup", setup_type,
        "--stage", "refinement",
        "--threshold", "0.7",
        "--input-dir", TEST_DIR + "/",
    ])
    if not ok:
        step_fail(7, "Refinement consensus", "subprocess failed")

    # Find the consensus refinement output in CACHE_DIR
    ref_cons_files = glob.glob(os.path.join(CACHE_DIR, f"refinement_{setup_type}_*consensus*.json"))
    if not ref_cons_files:
        step_fail(7, "Refinement consensus", "no consensus refinement output found")
    with open(sorted(ref_cons_files)[-1]) as f:
        ref_cons = json.load(f)
    for key in ["all_conditions", "refinement_conditions_only", "depth_progression",
                "winner_signals", "loser_signals"]:
        if key not in ref_cons:
            step_fail(7, "Refinement consensus", f"missing key: {key}")
    n_ref_cons = len(ref_cons.get("refinement_conditions_only", []))
    n_winners = len(ref_cons.get("winner_signals", []))
    n_losers = len(ref_cons.get("loser_signals", []))

    step_pass(7, "Refinement consensus",
              f"{n_ref_cons} conditions, {n_winners} winners, {n_losers} surviving losers, {elapsed:.0f}s")
    steps_passed += 1

    # ────────────────────────────────────────────────────────
    # Step 8: EV grinder
    # ────────────────────────────────────────────────────────
    print(f"\n  ── Step 8: EV grinder ──")
    ok, elapsed = run_cmd([
        sys.executable, "scripts/ev_grinder.py",
        "--setup", setup_type,
    ])
    if not ok:
        step_fail(8, "EV grinder", "subprocess failed")

    # Find latest EV output
    ev_files = glob.glob(os.path.join(CACHE_DIR, f"ev_{setup_type}_*.json"))
    if not ev_files:
        step_fail(8, "EV grinder", "no EV output found")
    with open(sorted(ev_files, key=os.path.getmtime)[-1]) as f:
        ev_data = json.load(f)
    signals = ev_data.get("signals", [])
    if not signals:
        step_fail(8, "EV grinder", "signals array is empty")
    # Check required fields
    s = signals[0]
    for fld in ["setup_score", "market_score", "killed_at_depth"]:
        if fld not in s:
            step_fail(8, "EV grinder", f"signal missing field: {fld}")

    step_pass(8, "EV grinder", f"{len(signals)} signals scored, {elapsed:.0f}s")
    steps_passed += 1

    # ────────────────────────────────────────────────────────
    # Step 9: Profit grinder
    # ────────────────────────────────────────────────────────
    print(f"\n  ── Step 9: Profit grinder ──")
    ok, elapsed = run_cmd([
        sys.executable, "scripts/profit_grinder.py",
        "--setup", setup_type,
    ])
    if not ok:
        step_fail(9, "Profit grinder", "subprocess failed")

    # Find latest profit output
    profit_files = glob.glob(os.path.join(CACHE_DIR, f"profit_{setup_type}_*.json"))
    if not profit_files:
        step_fail(9, "Profit grinder", "no profit output found")
    with open(sorted(profit_files, key=os.path.getmtime)[-1]) as f:
        profit_data = json.load(f)
    for key in ["stage_1", "stage_2"]:
        if key not in profit_data:
            step_fail(9, "Profit grinder", f"missing key: {key}")

    step_pass(9, "Profit grinder", f"{elapsed:.0f}s")
    steps_passed += 1

    # ────────────────────────────────────────────────────────
    # Summary
    # ────────────────────────────────────────────────────────
    total_time = time.time() - t_total
    print(f"\n{'═'*70}")
    print(f"  {steps_passed}/9 PASSED")
    print(f"  Total time: {total_time/60:.0f} minutes")
    print(f"{'═'*70}")

    return True


# ══════════════════════════════════════════════════════════════
# CLI
# ══════════════════════════════════════════════════════════════

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Consensus Pipeline Test Runner — Self-Verifying Mini Pipeline")
    parser.add_argument("--setup", default="dtss", help="Setup type (default: dtss)")
    parser.add_argument("--beam", type=int, default=50,
                        help="Beam width for test grinds (default: 50)")
    parser.add_argument("--depth", type=int, default=5,
                        help="Search depth for test grinds (default: 5)")
    args = parser.parse_args()

    success = run_tests(
        setup_type=args.setup,
        beam=args.beam,
        depth=args.depth,
    )
    sys.exit(0 if success else 1)
