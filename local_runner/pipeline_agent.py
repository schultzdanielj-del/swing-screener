"""
Pipeline Agent — Polls Railway for pipeline jobs, runs scripts locally, streams logs back.

Usage:
    python local_runner/pipeline_agent.py

Leave running alongside agent.py (or replace it). Polls every 5s for pipeline
jobs queued from the Railway dashboard. Runs each step as a subprocess, streams
stdout back to Railway in batches.

This is the bridge between the Railway dashboard UI and your local machine
where all the caches and compute happen.
"""

import os
import sys
import time
import json
import subprocess
import threading
import requests
from datetime import datetime, timezone

LOCAL_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(LOCAL_DIR)
sys.path.insert(0, REPO_ROOT)
sys.path.insert(0, LOCAL_DIR)

API_BASE = "https://web-production-e3025.up.railway.app"
POLL_INTERVAL = 5
LOG_BATCH_INTERVAL = 2  # seconds between log flushes to Railway
LOG_BATCH_SIZE = 50      # max lines per batch

# ============================================================
# Step definitions — command to run for each step_id
# ============================================================
STEP_COMMANDS = {
    "nightly": [sys.executable, os.path.join(LOCAL_DIR, "nightly.py")],

    # ── V2 pipeline steps ──
    # Single-command steps: value is a list of args.
    # Multi-command steps: value is a list of lists (run sequentially; any failure aborts).
    "signal_grind": [
        sys.executable, os.path.join(LOCAL_DIR, "pyramid_grinder.py"),
        "--setup", "dtss", "--peak-target", "3", "--beam", "10000", "--depth", "100",
    ],
    "exit_grind": [
        sys.executable, os.path.join(REPO_ROOT, "scripts", "exit_grinder.py"),
        "--setup", "dtss", "--max-forward", "120",
    ],
    "scan": [
        sys.executable, os.path.join(REPO_ROOT, "scripts", "signal_filter.py"),
        "--setup", "dtss",
    ],
    # refinement_grind is a two-command sequence:
    #   1. Blackout re-grind — re-runs pyramid grinder with post-entry bars masked out
    #   2. Setup refiner — prunes weak conditions + rescans + applies exit filter
    "refinement_grind": [
        [
            sys.executable, os.path.join(LOCAL_DIR, "pyramid_grinder.py"),
            "--setup", "dtss", "--blackout",
            "--peak-target", "3", "--beam", "10000", "--depth", "100",
        ],
        [
            sys.executable, os.path.join(REPO_ROOT, "scripts", "setup_refiner.py"),
            "--setup", "dtss",
        ],
    ],
    "regime": [
        sys.executable, os.path.join(REPO_ROOT, "scripts", "market_grinder.py"),
        "--setup", "dtss",
    ],
    "health": [
        sys.executable, os.path.join(REPO_ROOT, "scripts", "cycle_health.py"),
        "--setup", "dtss",
    ],

    # ── Legacy / utility steps ──
    "multistage_exit": [
        sys.executable, os.path.join(REPO_ROOT, "scripts", "multistage_exit_grinder.py"),
        "--setup", "dtss",
    ],
    "signal_filter": [
        sys.executable, os.path.join(REPO_ROOT, "scripts", "signal_filter.py"),
        "--setup", "dtss",
    ],
    "profit_grinder": [
        sys.executable, os.path.join(REPO_ROOT, "scripts", "profit_grinder.py"),
        "--setup", "dtss", "--max-forward", "120",
    ],
    "outcome_grind": [
        sys.executable, os.path.join(REPO_ROOT, "scripts", "outcome_grinder.py"),
        "--setup", "dtss",
    ],
    "backtest": [
        sys.executable, os.path.join(REPO_ROOT, "scripts", "backtest_runner.py"),
        "--setup", "dtss",
    ],
}


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def ts():
    return datetime.now().strftime("%H:%M:%S")


# ============================================================
# Railway API helpers
# ============================================================
def api_get(path, default=None):
    try:
        r = requests.get(f"{API_BASE}{path}", timeout=15)
        if r.status_code == 200:
            return r.json()
    except Exception as e:
        print(f"  [{ts()}] API GET {path} failed: {e}")
    return default


