"""
Research Dispatcher — Autonomous Claude Code research sessions.

Polls Railway for research jobs. For each job:
  1. Creates a sandbox git branch (research/job-NNN)
  2. Launches Claude Code with the prompt + full repo context
  3. Claude Code can modify code, run grinds via task queue, analyze results
  4. On completion, posts summary + diff + results to Railway
  5. v2 branch is never touched

Usage:
    python scripts/research_dispatcher.py

Leave running alongside the agent. Post jobs from anywhere:
    POST https://web-production-e3025.up.railway.app/api/v2/research
    {"prompt": "Find a way to reduce signal count without losing edge"}

Hard rails:
    - All work on sandbox branch (v2 untouched)
    - Examples are benched, not deleted (reversible)
    - Max runtime per job: 2 hours
    - Claude Code has full repo read/write access on the sandbox branch
"""

import os
import sys
import json
import time
import subprocess
import requests
from datetime import datetime, timezone

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
API_BASE = "https://web-production-e3025.up.railway.app"
POLL_INTERVAL = 15  # seconds
MAX_RUNTIME = 7200  # 2 hours per job

# Force UTF-8 on Windows
if sys.platform == 'win32':
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')


def log(msg):
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"  [{ts}] {msg}")


def update_job(job_id, **kwargs):
    try:
        requests.patch(f"{API_BASE}/api/v2/research/{job_id}", json=kwargs, timeout=15)
    except Exception as e:
        log(f"WARNING: failed to update job {job_id}: {e}")


def poll_for_job():
    try:
        r = requests.get(f"{API_BASE}/api/v2/research/pending", timeout=10)
        if r.status_code == 200:
            jobs = r.json().get("jobs", [])
            return jobs[0] if jobs else None
    except:
        pass
    return None


def create_sandbox_branch(job_id):
    """Create a sandbox branch from v2 for this research job."""
    branch = f"research/job-{job_id:03d}"
    # Make sure we're on v2 and up to date
    subprocess.run(["git", "checkout", "v2"], cwd=REPO_ROOT, capture_output=True)
    subprocess.run(["git", "pull"], cwd=REPO_ROOT, capture_output=True)
    # Create and switch to sandbox branch
    subprocess.run(["git", "checkout", "-b", branch], cwd=REPO_ROOT, capture_output=True)
    return branch


def cleanup_sandbox(branch):
    """Switch back to v2 after research. Don't delete branch — keep for review."""
    subprocess.run(["git", "checkout", "v2"], cwd=REPO_ROOT, capture_output=True)


def get_diff(branch):
    """Get the diff between v2 and the research branch."""
    result = subprocess.run(
        ["git", "diff", "v2", branch, "--stat"],
        cwd=REPO_ROOT, capture_output=True, text=True
    )
    stat = result.stdout.strip()
    result2 = subprocess.run(
        ["git", "diff", "v2", branch],
        cwd=REPO_ROOT, capture_output=True, text=True
    )
    full_diff = result2.stdout.strip()
    # Truncate if huge
    if len(full_diff) > 50000:
        full_diff = full_diff[:50000] + "\n\n... (truncated, full diff on branch)"
    return f"{stat}\n\n{full_diff}"


SYSTEM_PROMPT = """You are a research engineer working on the swing-screener trading system.
You have full access to the repository on a sandbox git branch. The v2 branch is untouched.

YOUR ENVIRONMENT:
- You're in the repo root directory
- You can read/edit any file, run any script
- The agent is running and will execute tasks you post to the task queue
- All grind results are mirrored to Railway at {api_base}
- The expression cache and OHLCV cache are local (don't rebuild these)

TASK QUEUE (for running grinds):
  Post: curl -s -X POST "{api_base}/api/v2/tasks" -H "Content-Type: application/json" -d '{{"command":"signal_grind","args":{{"setup":"dtss"}}}}'
  Poll: curl -s "{api_base}/api/v2/tasks/{{id}}"
  Available commands: signal_grind, signal_grind_blackout, exit_grind, scan, outlier_analysis, regime_model, health_check

GRIND RESULTS (on Railway):
  List: curl -s "{api_base}/api/v2/files?prefix=local_runner/cache/pyramid_dtss"
  Get:  curl -s "{api_base}/api/v2/files/{{path}}"

EXAMPLES:
  List: curl -s "{api_base}/api/examples/dtss"
  DO NOT permanently delete examples. If you want to test without an example, bench it:
    1. Save its full data to a bench file (data/research_bench_job_NNN.json)
    2. Delete from active set via API
    3. Record in your findings so it can be restored

KEY FILES TO READ FIRST:
  - PIPELINE_V2.md (pipeline spec)
  - ta_knowledge.md (TA concepts)
  - local_runner/pyramid_grinder.py (grinder code)
  - local_runner/spiderweb.py (beam search)
  - TODO.md (current state)

RULES:
  - Commit your changes to the sandbox branch as you go
  - Don't touch the v2 branch
  - Don't rebuild the expression cache
  - If a grind takes too long, move on to the next idea
  - Document everything — your reasoning, what you tried, what worked/didn't

WHEN YOU'RE DONE:
Write a file called RESEARCH_FINDINGS.md in the repo root with:
  1. What you tried (each experiment)
  2. Results (signal counts, condition counts, before/after)
  3. Recommendations (what Dan should keep, merge, or discard)
  4. Any examples you benched and why
Then commit it to the sandbox branch.
"""


