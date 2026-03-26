"""
Consensus Pipeline Orchestrator — Parallel Batch Execution.

Chains the full consensus pipeline:
  Steps 1A+1B: Signal grinds (15 real + 15 permuted, parallel batches)
  Step 2:      Signal consensus engine (z-score gate)
  Step 3:      Deterministic scan with locked conditions
  Step 3.5:    Re-grind exit condition on consensus population
  Step 4:      Refinement grinds (10 runs, parallel batches)
  Step 5:      Refinement consensus engine
  Step 6:      EV grinder
  Step 7:      Profit grinder

Usage:
    python scripts/run_consensus_pipeline.py --setup dtss
    python scripts/run_consensus_pipeline.py --setup dtss --parallel 4
    python scripts/run_consensus_pipeline.py --setup dtss --smoke-test --skip-nightly-check
    python scripts/run_consensus_pipeline.py --setup dtss --test-mode --skip-nightly-check
"""

import os
import sys
import json
import time
import glob
import subprocess
import argparse
from datetime import datetime, timezone, date
from itertools import permutations

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOCAL_DIR = os.path.join(REPO_ROOT, "local_runner")
CACHE_DIR = os.path.join(LOCAL_DIR, "cache")
CONSENSUS_DIR = os.path.join(CACHE_DIR, "consensus")
LOG_PATH = os.path.join(CACHE_DIR, "nightly_log.txt")

# Timeout for individual signal grind subprocesses (seconds).
# Prevents outlier permuted runs from blowing the timeline.
# A timed-out run is excluded from consensus, not fatal.
SIGNAL_GRIND_TIMEOUT = 45 * 60  # 45 minutes


# ══════════════════════════════════════════════════════════════
# HELPERS
# ══════════════════════════════════════════════════════════════

def run_cmd(args_list, label, cwd=REPO_ROOT, timeout=None):
    """Run a subprocess, print the command, check for errors.

    Args:
        args_list: command + args
        label: display label
        cwd: working directory
        timeout: max seconds before killing the subprocess. None = no limit.

    Returns:
        (success: bool, elapsed: float, timed_out: bool)
    """
    cmd_str = " ".join(args_list)
    print(f"\n  [{label}] Running: {cmd_str}")
    t0 = time.time()
    try:
        result = subprocess.run(
            args_list, cwd=cwd,
            stdout=sys.stdout, stderr=sys.stderr,
            timeout=timeout,
        )
        elapsed = time.time() - t0
        if result.returncode != 0:
            print(f"\n  \u2717 [{label}] FAILED (exit code {result.returncode}) after {elapsed:.0f}s")
            return False, elapsed, False
        print(f"  \u2713 [{label}] Done in {elapsed:.0f}s")
        return True, elapsed, False
    except subprocess.TimeoutExpired:
        elapsed = time.time() - t0
        print(f"\n  \u26a0 [{label}] TIMED OUT after {elapsed:.0f}s ({timeout}s limit)")
        return False, elapsed, True


def run_parallel_batch(jobs, max_parallel, cwd=REPO_ROOT, timeout=None):
    """Run a list of grind jobs in parallel batches.

    Launches up to max_parallel subprocesses simultaneously.
    Each subprocess writes stdout/stderr to its own log file.
    Waits for all in a batch to finish before starting the next.

    Args:
        jobs: list of dicts with 'cmd', 'label', 'log_path' keys
        max_parallel: max concurrent subprocesses
        cwd: working directory
        timeout: per-subprocess timeout in seconds

    Returns:
        list of result dicts (same order as input jobs)
    """
    results = [None] * len(jobs)
    n_batches = (len(jobs) + max_parallel - 1) // max_parallel

    for batch_i in range(n_batches):
        start = batch_i * max_parallel
        end = min(start + max_parallel, len(jobs))
        batch = jobs[start:end]

        batch_labels = [j["label"] for j in batch]
        print(f"\n  \u2500\u2500 Batch {batch_i+1}/{n_batches}: launching {len(batch)} "
              f"parallel runs \u2500\u2500")
        print(f"     {', '.join(batch_labels)}")

        t_batch = time.time()

        # Launch all subprocesses in this batch simultaneously
        processes = []
        for j in batch:
            log_f = open(j["log_path"], "w")
            proc = subprocess.Popen(
                j["cmd"], cwd=cwd,
                stdout=log_f, stderr=subprocess.STDOUT,
            )
            processes.append({
                "proc": proc, "log_f": log_f,
                "label": j["label"], "log_path": j["log_path"],
                "t0": time.time(),
            })

        # Wait for all processes in this batch to complete
        for p in processes:
            timed_out = False
            try:
                p["proc"].wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                p["proc"].kill()
                p["proc"].wait()
                timed_out = True
            finally:
                p["log_f"].close()

            elapsed = time.time() - p["t0"]
            ok = (p["proc"].returncode == 0) and not timed_out

            status = "\u2713" if ok else ("\u26a0 TIMEOUT" if timed_out else "\u2717 FAIL")
            print(f"     {status} {p['label']} \u2014 {elapsed:.0f}s")

            results[start + processes.index(p)] = {
                "label": p["label"], "ok": ok, "elapsed": elapsed,
                "timed_out": timed_out, "log": p["log_path"],
            }

        batch_wall = time.time() - t_batch

        # Print ETA after each batch
        completed_batches = batch_i + 1
        remaining_batches = n_batches - completed_batches
        if remaining_batches > 0:
            # Use this batch's wall time as estimate for remaining
            eta_s = batch_wall * remaining_batches
            eta_finish = datetime.now().timestamp() + eta_s
            eta_str = datetime.fromtimestamp(eta_finish).strftime("%I:%M %p")
            print(f"\n  \u2500\u2500 Batch took {batch_wall/60:.1f} min. "
                  f"{remaining_batches} batches left. "
                  f"ETA ~{eta_str} \u2500\u2500")

    return results