def api_post(path, data):
    try:
        r = requests.post(f"{API_BASE}{path}", json=data, timeout=15)
        return r.status_code == 200
    except Exception as e:
        print(f"  [{ts()}] API POST {path} failed: {e}")
        return False


def heartbeat():
    api_post("/api/grinder/agent/heartbeat", {
        "agent_id": "desktop",
        "timestamp": now_iso(),
    })


def check_stop_requested(step_id):
    """Check if a stop has been requested for the current job."""
    result = api_get("/api/pipeline/steps")
    if result:
        for j in result.get("jobs", []):
            pass
    # Check the pipeline state directly
    state = api_get("/api/pipeline/steps")
    if state and state.get("running") is None:
        # Job was cleared — might be a stop
        return True
    return False


def poll_pipeline_jobs():
    """Poll for queued pipeline jobs."""
    result = api_get("/api/pipeline/jobs/pending")
    if result:
        return result.get("jobs", [])
    return []


def post_status(step_id, status, **kwargs):
    """Update step status on Railway."""
    api_post("/api/pipeline/status", {
        "step_id": step_id,
        "status": status,
        "timestamp": now_iso(),
        **kwargs,
    })


def flush_logs(step_id, lines):
    """Send a batch of log lines to Railway."""
    if not lines:
        return
    api_post("/api/pipeline/logs", {
        "step_id": step_id,
        "lines": lines,
    })


# ============================================================
# Log streamer — collects subprocess output, flushes to Railway
# ============================================================
class LogStreamer:
    """Thread-safe log buffer that flushes to Railway periodically."""

    def __init__(self, step_id):
        self.step_id = step_id
        self.buffer = []
        self.lock = threading.Lock()
        self.running = True
        self.thread = threading.Thread(target=self._flush_loop, daemon=True)
        self.thread.start()

    def add(self, line):
        with self.lock:
            self.buffer.append(line)

    def _flush_loop(self):
        while self.running:
            time.sleep(LOG_BATCH_INTERVAL)
            self._flush()

    def _flush(self):
        with self.lock:
            if not self.buffer:
                return
            batch = self.buffer[:LOG_BATCH_SIZE]
            self.buffer = self.buffer[LOG_BATCH_SIZE:]

        flush_logs(self.step_id, batch)

        # If there's still more, keep flushing
        with self.lock:
            remaining = len(self.buffer)
        if remaining > 0:
            self._flush()

    def stop(self):
        self.running = False
        self._flush()  # Final flush


# ============================================================
# Step runner
# ============================================================
def extract_summary(output_lines):
    """Pull out key result lines from the output."""
    summary_lines = []
    for line in output_lines[-80:]:
        lower = line.lower().strip()
        if any(kw in lower for kw in [
            "winner", "best:", "result:", "final", "complete",
            "signals", "peak", "conditions", "floor=", "median=",
            "all passes complete", "total signals", "✓",
        ]):
            summary_lines.append(line.strip())
    return "\n".join(summary_lines[-10:]) if summary_lines else None