def build_prompt(job):
    """Build the full prompt for Claude Code."""
    prompt = job["prompt"]
    job_id = job["id"]

    return f"""RESEARCH JOB #{job_id}

PROMPT FROM DAN:
{prompt}

Read the key files listed in your system prompt first, then form a plan, then execute.
When done, write RESEARCH_FINDINGS.md and commit to this branch.
"""


def run_research_job(job):
    """Execute a research job via Claude Code."""
    job_id = job["id"]
    prompt = job["prompt"]

    log(f"Starting research job #{job_id}")
    log(f"Prompt: {prompt[:100]}...")

    # Create sandbox branch
    branch = create_sandbox_branch(job_id)
    log(f"Sandbox branch: {branch}")
    update_job(job_id, status="running", branch=branch)

    # Build Claude Code command
    system = SYSTEM_PROMPT.format(api_base=API_BASE)
    user_prompt = build_prompt(job)

    is_win = sys.platform == "win32"

    # Find claude command
    claude_cmd = None
    for cmd in ["claude", "claude.exe", "npx"]:
        try:
            if cmd == "npx":
                test = subprocess.run(
                    ["npx", "@anthropic-ai/claude-code", "--version"],
                    capture_output=True, text=True, timeout=10, shell=is_win
                )
            else:
                test = subprocess.run(
                    [cmd, "--version"],
                    capture_output=True, text=True, timeout=10, shell=is_win
                )
            if test.returncode == 0:
                claude_cmd = cmd
                break
        except (FileNotFoundError, subprocess.TimeoutExpired):
            continue

    if not claude_cmd:
        error = "Claude Code CLI not found (tried claude, claude.exe, npx)"
        log(error)
        update_job(job_id, status="failed", error=error)
        cleanup_sandbox(branch)
        return

    # Build CLI args
    if claude_cmd == "npx":
        cli_args = ["npx", "@anthropic-ai/claude-code",
                     "-p", user_prompt,
                     "--system-prompt", system]
    else:
        cli_args = [claude_cmd,
                     "-p", user_prompt,
                     "--system-prompt", system]

    log(f"Launching Claude Code...")

    try:
        proc = subprocess.Popen(
            cli_args, cwd=REPO_ROOT,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1,
            encoding='utf-8', errors='replace',
            shell=is_win,
        )

        output_lines = []
        start = time.time()

        for line in proc.stdout:
            line = line.rstrip()
            print(f"    {line}")
            output_lines.append(line)

            # Enforce max runtime
            if time.time() - start > MAX_RUNTIME:
                log(f"Max runtime ({MAX_RUNTIME}s) exceeded — killing")
                proc.terminate()
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()
                break

        proc.wait()
        duration = time.time() - start
        log(f"Claude Code finished in {duration:.0f}s (exit {proc.returncode})")

        # Capture output
        full_log = "\n".join(output_lines)
        # Truncate log if huge
        if len(full_log) > 100000:
            full_log = full_log[:50000] + "\n\n...(truncated)...\n\n" + full_log[-50000:]

        # Get diff
        diff = get_diff(branch)

        # Try to read RESEARCH_FINDINGS.md if it was created
        findings_path = os.path.join(REPO_ROOT, "RESEARCH_FINDINGS.md")
        summary = ""
        if os.path.exists(findings_path):
            with open(findings_path, "r", encoding="utf-8", errors="replace") as f:
                summary = f.read()

        # Check for benched examples
        bench_path = os.path.join(REPO_ROOT, "data", f"research_bench_job_{job_id:03d}.json")
        benched = ""
        if os.path.exists(bench_path):
            with open(bench_path, "r") as f:
                benched = f.read()

        # Update job
        status = "completed" if proc.returncode == 0 else "failed"
        update_job(
            job_id,
            status=status,
            summary=summary[:10000] if summary else f"Completed in {duration:.0f}s. Check branch {branch}.",
            diff=diff[:50000],
            examples_benched=benched[:5000] if benched else None,
            log=full_log[:100000],
            error=f"Exit code {proc.returncode}" if proc.returncode != 0 else None,
        )

        log(f"Job #{job_id} {status}. Branch: {branch}")

    except Exception as e:
        import traceback
        error_msg = f"{type(e).__name__}: {e}"
        log(f"Job #{job_id} error: {error_msg}")
        traceback.print_exc()
        update_job(job_id, status="failed", error=error_msg)

    finally:
        cleanup_sandbox(branch)


def main():
    print("\n" + "=" * 60)
    print("  RESEARCH DISPATCHER")
    print("=" * 60)
    print(f"  API:      {API_BASE}")
    print(f"  Poll:     every {POLL_INTERVAL}s")
    print(f"  Max time: {MAX_RUNTIME}s per job")
    print(f"  Repo:     {REPO_ROOT}")
    print(f"\n  Waiting for research jobs...\n")

    while True:
        try:
            job = poll_for_job()
            if job:
                run_research_job(job)
            time.sleep(POLL_INTERVAL)
        except KeyboardInterrupt:
            print("\n\n  Dispatcher stopped.")
            break
        except Exception as e:
            log(f"Dispatcher error: {e}")
            time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
