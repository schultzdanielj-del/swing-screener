"""
Consensus Pipeline Orchestrator — Overnight Unattended Run.

Chains the full consensus pipeline as one overnight run:
  Steps 1A+1B: Signal grinds (15 real + 15 permuted, interleaved)
  Step 2:      Signal consensus engine (z-score gate)
  Step 3:      Deterministic scan with locked conditions
  Step 3.5:    Re-grind exit condition on consensus population
  Step 4:      Refinement grinds (10 runs with loser subsampling)
  Step 5:      Refinement consensus engine
  Step 6:      EV grinder
  Step 7:      Profit grinder

Usage:
    python scripts/run_consensus_pipeline.py --setup dtss
    python scripts/run_consensus_pipeline.py --setup dtss --test-mode --skip-nightly-check
"""

import os
import sys
import json
import time
import subprocess
import argparse
from datetime import datetime, timezone, date
from itertools import permutations

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOCAL_DIR = os.path.join(REPO_ROOT, "local_runner")
CACHE_DIR = os.path.join(LOCAL_DIR, "cache")
CONSENSUS_DIR = os.path.join(CACHE_DIR, "consensus")
LOG_PATH = os.path.join(CACHE_DIR, "nightly_log.txt")


# ══════════════════════════════════════════════════════════════
# HELPERS
# ══════════════════════════════════════════════════════════════

def run_cmd(args_list, label, cwd=REPO_ROOT):
    """Run a subprocess, print the command, check for errors."""
    cmd_str = " ".join(args_list)
    print(f"\n  [{label}] Running: {cmd_str}")
    t0 = time.time()
    result = subprocess.run(
        args_list, cwd=cwd,
        stdout=sys.stdout, stderr=sys.stderr,
    )
    elapsed = time.time() - t0
    if result.returncode != 0:
        print(f"\n  ✗ [{label}] FAILED (exit code {result.returncode}) after {elapsed:.0f}s")
        return False, elapsed
    print(f"  ✓ [{label}] Done in {elapsed:.0f}s")
    return True, elapsed