def run_step(job):
    """Run a pipeline step as a local subprocess.

    STEP_COMMANDS values can be:
      - A flat list: single command, e.g. [sys.executable, "script.py", "--arg"]
      - A list of lists: multi-command sequence, e.g. [[cmd1], [cmd2]]
        Each command runs sequentially. Any non-zero exit code aborts the sequence.
    """
    step_id = job["step_id"]
    job_id = job["job_id"]
    params = job.get("params", {})

    cmd_entry = STEP_COMMANDS.get(step_id)
    if not cmd_entry:
        print(f"  [{ts()}] Unknown step: {step_id}")
        post_status(step_id, "error", error=f"Unknown step: {step_id}")
        return

    # Normalize to list of commands
    if cmd_entry and isinstance(cmd_entry[0], list):
        commands = [list(c) for c in cmd_entry]  # deep copy so we can modify
    else:
        commands = [list(cmd_entry)]

    # ── Inject job params into commands ──
    if step_id == "refinement_grind" and params:
        # Find the setup_refiner.py command in the sequence
        for cmd in commands:
            if any("setup_refiner.py" in str(a) for a in cmd):
                if params.get("skip_prune"):
                    cmd.append("--skip-prune")
                elif params.get("min_power") is not None:
                    cmd.extend(["--min-power", str(params["min_power"])])

    n_cmds = len(commands)
    print(f"\n{'='*60}")
    print(f"  PIPELINE: {step_id}")
    print(f"  Job:      {job_id}")
    if n_cmds > 1:
        print(f"  Commands: {n_cmds} sequential")
        for i, c in enumerate(commands, 1):
            print(f"    [{i}/{n_cmds}] {' '.join(c)}")
    else:
        print(f"  Command:  {' '.join(commands[0])}")
    print(f"  Time:     {ts()}")
    print(f"{'='*60}\n")

    # Notify Railway: running
    post_status(step_id, "running")

    streamer = LogStreamer(step_id)
    all_lines = []
    start_time = time.time()

    try:
        for cmd_idx, cmd in enumerate(commands):
            if n_cmds > 1:
                marker = f"\n── Sub-command {cmd_idx + 1}/{n_cmds}: {' '.join(cmd)}\n"
                print(marker)
                streamer.add(marker)
                all_lines.append(marker)

            process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                cwd=REPO_ROOT,
                env={**os.environ, "PYTHONUNBUFFERED": "1"},
                bufsize=1,
                universal_newlines=True,
            )

            for line in process.stdout:
                # Print locally
                print(f"  | {line}", end="")
                # Buffer for Railway
                streamer.add(line)
                all_lines.append(line)

            process.wait()
            exit_code = process.returncode
            duration = round(time.time() - start_time, 1)

            if exit_code != 0:
                # Abort sequence on first failure
                streamer.stop()
                error_tail = "".join(all_lines[-20:])
                label = f"sub-command {cmd_idx + 1}/{n_cmds}" if n_cmds > 1 else ""
                print(f"\n  ✗ {step_id} {label} failed (exit {exit_code}, {duration}s)")
                post_status(step_id, "error",
                            duration_s=duration, exit_code=exit_code,
                            error=error_tail,
                            result_summary=extract_summary(all_lines))
                return

        # All commands succeeded
        duration = round(time.time() - start_time, 1)
        streamer.stop()
        summary = extract_summary(all_lines)
        print(f"\n  ✓ {step_id} complete ({duration}s)")
        post_status(step_id, "done",
                    duration_s=duration, exit_code=0,
                    result_summary=summary)

    except Exception as e:
        streamer.stop()
        duration = round(time.time() - start_time, 1)
        error_msg = f"{type(e).__name__}: {str(e)}"
        print(f"\n  ✗ {step_id} exception: {error_msg}")
        post_status(step_id, "error",
                    duration_s=duration, error=error_msg)


# ============================================================
# Main loop
# ============================================================
def main():
    print(f"\n{'='*60}")
    print(f"  PIPELINE AGENT")
    print(f"  Polls: {API_BASE}")
    print(f"  Every: {POLL_INTERVAL}s")
    print(f"{'='*60}")

    # Register
    api_post("/api/grinder/agent/register", {
        "agent_id": "desktop",
        "timestamp": now_iso(),
        "status": "online",
    })

    # Retry any pending grind uploads from previous failed attempts
    try:
        from grind_uploader import retry_pending
        retry_pending()
    except Exception as e:
        print(f"  [{ts()}] WARNING: Pending upload retry failed: {e}")

    print(f"\n  [{ts()}] Waiting for pipeline jobs...\n")

    last_heartbeat = time.time()

    while True:
        try:
            # Poll for pipeline jobs
            jobs = poll_pipeline_jobs()
            for job in jobs:
                run_step(job)

            # Heartbeat every 30s
            if time.time() - last_heartbeat > 30:
                heartbeat()
                last_heartbeat = time.time()

            time.sleep(POLL_INTERVAL)

        except KeyboardInterrupt:
            print(f"\n\n  [{ts()}] Agent stopped.")
            api_post("/api/grinder/agent/register", {
                "agent_id": "desktop",
                "timestamp": now_iso(),
                "status": "offline",
            })
            break
        except Exception as e:
            import traceback
            print(f"  [{ts()}] Agent error: {e}")
            traceback.print_exc()
            time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