def check_nightly_refresh():
    """Check if today's nightly refresh completed."""
    if not os.path.exists(LOG_PATH):
        return False, "Nightly log file not found"
    today_str = date.today().strftime("%m/%d/%Y")
    today_iso = date.today().isoformat()
    try:
        with open(LOG_PATH, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
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
                 beam=10000, depth=100, threshold=0.7, max_parallel=4,
                 smoke_test=False):
    """Run the full consensus pipeline."""
    if smoke_test:
        n_signal_runs = 2
        n_refinement_runs = 0
    elif test_mode:
        n_signal_runs = 1
        n_refinement_runs = 1
    else:
        n_signal_runs = 15
        n_refinement_runs = 10

    grind_timeout = None if (test_mode or smoke_test) else SIGNAL_GRIND_TIMEOUT

    print("\n" + "\u2550" * 70)
    print("  CONSENSUS PIPELINE ORCHESTRATOR")
    print("\u2550" * 70)
    print(f"  Setup: {setup_type.upper()}")
    if smoke_test:
        print(f"  Mode: SMOKE TEST (2+2 signal, parallel={max_parallel})")
    elif test_mode:
        print(f"  Mode: TEST (1+1 signal, 1 refinement)")
    else:
        print(f"  Mode: FULL ({n_signal_runs}+{n_signal_runs} signal, "
              f"{n_refinement_runs} refinement, parallel={max_parallel})")
    print(f"  Beam: {beam}, Depth: {depth}")
    print(f"  Consensus threshold: {threshold}")
    print(f"  Max parallel: {max_parallel}")
    print(f"  Output dir: {CONSENSUS_DIR}")
    if grind_timeout:
        print(f"  Signal grind timeout: {grind_timeout // 60} minutes per run")

    t_pipeline = time.time()

    # ── Nightly refresh guard ──
    if not skip_nightly_check:
        ok, msg = check_nightly_refresh()
        if not ok:
            print(f"\n  \u2717 Nightly refresh not completed today: {msg}")
            print(f"    Wait for nightly to finish, or use --skip-nightly-check")
            return False
        print(f"\n  \u2713 Nightly refresh: {msg}")
    else:
        print(f"\n  Skipping nightly refresh check")

    # ── Setup directories ──
    os.makedirs(CONSENSUS_DIR, exist_ok=True)
    logs_dir = os.path.join(CONSENSUS_DIR, "logs")
    os.makedirs(logs_dir, exist_ok=True)

    # ══════════════════════════════════════════════════════════
    # Steps 1A + 1B: Signal grinds (parallel batches)
    # ══════════════════════════════════════════════════════════
    print(f"\n{'=' * 70}")
    print(f"  STEP 1: Signal grinds ({n_signal_runs} real + {n_signal_runs} "
          f"permuted, {max_parallel} parallel)")
    print(f"{'=' * 70}")

    pass_orderings = get_pass_orderings(n_signal_runs)

    # Build all jobs upfront (interleaved: real, perm, real, perm...)
    signal_jobs = []
    total_runs = n_signal_runs * 2
    for run_i in range(total_runs):
        is_permuted = (run_i % 2 == 1)
        run_num = run_i // 2 + 1
        seed = run_i + 1
        ordering = pass_orderings[(run_num - 1) % len(pass_orderings)]
        pass_order_str = ",".join(str(x) for x in ordering)

        label = f"{'Perm' if is_permuted else 'Real'} {run_num}/{n_signal_runs}"

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

        tag = f"{'perm' if is_permuted else 'real'}_{run_num:02d}"
        log_path = os.path.join(logs_dir, f"signal_{tag}.log")
        signal_jobs.append({
            "cmd": cmd, "label": label, "log_path": log_path,
            "is_permuted": is_permuted,
        })

    # Run in parallel batches (or sequential for test mode)
    if test_mode:
        signal_results = []
        for job in signal_jobs:
            ok, elapsed, timed_out = run_cmd(
                job["cmd"], job["label"], timeout=grind_timeout)
            signal_results.append({
                "label": job["label"], "ok": ok, "elapsed": elapsed,
                "timed_out": timed_out, "log": None,
            })
    else:
        signal_results = run_parallel_batch(
            signal_jobs, max_parallel=max_parallel,
            timeout=grind_timeout)

    # ── Collect condition counts ──
    real_counts = []
    perm_counts = []
    timed_out_runs = []
    failed_runs = []

    for i, r in enumerate(signal_results):
        is_permuted = signal_jobs[i]["is_permuted"]
        if r["timed_out"]:
            timed_out_runs.append(r["label"])
            continue
        if not r["ok"]:
            failed_runs.append(r["label"])
            continue

        prefix = "permuted" if is_permuted else "pyramid"
        pattern = os.path.join(CONSENSUS_DIR,
                               f"{prefix}_{setup_type}_mp_*.json")
        files = sorted(glob.glob(pattern), key=os.path.getmtime)
        if files:
            n_conds = count_conditions_in_file(files[-1])
            if is_permuted:
                perm_counts.append(n_conds)
            else:
                real_counts.append(n_conds)

    # ── Step 1 summary ──
    print(f"\n{'-' * 70}")
    print(f"  STEP 1 COMPLETE")
    print(f"{'-' * 70}")
    print(f"  Real runs completed: {len(real_counts)}/{n_signal_runs}")
    print(f"  Permuted runs completed: {len(perm_counts)}/{n_signal_runs}")
    if real_counts:
        print(f"  Real conditions: mean={sum(real_counts)/len(real_counts):.1f}, "
              f"range={min(real_counts)}-{max(real_counts)}")
    if perm_counts:
        print(f"  Permuted conditions: mean={sum(perm_counts)/len(perm_counts):.1f}, "
              f"range={min(perm_counts)}-{max(perm_counts)}")
    if timed_out_runs:
        print(f"  Timed out: {len(timed_out_runs)} runs "
              f"({', '.join(timed_out_runs)})")
    if failed_runs:
        print(f"  Failed: {len(failed_runs)} runs "
              f"({', '.join(failed_runs)})")

    # ── Smoke test: stop here ──
    if smoke_test:
        total_time = time.time() - t_pipeline
        print(f"\n{'=' * 70}")
        print(f"  SMOKE TEST COMPLETE \u2014 {total_time/60:.1f} min")
        print(f"{'=' * 70}")
        print(f"  {len(real_counts)} real + {len(perm_counts)} permuted "
              f"completed successfully")
        if real_counts and perm_counts:
            print(f"  Real avg: {sum(real_counts)/len(real_counts):.1f} conditions")
            print(f"  Perm avg: {sum(perm_counts)/len(perm_counts):.1f} conditions")
            # Extrapolate full run time
            successful = [r for r in signal_results if r["ok"]]
            if successful:
                max_elapsed = max(r["elapsed"] for r in successful)
                full_runs = 30
                full_batches = (full_runs + max_parallel - 1) // max_parallel
                est_full = max_elapsed * full_batches
                print(f"\n  Full run estimate: {full_batches} batches x "
                      f"{max_elapsed/60:.1f} min = ~{est_full/60:.0f} min "
                      f"({est_full/3600:.1f} hours)")
        if failed_runs or timed_out_runs:
            print(f"\n  ISSUES: {len(failed_runs)} failed, "
                  f"{len(timed_out_runs)} timed out")
            print(f"  Check logs in: {logs_dir}")
        return len(failed_runs) == 0 and len(timed_out_runs) == 0

    # Minimum viable data check
    if len(real_counts) < 3:
        print(f"\n  PIPELINE STOPPED: Only {len(real_counts)} real runs "
              f"completed (need >= 3).")
        return False
    if len(perm_counts) < 3:
        print(f"\n  PIPELINE STOPPED: Only {len(perm_counts)} permuted runs "
              f"completed (need >= 3).")
        return False

    # ══════════════════════════════════════════════════════════
    # Step 2: Signal consensus engine
    # ══════════════════════════════════════════════════════════
    print(f"\n{'=' * 70}")
    print(f"  STEP 2: Signal consensus engine")
    print(f"{'=' * 70}")

    consensus_cmd = [
        sys.executable, "scripts/consensus_engine.py",
        "--setup", setup_type,
        "--stage", "signal",
        "--threshold", str(threshold),
        "--input-dir", CONSENSUS_DIR + "/",
    ]
    if test_mode:
        consensus_cmd.extend(["--min-permuted", "1"])

    ok, _, _ = run_cmd(consensus_cmd, "Signal consensus")
    if not ok:
        print(f"\n  PIPELINE STOPPED: Consensus engine failed.")
        return False

    # Check gate decision
    consensus_path = os.path.join(CACHE_DIR,
                                   f"consensus_signal_{setup_type}.json")
    if not os.path.exists(consensus_path):
        gate_path = os.path.join(CONSENSUS_DIR,
                                  f"consensus_gate_{setup_type}.json")
        if os.path.exists(gate_path):
            with open(gate_path) as f:
                gate_data = json.load(f)
            z = gate_data.get("z_score", 0)
            print(f"\n  \u2717 z-score gate FAILED (z = {z:.2f})")
            print(f"    Vet more examples and re-run.")
            return False
        print(f"\n  \u2717 No consensus output found. Check output above.")
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

    # ══════════════════════════════════════════════════════════
    # Step 3: Deterministic scan
    # ══════════════════════════════════════════════════════════
    print(f"\n{'=' * 70}")
    print(f"  STEP 3: Deterministic scan with consensus conditions")
    print(f"{'=' * 70}")

    ok, _, _ = run_cmd([
        sys.executable, "local_runner/pyramid_grinder.py",
        "--setup", setup_type,
        "--scan-only",
        "--conditions-file", consensus_path,
    ], "Deterministic scan")
    if not ok:
        print(f"\n  PIPELINE STOPPED: Scan failed.")
        return False

    # ══════════════════════════════════════════════════════════
    # Step 3.5: Re-grind exit condition
    # ══════════════════════════════════════════════════════════
    print(f"\n{'=' * 70}")
    print(f"  STEP 3.5: Re-grind exit condition on consensus population")
    print(f"{'=' * 70}")

    ok, _, _ = run_cmd([
        sys.executable, "scripts/signal_exit_grinder.py",
        "--setup", setup_type,
        "--conditions-file", consensus_path,
    ], "Exit re-grind")
    if not ok:
        print(f"\n  PIPELINE STOPPED: Exit re-grind failed.")
        return False

    # ══════════════════════════════════════════════════════════
    # Step 4: Refinement grinds (parallel batches)
    # ══════════════════════════════════════════════════════════
    print(f"\n{'=' * 70}")
    print(f"  STEP 4: Refinement grinds ({n_refinement_runs} runs, "
          f"{max_parallel} parallel)")
    print(f"{'=' * 70}")

    ref_jobs = []
    for ref_i in range(n_refinement_runs):
        seed = ref_i + 1
        label = f"Refine {ref_i+1}/{n_refinement_runs}"
        cmd = [
            sys.executable, "local_runner/pyramid_grinder.py",
            "--setup", setup_type,
            "--blackout",
            "--skip-gather",
            "--subsample-losers",
            "--seed", str(seed),
            "--conditions-file", consensus_path,
            "--output-dir", CONSENSUS_DIR + "/",
        ]
        log_path = os.path.join(logs_dir, f"refinement_{ref_i+1:02d}.log")
        ref_jobs.append({"cmd": cmd, "label": label, "log_path": log_path})

    if n_refinement_runs == 1:
        ok, _, _ = run_cmd(ref_jobs[0]["cmd"], ref_jobs[0]["label"])
        if not ok:
            print(f"\n  PIPELINE STOPPED: Refinement run failed.")
            return False
    elif n_refinement_runs > 1:
        ref_results = run_parallel_batch(
            ref_jobs, max_parallel=max_parallel)
        ref_failed = [r for r in ref_results if not r["ok"]]
        if ref_failed:
            print(f"\n  PIPELINE STOPPED: {len(ref_failed)} refinement "
                  f"run(s) failed.")
            for r in ref_failed:
                print(f"    \u2717 {r['label']} \u2014 check {r['log']}")
            return False

    # ══════════════════════════════════════════════════════════
    # Step 5: Refinement consensus
    # ══════════════════════════════════════════════════════════
    print(f"\n{'=' * 70}")
    print(f"  STEP 5: Refinement consensus engine")
    print(f"{'=' * 70}")

    ok, _, _ = run_cmd([
        sys.executable, "scripts/consensus_engine.py",
        "--setup", setup_type,
        "--stage", "refinement",
        "--threshold", str(threshold),
        "--input-dir", CONSENSUS_DIR + "/",
    ], "Refinement consensus")
    if not ok:
        print(f"\n  PIPELINE STOPPED: Refinement consensus failed.")
        return False

    # ══════════════════════════════════════════════════════════
    # Step 6: EV grinder
    # ══════════════════════════════════════════════════════════
    print(f"\n{'=' * 70}")
    print(f"  STEP 6: EV grinder")
    print(f"{'=' * 70}")

    ok, _, _ = run_cmd([
        sys.executable, "scripts/ev_grinder.py",
        "--setup", setup_type,
    ], "EV grinder")
    if not ok:
        print(f"\n  PIPELINE STOPPED: EV grinder failed.")
        return False

    # ══════════════════════════════════════════════════════════
    # Step 7: Profit grinder
    # ══════════════════════════════════════════════════════════
    print(f"\n{'=' * 70}")
    print(f"  STEP 7: Profit grinder")
    print(f"{'=' * 70}")

    ok, _, _ = run_cmd([
        sys.executable, "scripts/profit_grinder.py",
        "--setup", setup_type,
    ], "Profit grinder")
    if not ok:
        print(f"\n  PIPELINE STOPPED: Profit grinder failed.")
        return False

    # ══════════════════════════════════════════════════════════
    # Summary report
    # ══════════════════════════════════════════════════════════
    total_time = time.time() - t_pipeline
    print(f"\n{'=' * 70}")
    print(f"  CONSENSUS PIPELINE COMPLETE")
    print(f"{'=' * 70}")
    print(f"  Total time: {total_time/60:.0f} minutes "
          f"({total_time/3600:.1f} hours)")
    print(f"  z-score: {z:.2f}")
    print(f"  Signal conditions: {n_locked}")
    if timed_out_runs:
        print(f"  Timed out: {len(timed_out_runs)} "
              f"({', '.join(timed_out_runs)})")
    if failed_runs:
        print(f"  Failed: {len(failed_runs)} "
              f"({', '.join(failed_runs)})")

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
        "max_parallel": max_parallel,
        "timed_out_runs": timed_out_runs,
        "failed_runs": failed_runs,
        "real_condition_counts": real_counts,
        "perm_condition_counts": perm_counts,
        "test_mode": test_mode,
    }
    summary_path = os.path.join(CACHE_DIR,
                                 f"consensus_complete_{setup_type}.json")
    with open(summary_path, "w") as f:
        json.dump(summary, f, indent=2)
    print(f"\n  Summary: {summary_path}")
    print(f"  \u2713 Pipeline complete. Review results in ScanPerfect.")

    return True


