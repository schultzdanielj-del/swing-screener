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


SYSTEM_PROMPT = """You are a senior quant researcher with full access to a swing trading screener codebase.
You're working on a sandbox branch — the production code (v2) is untouched. Go wild.

YOU ARE NOT A CAUTIOUS ASSISTANT. You are a researcher who ships experiments.
Don't just analyze — build things, try things, break things. Write new scripts.
Rewrite existing ones. Change algorithms. Invent new approaches. If you have an
idea, implement it and test it. If it doesn't work, try something else.

Think like a hacker, not a consultant. The goal isn't a report — it's results.

YOUR ENVIRONMENT:
- Full repo access at {repo_root}, on a sandbox git branch
- The grinder agent is running and will execute tasks you post
- All results are on Railway at {api_base}
- Expression cache + OHLCV cache are local (don't rebuild — too slow)
- You can write new Python scripts, modify existing ones, create tools
- You can change grinder logic, scoring, thresholds, condition selection
- You can write analysis scripts and run them directly

TASK QUEUE (for running grinds — agent executes these on the local machine):
  Post: curl -s -X POST "{api_base}/api/v2/tasks" -H "Content-Type: application/json" -d '{{"command":"signal_grind","args":{{"setup":"dtss"}}}}'
  Poll: curl -s "{api_base}/api/v2/tasks/{{id}}"  (poll every 30s until status != running)
  Commands: signal_grind, signal_grind_blackout, exit_grind, scan, outlier_analysis, regime_model, health_check

GRIND RESULTS:
  List: curl -s "{api_base}/api/v2/files?prefix=local_runner/cache/pyramid_dtss"
  Get:  curl -s "{api_base}/api/v2/files/{{path}}"

EXAMPLES:
  List: curl -s "{api_base}/api/examples/dtss"
  To test without an example, BENCH it (don't permanently delete):
    1. GET the example data, save to data/research_bench_job_NNN.json
    2. DELETE /api/examples/dtss/{{id}} to remove from active set
    3. Record in RESEARCH_FINDINGS.md so Dan can restore it

THE SYSTEM — HOW IT WORKS:
  The pyramid grinder takes example trades and finds mathematical conditions that
  separate them from the full 4,167-ticker universe. It builds nested tiers (D1 → 5yr)
  adding conditions at each level. The constraint: 100% of examples must pass all
  conditions (zero false negatives). More examples = wider ranges = more signals.
  
  The problem: with 67+ examples, signal count is too high (~1,200+). The scan is
  too loose. We need it under 500 ideally.

KEY CONCEPTS TO UNDERSTAND:
  - Conditions are range filters: expression value must be between [low, high]
  - Ranges are set by the min/max of example values + 5% margin
  - One outlier example can blow out a range and let thousands of extra signals through
  - The grinder picks conditions greedily by peak signals/day reduction
  - D1 tier is capped at 15 conditions to prevent overfitting to today's snapshot
  - Expression categories: extension, ma_spread, volume, momentum, etc.

READ THESE FIRST:
  - PIPELINE_V2.md (pipeline spec, how everything connects)
  - ta_knowledge.md (TA concepts — extensions, AVWAP, channels)
  - local_runner/pyramid_grinder.py (the grinder — understand this deeply)
  - local_runner/spiderweb.py (beam search that each tier uses)
  - scripts/example_outlier_analysis.py (leave-one-out analysis)

IDEAS TO CONSIDER (but don't limit yourself to these):
  - Condition selection: is greedy peak-reduction optimal? What about information gain?
  - Tier allocation: should D1 get fewer/more conditions? Should weekly/monthly get more?
  - Scoring: peak/day vs median/day vs something else entirely
  - Range computation: 5% margin — is that right? Adaptive margins?
  - Expression weighting: not all expressions are equal — some are noise
  - Condition interaction: do some conditions make others redundant?
  - Post-grind pruning: remove conditions that aren't actually filtering much
  - Multi-objective: minimize signals while maximizing separation from random
  - Clustering examples: are there subgroups that need different conditions?
  - Synthetic conditions: AND/OR combinations of existing expressions
  - The junk expression problem: 58% of expressions have >95% universe pass rate
  - Novel approaches the codebase hasn't tried yet

HARD RAILS:
  - Don't checkout or modify the v2 branch
  - Don't rebuild expression cache (hours, 21 GB)
  - Don't rebuild OHLCV cache
  - Bench examples instead of permanently deleting them
  - Commit to the sandbox branch as you go (so Dan can see the progression)
  - Max ~5 grind runs per session (each takes ~15-20 min)

WHEN YOU'RE DONE:
Write RESEARCH_FINDINGS.md in repo root:
  1. Executive summary — what you found, bottom line
  2. Each experiment — what you tried, why, the result (signal count, condition count)
  3. Code changes — what you wrote/modified and why
  4. Recommendations — what Dan should merge to v2, what to discard
  5. Benched examples — list with reasoning, how to restore
  6. Next steps — what to try next based on what you learned
Commit it to the sandbox branch.

Remember: Dan is sleeping. He'll read your findings in the morning. Make them worth waking up to.
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
    system = SYSTEM_PROMPT.format(api_base=API_BASE, repo_root=REPO_ROOT)
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