def check_nightly_refresh():
    """Check if today's nightly refresh completed."""
    if not os.path.exists(LOG_PATH):
        return False, "Nightly log file not found"
    today_str = date.today().strftime("%m/%d/%Y")
    # Also check YYYY-MM-DD format
    today_iso = date.today().isoformat()
    try:
        with open(LOG_PATH, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
        # Look for completion line with today's date
        for line in content.split("\n"):
            if "Nightly refresh complete" in line:
                if today_str in line or today_iso in line:
                    return True, line.strip()
    except Exception as e:
        return False, f"Error reading log: {e}"
    return False, "No completion entry for today"


def get_pass_orderings(n):
    """Generate n pass orderings cycling through all 6 permutations."""
    all_orderings = list(permutations([1, 2, 3]))
    return [all_orderings[i % len(all_orderings)] for i in range(n)]


def early_abort_check(real_counts, perm_counts):
    """Check if real vs permuted separation is sufficient to continue.

    Returns (should_continue, message).
    Threshold: (mean_real - mean_perm) / mean_perm >= 0.5
    """
    mean_real = sum(real_counts) / len(real_counts)
    mean_perm = sum(perm_counts) / len(perm_counts)

    if mean_perm == 0:
        return True, f"Permuted mean is 0 — strong separation (real mean: {mean_real:.0f})"

    ratio = (mean_real - mean_perm) / mean_perm

    if ratio < 0.5:
        return False, (
            f"Early abort: separation too small.\n"
            f"  Real mean: {mean_real:.1f} conditions/run\n"
            f"  Permuted mean: {mean_perm:.1f} conditions/run\n"
            f"  Ratio: {ratio:.2f} (need >= 0.50)\n"
            f"  Vet more examples and re-run."
        )
    else:
        return True, (
            f"Separation looks viable.\n"
            f"  Real mean: {mean_real:.1f} conditions/run\n"
            f"  Permuted mean: {mean_perm:.1f} conditions/run\n"
            f"  Ratio: {ratio:.2f} (>= 0.50 threshold)"
        )


def count_conditions_in_file(path):
    """Read a grind output JSON and return the number of conditions."""
    try:
        with open(path) as f:
            data = json.load(f)
        return len(data.get("all_conditions", []))
    except Exception:
        return 0


# ══════════════════════════════════════════════════════════════
# MAIN PIPELINE
# ══════════════════════════════════════════════════════════════

def run_pipeline(setup_type, test_mode=False, skip_nightly_check=False,
                 beam=10000, depth=100, threshold=0.7):
    """Run the full consensus pipeline."""
    n_signal_runs = 1 if test_mode else 15
    n_refinement_runs = 1 if test_mode else 10

    print("\n" + "═" * 70)
    print("  CONSENSUS PIPELINE ORCHESTRATOR")
    print("═" * 70)
    print(f"  Setup: {setup_type.upper()}")
    print(f"  Mode: {'TEST (1+1 signal, 1 refinement)' if test_mode else f'FULL ({n_signal_runs}+{n_signal_runs} signal, {n_refinement_runs} refinement)'}")
    print(f"  Beam: {beam}, Depth: {depth}")
    print(f"  Consensus threshold: {threshold}")
    print(f"  Output dir: {CONSENSUS_DIR}")

    t_pipeline = time.time()

    # ── Nightly refresh guard ──
    if not skip_nightly_check:
        ok, msg = check_nightly_refresh()
        if not ok:
            print(f"\n  ✗ Nightly refresh not completed today: {msg}")
            print(f"    Wait for nightly to finish, or use --skip-nightly-check")
            return False
        print(f"\n  ✓ Nightly refresh: {msg}")
    else:
        print(f"\n  Skipping nightly refresh check")

    # ── Setup consensus directory ──
    os.makedirs(CONSENSUS_DIR, exist_ok=True)

    # ── Steps 1A + 1B: Signal grinds (interleaved real/permuted) ──
    print(f"\n{'═'*70}")
    print(f"  STEP 1: Signal grinds ({n_signal_runs} real + {n_signal_runs} permuted)")
    print(f"{'═'*70}")

    pass_orderings = get_pass_orderings(n_signal_runs)
    real_counts = []
    perm_counts = []
    real_times = []
    perm_times = []

    total_runs = n_signal_runs * 2
    for run_i in range(total_runs):
        is_permuted = (run_i % 2 == 1)
        run_num = run_i // 2 + 1  # 1-indexed within real or permuted
        seed = run_i + 1
        ordering = pass_orderings[(run_num - 1) % len(pass_orderings)]
        pass_order_str = ",".join(str(x) for x in ordering)

        label = f"{'Permuted' if is_permuted else 'Real'} {run_num}/{n_signal_runs}"
        print(f"\n  ── Run {run_i+1}/{total_runs}: {label} (seed={seed}, order={pass_order_str}) ──")

        cmd = [
            sys.executable, "local_runner/pyramid_grinder.py",
            "--setup", setup_type,
            "--beam", str(beam),
            "--depth", str(depth),
            "--seed", str(seed),
            "--subsample", "0.5",
            "--zero-margin",
            "--no-peak-target",
            "--pass-order", pass_order_str,
            "--output-dir", CONSENSUS_DIR + "/",
        ]
        if is_permuted:
            cmd.append("--permute")

        ok, elapsed = run_cmd(cmd, label)
        if not ok:
            print(f"\n  PIPELINE STOPPED: Run failed.")
            return False

        # Count conditions in the output
        import glob
        prefix = "permuted" if is_permuted else "pyramid"
        pattern = os.path.join(CONSENSUS_DIR, f"{prefix}_{setup_type}_mp_*.json")
        files = sorted(glob.glob(pattern), key=os.path.getmtime)
        if files:
            n_conds = count_conditions_in_file(files[-1])
            if is_permuted:
                perm_counts.append(n_conds)
                perm_times.append(elapsed)
            else:
                real_counts.append(n_conds)
                real_times.append(elapsed)
            print(f"  Conditions: {n_conds}")

        # ETA after first 2 runs
        if run_i == 1:
            avg_time = (sum(real_times) + sum(perm_times)) / (len(real_times) + len(perm_times))
            remaining = total_runs - 2
            eta_s = avg_time * remaining
            eta_finish = datetime.now().timestamp() + eta_s
            eta_str = datetime.fromtimestamp(eta_finish).strftime("%I:%M %p")
            print(f"\n  ── ETA: {avg_time:.0f}s/run × {remaining} remaining = "
                  f"{eta_s/60:.0f} min. Finish ~{eta_str} ──")

        # Early abort checkpoint after 3+3 (6 runs done)
        if not test_mode and run_i == 5 and len(real_counts) >= 3 and len(perm_counts) >= 3:
            should_continue, msg = early_abort_check(real_counts[:3], perm_counts[:3])
            print(f"\n  ── EARLY ABORT CHECKPOINT ──")
            print(f"  {msg}")
            if not should_continue:
                abort_report = {
                    "setup_type": setup_type,
                    "status": "EARLY_ABORT",
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "real_counts": real_counts,
                    "perm_counts": perm_counts,
                    "message": msg,
                }
                abort_path = os.path.join(CONSENSUS_DIR, f"consensus_abort_{setup_type}.json")
                with open(abort_path, "w") as f:
                    json.dump(abort_report, f, indent=2)
                print(f"  Abort report: {abort_path}")
                return False

    # ── Step 2: Signal consensus engine ──
    print(f"\n{'═'*70}")
    print(f"  STEP 2: Signal consensus engine")
    print(f"{'═'*70}")

    consensus_cmd = [
        sys.executable, "scripts/consensus_engine.py",
        "--setup", setup_type,
        "--stage", "signal",
        "--threshold", str(threshold),
        "--input-dir", CONSENSUS_DIR + "/",
    ]
    if test_mode:
        consensus_cmd.extend(["--min-permuted", "1"])

    ok, _ = run_cmd(consensus_cmd, "Signal consensus")
    if not ok:
        print(f"\n  PIPELINE STOPPED: Consensus engine failed.")
        return False

    # Check gate decision
    consensus_path = os.path.join(CACHE_DIR, f"consensus_signal_{setup_type}.json")
    if not os.path.exists(consensus_path):
        # Check for gate report
        gate_path = os.path.join(CONSENSUS_DIR, f"consensus_gate_{setup_type}.json")
        if os.path.exists(gate_path):
            with open(gate_path) as f:
                gate = json.load(f)
            z = gate.get("z_score", 0)
            print(f"\n  ✗ z-score gate FAILED (z = {z:.2f})")
            print(f"    Vet more examples and re-run.")
            return False
        print(f"\n  ✗ No consensus output found. Check consensus engine output above.")
        return False

    with open(consensus_path) as f:
        consensus = json.load(f)
    z = consensus.get("z_score", 0)
    n_locked = consensus.get("n_conditions", 0)
    gate = consensus.get("gate_decision", "STOP")
    print(f"\n  z-score: {z:.2f}, gate: {gate}, locked conditions: {n_locked}")

    if gate == "STOP":
        print(f"  PIPELINE STOPPED: z < 2. Vet more examples.")
        return False

    # ── Step 3: Deterministic scan ──
    print(f"\n{'═'*70}")
    print(f"  STEP 3: Deterministic scan with consensus conditions")
    print(f"{'═'*70}")

    ok, _ = run_cmd([
        sys.executable, "local_runner/pyramid_grinder.py",
        "--setup", setup_type,
        "--scan-only",
        "--conditions-file", consensus_path,
    ], "Deterministic scan")
    if not ok:
        print(f"\n  PIPELINE STOPPED: Scan failed.")
        return False

    # ── Step 3.5: Re-grind exit condition ──
    print(f"\n{'═'*70}")
    print(f"  STEP 3.5: Re-grind exit condition on consensus population")
    print(f"{'═'*70}")

    ok, _ = run_cmd([
        sys.executable, "scripts/signal_exit_grinder.py",
        "--setup", setup_type,
        "--conditions-file", consensus_path,
    ], "Exit re-grind")
    if not ok:
        print(f"\n  PIPELINE STOPPED: Exit re-grind failed.")
        return False

    # ── Step 4: Refinement grinds ──
    print(f"\n{'═'*70}")
    print(f"  STEP 4: Refinement grinds ({n_refinement_runs} runs)")
    print(f"{'═'*70}")

    for ref_i in range(n_refinement_runs):
        seed = ref_i + 1
        label = f"Refinement {ref_i+1}/{n_refinement_runs}"
        ok, _ = run_cmd([
            sys.executable, "local_runner/pyramid_grinder.py",
            "--setup", setup_type,
            "--blackout",
            "--skip-gather",
            "--subsample-losers",
            "--seed", str(seed),
            "--conditions-file", consensus_path,
            "--output-dir", CONSENSUS_DIR + "/",
        ], label)
        if not ok:
            print(f"\n  PIPELINE STOPPED: Refinement run {ref_i+1} failed.")
            return False

    # ── Step 5: Refinement consensus ──
    print(f"\n{'═'*70}")
    print(f"  STEP 5: Refinement consensus engine")
    print(f"{'═'*70}")

    ref_consensus_cmd = [
        sys.executable, "scripts/consensus_engine.py",
        "--setup", setup_type,
        "--stage", "refinement",
        "--threshold", str(threshold),
        "--input-dir", CONSENSUS_DIR + "/",
    ]

    ok, _ = run_cmd(ref_consensus_cmd, "Refinement consensus")
    if not ok:
        print(f"\n  PIPELINE STOPPED: Refinement consensus failed.")
        return False

    # ── Step 6: EV grinder ──
    print(f"\n{'═'*70}")
    print(f"  STEP 6: EV grinder")
    print(f"{'═'*70}")

    ok, _ = run_cmd([
        sys.executable, "scripts/ev_grinder.py",
        "--setup", setup_type,
    ], "EV grinder")
    if not ok:
        print(f"\n  PIPELINE STOPPED: EV grinder failed.")
        return False

    # ── Step 7: Profit grinder ──
    print(f"\n{'═'*70}")
    print(f"  STEP 7: Profit grinder")
    print(f"{'═'*70}")

    ok, _ = run_cmd([
        sys.executable, "scripts/profit_grinder.py",
        "--setup", setup_type,
    ], "Profit grinder")
    if not ok:
        print(f"\n  PIPELINE STOPPED: Profit grinder failed.")
        return False

    # ── Summary report ──
    total_time = time.time() - t_pipeline
    print(f"\n{'═'*70}")
    print(f"  CONSENSUS PIPELINE COMPLETE")
    print(f"{'═'*70}")
    print(f"  Total time: {total_time/60:.0f} minutes ({total_time/3600:.1f} hours)")
    print(f"  z-score: {z:.2f}")
    print(f"  Signal conditions: {n_locked}")

    # Write summary
    summary = {
        "setup_type": setup_type,
        "status": "COMPLETE",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "total_time_s": round(total_time, 1),
        "z_score": z,
        "signal_conditions": n_locked,
        "gate_decision": gate,
        "n_signal_runs_real": len(real_counts),
        "n_signal_runs_permuted": len(perm_counts),
        "n_refinement_runs": n_refinement_runs,
        "test_mode": test_mode,
    }
    summary_path = os.path.join(CACHE_DIR, f"consensus_complete_{setup_type}.json")
    with open(summary_path, "w") as f:
        json.dump(summary, f, indent=2)
    print(f"\n  Summary: {summary_path}")
    print(f"  ✓ Pipeline complete. Review results in ScanPerfect.")

    return True


# ══════════════════════════════════════════════════════════════
# CLI
# ══════════════════════════════════════════════════════════════

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Consensus Pipeline Orchestrator — Overnight Unattended Run")
    parser.add_argument("--setup", default="dtss", help="Setup type (default: dtss)")
    parser.add_argument("--test-mode", action="store_true",
                        help="Mini run: 1+1 signal + 1 refinement (instead of 15+15+10)")
    parser.add_argument("--skip-nightly-check", action="store_true",
                        help="Skip the nightly refresh completion check")
    parser.add_argument("--beam", type=int, default=10000,
                        help="Beam width for signal grinds (default: 10000)")
    parser.add_argument("--depth", type=int, default=100,
                        help="Search depth for signal grinds (default: 100)")
    parser.add_argument("--threshold", type=float, default=0.7,
                        help="Consensus threshold (default: 0.7)")
    args = parser.parse_args()

    success = run_pipeline(
        setup_type=args.setup,
        test_mode=args.test_mode,
        skip_nightly_check=args.skip_nightly_check,
        beam=args.beam,
        depth=args.depth,
        threshold=args.threshold,
    )
    sys.exit(0 if success else 1)