# ══════════════════════════════════════════════════════════════
# CLI
# ══════════════════════════════════════════════════════════════

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Consensus Pipeline Orchestrator \u2014 Parallel Batch Execution")
    parser.add_argument("--setup", default="dtss",
                        help="Setup type (default: dtss)")
    parser.add_argument("--test-mode", action="store_true",
                        help="Mini run: 1+1 signal + 1 refinement "
                             "(sequential, not parallel)")
    parser.add_argument("--smoke-test", action="store_true",
                        help="Quick verify: 2+2 signal grinds in one "
                             "parallel batch, then stop. ~2 min.")
    parser.add_argument("--skip-nightly-check", action="store_true",
                        help="Skip the nightly refresh completion check")
    parser.add_argument("--beam", type=int, default=10000,
                        help="Beam width for signal grinds (default: 10000)")
    parser.add_argument("--depth", type=int, default=100,
                        help="Search depth for signal grinds (default: 100)")
    parser.add_argument("--threshold", type=float, default=0.7,
                        help="Consensus threshold (default: 0.7)")
    parser.add_argument("--parallel", type=int, default=4,
                        help="Max parallel grind subprocesses (default: 4). "
                             "Each needs ~3-4 GB RAM.")
    args = parser.parse_args()

    success = run_pipeline(
        setup_type=args.setup,
        test_mode=args.test_mode,
        skip_nightly_check=args.skip_nightly_check,
        beam=args.beam,
        depth=args.depth,
        threshold=args.threshold,
        max_parallel=args.parallel,
        smoke_test=args.smoke_test,
    )
    sys.exit(0 if success else 1)
